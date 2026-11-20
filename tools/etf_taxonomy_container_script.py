"""Emit a script that runs tools/publish_etf_taxonomy.py inside the worker container.

The production image does not ship tools/, and the container reads its settings from the
process environment instead of an environment file. The emitted script embeds the publish
tool verbatim and is piped into `docker exec -i ... python -B -`.
"""
import argparse
import json
from pathlib import Path

TEMPLATE = '''import json, os, types
from datetime import datetime
from pathlib import Path
SRC = {source}
TAX = {taxonomy}
mode = os.environ.get("TAXONOMY_MODE", "DRY")
if mode not in ("DRY", "PUBLISH"):
    raise SystemExit("TAXONOMY_MODE must be DRY or PUBLISH")
tool = types.ModuleType("publish_etf_taxonomy")
exec(compile(SRC, "publish_etf_taxonomy.py", "exec"), tool.__dict__)
settings = tool.Settings.from_values(os.environ)
ledger = tool.PostgresLedger(settings, "LIVE" if settings.env == "production" else "FROZEN")
result = tool.publish(ledger, Path(os.environ.get("APP_MAPPING_FILE", "/app/data/etf-mapping.json")),
                      json.loads(TAX), datetime.now(tool.CHINA), dry_run=mode == "DRY")
print(json.dumps(dict(result, env=settings.env, namespace=ledger.ns)))
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
