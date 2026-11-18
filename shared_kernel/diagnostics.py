"""Bounded, credential-free error groups shared by HTTP entry points."""
from collections import OrderedDict
from hashlib import sha256
import logging
from threading import Lock

LOCK=Lock()
GROUPS=OrderedDict()
LOG=logging.getLogger(__name__)


def record(stage, code, error):
    source=type(error).__name__
    identifier=sha256(f"{stage}:{code}:{source}".encode()).hexdigest()[:16]
    with LOCK:
        item=GROUPS.pop(identifier,dict(error_id=identifier,stage=stage,source=source,error=code,count=0))
        item["count"]+=1
        GROUPS[identifier]=item
        while len(GROUPS)>128:
            GROUPS.popitem(last=False)
        result=dict(item)
    if result["count"] & (result["count"]-1)==0:
        LOG.warning("operation failure %s", result)
    return result


def snapshot():
    with LOCK:
        return [dict(item) for item in GROUPS.values()]
