"""Frozen formal sector/index facts used by ETF V3.2."""
from decimal import Decimal
from hashlib import sha256
import json
import os

VERSION = "etf-representativeness-v1"
METHOD = "etf-direction-first-v3"
MODES = {"V2", "V3_SHADOW", "V3_ENFORCE"}
PARAMETERS = (
    ("weak_exposure_floor", "0.03"),
    ("single_stock_guard_cap", "0.10"),
    ("single_stock_guard_min_members", 10),
    ("core_exposure_tier_floor", "0.15"),
)
REJECTIONS = frozenset((
    "NO_OVERLAP_CONFIRMED", "WEAK_OVERLAP_REJECTED",
    "SINGLE_STOCK_CROSSOVER_REJECTED", "WEAK_OVERLAP_UPPER_BOUND_REJECTED",
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
                prompt_version=VERSION if mode == "V3_ENFORCE" else "scoped-facts-v1")


def evaluate_pair_representativeness(frozen_sector_members, known_weights, *,
                                     sector_members_complete, weights_complete_and_valid,
                                     partial_upper_bound_proven=False, explicit_relation=False,
                                     policy=None):
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


def shadow_result(stage, operation):
    """Run a shadow implementation on its caller-provided private input."""
    try:
        result = operation()
        if not isinstance(result, dict):
            raise ValueError("invalid ETF shadow result")
        json.dumps(result, allow_nan=False)
    except Exception as exc:
        return None, dict(stage=stage, error=type(exc).__name__)
    return result, None
