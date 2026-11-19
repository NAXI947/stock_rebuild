"""Scheduled OCTOP inputs and v1 report assembly on the worker side."""
from datetime import datetime
from hashlib import sha256
import json
import logging

from ..adapter import BOARD, unique_object
from ..ledger import encode
from ..simulator import CHINA, moment
from .octop_client import OctopRejected
from .octop_decision import render
from .penetration import number
from .stock_research import for_sector, breadth
from .discovery import select_directions
from .research_facts import NUMERIC_CONTRACT, ETF_SELECTION_CONTRACT, fact_catalog, market_facts

LOG = logging.getLogger(__name__)
MAX_RESEARCH_SECTORS = 6
MAX_PRIOR_REPORTS = 8
PROVIDER_ERRORS = frozenset({
    "APIConnectionError", "APITimeoutError", "APIStatusError", "AuthenticationError",
    "BadRequestError", "ConnectError", "ConnectTimeout", "ConnectionError",
    "HTTPStatusError", "InternalServerError", "PermissionDeniedError", "PoolTimeout",
    "RateLimitError", "ReadError", "ReadTimeout", "RemoteProtocolError", "TimeoutError",
    "UnprocessableEntityError", "ValueError", "WriteError", "WriteTimeout",
    "INCOMPLETE_MODEL_OUTPUT", "INVALID_MODEL_JSON", "OCTOP_SKILL_DISABLED", "UNSUPPORTED_MODEL_PROTOCOL",
})


def _log_attempt_failure(request, *, reply=None, transport_error=None):
    if reply is None:
        code = "BRIDGE_TIMEOUT" if isinstance(transport_error, TimeoutError) else "BRIDGE_CONNECTION"
        requests, status, retryable = None, None, True
    else:
        reported = reply.get("error_type")
        code = reported if isinstance(reported, str) and reported in PROVIDER_ERRORS else "UNCLASSIFIED_REPLY"
        requests = reply.get("requests")
        if type(requests) is not int or requests not in (0, 1):
            requests = None
        statuses = reply.get("http_status")
        status = statuses[0] if isinstance(statuses, list) and len(statuses) == 1 else None
        if type(status) is not int or not 100 <= status <= 599:
            status = None
        retryable = reply.get("retryable") is True
    usage = (reply or {}).get("usage")
    usage = usage if isinstance(usage, dict) else {}
    LOG.warning("OCTOP analysis attempt failed %s", encode(dict(
        trade_date=request["trade_date"], slot=request["round_slot"], error=code,
        http_requests=requests, http_status=status, retryable=retryable,
        usage={key: value for key, value in usage.items()
               if key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens")
               and type(value) is int and 0 <= value <= 100000000},
        finish_reason=(reply or {}).get("finish_reason")
            if (reply or {}).get("finish_reason") in ("stop", "length", "content_filter", "tool_calls") else None,
    )))


def board_observation(row, cutoff):
    try:
        at = moment(row["observed_at"])
        sid, batch = row["sector_id"], row["source_batch"]
        change = number(row["return_ratio"])
        if (not BOARD.fullmatch(sid) or not isinstance(batch, str) or not 1 <= len(batch) <= 256 or change is None
                or at.date() != cutoff.date() or at > cutoff):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return dict(ref=f"{sid}@{at.isoformat()}#{batch}", observed_at=at.isoformat(),
                board_return_ratio=str(change), relative_strength=row.get("strength"))


def mx_items(reply, cutoff):
    if (reply.get("status") != "READY" or reply.get("requests") != 1
            or reply.get("http_status") != 200 or reply.get("source") != "octop:mx-search"):
        return []
    items = []
    for row in reply["items"]:
        try:
            original = row["published_at"]
            published = datetime.fromisoformat(original)
            if published.tzinfo is None:
                published = published.replace(tzinfo=CHINA)
            published = published.astimezone(CHINA)
            title = row["title"]
            if published > cutoff or not isinstance(title, str) or not title.strip():
                continue
        except (ValueError, TypeError, KeyError):
            continue
        digest = sha256(encode([title, original, row.get("url")]).encode()).hexdigest()
        items.append(dict(ref="mx:"+digest, observed_at=published.isoformat(), original_published_at=original,
                          title=title, source=row.get("source"), url=row.get("url")))
    return items


