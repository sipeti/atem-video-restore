import sys
from pathlib import Path
import hashlib
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import atem_iso_rescue as iso
import atem_mp4_header_repair as hdr
import atem_resolve_timecode_repair as tc
import atem_zero_scan as zero

class RecoveryTests(unittest.TestCase):
    def test_sparse_zero_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/"test.bin"
            f.write_bytes(b"A"*1024+b"\0"*1024+b"B"*1024)
            self.assertEqual(zero.scan(f,1024,64),(3072,3,1,[(1024,2048)]))
    def test_avcc_rejection(self):
        self.assertEqual(iso.validate_avcc((3).to_bytes(4,"big")+b"\x65\x01\x02"),"")
        self.assertEqual(iso.validate_avcc((20).to_bytes(4,"big")+b"\x65\x01\x02"),"invalid-nal-length")
    def test_timecode(self):
        n=tc.tc_frame("18:03:58:07")
        f=ET.Element("file"); ET.SubElement(f,"media")
        tc.inject_tc(f,n)
        self.assertEqual(f.findtext("timecode/string"),"18:03:58:07")
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),"ffmpeg missing")
    def test_header_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); ref=root/"reference.mp4"; damaged=root/"damaged.mp4"; fixed=root/"fixed.mp4"
            subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","lavfi","-i","testsrc=size=160x90:rate=25","-t","2","-c:v","mpeg4","-q:v","5",str(ref)],check=True,timeout=30)
            original=ref.read_bytes()
            header,_,_,_=hdr.reference_header(ref)
            damaged.write_bytes(bytes(len(header))+original[len(header):])
            digest=hashlib.sha256(damaged.read_bytes()).digest()
            hdr.repair(damaged,ref,fixed)
            self.assertEqual(fixed.read_bytes(),original)
            self.assertEqual(hashlib.sha256(damaged.read_bytes()).digest(),digest)
            with self.assertRaises(ValueError): hdr.repair(damaged,ref,fixed)
if __name__=="__main__": unittest.main()
