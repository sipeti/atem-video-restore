#!/usr/bin/env python3
"""Repair a wholly zeroed ATEM-style MP4 opening on a NEW copy only.

Requires an intact, structurally compatible reference MP4 with a top-level
moov box ending at EOF. Does not restore missing encoded video payload.
"""
import argparse
from pathlib import Path
import shutil
import struct
import subprocess
import sys

def eof_moov(path, limit=64*1024*1024):
    n=path.stat().st_size
    off=max(0,n-limit)
    with path.open("rb") as f:
        f.seek(off); data=f.read()
    hits=[]
    start=0
    while True:
        i=data.find(b"moov",start)
        if i<0: break
        if i>=4:
            s=struct.unpack_from(">I",data,i-4)[0]
            abspos=off+i-4
            if s>=8 and abspos+s==n: hits.append((abspos,s))
        start=i+4
    if len(hits)!=1: raise ValueError("Expected unique 32-bit moov box at EOF")
    return hits[0]

def reference_header(path):
    with path.open("rb") as f: head=f.read(256)
    i=0
    while i+8<=len(head):
        n=struct.unpack_from(">I",head,i)[0]
        kind=head[i+4:i+8]
        if kind==b"mdat":
            if i==0: raise ValueError("Missing ftyp")
            if n==1:
                if i+16>len(head): raise ValueError("Truncated mdat")
                return bytearray(head[:i+16]),i,16,struct.unpack_from(">Q",head,i+8)[0]
            if n>=8: return bytearray(head[:i+8]),i,8,n
            raise ValueError("Unsupported mdat length")
        if i==0 and kind!=b"ftyp": raise ValueError("Reference must start with ftyp")
        if i>0 and kind not in (b"free",b"wide"): raise ValueError("Unsupported prefix box")
        if n<8 or i+n>len(head): raise ValueError("Invalid prefix box")
        i+=n
    raise ValueError("No mdat header within first 256 bytes")

def repair(damaged,reference,output):
    damaged=damaged.expanduser().resolve(strict=True)
    reference=reference.expanduser().resolve(strict=True)
    output=output.expanduser().resolve()
    if output in (damaged,reference) or output.exists():
        raise ValueError("Output must be new and distinct from both inputs")
    if not output.parent.is_dir(): raise ValueError("Output directory does not exist")
    moov,moov_len=eof_moov(damaged)
    ref_moov,_=eof_moov(reference)
    header,pos,width,ref_length=reference_header(reference)
    if pos+ref_length!=ref_moov: raise ValueError("Reference mdat does not end at moov")
    if moov<=len(header): raise ValueError("Invalid payload size")
    with damaged.open("rb") as f: old=f.read(len(header))
    if old!=bytes(len(header)): raise ValueError("Opening is not entirely zero; refusing")
    new_length=moov-pos
    if width==8:
        if not 8<=new_length<2**32: raise ValueError("32-bit size overflow")
        struct.pack_into(">I",header,pos,new_length)
    else:
        if new_length<16: raise ValueError("Invalid extended-size mdat")
        struct.pack_into(">Q",header,pos+8,new_length)
    if sys.platform=="darwin":
        r=subprocess.run(["cp","-c",str(damaged),str(output)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if r.returncode!=0:
            if output.exists(): output.unlink()
            with damaged.open("rb") as src,output.open("xb") as dst: shutil.copyfileobj(src,dst,8*1024*1024)
    else:
        with damaged.open("rb") as src,output.open("xb") as dst: shutil.copyfileobj(src,dst,8*1024*1024)
    if output.stat().st_size!=damaged.stat().st_size:
        raise IOError("Copy size mismatch")
    with output.open("r+b") as f: f.write(header)
    return {"output":output,"header_length":len(header),"moov_start":moov,"moov_length":moov_len}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--damaged",required=True,type=Path)
    p.add_argument("--reference",required=True,type=Path)
    p.add_argument("--output",required=True,type=Path)
    a=p.parse_args()
    info=repair(a.damaged,a.reference,a.output)
    print("HEADER RESTORED:",info["output"])
    print("Header bytes:",info["header_length"])
    print("MOOV:",info["moov_start"],info["moov_length"])
    if shutil.which("ffprobe"):
        subprocess.run(["ffprobe","-v","error","-show_entries","format=duration:stream=codec_type,codec_name","-of","default=noprint_wrappers=1",str(info["output"])],check=False)
    print("Sources untouched. Missing video bytes are NOT restored.")
if __name__=="__main__": main()
