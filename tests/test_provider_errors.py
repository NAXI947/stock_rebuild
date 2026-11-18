"""Safe diagnostics and completed-date adapters; no external provider calls."""
from datetime import datetime
import json
from unittest import TestCase
from unittest.mock import patch
from urllib.request import Request

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import market_bootstrap as market
from modules.strategy_hermes.stage2 import membership_refresh as members
from modules.strategy_hermes.stage2 import tushare_bootstrap as tushare
from modules.strategy_hermes.stage2.http_json import ProviderHTTPError
from modules.strategy_hermes.stage2.provider_errors import error_diagnostic
from test_provider_http import server


NOW = datetime(2026, 11, 17, 10, tzinfo=CHINA)
DAY = '2026-11-16'
SCOPE = dict(Date=DAY, PlateID='801027', Index='0', st='1000')
RAW = dict(errcode='0', Day=[DAY], Count=1, list=[['600000', '浦发银行']])


class SafeProviderDiagnostics(TestCase):
    def test_unknown_messages_and_mutated_details_never_enter_receipts(self):
        secret = 'https://private.test/?token=DO_NOT_LOG'
        exc = market.ProbeError(secret, kind='HTTP_STATUS', details={
            'http_status': 403, 'url': secret, 'provider_code': secret,
            'expected_date': secret, 'returned_dates': [secret], 'row_count': True})
        exc.details['token'] = secret
        self.assertEqual(error_diagnostic(exc), dict(error_code='ProbeError',
            error_kind='HTTP_STATUS', error_details={'http_status': 403}))
        self.assertEqual(error_diagnostic(ValueError(secret)), {'error_code': 'ValueError'})
        restored = tushare.SourceFailure.from_receipt(dict(error_kind=[secret], error_details={'url': secret}))
        self.assertEqual(error_diagnostic(restored), dict(error_code='SourceFailure', error_kind='SOURCE_FAILURE'))

    def test_transport_diagnostics_survive_both_adapter_wrappers(self):
        failure = ProviderHTTPError('unsafe URL token=hidden', kind='HTTP_STATUS', details={'http_status': 429})
        with patch.object(market, 'get_json', side_effect=failure) as fetch:
            with self.assertRaises(market.ProbeError) as caught:
                market.fetch(Request('https://provider.test/data?Token=hidden'))
        self.assertEqual(fetch.call_count, 1)
        diagnostic = error_diagnostic(caught.exception)
        self.assertEqual(diagnostic, dict(error_code='ProbeError', error_kind='HTTP_STATUS',
                                         error_details={'http_status': 429}))
        self.assertNotIn('hidden', str(caught.exception))
        client = self.client()
        with patch.object(tushare, 'post_json', side_effect=failure):
            with self.assertRaises(tushare.SourceFailure) as caught:
                client.fetch('etf_share_size', {'trade_date': '20261116'})
        receipt = error_diagnostic(caught.exception)
        self.assertEqual(receipt['error_details'], {'http_status': 429})
        self.assertEqual(error_diagnostic(tushare.SourceFailure.from_receipt(receipt)), receipt)

    @staticmethod
    def client():
        client = object.__new__(tushare.Tushare)
        client.config = dict(token='fixture-secret', base_url='https://provider.test')
        client.used = set()
        return client

    def test_tushare_empty_status_fields_and_pagination_are_distinct(self):
        fields = list(tushare.FIELDS['etf_share_size'])
        cases = [
            (dict(code=0, data=dict(fields=fields, items=[])), 'NOT_PUBLISHED', {}),
            (dict(code=-2002, msg='token=secret'), 'PROVIDER_STATUS', {'provider_code': -2002}),
            (dict(code=0, data=dict(fields=['unknown'], items=[])), 'FIELD_MISMATCH', {}),
            (dict(code=0, data=dict(fields=fields, items=[[0]*5]*5001)), 'PAGE_MISMATCH',
             {'row_count': 5001, 'page_size': 5000}),
        ]
        for response, kind, details in cases:
            with self.subTest(kind=kind), patch.object(tushare, 'post_json', return_value=response):
                with self.assertRaises(tushare.SourceFailure) as caught:
                    self.client().fetch('etf_share_size', {'trade_date': '20261116'})
                receipt = error_diagnostic(caught.exception)
                self.assertEqual(receipt['error_kind'], kind)
                self.assertEqual(receipt.get('error_details', {}), details)
                self.assertNotIn('secret', json.dumps(receipt))

    def test_kaipanla_empty_status_schema_and_dates_are_distinct(self):
        cases = [
            (RAW | {'list': []}, 'NOT_PUBLISHED', {}),
            (RAW | {'errcode': '-1', 'message': 'token=secret'}, 'PROVIDER_STATUS', {'provider_code': -1}),
            (RAW | {'list': {}}, 'FIELD_MISMATCH', {}),
            (RAW | {'Day': ['2026-11-13']}, 'DATE_MISMATCH',
             {'expected_date': DAY, 'returned_dates': ['2026-11-13']}),
            (RAW | {'list': [['600000', 'x'], ['600000', 'x']]}, 'INVALID_IDENTITY', {}),
        ]
        for response, kind, details in cases:
            with self.subTest(kind=kind), self.assertRaises(market.ProbeError) as caught:
                market.normalize_kaipanla(response, '046', SCOPE, NOW)
            receipt = error_diagnostic(caught.exception)
            self.assertEqual(receipt['error_kind'], kind)
            self.assertEqual(receipt.get('error_details', {}), details)

    def test_open_session_repair_uses_completed_day_and_never_collects_today(self):
        class Clock:
            now = staticmethod(lambda *_: NOW)
        with patch.object(members, 'datetime', Clock), patch.object(members, 'capture_request') as capture, \
             patch.object(members, 'fetch', return_value=RAW) as fetch, patch.object(members, 'save', return_value={}) as save:
            capture.return_value = (Request('https://provider.test'), SCOPE)
            result = members._page(None, None, 'kaipanla:801027', DAY, 0)
            self.assertEqual(result['count'], 1)
            self.assertEqual(capture.call_args.args[2]['Date'], DAY)
            for day in ('2026-11-17', '2026-11-18'):
                with self.assertRaises(market.ProbeError) as caught:
                    members._page(None, None, 'kaipanla:801027', day, 0)
                self.assertEqual(caught.exception.kind, 'DATE_NOT_COMPLETED')
            self.assertEqual((capture.call_count, fetch.call_count, save.call_count), (1, 1, 1))

    def test_incomplete_page_is_not_saved_and_keeps_numeric_diagnostics(self):
        class Clock:
            now = staticmethod(lambda *_: NOW)
        with patch.object(members, 'datetime', Clock), \
             patch.object(members, 'capture_request', return_value=(Request('https://provider.test'), SCOPE)), \
             patch.object(members, 'fetch', return_value=RAW | {'Count': 0}), patch.object(members, 'save') as save:
            with self.assertRaises(market.ProbeError) as caught:
                members._page(None, None, 'kaipanla:801027', DAY, 0)
            self.assertEqual(error_diagnostic(caught.exception), dict(error_code='ProbeError', error_kind='PAGE_MISMATCH',
                error_details=dict(offset=0, row_count=1, reported_total=0, page_size=1000)))
            save.assert_not_called()

    def test_shared_fetch_preserves_get_post_auth_body_and_no_redirects(self):
        from http.client import HTTPConnection
        with server() as (site, url), patch('http.client.HTTPSConnection', HTTPConnection):
            secure = url.replace('http://', 'https://')
            payload = {'universes': ['CN_ETF'], 'label': '中文'}
            response = market.fetch(Request(secure + '/ok', data=json.dumps(payload).encode(),
                                    headers={'Authorization': 'Bearer fixture', 'x-api-key': 'fixture'}))
            self.assertEqual(response['received'], payload)
            self.assertEqual(response['api_key'], 'fixture')
            self.assertEqual(site.requests[0][1]['Authorization'], 'Bearer fixture')
            market.fetch(Request(secure + '/ok?Date=2026-11-16'))
            with self.assertRaises(market.ProbeError) as caught:
                market.fetch(Request(secure + '/redirect/302'))
            self.assertEqual(caught.exception.kind, 'HTTP_STATUS')
            self.assertEqual(caught.exception.details, {'http_status': 302})
            self.assertEqual(len(site.requests), 3)
            self.assertEqual(site.requests[1][0], '/ok?Date=2026-11-16')
