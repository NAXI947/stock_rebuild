"""Keep live execution units stable without granting new candidate eligibility."""
import json
from copy import deepcopy

from ..simulator import CODE, integer


def execution_units(connection, namespace, origin, instruments, *, lock=True):
    """Merge new verified instruments into the existing live execution registry.

    Writers hold the same namespace transaction lock as ``_store``. The read-only
    quote projection may omit that lock; the final intake repeats this check in
    its write transaction. Existing units always retain their frozen values.
    """
    if origin != "LIVE":
        return instruments
    current = {code: integer(unit) for code, unit in instruments.items() if CODE.fullmatch(code)}
    if len(current) != len(instruments):
        raise ValueError("invalid ETF code in instrument registry")
    if lock:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (namespace,))
    row = connection.execute("SELECT config FROM runs WHERE ns=%s", (namespace,)).fetchone()
    if row is None:
        return current
    config = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return current | config["instruments"]


def quote_instruments(ledger, instruments):
    """Include already registered holdings when consuming shared market quotes."""
    if ledger.origin != "LIVE":
        return instruments
    with ledger._connect() as connection, connection.transaction():
        connection.execute("SET TRANSACTION READ ONLY")
        return execution_units(connection, ledger.ns, ledger.origin, instruments, lock=False)


def eligible_report(report, current, frozen):
    """Filter only new entry candidates; keep original report JSON and judgments."""
    choices = report.get("candidates")
    if not isinstance(choices, list):
        return report, []
    kept, blocked = [], {}
    for choice in choices:
        code = choice.get("etf") if isinstance(choice, dict) else None
        if not isinstance(code, str):
            kept.append(choice)  # The normalizer retains malformed-row diagnostics.
            continue
        if code not in current:
            blocked[code] = "RULE_UNVERIFIED"
        elif current[code] != frozen.get(code):
            blocked[code] = "TRADE_UNIT_CHANGED"
        else:
            kept.append(choice)
    return {**report, "candidates": kept}, [dict(etf=code, reason=reason)
                                           for code, reason in sorted(blocked.items())]


def apply_candidate_blocks(metadata, blocked):
    """Persist the final admission result in the same facts rendered to the user."""
    if not blocked:
        return metadata
    updated = {**metadata, "execution_candidate_blocks": blocked}
    original = metadata.get("etf_recommendations", {})
    if original.get("method") != "etf-direction-first-v2":
        return updated
    result = deepcopy(original)
    reasons = {item["etf"]: item["reason"] for item in blocked}
    for sector in result.get("sectors", {}).values():
        for product in sector.get("products", []):
            reason = reasons.get(product.get("etf"))
            if reason is None or not product.get("ai_selected"):
                continue
            pending = reason in {'RULE_REVIEW_REQUIRED', 'ENTRY_ADMISSION_UNAVAILABLE',
                'ENTRY_REFERENCE_UNPUBLISHED', 'PRODUCT_RULE_UNVERIFIED', 'RULE_UNVERIFIED',
                'TRADE_UNIT_UNVERIFIED', 'PRODUCT_IDENTITY_UNVERIFIED'}
            product.update(execution_eligibility="PENDING" if pending else "BLOCKED",
                           execution_status="PARTIAL" if pending else "FAILED",
                           recommendation_status="PENDING" if pending else "REJECTED", representative_current=False)
            product["execution_reasons"] = list(dict.fromkeys(product.get("execution_reasons", []) + [reason]))
            product["rejection_reasons"] = [] if pending else product["execution_reasons"]
            product.setdefault("checks", {})["execution_registry"] = dict(
                result="UNKNOWN" if pending else "FAIL", reason=reason, value=None, required=True, effect="EXECUTION")
    result["candidates"] = [item for item in result.get("candidates", []) if item["etf"] not in reasons]
    updated.update(penetration=result, etf_recommendations=result)
    return updated
