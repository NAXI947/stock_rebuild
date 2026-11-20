"""Frozen formal sector/index facts used by ETF V3.2."""
from decimal import Decimal
from hashlib import sha256
import json
import os

VERSION = "etf-representativeness-v2"
PROMPT_VERSION = "etf-representativeness-v1"  # prompt wording is unchanged by v2
METHOD = "etf-direction-first-v3"
MODES = {"V2", "V3_SHADOW", "V3_ENFORCE"}
PARAMETERS = (
    ("weak_exposure_floor", "0.03"),
    ("single_stock_guard_cap", "0.10"),
    ("single_stock_guard_min_members", 10),
    ("core_exposure_tier_floor", "0.15"),
    ("broad_sector_min_members", 300),
)
REJECTIONS = frozenset((
    "NO_OVERLAP_CONFIRMED", "WEAK_OVERLAP_REJECTED",
    "SINGLE_STOCK_CROSSOVER_REJECTED", "WEAK_OVERLAP_UPPER_BOUND_REJECTED",
    "BROAD_SECTOR_UNATTRIBUTED",
))
ZERO = Decimal(0)
ONE = Decimal(1)


def freeze_policy(mapping, cutoff, *, mode=None):
    if mode is None:
        mode = os.environ.get("ETF_REPRESENTATIVENESS_MODE", "V2")
    if mode not in MODES:
        raise ValueError("invalid ETF_REPRESENTATIVENESS_MODE")
    parameters = dict(PARAMETERS)
    return dict(policy_mode=mode, policy_version=VERSION, parameters=parameters,
                parameter_digest=sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest(),
                mapping_version=mapping["version"], data_cutoff=cutoff.isoformat(),
                taxonomy_version=(mapping.get("etf_taxonomy") or {}).get("version"),
                prompt_version=PROMPT_VERSION if mode == "V3_ENFORCE" else "scoped-facts-v1")


def evaluate_pair_representativeness(frozen_sector_members, known_weights, *,
                                     sector_members_complete, weights_complete_and_valid,
                                     partial_upper_bound_proven=False, explicit_relation=False,
                                     attributed=False, policy=None):
    parameters = dict(PARAMETERS) if policy is None else policy["parameters"]
    floor = Decimal(parameters["weak_exposure_floor"])
    guard = Decimal(parameters["single_stock_guard_cap"])
    core = Decimal(parameters["core_exposure_tier_floor"])
    members = set(frozen_sector_members)
    complete = bool(known_weights and sector_members_complete and weights_complete_and_valid)
    match = positive = exposure = upper = reverse = None
    basis, tier = "UNAVAILABLE", "UNKNOWN"
    status = "REPRESENTATIVENESS_UNKNOWN"
    associated = explicit_relation
    if known_weights:
        intersection = members.intersection(known_weights)
        match = len(intersection)
        positive = sum(known_weights[code] > ZERO for code in intersection)
        exposure = sum((known_weights[code] for code in sorted(intersection)), ZERO)
        associated = associated or bool(intersection)
        basis = "COMPLETE" if complete else "KNOWN_LOWER_BOUND"
        tier = ("CORE_REPRESENTATIVE" if exposure >= core else
                "LOW_EXPOSURE_CAUTION" if exposure >= floor else "BELOW_FLOOR")
        if complete:
            upper = exposure
            reverse = Decimal(match) / len(members) if members else None
            if exposure == ZERO:
                status = "NO_OVERLAP_CONFIRMED"
            elif exposure < floor:
                status = "WEAK_OVERLAP_REJECTED"
            elif exposure < guard and len(members) >= parameters["single_stock_guard_min_members"] and positive == 1:
                status = "SINGLE_STOCK_CROSSOVER_REJECTED"
            else:
                status = "RELEVANCE_NOT_REJECTED"
        else:
            total = sum(known_weights.values(), ZERO)
            if sector_members_complete and partial_upper_bound_proven and ZERO < total <= ONE:
                upper = exposure + (ONE - total)
            if upper is not None and upper < floor:
                status = "WEAK_OVERLAP_UPPER_BOUND_REJECTED"
            elif exposure >= floor:
                status = "RELEVANCE_NOT_REJECTED"
    # Membership overlap cannot tell a related index from an unrelated one once a concept
    # sector holds a large part of the market, so entry there needs a reviewed attribution.
    broad = parameters.get("broad_sector_min_members")
    if (status not in REJECTIONS and broad is not None and len(members) >= broad
            and not (attributed or explicit_relation)):
        status = "BROAD_SECTOR_UNATTRIBUTED"
    if status not in REJECTIONS and not associated:
        status = "ASSOCIATION_UNCONFIRMED"
    reasons = [] if status == "RELEVANCE_NOT_REJECTED" else [status]
    if explicit_relation and status == "NO_OVERLAP_CONFIRMED":
        reasons.append("EXPLICIT_RELATION_CONFLICT")
    return dict(matched_count=match, positive_weight_matched_count=positive, sector_member_count=len(members),
                known_exposure=str(exposure) if exposure is not None else None,
                max_possible_exposure=str(upper) if upper is not None else None,
                reverse_coverage=str(reverse) if reverse is not None else None,
                upper_bound_status="PROVEN" if upper is not None else "UNPROVEN",
                exposure_basis=basis, exposure_tier=tier, representativeness_status=status,
                entry_allowed=associated and status not in REJECTIONS, reason_codes=reasons)


