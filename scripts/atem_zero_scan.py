#!/usr/bin/env python3
"""Sparse, read-only sampling of all-zero regions in suspect files."""
import argparse
from pathlib import Path

def scan(path, step=1048576, sample=16384):
    if not (0 < sample <= step):
        raise ValueError("sample must be positive and <= step")
    size=path.stat().st_size
    total=zeros=0
    spans=[]
    start=None
    with path.open("rb") as inp:
        for pos in range(0,size,step):
            inp.seek(pos)
            b=inp.read(sample)
            zero=bool(b) and not any(b)
            if zero:
                zeros+=1
                if start is None: start=pos
            elif start is not None:
                spans.append((start,pos)); start=None
            total+=1
    if start is not None: spans.append((start,size))
    return size,total,zeros,spans

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("files",nargs="+",type=Path)
    p.add_argument("--step-kib",type=int,default=1024)
    p.add_argument("--sample-kib",type=int,default=16)
    p.add_argument("--top-runs",type=int,default=8)
    a=p.parse_args()
    for target in a.files:
        path=target.expanduser().resolve(strict=True)
        size,total,zeros,spans=scan(path,a.step_kib*1024,a.sample_kib*1024)
        print("\n=== %s ===" % path.name)
        print("Size: %.2f GiB" % (size/1024**3))
        print("All-zero samples: %d/%d (%.2f%%)" % (zeros,total,(zeros/total*100 if total else 0)))
        print("Sampled zero runs:",len(spans))
        for start,end in sorted(spans,key=lambda r:r[1]-r[0],reverse=True)[:a.top_runs]:
            print("  %.1f..%.1f MiB (sampled)" % (start/1048576,end/1048576))
    print("\nREAD ONLY COMPLETE. Zero-free sampling does not prove integrity.")
if __name__=="__main__": main()
