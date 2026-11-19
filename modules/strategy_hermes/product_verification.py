"""Import reviewed official evidence; activate it only in the maintenance worker.

The operator CLI never fetches URLs or publishes mappings. A review is an explicit
attributed assessment of cited official documents, not inferred from a catalog or a
matching hash. The worker checks those exact bytes at the official origin through
its existing request/window budget before publishing a new immutable version.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import urlsplit

import psycopg

from .ledger import encode, fingerprint
from .simulator import CHINA, CODE, decimal, integer, moment

SCHEMA = 'stock-v4-product-verification-v1'
METHOD = 'REVIEWED_OFFICIAL_EVIDENCE_V1'
OFFICIAL_DOMAINS = ('sse.com.cn', 'szse.cn', 'csindex.com.cn', 'cnindex.com.cn')
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_DOCUMENTS = 4
MAX_REQUESTS = 8
FIELDS = ('etf_code', 'scope', 'index_code', 'trade_unit', 'price_limit_ratio', 'settlement_cycle')
ACTIVE = ('PENDING', 'WAITING_DOCUMENTS', 'VERIFIED')
HEX = re.compile(r'^[0-9a-f]{64}$')
LABEL = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,47}$')


def _decode(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _text(value, maximum=200):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError('INVALID_REVIEW_TEXT')
    return value.strip()


def official_url(value):
    value = _text(value, 2048)
    address = urlsplit(value)
    try:
        port = address.port
    except ValueError:
        raise ValueError('UNAPPROVED_OFFICIAL_URL') from None
    host = (address.hostname or '').lower()
    if (address.scheme != 'https' or address.username is not None or address.password is not None
            or port not in (None, 443) or address.fragment or not address.path
            or not any(host == domain or host.endswith('.' + domain) for domain in OFFICIAL_DOMAINS)):
        raise ValueError('UNAPPROVED_OFFICIAL_URL')
    return value


def validate_dossier(value, now):
    """Validate the review contract without granting product eligibility."""
    if not isinstance(value, dict):
        raise ValueError('INVALID_VERIFICATION_DOSSIER')
    required = set(FIELDS) | {'schema_version', 'expected_mapping_version', 'effective_at',
                            'reviewed_at', 'reviewed_by', 'reason', 'documents', 'claims'}
    if set(value) - (required | {'supersedes'}) or not required <= set(value) or value['schema_version'] != SCHEMA:
        raise ValueError('INVALID_VERIFICATION_CONTRACT')
    result = deepcopy(value)
    if not isinstance(result['etf_code'], str) or not CODE.fullmatch(result['etf_code']):
        raise ValueError('INVALID_ETF_IDENTITY')
    from .stage2.tushare_bootstrap import INDEX_CODE
    if not isinstance(result['index_code'], str) or not INDEX_CODE.fullmatch(result['index_code']):
        raise ValueError('INVALID_INDEX_IDENTITY')
    if result['scope'] != 'domestic_passive_sector' or result['settlement_cycle'] != 'T+1':
        raise ValueError('PRODUCT_OUTSIDE_VERIFICATION_SCOPE')
    result['trade_unit'] = integer(result['trade_unit'])
    ratio = decimal(result['price_limit_ratio'])
    if not 0 < ratio <= 1:
        raise ValueError('INVALID_PRICE_LIMIT_RATIO')
    result['price_limit_ratio'] = str(ratio.normalize())
    from .stage2.inputs import VERSION
    if not isinstance(result['expected_mapping_version'], str) or not VERSION.fullmatch(result['expected_mapping_version']):
        raise ValueError('INVALID_EXPECTED_MAPPING_VERSION')
    result['reviewed_by'] = _text(result['reviewed_by'], 100)
    result['reason'] = _text(result['reason'], 1000)
    reviewed, effective = moment(result['reviewed_at']), moment(result['effective_at'])
    if reviewed > now:
        raise ValueError('FUTURE_REVIEW_TIME')
    result.update(reviewed_at=reviewed.isoformat(), effective_at=effective.isoformat())
    if result.get('supersedes') is not None:
        if not isinstance(result['supersedes'], str) or not re.fullmatch(r'pv-[0-9a-f]{40}', result['supersedes']):
            raise ValueError('INVALID_SUPERSEDED_REQUEST')
    documents = result['documents']
    if not isinstance(documents, dict) or not 1 <= len(documents) <= MAX_DOCUMENTS:
        raise ValueError('OFFICIAL_DOCUMENTS_REQUIRED')
    for label, document in documents.items():
        if not isinstance(label, str) or not LABEL.fullmatch(label) or not isinstance(document, dict):
            raise ValueError('INVALID_OFFICIAL_DOCUMENT')
        if set(document) != {'url', 'sha256', 'acquired_at', 'effective_at'}:
            raise ValueError('INVALID_OFFICIAL_DOCUMENT_FIELDS')
        document['url'] = official_url(document['url'])
        if not isinstance(document['sha256'], str) or not HEX.fullmatch(document['sha256']):
            raise ValueError('INVALID_DOCUMENT_HASH')
        acquired, active = moment(document['acquired_at']), moment(document['effective_at'])
        if acquired > reviewed or active > effective:
            raise ValueError('INVALID_DOCUMENT_CHRONOLOGY')
        document.update(acquired_at=acquired.isoformat(), effective_at=active.isoformat())
    claims = result['claims']
    if not isinstance(claims, dict) or set(claims) != set(FIELDS):
        raise ValueError('FIELD_EVIDENCE_REQUIRED')
    used = set()
    for field, citations in claims.items():
        if not isinstance(citations, list) or not 1 <= len(citations) <= MAX_DOCUMENTS:
            raise ValueError('FIELD_EVIDENCE_REQUIRED:' + field)
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {'document', 'locator', 'excerpt'}:
                raise ValueError('INVALID_FIELD_CITATION')
            if citation['document'] not in documents:
                raise ValueError('UNBOUND_FIELD_CITATION')
            citation['locator'] = _text(citation['locator'], 200)
            citation['excerpt'] = _text(citation['excerpt'], 2000)
            used.add(citation['document'])
    if used != set(documents):
        raise ValueError('UNUSED_OFFICIAL_DOCUMENT')
    return result


def load_bundle(path, now):
    """Read bounded local review files; no temporary files or network calls."""
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('REGULAR_VERIFICATION_BUNDLE_REQUIRED')
    with path.open('rb') as source:
        raw = source.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError('VERIFICATION_BUNDLE_TOO_LARGE')
    from .adapter import unique_object
    value = json.loads(raw, object_pairs_hook=unique_object)
    if (not isinstance(value, dict) or not isinstance(value.get('documents'), dict)
            or not 1 <= len(value['documents']) <= MAX_DOCUMENTS):
        raise ValueError('INVALID_VERIFICATION_BUNDLE')
    # Validate structural evidence and URLs before opening any attachment.
    stripped = deepcopy(value)
    for document in stripped['documents'].values():
        if not isinstance(document, dict) or 'file' not in document:
            raise ValueError('RELATIVE_DOCUMENT_FILE_REQUIRED')
        document.pop('file')
    normalized = validate_dossier(stripped, now)
    documents, total = {}, 0
    for document in value.get('documents', {}).values():
        name = document.pop('file', None)
        if not isinstance(name, str) or not name or Path(name).is_absolute():
            raise ValueError('RELATIVE_DOCUMENT_FILE_REQUIRED')
        source = path.parent / name
        if (source.is_symlink() or not source.resolve(strict=True).is_relative_to(path.parent)
                or not source.is_file()):
            raise ValueError('DOCUMENT_FILE_OUTSIDE_BUNDLE')
        with source.open('rb') as handle:
            content = handle.read(MAX_DOCUMENT_BYTES + 1)
        total += len(content)
        if not content or len(content) > MAX_DOCUMENT_BYTES or total > MAX_BUNDLE_BYTES:
            raise ValueError('DOCUMENT_SIZE_LIMIT')
        digest = sha256(content).hexdigest()
        if digest != document.get('sha256'):
            raise ValueError('LOCAL_DOCUMENT_HASH_MISMATCH')
        documents[digest] = content
    return normalized, documents


def _identity(dossier, mapping):
    if dossier['expected_mapping_version'] != mapping.get('version'):
        raise ValueError('MAPPING_PARENT_CONFLICT')
    product = mapping.get('products', {}).get(dossier['etf_code'])
    if not isinstance(product, dict) or product.get('index_code') != dossier['index_code']:
        raise ValueError('PRODUCT_INDEX_MISMATCH')
    if product.get('list_status') not in (None, 'L'):
        raise ValueError('PRODUCT_NOT_LISTED')


def submit(ledger, dossier, contents, mapping, *, operator, now=None):
    """Durably queue an attributed evidence review. Never activate it here."""
    now = now or datetime.now(CHINA)
    dossier = validate_dossier(dossier, now)
    operator = _text(operator, 100)
    request_id = 'pv-' + fingerprint(dossier)[:40]
    with ledger._connect() as conn, conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (ledger.ns,))
        existing = conn.execute('SELECT dossier FROM product_verification_requests WHERE ns=%s AND request_id=%s',
                                (ledger.ns, request_id)).fetchone()
        if existing:
            if _decode(existing[0]) != dossier:
                raise ValueError('VERIFICATION_ID_CONFLICT')
            return status(ledger, request_id, connection=conn)
        _identity(dossier, mapping)
        latest = conn.execute('SELECT version FROM maintained_mappings WHERE ns=%s ORDER BY available_at DESC,version DESC LIMIT 1',
                              (ledger.ns,)).fetchone()
        if latest and latest[0] != dossier['expected_mapping_version']:
            raise ValueError('MAPPING_PARENT_CONFLICT')
        prior = dossier.get('supersedes')
        if prior:
            row = conn.execute('SELECT etf_code,state FROM product_verification_requests WHERE ns=%s AND request_id=%s FOR UPDATE',
                               (ledger.ns, prior)).fetchone()
            if row is None or row[0] != dossier['etf_code']:
                raise ValueError('INVALID_SUPERSEDED_REQUEST')
            if row[1] != 'PUBLISHED':
                conn.execute("UPDATE product_verification_requests SET state='SUPERSEDED',updated_at=%s WHERE ns=%s AND request_id=%s",
                             (now, ledger.ns, prior))
        active = conn.execute('SELECT request_id FROM product_verification_requests WHERE ns=%s AND etf_code=%s AND state=ANY(%s::text[])',
                              (ledger.ns, dossier['etf_code'], list(ACTIVE))).fetchone()
        if active:
            raise ValueError('ACTIVE_VERIFICATION_REQUEST_EXISTS:' + active[0])
        for digest in {doc['sha256'] for doc in dossier['documents'].values()}:
            existing = conn.execute('SELECT content FROM product_verification_documents WHERE ns=%s AND sha256=%s',
                                    (ledger.ns, digest)).fetchone()
            content = bytes(existing[0]) if existing else contents.get(digest)
            if not isinstance(content, bytes) or not 0 < len(content) <= MAX_DOCUMENT_BYTES or sha256(content).hexdigest() != digest:
                raise ValueError('DURABLE_DOCUMENT_HASH_MISMATCH')
            conn.execute('INSERT INTO product_verification_documents(ns,sha256,content,imported_at) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                         (ledger.ns, digest, content, now))
        conn.execute('''INSERT INTO product_verification_requests
            (ns,request_id,etf_code,expected_mapping_version,state,dossier,imported_by,created_at,updated_at)
            VALUES(%s,%s,%s,%s,'PENDING',%s,%s,%s,%s)''',
            (ledger.ns, request_id, dossier['etf_code'], dossier['expected_mapping_version'], encode(dossier), operator, now, now))
        return status(ledger, request_id, connection=conn)


def status(ledger, request_id, *, connection=None):
    from contextlib import nullcontext
    with (ledger._connect() if connection is None else nullcontext(connection)) as conn:
        row = conn.execute('''SELECT request_id,etf_code,state,dossier,imported_by,created_at,updated_at,
            attempts,result,published_version,published_at FROM product_verification_requests WHERE ns=%s AND request_id=%s''',
            (ledger.ns, request_id)).fetchone()
    if row is None:
        raise ValueError('VERIFICATION_REQUEST_NOT_FOUND')
    keys = ('request_id', 'etf_code', 'state', 'dossier', 'imported_by', 'created_at', 'updated_at',
            'attempts', 'result', 'published_version', 'published_at')
    result = dict(zip(keys, row))
    for key in ('dossier', 'result'):
        result[key] = _decode(result[key])
    for key in ('created_at', 'updated_at', 'published_at'):
        if result[key] is not None:
            result[key] = result[key].isoformat()
    return result


def rebase(ledger, request_id, mapping, *, operator, reason, now=None):
    """Explicitly resubmit unchanged reviewed material against a new parent."""
    previous = status(ledger, request_id)
    if previous['state'] == 'PUBLISHED':
        raise ValueError('PUBLISHED_REVIEW_REQUIRES_NEW_DOSSIER')
    dossier = deepcopy(previous['dossier'])
    dossier.update(expected_mapping_version=mapping['version'], supersedes=request_id,
                   reason=_text(reason, 1000))
    return submit(ledger, dossier, {}, mapping, operator=operator, now=now)


def _record_attempt(ledger, request_id, state, result, now):
    with ledger._connect() as conn:
        return conn.execute('''UPDATE product_verification_requests SET state=%s,result=%s,attempts=attempts+1,
            updated_at=%s WHERE ns=%s AND request_id=%s AND state=ANY(%s::text[]) RETURNING request_id''',
            (state, encode(result), now, ledger.ns, request_id, [*ACTIVE,'NEEDS_REBASE'])).fetchone() is not None


def _verify_documents(ledger, dossier, now, request, checkpoint):
    """All physical calls go through the existing maintenance request callback."""
    from .stage2.mapping_maintenance import _cached, _read_market, _rule_hash
    from .stage2.market_bootstrap import save
    from .stage2.tushare_bootstrap import SourceFailure
    week = f'{now.isocalendar().year}-W{now.isocalendar().week}'
    evidence = {}
    for label, document in dossier['documents'].items():
        checkpoint()
        with ledger._connect() as conn:
            row = conn.execute('SELECT content FROM product_verification_documents WHERE ns=%s AND sha256=%s',
                               (ledger.ns, document['sha256'])).fetchone()
        if row is None or sha256(bytes(row[0])).hexdigest() != document['sha256']:
            raise ValueError('DURABLE_DOCUMENT_HASH_MISMATCH')
        scope = dict(url=document['url'], expected_sha256=document['sha256'], week=week)
        common_scope = dict(url=document['url'], week=week)
        candidates = []
        for endpoint, cache_scope in (('rule_hash', common_scope), ('rule_verification', scope)):
            cached = _cached(ledger, endpoint, cache_scope)
            if cached:
                body, acquired = _read_market(ledger, cached[0])
                candidates.append((acquired, endpoint == 'rule_verification', body, cached[0]))
        newest = max(candidates, key=lambda item: item[:2]) if candidates else None
        if newest and newest[2].get('sha256') == document['sha256']:
            acquired, _, body, batch = newest
            batch = body.get('verification_batch', batch)
        else:
            def operation(document=document, scope=scope):
                digest = _rule_hash(document['url'])
                observed = datetime.now(CHINA)
                # Include this observation's source clock in its immutable body:
                # identical bytes seen again must not reuse an older timestamp.
                return save(ledger, 'maintenance', 'rule_verification', scope,
                            {'sha256': digest, 'checked_at': observed.isoformat()}, 1, None, observed)
            response = request('maintenance:rule_verification', scope, operation)
            checkpoint()
            if response.get('status') != 'SAVED':
                raise SourceFailure.from_receipt(response)
            batch = response['receipt']['batch']
            body, acquired = _read_market(ledger, batch)
        # The ordinary weekly checker reads this shared URL/week cache. Publish
        # the same observed fact (with its original clock), so an older digest
        # cannot immediately invalidate the newly reviewed rule in this task.
        save(ledger, 'maintenance', 'rule_hash', common_scope,
             dict(sha256=body.get('sha256'), checked_at=acquired.isoformat(), verification_batch=batch),
             1, None, acquired)
        if body.get('sha256') != document['sha256']:
            raise ValueError('OFFICIAL_DOCUMENT_CHANGED:' + label)
        evidence[label] = dict(document, source_batch=batch, checked_at=acquired.isoformat())
    return evidence


def _safe_rebase(ledger, request_id, dossier, mapping, now):
    """An unrelated mapping update can advance the parent, never alter reviewed facts."""
    from .product_materials import product_basis
    with ledger._connect() as conn:
        row=conn.execute('SELECT body FROM maintained_mappings WHERE ns=%s AND version=%s',
                         (ledger.ns,dossier['expected_mapping_version'])).fetchone()
    if not row:
        raise ValueError('REBASE_BASELINE_UNAVAILABLE')
    previous=_decode(row[0]).get('products',{}).get(dossier['etf_code'])
    current=mapping.get('products',{}).get(dossier['etf_code'])
    if not previous or not current or product_basis(previous)!=product_basis(current):
        raise ValueError('REBASE_PRODUCT_CHANGED_REVIEW_REQUIRED')
    result=rebase(ledger,request_id,mapping,operator='maintenance-safe-rebase',
        reason='Automatic rebase: product identity, rules and evidence unchanged; unrelated mapping version advanced',now=now)
    with ledger._connect() as conn:
        conn.execute('''UPDATE product_verification_requests SET result=result || %s::jsonb
            WHERE ns=%s AND request_id=%s''',(encode({'rebased_request_id':result['request_id']}),ledger.ns,request_id))
        conn.execute('UPDATE product_verification_targets SET request_id=%s WHERE ns=%s AND request_id=%s',
                     (result['request_id'],ledger.ns,request_id))
        conn.execute('''UPDATE product_verification_requests SET attempts=greatest(attempts,
            (SELECT attempts FROM product_verification_requests WHERE ns=%s AND request_id=%s))
            WHERE ns=%s AND request_id=%s''',(ledger.ns,request_id,ledger.ns,result['request_id']))
    return result['request_id'],result['dossier']


def apply_queued(ledger, original, updated, summary, now, request, checkpoint, *, clock=None, request_ids=None):
    """Stage checked product rules; publish_mapping atomically activates them."""
    from .left_right_backfill import Interrupted
    from .maintenance_policy import allowed_window
    from .stage2.provider_errors import ProviderFailure, error_diagnostic
    clock = clock or (lambda: datetime.now(CHINA))
    if request_ids is not None and (not isinstance(request_ids,(list,tuple)) or not 1<=len(request_ids)<=MAX_REQUESTS
            or any(not isinstance(rid,str) or not re.fullmatch(r'pv-[0-9a-f]{40}',rid) for rid in request_ids)
            or len(set(request_ids))!=len(request_ids)):
        raise ValueError('INVALID_VERIFICATION_REQUEST_IDS')
    if summary.get('mode')=='verify' and request_ids is None:
        raise ValueError('VERIFY_REQUIRES_EXPLICIT_REQUEST_IDS')
    if summary.get('mode')!='verify' and not allowed_window(summary.get('mode'), now):
        return updated, summary
    from .stage2.mapping_maintenance import _finish
    with ledger._connect() as lock:
        acquired, = lock.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',
                                 ('product-verification:' + ledger.ns,)).fetchone()
        if not acquired:
            summary['product_verification'] = {'queue': {'status': 'PENDING', 'reason': 'VERIFICATION_BUSY'}}
            return _finish(original, updated, summary)
        rows = lock.execute('''SELECT request_id,dossier FROM product_verification_requests
            WHERE ns=%s AND state=ANY(%s::text[]) AND created_at<=%s
            AND (%s::text[] IS NULL OR request_id=ANY(%s::text[]) OR dossier->>'supersedes'=ANY(%s::text[]))
            AND (%s::text[] IS NOT NULL OR (
                (dossier->>'effective_at')::timestamptz <= %s AND NOT EXISTS (
                    SELECT 1 FROM jsonb_each(dossier->'documents') document
                    WHERE NOT EXISTS (
                        SELECT 1 FROM product_verification_documents stored
                        WHERE stored.ns=product_verification_requests.ns
                          AND stored.sha256=document.value->>'sha256'
                    )
                )
            ))
            ORDER BY created_at,request_id LIMIT %s''',
            (ledger.ns, [*ACTIVE,'NEEDS_REBASE'], now,request_ids,request_ids,request_ids,
             request_ids,now,MAX_REQUESTS)).fetchall()
        for request_id, raw in rows:
            dossier = _decode(raw)
            code = dossier['etf_code']
            results = summary.setdefault('product_verification', {})
            try:
                checkpoint()
                dossier = validate_dossier(dossier, now)
                if dossier['expected_mapping_version']!=original['version']:
                    request_id,dossier=_safe_rebase(ledger,request_id,dossier,original,clock())
                _identity(dossier, original)
                _identity({**dossier, 'expected_mapping_version': updated['version']}, updated)
                if moment(dossier['effective_at']) > now:
                    results[code] = dict(status='PENDING', request_id=request_id, reason='RULE_NOT_EFFECTIVE')
                    continue
                evidence = _verify_documents(ledger, dossier, now, request, checkpoint)
                checked_at = clock()
                if checked_at < now or any(moment(doc['checked_at']) > checked_at for doc in evidence.values()):
                    raise ValueError('FUTURE_DOCUMENT_RECEIPT')
                checkpoint()
                rule = dict(verified=True, scope=dossier['scope'], index_code=dossier['index_code'],
                            price_limit_ratio=dossier['price_limit_ratio'], settlement_cycle=dossier['settlement_cycle'],
                            verified_at=checked_at.isoformat(), effective_at=dossier['effective_at'],
                            verification_id=request_id, verification_method=METHOD,
                            reviewed_by=dossier['reviewed_by'], reviewed_at=dossier['reviewed_at'],
                            review_reason=dossier['reason'], review_required=False,
                            evidence=evidence, claims=dossier['claims'])
                prior = updated['products'][code].get('rule') or {}
                if prior.get('verification_id') and prior['verification_id'] != request_id:
                    rule['previous_verification_id'] = prior['verification_id']
                result = dict(rule=rule, trade_unit=dossier['trade_unit'], checked_at=checked_at.isoformat())
                if not _record_attempt(ledger, request_id, 'VERIFIED', result, checked_at):
                    results[code] = dict(status='PENDING', request_id=request_id, reason='REQUEST_SUPERSEDED')
                    continue
                updated['products'][code].update(rule=rule, trade_unit=dossier['trade_unit'])
                results[code] = dict(status='UPDATED', request_id=request_id, verified_at=checked_at.isoformat())
            except Interrupted as exc:
                # Preserve completed items on request/time budget exhaustion;
                # execute_job checks cancellation and the lease before publish.
                summary['interrupted'] = str(exc)
                break
            except (ProviderFailure, OSError) as exc:
                diagnostic = error_diagnostic(exc)
                _record_attempt(ledger, request_id, 'WAITING_DOCUMENTS', diagnostic, clock())
                results[code] = dict(status='PENDING', request_id=request_id, **diagnostic)
            except (ValueError, TypeError, KeyError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                state = 'NEEDS_REBASE' if reason == 'MAPPING_PARENT_CONFLICT' else 'REVIEW_REQUIRED'
                _record_attempt(ledger, request_id, state, {'reason': reason}, clock())
                results[code] = dict(status='REVIEW_REQUIRED', request_id=request_id, reason=reason)
    return _finish(original, updated, summary)


def queue_snapshot(ledger, request_ids=None):
    """Persisted material/approval states remain visible after the original job ends."""
    with ledger._connect() as conn:
        counts=dict(conn.execute('''SELECT state,count(*) FROM product_verification_requests r WHERE ns=%s
            AND (%s::text[] IS NULL OR request_id=ANY(%s::text[]) OR dossier->>'supersedes'=ANY(%s::text[])) GROUP BY state''',
            (ledger.ns,request_ids,request_ids,request_ids)).fetchall())
        if request_ids is None:
            for state,count in conn.execute("SELECT state,count(*) FROM product_verification_targets WHERE ns=%s AND (request_id IS NULL OR state IN ('NEEDS_MATERIALS','AWAITING_REVIEW')) GROUP BY state",(ledger.ns,)):
                counts[state]=counts.get(state,0)+count
        rows=conn.execute('''SELECT r.etf_code,r.request_id,r.state,r.attempts,r.result,r.published_version,
            (SELECT t.target_id FROM product_verification_targets t WHERE t.ns=r.ns AND t.request_id=r.request_id LIMIT 1)
            FROM product_verification_requests r WHERE r.ns=%s
            AND (%s::text[] IS NULL OR r.request_id=ANY(%s::text[]) OR r.dossier->>'supersedes'=ANY(%s::text[]))
            ORDER BY CASE WHEN r.state IN ('REVIEW_REQUIRED','NEEDS_REBASE') THEN 0
                WHEN r.state IN ('PENDING','WAITING_DOCUMENTS','VERIFIED') THEN 1 ELSE 2 END,
                r.updated_at DESC,r.request_id LIMIT 32''',(ledger.ns,request_ids,request_ids,request_ids)).fetchall()
        targets=conn.execute('''SELECT etf_code,target_id,state,reason FROM product_verification_targets
            WHERE ns=%s AND (request_id IS NULL OR state IN ('NEEDS_MATERIALS','AWAITING_REVIEW'))
            ORDER BY updated_at DESC,target_id LIMIT 32''',(ledger.ns,)).fetchall() if request_ids is None else []
    items=[]
    for code,rid,state,attempts,result,version,target in rows:
        detail=_decode(result)
        items.append(dict(etf_code=code,request_id=rid,target_id=target,state=state,
                          reason=detail.get('reason') or detail.get('error_kind'),attempts=attempts,published_version=version))
    for code,target,state,reason in targets:
        items.append(dict(etf_code=code,target_id=target,request_id=None,state=state,reason=reason,attempts=0,published_version=None))
    priority={'AWAITING_REVIEW':0,'NEEDS_MATERIALS':1,'REVIEW_REQUIRED':2,'NEEDS_REBASE':2,
              'WAITING_DOCUMENTS':3,'PENDING':3,'VERIFIED':4,'PUBLISHED':5,'SUPERSEDED':6}
    items.sort(key=lambda row:(priority.get(row['state'],4),row['etf_code']))
    return dict(counts=counts,items=items[:32],omitted=max(0,sum(counts.values())-min(32,len(items))))


def prepare_materials(*args,**kwargs):
    from .product_materials import prepare_materials as run
    return run(*args,**kwargs)


def review_context(ledger,target_id):
    from .product_materials import review_context as read
    return read(ledger,target_id)


def submit_review(ledger,target_id,review,now=None):
    """Bound the model's semantic assessment to durable official text and server identity."""
    from .product_materials import get_target,product_basis,_save_target
    now=now or datetime.now(CHINA)
    target=get_target(ledger,target_id)
    if not isinstance(review,dict) or len(encode(review))>128000 or review.get('decision') not in ('APPROVE','NEEDS_MATERIALS'):
        raise ValueError('INVALID_AI_REVIEW')
    if target['request_id']:
        current=status(ledger,target['request_id'])
        if current['state'] in (*ACTIVE,'PUBLISHED'):
            return dict(target_id=target_id,state=current['state'],request_id=current['request_id'])
    if target['state'] not in ('AWAITING_REVIEW','REVIEW_REQUIRED'):
        raise ValueError('OFFICIAL_MATERIAL_NOT_READY')
    if review['decision']=='NEEDS_MATERIALS':
        if set(review)!={'decision','reason'}: raise ValueError('INVALID_AI_REVIEW_FIELDS')
        _save_target(ledger,target_id,state='REVIEW_REQUIRED',reason=_text(review['reason'],1000))
        return dict(target_id=target_id,state='REVIEW_REQUIRED',request_id=None)
    allowed={'decision','scope','trade_unit','price_limit_ratio','settlement_cycle','effective_at','documents','claims','reason'}
    if set(review)!=allowed: raise ValueError('INVALID_AI_REVIEW_FIELDS')
    with ledger._connect() as conn:
        row=conn.execute('SELECT body FROM maintained_mappings WHERE ns=%s AND available_at<=%s ORDER BY available_at DESC,version DESC LIMIT 1',
                         (ledger.ns,now)).fetchone()
    if not row: raise ValueError('PUBLISHED_MAPPING_REQUIRED')
    mapping=_decode(row[0]); product=mapping['products'].get(target['etf_code'])
    if not product or product_basis(product)!=target['product_basis']:
        _save_target(ledger,target_id,state='REVIEW_REQUIRED',reason='REVIEW_PRODUCT_CHANGED')
        raise ValueError('REVIEW_PRODUCT_CHANGED')
    docs=review['documents']
    if not isinstance(docs,dict) or not 1<=len(docs)<=MAX_DOCUMENTS:
        raise ValueError('OFFICIAL_DOCUMENTS_REQUIRED')
    documents={}
    for label,fields in docs.items():
        if label not in target['documents'] or not isinstance(fields,dict) or set(fields)!={'effective_at'}:
            raise ValueError('UNKNOWN_OFFICIAL_REVIEW_DOCUMENT')
        evidence=target['documents'][label]
        documents[label]={key:evidence[key] for key in ('url','sha256','acquired_at')}
        documents[label]['effective_at']=fields['effective_at']
    claims=review['claims']
    if not isinstance(claims,dict) or set(claims)!=set(FIELDS): raise ValueError('FIELD_EVIDENCE_REQUIRED')
    normalize=lambda text:re.sub(r'\s+','',text)
    for field,citations in claims.items():
        if not isinstance(citations,list) or not 1<=len(citations)<=MAX_DOCUMENTS: raise ValueError('FIELD_EVIDENCE_REQUIRED')
        for citation in citations:
            if not isinstance(citation,dict) or set(citation)!={'document','locator','excerpt'}:
                raise ValueError('INVALID_FIELD_CITATION')
            label=citation['document']
            if label not in documents: raise ValueError('UNBOUND_FIELD_CITATION')
            segments={s['locator']:s['text'] for s in target['documents'][label]['segments']}
            excerpt=_text(citation['excerpt'],2000)
            if citation['locator'] not in segments or normalize(excerpt) not in normalize(segments[citation['locator']]):
                raise ValueError('CITATION_NOT_IN_OFFICIAL_TEXT')
    validate_review_claim_values(review,target['etf_code'],product['index_code'])
    dossier=dict(schema_version=SCHEMA,etf_code=target['etf_code'],index_code=product['index_code'],
        expected_mapping_version=mapping['version'],documents=documents,claims=claims,
        reviewed_at=now.isoformat(),reviewed_by='OCTOP AI-assisted official evidence review',
        **{key:review[key] for key in ('scope','trade_unit','price_limit_ratio','settlement_cycle','effective_at','reason')})
    if target['request_id']: dossier['supersedes']=target['request_id']
    receipt=submit(ledger,dossier,{},mapping,operator='octop-material-review',now=now)
    _save_target(ledger,target_id,state='QUEUED',request_id=receipt['request_id'])
    return dict(target_id=target_id,state=receipt['state'],request_id=receipt['request_id'])