class OctopAnalyst:
    def __init__(self, ledger, mapping, market, client, cutoff):
        self.ledger, self.mapping, self.market = ledger, mapping, market
        self.client, self.cutoff = client, cutoff.astimezone(CHINA)

    def _prior(self, current):
        """Read bounded observation history independently of active trade signals."""
        if not current:
            return {}
        with self.ledger._connect() as conn:
            rows = conn.execute(
                """SELECT at,payload FROM events WHERE ns=%s AND kind='report' AND at<%s
                   ORDER BY at DESC,id DESC LIMIT %s""",
                (self.ledger.ns, self.cutoff.isoformat(), MAX_PRIOR_REPORTS),
            )
            prior = {}
            for published_at, payload in rows:
                report = json.loads(payload) if isinstance(payload, str) else payload
                history = report.get("input_meta", {}).get("octop", {}).get("market_evidence", {})
                for sid, latest in current.items():
                    before = history.get(sid)
                    if not isinstance(before, dict):
                        continue
                    try:
                        observed = moment(before["observed_at"])
                        ref = before["ref"]
                        if (not isinstance(ref, str) or not ref or number(before.get("board_return_ratio")) is None
                                or observed >= self.cutoff or observed > moment(published_at)
                                or (latest and (ref == latest["ref"]
                                                or observed >= moment(latest["observed_at"])))):
                            continue
                    except (KeyError, TypeError, ValueError):
                        continue
                    if sid not in prior or observed > moment(prior[sid]["observed_at"]):
                        prior[sid] = before
            return prior

    def prepare(self, request, budget):
        boards = {row["sector_id"]: row for row in (self.market.body.get("sectors") or [])
                  if isinstance(row, dict) and isinstance(row.get("sector_id"), str)} if self.market else {}
        active = request["active_directions"]
        tracked = set(request["tracked_sectors"])
        mapped = self.mapping["sectors"]
        ids, discovery = select_directions(boards, active, tracked, self.cutoff)
        current_by_sector = {sid: board_observation(boards.get(sid, {}), self.cutoff) for sid in ids}
        prior = self._prior(current_by_sector)
        evidence = {}
        for sid in ids:
            row = boards.get(sid, {})
            current = current_by_sector[sid]
            before = prior.get(sid)
            previous = [before] if before else []
            name = row.get("sector_name") or mapped.get(sid, {}).get("sector_name") or sid
            quality = self.market.body.get("quality") or {} if self.market else {}
            flags = []
            if quality.get("iopv") in ("NOT_COLLECTED", "UNAVAILABLE", "CONFIG_UNAVAILABLE", "EMPTY_SCOPE"):
                flags.append("IOPV_MISSING")
            if quality.get("stock_quotes") == "NOT_COLLECTED":
                flags.append("STOCK_QUOTES_MISSING")
            stocks = for_sector(self.market, sid, request["round_slot"], self.cutoff) if current else []
            if not stocks and quality.get("stock_quotes") in ("AVAILABLE", "UNAVAILABLE"):
                flags.append("STOCK_QUOTES_MISSING")
            if sid not in mapped:
                flags.append("SECTOR_MEMBERS_UNAVAILABLE")
            participation = breadth(self.market, self.mapping, sid, request["round_slot"], self.cutoff)
            facts = dict(board_return_ratio=current["board_return_ratio"] if current else None,
                         breadth=participation, quality=flags)
            if current and before and moment(before["observed_at"]).date() == moment(current["observed_at"]).date():
                facts.update(previous_board_return_ratio=before["board_return_ratio"],
                             board_return_change_ratio=str(number(current["board_return_ratio"])
                                                           - number(before["board_return_ratio"])),
                             comparison_observed_at=before["observed_at"], comparison_ref=before["ref"])
            evidence[sid] = dict(sector_id=sid, sector_name=name, trade_date=request["trade_date"],
                                 data_as_of=self.market.observed_at if self.market else None,
                                 prior_active=sid in active, held=sid in tracked,
                                 previous=previous, current=[current] if current else [], facts=facts)
            if stocks:
                evidence[sid]["stock_evidence"] = stocks

        # One targeted catalyst query on follow-up or on a held direction reversal.
        slot = request["round_slot"]
        eligible = []
        for sid, row in evidence.items():
            if not row["current"] or row["sector_name"] == sid:
                continue
            previous = row["previous"]
            reversed_direction = (
                sid in tracked and previous
                and (number(previous[0]["board_return_ratio"]) > 0)
                != (number(row["current"][0]["board_return_ratio"]) > 0)
            )
            if slot == "10:35" or reversed_direction:
                eligible.append(sid)
        selected = min(eligible, key=lambda sid: (sid not in tracked, sid not in active, sid), default=None)
        mx = {"status": "NOT_NEEDED", "items": []}
        if selected:
            name = evidence[selected]["sector_name"][:100]
            query = f"{request['trade_date']} 截至{slot}北京时间，A股{name}板块相关公开公告和产业事件，提供标题、来源和原始发布时间"
            try:
                response = budget.call("mx", lambda timeout: self.client.call("mx", {"query": query}, timeout=timeout),
                                       attempts=1, timeout=18)
                items = mx_items(response, self.cutoff)
                mx = {**response, "items": items, "query": query, "sector_id": selected}
                if response.get("status") == "READY" and not items:
                    mx["status"] = "NO_ITEMS_BEFORE_CUTOFF"
                evidence[selected]["current"].extend(items)
            except (TimeoutError, ConnectionError, ValueError, KeyError, TypeError) as exc:
                mx = {"status": "UNAVAILABLE", "error_type": type(exc).__name__, "items": [], "sector_id": selected}
        from .etf_representativeness import freeze_policy, shadow_result
        from .penetration import freeze_pair_facts, prepare_etf_facts
        policy = freeze_policy(self.mapping, self.cutoff)
        representation, shadow_error = None, None
        if policy["policy_mode"] == "V3_ENFORCE":
            representation = freeze_pair_facts(self.mapping, self.cutoff, evidence, policy)
        elif policy["policy_mode"] == "V3_SHADOW":
            from copy import deepcopy
            representation, shadow_error = shadow_result("PREPARE", lambda: freeze_pair_facts(
                deepcopy(self.mapping), self.cutoff, tuple(evidence), deepcopy(policy)))
        options = prepare_etf_facts(self.mapping, self.market, self.cutoff, evidence,
                                    held_codes=request.get("held_etfs", ()),
                                    representation=representation if policy["policy_mode"] == "V3_ENFORCE" else None)
        if policy["policy_mode"] == "V3_ENFORCE" and policy["taxonomy_version"]:
            from .etf_support_facts import attach_static_support
            attach_static_support(options, self.mapping, representation, policy)
        common = market_facts(self.market, self.cutoff)
        for sid, row in evidence.items():
            row["etf_options"] = {**options.get(sid, {}), "mapping_version": self.mapping["version"]}
            row.update(numeric_contract=NUMERIC_CONTRACT, etf_selection_contract=ETF_SELECTION_CONTRACT,
                       fact_catalog=fact_catalog(row, common))
        return {**request, "evidence": evidence, "mx": mx, "discovery": discovery,
                "etf_policy": policy, "etf_pair_facts": representation, "etf_shadow_error": shadow_error,
                "numeric_contract": NUMERIC_CONTRACT, "etf_selection_contract": ETF_SELECTION_CONTRACT}

    def _analyze(self, request, budget):
        inputs = list(request["evidence"].values())
        if not any(row["current"] for row in inputs):
            return dict(status="NOT_NEEDED", requests=0, generated_at=datetime.now(CHINA).isoformat()), {}, set()

        explanatory = sorted(
            (row for row in inputs if row["current"]),
            key=lambda row: (not row["held"], not row["prior_active"],
                             -abs(number(row["facts"].get("board_return_ratio")) or 0), row["sector_id"]),
        )
        research_ids = [row["sector_id"] for row in explanatory[:MAX_RESEARCH_SECTORS]]

        def call(timeout):
            try:
                reply = self.client.call("analyze", {
                    "trade_date": request["trade_date"], "round_slot": request["round_slot"],
                    "planned_at": request["planned_at"], "input_kind": self.ledger.origin, "sectors": inputs,
                    "research_explanations": True, "research_sector_ids": research_ids,
                    "numeric_contract": request.get("numeric_contract"),
                    "etf_selection_contract": request.get("etf_selection_contract"),
                    **({"etf_policy_version": request["etf_policy"]["policy_version"]}
                       if (request.get("etf_policy") or {}).get("policy_mode") == "V3_ENFORCE" else {}),
                }, timeout=timeout)
            except (TimeoutError, ConnectionError) as exc:
                _log_attempt_failure(request, transport_error=exc)
                raise
            if reply.get("status") != "READY":
                _log_attempt_failure(request, reply=reply)
                if reply.get("retryable") is True:
                    raise ConnectionError("OCTOP analysis temporarily unavailable")
                raise OctopRejected("OCTOP analysis rejected")
            return reply

        receipt = budget.call("hermes", call, attempts=2, timeout=70)
        if (receipt.get("requests") != 1 or receipt.get("http_status") != [200]
                or receipt.get("tool_calls") != 0 or receipt.get("finish_reason") != "stop"):
            raise ValueError("incomplete OCTOP round response")
        raw = json.loads(receipt["output"], object_pairs_hook=unique_object)
        if not isinstance(raw, dict) or set(raw) != {"decisions"} or not isinstance(raw["decisions"], list):
            raise ValueError("invalid OCTOP round response")
        decisions = {}
        duplicates = set()
        for item in raw["decisions"]:
            if not isinstance(item, dict) or not isinstance(item.get("sector_id"), str):
                continue
            sid = item["sector_id"]
            if "research" in item and sid not in research_ids:
                item = {key: value for key, value in item.items() if key != "research"}
            if sid in decisions:
                duplicates.add(sid)
            decisions[sid] = item
        return receipt, decisions, duplicates

    def __call__(self, request, budget):
        evidence, mx = request["evidence"], request["mx"]
        receipt, decisions, duplicates = self._analyze(request, budget)
        generated = moment(receipt["generated_at"])
        if generated < self.cutoff or generated.date().isoformat() != request["trade_date"]:
            raise ValueError("OCTOP response generation does not belong to this round")
        report_id = "octop-" + sha256(encode([self.ledger.ns, request["planned_at"]]).encode()).hexdigest()[:28]
        sectors, judgments, warnings, times, summaries = [], [], [], [], []
        market_evidence, source_facts, stock_evidence, core_leaders = {}, {}, {}, {}
        explanations, research_evidence, unconfirmed = {}, {}, []
        judgment_reviews, etf_choices, etf_reviews, etf_audits = {}, {}, {}, {}
        for sid, context in evidence.items():
            current = context["current"]
            item = decisions.get(sid)
            try:
                if receipt["status"] == "NOT_NEEDED":
                    item = dict(sector_id=sid, phase="NOT_COVERED", evidence_refs=[])
                if item is None or sid in duplicates:
                    raise ValueError("missing or duplicate sector decision")
                direction = item["phase"]
                if direction not in ("UNKNOWN", "NOT_COVERED", "EVENT_ONLY") and (
                        not current or current[0]["ref"] not in item["evidence_refs"]):
                    raise ValueError("confirmed phase requires a current market reference")
                result = render({**receipt, "finish_reason": "stop", "report_id": report_id, "output": encode(item)}, context)
            except (ValueError, KeyError, TypeError) as exc:
                warnings.append(f"{sid}:OCTOP_DECISION_UNCONFIRMED:{type(exc).__name__}")
                fallback = dict(sector_id=sid, phase="UNKNOWN" if current else "NOT_COVERED",
                                evidence_refs=[row["ref"] for row in current])
                result = render({**receipt, "finish_reason": "stop", "report_id": report_id, "output": encode(fallback)}, context)
            explanation = result["research"]
            if result["judgment_review"]:
                judgment_reviews[sid] = result["judgment_review"]
            if result["etf_choices"] is not None:
                etf_choices[sid] = result["etf_choices"]
            if result["etf_selection_review"]:
                etf_reviews[sid] = result["etf_selection_review"]
            if result.get("etf_selection_audit"):
                etf_audits[sid] = result["etf_selection_audit"]
            if explanation:
                explanations[sid] = explanation
                refs = set(explanation["counter_evidence_refs"])
                if refs:
                    research_evidence[sid] = {row["ref"]: row for key in ("current", "previous", "stock_evidence")
                                              for row in context.get(key, ()) if row["ref"] in refs}
            if item and "research" in item and (not explanation or explanation.get("source") == "PROGRAM_FACTS"):
                unconfirmed.append(sid)
            core_leaders[sid] = result["core_leaders"]
            section = result["report"]
            sectors.extend(section["sectors"])
            summaries.append(section["summary"])
            source_facts[sid] = section["source_facts"]
            if context.get("stock_evidence"):
                stock_evidence[sid] = context["stock_evidence"]
            judgments.extend(result["judgments"])
            warnings.extend(section["warnings"])
            if section["data_as_of"]:
                times.append(section["data_as_of"])
            if current:
                market_evidence[sid] = current[0]
        if mx["status"] not in ("NOT_NEEDED", "READY"):
            warnings.append("MX_"+mx["status"])
        if not sectors:
            warnings.append("NO_SECTOR_OBSERVATION")
        partial = warnings or any(row["signal"] == "UNCERTAIN" for row in sectors)
        report = dict(schema_version="hermes_sector_rotation_v1", report_id=report_id,
                      trade_date=request["trade_date"], generated_at=generated.isoformat(),
                      data_as_of=max(times, key=moment, default=None), expires_at=None,
                      status=("PARTIAL" if partial else "READY") if sectors else "UNAVAILABLE",
                      summary=" ".join(summaries) or "本轮缺少可用板块观测。",
                      sectors=sectors, warnings=list(dict.fromkeys(warnings)), source_facts=source_facts)
        return dict(input_kind=self.ledger.origin, published_at=generated.isoformat(), report_json=encode(report),
                    judgments=judgments, analyst_version="octop-scheduled-hybrid-v2",
                    octop=dict(receipt=receipt, market_evidence=market_evidence,
                               stock_research=stock_evidence, core_leaders=core_leaders,
                               tracked_sectors=request.get("tracked_sectors", []),
                               discovery=request.get("discovery"), research_explanations=explanations,
                               research_explanation_evidence=research_evidence,
                               research_explanations_unconfirmed=unconfirmed, judgment_review=judgment_reviews,
                               **({"etf_selection_audit": etf_audits} if etf_audits else {}),
                               **(dict(etf_choices=etf_choices, etf_selection_review=etf_reviews,
                                       etf_selection_contract=ETF_SELECTION_CONTRACT)
                                  if request.get("etf_selection_contract") == ETF_SELECTION_CONTRACT else {}),
                               numeric_contract=request.get("numeric_contract"), mx=mx,
                               market_snapshot_id=request["market_snapshot_id"], input_cutoff=self.cutoff.isoformat()))
