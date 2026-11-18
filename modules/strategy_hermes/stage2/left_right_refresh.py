"""Scheduled LEFT/RIGHT refresh from already committed evidence only."""
from datetime import date, datetime, time
import logging

import psycopg

from ..ledger import encode, fingerprint
from ..simulator import CHINA, moment
from .inputs import document, trading_days
from .left_right_eod import MAX_CODES, STOCK, _database_context, _database_input, produce

DATASET = "left_right_eod"
START = time(15, 35)
INTERVAL = 300
LOG = logging.getLogger(__name__)


def load_archive(ledger):
    with ledger._connect() as conn:
        row = conn.execute("SELECT body FROM snapshot_state WHERE ns=%s AND dataset=%s",
                           (ledger.ns, DATASET)).fetchone()
        latest = conn.execute("""SELECT trade_date FROM left_right_assessments WHERE ns=%s AND status='READY'
            ORDER BY trade_date DESC LIMIT 1""", (ledger.ns,)).fetchone()
        old = document(row[0]) if row else None
        if not latest or old and old["trade_date"] > latest[0].isoformat():
            return old
        rows = conn.execute("""SELECT body FROM left_right_assessments WHERE ns=%s AND trade_date=%s
            ORDER BY setup_score DESC NULLS LAST,code,family LIMIT 100""", (ledger.ns, latest[0])).fetchall()
    items = [_archive(raw) for (raw,) in rows]
    return dict(schema_version="stock-v4-left-right-eod-v1", trade_date=latest[0].isoformat(),
                observed_at=items[0]["observed_at"], source=items[0]["source"],
                items=[dict(row, code=row["ts_code"], side=row["family"]) for row in items])


def _archive(raw):
    return document(raw) if isinstance(raw, (str, bytes)) else raw


def _item_key(row):
    return row.get("code", row.get("ts_code")), row.get("side", row.get("family"))


def _dated_items(archive):
    return [{**row, "observed_at": row.get("observed_at", archive.get("observed_at")),
             "published_at": row.get("published_at", archive.get("input_as_of")),
             "evidence_sha256": row.get("evidence_sha256", archive.get("evidence_sha256")),
             "source": row.get("source", archive.get("source")),
             "version_id": row.get("version_id", archive.get("model_versions", {}).get(row.get("side")))}
            for row in archive["items"]]


def _merge_archive(previous, archive):
    if not previous or previous["trade_date"] != archive["trade_date"]:
        return {**archive, "items": _dated_items(archive)}, True
    items = {_item_key(row): row for row in _dated_items(previous)}
    changed = False
    for row in _dated_items(archive):
        key, prior = _item_key(row), items.get(_item_key(row))
        if prior:
            if prior.get("evidence_sha256") == row.get("evidence_sha256"):
                continue
            if moment(prior["published_at"]) >= moment(row["published_at"]):
                continue
        items[key] = row
        changed = True
    if not changed:
        return previous, False
    rows = [items[key] for key in sorted(items)]
    # Aggregate provenance covers the retained rows; every row keeps its own
    # exact evidence identity and publication time after a subset recompute.
    return {**archive, "items": rows,
            "input_as_of": max(rows, key=lambda row: moment(row["published_at"]))["published_at"],
            "source_batches": sorted(set(previous.get("source_batches", [])) | set(archive.get("source_batches", []))),
            "evidence_sha256": fingerprint([[row["code"], row["side"], row["evidence_sha256"]] for row in rows])}, True