def validate_review_claim_values(review,etf_code,index_code):
    """Bind explicit scalar claims to their own citations; applicability remains AI-reviewed.

    Seeing a percentage elsewhere in a document is not its price-limit citation.
    This checks the field-specific excerpts, not the whole source or catalog name.
    """
    def cited(field):
        text=' '.join(row['excerpt'] for row in review['claims'][field])
        return re.sub(r'\s+','',unicodedata.normalize('NFKC',text)).upper()
    for field,code in (('etf_code',etf_code),('index_code',index_code)):
        if not re.search(r'(?<![0-9A-Z])'+re.escape(code.split('.')[0])+r'(?![0-9A-Z])',cited(field)):
            raise ValueError('CITATION_VALUE_MISMATCH:'+field)
    unit=integer(review['trade_unit'])
    if not re.search(r'(?<!\d)'+str(unit)+r'(?:份|基金份额)',cited('trade_unit')):
        raise ValueError('CITATION_VALUE_MISMATCH:trade_unit')
    percent=format((decimal(review['price_limit_ratio'])*100).normalize(),'f')
    if not re.search(r'(?<![\d.])'+re.escape(percent)+r'(?:\.0+)?%',cited('price_limit_ratio')):
        raise ValueError('CITATION_VALUE_MISMATCH:price_limit_ratio')
    if review['settlement_cycle']!='T+1' or 'T+1' not in cited('settlement_cycle'):
        raise ValueError('CITATION_VALUE_MISMATCH:settlement_cycle')


