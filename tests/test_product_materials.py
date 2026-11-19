"""First-review production uses bounded actual source bytes, never inferred rules."""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modules.strategy_hermes import product_materials as materials
from modules.strategy_hermes import product_verification as verification
from modules.strategy_hermes.ledger import encode
import test_product_verification as review_fixtures
from test_product_verification import NOW, ETF, INDEX, dossier, mapping

TEXT='<html><p>基金代码588100，指数000685。指数型行业基金完全复制。</p><p>100份，20%，T+1。</p></html>'.encode()
URL='https://www.sse.com.cn/material-test.html'


def approval(context):
    label=next(iter(context['documents'])); segments=context['documents'][label]['segments']
    # All fields use actual returned test paragraphs. No claim of real official semantics.
    citations=[dict(document=label,locator=s['locator'],excerpt=s['text'].strip()) for s in segments]
    return dict(decision='APPROVE',scope='domestic_passive_sector',trade_unit=100,price_limit_ratio='0.2',
        settlement_cycle='T+1',effective_at='2026-01-01T00:00:00+08:00',
        documents={label:dict(effective_at='2026-01-01T00:00:00+08:00')},
        claims={f:citations for f in verification.FIELDS},reason='Synthetic attributed review fixture')


class MaterialContract(unittest.TestCase):
    def test_document_text_with_null_characters_keeps_citable_segments(self):
        page = SimpleNamespace(get_contents=lambda: None, extract_text=lambda: '基金\x00代码588100')
        reader = SimpleNamespace(is_encrypted=False, pages=[page], close=lambda: None)
        with patch('pypdf.PdfReader', return_value=reader):
            self.assertEqual(materials.extract_segments(b'%PDF'),
                             [dict(locator='page:1', text='基金 代码588100')])
        self.assertEqual(materials.extract_segments(b'<p>ETF\x00 code</p>'),
                         [dict(locator='paragraph:1', text='ETF  code')])

    def test_docx_rules_keep_general_limit_exception_conditions_and_effective_date(self):
        from io import BytesIO
        from zipfile import ZipFile
        paragraphs=['规则目录']*100+[
            '3.3.13 本所对股票、基金交易实行价格涨跌幅限制，涨跌幅限制比例为10%。',
            '6.6 本所上市交易的以下基金，涨跌幅限制比例为20%：',
            '（一）跟踪指数成份股仅为科创板股票或其他涨跌幅限制比例为20%的股票的交易型开放式指数基金；',
            '（二）根据基金合同约定，相关股票比例不低于80%的上市开放式基金。',
            '11.9 本规则自2026年7月6日起施行。']
        raw=BytesIO()
        xml=('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'+
            ''.join('<w:p><w:r><w:t>'+text+'</w:t></w:r></w:p>' for text in paragraphs)+'</w:document>').encode()
        with ZipFile(raw,'w') as archive:archive.writestr('word/document.xml',xml)
        segments=materials.extract_segments(raw.getvalue())
        selected=materials.context_segments(segments,('基金','涨跌幅'))
        text=''.join(row['text'] for row in selected)
        self.assertIn('涨跌幅限制比例为10%',text)
        self.assertIn('涨跌幅限制比例为20%',text)
        self.assertIn('比例不低于80%',text)
        self.assertIn('2026年7月6日起施行',text)
        self.assertLessEqual(len(text),materials.MAX_CONTEXT_DOCUMENT)
        self.assertEqual(segments[-1]['locator'],'paragraph:105')
        unsafe=BytesIO()
        with ZipFile(unsafe,'w') as archive:archive.writestr('word/document.xml',b'<!DOCTYPE x [<!ENTITY e "unsafe">]><x/>')
        with self.assertRaisesRegex(ValueError,'OFFICIAL_DOCX_XML_UNSAFE'):materials.extract_segments(unsafe.getvalue())

    def test_review_contract_has_exact_branch_shapes_and_dictionary_citations(self):
        contract=materials.review_contract()
        self.assertIn('decision',contract['fields'])
        needs=contract['variants']['NEEDS_MATERIALS']
        self.assertEqual(set(needs['example']),{'decision','reason'})
        approve=contract['variants']['APPROVE']
        self.assertEqual(set(approve['example']),set(approve['required']))
        self.assertEqual(approve['example']['scope'],'domestic_passive_sector')
        self.assertEqual(set(approve['example']['claims']),set(verification.FIELDS))
        self.assertNotIn('target_id',approve['required'])
        self.assertIn('不以acquired_at',contract['effective_dates'])
        self.assertEqual(len({doc['effective_at'] for doc in approve['example']['documents'].values()}),4)

    def test_context_keeps_cover_and_actual_contract_date_beyond_page_prefix(self):
        cover='基金招募说明书（2026年第1号）'
        cutoff='本招募说明书所载内容截止日为2026年04月22日。'
        effective='《基金合同》已于2019年5月8日生效。'
        segments=[dict(locator='page:1',text=cover),dict(locator='page:2',text='标的指数 被动 指数型 完全复制。'*180),
                  dict(locator='page:3',text='风险说明。'*100+cutoff),
                  dict(locator='page:39',text='普通段落。'*1100+effective+'自基金合同生效日起开始管理本基金。')]
        selected=materials.context_segments(segments,('基金','标的指数','被动','生效'))
        text=''.join(s['text'] for s in selected)
        self.assertIn(cover,text);self.assertIn(cutoff,text);self.assertIn(effective,text)
        self.assertLessEqual(len(text),materials.MAX_CONTEXT_DOCUMENT)
        originals={s['locator']:s['text'] for s in segments}
        self.assertTrue(all(s['text'] in originals[s['locator']] for s in selected))

    def test_pinned_pdf_reader_extracts_page_text_with_scoped_limits(self):
        from io import BytesIO
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
        writer=PdfWriter();page=writer.add_blank_page(width=200,height=200)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),
                               NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 10 100 Td (Synthetic official PDF text) Tj ET')
        page[NameObject('/Contents')]=stream
        output=BytesIO();writer.write(output);writer.close()
        segments=materials.extract_segments(output.getvalue())
        self.assertEqual(segments[0]['locator'],'page:1')
        self.assertIn('Synthetic official PDF text',segments[0]['text'])

    def test_html_text_has_bound_locators_and_excludes_scripts(self):
        segments=materials.extract_segments(b'<p>100 units</p><script>fake approval</script><p>T+1</p>')
        self.assertTrue(all(s['locator'].startswith('paragraph:') for s in segments))
        self.assertNotIn('fake approval',str(segments))
        self.assertIn('T+1',str(segments))

    def test_discovery_selects_actual_latest_matching_product_not_response_order(self):
        rows=[dict(SECURITY_CODE='588100',TITLE='更新招募说明书',SSEDATE=day,URL='/'+name+'.pdf')
              for day,name in [('2026-10-02','new'),('2025-02-01','old')]]
        rows.append(dict(SECURITY_CODE='512480',TITLE='招募说明书',SSEDATE='2026-11-16',URL='/wrong.pdf'))
        self.assertEqual(materials.parse_sse_discovery(json.dumps({'result':rows}),ETF,NOW),'https://www.sse.com.cn/new.pdf')

    def test_basis_ignores_only_unrelated_statistics(self):
        original=mapping()['products'][ETF]; changed=deepcopy(original)
        changed['stats']={'aum_yuan':'123'}
        self.assertEqual(materials.product_basis(original),materials.product_basis(changed))
        changed['trade_unit']=200
        self.assertNotEqual(materials.product_basis(original),materials.product_basis(changed))

    def test_szse_uses_official_post_scope_and_matching_document(self):
        scope=materials.szse_discovery_body('159001.SZ',NOW)
        self.assertEqual((scope['pageSize'],scope['pageNum'],scope['channelCode']),(10,1,['fundinfoNotice_disc']))
        body={'data':[dict(secCode=['159001'],title='基金更新招募说明书',publishTime='2026-11-16 10:00:00',
                           attachPath='/download/disc/finalpage/fund.PDF')]}
        self.assertEqual(materials.parse_szse_discovery(json.dumps(body),'159001.SZ',NOW),
                         'https://disc.static.szse.cn/download/disc/finalpage/fund.PDF')

    def test_exchange_rules_are_shared_per_listing_exchange(self):
        sh=dict(materials.exchange_rule_sources('588100.SH'));sz=dict(materials.exchange_rule_sources('159915.SZ'))
        self.assertEqual(sh,dict(trade_guide=materials.TRADE_GUIDE,price_rule=materials.PRICE_RULE))
        self.assertEqual(sz,dict(trade_guide=materials.SZSE_TRADE_GUIDE,price_rule=materials.SZSE_PRICE_RULE))
        for url in sz.values():
            self.assertEqual(verification.official_url(url),url);self.assertIn('szse.cn/',url)
        self.assertEqual(materials.exchange_rule_sources('920001.BJ'),())

    def test_szse_rule_wording_satisfies_existing_scalar_citations(self):
        # Sentences as published in the two configured SZSE sources; the validator is unchanged.
        page=('<html><p>基金代码159915，指数399006。</p><p>最小交易单位为1手（1手为100份），价格最小变动单位为0.001元</p>'
              '<p>境内A股ETF则为T+1交易，即T日买入的ETF T+1日方可卖出。</p><p>基金的价格涨跌幅限制比例为10%。</p></html>').encode()
        segments=materials.extract_segments(page)
        cite=lambda needle:[dict(document='source',locator=s['locator'],excerpt=s['text'].strip()) for s in segments if needle in s['text']]
        review=dict(trade_unit=100,price_limit_ratio='0.1',settlement_cycle='T+1',
            claims=dict(etf_code=cite('159915'),index_code=cite('399006'),trade_unit=cite('100份'),
                        price_limit_ratio=cite('10%'),settlement_cycle=cite('T+1')))
        self.assertTrue(all(review['claims'].values()))
        verification.validate_review_claim_values(review,'159915.SZ','399006.SZ')

    def test_target_selection_needs_active_real_association_and_is_bounded(self):
        current=mapping()
        for number in range(12):current['products'][f'{588101+number}.SH']=deepcopy(current['products'][ETF])
        current['products']['512800.SH']=dict(index_code='missing',rule={'verified':False})
        with patch('modules.strategy_hermes.stage2.mapping_maintenance._priorities',return_value=([],['kaipanla:chip'])), \
             patch.object(materials,'_target_history',return_value=({},set())):
            selected=materials.associated_targets(None,current,NOW)
        self.assertLessEqual(len(selected),8)
        self.assertTrue(selected)
        self.assertNotIn('512800.SH',[code for code,_ in selected])
        with patch('modules.strategy_hermes.stage2.mapping_maintenance._priorities',return_value=([],[])), \
             patch.object(materials,'_target_history',return_value=({},set())):
            self.assertEqual(materials.associated_targets(None,current,NOW),[])

    def test_fresh_and_oldest_failed_targets_progress_without_fixed_prefix_starvation(self):
        current=mapping()
        for i in range(12):current['products'][f'{588101+i}.SH']=deepcopy(current['products'][ETF])
        codes=sorted(current['products'])
        history={c:dict(basis=materials.product_basis(current['products'][c]),state='NEEDS_MATERIALS',at=NOW)
                 for c in codes[:8]}
        with patch('modules.strategy_hermes.stage2.mapping_maintenance._priorities',return_value=([],['kaipanla:chip'])), \
             patch.object(materials,'_target_history',return_value=(history,set())):
            chosen=materials.associated_targets(None,current,NOW)
        self.assertTrue(set(codes[8:])<={c for c,_ in chosen})
        for c in codes:history[c]=dict(basis=materials.product_basis(current['products'][c]),state='NEEDS_MATERIALS',at=NOW)
        history[codes[-1]]['at']=NOW-timedelta(days=1)
        history[codes[0]]['state']='AWAITING_REVIEW'
        with patch('modules.strategy_hermes.stage2.mapping_maintenance._priorities',return_value=([],['kaipanla:chip'])), \
             patch.object(materials,'_target_history',return_value=(history,{codes[1]})):
            chosen=materials.associated_targets(None,current,NOW)
        self.assertEqual(chosen[0][0],codes[-1])
        self.assertFalse({codes[0],codes[1]}&{c for c,_ in chosen})

    def test_explicit_scope_is_stable_but_requires_active_true_association(self):
        current=mapping();current['products']['512480.SH']=deepcopy(current['products'][ETF])
        with patch('modules.strategy_hermes.stage2.mapping_maintenance._priorities',return_value=([],['kaipanla:chip'])):
            self.assertEqual([c for c,_ in materials.associated_targets(None,current,NOW,[ETF,'512480.SH'])],[ETF,'512480.SH'])
            with self.assertRaisesRegex(ValueError,'TARGET_WITHOUT_ACTIVE_ASSOCIATION'):
                materials.associated_targets(None,current,NOW,['159001.SZ'])

    def test_internal_normalized_weights_keep_full_precision_for_association(self):
        current=mapping()
        current['weights'][INDEX]['rows']=[dict(code='688001.SH',weight='0.3333',mainboard=False),
                                          dict(code='688002.SH',weight='0.6666',mainboard=False)]
        with patch('modules.strategy_hermes.stage2.mapping_maintenance._priorities',return_value=([],['kaipanla:chip'])):
            self.assertEqual(materials.associated_targets(None,current,NOW,[ETF]),[(ETF,['kaipanla:chip'])])

    def test_verify_requires_frozen_bounded_request_ids(self):
        for ids in (None,[],['invalid'],['pv-'+'0'*40]*9):
            with self.subTest(ids=ids),self.assertRaises(ValueError):
                verification.apply_queued(None,mapping(),mapping(),{'mode':'verify'},NOW,None,None,request_ids=ids)

    def test_each_scalar_must_be_supported_by_its_own_field_citations(self):
        context=dict(documents={'source':dict(segments=materials.extract_segments(TEXT))})
        base=approval(context)
        verification.validate_review_claim_values(base,ETF,INDEX)
        for field,value in [('trade_unit',200),('price_limit_ratio','0.1'),('settlement_cycle','T+0')]:
            bad=deepcopy(base);bad[field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'CITATION_VALUE_MISMATCH:'+field):
                verification.validate_review_claim_values(bad,ETF,INDEX)
        for code,index in [('512800.SH',INDEX),(ETF,'399986.SZ')]:
            with self.assertRaisesRegex(ValueError,'CITATION_VALUE_MISMATCH'):
                verification.validate_review_claim_values(base,code,index)


