"""Bounded market-wide direction discovery, independent of ETF mapping."""
from ..adapter import BOARD
from .penetration import number
from ..simulator import moment

MAX_NEW_DIRECTIONS = 8


def select_directions(boards, active, tracked, cutoff):
    eligible = {}
    for sid, row in boards.items():
        try:
            at = moment(row["observed_at"])
            change = number(row.get("return_ratio"))
            if (BOARD.fullmatch(sid) and at.date() == cutoff.date()
                    and at <= cutoff and change is not None):
                eligible[sid] = (change, number(row.get("strength")))
        except (ValueError, KeyError, TypeError):
            continue
    ranked_return = sorted(eligible, key=lambda sid: (-eligible[sid][0], sid))
    ranked_strength = sorted((sid for sid in eligible if eligible[sid][1] is not None),
                             key=lambda sid: (-eligible[sid][1], -eligible[sid][0], sid))
    fresh = []
    for sid in (*ranked_return[:4], *ranked_strength[:4], *ranked_return):
        if sid not in fresh:
            fresh.append(sid)
        if len(fresh) == MAX_NEW_DIRECTIONS:
            break
    required = set(active) | set(tracked)
    selected = sorted(required | set(fresh))
    return selected, dict(method="market-discovery-v2", observed_count=len(eligible),
                          new_direction_limit=MAX_NEW_DIRECTIONS,
                          retained_count=len(required), analyzed_count=len(selected),
                          unevaluated_count=len(set(eligible) - set(selected)),
                          evaluation_complete=set(eligible) <= set(selected))
