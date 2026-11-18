"""The frozen direction lifecycle; membership is independent of settlement."""
from hashlib import sha256
import json

LABELS = {
    "NEW": "新增",
    "MAINTAIN": "维持",
    "STRENGTHEN": "加强",
    "WEAKEN": "减弱",
    "RETREAT": "退潮",
    "UNKNOWN": "待确认",
}
COUNTED_PHASES = frozenset(("MAINTAIN", "STRENGTHEN", "WEAKEN"))


def describe(phase, streak):
    label = LABELS[phase]
    if streak > 1:
        label = f"{label}×{streak}"
    return dict(phase=phase, streak=streak, label=label)


def evidence_key(report, sid, row):
    if report["evidence_key"]:
        return report["evidence_key"]
    parts = [sid, row["data_as_of"], sorted(set(row["sources"]))]
    return sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def archive_phase(row):
    """Return the phase that advanced the chain, if this report advanced it."""
    if row.get("duplicate_evidence") or row.get("reason") in (
        "COINCIDENT_REPORT_SUPERSEDED", "NO_ACTIVE_DIRECTION", "ALREADY_IN_CHAIN",
    ):
        return None
    phase = row.get("phase")
    if phase in (None, "UNKNOWN"):
        return None
    return "STRENGTHEN" if phase == "STRENGTHEN_X2" else phase


def advance(previous, report, sid, row):
    """Return updated membership and effective report row, without changing raw v1."""
    before = previous or {"member": False, "streak": 0, "seen": [], "phase": "UNKNOWN"}
    raw_phase = row["phase"]

    def result(phase, streak, **extra):
        return dict(row, **describe(phase, streak),
                    strengthening_count=streak if phase == "STRENGTHEN" else 0, **extra)

    if raw_phase == "UNKNOWN":
        return previous, result("UNKNOWN", 0)
    key = evidence_key(report, sid, row)
    evidence = {"evidence_key": key}
    if raw_phase == "RETREAT":
        if before["phase"] == "RETREAT" and key in before["seen"]:
            return previous, result("RETREAT", 0, **evidence, state="UNKNOWN", duplicate_evidence=True)
        updated = dict(member=False, phase="RETREAT", streak=0,
                       seen=list(dict.fromkeys([*before["seen"], key])), report_id=report["id"])
        return updated, result("RETREAT", 0, **evidence)
    if key in before["seen"]:
        if before["member"]:
            return previous, result(before["phase"], before["streak"], **evidence,
                                    state="HOLD", duplicate_evidence=True)
        return previous, result("UNKNOWN", 0, **evidence, state="UNKNOWN", duplicate_evidence=True)
    if not before["member"] and raw_phase != "NEW":
        return previous, result("UNKNOWN", 0, **evidence, state="UNKNOWN", reason="NO_ACTIVE_DIRECTION")
    if before["member"] and raw_phase == "NEW":
        return previous, result("MAINTAIN", 0, **evidence, state="HOLD", reason="ALREADY_IN_CHAIN")

    streak = 0
    if raw_phase in COUNTED_PHASES:
        streak = before["streak"] + 1 if before["phase"] == raw_phase else 1
    updated = dict(member=True, phase=raw_phase, streak=streak,
                   seen=[*before["seen"], key], report_id=report["id"])
    return updated, result(raw_phase, streak, **evidence)
