"""Bounded GET-only SNAP playback sample on local 8061; no hardware access.

Web-service CPU includes concurrent requests. Warm filesystem caches are not
cleared. This measures one client, not a multi-user or production impact test.
"""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlencode


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--day',default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    service='casm-monitor-preview.service'
    pid=int(subprocess.check_output(['systemctl','--user','show',service,'-p','MainPID','--value']))
    group=subprocess.check_output(['systemctl','--user','show',service,'-p','ControlGroup','--value'],text=True).strip()
    root=Path('/sys/fs/cgroup'+group)
    def fields(path):return dict(line.split() for line in path.read_text().splitlines())
    def sample():
        mem=fields(root/'memory.stat')
        rss=next(int(s.split()[1])*1024 for s in Path(f'/proc/{pid}/status').read_text().splitlines() if s.startswith('VmRSS:'))
        return dict(cpu_s=int(fields(root/'cpu.stat')['usage_usec'])/1e6,rss_bytes=rss,
                    cgroup_bytes=int((root/'memory.current').read_text()),anon_bytes=int(mem['anon']),file_bytes=int(mem['file']))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def measure(path,label,binary=False):
        samples=[sample()];stop=threading.Event()
        def poll():
            while not stop.wait(.1):samples.append(sample())
        thread=threading.Thread(target=poll,daemon=True);thread.start()
        begin=time.monotonic()
        try:
            with opener.open('http://127.0.0.1:8061'+path,timeout=90) as response:body=response.read()
        finally:stop.set();thread.join()
        elapsed=time.monotonic()-begin;samples.append(sample())
        record=dict(label=label,wall_s=round(elapsed,3),web_cpu_s=round(samples[-1]['cpu_s']-samples[0]['cpu_s'],3),response_bytes=len(body),
                    peak_rss_bytes=max(s['rss_bytes'] for s in samples),peak_cgroup_bytes=max(s['cgroup_bytes'] for s in samples))
        print(json.dumps(record),flush=True)
        return (None if binary else json.loads(body)),record
    start=sample();measurements=[];manifest=None
    for span in (1,2,7):
        path=f'/api/snap-workspace/spectra-playback?day={args.day}&span_days={span}'
        manifest,result=measure(path,f'{span}-day manifest first request');measurements.append(result)
        _,result=measure(path,f'{span}-day manifest repeat');measurements.append(result)
    for snapshot in manifest['snapshots'][-6:]:
        _,result=measure('/api/snap-workspace/spectra?ids='+','.join(map(str,snapshot['ids'])),'saved frame')
        measurements.append(result)
    membership,_=measure('/api/snap-workspace/beamforming','membership')
    keys=[f'{i["ip"]}/{i["adc"]}' for i in membership['inputs'] if i['beamforming']]
    for start_index in range(max(0,len(manifest['snapshots'])-36),len(manifest['snapshots']),12):
        selection=manifest['snapshots'][start_index:start_index+12]
        params=urlencode(dict(frames=';'.join(','.join(map(str,s['ids'])) for s in selection),inputs=','.join(keys)))
        _,result=measure('/api/snap-workspace/spectra-playback-frames?'+params,f'packed {len(selection)} frames / {len(keys)} inputs',binary=True)
        measurements.append(result)
    output=dict(ts=datetime.now(timezone.utc).isoformat(),day=args.day,pid=pid,baseline=start,after=sample(),measurements=measurements,
                note='One GET-only client; filesystem cache not cleared; CPU includes concurrent web traffic. No hardware, source or Search renders, cache eviction, service-limit changes or production writes.')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(output,indent=2)+'\n')


if __name__=='__main__':main()
