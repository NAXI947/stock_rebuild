"""Frozen structure_watch_v2 legacy cases, adapted to unittest without changing inputs."""
from copy import deepcopy
from datetime import date, timedelta
from math import nan
from unittest import TestCase
from modules.strategy_hermes.structure_screen import VERSION, RULES, evaluate_structure, score_gate

def sample(pattern='STARTUP'):
    days, day = ([], date(2026, 9, 1))
    while len(days) < 20:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    bars = [{'trade_date': day, 'open': 10, 'high': 10.4, 'low': 9.6, 'close': 10, 'volume_shares': 10000000, 'amount_cny': 100000000, 'adj_factor': 1.0} for day in days]
    for row in bars[-4:-1]:
        row.update(high=10.15, low=9.85, volume_shares=6000000, amount_cny=60000000)
    bars[-1].update(open=10.1, high=10.4, low=10, close=10.3)
    left_values = [68, 71, 74, 72, 73, 74]
    right_values = [68, 70, 69, 66, 68, 71]
    if pattern == 'PULLBACK':
        bars[-10]['high'] = 10.8
        left_values = [68, 69, 70, 72, 73, 72]
        right_values = [75, 73, 70, 66, 68, 71]
    scores = []
    for index, day in enumerate(days[-6:]):

        def item(score, parts, weights):
            return {'status': 'READY', 'score': score, 'quality': 0.9, 'version_id': 'version-1', 'subscores': {'parts': parts, 'weights': weights}}
        scores.append({'trade_date': day, 'left': item(left_values[index], {'left_wash': 70, 'safety': 80}, {'left_wash': 0.2, 'safety': 0.15}), 'right': item(right_values[index], {'trend': 76 + index * 2, 'slope': 50 + index * 5, 'safety': 80}, {'trend': 0.2, 'slope': 0.15, 'safety': 0.15}), 'metric_values': {'ma20': 999, 'ma60': 999, 'close_adj': 999}})
    return (scores, bars)

def evaluate(scores, bars, pattern='STARTUP', **kwargs):
    return evaluate_structure(scores, bars, pattern=pattern, **kwargs)