def _persist_rows(conn, ledger, archive):
    """One bounded SQL upsert retains per-assessment provenance on subset reruns."""
    from ..left_right_info import _normalize
    normalized = _normalize(archive, set())
    originals = {_item_key(row): row for row in _dated_items(archive)}
    records = [dict(trade_date=row["trade_date"], code=row["ts_code"], family=row["family"],
                    published_at=row["published_at"], evidence_sha256=originals[(row["ts_code"], row["family"])]["evidence_sha256"],
                    status=row["status"], setup_score=row["setup_score"], return_ratio=row["return_ratio"],
                    name=row["name"], version_id=row["version_id"], source=row["source"], body=row)
               for row in normalized["items"]]
    changed = conn.execute("""INSERT INTO left_right_assessments
        (ns,trade_date,code,family,published_at,evidence_sha256,status,setup_score,return_ratio,name,version_id,source,body)
        SELECT %s,r.* FROM jsonb_to_recordset(%s::jsonb) AS r(trade_date date,code text,family text,
            published_at timestamptz,evidence_sha256 text,status text,setup_score numeric,return_ratio numeric,
            name text,version_id text,source text,body jsonb)
        ON CONFLICT (ns,trade_date,code,family) DO UPDATE SET published_at=excluded.published_at,
            evidence_sha256=excluded.evidence_sha256,status=excluded.status,setup_score=excluded.setup_score,
            return_ratio=excluded.return_ratio,name=excluded.name,version_id=excluded.version_id,
            source=excluded.source,body=excluded.body
        WHERE left_right_assessments.published_at<excluded.published_at
          AND left_right_assessments.evidence_sha256 IS DISTINCT FROM excluded.evidence_sha256
        RETURNING code,family""", (ledger.ns, encode(records))).fetchall()
    if not changed:
        return False
    keys = set(changed)
    manifests = {}
    for key in keys:
        row = originals[key]
        digest = row["evidence_sha256"]
        manifests.setdefault(digest, dict(schema_version="stock-v4-left-right-evidence-manifest-v1",
            evidence_sha256=digest, trade_date=archive["trade_date"], observed_at=row["observed_at"],
            input_as_of=row["published_at"], source=row["source"], source_batches=archive.get("source_batches", [])))
    for digest, manifest in manifests.items():
        # The hash links each assessment to its durable supplier batch lineage.
        # A chunk shares one manifest; do not repeat hundreds of batch IDs in
        # every stock row or store the full market's daily input JSON again.
        conn.execute("""INSERT INTO snapshot_state(ns,dataset,observed_at,revision,body)
            VALUES(%s,%s,%s,0,%s) ON CONFLICT(ns,dataset) DO NOTHING""",
            (ledger.ns, "left_right_evidence:"+digest, moment(manifest["observed_at"]), encode(manifest)))
    changed_json = encode([dict(code=code, family=family) for code, family in changed])
    conn.execute("""DELETE FROM left_right_assessment_sectors s USING
        jsonb_to_recordset(%s::jsonb) AS r(code text,family text)
        WHERE s.ns=%s AND s.trade_date=%s AND s.code=r.code AND s.family=r.family""",
        (changed_json, ledger.ns, archive["trade_date"]))
    links = [dict(code=row["ts_code"], family=row["family"], sector_id=sid)
             for row in normalized["items"] if (row["ts_code"], row["family"]) in keys
             for sid in sorted({link["id"] for link in row["sectors"]})]
    if links:
        conn.execute("""INSERT INTO left_right_assessment_sectors(ns,trade_date,code,family,sector_id)
            SELECT %s,%s,r.* FROM jsonb_to_recordset(%s::jsonb) AS r(code text,family text,sector_id text)
            ON CONFLICT DO NOTHING""", (ledger.ns, archive["trade_date"], encode(links)))
    return True


def persist_archive(ledger, archive, *, indexed_only=False):
    """Keep every dated result, merging subset codes; latest requires READY."""
    if not archive or not archive.get("items"):
        return dict(history=False, latest=False, state="UNCHANGED")
    dataset = DATASET + ":" + archive["trade_date"]
    changes = dict(history=False, latest=False)
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (ledger.ns+":left-right-archive",))
        if indexed_only:
            changed = _persist_rows(conn, ledger, archive)
            return dict(history=changed, latest=changed and any(row["status"] == "READY" for row in archive["items"]),
                        state="ARCHIVED" if changed else "UNCHANGED")
        stored = conn.execute("SELECT dataset,body FROM snapshot_state WHERE ns=%s AND dataset=ANY(%s) FOR UPDATE",
                              (ledger.ns, [dataset, DATASET])).fetchall()
        previous = {name: _archive(raw) for name, raw in stored}
        # Existing small snapshots may predate the indexed store. Import their
        # retained rows together with this subset, preserving each row's dates.
        merged_history, _ = _merge_archive(previous.get(dataset) or previous.get(DATASET), archive)
        _persist_rows(conn, ledger, merged_history)
        for name, key in ((dataset, "history"), (DATASET, "latest")):
            prior = previous.get(name)
            if key == "latest" and (not any(row["status"] == "READY" for row in archive["items"])
                    or prior and prior["trade_date"] > archive["trade_date"]):
                continue
            merged, changed = _merge_archive(prior, archive)
            if not changed:
                continue
            conn.execute("""INSERT INTO snapshot_state(ns,dataset,observed_at,revision,body) VALUES (%s,%s,%s,0,%s)
                ON CONFLICT (ns,dataset) DO UPDATE SET observed_at=excluded.observed_at,
                    revision=snapshot_state.revision+1,body=excluded.body""",
                (ledger.ns, name, moment(merged["observed_at"]), encode(merged)))
            changes[key] = True
    return dict(changes, state="PUBLISHED" if changes["latest"] else "ARCHIVED" if changes["history"] else "UNCHANGED")


