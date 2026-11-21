"""Read-only facts of the round that opened a cycle, kept for later study and never read by the engine."""
from decimal import Decimal
import json

from .market_regime import FALLBACK

LOOKBACK = 10


def read(conn, ns, cycle):
    """Four entry fields from the saved entry report and the reports before it; a missing fact stays None."""
    plan, report_id = cycle["exit_plan"], cycle["orders"][0]["report_id"]
    result = dict(entry_market_regime=None if FALLBACK in plan["flags"] else plan["market_regime"],
                  entry_round_slot=None, entry_up_streak=None, entry_market_breadth_ratio=None,
                  report_id=report_id, breadth_boards=None, breadth_observed_at=None)
    rows = conn.execute("""SELECT id,payload->>'round_slot',payload->'sectors'->%s->>'state',
            payload->'input_meta'->'market_scope'->'board_counts',payload->'input_meta'->>'source_observed_at'
        FROM events WHERE ns=%s AND kind='report'
          AND at<=(SELECT at FROM events WHERE ns=%s AND kind='report' AND id=%s)
        ORDER BY at DESC,id DESC LIMIT %s""", (cycle["primary_sector"], ns, ns, report_id, LOOKBACK)).fetchall()
    if not rows or rows[0][0] != report_id:
        return result
    _, slot, _, counts, observed = rows[0]
    streak = 0
    for row in rows:
        if row[2] != "UP":
            break
        streak += 1
    # Consecutive saved reports, the entry report last, whose primary sector state is UP; counted up to LOOKBACK.
    result.update(entry_round_slot=slot, entry_up_streak=streak)
    if isinstance(counts, str):
        counts = json.loads(counts)
    if isinstance(counts, dict):
        known = [counts.get(key) for key in ("up", "down", "flat")]
        if all(type(value) is int and value >= 0 for value in known) and sum(known):
            result.update(entry_market_breadth_ratio=str((Decimal(known[0]) / sum(known)).quantize(Decimal("0.000001"))),
                          breadth_boards=dict(zip(("up", "down", "flat"), known)), breadth_observed_at=observed)
    return result
