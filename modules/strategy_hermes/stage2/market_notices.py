"""Append compact notices alongside already-committed market and report caches."""

from ..ledger import encode


NOTICE_LUA = """
local function notice(stream, watermark, identity, payload)
    redis.call('PERSIST', stream)
    if redis.call('GET', watermark) ~= identity then
        redis.call('XADD', stream, 'MAXLEN', 512, '*', 'data', payload)
        redis.call('SET', watermark, identity, 'EX', 86400)
    end
end
"""


def stream_key(namespace):
    return namespace + ":market:stream"


def notice_keys(namespace, topic):
    return stream_key(namespace), namespace + ":market:notice:" + topic


def notice_body(topic, dataset, revision, observed_at):
    return encode(dict(schema_version="market_update_v1", topic=topic, dataset=dataset,
                       revision=revision, observed_at=observed_at))
