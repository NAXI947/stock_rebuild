"""Point-in-time ETF support projection from ledger-effective sector decisions."""
from collections import Counter, defaultdict
from decimal import Decimal

from ..ledger import fingerprint
from ..simulator import moment
from .penetration import _dated_weights, _formal_members, product_entry_reason, research_products


def support_facts(mapping, pairs, policy, *, published=None, recommendations=None):
    taxonomy = mapping.get("etf_taxonomy") or {}
    if not policy.get("taxonomy_version") or taxonomy.get("version") != policy["taxonomy_version"]:
        return []
    at = moment(policy["data_cutoff"])
    groups = defaultdict(list)
    seen = set()
    for row in taxonomy.get("rows", ()):
        sector, index = row["sector_id"], row["index_code"]
        if moment(row["available_at"]) > at or not row.get("evidence_refs"):
            continue
        key = sector, index
        if key in seen:
            raise ValueError("duplicate effective ETF taxonomy pair")
        seen.add(key)
        for code, fact in pairs.get(sector, {}).items():
            if fact["index"] == index:
                groups[index, row["family_id"], code].append((row, fact))
    effective = published["sectors"] if published is not None else {}
    result = []
    for (index, family, code), sources in sorted(groups.items()):
        branches, selected, stocks = defaultdict(set), set(), set()
        included, excluded, supporting = [], {}, []
        members_complete = True
        for row, fact in sorted(sources, key=lambda item: item[0]["sector_id"]):
            sid, branch = row["sector_id"], row["canonical_branch_id"]
            if not fact["entry_allowed"]:
                excluded[sid] = fact["representativeness_status"]
                continue
            included.append(fact)
            state = effective.get(sid, {}).get("state", "UNKNOWN")
            branches[branch].add(state)
            if published is not None and state not in ("UP", "HOLD"):
                continue
            supporting.append(fact)
            members, complete = _formal_members(mapping, sid, at)
            stocks.update(members)
            members_complete = members_complete and complete
            if state == "UP" and any(item["etf"] == code and item["representative_current"]
                    for item in (recommendations or {}).get("sectors", {}).get(sid, {}).get("products", ())):
                selected.add(branch)
        conflicts = {branch: sorted(states) for branch, states in branches.items()
                     if "UP" in states and states.intersection(("WEAK", "EXIT"))}
        raw = mapping.get("weights", {}).get(index)
        weights = _dated_weights(raw, at)
        exposure, basis = None, "UNAVAILABLE"
        if weights is not None and supporting:
            exposure = sum((weights[2][stock] for stock in sorted(stocks.intersection(weights[2]))), Decimal(0))
            basis = "COMPLETE" if members_complete and raw.get("complete") is True else "KNOWN_LOWER_BOUND"
        result.append(dict(
            group_id=fingerprint([mapping["version"], taxonomy["version"], index, family]),
            index=index, family_id=family, etf=code, taxonomy_version=taxonomy["version"],
            mapping_version=mapping["version"], report_id=published["id"] if published is not None else None,
            phase_basis="PUBLISHED_EFFECTIVE" if published is not None else "PREPARED_STATIC",
            source_sector_ids=[f["sector_id"] for f in included],
            source_fact_refs=[dict(sector_id=f["sector_id"], refs=f["fact_refs"]) for f in included],
            branch_count=len(branches), up_branch_count=sum("UP" in states for states in branches.values()),
            selected_up_branch_count=len(selected), direction_conflicts=conflicts, excluded_sources=excluded,
            union_exposure=str(exposure) if exposure is not None else None, exposure_basis=basis,
            union_reference=("MET" if exposure is not None and exposure >= Decimal(".40") else
                             "BELOW" if basis == "COMPLETE" else "UNKNOWN")))
    return result


def attach_static_support(options, mapping, pairs, policy):
    groups = defaultdict(list)
    for group in support_facts(mapping, pairs, policy):
        for sid in group["source_sector_ids"]:
            groups[sid, group["etf"]].append(group)
    for sid, context in options.items():
        visible = [group for row in context["products"] for group in groups[sid, row["etf"]]]
        if visible:
            context["support_facts"] = visible[:8]
            context["omitted_support_count"] = max(0, len(visible)-8)