def complete_publications(ledger, conn, mapping, at):
    """Called only inside mapping publication's namespace + mapping locks."""
    previous = None
    for code, product in mapping['products'].items():
        rule = product.get('rule') or {}
        request_id = rule.get('verification_id')
        if not request_id:
            continue
        row = conn.execute('SELECT etf_code,state,result FROM product_verification_requests WHERE ns=%s AND request_id=%s FOR UPDATE',
                           (ledger.ns, request_id)).fetchone()
        if row is None or row[0] != code:
            raise ValueError('UNREGISTERED_PRODUCT_VERIFICATION')
        result = _decode(row[2])
        if row[1] == 'PUBLISHED':
            mutable = {'review_required', 'review_reason'}
            stable = lambda value: {key: val for key, val in value.items() if key not in mutable}
            if stable(rule) != stable(result.get('rule', {})) or result.get('trade_unit') != product.get('trade_unit'):
                raise ValueError('PUBLISHED_VERIFICATION_MUTATED')
            if previous is None:
                before = conn.execute('''SELECT body FROM maintained_mappings WHERE ns=%s AND version<>%s
                    ORDER BY available_at DESC,version DESC LIMIT 1''', (ledger.ns, mapping['version'])).fetchone()
                previous = _decode(before[0]) if before else {}
            old_rule = (previous.get('products', {}).get(code, {}).get('rule') or {})
            if (old_rule and old_rule.get('verification_id') != request_id
                    or old_rule.get('review_required') is True and rule.get('review_required') is not True):
                raise ValueError('RULE_REACTIVATION_REQUIRES_NEW_VERIFICATION')
            continue
        if row[1] != 'VERIFIED' or result.get('rule') != rule or result.get('trade_unit') != product.get('trade_unit'):
            raise ValueError('UNVERIFIED_PRODUCT_PUBLICATION')
        if moment(rule['verified_at']) > at or moment(rule['effective_at']) > at:
            raise ValueError('FUTURE_PRODUCT_PUBLICATION')
        conn.execute("UPDATE product_verification_requests SET state='PUBLISHED',published_version=%s,published_at=%s,updated_at=%s WHERE ns=%s AND request_id=%s",
                     (mapping['version'], at, at, ledger.ns, request_id))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, help='private environment file; otherwise use process environment')
    parser.add_argument('--input-kind', choices=('LIVE', 'FROZEN'), default='LIVE')
    commands = parser.add_subparsers(dest='command', required=True)
    importer = commands.add_parser('import', help='import local evidence only; wait for normal OCTOP maintenance')
    importer.add_argument('bundle', type=Path)
    importer.add_argument('--mapping', type=Path, required=True, help='fallback mapping; latest saved mapping has priority')
    importer.add_argument('--operator', required=True)
    inspect = commands.add_parser('status')
    inspect.add_argument('request_id')
    retry = commands.add_parser('rebase', help='resubmit unchanged material after an unrelated mapping update')
    retry.add_argument('request_id')
    retry.add_argument('--mapping', type=Path, required=True)
    retry.add_argument('--operator', required=True)
    retry.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    try:
        from .stage2.settings import Settings
        from .stage2.postgres import PostgresLedger
        from .stage2.maintenance_store import live_mapping
        settings = Settings.read(args.env_file) if args.env_file else Settings.from_values(os.environ)
        ledger = PostgresLedger(settings, args.input_kind)
        if args.command == 'status':
            result = status(ledger, args.request_id)
        else:
            now = datetime.now(CHINA)
            mapping = live_mapping(ledger, args.mapping, now)
            if args.command == 'import':
                dossier, contents = load_bundle(args.bundle, now)
                result = submit(ledger, dossier, contents, mapping, operator=args.operator, now=now)
            else:
                result = rebase(ledger, args.request_id, mapping, operator=args.operator, reason=args.reason, now=now)
        print(encode(result))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print('product-verification: ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print('product-verification: storage failure (' + type(exc).__name__ + ')', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
