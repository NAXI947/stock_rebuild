"""Bounded LEFT/RIGHT history views from saved, dated evidence only."""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import parse_qs

from .ledger import encode, fingerprint
from .simulator import CHINA
from .stage2.inputs import read_file
from .stage2.left_right_features.scores import LeftSetup, RightSetup

HISTORY_SCHEMA = "stock-v4-left-right-history-v1"
EOD_SCHEMA = "stock-v4-left-right-eod-v1"
MAX_DAYS = 120
MAX_ITEMS = 10000
STOCK = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z")
SECTOR = re.compile(r"kaipanla:[0-9]{6}\Z")
VERSIONS = {"left_v3.1.2_adj": LeftSetup, "right_v3.1.2_adj": RightSetup}


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("invalid LEFT/RIGHT date")
    return value


def _text(value, maximum=500):
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= maximum else None


def _number(value, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        return None
    if (not number.is_finite() or len(number.as_tuple().digits) > 38
            or abs(number.as_tuple().exponent) > 100
            or minimum is not None and number < minimum
            or maximum is not None and number > maximum):
        return None
    return str(number)


def _timestamp(value, issues):
    if value is None:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError("timezone required")
        return stamp.astimezone(CHINA).isoformat()
    except (ValueError, TypeError, OverflowError):
        issues.add("LR_TIMESTAMP_INVALID")
        return None


def _object(raw):
    raw = json.loads(raw, parse_float=str) if isinstance(raw, str) else raw
    if not isinstance(raw, dict):
        raise ValueError("saved LEFT/RIGHT object required")
    return raw


def _read_document(path, kind, issues):
    if path is None:
        return None
    try:
        return read_file(path)
    except FileNotFoundError:
        issues.add(kind + "_FILE_MISSING")
    except (OSError, ValueError, UnicodeError):
        issues.add(kind + "_FILE_INVALID")
    return None


def _sectors(raw, day, issues):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 500:
        issues.add("LR_SECTOR_LINK_INVALID")
        return []
    result = {}
    for row in raw:
        if not isinstance(row, dict):
            issues.add("LR_SECTOR_LINK_INVALID")
            continue
        sid, source, batch = row.get("id"), _text(row.get("source")), _text(row.get("source_batch"), 200)
        try:
            membership_day = _day(row.get("membership_date"))
        except (ValueError, TypeError):
            membership_day = None
        if (not isinstance(sid, str) or not SECTOR.fullmatch(sid) or not source or not batch
                or membership_day is None or membership_day > day):
            issues.add("LR_SECTOR_LINK_INVALID")
            continue
        result[(sid, membership_day, batch)] = dict(id=sid, name=_text(row.get("name")),
            membership_date=membership_day, source=source, source_batch=batch)
    return [result[key] for key in sorted(result)]


def _subscores(row, version):
    saved = row.get("subscores") if isinstance(row.get("subscores"), dict) else {}
    parts = saved.get("parts", row.get("parts"))
    parts = ({key: _number(value, minimum=0, maximum=100) for key, value in parts.items()
              if _text(key, 80)} if isinstance(parts, dict) and len(parts) <= 40 else None)
    weights = saved.get("weights", row.get("weights"))
    if weights is None and version in VERSIONS:
        weights = VERSIONS[version].__dataclass_fields__["weights"].default_factory()
    weights = ({key: _number(value, minimum=0, maximum=1) for key, value in weights.items()
                if _text(key, 80)} if isinstance(weights, dict) and len(weights) <= 40 else None)
    return dict(parts=parts, weights=weights)


def _normalize(raw, issues):
    raw = _object(raw)
    if raw.get("schema_version") != EOD_SCHEMA:
        raise ValueError("invalid LEFT/RIGHT archive schema")
    day = _day(raw.get("trade_date"))
    entries = raw.get("items")
    if not isinstance(entries, list) or len(entries) > MAX_ITEMS:
        raise ValueError("invalid LEFT/RIGHT archive items")
    observed = _timestamp(raw.get("observed_at"), issues)
    published = _timestamp(raw.get("input_as_of", raw.get("published_at")), issues)
    source = _text(raw.get("source"))
    versions = raw.get("model_versions") if isinstance(raw.get("model_versions"), dict) else {}
    items = {}
    for row in entries:
        if not isinstance(row, dict):
            issues.add("LR_ITEM_INVALID")
            continue
        code, family = row.get("ts_code", row.get("code")), row.get("family", row.get("side"))
        if not isinstance(code, str) or not STOCK.fullmatch(code) or family not in ("LEFT", "RIGHT"):
            issues.add("LR_ITEM_INVALID")
            continue
        version = _text(row.get("version_id", versions.get(family)), 160)
        reasons = row.get("reasons")
        reasons = ([_text(value) for value in reasons if _text(value)]
                   if isinstance(reasons, list) and len(reasons) <= 100 else None)
        row_observed = _timestamp(row.get("observed_at", observed), issues)
        row_published = _timestamp(row.get("published_at", published), issues)
        identity = dict(trade_date=day, ts_code=code, family=family, version_id=version,
                        published_at=row_published,
                        evidence_sha256=_text(row.get("evidence_sha256", raw.get("evidence_sha256")), 200))
        item = dict(assessment_id=fingerprint(identity), ts_code=code, name=_text(row.get("name")),
            trade_date=day, family=family, version_id=version, status=_text(row.get("status"), 80),
            setup_score=_number(row.get("setup_score", row.get("score")), minimum=0, maximum=100),
            close=_number(row.get("close"), minimum=0), return_ratio=_number(row.get("return_ratio")),
            volume_ratio=_number(row.get("volume_ratio"), minimum=0),
            turnover_ratio=_number(row.get("turnover_ratio"), minimum=0),
            subscores=_subscores(row, version), quality=row.get("quality") if isinstance(row.get("quality"), dict) else None,
            reasons=reasons, note=_text(row.get("note")),
            observed_at=row_observed, published_at=row_published,
            source=_text(row.get("source", source)), sectors=_sectors(row.get("sectors"), day, issues))
        key = (code, family)
        if key in items:
            issues.add("LR_DUPLICATE_ITEM")
            continue
        items[key] = item
    return dict(trade_date=day, source=source, observed_at=observed,
                published_at=published, items=list(items.values()))


def _merge_same_revision(previous, added):
    """Enrichment of one revision must not remove other symbols or families."""
    items = {(row["ts_code"], row["family"]): row for row in previous["items"]}
    for row in added["items"]:
        key = (row["ts_code"], row["family"])
        if key not in items:
            items[key] = row
            continue
        old = items[key]
        merged = {**old, **{key: value for key, value in row.items()
                           if value is not None and key not in ("assessment_id", "sectors", "subscores")}}
        links = {(link["id"], link["membership_date"], link["source_batch"]): link
                 for link in old["sectors"] + row["sectors"]}
        merged["sectors"] = [links[key] for key in sorted(links)]
        merged["subscores"] = {}
        for field in ("parts", "weights"):
            first, second = old["subscores"][field], row["subscores"][field]
            merged["subscores"][field] = ({**(first or {}), **{key: value for key, value in (second or {}).items()
                                                       if value is not None}}
                                                if first is not None or second is not None else None)
        items[key] = merged
    return {**previous, "items": list(items.values())}


def load_history(ledger, eod_file=None, history_file=None, *, dates=None, include_database=True):
    """Read at most 120 dates; original availability selects each day's revision."""
    issues, documents = set(), []
    if include_database and dates != []:
        with ledger._connect() as conn:
            rows = conn.execute("""SELECT body FROM snapshot_state WHERE ns=%s
                AND (dataset='left_right_eod' OR dataset ~ '^left_right_eod:[0-9]{4}-[0-9]{2}-[0-9]{2}$')
                """ + (" AND body->>'trade_date'=ANY(%s) " if dates is not None else "") +
                "ORDER BY observed_at DESC,dataset DESC LIMIT 121",
                (ledger.ns, dates) if dates is not None else (ledger.ns,)).fetchall()
        documents.extend(raw for (raw,) in rows)
    if (eod := _read_document(eod_file, "LR_EOD", issues)) is not None:
        documents.append(eod)
    if (history := _read_document(history_file, "LR_HISTORY", issues)) is not None:
        archives = history.get("archives")
        if history.get("schema_version") != HISTORY_SCHEMA or not isinstance(archives, list) or len(archives) > MAX_DAYS:
            issues.add("LR_HISTORY_FILE_INVALID")
        else:
            documents.extend(archives)
    selected, identities = {}, {}
    for raw in documents:
        try:
            raw = _object(raw)
            if dates is not None and raw.get("trade_date") not in dates:
                continue
            archive = _normalize(raw, issues)
        except (ValueError, TypeError, KeyError, OverflowError):
            issues.add("LR_ARCHIVE_INVALID")
            continue
        day = archive["trade_date"]
        current = selected.get(day)
        stamp = lambda value: (datetime.fromisoformat(value["published_at"]).astimezone(timezone.utc)
                              if value["published_at"] else datetime.min.replace(tzinfo=timezone.utc))
        evidence = _text(raw.get("evidence_sha256"), 200)
        if current is None or stamp(archive) > stamp(current):
            selected[day] = archive
            identities[day] = evidence
        elif stamp(archive) == stamp(current):
            if evidence and identities[day] and evidence != identities[day]:
                issues.add("LR_ARCHIVE_REVISION_CONFLICT")
            else:
                selected[day] = _merge_same_revision(current, archive)
                identities[day] = identities[day] or evidence
    return dict(archives=[selected[day] for day in sorted(selected, reverse=True)[:MAX_DAYS]], issues=sorted(issues))


def _query(query):
    if not isinstance(query, str) or len(query) > 2000:
        raise ValueError("invalid LEFT/RIGHT query")
    args = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=9, errors="strict")
    if set(args) - {"family", "trade_date", "q", "page", "page_size", "structure_mode",
                    "min_left", "min_right_gain", "max_ma20_distance"} or any(len(v) != 1 for v in args.values()):
        raise ValueError("invalid LEFT/RIGHT query fields")
    family, day, query = (args.get(key, [default])[0] for key, default in
                          (("family", "RIGHT"), ("trade_date", ""), ("q", "")))
    if family not in ("LEFT", "RIGHT") or len(query) > 80:
        raise ValueError("invalid LEFT/RIGHT family or search")
    if day:
        _day(day)
    page, size = args.get("page", ["1"])[0], args.get("page_size", ["20"])[0]
    if any(not value.isascii() or not value.isdigit() or len(value) > 9 for value in (page, size)):
        raise ValueError("invalid LEFT/RIGHT pagination")
    page, size = int(page), int(size)
    if page < 1 or not 1 <= size <= 100:
        raise ValueError("invalid LEFT/RIGHT pagination")
    mode = args.get("structure_mode", ["ALL"])[0]
    if mode not in ("ALL", "STARTUP", "PULLBACK"):
        raise ValueError("invalid LEFT/RIGHT structure mode")
    parameters = {}
    for key, default, low, high in (("min_left", "70", 50, 90), ("min_right_gain", "3", 1, 15),
                                    ("max_ma20_distance", "0.10", Decimal(".03"), Decimal(".20"))):
        value = _number(args.get(key, [default])[0], minimum=low, maximum=high)
        if value is None:
            raise ValueError("invalid LEFT/RIGHT structure parameter: " + key)
        parameters[key] = float(value)
    return family, day, query.strip(), page, size, mode, parameters


