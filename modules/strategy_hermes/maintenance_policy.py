"""Shared maintenance clocks; source-specific cadence stays in the collectors."""
from datetime import time

from .simulator import CHINA

ACTIVE_MODES = {'market': '10:00', 'retry': '14:15'}
MANUAL_MODES = {'verify': '手动静态核验', 'materials': '手动官方材料采集'}
LEGACY_MODES = {'close': '18:00', 'review': '21:00', 'preopen': '08:45'}
MODES = {**LEGACY_MODES, **ACTIVE_MODES, **MANUAL_MODES}
WINDOWS = {'market': (time(10), time(10, 15)),
           'retry': (time(14, 15), time(14, 27))}


def allowed_window(mode, now):
    window = WINDOWS.get(mode)
    return window is not None and window[0] <= now.astimezone(CHINA).time() < window[1]


def maintenance_attempt(now):
    return next((mode for mode in ACTIVE_MODES if allowed_window(mode, now)), None)


def completed_session(calendar, now):
    clock = now.astimezone(CHINA)
    closed = [day for day in calendar if day < clock.date().isoformat()
              or (day == clock.date().isoformat() and clock.time() >= time(15, 35))]
    return max(closed) if closed else None
