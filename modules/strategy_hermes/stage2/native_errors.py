"""Stable, safe operator diagnostics without exporting arbitrary exception text."""

KNOWN_ERRORS = {
    'calendar update inserts an already processed trading day': 'EXECUTION_CALENDAR_HISTORY_CONFLICT',
    'published calendar days must remain available': 'EXECUTION_CALENDAR_REMOVED',
    'previous instrument units must be present and unchanged': 'EXECUTION_UNIT_HISTORY_CONFLICT',
    'changed rules require a new version': 'EXECUTION_RULE_VERSION_CONFLICT',
    'report ID already belongs to a committed round': 'REPORT_ALREADY_COMMITTED',
    'round no longer owns its publishing lease': 'ROUND_PUBLISH_LEASE_LOST',
    'round publishing lease expired': 'ROUND_PUBLISH_LEASE_EXPIRED',
    'hermes request budget exhausted': 'HERMES_REQUEST_BUDGET_EXHAUSTED',
    'mx request budget exhausted': 'MX_REQUEST_BUDGET_EXHAUSTED',
    'native analysis exceeds transport budget': 'NATIVE_ANALYSIS_TOO_LARGE',
}


def error_code(exc):
    from .native_recovery import RecoveryError
    if isinstance(exc, RecoveryError):
        return exc.code
    return KNOWN_ERRORS.get(str(exc), type(exc).__name__[:80])