def _available_dates(ledger):
    with ledger._connect() as conn:
        rows = conn.execute("""SELECT day FROM (
            SELECT trade_date::text AS day FROM (SELECT DISTINCT trade_date FROM left_right_assessments
                WHERE ns=%s ORDER BY trade_date DESC LIMIT 120) recent
            UNION SELECT body->>'trade_date' AS day FROM snapshot_state WHERE ns=%s
            AND (dataset='left_right_eod' OR dataset ~ '^left_right_eod:[0-9]{4}-[0-9]{2}-[0-9]{2}$')
            ) d WHERE day IS NOT NULL ORDER BY day DESC LIMIT 120""", (ledger.ns, ledger.ns)).fetchall()
    dates = []
    for (day,) in rows:
        try:
            dates.append(_day(day))
        except (ValueError, TypeError):
            continue
    return dates


def _indexed_info(ledger, family, day, query, page, page_size, seed_items=()):
    if day is None:
        return None
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        summary_sql = """SELECT count(*),count(setup_score),max(setup_score),count(return_ratio),
            count(*) FILTER(WHERE return_ratio>0),count(*) FILTER(WHERE return_ratio>=0.095),
            max(published_at),array_agg(DISTINCT version_id),min(source) FROM """
        source, prefix, where = "left_right_assessments", "", "ns=%s AND trade_date=%s AND family=%s"
        args = (ledger.ns, day, family)
        summary = conn.execute(summary_sql+source+" WHERE "+where, args).fetchone()
        if not summary or not summary[0]:
            return None
        if seed_items:
            # Static pre-migration archives are small. Preserve their other
            # symbols without decoding or merging all persisted history.
            records = [dict(code=row["ts_code"], status=row["status"], setup_score=row["setup_score"],
                            return_ratio=row["return_ratio"], name=row["name"], version_id=row["version_id"],
                            source=row["source"], published_at=row["published_at"], body=row)
                       for row in seed_items if row["family"] == family]
            prefix = """WITH saved AS (
                SELECT code,status,setup_score,return_ratio,name,version_id,source,published_at,body
                FROM left_right_assessments WHERE ns=%s AND trade_date=%s AND family=%s
                UNION ALL SELECT r.* FROM jsonb_to_recordset(%s::jsonb) AS r(code text,status text,
                    setup_score numeric,return_ratio numeric,name text,version_id text,source text,
                    published_at timestamptz,body jsonb)
                WHERE NOT EXISTS(SELECT 1 FROM left_right_assessments a
                    WHERE a.ns=%s AND a.trade_date=%s AND a.family=%s AND a.code=r.code)) """
            args = (ledger.ns, day, family, encode(records), ledger.ns, day, family)
            source, where = "saved", "true"
            summary = conn.execute(prefix+summary_sql+source, args).fetchone()
        # strpos treats %, _ and backslashes as ordinary search characters.
        if query:
            where += " AND strpos(lower(code||' '||coalesce(name,'')),lower(%s))>0"
            args += (query,)
        total = conn.execute(prefix+"SELECT count(*) FROM "+source+" WHERE "+where, args).fetchone()[0] if query else summary[0]
        rows = conn.execute(prefix+"SELECT body FROM "+source+" WHERE "+where+
            " ORDER BY setup_score DESC NULLS LAST,code LIMIT %s OFFSET %s",
            args+(page_size, (page-1)*page_size)).fetchall()
    versions = [value for value in summary[7] if value]
    return dict(summary=dict(total=summary[0], valid_score_count=summary[1],
                    max_score=str(summary[2]) if summary[2] is not None else None,
                    valid_return_count=summary[3], up_count=summary[4], high_return_count=summary[5]),
                batch=dict(published_at=summary[6].astimezone(CHINA).isoformat(),
                           version_id=versions[0] if len(versions) == 1 else None, source=summary[8]),
                items=[_object(raw) for (raw,) in rows], total=total, pages=(total+page_size-1)//page_size)


def load_sector_groups(ledger, selections):
    """Read at most five saved assessments per requested sector/family in SQL."""
    if not selections:
        return {}, set()
    result = {}
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        # A newly populated namespace may still be estimated as one row until
        # autovacuum updates statistics. A nested-loop history scan would then
        # rescan the whole daily universe for every membership. These bounded
        # market-wide joins use hash/merge plans within this read transaction.
        conn.execute("SET LOCAL enable_nestloop=off")
        existing = conn.execute("""SELECT DISTINCT trade_date FROM left_right_assessments
            WHERE ns=%s AND trade_date=ANY(%s)""", (ledger.ns, [date.fromisoformat(day) for day in selections])).fetchall()
        days = {value.isoformat() for (value,) in existing}
        for day in sorted(days):
            result[day] = {sid: ({"LEFT": [], "RIGHT": []}, {"LEFT": 0, "RIGHT": 0}) for sid in selections[day]}
            rows = conn.execute("""WITH ranked AS (
                SELECT s.sector_id,a.code,a.family,
                    count(*) OVER(PARTITION BY s.sector_id,a.family) AS total,
                    row_number() OVER(PARTITION BY s.sector_id,a.family ORDER BY a.setup_score DESC,a.code) AS position
                FROM left_right_assessment_sectors s JOIN left_right_assessments a
                  ON (a.ns,a.trade_date,a.code,a.family)=(s.ns,s.trade_date,s.code,s.family)
                WHERE a.ns=%s AND a.trade_date=%s AND s.sector_id=ANY(%s) AND a.status='READY'
                  AND a.setup_score>=CASE WHEN a.family='LEFT' THEN 75 ELSE 70 END
                ), chosen AS MATERIALIZED (SELECT * FROM ranked WHERE position<=5)
                SELECT r.sector_id,r.family,a.body,r.total FROM chosen r
                JOIN left_right_assessments a ON a.ns=%s AND a.trade_date=%s AND a.code=r.code AND a.family=r.family
                ORDER BY r.sector_id,r.family,r.position""", (ledger.ns, day, selections[day], ledger.ns, day)).fetchall()
            for sid, family, raw, total in rows:
                result[day][sid][0][family].append(_object(raw))
                result[day][sid][1][family] = total
    return result, days


def read_info(ledger, query="", eod_file=None, history_file=None):
    family, day, query, page, page_size, mode, parameters = _query(query)
    seeds = load_history(ledger, eod_file, history_file, include_database=False)
    available = sorted(set(_available_dates(ledger)) | {row["trade_date"] for row in seeds["archives"]}, reverse=True)[:MAX_DAYS]
    day = day or (available[0] if available else None)
    if mode != "ALL":
        from .left_right_views import read_structure
        return read_structure(ledger, day=day, available=available, mode=mode, parameters=parameters,
                              query=query, page=page, page_size=page_size, seeds=seeds,
                              eod_file=eod_file, history_file=history_file)
    seed_items = next((archive["items"] for archive in seeds["archives"] if archive["trade_date"] == day), [])
    indexed = _indexed_info(ledger, family, day, query, page, page_size, seed_items)
    if indexed is not None:
        return dict(indexed, available_dates=available, selected_trade_date=day, family=family,
                    page=page, page_size=page_size, q=query, issues=seeds["issues"])
    history = load_history(ledger, eod_file, history_file, dates=[day] if day else [])
    archive = next((row for row in history["archives"] if row["trade_date"] == day), {})
    items = [item for item in archive.get("items", []) if item["family"] == family]
    scores = [Decimal(row["setup_score"]) for row in items if row["setup_score"] is not None]
    returns = [Decimal(row["return_ratio"]) for row in items if row["return_ratio"] is not None]
    summary = dict(total=len(items), valid_score_count=len(scores), max_score=str(max(scores)) if scores else None,
                   valid_return_count=len(returns), up_count=sum(value > 0 for value in returns),
                   high_return_count=sum(value >= Decimal("0.095") for value in returns))
    versions = sorted({item["version_id"] for item in items if item["version_id"]})
    filtered = [row for row in items if not query or query.casefold() in
                (row["ts_code"] + " " + (row["name"] or "")).casefold()]
    filtered.sort(key=lambda row: (row["setup_score"] is None,
        -Decimal(row["setup_score"]) if row["setup_score"] is not None else Decimal(0), row["ts_code"]))
    total = len(filtered)
    return dict(available_dates=available, selected_trade_date=day, family=family, summary=summary,
        batch=dict(published_at=archive.get("published_at"), version_id=versions[0] if len(versions) == 1 else None,
                   source=archive.get("source")), items=filtered[(page-1)*page_size:page*page_size], total=total,
        page=page, page_size=page_size, pages=(total+page_size-1)//page_size, q=query, issues=history["issues"])
