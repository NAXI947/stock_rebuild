"""Render validated OCTOP direction decisions with program-owned market facts."""
from decimal import Decimal
import json

from ..adapter import READ_ONLY_OBSERVATION, normalize, unique_object
from ..ledger import encode
from ..simulator import moment, session
from .stock_research import MAX_PER_SECTOR, MAX_CORE_LEADERS
from .research_facts import explanation, judgment_review, selection_detail

DECISION_PROTOCOL = "stock-v4-octop-direction-v1"
RESEARCH_ROLES = frozenset(("MAINLINE", "ROTATION", "WATCH", "EVENT_FIRST"))
COUNTER_EVIDENCE_STATES = frozenset(("FOUND", "NOT_FOUND", "NOT_CHECKED"))
RESEARCH_FIELDS = {"role", "rationale", "counter_evidence_status", "counter_evidence_refs",
                   "invalidation_condition", "key_unknown"}
PHASES = {
    "NEW": ("新增", "STRENGTHENING"),
    "MAINTAIN": ("维持", "STABLE"),
    "STRENGTHEN": ("加强", "STRENGTHENING"),
    "WEAKEN": ("减弱", "WEAKENING"),
    "RETREAT": ("撤回", "WEAKENING"),
    "UNKNOWN": ("待核实", "UNCERTAIN"),
    "NOT_COVERED": ("本轮未覆盖", "UNCERTAIN"),
    "EVENT_ONLY": ("事件先行", "UNCERTAIN"),
}

PROMPT = """只按当前、前序证据独立研判单一板块。使用技能中的状态语义。输出仅一个 JSON 对象，包含 sector_id、phase、evidence_refs。phase 可为 NEW（新增）、MAINTAIN（维持）、STRENGTHEN（加强）、WEAKEN（减弱）、RETREAT（明确退潮）、UNKNOWN（待核实）、NOT_COVERED（本轮未覆盖）、EVENT_ONLY（事件先行）。证据引用从 current 的 ref 原样复制，列出支撑结论的引用。当前证据为空时 evidence_refs 为空。只输出这些字段。指标、缺口和中文摘要由应用按来源生成。constituent_quote_coverage 为成分股报价覆盖，sector_exposure 为板块权重暴露。IOPV 缺失和报价覆盖不足只影响相关指标，按已有可比证据判断方向。previous 是已发布报告保存的板块观测，独立于 prior_active；同日可比价格变化见 facts.board_return_change_ratio，跨日 previous 仅作为历史背景。缺板块成分或个股报价只限制参与宽度、龙头和穿透分析，不单独要求整板块 UNKNOWN。phase 仍为交易方向状态：NEW 需要 prior_active=false 且当轮证据支持新方向，无需 previous；MAINTAIN/STRENGTHEN/WEAKEN/RETREAT 需要 prior_active=true 和可比 previous；价格变化由程序单独展示，不直接当作交易信号。"""


def _percentage(value, *, signed=False):
    return format(Decimal(value), "+.2%" if signed else ".2%")


def fact_lines(facts):
    lines = []
    for key, label, signed in (
        ("board_return_ratio", "板块涨跌", True),
        ("etf_return_ratio", "ETF 涨跌", True),
        ("sector_exposure", "板块权重暴露", False),
        ("constituent_quote_coverage", "成分股报价覆盖", False),
        ("mainboard_weight", "主板权重", False),
    ):
        if facts.get(key) is not None:
            lines.append(f"{label} {_percentage(facts[key], signed=signed)}")
    if facts.get("board_return_change_ratio") is not None and facts.get("comparison_observed_at"):
        compared = moment(facts["comparison_observed_at"])
        change = Decimal(facts["board_return_change_ratio"]) * 100
        lines.append(f"较{compared:%H:%M}日涨幅变化 {change:+.2f}个百分点")
    return lines