@unittest.skipUnless(os.environ.get('STAGE2_TEST_ENV_FILE'),'requires isolated PostgreSQL/Redis')
class MaterialFlow(unittest.TestCase):
    def setUp(self):
        review_fixtures.PersistedReview.setUp(self)
        self.evidence_url='https://www.sse.com.cn/material-'+self.ns.rsplit(':',1)[-1]+'.html'
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        publish_mapping(self.store,self.map,{},at=NOW-timedelta(minutes=1))

    def clean(self):
        with self.store._connect() as conn:
            conn.execute('DELETE FROM product_verification_targets WHERE ns=%s',(self.ns,))
        review_fixtures.PersistedReview.clean(self)

    def prepare(self,content=TEXT):
        calls=[]
        def request(source,scope,operation):
            calls.append(source);return dict(status='SAVED',receipt=operation())
        def fetch(url,payload=None):
            if 'commonQuery' in url:return b'{"result":[]}'
            return content
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return NOW
        with patch.object(materials,'associated_targets',return_value=[(ETF,['kaipanla:chip'])]), \
             patch.object(materials,'source_seeds',return_value={'prospectus':self.evidence_url}), \
             patch.object(materials,'TRADE_GUIDE',self.evidence_url),patch.object(materials,'PRICE_RULE',self.evidence_url), \
             patch.object(materials,'_download',side_effect=fetch),patch.object(materials,'datetime',Clock):
            result=materials.prepare_materials(self.store,self.map,None,NOW,request,lambda:None)
        return result,calls

    def prepare_szse(self,found=True):
        tag=self.ns.rsplit(':',1)[-1];code='159'+format(int(sha256(tag.encode()).hexdigest(),16)%1000,'03d')+'.SZ'
        current=deepcopy(self.map);current['products'][code]=deepcopy(current['products'][ETF])
        guide,rule=('https://investor.szse.cn/material-'+tag+'-'+name+'.html' for name in ('guide','rule'))
        def request(source,scope,operation):return dict(status='SAVED',receipt=operation())
        def fetch(url,payload=None):
            if payload is None:return TEXT
            title=payload['searchKey'][0]
            rows=[dict(secCode=[code[:6]],title='基金'+title,publishTime='2026-11-16 10:00:00',
                       attachPath='/download/disc/'+tag+'-'+str(len(title))+'.PDF')] if found else []
            return json.dumps({'data':rows}).encode()
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return NOW
        with patch.object(materials,'associated_targets',return_value=[(code,['kaipanla:chip'])]), \
             patch.object(materials,'source_seeds',return_value={}), \
             patch.object(materials,'SZSE_TRADE_GUIDE',guide),patch.object(materials,'SZSE_PRICE_RULE',rule), \
             patch.object(materials,'_download',side_effect=fetch),patch.object(materials,'datetime',Clock):
            result=materials.prepare_materials(self.store,current,None,NOW,request,lambda:None)
        with self.store._connect() as conn:
            target_id=conn.execute('SELECT target_id FROM product_verification_targets WHERE ns=%s AND etf_code=%s',
                                   (self.ns,code)).fetchone()[0]
        return result,materials.get_target(self.store,target_id),guide,rule

    def test_szse_target_receives_shared_exchange_rules_with_its_own_documents(self):
        result,target,guide,rule=self.prepare_szse()
        self.assertEqual(result['counts']['AWAITING_REVIEW'],1)
        self.assertEqual(set(target['documents']),{'prospectus','listing','trade_guide','price_rule'})
        self.assertEqual((target['documents']['trade_guide']['url'],target['documents']['price_rule']['url']),(guide,rule))
        self.assertTrue(all(doc['segments'] for doc in target['documents'].values()))

    def test_szse_shared_rules_alone_do_not_form_a_dossier(self):
        result,target,_,_=self.prepare_szse(found=False)
        self.assertEqual((target['state'],target['reason']),('NEEDS_MATERIALS','OFFICIAL_DOCUMENTS_NOT_FOUND'))
        self.assertFalse(target['documents'])

    def test_producer_text_to_ai_review_to_formal_request_then_publish(self):
        result,calls=self.prepare()
        self.assertEqual(result['counts']['AWAITING_REVIEW'],1)
        target=result['items'][0]['target_id']
        context=verification.review_context(self.store,target)
        self.assertTrue(calls);self.assertEqual(context['state'],'AWAITING_REVIEW')
        review=approval(context)
        at=NOW+timedelta(minutes=1)
        receipt=verification.submit_review(self.store,target,review,now=at)
        self.assertEqual(receipt['state'],'PENDING')
        saved=verification.status(self.store,receipt['request_id'])
        self.assertEqual(saved['dossier']['reviewed_by'],'OCTOP AI-assisted official evidence review')
        self.assertEqual(saved['dossier']['reviewed_at'],at.isoformat())
        self.assertFalse(self.map['products'][ETF]['rule']['verified'])
        # Actual time is after the source observations; static verify has no market clock restriction.
        at=max(at,*[__import__('datetime').datetime.fromisoformat(v['acquired_at']) for v in context['documents'].values()])+timedelta(seconds=1)
        request=lambda source,scope,operation:dict(status='SAVED',receipt=operation())
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return at
        with patch('modules.strategy_hermes.stage2.mapping_maintenance._rule_hash',return_value=sha256(TEXT).hexdigest()), \
             patch.object(verification,'datetime',Clock):
            updated,summary=verification.apply_queued(self.store,self.map,deepcopy(self.map),{'mode':'verify'},at,
                request,lambda:None,clock=lambda:at+timedelta(seconds=1),request_ids=[receipt['request_id']])
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        publish_mapping(self.store,updated,summary,at=at+timedelta(seconds=2),expected_parent=self.map['version'])
        self.assertEqual(verification.status(self.store,receipt['request_id'])['state'],'PUBLISHED')
        self.assertEqual(verification.queue_snapshot(self.store)['counts']['PUBLISHED'],1)

    def test_unknown_or_fabricated_citations_do_not_create_requests(self):
        result,_=self.prepare();target=result['items'][0]['target_id'];context=verification.review_context(self.store,target)
        review=approval(context);review['claims']['scope'][0]['excerpt']='This sentence never occurred in the source'
        with self.assertRaisesRegex(ValueError,'CITATION_NOT_IN_OFFICIAL_TEXT'):
            verification.submit_review(self.store,target,review)
        self.assertNotIn('PENDING',verification.queue_snapshot(self.store)['counts'])

    def test_semantic_uncertainty_stays_visible_for_later_review(self):
        result,_=self.prepare();target=result['items'][0]['target_id']
        review=materials.review_contract()['variants']['NEEDS_MATERIALS']['example']
        review['reason']='Actual price limit not established'
        verification.submit_review(self.store,target,review)
        first=verification.queue_snapshot(self.store)
        self.assertEqual(first['counts']['REVIEW_REQUIRED'],1)
        self.assertIn('price limit',first['items'][0]['reason'])

    def test_source_upgrade_only_fetches_replacement_and_preserves_original_evidence(self):
        first,_=self.prepare();target=first['items'][0]['target_id']
        old=verification.review_context(self.store,target)['documents']['price_rule']
        replacement=self.evidence_url.replace('.html','-rules.html')
        content=TEXT+b'<p>General fund price limit 10%; conditional exception 20%.</p>'
        calls=[]
        def request(source,scope,operation):
            calls.append((source,scope['url']));return dict(status='SAVED',receipt=operation())
        with patch.object(materials,'TRADE_GUIDE',self.evidence_url),patch.object(materials,'PRICE_RULE',replacement), \
             patch.object(materials,'_download',return_value=content) as download, \
             patch.object(materials,'associated_targets',side_effect=AssertionError('must not rediscover targets')):
            changes=materials.source_upgrades(self.store,self.map,[ETF])
            self.assertEqual(len(changes),1)
            self.assertEqual(changes[0]['label'],'price_rule')
            materials.prepare_materials(self.store,self.map,None,NOW,request,lambda:None,source_upgrades=changes)
            materials.prepare_materials(self.store,self.map,None,NOW,request,lambda:None,source_upgrades=changes)
            download.assert_called_once_with(replacement,None)
        self.assertEqual(calls,[('maintenance:official_material',replacement)])
        current=verification.review_context(self.store,target)
        self.assertEqual(current['state'],'AWAITING_REVIEW')
        self.assertEqual(current['documents']['price_rule']['sha256'],sha256(content).hexdigest())
        self.assertEqual(current['documents']['prospectus']['sha256'],old['sha256'])
        with self.store._connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT 1 FROM product_verification_documents WHERE ns=%s AND sha256=%s',
                                             (self.ns,old['sha256'])).fetchone())

    def test_source_upgrade_does_not_overwrite_review_submitted_during_http(self):
        first,_=self.prepare();target=first['items'][0]['target_id']
        context=verification.review_context(self.store,target);original=materials.get_target(self.store,target)['documents']
        replacement=self.evidence_url.replace('.html','-racing-rules.html')
        content=TEXT+b'<p>Replacement rule document.</p>';submitted=[]
        def fetch(url,payload=None):
            submitted.append(verification.submit_review(self.store,target,approval(context),now=NOW+timedelta(seconds=1)))
            return content
        request=lambda source,scope,operation:dict(status='SAVED',receipt=operation())
        with patch.object(materials,'TRADE_GUIDE',self.evidence_url),patch.object(materials,'PRICE_RULE',replacement), \
             patch.object(materials,'_download',side_effect=fetch):
            changes=materials.source_upgrades(self.store,self.map,[ETF])
            result=materials.prepare_materials(self.store,self.map,None,NOW,request,lambda:None,source_upgrades=changes)
        current=materials.get_target(self.store,target)
        self.assertEqual(current['state'],'QUEUED')
        self.assertEqual(current['request_id'],submitted[0]['request_id'])
        self.assertEqual(current['documents'],original)
        self.assertEqual(result['counts']['PENDING'],1)
        with self.store._connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT 1 FROM product_verification_documents WHERE ns=%s AND sha256=%s',
                                             (self.ns,sha256(content).hexdigest())).fetchone())

    def test_genuine_excerpt_from_other_product_cannot_approve_this_target(self):
        wrong=TEXT.replace(b'588100',b'512800').replace(b'000685',b'399986')
        result,_=self.prepare(wrong);target=result['items'][0]['target_id']
        review=approval(verification.review_context(self.store,target))
        with self.assertRaisesRegex(ValueError,'CITATION_VALUE_MISMATCH:etf_code'):
            verification.submit_review(self.store,target,review)
        self.assertNotIn('PENDING',verification.queue_snapshot(self.store)['counts'])

    def test_changed_official_hash_refreshes_queued_target_and_allows_new_ai_review(self):
        from modules.strategy_hermes.stage2.market_bootstrap import save
        result,_=self.prepare();target=result['items'][0]['target_id']
        context=verification.review_context(self.store,target)
        receipt=verification.submit_review(self.store,target,approval(context),now=NOW+timedelta(seconds=1))
        verification._record_attempt(self.store,receipt['request_id'],'REVIEW_REQUIRED',
                                     {'reason':'OFFICIAL_DOCUMENT_CHANGED:prospectus'},NOW+timedelta(seconds=2))
        changed=TEXT+b'<p>New official observation after prior review.</p>'
        scope=dict(url=self.evidence_url,week=f'{NOW.isocalendar().year}-W{NOW.isocalendar().week}')
        save(self.store,'maintenance','rule_hash',scope,{'sha256':sha256(changed).hexdigest()},1,None,NOW+timedelta(seconds=2))
        result,calls=self.prepare(changed)
        self.assertTrue(calls)
        self.assertEqual(result['counts']['AWAITING_REVIEW'],1)
        context=verification.review_context(self.store,target)
        self.assertEqual(context['state'],'AWAITING_REVIEW')
        self.assertEqual(context['documents']['prospectus']['sha256'],sha256(changed).hexdigest())
        newer=verification.submit_review(self.store,target,approval(context),now=NOW+timedelta(seconds=3))
        self.assertNotEqual(newer['request_id'],receipt['request_id'])
        self.assertEqual(verification.status(self.store,receipt['request_id'])['state'],'SUPERSEDED')

    def test_unrelated_parent_change_auto_rebases_but_identity_change_does_not(self):
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        request=review_fixtures.PersistedReview.enqueue(self)
        changed=deepcopy(self.map);changed['version']='stats-only';changed['products'][ETF]['stats']={'aum_yuan':'123'}
        publish_mapping(self.store,changed,{},at=NOW+timedelta(seconds=1),expected_parent=self.map['version'])
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return NOW+timedelta(minutes=1)
        with patch('modules.strategy_hermes.stage2.mapping_maintenance._rule_hash',return_value=request['dossier']['documents']['prospectus']['sha256']), \
             patch.object(verification,'datetime',Clock):
            output,summary=verification.apply_queued(self.store,changed,deepcopy(changed),{'mode':'verify'},NOW+timedelta(minutes=1),
                lambda source,scope,operation:dict(status='SAVED',receipt=operation()),lambda:None,
                clock=lambda:NOW+timedelta(minutes=2),request_ids=[request['request_id']])
        self.assertEqual(verification.status(self.store,request['request_id'])['state'],'SUPERSEDED')
        self.assertEqual(output['products'][ETF]['rule']['price_limit_ratio'],'0.2')
        bad=deepcopy(changed);bad['products'][ETF]['index_code']='000300.SH'
        with self.assertRaisesRegex(ValueError,'REBASE_PRODUCT_CHANGED'):
            verification._safe_rebase(self.store,request['request_id'],request['dossier'],bad,NOW)

    def test_budget_pause_retains_material_target_without_approving(self):
        from modules.strategy_hermes.left_right_backfill import Interrupted
        with patch.object(materials,'associated_targets',return_value=[(ETF,['kaipanla:chip'])]):
            result=materials.prepare_materials(self.store,self.map,None,NOW,None,
                        lambda:(_ for _ in ()).throw(Interrupted('REQUEST_BUDGET_EXHAUSTED')))
        self.assertEqual(result['interrupted'],'REQUEST_BUDGET_EXHAUSTED')
        self.assertEqual(result['counts']['NEEDS_MATERIALS'],1)
        self.assertFalse(self.map['products'][ETF]['rule']['verified'])


if __name__=='__main__':unittest.main()
