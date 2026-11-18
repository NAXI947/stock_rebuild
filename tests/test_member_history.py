"""Historical member recovery preserves provenance and exposes real missing data."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
import os
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import mapping_maintenance as maintenance
from modules.strategy_hermes.stage2.member_history import (
    MAX_HISTORY_DATES, MAX_MEMBER_PAGES, assemble_members, cached_members, usable_members,
)

NOW = datetime(2026, 11, 16, 18, tzinfo=CHINA)
SID = 'kaipanla:801004'
IDENTITIES = {'600000': ['600000.SH'], '600001': ['600001.SH'], '600002': ['600002.SH']}
IDENTITY_AT = NOW - timedelta(hours=2)


def page(day='2026-11-12', codes=('600000',), total=None, offset=0, at=None, batch=None):
    return (batch or f'{day}:{offset}', {'Index': str(offset), 'Date': day, 'PlateID': '801004'},
            dict(rows=[[code, '成员'] for code in codes], reported_total=len(codes) if total is None else total,
                 trade_days=[day]), at or NOW - timedelta(days=1))


def reference(day='2026-11-13', codes=('600000.SH',)):
    return dict(members=list(codes), sector_name='锂电池', as_of_date=day,
                reported_count=len(codes), unresolved_count=0, source_batches=['published-source'],
                available_at=(NOW-timedelta(days=1)).isoformat())


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


class Store:
    def __init__(self, pages=(), identity_at=IDENTITY_AT):
        self.pages, self.identity_at, self.queries = list(pages), identity_at, []
    def _connect(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, args):
        self.queries.append((sql, args))
        if sql.startswith('SELECT collected_at FROM supplier_batches'):
            return Cursor([(self.identity_at,)] if self.identity_at and self.identity_at <= args[1] else [])
        if 'SELECT DISTINCT scope' in sql:
            plate, day, at, limit = args
            days = sorted({r[1]['Date'] for r in self.pages
                           if r[1]['PlateID'] == plate and r[1]['Date'] < day and r[3] <= at}, reverse=True)
            return Cursor([(day,) for day in days[:limit]])
        if 'SELECT DISTINCT ON' in sql:
            day, plate, at, limit = args
            latest = {}
            for row in sorted(self.pages, key=lambda r: (r[3],r[0])):
                if row[1]['Date'] == day and row[1]['PlateID'] == plate and row[3] <= at:
                    latest[row[1]['Index']] = row
            return Cursor([latest[k] for k in sorted(latest)][:limit])
        raise AssertionError(sql)


class MemberRecovery(TestCase):
    def run_members(self, pages=(), old=None, *, request=None, identities=IDENTITIES,
                    identity_at=IDENTITY_AT, checkpoint=lambda: None, captures='configured', review=False):
        self.store = Store(pages, identity_at)
        self.mapping = dict(sectors={SID: deepcopy(old)} if old is not None else {}, version='original',
                            products={}, weights={})
        self.summary, self.calls = {}, []
        def counted(source, scope, operation):
            self.calls.append((source, scope))
            return request(source, scope, operation) if request else dict(status='FAILED')
        def targets(*args, scope=None, policy=None):
            if scope is not None: scope.update(candidate_count=1, selected_count=1, deferred_count=0)
            return [SID], {SID: {'sector_name': '锂电池'}}
        with patch.object(maintenance, 'sector_targets', side_effect=targets):
            maintenance._members(self.store, self.mapping, None, identities, 'identity', [], NOW.date(),
                                 captures, NOW, counted, checkpoint, self.summary, review=review)
        return self.summary['members'][SID]

    def test_missing_new_sector_recovers_cached_complete_members_without_http_fallback(self):
        state = self.run_members([page()])
        saved = self.mapping['sectors'][SID]
        self.assertEqual((state['status'], state['source'], state['usable']), ('RETAINED','history_cache',True))
        self.assertEqual(saved['as_of_date'], '2026-11-12')
        self.assertEqual(saved['available_at'], IDENTITY_AT.isoformat())
        self.assertEqual(saved['source_batches'], ['2026-11-12:0'])
        self.assertTrue(usable_members(saved, NOW))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]['day'], '2026-11-16')

    def test_absent_source_is_pending_and_has_no_fake_retained_members(self):
        state = self.run_members()
        self.assertEqual((state['status'],state['usable'],state['known']), ('PENDING',False,0))
        self.assertIsNone(state['expected_count'])
        self.assertIsNone(state['missing'])
        self.assertNotIn(SID,self.mapping['sectors'])

    def test_invalid_old_mapping_cannot_be_reported_retained(self):
        for changes in (dict(unresolved_count=1), dict(reported_count=2), dict(source_batches=[]),
                        dict(available_at=(NOW+timedelta(hours=1)).isoformat())):
            with self.subTest(changes=changes):
                self.assertEqual(self.run_members(old={**reference(),**changes})['status'], 'PENDING')

    def test_newer_usable_old_mapping_is_preserved_over_older_cache(self):
        old=reference()
        state=self.run_members([page()],old)
        self.assertEqual(self.mapping['sectors'][SID],old)
        self.assertEqual((state['status'],state['source']),('RETAINED','existing_mapping'))

    def test_newer_complete_cache_replaces_older_published_list(self):
        state=self.run_members([page('2026-11-13',('600000','600001'))],reference('2026-11-12'))
        self.assertEqual(self.mapping['sectors'][SID]['members'],['600000.SH','600001.SH'])
        self.assertEqual(state['source'],'history_cache')

    def test_partial_latest_and_bad_pages_fall_back_to_latest_complete_date(self):
        pages=[page('2026-11-13',('600000',),total=2), page('2026-11-12'),
               page('2026-11-15',('600000','600000'),total=2)]
        state=self.run_members(pages)
        self.assertEqual((state['status'],state['as_of_date']),('RETAINED','2026-11-12'))

    def test_new_first_page_and_old_tail_are_rejected_in_favor_of_older_complete_date(self):
        mixed=[page('2026-11-13',('600001',),total=2,at=IDENTITY_AT),
               page('2026-11-13',('600002',),total=2,offset=1,
                    at=IDENTITY_AT-timedelta(minutes=1))]
        with self.assertRaisesRegex(ValueError,'timestamps move backwards'):
            assemble_members(mixed,'2026-11-13',IDENTITIES,'identity',IDENTITY_AT,'锂电池')
        state=self.run_members([*mixed,page()])
        self.assertEqual((state['status'],state['as_of_date']),('RETAINED','2026-11-12'))
        self.assertEqual(self.mapping['sectors'][SID]['source_batches'],['2026-11-12:0'])

    def test_unresolved_historical_identity_is_not_usable(self):
        state=self.run_members([page(codes=('600000','999999'))])
        self.assertEqual(state['status'],'PENDING')
        self.assertNotIn(SID,self.mapping['sectors'])

    def test_unavailable_identity_still_retains_valid_existing_reference(self):
        state=self.run_members(old=reference(),identities={},identity_at=None)
        self.assertEqual(state['status'],'RETAINED')
        self.assertEqual(state['error'],'IDENTITIES_UNAVAILABLE')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.run_members(identities={},identity_at=None)['status'],'PENDING')

    def test_missing_capture_still_recovers_local_history(self):
        state=self.run_members([page()],captures=None)
        self.assertEqual(state['source'],'history_cache')
        self.assertEqual(self.calls,[])

    def test_new_target_day_members_replace_the_old_population(self):
        current=page('2026-11-16',('600000','600001'),at=NOW-timedelta(minutes=5))
        state=self.run_members([current],reference())
        self.assertEqual(state['status'],'UPDATED')
        self.assertEqual(self.mapping['sectors'][SID]['members'],['600000.SH','600001.SH'])
        self.assertEqual(self.mapping['sectors'][SID]['as_of_date'],'2026-11-16')
        self.assertEqual(self.calls,[])

    def test_complete_same_day_reference_reuses_cache_outside_review(self):
        old=reference('2026-11-16')
        state=self.run_members([page('2026-11-16')],old)
        self.assertEqual(state['status'],'CURRENT')
        self.assertEqual(self.mapping['sectors'][SID],old)
        self.assertEqual(self.calls,[])

    def test_review_fetches_same_day_revision_despite_current_complete_cached_list(self):
        old=reference('2026-11-16')
        body=page('2026-11-16',('600000','600001'))[2]
        received=NOW+timedelta(seconds=2)
        with patch.object(maintenance,'_read_market',return_value=(body,received)):
            state=self.run_members([page('2026-11-16')],old,review=True,
                request=lambda *args:dict(status='SAVED',receipt={'batch':'review-page'}))
        self.assertEqual(state['status'],'UPDATED')
        saved=self.mapping['sectors'][SID]
        self.assertEqual(saved['members'],['600000.SH','600001.SH'])
        self.assertEqual(saved['source_batches'],['review-page'])
        self.assertEqual(saved['available_at'],received.isoformat())
        self.assertEqual(self.calls,[('maintenance:members',dict(day='2026-11-16',sector_id=SID,offset=0,review=True))])
        self.assertFalse(any('SELECT DISTINCT ON' in sql and args[0]=='2026-11-16' for sql,args in self.store.queries))

    def test_review_source_failure_retains_verified_same_day_list_with_explicit_reason(self):
        old=reference('2026-11-16')
        state=self.run_members([page('2026-11-16')],old,review=True)
        self.assertEqual(state['status'],'RETAINED')
        self.assertEqual(state['reason'],'REVIEW_PENDING')
        self.assertEqual(state['error'],'SourceFailure')
        self.assertTrue(state['usable'])
        self.assertEqual(self.mapping['sectors'][SID],old)
        self.assertEqual(len(self.calls),1)

    def test_review_recovers_complete_same_day_cache_before_fresh_source_failure(self):
        received=NOW-timedelta(minutes=3)
        state=self.run_members([page('2026-11-16',('600000','600001'),at=received)],review=True)
        self.assertEqual((state['status'],state['source'],state['usable']),
                         ('RETAINED','target_cache',True))
        self.assertEqual(state['reason'],'REVIEW_PENDING')
        self.assertEqual(state['error'],'SourceFailure')
        self.assertEqual(self.mapping['sectors'][SID]['members'],['600000.SH','600001.SH'])
        self.assertEqual(self.mapping['sectors'][SID]['as_of_date'],'2026-11-16')
        self.assertEqual(self.mapping['sectors'][SID]['available_at'],received.isoformat())
        self.assertEqual(self.calls,[('maintenance:members',dict(day='2026-11-16',sector_id=SID,offset=0,review=True))])

    def test_review_partial_revision_preserves_complete_list_and_true_target_gap(self):
        old=reference('2026-11-16')
        body=page('2026-11-16',('600000','999999'))[2]
        with patch.object(maintenance,'_read_market',return_value=(body,NOW)):
            state=self.run_members(old=old,review=True,
                request=lambda *args:dict(status='SAVED',receipt={'batch':'partial-review'}))
        self.assertEqual(state['status'],'RETAINED')
        self.assertEqual(state['error'],'INCOMPLETE_TARGET_MEMBERS')
        self.assertEqual((state['target_known'],state['target_expected_count'],state['target_missing']),(1,2,1))
        self.assertEqual(self.mapping['sectors'][SID],old)

    def test_fresh_page_retains_actual_received_time_after_task_started(self):
        received=NOW+timedelta(seconds=2)
        body=page('2026-11-16',('600000','600001'))[2]
        with patch.object(maintenance,'_read_market',return_value=(body,received)):
            state=self.run_members(old=reference(),request=lambda *args:dict(status='SAVED',receipt={'batch':'fresh'}))
        self.assertEqual(state['status'],'UPDATED')
        self.assertEqual(self.mapping['sectors'][SID]['available_at'],received.isoformat())

    def test_missing_new_page_preserves_the_correct_target_denominator(self):
        state=self.run_members([page('2026-11-16',('600000',),total=3)])
        self.assertEqual((state['status'],state['known'],state['expected_count'],state['missing']),
                         ('PENDING',1,3,2))
        self.assertNotIn(SID,self.mapping['sectors'])

    def test_partial_new_list_does_not_downgrade_complete_old_list(self):
        old=reference()
        state=self.run_members([page('2026-11-16',('600000','999999'))],old)
        self.assertEqual(state['status'],'RETAINED')
        self.assertEqual((state['target_known'],state['target_expected_count'],state['target_missing']),(1,2,1))
        self.assertEqual(self.mapping['sectors'][SID],old)

    def test_interrupted_http_preserves_completed_local_recovery(self):
        def exhausted(*args): raise maintenance.Interrupted('REQUEST_BUDGET_EXHAUSTED')
        with self.assertRaisesRegex(maintenance.Interrupted,'REQUEST_BUDGET_EXHAUSTED'):
            self.run_members([page()],request=exhausted)
        self.assertEqual(self.summary['members'][SID]['source'],'history_cache')
        self.assertTrue(usable_members(self.mapping['sectors'][SID],NOW))
        self.assertEqual(len(self.calls),1)

    def test_stop_checkpoint_prevents_new_attempts(self):
        def stopped(): raise maintenance.Interrupted('CANCELLED')
        with self.assertRaisesRegex(maintenance.Interrupted,'CANCELLED'):
            self.run_members([page()],checkpoint=stopped)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.mapping['sectors'],{})

    def test_history_is_bounded_and_never_reads_future_sources(self):
        rows=[page('2026-11-13',('600000','600001'),at=NOW+timedelta(seconds=1)),page()]
        state=self.run_members(rows)
        self.assertEqual(state['as_of_date'],'2026-11-12')
        date_queries=[args for sql,args in self.store.queries if 'SELECT DISTINCT scope' in sql]
        page_queries=[args for sql,args in self.store.queries if 'SELECT DISTINCT ON' in sql]
        self.assertTrue(date_queries)
        self.assertTrue(all(args[-1]==MAX_HISTORY_DATES for args in date_queries))
        self.assertTrue(all(args[-1]==MAX_MEMBER_PAGES+1 for args in page_queries))

    def test_overlapping_missing_and_oversized_pages_are_rejected(self):
        for rows in ([page(total=2),page(codes=('600000',),total=2,offset=1)],
                     [page(total=2),page(codes=('600001',),total=2,offset=2)],
                     [page(codes=('600000','600001'),total=1)],
                     [page(offset=i,total=9) for i in range(9)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                assemble_members(rows,'2026-11-12',IDENTITIES,'identity',IDENTITY_AT,'锂电池')


@skipUnless(os.environ.get('STAGE2_TEST_ENV_FILE'), 'requires isolated PostgreSQL')
class MemberRecoveryPostgres(TestCase):
    def setUp(self):
        from modules.strategy_hermes.ledger import encode
        from modules.strategy_hermes.stage2.postgres import PostgresLedger
        from modules.strategy_hermes.stage2.settings import Settings
        self.encode=encode
        settings=Settings.read(os.environ['STAGE2_TEST_ENV_FILE'])
        self.assertEqual(settings.env,'test')
        unique=uuid4().hex
        self.store=PostgresLedger(replace(settings,namespace=settings.redis_prefix+'members:'+unique),'FROZEN')
        self.store.migrate()
        self.sid='kaipanla:membertest-'+unique
        self.identity='member-identity-'+unique
        self.batches=[]
        def cleanup():
            with self.store._connect() as conn:
                conn.execute('DELETE FROM supplier_market_batches WHERE batch_id=ANY(%s)',(self.batches,))
                conn.execute('DELETE FROM supplier_batches WHERE batch_id=%s',(self.identity,))
        self.addCleanup(cleanup)
        rows=[dict(ts_code=codes[0],symbol=symbol,list_status='L') for symbol,codes in IDENTITIES.items()]
        with self.store._connect() as conn:
            conn.execute('INSERT INTO supplier_batches VALUES (%s,%s,%s,%s,%s,%s,%s)',
                         (self.identity,'stock_basic',encode({'fixture':unique}),IDENTITY_AT,len(rows),encode(rows),encode(rows)))

    def save(self, day, codes, total, offset=0, at=None):
        batch='member-page-'+uuid4().hex
        self.batches.append(batch)
        body=dict(rows=[[code,'成员'] for code in codes],reported_total=total,trade_days=[day])
        scope=dict(Date=day,PlateID=self.sid.split(':')[1],Index=str(offset))
        with self.store._connect() as conn:
            conn.execute('INSERT INTO supplier_market_batches VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                         (batch,'kaipanla','046',self.encode(scope),at or IDENTITY_AT-timedelta(hours=1),
                          None,len(codes),self.encode(body)))
        return batch

    def test_real_sql_uses_latest_visible_offset_revision_and_restores_new_mapping(self):
        from modules.strategy_hermes.stage2.build_sources import stock_identity
        self.save('2026-11-13',['600000'],2)
        newest=self.save('2026-11-13',['600001'],2,at=IDENTITY_AT-timedelta(minutes=1))
        second=self.save('2026-11-13',['600002'],2,offset=1,at=IDENTITY_AT)
        self.save('2026-11-13',['600000'],2,at=NOW+timedelta(hours=1))
        with self.store._connect() as conn:
            identities,_=stock_identity(conn,self.identity)
        mapping=dict(version='fixture',sectors={},products={},weights={})
        summary={}
        with patch.object(maintenance,'sector_targets',return_value=([self.sid],{self.sid:{'sector_name':'测试板块'}})):
            maintenance._members(self.store,mapping,None,identities,self.identity,[],NOW.date(),None,NOW,
                                 lambda *a: self.fail('no external call allowed'),lambda:None,summary)
        saved=mapping['sectors'][self.sid]
        self.assertEqual(saved['members'],['600001.SH','600002.SH'])
        self.assertEqual(saved['source_batches'],[newest,second])
        self.assertEqual(saved['available_at'],IDENTITY_AT.isoformat())
        self.assertEqual(summary['members'][self.sid]['source'],'history_cache')
        self.assertTrue(summary['members'][self.sid]['usable'])

    def test_real_sql_rejects_latest_first_page_mixed_with_old_tail(self):
        fallback=self.save('2026-11-12',['600000'],1)
        self.save('2026-11-13',['600000'],2)
        self.save('2026-11-13',['600002'],2,offset=1,at=IDENTITY_AT-timedelta(minutes=30))
        self.save('2026-11-13',['600001'],2,at=IDENTITY_AT)
        result=cached_members(self.store,self.sid,NOW.date(),NOW,IDENTITIES,
                              self.identity,IDENTITY_AT,'测试板块',lambda:None)
        self.assertEqual(result['as_of_date'],'2026-11-12')
        self.assertEqual(result['source_batches'],[fallback])
        self.assertEqual(result['members'],['600000.SH'])
        self.assertEqual(result['available_at'],IDENTITY_AT.isoformat())

    def test_real_sql_limits_history_dates_and_advances_when_complete_new_page_arrives(self):
        self.save('2026-11-01',['600000'],1)
        for day in range(2,10):
            self.save(f'2026-11-{day:02d}',['600000'],2)
        self.assertIsNone(cached_members(self.store,self.sid,NOW.date(),NOW,IDENTITIES,
                                        self.identity,IDENTITY_AT,'测试板块',lambda:None))
        batch=self.save('2026-11-09',['600000','600001'],2,at=IDENTITY_AT)
        result=cached_members(self.store,self.sid,NOW.date(),NOW,IDENTITIES,
                              self.identity,IDENTITY_AT,'测试板块',lambda:None)
        self.assertEqual(result['as_of_date'],'2026-11-09')
        self.assertEqual(result['source_batches'],[batch])
        self.assertEqual(result['members'],['600000.SH','600001.SH'])
