"""Build read-only OCTOP preview input from a saved market snapshot and mapping."""
import argparse
import json
import os
from pathlib import Path

from shared_kernel.temp_manager import TempManager
from ..adapter import READ_ONLY_OBSERVATION
from ..ledger import encode, fingerprint
from ..simulator import moment
from .build_sources import store_file
from .inputs import SharedMarket, mapping_file, read_file
from .penetration import explain_sector


def preview(market, mapping, sector_id):
    at=moment(market.observed_at)
    board=next((row for row in market.body["sectors"] if row["sector_id"]==sector_id),None)
    if board is None or sector_id not in mapping["sectors"]:
        raise ValueError("preview sector is missing from source snapshot or mapping")
    sector=mapping["sectors"][sector_id]
    details=explain_sector(mapping,sector_id,market.body["quotes"],at)
    products=[]
    for product in details.get("products",[]):
        copy=dict(product)
        copy["matched_count"]=len(copy.get("matched",[]))
        copy["matched_top5"]=copy.pop("matched",[])[:5]
        copy["quote"]=market.body["quotes"].get(copy["etf"])
        products.append(copy)
    return dict(schema_version="stock-v4-octop-preview-v1",purpose=READ_ONLY_OBSERVATION,
                trade_date=market.body["trade_date"],snapshot_id=market.snapshot_id,
                observed_at=market.observed_at,mapping_version=mapping["version"],
                source_quality=market.body["quality"],board=board,
                membership=dict(as_of_date=sector["as_of_date"],count=sector["reported_count"],
                                unresolved=sector["unresolved_count"],batches=sector["source_batches"]),
                penetration=products,
                reference_policy="Provide an evidence-bound observation. Preserve observation dates and identify missing inputs.")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market",type=Path,required=True)
    parser.add_argument("--mapping",type=Path,required=True)
    parser.add_argument("--sector",required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args()
    output=args.output_dir
    if not output.is_absolute() or output.resolve()!=output or not output.is_dir() or output.stat().st_mode&0o077:
        parser.error("preview output requires a private absolute directory")
    market=SharedMarket.read(read_file(args.market))
    mapping=mapping_file(args.mapping)
    prepared=preview(market,mapping,args.sector)
    content=encode(prepared).encode()
    manager=TempManager(os.environ["APP_TMP_ROOT"],persistent=output)
    name="octop-preview-"+fingerprint(prepared)[:24]+".json"
    path=store_file(manager,output,name,content)
    print(json.dumps(dict(path=path,sector=prepared["board"]["sector_name"],
        products=len(prepared["penetration"]),purpose=prepared["purpose"]),ensure_ascii=False))


if __name__=="__main__":
    main()