def build_manual_archives(ledger, dates, codes, as_of, *, checkpoint=None, persist=False, progress=None):
    """Construct requested historical assessments only from committed inputs."""
    dates = [value.isoformat() if isinstance(value, date) else value for value in dates]
    all_market = codes is None
    codes = list(codes) if codes is not None else []
    as_of = moment(as_of.isoformat() if isinstance(as_of, datetime) else as_of)
    if (not 1 <= len(dates) <= 120 or len(set(dates)) != len(dates)
            or not all_market and not 1 <= len(codes) <= MAX_CODES or len(set(codes)) != len(codes)
            or any(not isinstance(code, str) or not STOCK.fullmatch(code) for code in codes)):
        raise ValueError("bounded unique LEFT/RIGHT dates and codes required")
    if all_market and not persist:
        raise ValueError("full-market LEFT/RIGHT requires incremental persistence")
    days = sorted(date.fromisoformat(value) for value in dates)
    if any(as_of < datetime.combine(day, START, CHINA) for day in days):
        raise ValueError("LEFT/RIGHT historical day has not completed")
    if checkpoint:
        checkpoint()
    with ledger._connect() as conn:
        available = conn.execute("""SELECT c.cal_date FROM supplier_trade_calendar c
            JOIN supplier_batches b ON b.batch_id=c.batch_id WHERE c.exchange='SSE' AND c.is_open
            AND c.cal_date=ANY(%s) AND b.collected_at<=%s ORDER BY c.cal_date""", (days, as_of)).fetchall()
    calendar = [row[0].isoformat() for row in available]
    archives, missing, missing_count, ready_items, expected_items, saved_days, universe_count = [], [], 0, 0, 0, 0, 0

    def absent(**row):
        nonlocal missing_count
        missing_count += 1
        if len(missing) < 100:
            missing.append(row)

    for day_index, day in enumerate(days):
        if checkpoint:
            checkpoint()
        selected = codes
        if all_market:
            with ledger._connect() as conn:
                rows = conn.execute("""SELECT d.code FROM supplier_daily_bars d JOIN supplier_batches b USING(batch_id)
                    WHERE d.trade_date=%s AND b.collected_at<=%s ORDER BY d.code""", (day, as_of)).fetchall()
            selected = [code for (code,) in rows if STOCK.fullmatch(code)]
        universe_count += len(selected)
        expected_items += max(1, len(selected))*2
        if day.isoformat() not in calendar:
            for code in selected or [None]:
                absent(trade_date=day.isoformat(), code=code, reason="TRADING_CALENDAR_UNAVAILABLE")
            continue
        if not selected:
            absent(trade_date=day.isoformat(), code=None, reason="NO_SAVED_MARKET_CLOSES")
            continue
        context = _database_context(ledger, day, selected, as_of, checkpoint=checkpoint) if all_market else None
        day_ready, day_saved = 0, False
        for start in range(0, len(selected), MAX_CODES):
            chunk = selected[start:start+MAX_CODES]
            if checkpoint:
                checkpoint()
            kwargs = {"checkpoint": checkpoint} if checkpoint else {}
            if context is not None:
                kwargs["context"] = context
            evidence = _database_input(ledger, day, chunk, as_of, **kwargs)
            archive = produce(day, calendar, as_of, evidence)
            if checkpoint:
                checkpoint()
            represented = set()
            if archive:
                if persist:
                    persist_archive(ledger, archive, indexed_only=all_market)
                    day_saved = True
                else:
                    archives.append(archive)
                for row in archive["items"]:
                    represented.add(row["code"])
                    if row["status"] == "READY":
                        ready_items += 1
                        day_ready += 1
                    else:
                        absent(trade_date=day.isoformat(), code=row["code"], family=row["side"],
                               reason="ASSESSMENT_INPUT_MISSING", details=row["reasons"])
            for code in chunk:
                if code not in represented:
                    absent(trade_date=day.isoformat(), code=code, reason="NO_SAVED_CLOSE")
            if checkpoint:
                checkpoint()
            if progress:
                progress(phase="COMPUTING", trade_date=day.isoformat(), processed_stocks=min(start+MAX_CODES, len(selected)),
                         ready_items=day_ready, completed_days=day_index, total_days=len(days))
        saved_days += int(day_saved)
        if progress:
            progress(phase="COMPUTING", trade_date=day.isoformat(), processed_stocks=len(selected),
                     ready_items=day_ready, completed_days=day_index+1, total_days=len(days))
    return dict(archives=archives, missing=missing, missing_count=missing_count, ready_items=ready_items,
                expected_items=expected_items, archives_saved=saved_days, universe_count=universe_count)


def refresh(ledger, calendar, codes, now):
    day = now.date()
    if now.time() < START or day.isoformat() not in calendar:
        return "OUTSIDE_WINDOW"
    evidence = _database_input(ledger, day, codes, now)
    archive = produce(day, calendar, now, evidence)
    if archive is None:
        return "RETAINED"
    return persist_archive(ledger, archive)["state"]


class LeftRightRefresh:
    def __init__(self, ledger, calendar_file, scope):
        self.ledger, self.calendar_file = ledger, calendar_file
        self.codes = tuple(part.strip().upper() for part in scope.split(","))
        if (not 1 <= len(self.codes) <= MAX_CODES or len(set(self.codes)) != len(self.codes)
                or any(not STOCK.fullmatch(code) for code in self.codes) or calendar_file is None):
            raise ValueError("LEFT/RIGHT needs 1–50 explicit stock codes and a trading calendar")
        self.next_check = 0

    def tick(self, monotonic_now, *, now=None):
        now = now or datetime.now(CHINA)
        if now.time() < START or monotonic_now < self.next_check:
            return None
        self.next_check = monotonic_now + INTERVAL
        try:
            return refresh(self.ledger, trading_days(self.calendar_file), self.codes, now)
        except (OSError, ValueError, TypeError, KeyError, psycopg.Error) as exc:
            LOG.warning("LEFT/RIGHT archive refresh failed: %s", type(exc).__name__)
            return "ERROR"