def research_explanation(raw, evidence, phase):
    if (not isinstance(raw, dict) or set(raw) | {"counter_evidence_refs"} != RESEARCH_FIELDS
            or phase == "NOT_COVERED"):
        return None
    role = raw["role"]
    if not isinstance(role, str) or role not in RESEARCH_ROLES:
        return None
    if role in ("MAINLINE", "ROTATION") and phase not in ("NEW", "MAINTAIN", "STRENGTHEN"):
        return None
    if role == "EVENT_FIRST" and phase != "EVENT_ONLY":
        return None

    status, refs = raw["counter_evidence_status"], raw.get("counter_evidence_refs", [])
    allowed = {row["ref"] for key in ("current", "previous", "stock_evidence")
               for row in evidence.get(key, ()) if isinstance(row.get("ref"), str)}
    if (not isinstance(status, str) or status not in COUNTER_EVIDENCE_STATES or not isinstance(refs, list)
            or len(refs) > 4 or any(not isinstance(ref, str) or ref not in allowed for ref in refs)
            or len(set(refs)) != len(refs) or (status == "FOUND") != bool(refs)):
        return None
    if status == "NOT_FOUND" and not (evidence["previous"] or evidence.get("stock_evidence")):
        return None

    normalized = dict(role=role, counter_evidence_status=status, counter_evidence_refs=refs)
    for key, limit in (("rationale", 180), ("invalidation_condition", 120), ("key_unknown", 120)):
        value = raw[key]
        if not isinstance(value, str) or not 1 <= len(value) <= limit or any(ord(char) < 32 for char in value):
            return None
        clean = " ".join(value.split())
        if not clean:
            return None
        normalized[key] = clean
    return normalized


