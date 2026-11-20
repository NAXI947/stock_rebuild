"""Worker-owned breadth and global snapshots, persisted before display."""
from datetime import datetime, time
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
from .market_regime import COLLECTED, normalize_index

LOG=logging.getLogger(__name__)
INDEX_READY=time(15,5)


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
        self.due={"072":0,"018":0,"107":0}
        self.status=None
        self.parts={}
        self.index_day={}
        self.index_stored=set()

    def _index_due(self, stamp):
        """One index per tick: once after each weekday close, or at once when none is stored."""
        today=stamp.date().isoformat()
        for code in COLLECTED:
            if self.index_day.get(code)==today:
                continue
            if stamp.weekday()<5 and stamp.time()>=INDEX_READY:
                return code
            if code not in self.index_stored:
                with self.ledger._connect() as conn:
                    found=conn.execute("""SELECT 1 FROM supplier_market_batches WHERE provider='kaipanla'
                        AND endpoint='107' AND scope->>'StockID'=%s LIMIT 1""",(code,)).fetchone()
                if not found:
                    return code
                self.index_stored.add(code)
        return None

    def tick(self, now):
        stamp=datetime.now(CHINA)
        index=None
        for endpoint,due in self.due.items():
            if now<due:
                continue
            if endpoint!="107":
                break
            try:
                index=self._index_due(stamp)
            except psycopg.Error:
                index=None
            if index:
                break
        else:
            return self.status
        self.due[endpoint]=now+900
        try:
            from .bulk_collect import RequestLog
            bucket=int(stamp.timestamp())//900
            log=RequestLog(self.ledger,"panorama-background:"+str(bucket))
            def collect():
                request,scope=capture_request(self.captures,endpoint,dict(StockID=index) if index else None)
                body=get_json(request.full_url,headers=tuple(request.header_items()),timeout=8)
                received=datetime.now(CHINA)
                if index:
                    rows=normalize_index(body,index)
                    saved=save(self.ledger,"kaipanla",endpoint,dict(purpose="index-kline",StockID=index),
                               dict(rows=rows,code=index),len(rows),None,received)
                    return dict(state="STORED",collected_at=received.isoformat(),rows=len(rows),batch=saved["batch"],
                                code=index,latest=rows[-1]["trade_date"])
                result=(normalize_live(body,received.isoformat()) if endpoint=="072"
                        else normalize_background(body,received.isoformat()))
                rows=len(result["items"]) if endpoint=="072" else 1
                purpose="macro-background" if endpoint=="072" else "market-background"
                saved=save(self.ledger,"kaipanla",endpoint,dict(purpose=purpose),result,rows,result.get("observed_at"),received)
                return dict(state="STORED",collected_at=received.isoformat(),rows=rows,batch=saved["batch"])
            source="kaipanla:"+endpoint+(":"+index if index else "")
            self.parts[endpoint]=log.call(source,dict(endpoint=endpoint),collect,limit=1)
            if index:
                self.index_day[index]=stamp.date().isoformat()
                self.index_stored.add(index)
                self.due[endpoint]=now+60
        except (OSError,ValueError,KeyError,TypeError,ArithmeticError,ProbeError,ProviderHTTPError,psycopg.Error) as exc:
            self.parts[endpoint]=dict(state="FAILED",error=type(exc).__name__)
            LOG.warning("%s refresh failed: %s",endpoint,type(exc).__name__)
        self.status=dict(self.parts[endpoint],endpoint=endpoint,parts=dict(self.parts))
        return self.status
