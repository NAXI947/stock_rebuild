"""Persist and copy isolated manual backtest revisions."""
import json
from pathlib import Path
import re

from shared_kernel.temp_manager import store_file
from .ledger import encode, fingerprint
from .manual_portfolio import SCHEMA, execution_version, replay
from .simulator import moment

REVISION = re.compile(r"manual-[a-f0-9]{32}\Z")


class ManualCases:
    def __init__(self, directory):
        self.directory = Path(directory)
        project = Path(__file__).resolve().parents[2]
        if (not self.directory.is_absolute() or self.directory.resolve() != self.directory
                or not self.directory.is_dir() or self.directory.stat().st_mode & 0o077
                or self.directory.is_relative_to(project)):
            raise ValueError("manual case store requires a private directory outside the project")

    def get(self, revision):
        if not REVISION.fullmatch(revision):
            raise ValueError("invalid manual revision")
        file = self.directory / (revision + ".json")
        if file.is_symlink():
            raise ValueError("invalid manual revision path")
        with file.open(encoding="utf-8") as stream:
            return json.load(stream)

    def save(self, manager, bundle, *, parent=None):
        if parent is not None:
            self.get(parent)
        result = replay(bundle)
        identity = "manual-" + fingerprint([execution_version(bundle), parent, bundle])[:32]
        record = {"schema_version": "stock-v4-manual-revision-v1", "revision": identity,
                  "parent_revision": parent, "input": bundle, "result": result}
        store_file(manager, self.directory, identity + ".json", encode(record).encode())
        return record


def copy_auto(ledger, cycle_id, *, cutoff=None):
    """Read a Stage 2 automatic cycle and quote evidence from one database snapshot."""
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        row = conn.execute(
            """SELECT c.body,c.opened_at,r.config,r.watermark,r.origin
               FROM cycles c JOIN runs r ON c.ns=r.ns WHERE c.ns=%s AND c.id=%s""",
            (ledger.ns, cycle_id),
        ).fetchone()
        if not row or row[4] != ledger.origin:
            raise ValueError("automatic cycle unavailable in the selected run")
        raw, opened, raw_config, watermark, origin = row
        cycle, config = json.loads(raw), json.loads(raw_config)
        end = moment(cutoff or watermark)
        if end > moment(watermark) or end < moment(opened):
            raise ValueError("manual copy cutoff outside the recorded automatic run")
        quotes = conn.execute(
            """SELECT payload FROM events WHERE ns=%s AND kind='quote' AND payload->>'etf'=%s
               AND at >= %s AND at <= %s ORDER BY at,id LIMIT 100001""",
            (ledger.ns, cycle["etf"], moment(opened).isoformat(), end.isoformat()),
        ).fetchall()
    rule = cycle["rule"]
    actions = {"BUY": ("BUY", "CASH_RATIO", "1"),
               "REDUCE": ("REDUCE", "POSITION_RATIO", rule["reduce_ratio"]),
               "EXIT": ("SELL", "ALL", None)}
    nodes, original_ids = [], []
    for order in cycle["orders"]:
        at = moment(order["requested_at"])
        if at > end:
            continue
        action, mode, size = actions[order["action"]]
        nodes.append(dict(id="auto-node-" + str(len(nodes) + 1), etf=cycle["etf"], at=at.isoformat(),
                          action=action, size_mode=mode, size=size))
        original_ids.append(order["id"])
    history = [json.loads(row[0]) for row in quotes]
    if origin == "FROZEN":
        for quote in history:
            quote["price_kind"] = "ASSUMED"
    return dict(schema_version=SCHEMA, calendar=config["calendar"], cutoff=end.isoformat(),
                instruments={cycle["etf"]:cycle["unit"]}, initial_cash=rule["initial_cash"],
                costs={key:rule[key] for key in ("fee_rate","minimum_fee","slippage_bps")},
                price_mode="HISTORICAL" if origin == "LIVE" else "ASSUMPTION", nodes=nodes, quotes=history,
                source=dict(kind="STAGE2_AUTOMATIC_CYCLE", origin=origin, namespace=ledger.ns,
                            cycle_id=cycle_id, automatic_order_ids=original_ids))
