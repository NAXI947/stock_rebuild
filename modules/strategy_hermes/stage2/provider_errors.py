"""Typed provider diagnostics: persist only bounded, explicitly safe facts."""
from datetime import date
import re


KINDS = frozenset({
    'SOURCE_FAILURE', 'NOT_PUBLISHED', 'CONNECTION_FAILED', 'HTTP_STATUS',
    'PROVIDER_STATUS', 'INVALID_JSON', 'FIELD_MISMATCH', 'DATE_MISMATCH',
    'DATE_NOT_COMPLETED', 'PAGE_MISMATCH', 'INVALID_IDENTITY', 'INVALID_TIMESTAMP',
    'RESPONSE_TOO_LARGE', 'INCOMPLETE_RESPONSE', 'CONFIG_UNAVAILABLE',
    'REQUEST_BUDGET_EXHAUSTED',
})
_LEGACY_KINDS = {
    'source data not published yet': 'NOT_PUBLISHED',
    'empty provider result': 'NOT_PUBLISHED',
    'endpoint budget already consumed': 'REQUEST_BUDGET_EXHAUSTED',
}


def safe_details(details):
    """No source text, URLs, headers, credentials or arbitrary string values."""
    if not isinstance(details, dict):
        return {}
    result = {}
    for key, value in details.items():
        if key == 'http_status' and type(value) is int and 100 <= value <= 599:
            result[key] = value
        elif key == 'provider_code':
            if isinstance(value, str) and re.fullmatch(r'-?[0-9]{1,12}', value):
                value = int(value)
            if type(value) is int and abs(value) < 10**12:
                result[key] = value
        elif key in ('offset', 'row_count', 'page_size', 'reported_total'):
            if type(value) is int and 0 <= value <= 10**9:
                result[key] = value
        elif key in ('expected_date', 'returned_dates'):
            values = value if key == 'returned_dates' and isinstance(value, list) else [value]
            if len(values) > 8:
                continue
            try:
                if all(isinstance(item, str) and len(item) == 10
                       and date.fromisoformat(item).isoformat() == item for item in values):
                    result[key] = values if key == 'returned_dates' else values[0]
            except ValueError:
                pass
    return result


class ProviderFailure(RuntimeError):
    def __init__(self, message, *, kind=None, details=None):
        super().__init__(message)
        kind = kind or (_LEGACY_KINDS.get(message, 'SOURCE_FAILURE') if isinstance(message, str) else 'SOURCE_FAILURE')
        self.kind = kind if isinstance(kind, str) and kind in KINDS else 'SOURCE_FAILURE'
        self.details = safe_details(details)

    @classmethod
    def from_receipt(cls, receipt):
        receipt = receipt if isinstance(receipt, dict) else {}
        return cls('provider request failed', kind=receipt.get('error_kind'),
                   details=receipt.get('error_details'))


def error_diagnostic(exc):
    """Preserve the old exception-type contract and add safe typed diagnostics."""
    result = {'error_code': type(exc).__name__}
    if isinstance(exc, ProviderFailure):
        result['error_kind'] = exc.kind if isinstance(exc.kind, str) and exc.kind in KINDS else 'SOURCE_FAILURE'
        details = safe_details(exc.details)
        if details:
            result['error_details'] = details
    return result