def render(receipt, evidence):
    if receipt.get("finish_reason") != "stop":
        raise ValueError("OCTOP decision is incomplete")
    decision = json.loads(receipt["output"], object_pairs_hook=unique_object)
    required = {"sector_id", "phase", "evidence_refs"}
    if (not isinstance(decision, dict) or not required <= set(decision)
            or set(decision) - required - {"core_stock_codes", "core_leader_codes", "research", "fact_refs", "etf_choices"}):
        raise ValueError("OCTOP decision must contain only the direction, evidence references and approved optional fields")
    if decision["sector_id"] != evidence["sector_id"] or decision["phase"] not in PHASES:
        raise ValueError("invalid OCTOP sector or phase")
    review = judgment_review(decision, evidence)
    etf_choices, selection_review, selection_audit = selection_detail(decision, evidence)
    if review:
        decision = {**decision, "phase": "UNKNOWN" if evidence["current"] else "NOT_COVERED",
                    "evidence_refs": [row["ref"] for row in evidence["current"]],
                    "core_stock_codes": [], "core_leader_codes": []}
    phase = decision["phase"]
    refs = decision["evidence_refs"]
    current = {row["ref"]: row for row in evidence["current"]}
    if (not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in current for ref in refs)
            or len(set(refs)) != len(refs) or bool(refs) != bool(current)):
        raise ValueError("OCTOP referenced missing, duplicate, or non-current evidence")
    if (phase == "NOT_COVERED") != (not current):
        raise ValueError("OCTOP phase conflicts with current sector coverage")

    previous = evidence["previous"]
    if phase in ("MAINTAIN", "STRENGTHEN", "WEAKEN", "RETREAT"):
        if evidence["prior_active"] is not True or not previous:
            raise ValueError("OCTOP phase requires a comparable active direction")
    if phase == "NEW" and evidence["prior_active"] is not False:
        raise ValueError("OCTOP new direction requires known prior rotation status")

    source_at = max((row["observed_at"] for row in (*current.values(), *previous)),
                    key=moment, default=None)
    if source_at and moment(receipt["generated_at"]) < moment(source_at):
        raise ValueError("report generation predates its evidence")
    observation, signal = PHASES[phase]
    display = "退潮" if phase == "RETREAT" else observation
    facts = evidence["facts"]
    lines = fact_lines(facts)
    warnings = [*facts["quality"], *review, *selection_review]
    selected = decision.get("core_stock_codes", [])
    permitted = {row["code"]: row for row in evidence.get("stock_evidence", [])}
    if (not isinstance(selected, list) or len(selected) > MAX_PER_SECTOR
            or any(not isinstance(code, str) or code not in permitted for code in selected)
            or len(set(selected)) != len(selected)
            or (selected and (not current or phase not in ("NEW", "MAINTAIN", "STRENGTHEN")))):
        warnings.append("STOCK_SELECTION_UNCONFIRMED")
        selected = []
    if selected:
        source_at = max([source_at, *(permitted[code]["observed_at"] for code in selected)],
                        key=moment)
        if moment(receipt["generated_at"]) < moment(source_at):
            raise ValueError("report generation predates selected stock evidence")
    leaders = decision.get("core_leader_codes")
    core_status = "UNKNOWN"
    if leaders is not None:
        valid_leaders = (isinstance(leaders, list) and len(leaders) <= MAX_CORE_LEADERS
                         and len(set(code for code in leaders if isinstance(code, str))) == len(leaders)
                         and bool(permitted) and bool(current))
        if valid_leaders:
            for code in leaders:
                row = permitted.get(code, {})
                try:
                    observed = moment(row["observed_at"])
                    valid_leaders = (observed.date().isoformat() == evidence["trade_date"]
                                     and session(observed) and observed <= moment(receipt["generated_at"])
                                     and isinstance(row.get("ref"), str) and bool(row["ref"])
                                     and isinstance(row.get("source"), str) and bool(row["source"]))
                except (ValueError, KeyError, TypeError):
                    valid_leaders = False
                if not valid_leaders:
                    break
        if valid_leaders:
            core_status = "VERIFIED"
        else:
            warnings.append("CORE_LEADERS_UNCONFIRMED")
            leaders = []
    else:
        leaders = []
    if leaders:
        source_at = max([source_at, *(permitted[code]["observed_at"] for code in leaders)], key=moment)
    if not current:
        warnings.append("NO_CURRENT_SECTOR_EVIDENCE")
    if not previous:
        warnings.append("NO_COMPARABLE_PRIOR")
    summary = "。".join([f"{evidence['sector_name']}｜{display}", "，".join(lines)] if lines
                       else [f"{evidence['sector_name']}｜{display}"]) + "。"
    judgments = ([{"phase": "RETREAT", "sector_id": evidence["sector_id"], "evidence_ref": refs[0]}]
                 if phase == "RETREAT" else [])
    report = {
        "schema_version": "hermes_sector_rotation_v1",
        "purpose": READ_ONLY_OBSERVATION,
        "report_id": receipt["report_id"],
        "trade_date": evidence["trade_date"],
        "generated_at": receipt["generated_at"],
        "data_as_of": source_at,
        "expires_at": None,
        "status": "PARTIAL" if warnings or signal == "UNCERTAIN" else "READY",
        "summary": summary,
        "warnings": warnings,
        "sectors": [{
            "sector_name": evidence["sector_name"],
            "kaipanla_plate_id": evidence["sector_id"].split(":", 1)[1],
            "observation_state": observation,
            "signal": signal,
            "evidence_status": "PARTIAL" if warnings else "VERIFIED",
            "risk_level": "UNKNOWN",
            "reasons": lines,
            "risks": warnings,
            "sources": refs,
            **({"core_stock_codes": selected} if selected else {}),
            "core_leader_codes": leaders, "core_selection_status": core_status,
        }],
        "source_facts": {**facts, "input_as_of": evidence["data_as_of"]},
    }
    normalized = normalize({"report_json": encode(report), "judgments": judgments}, allow_read_only=True)
    if normalized["diagnostics"]:
        raise ValueError("OCTOP report has invalid normalization diagnostics")
    checked = research_explanation(decision.get("research"), evidence, phase)
    return {"report": report, "judgments": judgments, "normalized": normalized["sectors"],
            "research": explanation(decision, evidence, checked, review),
            "judgment_review": review, "etf_choices": etf_choices, "etf_selection_review": selection_review,
            **({"etf_selection_audit": selection_audit} if selection_audit else {}),
            "core_leaders": dict(status=core_status, codes=leaders,
                                 evidence=[permitted[code] for code in leaders])}
