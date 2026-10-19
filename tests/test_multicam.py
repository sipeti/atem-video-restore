"""Synthetic 1..8 ISO discovery and end-to-end Resolve XML regression coverage."""
import csv
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from atem_multicam import discover_sources, select_sources


class SourceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recordings = self.root / "forrasmasolat"
        self.recordings.mkdir()

    def create(self, name):
        p = self.recordings / name
        p.write_bytes(b"dummy")
        return p

    def test_two_camera_legacy_explicit(self):
        a = self.create("legacy-cam1.mp4")
        b = self.create("legacy-cam2.mp4")
        m = select_sources(self.root, camera="both", cam1=a, cam2=b)
        self.assertEqual(list(m), ["1", "2"])
        self.assertEqual(m["2"], b)

    def test_eight_camera_autodiscovery(self):
        for i in range(1, 9):
            self.create("Lecture CAM %d 01.mp4" % i)
        m = select_sources(self.root, input_dir=self.recordings)
        self.assertEqual(list(m), list(map(str, range(1, 9))))
        self.assertTrue(m["8"].name.endswith("CAM 8 01.mp4"))

    def test_subset_without_renumbering(self):
        for i in (1, 4, 8):
            self.create("Lecture CAM %d 01.mp4" % i)
        self.assertEqual(list(select_sources(self.root)), ["1", "4", "8"])
        self.assertEqual(list(select_sources(self.root, camera="8,1")), ["1", "8"])
        with self.assertRaisesRegex(ValueError, "no matching input"):
            select_sources(self.root, camera="1,2")

    def test_rejects_ambiguous_and_mixed_recordings(self):
        self.create("Lecture CAM 1 01.mp4")
        self.create("lecture CAM1 01.mp4")
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            discover_sources(self.recordings)
        (self.recordings / "lecture CAM1 01.mp4").unlink()
        self.create("AnotherShow CAM 2 01.mp4")
        with self.assertRaisesRegex(ValueError, "Multiple recording prefixes"):
            discover_sources(self.recordings)

    def test_separate_part_and_explicit_override(self):
        old = self.create("Lecture CAM 3 01.mp4")
        second = self.create("Lecture CAM 3 02.mp4")
        other = self.create("repaired_cam3.mp4")
        self.assertEqual(discover_sources(self.recordings, "02")["3"], second)
        self.assertEqual(discover_sources(self.recordings)["3"], old)
        self.assertEqual(select_sources(self.root, input_dir=self.recordings,
                                        mappings=["CAM3=" + str(other)])["3"], other)
        with self.assertRaisesRegex(ValueError, "Repeated camera"):
            select_sources(self.root, input_dir=self.recordings, camera="3,3")

    def test_rejects_out_of_range_camera(self):
        self.create("Lecture CAM 9 01.mp4")
        with self.assertRaisesRegex(ValueError, "No ATEM CAM1..CAM8"):
            discover_sources(self.recordings)
        with self.assertRaisesRegex(ValueError, "Expected --cam"):
            select_sources(self.root, mappings=["CAM9=/tmp/nope"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class ResolveXMLIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = Path(self.tmp.name)
        self.rec = self.job / "RECOVERED"
        self.rec.mkdir()
        self.sample = self.job / "source.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=25",
            "-t", "1", "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-g", "25", str(self.sample)
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=50)

    def run_script(self, name, *args):
        proc = subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)],
                              text=True, capture_output=True, timeout=90)
        self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)
        return proc.stdout

    def make_manifest(self, cams):
        rows = []
        for cam in cams:
            folder = self.rec / ("CAM%d" % cam)
            folder.mkdir()
            name = "CAM%d_00-00-00.000_to_00-00-01.000.mp4" % cam
            shutil.copyfile(self.sample, folder / name)
            rows.append({"camera": str(cam), "output": "CAM%d/%s" % (cam,name),
                         "status": "VIDEO_DECODE_PASS",
                         "original_start_s": "0.000", "original_end_s": "1.000"})
        with (self.rec / "recovery_manifest.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader(); writer.writerows(rows)

    def make_program(self):
        root = self.job / "RECOVERED_PROGRAM"
        folder = root / "PROGRAM"
        folder.mkdir(parents=True)
        name = "PROGRAM_00-00-00.000_to_00-00-01.000.mp4"
        shutil.copyfile(self.sample, folder / name)
        with (root / "recovery_manifest.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["camera","output","status","original_start_s","original_end_s"])
            writer.writeheader()
            writer.writerow({"camera":"PROGRAM","output":"PROGRAM/"+name,
                             "status":"VIDEO_DECODE_PASS",
                             "original_start_s":"0.000","original_end_s":"1.000"})

    def assert_valid_run(self, cams):
        self.make_manifest(cams)
        self.run_script("atem_resolve_timeline.py", "--root", self.rec, "--duration-seconds", "1")
        raw = ET.parse(self.rec / "ATEM_ISO_VERIFIED_WITH_AUDIO.xml")
        tracks = raw.findall("./sequence/media/video/track")
        self.assertEqual(len(tracks), len(cams))
        self.assertEqual([t.findtext("clipitem/name").split("_")[0] for t in tracks],
                         ["CAM%d" % cam for cam in cams])
        self.run_script("atem_resolve_timecode_repair.py", "--root", self.rec)
        patch_xml = self.rec / "ATEM_ISO_VERIFIED_WITH_AUDIO_TCFIX.xml"
        root = ET.parse(patch_xml).getroot()
        tracks = root.findall("./sequence/media/video/track")
        self.assertEqual(len(tracks), len(cams))
        for track in tracks:
            self.assertEqual(track.findtext("clipitem/file/timecode/string"), "00:00:00:00")
        trial = ET.parse(self.rec / ("ATEM_ISO_CAM%d_ONECLIP_TCFIX.xml" % cams[0])).getroot()
        self.assertEqual(sum(len(t.findall("clipitem")) for t in trial.findall("./sequence/media/video/track")), 1)
        self.make_program()
        self.run_script("atem_resolve_three_track.py", "--root", self.job)
        end = ET.parse(self.rec / "ATEM_ISO_WITH_PROGRAM_TCFIX.xml").getroot()
        combined = end.findall("./sequence/media/video/track")
        self.assertEqual(len(combined), len(cams) + 1)
        self.assertEqual(combined[-1].findtext("clipitem/name").split("_")[0], "PROGRAM")
        with (self.rec / "ATEM_ISO_MULTI_SOURCE_COVERAGE.csv").open() as f:
            columns = next(csv.reader(f))
        self.assertEqual(columns[5:-1], ["CAM%d" % i for i in cams] + ["PROGRAM"])
        single = ET.parse(self.rec / "ATEM_ISO_PROGRAM_ONECLIP_TCFIX.xml").getroot()
        self.assertEqual(sum(len(t.findall("clipitem")) for t in single.findall("./sequence/media/video/track")),1)

    def test_cam8_actual_extraction_and_manifest(self):
        self.run_script("atem_iso_rescue.py", "--root", self.job,
                        "--cam", "CAM8=" + str(self.sample),
                        "--reserve-gib", "0")
        manifest = self.rec / "recovery_manifest.csv"
        with manifest.open() as f:
            rows = list(csv.DictReader(f))
        self.assertTrue(rows)
        self.assertEqual({x["camera"] for x in rows}, {"8"})
        self.assertEqual({x["status"] for x in rows}, {"VIDEO_DECODE_PASS"})
        self.assertTrue((self.rec / "CAM8").is_dir())
        with self.assertRaisesRegex(AssertionError, "existing report"):
            self.run_script("atem_iso_rescue.py", "--root", self.job,
                            "--cam", "CAM8=" + str(self.sample),
                            "--reserve-gib", "0")

    def test_two_camera_regression(self):
        self.assert_valid_run([1,2])

    def test_eight_camera_end_to_end(self):
        self.assert_valid_run(list(range(1,9)))

    def test_sparse_camera_tracks(self):
        self.assert_valid_run([1,4,8])


if __name__ == "__main__":
    unittest.main()