def coverage_statistics(mapping, pairs, published, recommendations, orders, at, quote_sectors=()):
    products = research_products(mapping, at)
    indexes = {p["index_code"] for p in products.values()}
    weights = {idx: (mapping.get("weights", {}).get(idx) or {}) for idx in indexes}
    available = {idx for idx, raw in weights.items() if _dated_weights(raw, at) is not None}
    facts = [fact for entries in pairs.values() for fact in entries.values()]
    allowed = [f for f in facts if f["entry_allowed"]]
    rows = list(recommendations.get("sectors", {}).values())
    selected = {code for row in rows for code in row["representatives"]}
    return dict(
        sectors=dict(total=len(pairs), with_quotes=len(set(quote_sectors).intersection(pairs)),
                     with_members=sum(bool(_formal_members(mapping, sid, at)[0]) for sid in pairs),
                     with_relations=sum(bool(row) for row in pairs.values()),
                     with_selectable=len({f["sector_id"] for f in allowed}),
                     effective_up=sum(v["state"] == "UP" for v in published["sectors"].values()),
                     with_ai_selection=sum(bool(row["representatives"]) for row in rows),
                     stripped_empty=sum((row.get("selection_audit") or {}).get("reason")
                                        == "EMPTY_AFTER_WEAK_PAIR_STRIP" for row in rows)),
        pairs=dict(total=len(facts), explicit=sum(f["explicit_relation"] for f in facts),
                   with_intersection=sum(bool(f["matched_count"]) for f in facts),
                   unproven_upper_bound=sum(f["upper_bound_status"] == "UNPROVEN" for f in facts),
                   by_status=dict(Counter(f["representativeness_status"] for f in facts)),
                   selectable=len(allowed),
                   selectable_by_tier=dict(core=sum(f["exposure_tier"] == "CORE_REPRESENTATIVE" for f in allowed),
                       below_core=sum(f["exposure_tier"] not in ("CORE_REPRESENTATIVE", "UNKNOWN") for f in allowed),
                       unknown=sum(f["exposure_tier"] == "UNKNOWN" for f in allowed))),
        indexes=dict(total=len(indexes), weights_available=len(available),
                     partial=sum(weights[idx].get("complete") is not True for idx in available),
                     missing_weights=len(indexes-available),
                     with_verified_product=len({p["index_code"] for p in products.values()
                                                if product_entry_reason(p, at) is None})),
        products=dict(total=len(products), selectable=len({f["etf"] for f in allowed}), ai_selected=len(selected),
                      rule_status=dict(Counter(product_entry_reason(p, at) or "VERIFIED" for p in products.values())),
                      execution_ready=len({p["etf"] for row in rows for p in row["products"]
                                           if p["execution_eligibility"] == "READY" and p["ai_selected"]})),
        orders=dict(total=len(orders), by_action=dict(Counter(o[0] for o in orders)),
                    by_status=dict(Counter(o[1] for o in orders)),
                    by_block=dict(Counter(o[2] for o in orders if o[2]))))


def finalize_support_facts(conn, ns, frozen, published, recommendations):
    mapping, pairs, policy = frozen["mapping"], frozen["pairs"], frozen["policy"]
    orders = conn.execute(
        """SELECT action,status,CASE WHEN status <> 'PENDING' THEN NULL
           WHEN evidence->>'last_block' IS NOT NULL THEN evidence->>'last_block'
           WHEN action <> 'BUY' AND evidence->>'eligible_at' IS NOT NULL
             AND (evidence->>'eligible_at')::timestamptz > %s::timestamptz THEN 'WAIT_T1'
           ELSE 'WAIT_PRICE' END
           FROM simulation_orders
           WHERE ns=%s AND evidence->>'report_id'=%s""", (published["at"], ns, published["id"])).fetchall()
    return dict(policy=policy, pairs=pairs,
                current_support=support_facts(mapping, pairs, policy, published=published,
                                              recommendations=recommendations),
                statistics=coverage_statistics(mapping, pairs, published, recommendations, orders,
                                               moment(policy["data_cutoff"]), frozen.get("quote_sectors", ())))
