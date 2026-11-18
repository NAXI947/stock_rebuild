"""Worker-owned breadth and global snapshots, persisted before display."""
from datetime import datetime
import json
import logging
from pathlib import Path
import psycopg

from ..ledger import fingerprint
from ..simulator import CHINA
from .macro_background import normalize
from .market_bootstrap import capture_request, save, ProbeError
from .http_json import get_json, ProviderHTTPError
from .market_panorama import normalize_background

LOG=logging.getLogger(__name__)


def normalize_live(body, collected_at):
    result=normalize(dict(id=72,直调返回=dict(ok=True,http_status=200,return_json=body)),fingerprint(body))
    result.update(source="开盘啦 072 正式后台采集",input_kind="LIVE",collected_at=collected_at)
    return result


def load(ledger):
    with ledger._connect() as conn:
        row=conn.execute("""SELECT body FROM supplier_market_batches
            WHERE provider='kaipanla' AND endpoint='072' AND scope->>'purpose'='macro-background'
            ORDER BY collected_at DESC,batch_id DESC LIMIT 1""").fetchone()
    return json.loads(row[0]) if row else None


class MacroRefresh:
    def __init__(self, ledger, captures):
        self.ledger,self.captures=ledger,Path(captures)
        self.due={"072":0,"018":0}
        self.status=None
        self.parts={}

    def tick(self, now):
        endpoint=next((key for key,due in self.due.items() if now>=due),None)
        if endpoint is None:
            return self.status
        self.due[endpoint]=now+900
        try:
            from .bulk_collect import RequestLog
            stamp=datetime.now(CHINA)
            bucket=int(stamp.timestamp())//900
            log=RequestLog(self.ledger,"panorama-background:"+str(bucket))
            def collect():
                request,_=capture_request(self.captures,endpoint)
                body=get_json(request.full_url,headers=tuple(request.header_items()),timeout=8)
                received=datetime.now(CHINA)
                result=(normalize_live(body,received.isoformat()) if endpoint=="072"
                        else normalize_background(body,received.isoformat()))
                rows=len(result["items"]) if endpoint=="072" else 1
                purpose="macro-background" if endpoint=="072" else "market-background"
                saved=save(self.ledger,"kaipanla",endpoint,dict(purpose=purpose),result,rows,result.get("observed_at"),received)
                return dict(state="STORED",collected_at=received.isoformat(),rows=rows,batch=saved["batch"])
            self.parts[endpoint]=log.call("kaipanla:"+endpoint,dict(endpoint=endpoint),collect,limit=1)
        except (OSError,ValueError,KeyError,TypeError,ProbeError,ProviderHTTPError,psycopg.Error) as exc:
            self.parts[endpoint]=dict(state="FAILED",error=type(exc).__name__)
            LOG.warning("%s refresh failed: %s",endpoint,type(exc).__name__)
        self.status=dict(self.parts[endpoint],endpoint=endpoint,parts=dict(self.parts))
        return self.status
