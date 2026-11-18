"""Durable cadence and bounded retries inside the published reference version.

This state selects work; the existing queue remains the sole HTTP budget owner.
It never calls a provider or changes the availability date of cached evidence.
"""
from datetime import timedelta

from ..simulator import moment

MAX_PENDING = 256
MAX_ATTEMPTS = 4


class ReferencePolicy:
    def __init__(self, mapping, now, mode):
        self.now, self.mode = now, mode
        self.state = mapping.setdefault('reference_maintenance', {})
        self.state['version'] = 'reference-cadence-v2'
        self.checks = self.state.setdefault('checks', {})
        self.pending = self.state.setdefault('pending', {})
        self.overflow = []

    def due(self, key, *, days=7, target=None):
        pending = self.pending.get(key)
        if pending:
            # One attempt per slot; exhausted targets remain visible for review.
            return (pending.get('attempts', 0) < MAX_ATTEMPTS
                    and pending.get('last_slot') != self.slot)
        if self.mode == 'retry':
            return False
        checked = self.checks.get(key) or {}
        if not checked:
            return True
        if days == 0:
            return checked.get('target') != target
        try:
            return self.now - moment(checked['checked_at']) >= timedelta(days=days)
        except (KeyError, TypeError, ValueError):
            return True

    @property
    def slot(self):
        return self.now.date().isoformat() + ':' + self.mode

    def target(self, key, default):
        return (self.pending.get(key) or {}).get('target', default)

    def begin(self, key, target):
        old = self.pending.get(key) or {}
        if not old and len(self.pending) >= MAX_PENDING:
            self.overflow.append(key)
            return False
        self.pending[key] = dict(target=old.get('target', target),
            attempts=old.get('attempts', 0) + 1, last_slot=self.slot,
            attempted_at=self.now.isoformat(), error_kind='ATTEMPT_INTERRUPTED')
        return True

    def succeeded(self, key, target):
        self.checks[key] = dict(checked_at=self.now.isoformat(), target=target)
        self.pending.pop(key, None)

    def failed(self, key, diagnostic):
        if key in self.pending:
            self.pending[key].update(diagnostic)

    def report(self):
        return dict(pending_count=len(self.pending),
            review_required=sum(v.get('attempts', 0) >= MAX_ATTEMPTS for v in self.pending.values()),
            max_attempts=MAX_ATTEMPTS, overflow_count=len(self.overflow),
            pending=[dict(key=k, **v) for k, v in sorted(self.pending.items())[:12]])
