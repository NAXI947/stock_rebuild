"""Emit a script that runs tools/publish_etf_taxonomy.py inside the worker container.

The production image does not ship tools/, and the container reads its settings from the
process environment instead of an environment file. The emitted script embeds the publish
tool verbatim and is piped into `docker exec -i ... python -B -`.
"""
import argparse
import json
from pathlib import Path

TEMPLATE = '''import json, os, sys, tempfile, types
from datetime import datetime
from pathlib import Path
SRC = {source}
TAX = {taxonomy}
MAPPING = os.environ.get("APP_MAPPING_FILE", "/app/data/etf-mapping.json")
tool = types.ModuleType("publish_etf_taxonomy")
exec(compile(SRC, "publish_etf_taxonomy.py", "exec"), tool.__dict__)
mode = os.environ.get("TAXONOMY_MODE", "DRY")
if mode == "DRY":
    settings = tool.Settings.from_values(os.environ)
    ledger = tool.PostgresLedger(settings, "LIVE" if settings.env == "production" else "FROZEN")
    at = datetime.now(tool.CHINA)
    current = tool.live_mapping(ledger, Path(MAPPING), at)
    taxonomy = tool.reviewed_taxonomy(json.loads(TAX), current, at)
    previous = current.get("etf_taxonomy") or {{}}
    print(json.dumps(dict(status="DRY_OK", env=settings.env, namespace=ledger.ns, mapping_version=current["version"],
                          previous_taxonomy=previous.get("version"), previous_rows=len(previous.get("rows", [])),
                          new_taxonomy=taxonomy["version"], new_rows=len(taxonomy["rows"]))))
elif mode == "PUBLISH":
    tool.Settings.read = classmethod(lambda cls, path: cls.from_values(os.environ))
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
        handle.write(TAX)
    try:
        sys.argv = ["publish_etf_taxonomy", "--environment-file", "/proc/self/environ",
                    "--mapping-file", MAPPING, "--taxonomy-file", handle.name]
        tool.main()
    finally:
        os.unlink(handle.name)
else:
    raise SystemExit("TAXONOMY_MODE must be DRY or PUBLISH")
'''


def container_script(taxonomy_text):
    json.loads(taxonomy_text)
    source = Path(__file__).with_name("publish_etf_taxonomy.py").read_text(encoding="utf-8")
    return TEMPLATE.format(source=json.dumps(source, ensure_ascii=False),
                           taxonomy=json.dumps(taxonomy_text, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--taxonomy-file", type=Path, required=True)
    args = parser.parse_args()
    print(container_script(args.taxonomy_file.read_text(encoding="utf-8")), end="")


if __name__ == "__main__":
    main()