class FrozenStructureScreenTests(TestCase):

    def test_positive_patterns_report_auditable_metrics_without_changing_inputs(self):
        for pattern in ['STARTUP', 'PULLBACK']:
            with self.subTest(pattern=pattern):
                scores, bars = sample(pattern)
                original = deepcopy((scores, bars))
                result = evaluate(scores, bars, pattern)
                self.assertTrue(result['status'] == 'MATCH', result)
                self.assertTrue(VERSION == 'structure_watch_v2' and RULES[pattern]['version'] == VERSION)
                structure = result['structure']
                self.assertTrue(structure['pattern'] == pattern and structure['label'])
                self.assertTrue(structure['stage'] == 'CONFIRMED' and structure['stage_label'] == '条件齐备')
                self.assertTrue(structure['left_priority'] is True)
                self.assertTrue(structure['pending_confirmations'] == [])
                self.assertTrue(structure['confirmed_count'] == structure['confirmation_total'] == (4 if pattern == 'STARTUP' else 6))
                self.assertTrue(all((item['met'] is True for item in structure['confirmations'])))
                self.assertTrue(structure['right_gain_2d'] == 5)
                self.assertTrue(structure['metrics']['volume_ratio'] == 0.6)
                self.assertAlmostEqual(structure['metrics']['amplitude_ratio'], 0.375, delta=max(1e-12, abs(0.375) * 1e-06))
                self.assertAlmostEqual(structure['metrics']['ma20'], 10.015, delta=max(1e-12, abs(10.015) * 1e-06))
                self.assertAlmostEqual(structure['metrics']['ma20_distance'], 10.3 / 10.015 - 1, delta=max(1e-12, abs(10.3 / 10.015 - 1) * 1e-06))
                self.assertTrue(structure['metrics']['platform_high'] == 10.4)
                self.assertTrue(structure['metrics']['latest_trend_score'] == 86)
                self.assertTrue(structure['metrics']['latest_slope_score'] == 75)
                self.assertTrue(structure['metrics']['right_gain_1d'] == 3)
                self.assertTrue(any(('不代表上涨概率' in text for text in structure['risks'])))
                self.assertTrue(any(('支撑' in text and '失效' in text for text in structure['risks'])))
                self.assertTrue(any(('资金流' in text and '新闻' in text for text in structure['risks'])))
                self.assertTrue((scores, bars) == original)

    def test_left_fall_is_allowed_at_startup_but_never_automatically_called_washout(self):
        scores, bars = sample()
        scores[-1]['left']['score'] = 71
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'MATCH')
        self.assertTrue(any(('不能自动解释为洗盘' in text for text in result['structure']['risks'])))

    def test_shared_factor_only_right_gain_is_rejected_before_loading_bars(self):
        scores, bars = sample()
        for row in scores[-3:]:
            row['right']['subscores']['parts'].update(trend=84, slope=60)
        scores[-3]['right']['subscores']['parts']['safety'] = 60
        gate = score_gate(scores)
        self.assertTrue(gate['status'] == 'NO_MATCH')
        self.assertTrue('共享因子' in ''.join(gate['reasons']))
        self.assertTrue(evaluate(scores, bars)['status'] == 'NO_MATCH')

    def test_zero_turnover_decline_cannot_masquerade_as_contraction(self):
        for field in ['volume_shares', 'amount_cny']:
            with self.subTest(field=field):
                scores, bars = sample()
                bars[-3][field] = 0
                result = evaluate(scores, bars)
                self.assertTrue(result['status'] == 'NO_MATCH')
                self.assertTrue('零成交' in ''.join(result['reasons']))

    def test_pullback_support_break_rejects_even_if_recovery_score_passes(self):
        scores, bars = sample('PULLBACK')
        bars[-3]['low'] = 9.4
        result = evaluate(scores, bars, 'PULLBACK')
        self.assertTrue(result['status'] == 'NO_MATCH')
        self.assertTrue('支撑' in ''.join(result['reasons']))

    def test_downtrend_below_ma20_is_rejected_despite_score_improvement(self):
        scores, bars = sample()
        bars[-1].update(open=9.7, close=9.7, high=9.9, low=9.5)
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'NO_MATCH')
        self.assertTrue('辅助MA20' in ''.join(result['reasons']))

    def test_chasing_extended_price_fails_ma_distance_and_platform_gate(self):
        scores, bars = sample()
        bars[-1].update(open=11.5, close=11.5, high=11.6, low=11.3)
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'NO_MATCH')
        self.assertTrue('辅助MA20' in ''.join(result['reasons']))
        self.assertTrue('平台高点' in ''.join(result['reasons']))

    def test_contraction_windows_exclude_today_and_do_not_overlap(self):
        scores, bars = sample()
        bars[-1].update(volume_shares=90000000, amount_cny=900000000)
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'MATCH')
        self.assertTrue(result['structure']['metrics']['volume_ratio'] == 0.6)
        bars[-5]['volume_shares'] = 2000000
        result = evaluate(scores, bars)
        self.assertAlmostEqual(result['structure']['metrics']['volume_ratio'], 6 / 8.4, delta=max(1e-12, abs(6 / 8.4) * 1e-06))
        bars[-2]['volume_shares'] = 20000000
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'MATCH' and result['structure']['stage'] == 'WATCH')
        self.assertTrue(any(('均量比' in text for text in result['structure']['pending_confirmations'])))

    def test_missing_nan_date_holes_version_weights_and_quality_are_insufficient(self):
        for mutation in [lambda s, b: s.pop(), lambda s, b: s.__setitem__(2, None), lambda s, b: s[2].update(trade_date=s[1]['trade_date']), lambda s, b: s[2].update(trade_date='2026-09-01'), lambda s, b: s[-1].update(trade_date='2026-10-01'), lambda s, b: s[1]['left'].update(score=nan), lambda s, b: s[1]['right'].update(score=101), lambda s, b: s[1]['right'].update(status='MISSING'), lambda s, b: s[1]['right'].update(quality=0.59), lambda s, b: s[1]['left'].update(quality=None), lambda s, b: s[1]['right'].update(version_id='version-2'), lambda s, b: s[1]['left']['subscores']['weights'].update(left_wash=0.3), lambda s, b: s[1]['right']['subscores']['parts'].pop('slope'), lambda s, b: s[1]['left']['subscores']['parts'].update(left_wash=nan), lambda s, b: s[1]['right']['subscores']['weights'].update(trend=None), lambda s, b: b.pop(), lambda s, b: b.__setitem__(2, None), lambda s, b: b[2].update(trade_date=b[1]['trade_date']), lambda s, b: b[2].update(close=nan), lambda s, b: b[2].update(adj_factor=0), lambda s, b: b[2].update(volume_shares=None), lambda s, b: b[2].update(high=9)]:
            with self.subTest(mutation=mutation):
                scores, bars = sample()
                mutation(scores, bars)
                result = evaluate(scores, bars)
                self.assertTrue(result['status'] == 'INSUFFICIENT_DATA', result)
                self.assertTrue(result['reasons'])

    def test_recent_split_or_dividend_factor_change_is_excluded(self):
        scores, bars = sample()
        bars[-2]['adj_factor'] = 1.01
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'NO_MATCH')
        self.assertTrue('复权因子' in ''.join(result['reasons']))

    def test_old_factor_change_uses_prices_adjusted_to_current_basis(self):
        scores, bars = sample()
        for row in bars[:10]:
            row['adj_factor'] = 0.5
            for key in ('open', 'high', 'low', 'close'):
                row[key] *= 2
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'MATCH')
        self.assertAlmostEqual(result['structure']['metrics']['ma20'], 10.015, delta=max(1e-12, abs(10.015) * 1e-06))

    def test_common_gates_have_specific_reasons(self):
        for change, reason in [('trend', '趋势分'), ('safety', '安全分'), ('right_flat', '最新日上涨'), ('liquidity', '流动性')]:
            with self.subTest(change=change, reason=reason):
                scores, bars = sample()
                if change == 'trend':
                    scores[-1]['right']['subscores']['parts']['trend'] = 79
                elif change == 'safety':
                    scores[-1]['left']['subscores']['parts']['safety'] = 59
                elif change == 'right_flat':
                    scores[-1]['right']['score'] = scores[-2]['right']['score']
                elif change == 'liquidity':
                    for row in bars[:11]:
                        row['amount_cny'] = 19000000
                result = evaluate(scores, bars)
                self.assertTrue(result['status'] == 'NO_MATCH')
                self.assertTrue(reason in ''.join(result['reasons']))

    def test_left_reference_changes_confirmation_not_pool_membership_while_other_thresholds_are_hard(self):
        scores, bars = sample()
        reference74 = evaluate(scores, bars, min_left=74)
        reference75 = evaluate(scores, bars, min_left=75)
        self.assertTrue(reference74['status'] == reference75['status'] == 'MATCH')
        self.assertTrue(reference74['structure']['stage'] == 'CONFIRMED')
        self.assertTrue(reference75['structure']['stage'] == 'WATCH')
        self.assertTrue(reference74['structure']['left_priority'] is True)
        self.assertTrue(reference75['structure']['left_priority'] is False)
        self.assertTrue(score_gate(scores, min_left=90)['status'] == 'MATCH')
        self.assertTrue(evaluate(scores, bars, min_right_gain=5)['status'] == 'MATCH')
        self.assertTrue(evaluate(scores, bars, min_right_gain=6)['status'] == 'NO_MATCH')
        bars[-1].update(close=10.5, high=10.6)
        self.assertTrue(evaluate(scores, bars, max_ma20_distance=0.03)['status'] == 'NO_MATCH')
        self.assertTrue(evaluate(scores, bars, max_ma20_distance=0.1)['status'] == 'MATCH')

    def test_out_of_bounds_parameters_rejected(self):
        for kwargs in [{'min_left': 49}, {'min_left': 91}, {'min_left': nan}, {'min_left': True}, {'min_right_gain': 0.5}, {'min_right_gain': 16}, {'max_ma20_distance': 0.02}, {'max_ma20_distance': 0.21}, {'max_ma20_distance': nan}, {'max_ma20_distance': None}]:
            with self.subTest(kwargs=kwargs):
                scores, bars = sample()
                with self.assertRaises(ValueError):
                    evaluate(scores, bars, **kwargs)

    def test_unknown_pattern_rejected(self):
        scores, bars = sample()
        with self.assertRaises(ValueError):
            evaluate(scores, bars, 'OTHER')

    def test_pullback_prior_right_strength_and_left_recovery_are_soft_confirmations(self):
        scores, bars = sample('PULLBACK')
        for row in scores[:3]:
            row['right']['score'] = 68
        result = evaluate(scores, bars, 'PULLBACK')
        self.assertTrue(result['status'] == 'MATCH' and result['structure']['stage'] == 'WATCH')
        self.assertTrue(any(('此前强势后回落' in text for text in result['structure']['pending_confirmations'])))
        scores, bars = sample('PULLBACK')
        for row in scores[:3]:
            row['left']['score'] = 72
        result = evaluate(scores, bars, 'PULLBACK')
        self.assertTrue(result['status'] == 'MATCH' and result['structure']['stage'] == 'WATCH')
        self.assertTrue(any(('LEFT回调期间恢复' in text for text in result['structure']['pending_confirmations'])))

    def test_startup_wide_platform_and_pullback_depth_have_separate_gates(self):
        scores, bars = sample()
        bars[-6]['high'] = 11
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'NO_MATCH')
        self.assertTrue('平台区间' in ''.join(result['reasons']))
        scores, bars = sample('PULLBACK')
        bars[-10]['high'] = 11.5
        result = evaluate(scores, bars, 'PULLBACK')
        self.assertTrue(result['status'] == 'NO_MATCH')
        self.assertTrue('回撤' in ''.join(result['reasons']))

    def test_today_liquidity_is_independent_of_twenty_day_median(self):
        scores, bars = sample()
        bars[-1]['amount_cny'] = 29999999
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'NO_MATCH')
        self.assertTrue('流动性' in ''.join(result['reasons']))
        bars[-1]['amount_cny'] = 30000000
        self.assertTrue(evaluate(scores, bars)['status'] == 'MATCH')

    def test_separate_family_versions_are_allowed_but_each_must_be_stable(self):
        scores, bars = sample()
        for row in scores:
            row['right']['version_id'] = 'right-model-version'
        self.assertTrue(evaluate(scores, bars)['status'] == 'MATCH')

    def test_numeric_overflow_cannot_be_reported_as_matching_metrics(self):
        scores, bars = sample()
        for row in bars:
            row['amount_cny'] = 1e+308
        self.assertTrue(evaluate(scores, bars)['status'] == 'INSUFFICIENT_DATA')

    def test_low_left_and_no_historical_activity_enter_watch_without_false_positive_reasons(self):
        for pattern in ['STARTUP', 'PULLBACK']:
            with self.subTest(pattern=pattern):
                scores, bars = sample(pattern)
                for row in scores:
                    row['left']['score'] = 45
                    row['left']['subscores']['parts']['left_wash'] = 0
                result = evaluate(scores, bars, pattern)
                self.assertTrue(result['status'] == 'MATCH')
                structure = result['structure']
                self.assertTrue(structure['stage'] == 'WATCH' and structure['stage_label'] == '观察中')
                self.assertTrue(structure['left_priority'] is False)
                by_key = {item['key']: item for item in structure['confirmations']}
                self.assertTrue(by_key['left_reference']['met'] is False)
                self.assertTrue(by_key['left_activity']['met'] is False)
                self.assertTrue(any(('LEFT优先参考' in text for text in structure['pending_confirmations'])))
                self.assertTrue(any(('历史活跃依据' in text for text in structure['pending_confirmations'])))
                self.assertTrue(all((item['detail'] not in result['reasons'] for item in structure['confirmations'] if not item['met'])))
                self.assertTrue(all((item['detail'] in result['reasons'] for item in structure['confirmations'] if item['met'])))

    def test_right_may_fall_on_middle_day_but_latest_rise_and_two_day_net_gain_remain_hard(self):
        scores, bars = sample()
        scores[-3]['right']['score'] = 68
        scores[-2]['right']['score'] = 65
        scores[-1]['right']['score'] = 71
        gate = score_gate(scores)
        self.assertTrue(gate['status'] == 'MATCH')
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'MATCH')
        self.assertTrue(result['structure']['right_gain_2d'] == 3)
        self.assertTrue(result['structure']['metrics']['right_gain_1d'] == 6)
        scores[-2]['right']['score'] = 72
        self.assertTrue(score_gate(scores)['status'] == 'NO_MATCH')
        scores[-2]['right']['score'] = 65
        scores[-1]['right']['score'] = 70
        self.assertTrue(score_gate(scores)['status'] == 'NO_MATCH')

    def test_no_contraction_is_pending_evidence_not_an_exclusion_or_a_false_claim(self):
        scores, bars = sample()
        for row in bars[-4:-1]:
            row.update(high=10.4, low=9.6, volume_shares=12000000)
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'MATCH')
        structure = result['structure']
        self.assertTrue(structure['stage'] == 'WATCH')
        self.assertTrue(structure['confirmed_count'] == 2 and structure['confirmation_total'] == 4)
        self.assertTrue({item['key'] for item in structure['confirmations'] if not item['met']} == {'volume_contraction', 'amplitude_contraction'})
        self.assertTrue(len(structure['pending_confirmations']) == 2)
        self.assertTrue(not any(('均量比' in text or '平均振幅比' in text for text in structure['reasons'])))

    def test_zero_amplitude_baseline_retains_null_ratio_and_pending_confirmation(self):
        scores, bars = sample()
        for row in bars[-9:-4]:
            row.update(open=10, close=10, high=10, low=10)
        result = evaluate(scores, bars)
        self.assertTrue(result['status'] == 'MATCH')
        structure = result['structure']
        self.assertTrue(structure['stage'] == 'WATCH')
        self.assertTrue(structure['metrics']['amplitude_ratio'] is None)
        confirmation = next((item for item in structure['confirmations'] if item['key'] == 'amplitude_contraction'))
        self.assertTrue(confirmation['met'] is False)
        self.assertTrue('基准为零' in confirmation['detail'])
        self.assertTrue(any(('基准为零' in text for text in structure['pending_confirmations'])))

    def test_pullback_needs_a_rising_latest_close_even_when_all_soft_checks_pass(self):
        scores, bars = sample('PULLBACK')
        bars[-1].update(open=10, high=10.1, low=9.95, close=10)
        result = evaluate(scores, bars, 'PULLBACK')
        self.assertTrue(result['status'] == 'NO_MATCH')
        self.assertTrue(any(('最新收盘未较前日上涨' in text for text in result['reasons'])))

    def test_slope_below_or_at_neutral_reference_warns_without_adding_a_hidden_gate(self):
        for latest_slope in [20, 100 / 3]:
            with self.subTest(latest_slope=latest_slope):
                scores, bars = sample()
                scores[-3]['right']['subscores']['parts']['slope'] = 10
                scores[-2]['right']['subscores']['parts']['slope'] = 15
                scores[-1]['right']['subscores']['parts']['slope'] = latest_slope
                result = evaluate(scores, bars)
                self.assertTrue(result['status'] == 'MATCH')
                self.assertTrue(result['structure']['stage'] == 'CONFIRMED')
                self.assertAlmostEqual(result['structure']['metrics']['latest_slope_score'], latest_slope, delta=max(1e-12, abs(latest_slope) * 1e-06))
                self.assertTrue(any(('均线斜率因子仍未高于中性参考' in text for text in result['structure']['risks'])))

    def test_each_parameterized_pattern_keeps_membership_when_only_left_reference_changes(self):
        for pattern in RULES:
            scores, bars = sample(pattern)
            results = [evaluate(scores, bars, pattern, min_left=value) for value in (50, 70, 90)]
            self.assertTrue(all((result['status'] == 'MATCH' for result in results)))
            self.assertTrue(results[0]['structure']['left_priority'] is True)
            self.assertTrue(results[-1]['structure']['left_priority'] is False)
            self.assertTrue(results[-1]['structure']['stage'] == 'WATCH')
            for result in results:
                structure = result['structure']
                self.assertTrue(structure['confirmed_count'] == sum((item['met'] for item in structure['confirmations'])))
                self.assertTrue(len(structure['pending_confirmations']) + structure['confirmed_count'] == structure['confirmation_total'])
