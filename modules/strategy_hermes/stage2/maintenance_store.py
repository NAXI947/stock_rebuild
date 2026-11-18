"""Read immutable, locally published reference versions; never call a provider."""
from datetime import datetime
from contextlib import nullcontext
import json
import logging

import psycopg

from ..ledger import encode, fingerprint
from ..simulator import CHINA
from .inputs import mapping_file, VERSION

LOG = logging.getLogger(__name__)


def valid_mapping(value):
    return (isinstance(value,dict) and value.get('schema_version') == 'stock-v4-etf-mapping-v1'
            and isinstance(value.get('version'),str) and VERSION.fullmatch(value['version'])
            and all(isinstance(value.get(k),dict) for k in ('sectors','products','weights')))


def live_mapping(ledger, path, at=None):
    """The deployment file is a fallback, not a completeness gate."""
    at = at or datetime.now(CHINA)
    try:
        with ledger._connect() as conn, conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            rows = conn.execute('SELECT body FROM maintained_mappings WHERE ns=%s AND available_at<=%s '
                                'ORDER BY available_at DESC,version DESC LIMIT 3',(ledger.ns,at)).fetchall()
        for row in rows:
            value = json.loads(row[0]) if isinstance(row[0],str) else row[0]
            if valid_mapping(value):
                return value
    except (psycopg.Error,OSError,ValueError,TypeError,KeyError):
        LOG.warning('Maintained mapping unavailable; retaining deployment version')
    try:
        return mapping_file(path)
    except (OSError,ValueError,TypeError,KeyError):
        LOG.warning('No mapping available; continuing sector research with explicit empty reference scope')
        return dict(schema_version='stock-v4-etf-mapping-v1',version='mapping-unavailable',
                    sectors={},products={},weights={},maintenance_quality='MAPPING_UNAVAILABLE')


def publish_mapping(ledger, mapping, summary, at=None, *, connection=None, expected_parent=None):
    if not valid_mapping(mapping):
        raise ValueError('invalid maintained mapping structure')
    at = at or datetime.now(CHINA)
    with (ledger._connect() if connection is None else nullcontext(connection)) as conn, conn.transaction():
        # Entry/WAIT_BUY intake holds the same namespace lock before reading the
        # latest rule facts. Keep the global order: namespace, then mapping.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (ledger.ns,))
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('mapping-publish:'+ledger.ns,))
        row = conn.execute('SELECT body FROM maintained_mappings WHERE ns=%s AND version=%s',
                           (ledger.ns,mapping['version'])).fetchone()
        if row:
            old = json.loads(row[0]) if isinstance(row[0],str) else row[0]
            if fingerprint(old) != fingerprint(mapping):
                raise ValueError('immutable mapping version conflict')
            return False
        parent = expected_parent if expected_parent is not None else mapping.get('parent_mapping_version')
        latest = conn.execute('SELECT version FROM maintained_mappings WHERE ns=%s '
                              'ORDER BY available_at DESC,version DESC LIMIT 1', (ledger.ns,)).fetchone()
        if latest and parent != latest[0]:
            raise ValueError('MAPPING_PARENT_CONFLICT')
        conn.execute('INSERT INTO maintained_mappings(ns,version,available_at,body,summary) VALUES(%s,%s,%s,%s,%s)',
                     (ledger.ns,mapping['version'],at,encode(mapping),encode(summary)))
        from ..product_verification import complete_publications
        complete_publications(ledger, conn, mapping, at)
    return True
