"""Project reference calendar expansion onto an existing live execution ledger."""
import json

from ..simulator import Calendar, moment


def execution_calendar(connection, namespace, origin, days):
    """Keep committed dates and add only dates beyond the live run watermark.

    The caller owns the publication/quote transaction. Use the same namespace
    lock as ``_store`` so another writer cannot advance the watermark between
    this projection and the guarded commit. Reference calendars remain intact.
    """
    if origin != "LIVE":
        return days
    Calendar(days)
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (namespace,))
    row = connection.execute("SELECT config,watermark FROM runs WHERE ns=%s", (namespace,)).fetchone()
    if row is None or row[1] is None:
        return days
    config = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    boundary = moment(row[1]).date().isoformat()
    # Retain future dates already used by existing T+1 orders as well as history.
    return sorted(set(config["calendar"]) | {day for day in days if day > boundary})
