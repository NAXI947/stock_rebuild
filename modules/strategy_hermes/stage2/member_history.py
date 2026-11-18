"""Bounded, source-dated board membership recovery from saved 046 pages."""
from datetime import date, datetime
import json
import re

from ..simulator import CHINA, moment

MAX_HISTORY_DATES = 8
MAX_MEMBER_PAGES = 8
MEMBER_CODE = re.compile(r'[0-9]{6}\.(?:SH|SZ|BJ)\Z')


def decode(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def source_time(value):
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError('source availability requires a timezone')
        return value.astimezone(CHINA)
    return moment(value)


def usable_members(value, now, day=None):
    """An old reference is retained only when consumers can actually use it."""
    try:
        members = value['members']
        return (isinstance(members, list) and bool(members)
                and all(isinstance(code, str) and MEMBER_CODE.fullmatch(code) for code in members)
                and len(set(members)) == len(members)
                and type(value['reported_count']) is int and value['reported_count'] == len(members)
                and type(value['unresolved_count']) is int and value['unresolved_count'] == 0
                and isinstance(value['source_batches'], list) and bool(value['source_batches'])
                and all(isinstance(ref, str) and ref for ref in value['source_batches'])
                and date.fromisoformat(value['as_of_date']) <= (day or now.date())
                and (not value.get('available_at') or moment(value['available_at']) <= now))
    except (KeyError, TypeError, ValueError):
        return False


def saved_pages(conn, sid, day, now):
    """Latest visible revision of each offset; the ninth page detects overflow."""
    return conn.execute("""SELECT DISTINCT ON (scope->>'Index')
        batch_id,scope,body,collected_at FROM supplier_market_batches
        WHERE provider='kaipanla' AND endpoint='046' AND scope->>'Date'=%s
        AND scope->>'PlateID'=%s AND collected_at<=%s
        ORDER BY scope->>'Index',collected_at DESC,batch_id DESC LIMIT %s""",
        (day, sid.split(':')[1], now, MAX_MEMBER_PAGES + 1)).fetchall()


def assemble_members(pages, day, identities, identity_batch, identity_at, name):
    """Keep the reported denominator even when pages or identities are partial."""
    if not identity_batch or identity_at is None or not pages or len(pages) > MAX_MEMBER_PAGES:
        raise ValueError('membership source basis missing or over budget')
    ordered = sorted((int(decode(scope)['Index']), batch, decode(body), at)
                     for batch, scope, body, at in pages)
    offset, total, seen, batches, observed = 0, None, set(), [], []
    for start, batch, body, at in ordered:
        received = source_time(at)
        if observed and received < observed[-1]:
            raise ValueError('membership page timestamps move backwards across offsets')
        rows = body['rows']
        codes = {str(row[0]) for row in rows}
        expected = body['reported_total']
        if (type(expected) is not int or expected <= 0 or start != offset or not codes
                or body['trade_days'] != [day] or seen.intersection(codes)
                or len(codes) != len(rows) or (total is not None and expected != total)):
            raise ValueError('membership pages disagree on identity, date or population')
        total = expected
        offset += len(codes)
        if offset > total:
            raise ValueError('membership pages exceed reported population')
        seen.update(codes); batches.append(batch); observed.append(received)
    resolved = sorted(identities[code][0] for code in seen if len(identities.get(code, ())) == 1)
    if len(set(resolved)) != len(resolved) or any(not MEMBER_CODE.fullmatch(code) for code in resolved):
        raise ValueError('member identities are not unique qualified stock codes')
    return dict(members=resolved, as_of_date=day, reported_count=total,
                unresolved_count=total-len(resolved), source_batches=batches,
                identity_source_batch=identity_batch,
                available_at=max([source_time(identity_at), *observed]).astimezone(CHINA).isoformat(),
                sector_name=name)


def cached_members(ledger, sid, day, now, identities, identity_batch, identity_at, name, checkpoint):
    """Find the latest complete older reference without any external request."""
    if not identities or not identity_batch or identity_at is None:
        return None
    with ledger._connect() as conn:
        dates = conn.execute("""SELECT DISTINCT scope->>'Date' AS day FROM supplier_market_batches
            WHERE provider='kaipanla' AND endpoint='046' AND scope->>'PlateID'=%s
            AND scope->>'Date'<%s AND collected_at<=%s ORDER BY day DESC LIMIT %s""",
            (sid.split(':')[1], day.isoformat(), now, MAX_HISTORY_DATES)).fetchall()
        for (source_day,) in dates:
            checkpoint()
            try:
                if date.fromisoformat(source_day) >= day:
                    continue
                candidate = assemble_members(saved_pages(conn, sid, source_day, now), source_day,
                                             identities, identity_batch, identity_at, name)
                if usable_members(candidate, now, day):
                    return candidate
            except (KeyError, TypeError, ValueError, IndexError):
                continue
    return None