def _fields(value, fields):
    if not isinstance(value, dict) or value.keys() != fields.keys() or any(
            not isinstance(value[key], allowed) or (allowed is int and type(value[key]) is not int)
            for key, allowed in fields.items()):
        raise ValueError("invalid ETF shadow fields")


_PAIR_FIELDS = {
    **dict.fromkeys(("matched_count", "positive_weight_matched_count"), (int, type(None))),
    **dict.fromkeys(("known_exposure", "max_possible_exposure", "reverse_coverage", "source_time"), (str, type(None))),
    **dict.fromkeys(("upper_bound_status", "exposure_basis", "exposure_tier", "representativeness_status",
                     "sector_id", "etf", "index", "policy_mode", "policy_version", "parameter_digest",
                     "mapping_version"), str),
    **dict.fromkeys(("entry_allowed", "explicit_relation", "weight_available", "weight_complete"), bool),
    "sector_member_count": int, "reason_codes": list, "fact_refs": list,
}

_STATISTIC_FIELDS = {
    "sectors": dict.fromkeys(("total", "with_quotes", "with_members", "with_relations", "with_selectable",
                              "effective_up", "with_ai_selection", "stripped_empty"), int),
    "pairs": dict(total=int, explicit=int, with_intersection=int, unproven_upper_bound=int, by_status=dict,
                  selectable=int, selectable_by_tier=dict),
    "indexes": dict.fromkeys(("total", "weights_available", "partial", "missing_weights", "with_verified_product"), int),
    "products": dict(total=int, selectable=int, ai_selected=int, rule_status=dict, execution_ready=int),
    "orders": dict(total=int, by_action=dict, by_status=dict, by_block=dict),
}

_SUPPORT_FIELDS = {
    **dict.fromkeys(("group_id", "index", "family_id", "etf", "taxonomy_version", "mapping_version",
                     "phase_basis", "exposure_basis", "union_reference"), str),
    **dict.fromkeys(("union_exposure", "report_id"), (str, type(None))),
    **dict.fromkeys(("source_sector_ids", "source_fact_refs"), list),
    **dict.fromkeys(("branch_count", "up_branch_count", "selected_up_branch_count"), int),
    **dict.fromkeys(("direction_conflicts", "excluded_sources"), dict),
}


def _validate_pairs(pairs):
    for sector, entries in pairs.items():
        if not isinstance(entries, dict) or not isinstance(sector, str):
            raise ValueError("invalid ETF shadow pair group")
        for code, pair in entries.items():
            _fields(pair, _PAIR_FIELDS)
            if (pair["sector_id"] != sector or pair["etf"] != code
                    or any(not isinstance(item, str) for item in pair["reason_codes"] + pair["fact_refs"])):
                raise ValueError("invalid ETF shadow pair")


def validate_shadow_fields(stage, result):
    if stage == "PREPARE":
        _validate_pairs(result)
    elif stage == "RECOMMEND":
        _fields(result, dict(policy=dict, pairs=dict, mapping=dict, recommendations=dict, quote_sectors=list))
        recommendations = result["recommendations"]
        _fields(recommendations, dict(method=str, automatic_method=str, mapping_version=str,
                                     market_snapshot=(str, type(None)), sectors=dict,
                                     mapping_scope=dict, candidates=list))
        for row in recommendations["candidates"]:
            _fields(row, dict(etf=str, sector_id=str, rank=int, mapping_ref=str))
        _fields(recommendations["mapping_scope"], dict(sectors=int, products=int, indexes=int))
    elif stage == "FINALIZE":
        _fields(result, dict(policy=dict, pairs=dict, current_support=list, statistics=dict))
        _validate_pairs(result["pairs"])
        stats = result["statistics"]
        _fields(stats, dict.fromkeys(_STATISTIC_FIELDS, dict))
        for name, fields in _STATISTIC_FIELDS.items():
            _fields(stats[name], fields)
        _fields(stats["pairs"]["selectable_by_tier"], dict(core=int, below_core=int, unknown=int))
        for group, name in (("pairs", "by_status"), ("products", "rule_status"),
                            ("orders", "by_action"), ("orders", "by_status"), ("orders", "by_block")):
            if any(not isinstance(key, str) or type(count) is not int
                   for key, count in stats[group][name].items()):
                raise ValueError("invalid ETF shadow counters")
        for group in result["current_support"]:
            _fields(group, _SUPPORT_FIELDS)
            for source in group["source_fact_refs"]:
                _fields(source, dict(sector_id=str, refs=list))
    else:
        raise ValueError("unknown ETF shadow stage")


def shadow_result(stage, operation):
    """Run a shadow implementation on its caller-provided private input."""
    try:
        result = operation()
        if not isinstance(result, dict):
            raise ValueError("invalid ETF shadow result")
        json.dumps(result, allow_nan=False)
        validate_shadow_fields(stage, result)
    except Exception as exc:
        return None, dict(stage=stage, error=type(exc).__name__)
    return result, None
