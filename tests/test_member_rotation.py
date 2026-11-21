"""Bounded maintenance rotates failed/new boards and reports missing lists truthfully."""
from copy import deepcopy
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import mapping_maintenance as maintenance
from modules.strategy_hermes.qq_message_fields import maintenance_fields
from modules.strategy_hermes.etf_maintenance import feedback
from modules.strategy_hermes.qq_templates import render_message


NOW = datetime(2026, 11, 16, 18, tzinfo=CHINA)


def mapping():
    return dict(version='original', products={}, sectors={}, weights={})


class MemberRotation(TestCase):
    def test_reserved_slots_and_cursors_eventually_cover_new_boards_even_when_priorities_fail(self):
        data = mapping()
        priorities = [f'kaipanla:{801000+i}' for i in range(12)]
        fresh = [f'kaipanla:{802000+i}' for i in range(8)]
        market = dict(observed_at=NOW.isoformat(), sectors=[dict(sector_id=s) for s in fresh])
        seen = set()
        with patch.object(maintenance, 'select_directions', return_value=(fresh, {})):
            for run in range(4):
                at = NOW + timedelta(hours=3*run)
                before = deepcopy(data)
                scope = {}
                targets, _ = maintenance.sector_targets(data, market, priorities, at, scope=scope)
                self.assertEqual(len(targets), 8)
                self.assertEqual(len(set(targets) & set(priorities)), 6)
                self.assertEqual(len(set(targets) & set(fresh)), 2)
                self.assertEqual(scope['deferred_count'], 12)
                summary = dict(members={}, members_scope=scope)
                for sid in targets:
                    maintenance._member_attempt(data, sid, at, priorities)
                    summary['members'][sid] = dict(status='PENDING', usable=False)
                data, result = maintenance._finish(before, data, summary)
                self.assertTrue(result['changed'])
                self.assertNotEqual(data['version'], before['version'])
                self.assertEqual(data['sectors'], {})
                self.assertEqual(result['status'], 'PARTIAL')
                self.assertIn('名额外12个板块待后续维护', result['message'])
                self.assertNotIn('沿用', result['message'])
                seen.update(targets)
        self.assertEqual(seen, set(priorities + fresh))
        self.assertEqual(set(data['member_maintenance']),
                         {'priority_after', 'other_after', 'last_attempted_at', 'pending', 'pending_overflow_count'})

    def test_failed_or_deferred_new_board_stays_pending_after_hotspot_changes(self):
        data = mapping()
        old_hot = [f'kaipanla:{802000+i}' for i in range(8)]
        priorities = [f'kaipanla:{801000+i}' for i in range(6)]
        market = dict(observed_at=NOW.isoformat(), sectors=[dict(sector_id=s) for s in old_hot])
        scope = {}
        with patch.object(maintenance, 'select_directions', return_value=(old_hot, {})):
            targets, _ = maintenance.sector_targets(data, market, priorities, NOW, scope=scope)
        original = deepcopy(data)
        for sid in targets:
            maintenance._member_attempt(data, sid, NOW, priorities)
        data, _ = maintenance._finish(original, data, dict(members_scope=scope,
            members={sid: dict(status='PENDING', usable=False) for sid in targets}))
        self.assertTrue(set(old_hot) <= set(data['member_maintenance']['pending']))
        new_hot = ['kaipanla:803000']
        seen = set()
        with patch.object(maintenance, 'select_directions', return_value=(new_hot, {})):
            for run in range(5):
                scope = {}
                targets, _ = maintenance.sector_targets(data, market, priorities, NOW, scope=scope)
                before = deepcopy(data)
                for sid in targets:
                    maintenance._member_attempt(data, sid, NOW, priorities)
                data, _ = maintenance._finish(before, data, dict(members_scope=scope,
                    members={sid: dict(status='PENDING', usable=False) for sid in targets}))
                seen.update(targets)
        self.assertTrue(set(old_hot) <= seen)
        restored = old_hot[0]
        data, _ = maintenance._finish(deepcopy(data), data, dict(members_scope=scope,
            members={restored: dict(status='RETAINED', usable=True)}))
        self.assertNotIn(restored, data['member_maintenance']['pending'])

    def test_pending_queue_preserves_existing_entries_and_exposes_capacity_overflow(self):
        data = mapping()
        old = [f'kaipanla:{800000+i}' for i in range(maintenance.MAX_PENDING_MEMBERS)]
        data['member_maintenance'] = dict(pending=old)
        before = deepcopy(data)
        summary = dict(members_scope=dict(new_candidates=['kaipanla:899999'], deferred_count=1))
        data, result = maintenance._finish(before, data, summary)
        self.assertEqual(data['member_maintenance']['pending'], old)
        self.assertEqual(result['members_scope']['overflow_count'], 1)
        self.assertIn('另1个板块未纳入队列', result['message'])
        self.assertEqual(result['status'], 'PARTIAL')

    def test_existing_sector_population_larger_than_history_cache_still_rotates(self):
        data = mapping()
        data['sectors'] = {f'kaipanla:{800000+i}': {} for i in range(300)}
        seen = set()
        for run in range(38):
            targets, _ = maintenance.sector_targets(data, None, [], NOW)
            self.assertLessEqual(len(targets), 8)
            for sid in targets:
                maintenance._member_attempt(data, sid, NOW, [])
            seen.update(targets)
        self.assertEqual(seen, set(data['sectors']))
        self.assertLessEqual(len(data['member_maintenance']), 3)

    def test_small_priority_group_gives_unused_slots_to_new_and_existing_boards(self):
        data = mapping()
        priorities = ['kaipanla:700000']
        data['sectors'] = {f'kaipanla:{801000+i}': {} for i in range(10)}
        first, _ = maintenance.sector_targets(data, None, priorities, NOW)
        self.assertEqual(len(first), 8)
        self.assertEqual(first[0], priorities[0])
        for sid in first:
            maintenance._member_attempt(data, sid, NOW, priorities)
        resumed = deepcopy(data)
        second, _ = maintenance.sector_targets(resumed, None, priorities, NOW + timedelta(hours=3))
        self.assertEqual(set(first + second), set(data['sectors']) | set(priorities))

    def test_boards_without_members_come_first_in_both_slots(self):
        from modules.strategy_hermes.stage2.reference_policy import ReferencePolicy
        full = {f'kaipanla:{801000+i}': dict(members=['600000.SH']) for i in range(12)}
        active = [f'kaipanla:{801000+i}' for i in range(8)] + ['kaipanla:801159', 'kaipanla:801216']
        weakening = ['kaipanla:801964']
        for mode, expected in (('market', 8), ('retry', 3)):
            data = mapping() | dict(sectors=deepcopy(full), member_maintenance=dict(pending=weakening))
            targets, _ = maintenance.sector_targets(data, None, active, NOW, policy=ReferencePolicy(data, NOW, mode))
            # Active boards without members lead, then the pending one that today's reports still show.
            self.assertEqual((len(targets), targets[:2], weakening[0] in targets), (expected, active[8:], True), mode)
        # A board the retry slot has already tried today, or four times in all, is left alone.
        data = mapping() | dict(sectors=deepcopy(full), member_maintenance=dict(pending=weakening))
        policy = ReferencePolicy(data, NOW, 'retry')
        policy.begin('members:kaipanla:801159', '2026-11-13')
        policy.pending['members:kaipanla:801216'] = dict(target='2026-11-13', attempts=4, last_slot='2026-11-13:market')
        targets, _ = maintenance.sector_targets(data, None, active, NOW, policy=policy)
        self.assertEqual(targets, weakening)
        # Without a cadence policy the order is the plain cursor order.
        data = mapping() | dict(sectors=deepcopy(full))
        self.assertEqual(maintenance.sector_targets(data, None, active, NOW)[0][:6], active[:6])

    def test_removed_cursor_target_does_not_reset_rotation_and_scope_list_is_bounded(self):
        data = mapping()
        data['sectors'] = {f'kaipanla:{801000+i}': {} for i in range(40)}
        data['member_maintenance'] = dict(other_after='kaipanla:801015')
        data['sectors'].pop('kaipanla:801015')
        scope = {}
        targets, _ = maintenance.sector_targets(data, None, [], NOW, scope=scope)
        self.assertEqual(targets[0], 'kaipanla:801016')
        self.assertEqual(scope['deferred_count'], 31)
        self.assertEqual(len(scope['deferred']), 16)


