"""Produce read-only market background from a saved Kaipanla 072 response."""
import argparse
from hashlib import sha256
from pathlib import Path
import re
import sys

from shared_kernel.temp_manager import TempManager, store_file
from ..ledger import encode, fingerprint
from ..simulator import decimal, moment
from .inputs import MAX_INPUT, document, read_file

SCHEMA = "stock-v4-macro-background-v1"
GROUPS = {
    "CYWWZS": "全球股指",
    "RMGZ": "股指期货",
    "QQSP": "全球商品",
    "HLZS": "汇率",
}
INDEX_CODE = re.compile(r"[A-Za-z0-9@.:_-]{1,24}\Z")
MAX_GROUP_ROWS = 20


def _positive(value):
    try:
        number = decimal(value)
        return str(number) if number > 0 else None
    except ValueError:
        return None


def _ratio(value):
    if not isinstance(value, str) or not value.endswith("%"):
        return None
    try:
        return str(decimal(value[:-1].strip()) / 100)
    except ValueError:
        return None


def normalize(capture, digest):
    """Keep only registered index, futures, commodity and FX groups."""
    record = capture.get("直调返回")
    if capture.get("id") not in (72, "072", "72") or not isinstance(record, dict):
        raise ValueError("expected a saved 072 provider capture")
    response = record.get("return_json")
    if (record.get("ok") is not True or record.get("http_status") != 200
            or not isinstance(response, dict) or response.get("errcode") not in ("0", 0)):
        raise ValueError("saved 072 response was not successful")
    result = []
    for key, group in GROUPS.items():
        rows = response.get(key, [])
        if not isinstance(rows, list) or len(rows) > MAX_GROUP_ROWS:
            raise ValueError("invalid 072 background group")
        seen = set()
        for row in rows:
            code, label = row.get("code"), row.get("prod_name")
            if (not isinstance(code, str) or not INDEX_CODE.fullmatch(code) or code in seen
                    or not isinstance(label, str) or not label.strip() or len(label) > 80):
                raise ValueError("invalid 072 background identity")
            seen.add(code)
            label = " ".join(label.split())
            result.append(dict(group=group, code=code, name=label, price=_positive(row.get("last_px")),
                               return_ratio=_ratio(row.get("increase_rate")), observed_at=None))
    if not result:
        raise ValueError("saved 072 response has no background entries")
    # The supplied 072 contract has no provider observation timestamp. Neither
    # latency metadata nor the local capture file's mtime is the market clock.
    return dict(schema_version=SCHEMA, source="开盘啦 072 历史接口样例",
                observed_at=None, observation_status="SOURCE_TIME_UNAVAILABLE",
                source_sha256=digest, items=result)


def read_background(path):
    if not path or not Path(path).is_file():
        return None
    body = read_file(path)
    if (body.get("schema_version") != SCHEMA or not isinstance(body.get("source"), str)
            or len(body["source"]) > 100 or not isinstance(body.get("items"), list)
            or not 1 <= len(body["items"]) <= len(GROUPS) * MAX_GROUP_ROWS):
        raise ValueError("invalid market background archive")
    if body.get("observed_at") is not None:
        moment(body["observed_at"])
    for item in body["items"]:
        if (not isinstance(item, dict) or item.get("group") not in GROUPS.values()
                or not INDEX_CODE.fullmatch(item.get("code") or "")
                or not isinstance(item.get("name"), str) or not 1 <= len(item["name"]) <= 80
                or item.get("price") is not None and _positive(item["price"]) is None):
            raise ValueError("invalid market background entry")
        if item.get("return_ratio") is not None:
            decimal(item["return_ratio"])
        if item.get("observed_at") is not None:
            moment(item["observed_at"])
    return {key: body.get(key) for key in ("source", "observed_at", "observation_status", "items")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--tmp", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if bool(args.tmp) != bool(args.output_dir):
            raise ValueError("provide both managed temporary and persistent output directories")
        with args.capture.open("rb") as stream:
            raw = stream.read(MAX_INPUT+1)
        body = normalize(document(raw.decode("utf-8-sig")), sha256(raw).hexdigest())
        if args.output_dir:
            output = args.output_dir
            if not output.is_absolute() or output.resolve() != output or not output.is_dir():
                raise ValueError("output requires a persistent absolute directory")
            manager = TempManager(args.tmp, persistent=output)
            path = store_file(manager, output, "market-background-"+fingerprint(body)[:24]+".json",
                              encode(body).encode())
            print(encode({"status": "ARCHIVED", "path": path, "rows": len(body["items"])}))
        else:
            print(encode(body))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(f"market background: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
