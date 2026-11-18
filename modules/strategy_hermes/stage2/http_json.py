"""Single bounded standard-library requests; provider credentials stay in memory."""
from contextlib import closing
import http.client
import math
import re
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

from ..ledger import encode
from .inputs import document
from .provider_errors import ProviderFailure

MAX_BYTES = 4 * 1024 * 1024
USER_AGENT = "stock-v4-provider-client/1.0"
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


class ProviderHTTPError(ProviderFailure):
    pass


def post_json(url, payload, *, headers=(), timeout=12, protocols="=https"):
    return _request_json("POST", url, payload, headers=headers, timeout=timeout, protocols=protocols)


def get_json(url, *, headers=(), timeout=8):
    return _request_json("GET", url, None, headers=headers, timeout=timeout, protocols="=https")


def _request_json(method, url, payload, *, headers, timeout, protocols):
    if (not isinstance(protocols, str) or not protocols.startswith("=")
            or not (allowed := set(protocols[1:].split(","))) <= {"http", "https"}):
        raise ValueError("invalid provider protocols")
    try:
        address = urlsplit(url)
        port = address.port
    except (TypeError, ValueError):
        raise ValueError("invalid provider URL") from None
    if (address.scheme not in allowed or not address.hostname or address.username is not None
            or address.password is not None or address.fragment):
        raise ValueError("provider URL outside allowed protocols")
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("invalid provider timeout")
    request_headers = {"Content-Type": "application/json", "Accept": "application/json",
                       "User-Agent": USER_AGENT}
    for name, value in headers:
        if (not isinstance(name, str) or not HEADER_NAME.fullmatch(name)
                or not isinstance(value, str) or any(char in value for char in "\r\n")):
            raise ValueError("invalid HTTP header")
        # Caller casing must not produce duplicate content/authentication headers.
        for existing in list(request_headers):
            if existing.lower() == name.lower():
                del request_headers[existing]
        request_headers[name] = value
    data = encode(payload).encode("utf-8") if method == "POST" else None
    target = urlunsplit(("", "", address.path or "/", address.query, ""))
    deadline = monotonic() + timeout

    def remaining():
        left = deadline - monotonic()
        if left <= 0:
            raise TimeoutError()
        return left

    connection_type = http.client.HTTPSConnection if address.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(address.hostname, port=port, timeout=min(5, timeout))
    try:
        with closing(connection):
            connection.connect()
            transport = connection.sock
            transport.settimeout(remaining())
            connection.request(method, target, body=data, headers=request_headers)
            transport.settimeout(remaining())
            # http.client does not redirect or retry; one call is one physical request.
            with connection.getresponse() as response:
                if response.status != 200:
                    raise ProviderHTTPError(f"HTTP status {response.status}", kind='HTTP_STATUS',
                                            details={'http_status': response.status})
                length = response.getheader("Content-Length")
                if length is not None:
                    if not length.isascii() or not length.isdecimal():
                        raise ProviderHTTPError("invalid provider content length", kind='FIELD_MISMATCH')
                    try:
                        length = int(length)
                    except ValueError:
                        raise ProviderHTTPError("invalid provider content length", kind='FIELD_MISMATCH') from None
                    if length > MAX_BYTES:
                        raise ProviderHTTPError("provider response exceeded size bound", kind='RESPONSE_TOO_LARGE')
                body = bytearray()
                while not response.isclosed():
                    transport.settimeout(remaining())
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise ProviderHTTPError("provider response exceeded size bound", kind='RESPONSE_TOO_LARGE')
                if length is not None and not response.chunked and len(body) != length:
                    raise ProviderHTTPError("provider response was incomplete", kind='INCOMPLETE_RESPONSE')
    except (OSError, http.client.HTTPException):
        raise ProviderHTTPError("provider connection or timeout", kind='CONNECTION_FAILED') from None
    try:
        return document(body)
    except (ValueError, UnicodeError):
        raise ProviderHTTPError("provider response was not JSON", kind='INVALID_JSON') from None
