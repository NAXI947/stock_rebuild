"""Bounded JSON lines client for the OCTOP worker subprocess or operator relay."""
import os
import select
from time import monotonic
from uuid import uuid4

from ..ledger import encode
from .inputs import document

PROTOCOL = "stock-v4-octop-rpc-v1"
MAX_LINE = 512 * 1024


class OctopRejected(ValueError):
    """A provider request was definitively rejected."""


class OctopClient:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.buffer = bytearray()

    def peer_open(self):
        if not select.select([self.reader], [], [], 0)[0]:
            return True
        chunk = os.read(self.reader, 65536)
        self.buffer.extend(chunk)
        if len(self.buffer) > MAX_LINE:
            raise ValueError("OCTOP response exceeds limit")
        return bool(chunk)

    def _read(self, deadline):
        while b"\n" not in self.buffer:
            remaining = deadline - monotonic()
            if remaining <= 0 or not select.select([self.reader], [], [], remaining)[0]:
                raise TimeoutError("OCTOP response deadline")
            chunk = os.read(self.reader, min(65536, MAX_LINE + 1 - len(self.buffer)))
            if not chunk:
                raise ConnectionError("OCTOP bridge closed")
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_LINE:
                raise ValueError("OCTOP response exceeds limit")
        line, _, remainder = self.buffer.partition(b"\n")
        self.buffer = remainder
        return document(line)

    def call(self, operation, payload, *, timeout):
        ident = uuid4().hex
        deadline = monotonic() + timeout
        raw = (encode(dict(protocol=PROTOCOL, id=ident, operation=operation,
                           timeout=min(90, timeout), payload=payload)) + "\n").encode()
        if len(raw) > MAX_LINE:
            raise ValueError("OCTOP request exceeds limit")
        remaining_bytes = memoryview(raw)
        while remaining_bytes:
            remaining = deadline - monotonic()
            if remaining <= 0 or not select.select([], [self.writer], [], remaining)[1]:
                raise TimeoutError("OCTOP request deadline")
            try:
                written = os.write(self.writer, remaining_bytes[:4096])
            except BrokenPipeError as exc:
                raise ConnectionError("OCTOP bridge closed") from exc
            remaining_bytes = remaining_bytes[written:]
        while True:
            envelope = self._read(deadline)
            if envelope.get("protocol") != PROTOCOL:
                raise ValueError("invalid OCTOP response protocol")
            if envelope.get("id") == ident:
                result = envelope["result"]
                if not isinstance(result, dict):
                    raise ValueError("invalid OCTOP result")
                return result