class MemberMessage(TestCase):
    def result(self, members, *, deferred=0):
        return dict(mapping=dict(sectors=1, products=0, weights=0, version='saved'),
                    items=[dict(dataset='mapping', status='PARTIAL', message='成员资料部分完成')],
                    mapping_details=dict(members=members, members_scope=dict(deferred_count=deferred)))

    def test_no_usable_members_is_pending_in_real_qq_template_and_legacy_feedback(self):
        body = dict(mode='close', business_date='2026-11-16', request_id='pending')
        result = self.result({'new': dict(status='PENDING', usable=False, error='SourceFailure')}, deferred=2)
        fields = maintenance_fields(body, result, 'PARTIAL')
        text = render_message('maintenance', fields)['text']
        legacy = feedback(body, result, state='PARTIAL')
        for value in (text, legacy):
            self.assertIn('待补', value)
            self.assertIn('名额外', value)
            self.assertNotIn('沿用', value)

    def test_historical_member_is_only_retained_with_explicit_usable_evidence(self):
        body = dict(mode='review', business_date='2026-11-16', request_id='mixed')
        result = self.result({'valid': dict(status='RETAINED', usable=True, as_of_date='2026-11-12'),
                              'legacy': dict(status='RETAINED', error='SourceFailure')})
        fields = maintenance_fields(body, result, 'PARTIAL')
        text = render_message('maintenance', fields)['text']
        self.assertIn('沿用历史1项', text)
        self.assertIn('待补1项', text)
        self.assertIn('历史名单2026-11-12可用', text)
        self.assertIn('legacy (名单待补', text)

    def test_no_mapping_read_is_explicitly_unknown_in_both_feedback_paths(self):
        body = dict(mode='close', business_date='2026-11-16', request_id='none')
        fields = maintenance_fields(body, {}, 'FAILED')
        text = render_message('maintenance', fields)['text']
        for value in (text, feedback(body, {}, state='FAILED')):
            self.assertIn('未完成读取', value)
            self.assertNotIn('沿用', value)

    def test_queue_capacity_issue_is_visible_in_both_feedback_paths(self):
        body = dict(mode='close', business_date='2026-11-16', request_id='full')
        result = self.result({'new': dict(status='PENDING', usable=False)})
        result['mapping_details']['members_scope']['overflow_count'] = 3
        text = render_message('maintenance', maintenance_fields(body, result, 'PARTIAL'))['text']
        for value in (text, feedback(body, result, state='PARTIAL')):
            self.assertIn('待补队列已满', value)
            self.assertIn('另3个板块未纳入队列', value)
