"""Normalize the published v1 JSON without editing it or reading prose as orders."""
import json
import re
from datetime import date

from .simulator import CODE, moment

VERSION = "hermes_v1_to_stage1_v1"
READ_ONLY_OBSERVATION = "READ_ONLY_OBSERVATION"
OBS = {"新增": ("NEW", "UP"), "加强": ("STRENGTHEN", "UP"),
       "维持": ("MAINTAIN", "HOLD"), "减弱": ("WEAKEN", "WEAK"),
       "待核实": ("UNKNOWN", "UNKNOWN"), "本轮未覆盖": ("UNKNOWN", "UNKNOWN"),
       "事件先行": ("UNKNOWN", "UNKNOWN")}
SIGNALS = {"STRENGTHENING": "UP", "STABLE": "HOLD", "WEAKENING": "WEAK", "UNCERTAIN": "UNKNOWN"}
BOARD = re.compile(r"kaipanla:[0-9A-Za-z_-]{1,64}\Z")


def strings(v):
    return [x for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []


def array(v):
    return v if isinstance(v, list) else []


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field in Hermes JSON")
        result[key] = value
    return result


def normalize(event, *, allow_read_only=False):
    """Only whitelisted v1 fields plus an explicit frozen structured judgment."""
    raw = event["report_json"]
    doc = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(doc, dict) or doc.get("schema_version") != "hermes_sector_rotation_v1":
        raise ValueError("unsupported Hermes report schema")
    if not allow_read_only and doc.get("purpose") == READ_ONLY_OBSERVATION:
        raise ValueError("read-only OCTOP observation cannot publish a round")
    rid = doc["report_id"]
    if not isinstance(rid, str) or not 0 < len(rid) <= 128:
        raise ValueError("invalid report ID")
    generated = moment(doc["generated_at"])
    at = moment(event.get("published_at", doc["generated_at"]))
    if at < generated:
        raise ValueError("publication precedes report generation")
    status = doc["status"]
    if status not in ("READY", "PARTIAL", "UNAVAILABLE"):
        raise ValueError("invalid report status")
    same_day = date.fromisoformat(doc["trade_date"]) == at.date()
    context_ok = same_day and status != "UNAVAILABLE"
    rows, conflicts, diagnostics, complete_retreat = {}, set(), [], {}
    for mark in array(event.get("judgments")):
        if (isinstance(mark, dict) and isinstance(mark.get("sector_id"), str)
                and BOARD.fullmatch(mark["sector_id"]) and mark.get("phase") == "RETREAT"
                and isinstance(mark.get("evidence_ref"), str) and mark["evidence_ref"].strip()):
            sid = mark["sector_id"]
            complete_retreat.setdefault(sid, set()).add(mark["evidence_ref"])
        else:
            diagnostics.append("INVALID_STRUCTURED_JUDGMENT")

    for idx, row in enumerate(array(doc.get("sectors"))):
        if not isinstance(row, dict):
            diagnostics.append(f"INVALID_SECTOR:{idx}")
            continue
        pid = row.get("kaipanla_plate_id")
        sid = "kaipanla:" + pid if isinstance(pid, str) else ""
        if not BOARD.fullmatch(sid):
            diagnostics.append(f"UNRESOLVED_SECTOR:{idx}")
            continue
        if sid in rows:
            conflicts.add(sid)
            continue
        refs = strings(row.get("sources"))
        evidence = row.get("evidence_status")
        obs = row.get("observation_state")
        signal = SIGNALS.get(row.get("signal")) if isinstance(row.get("signal"), str) else None
        observed = OBS.get(obs) if isinstance(obs, str) else None
        phase, state = "UNKNOWN", "UNKNOWN"
        approved = sorted(complete_retreat.get(sid, set()).intersection(refs))
        if (not context_ok or not refs or signal is None or row.get("validation_status") == "REJECTED"
                or row.get("identity_status") == "UNVERIFIED" or row.get("evidence_valid") is False
                or evidence in ("UNVERIFIED", "NOT_COVERED", "EVENT_ONLY")
                or obs in ("待核实", "本轮未覆盖", "事件先行")):
            state = "UNKNOWN"
        elif approved and signal == "WEAK" and obs in (None, "撤回"):
            phase, state = "RETREAT", "EXIT"
        elif obs == "撤回" or row.get("support_withdrawn") is True:
            state = "UNKNOWN"
            diagnostics.append(f"WITHDRAWAL_REQUIRES_EXPLICIT_RETREAT:{sid}")
        elif observed is None or observed[1] != signal or approved:
            state = "UNKNOWN"
            diagnostics.append(f"AMBIGUOUS_OR_CONFLICTING_SECTOR_STATE:{sid}")
        else:
            phase, state = observed
        rows[sid] = dict(phase=phase, state=state, row=idx, sources=refs, judgment_refs=approved if state=="EXIT" else [],
                         reasons=strings(row.get("reasons")), direction_id=row.get("direction_id"),
                         data_as_of=doc.get("data_as_of"))
    for sid in conflicts:
        rows[sid]["phase"] = rows[sid]["state"] = "UNKNOWN"
        rows[sid]["judgment_refs"] = []
        diagnostics.append(f"DUPLICATE_SECTOR:{sid}")

    candidates = {}
    for idx, choice in enumerate(array(event.get("candidates"))):
        if not isinstance(choice, dict):
            diagnostics.append(f"INVALID_ETF:{idx}")
            continue
        code, sid, rank = choice.get("etf"), choice.get("sector_id"), choice.get("rank")
        if (not isinstance(code, str) or not CODE.fullmatch(code) or not isinstance(sid, str) or sid not in rows
                or type(rank) is not int or rank < 1 or not isinstance(choice.get("mapping_ref"), str)
                or not choice["mapping_ref"].strip()):
            diagnostics.append(f"INVALID_ETF:{idx}")
            continue
        candidates.setdefault(code, []).append(dict(rank=rank, sector_id=sid, mapping_ref=choice["mapping_ref"]))
    for code, choices in candidates.items():
        unique = {(c["rank"], c["sector_id"], c["mapping_ref"]): c for c in choices}
        candidates[code] = sorted(unique.values(), key=lambda c: (c["rank"], c["sector_id"], c["mapping_ref"]))
    research = doc.get("research_context")
    key = research.get("evidence_key") if isinstance(research, dict) else None
    if not isinstance(key, str) or not key.strip():
        key = None
    return dict(id=rid, at=at.isoformat(), raw=raw, adapter_version=VERSION, evidence_key=key,
                sectors=rows, candidates=candidates, diagnostics=diagnostics, status=status,
                generated_at=generated.isoformat(), data_as_of=doc.get("data_as_of"))
