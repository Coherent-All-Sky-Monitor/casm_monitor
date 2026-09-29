"""Read-only bounded review of proposed change thresholds on saved SNAP data."""
import argparse
import json
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.config import load_settings
from casm_monitor.store import Store
from casm_monitor.web.snap_spectra import build_router


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--backtest-hours',type=int,default=0,choices=range(0,49))
    args=parser.parse_args()
    settings=load_settings()
    store=Store(settings.db_path,store_root=settings.store_root,read_only=True)
    app=FastAPI()
    app.include_router(build_router(settings,store))
    start=time.monotonic()
    with TestClient(app) as client:
        response=client.get('/api/snap-workspace/changes')
        response.raise_for_status()
        result=response.json()
        history=[]
        now=time.time()
        for hours in range(1,args.backtest_hours+1):
            # Historical evaluation must not apply today's latest-attempt status.
            with patch('casm_monitor.web.snap_spectra.time.time',return_value=now-hours*3600), \
                 patch('casm_monitor.web.snap_spectra.latest_reads',return_value={}):
                old=client.get('/api/snap-workspace/changes')
                old.raise_for_status()
                inputs=[i for i in old.json()['inputs'] if i['functional']]
                history.append(dict(hours_ago=hours,states=dict(Counter(i['state'] for i in inputs)),
                                    changes=[i for i in inputs if i['state'] in ('watch','attention')]))
        result['backtest']=history
    result['elapsed_s']=round(time.monotonic()-start,3)
    result['hardware_contact']=False
    result['wired_counts']=dict(Counter(i['state'] for i in result['inputs'] if i['functional']))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('inputs','backtest')},indent=2))
    print('Historical state totals:',dict(Counter(i['state'] for h in history for i in h['changes'])))
    store.close()


if __name__=='__main__':
    main()
