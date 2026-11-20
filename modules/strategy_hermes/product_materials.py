"""Bounded official-source discovery and durable text for attributed AI review.

Collection produces evidence, not rules. All physical HTTP is inside the caller's
request callback. Tool reads and submitted reviews never fetch external content.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode, urljoin
from urllib.request import Request, build_opener
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .ledger import encode, fingerprint
from .simulator import CHINA, CODE, moment

MAX_TARGETS = 8
MAX_PAGES = 80
MAX_TEXT = 240000
MAX_CONTEXT_DOCUMENT = 8000
TRADE_GUIDE = 'https://edu.sse.com.cn/service/hotline/qa/c/4956495.shtml'
PRICE_RULE = 'https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/10817739/files/7ffa42ba08bf4605a9e680bd38d452b4.docx'
SZSE_TRADE_GUIDE = 'https://investor.szse.cn/warning/activities/fundIntroduction/t20210104_584079.html'
SZSE_PRICE_RULE = 'https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf'


def decode(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def product_basis(product):
    """Exclude unrelated statistics/availability, retain every approval identity/rule."""
    return {key: product.get(key) for key in ('name','index_code','list_status','list_date',
            'source_classification','trade_unit','rule')}


def material_seed_config(mapping):
    configured = mapping.get("etf_material_seeds")
    try:
        if configured is None:
            if os.environ.get("ETF_REPRESENTATIVENESS_MODE", "V2") not in ("V3_SHADOW", "V3_ENFORCE"):
                return None, set()
            configured = json.loads(Path(__file__).with_name("etf_material_seeds.json").read_text(encoding="utf-8"))
        version, source, rows = (configured[key] for key in ("seed_config_version", "source_ref", "targets"))
        if (not isinstance(version, str) or not version or not isinstance(source, str) or not source
                or not isinstance(rows, list) or len(rows) > 43):
            return None, set()
        values = {row["etf"]: row for row in rows}
        if len(values) != len(rows) or any(not CODE.fullmatch(code) or type(row["materials_enabled"]) is not bool
                or not isinstance(row["reason"], str) or not row["reason"] for code, row in values.items()):
            return None, set()
        return version, {code for code, row in values.items() if row["materials_enabled"]}
    except (OSError, TypeError, KeyError, ValueError):
        return None, set()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.hidden=0
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.hidden += 1
    def handle_endtag(self, tag):
        if tag in ('script','style'): self.hidden=max(0,self.hidden-1)
        if tag in ('p','div','li','br'): self.parts.append('\n')
    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)


def extract_segments(content):
    """Only bounded in-memory PDF/DOCX/HTML text; never create temp files."""
    if content.startswith(b'PK\x03\x04'):
        try:
            with ZipFile(BytesIO(content)) as archive:
                if len(archive.infolist())>256:raise ValueError('OFFICIAL_DOCX_PART_LIMIT')
                entry=archive.getinfo('word/document.xml')
                if entry.file_size>4*1024*1024:raise ValueError('OFFICIAL_DOCX_XML_TOO_LARGE')
                with archive.open(entry) as stream:raw=stream.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:raise ValueError('OFFICIAL_DOCX_XML_TOO_LARGE')
            if b'\x00' in raw or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('OFFICIAL_DOCX_XML_UNSAFE')
            tree=ElementTree.fromstring(raw);namespace={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            rows=[];used=0
            for index,paragraph in enumerate(tree.findall('.//w:p',namespace)):
                value=''.join(node.text or '' for node in paragraph.findall('.//w:t',namespace))[:20000]
                value=value[:MAX_TEXT-used];used+=len(value)
                if value.strip():rows.append(dict(locator='paragraph:'+str(index+1),text=value))
                if used>=MAX_TEXT:break
        except (BadZipFile,KeyError,ElementTree.ParseError) as exc:
            raise ValueError('OFFICIAL_DOCX_UNREADABLE') from exc
    elif content.startswith(b'%PDF'):
        from pypdf import PdfReader, apply_configuration
        from pypdf.errors import PyPdfError
        try:
            with apply_configuration(zlib_maximum_output_length=4*1024*1024,
                                     array_based_stream_maximum_output_length=4*1024*1024):
                reader=PdfReader(BytesIO(content),strict=False)
                if reader.is_encrypted: raise ValueError('ENCRYPTED_OFFICIAL_DOCUMENT')
                rows=[]; used=0
                for index,page in enumerate(reader.pages[:MAX_PAGES]):
                    stream=page.get_contents()
                    if stream is not None and len(stream.get_data())>4*1024*1024:
                        raise ValueError('DOCUMENT_PAGE_TOO_LARGE')
                    # Keep the original bytes for digest verification. The text
                    # projection must be safe to store in PostgreSQL JSONB.
                    value=(page.extract_text() or '').replace('\x00', ' ')[:20000]
                    value=value[:MAX_TEXT-used]; used+=len(value)
                    if value.strip(): rows.append(dict(locator='page:'+str(index+1),text=value))
                    if used>=MAX_TEXT: break
                reader.close()
        except PyPdfError as exc:
            raise ValueError('OFFICIAL_PDF_UNREADABLE') from exc
    else:
        parser=_Text(); parser.feed(content.decode('utf-8',errors='replace'))
        text=''.join(parser.parts).replace('\x00', ' ')[:MAX_TEXT]
        rows=[dict(locator='paragraph:'+str(i+1),text=line[:20000])
              for i,line in enumerate(text.splitlines()) if line.strip()]
    if not rows: raise ValueError('OFFICIAL_TEXT_UNAVAILABLE')
    return rows


def exchange_rule_sources(code):
    # Exchange-level trading rules are shared by every product listed there.
    if code.endswith('.SH'):return (('trade_guide',TRADE_GUIDE),('price_rule',PRICE_RULE))
    if code.endswith('.SZ'):return (('trade_guide',SZSE_TRADE_GUIDE),('price_rule',SZSE_PRICE_RULE))
    return ()


def source_seeds(code):
    # URL seeds are discovery hints only. Their bytes and semantics are reviewed afresh.
    seeds=json.loads(Path(__file__).with_name('official_product_sources.json').read_text(encoding='utf-8'))
    return dict(seeds.get(code, {}))


def source_upgrades(ledger,mapping,codes):
    """Only an existing unapproved target's configured static URL replacement.

    Prospectus discovery may legitimately supersede a seed; it is never a
    trigger. Published products and an active review request are left intact.
    """
    from .stage2.penetration import product_entry_reason
    if not isinstance(codes,list) or len(codes)>MAX_TARGETS or len(set(codes))!=len(codes):
        raise ValueError('INVALID_MATERIAL_UPGRADE_SCOPE')
    changes=[]
    with ledger._connect() as conn:
        for code in sorted(codes):
            product=mapping['products'].get(code)
            if not exchange_rule_sources(code) or not product or product_entry_reason(product,datetime.now(CHINA)) is None:continue
            basis=product_basis(product);target_id='pt-'+fingerprint([code,basis])[:40]
            row=conn.execute('''SELECT state,documents,product_basis,request_id FROM product_verification_targets
                WHERE ns=%s AND target_id=%s''',(ledger.ns,target_id)).fetchone()
            if not row or row[0] not in ('AWAITING_REVIEW','REVIEW_REQUIRED','NEEDS_MATERIALS') or decode(row[2])!=basis:continue
            active=conn.execute('''SELECT 1 FROM product_verification_requests WHERE ns=%s AND etf_code=%s
                AND state IN ('PENDING','WAITING_DOCUMENTS','VERIFIED','PUBLISHED') LIMIT 1''',(ledger.ns,code)).fetchone()
            if active:continue
            documents=decode(row[1])
            for label,url in exchange_rule_sources(code):
                previous=documents.get(label)
                if previous and previous.get('url')!=url:
                    changes.append(dict(etf_code=code,target_id=target_id,label=label,previous_url=previous['url'],url=url))
    return sorted(changes,key=lambda row:(row['etf_code'],row['target_id'],row['label'],row['url']))


def _commit_source_upgrade(ledger,change,prior,*,document=None,error=None):
    """Do not replace evidence after a review submitted while HTTP was in flight."""
    with ledger._connect() as conn,conn.transaction():
        # submit() takes this same lock before creating an active application.
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(ledger.ns,))
        row=conn.execute('''SELECT state,request_id,product_basis,documents FROM product_verification_targets
            WHERE ns=%s AND target_id=%s FOR UPDATE''',(ledger.ns,change['target_id'])).fetchone()
        if (not row or row[0] not in ('AWAITING_REVIEW','REVIEW_REQUIRED','NEEDS_MATERIALS')
                or row[0]!=prior['state'] or row[1]!=prior['request_id']
                or decode(row[2])!=prior['product_basis']):return False
        documents=decode(row[3]);old=documents.get(change['label']);expected=prior['documents'].get(change['label'])
        if (not old or not expected or old.get('url')!=change['previous_url']
                or (old.get('url'),old.get('sha256'))!=(expected.get('url'),expected.get('sha256'))):return False
        active=conn.execute('''SELECT 1 FROM product_verification_requests WHERE ns=%s AND etf_code=%s
            AND state IN ('PENDING','WAITING_DOCUMENTS','VERIFIED','PUBLISHED') LIMIT 1''',
            (ledger.ns,change['etf_code'])).fetchone()
        if active:return False
        if document is not None:documents[change['label']]=document
        conn.execute('''UPDATE product_verification_targets SET state=%s,reason=%s,documents=%s::jsonb,
            updated_at=clock_timestamp() WHERE ns=%s AND target_id=%s''',
            ('AWAITING_REVIEW' if document is not None else 'NEEDS_MATERIALS',
             'OFFICIAL_SOURCE_UPDATED_REVIEW_REQUIRED' if document is not None else str(error)[:300],
             encode(documents),ledger.ns,change['target_id']))
        return True


def _apply_source_upgrades(ledger,mapping,now,request,checkpoint,changes):
    """Fill only approved source replacements within the original job budget."""
    from .left_right_backfill import Interrupted
    from .product_verification import queue_snapshot
    from .stage2.provider_errors import ProviderFailure
    if not isinstance(changes,list) or not 1<=len(changes)<=MAX_TARGETS*2:
        raise ValueError('INVALID_MATERIAL_UPGRADE_SCOPE')
    fields={'etf_code','target_id','label','previous_url','url'}
    if any(not isinstance(row,dict) or set(row)!=fields for row in changes):
        raise ValueError('INVALID_MATERIAL_UPGRADE_SCOPE')
    codes=sorted({row['etf_code'] for row in changes})
    approved={fingerprint(row) for row in source_upgrades(ledger,mapping,codes)}
    interrupted=None
    for change in changes:
        # A concurrent semantic approval or already-finished replacement must
        # not be overwritten or fetched again on worker resume.
        if fingerprint(change) not in approved:continue
        target=get_target(ledger,change['target_id'])
        try:
            content,at,batch=_fetch(ledger,change['url'],request,checkpoint,now,kind='official_material')
            document=dict(url=change['url'],sha256=sha256(content).hexdigest(),acquired_at=at.isoformat(),
                          source_batch=batch,segments=extract_segments(content))
            _commit_source_upgrade(ledger,change,target,document=document)
        except Interrupted as exc:
            interrupted=str(exc);break
        except (ProviderFailure,OSError,ValueError,TypeError,KeyError) as exc:
            _commit_source_upgrade(ledger,change,target,error=exc)
    result=queue_snapshot(ledger)
    if interrupted:result['interrupted']=interrupted
    return result


def sse_discovery_url(code):
    return 'https://query.sse.com.cn/commonQuery.do?'+urlencode({
        'isPagination':'true','pageHelp.pageSize':10,'pageHelp.pageNo':1,'pageHelp.beginPage':1,
        'pageHelp.cacheSize':1,'pageHelp.endPage':1,'type':'inParams',
        'sqlId':'COMMON_PL_JJXX_JJGG_NEW_L','TITLE':'招募说明书','SECURITY_CODE':code[:6],
        'BULLETIN_TYPE':'','START_DATE':'','END_DATE':'','DATE_DESC':1,'DATE_ASC':'','CODE_DESC':'','CODE_ASC':''})


def parse_sse_discovery(content, code, now):
    from .product_verification import official_url
    body=json.loads(content)
    rows=body.get('result') or (body.get('pageHelp') or {}).get('data') or []
    if not isinstance(rows,list) or len(rows)>10: raise ValueError('INVALID_DISCLOSURE_PAGE')
    choices=[]
    for row in rows:
        if (row.get('SECURITY_CODE')!=code[:6] or '招募说明书' not in row.get('TITLE','')
                or not isinstance(row.get('SSEDATE'),str) or row['SSEDATE']>now.date().isoformat()): continue
        url=official_url(urljoin('https://www.sse.com.cn',row.get('URL','')))
        if not url.lower().endswith('.pdf'): continue
        choices.append((row['SSEDATE'],url))
    return max(choices)[1] if choices else None


def szse_discovery_body(code,now,*,title='招募说明书'):
    return dict(type=2,pageSize=10,pageNum=1,stock=[code[:6]],channelCode=['fundinfoNotice_disc'],
                searchKey=[title],seDate=[(now-timedelta(days=1826)).date().isoformat(),now.date().isoformat()])


def parse_szse_discovery(content,code,now,*,title='招募说明书'):
    from .product_verification import official_url
    body=json.loads(content);rows=body.get('data')
    if not isinstance(rows,list) or len(rows)>10: raise ValueError('INVALID_DISCLOSURE_PAGE')
    choices=[]
    for row in rows:
        codes=row.get('secCode') or row.get('seccode') or []
        if isinstance(codes,str): codes=[codes]
        if code[:6] not in codes or title not in row.get('title',''): continue
        date=str(row.get('publishTime',''))[:10]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date) or date>now.date().isoformat(): continue
        path=row.get('attachPath')
        if not isinstance(path,str) or not path.lower().endswith('.pdf'):continue
        url=official_url(urljoin('https://disc.static.szse.cn',path))
        choices.append((date,url))
    return max(choices)[1] if choices else None


def _download(url,payload=None):
    from .product_verification import official_url,MAX_DOCUMENT_BYTES
    from .stage2.mapping_maintenance import _NoRedirect
    url=official_url(url)
    if payload is not None and url!='https://www.szse.cn/api/disc/announcement/annList':
        raise ValueError('UNAPPROVED_DISCLOSURE_POST')
    headers={'User-Agent':'Mozilla/5.0 stock-v4-official-materials/1.0',
             'Referer':'https://www.szse.cn/disclosure/fund/notice/index.html' if 'szse.cn/' in url else 'https://www.sse.com.cn/'}
    data=None
    if payload is not None:
        data=encode(payload).encode();headers['Content-Type']='application/json'
    with build_opener(_NoRedirect()).open(Request(url,data=data,headers=headers),timeout=10) as response:
        content=response.read(MAX_DOCUMENT_BYTES+1)
    if not content or len(content)>MAX_DOCUMENT_BYTES:raise ValueError('DOCUMENT_SIZE_LIMIT')
    return content


def _fetch(ledger,url,request,checkpoint,now,*,kind,payload=None):
    from .stage2.mapping_maintenance import _cached, _read_market
    from .stage2.market_bootstrap import save
    from .stage2.tushare_bootstrap import SourceFailure
    scope=dict(url=url,day=now.date().isoformat())
    if payload is not None:scope['body']=payload
    if kind=='official_material':
        week=f'{now.isocalendar().year}-W{now.isocalendar().week}'
        observed=_cached(ledger,'rule_hash',dict(url=url,week=week))
        if observed:
            current,_=_read_market(ledger,observed[0])
            # A hash mismatch already observed by verification invalidates the
            # old download receipt without resetting the enclosing job budget.
            scope['known_official_sha256']=current.get('sha256')
    cached=_cached(ledger,kind,scope)
    if cached:
        body,at=_read_market(ledger,cached[0])
        with ledger._connect() as conn:
            row=conn.execute('SELECT content FROM product_verification_documents WHERE ns=%s AND sha256=%s',
                             (ledger.ns,body['sha256'])).fetchone()
        if row and sha256(bytes(row[0])).hexdigest()==body['sha256']:
            return bytes(row[0]),at,cached[0]
    def operation():
        content=_download(url,payload); at=datetime.now(CHINA); digest=sha256(content).hexdigest()
        with ledger._connect() as conn:
            conn.execute('INSERT INTO product_verification_documents(ns,sha256,content,imported_at) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                         (ledger.ns,digest,content,at))
        return save(ledger,'maintenance',kind,scope,dict(sha256=digest,checked_at=at.isoformat()),1,None,at)
    checkpoint(); response=request('maintenance:'+kind,scope,operation); checkpoint()
    if response.get('status')!='SAVED': raise SourceFailure.from_receipt(response)
    batch=response['receipt']['batch']; body,at=_read_market(ledger,batch)
    with ledger._connect() as conn:
        row=conn.execute('SELECT content FROM product_verification_documents WHERE ns=%s AND sha256=%s',
                         (ledger.ns,body['sha256'])).fetchone()
    if not row or sha256(bytes(row[0])).hexdigest()!=body['sha256']:
        raise ValueError('DURABLE_DOCUMENT_HASH_MISMATCH')
    return bytes(row[0]),at,batch


def _target_history(ledger,codes):
    with ledger._connect() as conn:
        rows=conn.execute('''SELECT DISTINCT ON (etf_code) etf_code,product_basis,state,updated_at
            FROM product_verification_targets WHERE ns=%s AND etf_code=ANY(%s::text[])
            ORDER BY etf_code,updated_at DESC,target_id''',(ledger.ns,list(codes))).fetchall()
        active=conn.execute("SELECT etf_code FROM product_verification_requests WHERE ns=%s AND state IN ('PENDING','WAITING_DOCUMENTS','VERIFIED')",
                            (ledger.ns,)).fetchall()
    return {code:dict(basis=decode(basis),state=state,at=at) for code,basis,state,at in rows},{row[0] for row in active}


def associated_targets(ledger,mapping,now,target_codes=None):
    from .stage2.mapping_maintenance import _priorities
    from .stage2.penetration import _recommend_sector,_add_explicit_relations,product_entry_reason
    from .simulator import CODE,moment
    from .stage2.etf_representativeness import PARAMETERS
    if target_codes is not None and (not isinstance(target_codes,(list,tuple)) or not 1<=len(target_codes)<=MAX_TARGETS
            or any(not isinstance(c,str) or not CODE.fullmatch(c) for c in target_codes)
            or len(set(target_codes))!=len(target_codes)):
        raise ValueError('INVALID_MATERIAL_TARGET_CODES')
    held,sectors=_priorities(ledger)
    _, seeds = material_seed_config(mapping)
    targets={}
    broad=dict(PARAMETERS)['broad_sector_min_members']
    exposure={}
    # Reuse the exact association calculation before research's display cap and
    # same-index representative choice. Those presentation limits must not starve
    # another product of its first evidence review. Reads only existing mapping.
    source=mapping if target_codes is None else {**mapping,'products':{c:p for c,p in mapping['products'].items() if c in target_codes}}
    for sid in sectors:  # _priorities itself reads at most 100 active/held rows.
        details=_recommend_sector(source,sid,None,now,[],'UNKNOWN',set(),direction_policy=True)
        _add_explicit_relations(details,source,sid,None,now)
        for item in details['products']:
            code=item['etf']
            # These are normalized internal weights already validated by the
            # penetration calculation, not ledger amount inputs. Division can
            # legitimately produce more than the ledger's 18 input digits.
            if item.get('association_status')!='VERIFIED' and not any(Decimal(r['weight'])>0 for r in item.get('matched',[])): continue
            targets.setdefault(code,[]).append(sid)
            # Overlap with a broad sector says nothing about the product, so it earns no place in the queue.
            if len((source['sectors'].get(sid) or {}).get('members',()))<broad:
                exposure[code]=max(exposure.get(code,Decimal(0)),Decimal(str(item.get('metrics',{}).get('E') or 0)))
    # A reviewed attribution is the only way into a broad sector. Verify its products before the
    # sector turns up, ahead of products that are associated by overlap alone.
    attributed={}
    for row in (mapping.get('etf_taxonomy') or {}).get('rows',()):
        if row.get('evidence_refs') and moment(row['available_at'])<=now:
            attributed.setdefault(row['index_code'],[]).append(row['sector_id'])
    for code,product in source['products'].items():
        if product.get('index_code') in attributed and product.get('list_status','L')=='L':
            targets.setdefault(code,[]).extend(
                sid for sid in attributed[product['index_code']] if sid not in targets[code])
    for code in seeds.intersection(source['products']):
        if source['products'][code].get('list_status', 'L') == 'L':
            targets.setdefault(code, [])
    if target_codes is not None:
        missing=set(target_codes)-set(targets)
        if missing:raise ValueError('TARGET_WITHOUT_ACTIVE_ASSOCIATION:'+','.join(sorted(missing)))
        # Explicit scope stays stable across duplicate clicks and state changes.
        return [(code,targets[code]) for code in target_codes]
    history,active=_target_history(ledger,targets)
    depth={}  # largest fund of each attributed index first, so one pass reaches every index
    for index in attributed:
        funds=sorted((c for c,p in mapping['products'].items() if p.get('index_code')==index),
                     key=lambda c:(-Decimal(str((mapping['products'][c].get('stats') or {}).get('aum_yuan') or 0)),c))
        depth.update((c,n) for n,c in enumerate(funds))
    ranked=[]
    for code in targets:
        reason=product_entry_reason(mapping['products'][code],now)
        if reason is None or reason=='PRODUCT_OUTSIDE_SCOPE' or code in active:continue
        prior=history.get(code)
        if prior and prior['basis']!=product_basis(mapping['products'][code]):prior=None
        if prior and prior['state']=='AWAITING_REVIEW':continue
        priority = 0 if code in held else 1 if code in depth else 2 if targets[code] else 3
        order = depth[code] if priority==1 else -exposure.get(code,Decimal(0)) if priority==2 else 0
        ranked.append((priority,prior is not None,order,prior['at'] if prior else now,code))
    ranked.sort()
    reserved=[row for row in ranked if row[0]==2][:2]  # overlap-associated products keep moving
    chosen=list(dict.fromkeys(ranked[:MAX_TARGETS-len(reserved)]+reserved+ranked))[:MAX_TARGETS]
    return [(row[4],targets[row[4]]) for row in chosen]


def _save_target(ledger,target_id,*,state,reason=None,documents=None,request_id=None):
    with ledger._connect() as conn:
        conn.execute('''UPDATE product_verification_targets SET state=%s,reason=%s,
            documents=COALESCE(%s::jsonb,documents),request_id=COALESCE(%s,request_id),updated_at=clock_timestamp()
            WHERE ns=%s AND target_id=%s''',
            (state,reason,encode(documents) if documents is not None else None,request_id,ledger.ns,target_id))


def prepare_materials(ledger,mapping,market,now,request,checkpoint,*,target_codes=None,source_upgrades=None):
    from .left_right_backfill import Interrupted
    from .product_verification import queue_snapshot
    from .stage2.provider_errors import ProviderFailure
    if source_upgrades is not None:
        return _apply_source_upgrades(ledger,mapping,now,request,checkpoint,source_upgrades)
    interrupted=None
    for code,sectors in associated_targets(ledger,mapping,now,target_codes):
        from .stage2.penetration import product_entry_reason
        if product_entry_reason(mapping['products'][code],now) is None:continue
        basis=product_basis(mapping['products'][code]); target_id='pt-'+fingerprint([code,basis])[:40]
        with ledger._connect() as conn:
            conn.execute('''INSERT INTO product_verification_targets
                (ns,target_id,etf_code,mapping_version,product_basis,sector_ids,state,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,'NEEDS_MATERIALS',%s,%s) ON CONFLICT DO NOTHING''',
                (ledger.ns,target_id,code,mapping['version'],encode(basis),encode(sectors),now,now))
            row=conn.execute('SELECT state,documents,request_id FROM product_verification_targets WHERE ns=%s AND target_id=%s',
                             (ledger.ns,target_id)).fetchone()
            existing=conn.execute('''SELECT request_id,state FROM product_verification_requests
                WHERE ns=%s AND etf_code=%s AND state IN ('PENDING','WAITING_DOCUMENTS','VERIFIED')
                ORDER BY created_at DESC LIMIT 1''',(ledger.ns,code)).fetchone()
            previous=conn.execute('SELECT state FROM product_verification_requests WHERE ns=%s AND request_id=%s',
                                  (ledger.ns,row[2])).fetchone() if row[2] else None
        if existing:
            _save_target(ledger,target_id,state='QUEUED',request_id=existing[0]); continue
        # A failed official hash check must refresh the old target's material,
        # even though its target was previously marked QUEUED at submission.
        if previous and previous[0] in ('REVIEW_REQUIRED','NEEDS_REBASE') and row[0]=='QUEUED':
            row=('REVIEW_REQUIRED',row[1],row[2])
        if row[0]=='AWAITING_REVIEW': continue
        documents=decode(row[1]); previous_hashes={k:v['sha256'] for k,v in documents.items()}; sources=source_seeds(code)
        discovery_note=None
        try:
            checkpoint()
            if code.endswith('.SH'):
                # One bounded discovery page. Seeds keep known official URLs usable if search is unavailable.
                try:
                    data,_,_=_fetch(ledger,sse_discovery_url(code),request,checkpoint,now,kind='official_discovery')
                    latest=parse_sse_discovery(data,code,now)
                    if latest: sources['prospectus']=latest
                except (ProviderFailure,OSError,ValueError) as exc:
                    if not sources: raise
                    discovery_note='DISCOVERY_UNAVAILABLE_USING_OFFICIAL_SOURCE_SEEDS:'+type(exc).__name__
                for label,url in exchange_rule_sources(code):sources.setdefault(label,url)
            elif code.endswith('.SZ'):
                for label,title in (('prospectus','招募说明书'),('listing','上市交易公告书')):
                    data,_,_=_fetch(ledger,'https://www.szse.cn/api/disc/announcement/annList',request,checkpoint,now,
                                   kind='official_discovery',payload=szse_discovery_body(code,now,title=title))
                    url=parse_szse_discovery(data,code,now,title=title)
                    if url:sources[label]=url
                # Keep the missing-product-document failure; shared rules alone are not a dossier.
                if sources:
                    for label,url in exchange_rule_sources(code):sources.setdefault(label,url)
            if not sources: raise ValueError('OFFICIAL_DOCUMENTS_NOT_FOUND')
            for label,url in list(sources.items())[:4]:
                if label in documents and documents[label]['url']==url and row[0]!='REVIEW_REQUIRED': continue
                content,at,batch=_fetch(ledger,url,request,checkpoint,now,kind='official_material')
                documents[label]=dict(url=url,sha256=sha256(content).hexdigest(),acquired_at=at.isoformat(),
                                      source_batch=batch,segments=extract_segments(content))
                _save_target(ledger,target_id,state='NEEDS_MATERIALS',documents=documents)
            changed={k:v['sha256'] for k,v in documents.items()}!=previous_hashes
            _save_target(ledger,target_id,state='AWAITING_REVIEW' if row[0]!='REVIEW_REQUIRED' or changed else 'REVIEW_REQUIRED',
                         reason='OFFICIAL_MATERIAL_UNCHANGED_REVIEW_STILL_REQUIRED' if row[0]=='REVIEW_REQUIRED' and not changed else discovery_note,
                         documents=documents)
        except Interrupted as exc:
            interrupted=str(exc); break
        except (ProviderFailure,OSError,ValueError,TypeError,KeyError) as exc:
            _save_target(ledger,target_id,state='NEEDS_MATERIALS',reason=str(exc)[:300],documents=documents)
    result=queue_snapshot(ledger)
    if interrupted: result['interrupted']=interrupted
    return result


def get_target(ledger,target_id):
    if not isinstance(target_id,str) or not re.fullmatch(r'pt-[0-9a-f]{40}',target_id):
        raise ValueError('INVALID_MATERIAL_TARGET')
    with ledger._connect() as conn:
        row=conn.execute('''SELECT etf_code,mapping_version,product_basis,sector_ids,state,documents,request_id,reason
            FROM product_verification_targets WHERE ns=%s AND target_id=%s''',(ledger.ns,target_id)).fetchone()
    if not row: raise ValueError('MATERIAL_TARGET_NOT_FOUND')
    result=dict(zip(('etf_code','mapping_version','product_basis','sector_ids','state','documents','request_id','reason'),row))
    for key in ('product_basis','sector_ids','documents'): result[key]=decode(result[key])
    return result


def review_contract():
    """Exact submit shapes; examples describe structure, never approve a product."""
    from .product_verification import FIELDS
    fields=['decision','scope','trade_unit','price_limit_ratio','settlement_cycle','effective_at','documents','claims','reason']
    example=dict(decision='APPROVE',scope='domestic_passive_sector',trade_unit=100,price_limit_ratio='0.1',
        settlement_cycle='T+1',effective_at='2026-10-30T00:00:00+08:00',
        documents={label:{'effective_at':date+'T00:00:00+08:00'} for label,date in
                   (('prospectus','2019-05-08'),('index_method','2026-10-30'),
                    ('trade_guide','2019-11-29'),('price_rule','2026-07-06'))},
        claims={field:[dict(document=label,locator='paragraph:1',excerpt='复制该字段适用的官方原文')]
                for field,label in (('etf_code','prospectus'),('scope','prospectus'),('index_code','index_method'),
                                    ('trade_unit','trade_guide'),('price_limit_ratio','price_rule'),('settlement_cycle','trade_guide'))},
        reason='说明全部字段依据及一般规则、例外条件如何适用于该产品')
    return dict(decisions=['APPROVE','NEEDS_MATERIALS'],fields=fields,
        variants={
            'NEEDS_MATERIALS':dict(required=['decision','reason'],additional_properties=False,
                example=dict(decision='NEEDS_MATERIALS',reason='缺少适用涨跌幅规则的官方条款，需补材料')),
            'APPROVE':dict(required=fields,additional_properties=False,example=example)},
        example_notice='示例仅演示合法JSON形状，所有日期均非默认值；不同文档分别核定，不复制同一个日期。代码、数值、日期、文档标签、定位和原文都须按本目标返回材料核读替换。',
        scope_enum=['domestic_passive_sector'],required_claims=list(FIELDS),
        claims_shape='claims必须为对象，六个字段名各映射1至4条{document,locator,excerpt}数组；不是带field属性的列表。',
        server_bound_fields=['target_id','etf_code','index_code','expected_mapping_version','reviewed_at','reviewed_by'],
        server_bound_note='target_id仅放工具顶层参数；以上字段不要放review内，由服务端绑定。',
        documents='选择1至4个已有label，每个值仅为{effective_at:带时区ISO时间}，不传URL、文件、hash。',
        effective_dates='effective_at表示该产品/规则的实际生效或适用时间；documents各自保留原文生效/适用时间。按施行/生效条款核定，不以acquired_at或当前时间代替；区分合同生效日、正文截止日、发布日期、指数数据日期，不把后者自动当作法规生效日；缺少必要日期依据使用NEEDS_MATERIALS。',
        date_locator_example='例如现行交易规则第11.9条原文“本规则自2026年7月6日起施行”，应引用返回的该条真实locator并据此填写price_rule日期；招募说明书、指数资料及交易指南需分别查各自原文。',
        claims='每条引用使用返回的精确locator与该段落原文，保留规则适用条件。',
        scalar_evidence='etf_code/index_code引用含实际代码；trade_unit含整数及份；price_limit_ratio含对应百分数；settlement_cycle含T+1。一般比例与例外条件都须判断适用性，不能仅按数字匹配批准。',
        attribution='Server records OCTOP AI-assisted review, not a human signature.',
        incomplete='任何必要字段缺证据时，只提交NEEDS_MATERIALS示例的decision和reason两个字段，不提交空值或半份APPROVE。')


def context_segments(segments,terms):
    """Keep regulatory scope, neighboring exception conditions and effective date."""
    important=[];dates=[]
    for index,segment in enumerate(segments):
        compact=re.sub(r'\s+','',segment['text'])
        if (re.search(r'基金.{0,100}涨跌幅|涨跌幅.{0,100}基金',compact)
                or re.search(r'(?:本规则|本通知|本办法)自.{0,30}(?:施行|实施|生效)',compact)):
            # DOCX/HTML conditions often occupy separate adjacent paragraphs.
            neighbors=range(max(0,index-1),min(len(segments),index+3)) if segment['locator'].startswith('paragraph:') else [index]
            important.extend(neighbors)
        # Short verbatim windows keep dated applicability evidence visible even
        # when it occurs late on a long PDF page. Dates remain evidence, not an
        # inferred effective_at value or a replacement for the recorded source.
        text=segment['text']
        for match in re.finditer(r'(?:19|20)\d{2}(?:\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|[-/]\d{1,2}[-/]\d{1,2})',text):
            nearby=re.sub(r'\s+','',text[max(0,match.start()-120):match.end()+120])
            explicit=(re.search(r'本(?:规则|通知|办法)自.{0,40}(?:施行|实施|生效)',nearby)
                      or '基金合同' in nearby and '已于' in nearby and '生效' in nearby)
            score=0 if explicit else 1 if index<4 and any(t in nearby for t in ('截止','发布日期','更新日期')) else 2
            if explicit or index<3 or score==1:
                dates.append((score,index,max(0,match.start()-300),min(len(text),match.end()+400)))
    ranked=sorted(range(len(segments)),key=lambda i:(-sum(term in segments[i]['text'] for term in terms if term),i))
    chosen=[];remaining=MAX_CONTEXT_DOCUMENT;seen=set()
    # Reserve at most 2k of the same 8k document allowance for dates and cover.
    date_budget=2000;date_seen=set()
    if segments and segments[0]['locator']=='page:1':
        cover=segments[0]['text'][:400];chosen.append(dict(locator='page:1',text=cover))
        remaining-=len(cover);date_budget-=len(cover)
        if len(cover)==len(segments[0]['text']):seen.add(0)
    for _,index,start,end in sorted(dates):
        if index in date_seen or index in seen or date_budget<400:continue
        text=segments[index]['text'][start:min(end,start+date_budget)]
        chosen.append(dict(locator=segments[index]['locator'],text=text))
        remaining-=len(text);date_budget-=len(text);date_seen.add(index)
        if start==0 and len(text)==len(segments[index]['text']):seen.add(index)
    for index in [*important,*ranked]:
        if index in seen:continue
        seen.add(index)
        if not remaining:break
        segment=segments[index];text=segment['text'][:min(remaining,4000)]
        chosen.append(dict(locator=segment['locator'],text=text));remaining-=len(text)
    return chosen


def review_context(ledger,target_id):
    target=get_target(ledger,target_id); documents={}
    if target['request_id']:
        from .product_verification import status
        saved=status(ledger,target['request_id'])
        if target['state']=='QUEUED':
            target['state']=saved['state']
            target['reason']=saved['result'].get('reason') or target['reason']
    terms=(target['etf_code'][:6],str(target['product_basis'].get('index_code','')).split('.')[0],
           '标的指数','被动','指数型','完全复制','样本空间','涨跌幅','100份','T+1','上市','生效')
    for label,doc in target['documents'].items():
        documents[label]={**{k:v for k,v in doc.items() if k!='segments'},'segments':context_segments(doc['segments'],terms),
                          'truncated':sum(len(s['text']) for s in doc['segments'])>MAX_CONTEXT_DOCUMENT}
    return dict(target_id=target_id,state=target['state'],request_id=target['request_id'],etf_code=target['etf_code'],
        index_code=target['product_basis'].get('index_code'),documents=documents,reason=target['reason'],
        review_contract=review_contract())
