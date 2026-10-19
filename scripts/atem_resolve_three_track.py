#!/usr/bin/env python3
"""Add verified PROGRAM clips above 1..8 timecode-fixed ATEM ISO camera tracks.

Non-destructive: reads the already timecode-fixed ISO XML, the
RECOVERED_PROGRAM/recovery_manifest.csv and each PROGRAM clip (via ffprobe).
Creates a single-clip trial, two full XMLs and a coverage CSV. Never edits
source MP4, source manifest, original XML, or an existing Resolve project.

Expected layout:
~/Desktop/ATEM_RESCUE/
    RECOVERED/ATEM_ISO_VERIFIED_WITH_AUDIO_TCFIX.xml
    RECOVERED_PROGRAM/recovery_manifest.csv
    RECOVERED_PROGRAM/PROGRAM/*.mp4

Requires Python 3.8+ and ffprobe installed on the Mac.
"""
import argparse
import copy
import csv
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import quote, unquote, urlsplit
import xml.etree.ElementTree as ET

FPS = 25
FPS_D = Decimal(FPS)
TIMECODE_RX = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[:;](\d{2})$")


def frames(value):
    return int((Decimal(str(value)) * FPS_D).to_integral_value(rounding=ROUND_HALF_UP))


def timecode_frames(tc):
    match = TIMECODE_RX.fullmatch(tc)
    if not match:
        raise ValueError("Unsupported source timecode: %r" % tc)
    hh, mm, ss, ff = map(int, match.groups())
    if mm >= 60 or ss >= 60 or ff >= FPS:
        raise ValueError("Invalid source timecode: %r" % tc)
    return ((hh * 60 + mm) * 60 + ss) * FPS + ff


def make(parent, tag, value=None, **attrs):
    elem = ET.SubElement(parent, tag, attrs)
    if value is not None:
        elem.text = str(value)
    return elem


def make_rate(parent):
    obj = make(parent, "rate")
    make(obj, "timebase", FPS)
    make(obj, "ntsc", "FALSE")
    return obj


def file_url(path):
    return "file://localhost" + quote(path.resolve().as_posix(), safe="/")


def read_xml(path):
    if not path.is_file():
        raise ValueError("Known-good TCFIX XML missing: %s" % path)
    tree = ET.parse(str(path))
    root = tree.getroot()
    tracks = root.findall("./sequence/media/video/track")
    if not 1 <= len(tracks) <= 8:
        raise ValueError("Expected 1..8 original camera video tracks; found %s" % len(tracks))
    counts = [len(track.findall("clipitem")) for track in tracks]
    if not all(n > 0 for n in counts):
        raise ValueError("All camera tracks must have at least one clip: %s" % counts)
    camera_labels(root)  # distinct, well-formed track mappings only
    if root.findtext("./sequence/rate/timebase") != "25":
        raise ValueError("Original XML frame rate is not 25fps")
    if int(root.findtext("./sequence/duration", "0")) <= 0:
        raise ValueError("Invalid original timeline duration")
    existing_files = root.findall(".//file")
    ids = {node.attrib["id"] for node in existing_files if node.get("id")}
    if any(key.startswith("file_program_") for key in ids):
        raise ValueError("PROGRAM is already present in original XML")
    for i, track in enumerate(tracks):
        for clip in track.findall("clipitem"):
            full = clip.find("file")
            if full is None or full.find("timecode") is None:
                raise ValueError("Original XML lacks source TCFIX; refusing non-TCFIX XML")
            pathurl = full.findtext("pathurl", "")
            parsed = urlsplit(pathurl)
            if parsed.scheme != "file":
                raise ValueError("Original XML has an unsupported media path")
            actual = Path(unquote(parsed.path))
            if not actual.is_file():
                raise ValueError("Original camera media is missing: %s" % actual)
    return root


def camera_labels(root):
    tracks = root.findall("./sequence/media/video/track")
    labels = []
    for track in tracks:
        names = [clip.findtext("name") or "" for clip in track.findall("clipitem")]
        matches = [re.match(r"CAM([1-8])_", name, re.I) for name in names]
        if not names or any(m is None for m in matches):
            raise ValueError("Cannot infer CAM1..CAM8 identity from a video track")
        cams = {m.group(1) for m in matches}
        if len(cams) != 1:
            raise ValueError("Multiple cameras were mixed into one track")
        labels.append("CAM" + cams.pop())
    if len(labels) != len(set(labels)):
        raise ValueError("Duplicate camera tracks in original XML")
    if len(labels) != len(tracks) or len(labels) > 8:
        raise ValueError("Invalid camera track count")
    return labels


def get_probe(path):
    command = [
        "ffprobe", "-hide_banner", "-v", "error",
        "-show_entries",
        "format=duration:format_tags=timecode:"
        "stream=index,codec_type,codec_name,width,height,avg_frame_rate,nb_frames,duration,channels,sample_rate:"
        "stream_tags=timecode", "-of", "json", str(path),
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=90, check=False)
    if result.returncode:
        raise ValueError("ffprobe failed for %s: %s" % (path.name, result.stderr.strip()[:250]))
    decoded = json.loads(result.stdout)
    allstreams = decoded.get("streams", [])
    video = next((s for s in allstreams if s.get("codec_type") == "video"), None)
    audio = next((s for s in allstreams if s.get("codec_type") == "audio"), None)
    if video is None or video.get("codec_name") != "h264":
        raise ValueError("Missing H.264 video: %s" % path.name)
    if (video.get("width"), video.get("height")) != (1920, 1080):
        raise ValueError("Unexpected resolution for %s" % path.name)
    if video.get("avg_frame_rate") not in ("25/1", "50/2"):
        raise ValueError("Unexpected framerate for %s: %s" % (path.name, video.get("avg_frame_rate")))
    count = video.get("nb_frames")
    if str(count).isdigit() and int(count) > 0:
        numframes = int(count)
    else:
        duration = video.get("duration") or decoded.get("format", {}).get("duration")
        if duration is None:
            raise ValueError("Unknown source length: %s" % path.name)
        numframes = frames(duration)
    if numframes < 1:
        raise ValueError("Source clip has no frames: %s" % path.name)
    tag = (video.get("tags") or {}).get("timecode")
    if not tag:
        tag = next(((s.get("tags") or {}).get("timecode") for s in allstreams
                    if (s.get("tags") or {}).get("timecode")), None)
    if not tag:
        tag = (decoded.get("format", {}).get("tags") or {}).get("timecode")
    tag = tag or "00:00:00:00"
    tcframes = timecode_frames(tag)
    return {
        "frames": numframes, "timecode": tag, "tcframes": tcframes,
        "audio": bool(audio and audio.get("codec_name") == "aac"),
        "channels": int(audio.get("channels") or 2) if audio else 0,
        "samplerate": int(audio.get("sample_rate") or 48000) if audio else 48000,
    }


def load_program(root, timeline_frames):
    manifest = root / "recovery_manifest.csv"
    if not manifest.is_file():
        raise ValueError("PROGRAM manifest missing: %s" % manifest)
    with manifest.open("r", newline="", encoding="utf-8-sig") as f:
        records = list(csv.DictReader(f))
    required = {"camera", "output", "status", "original_start_s", "original_end_s"}
    if not records or not required.issubset(set(records[0].keys())):
        raise ValueError("PROGRAM manifest is empty or missing columns")
    good, bad = [], []
    for index, row in enumerate(records, 1):
        if row["camera"] != "PROGRAM":
            raise ValueError("Non-PROGRAM row %d in PROGRAM manifest" % index)
        if row["status"] != "VIDEO_DECODE_PASS":
            bad.append((index, row["status"]))
            continue
        rel = Path(row["output"])
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "PROGRAM":
            raise ValueError("Unsafe PROGRAM output path in row %d" % index)
        p = (root / rel).resolve()
        if root.resolve() not in p.parents or not p.is_file():
            raise ValueError("PROGRAM clip file not found: %s" % p)
        st = frames(row["original_start_s"])
        en = frames(row["original_end_s"])
        if not 0 <= st < en <= timeline_frames:
            raise ValueError("PROGRAM time is outside original timeline in row %d" % index)
        good.append({"index": index, "path": p, "start": st, "end": en})
    if bad:
        print("WARNING: skipped %d PROGRAM clips not video-decode verified" % len(bad))
    if not good:
        raise ValueError("No verified PROGRAM clips found")
    good.sort(key=lambda row: (row["start"], row["index"]))
    last = 0
    for item in good:
        if item["start"] < last:
            raise ValueError("PROGRAM clips overlap (manifest): %s" % item["path"].name)
        last = item["end"]
    for index, item in enumerate(good, 1):
        item["meta"] = get_probe(item["path"])
        target = item["end"] - item["start"]
        available = item["meta"]["frames"]
        item["length"] = min(available, target)
        if item["length"] < target:
            print("WARNING: shortening %s by %d frames (media duration)" %
                  (item["path"].name, target - item["length"]))
        item["record_end"] = item["start"] + item["length"]
        if index % 20 == 0:
            print("  verified metadata %d/%d PROGRAM clips" % (index, len(good)), flush=True)
    return good


def add_program_file(clip, item, fid, with_audio):
    meta = item["meta"]
    mediafile = make(clip, "file", id=fid)
    make(mediafile, "name", item["path"].name)
    make(mediafile, "pathurl", file_url(item["path"]))
    make_rate(mediafile)
    make(mediafile, "duration", meta["frames"])
    timecode = make(mediafile, "timecode")
    make_rate(timecode)
    make(timecode, "string", meta["timecode"])
    make(timecode, "frame", meta["tcframes"])
    make(timecode, "displayformat", "NDF")
    media = make(mediafile, "media")
    vm = make(media, "video")
    make(vm, "duration", meta["frames"])
    sample = make(vm, "samplecharacteristics")
    make_rate(sample)
    make(sample, "width", 1920)
    make(sample, "height", 1080)
    make(sample, "anamorphic", "FALSE")
    make(sample, "pixelaspectratio", "square")
    make(sample, "fielddominance", "none")
    if with_audio and meta["audio"]:
        aud = make(media, "audio")
        sc = make(aud, "samplecharacteristics")
        make(sc, "samplerate", meta["samplerate"])
        make(sc, "depth", 16)
        make(aud, "channelcount", meta["channels"])
        if meta["channels"] == 2:
            make(aud, "layout", "stereo")


def make_clip(track, item, index, is_audio=False, with_audio=False):
    fid = "file_program_%03d" % index
    cid = ("audio_program_%03d" if is_audio else "video_program_%03d") % index
    clip = make(track, "clipitem", id=cid)
    make(clip, "name", item["path"].name)
    make(clip, "duration", item["meta"]["frames"])
    make_rate(clip)
    make(clip, "start", item["start"])
    make(clip, "end", item["record_end"])
    make(clip, "in", 0)
    make(clip, "out", item["length"])
    make(clip, "enabled", "TRUE")
    if not is_audio:
        make(clip, "anamorphic", "FALSE")
        make(clip, "alphatype", "none")
        add_program_file(clip, item, fid, with_audio)
    else:
        make(clip, "file", id=fid)
    source = make(clip, "sourcetrack")
    make(source, "mediatype", "audio" if is_audio else "video")
    make(source, "trackindex", 1)


def add_program_track(root, program, *, with_audio):
    media = root.find("./sequence/media")
    video = media.find("video")
    if video is None:
        raise ValueError("Original XML missing video section")
    labels = camera_labels(root)
    track = make(video, "track")
    for i, entry in enumerate(program, 1):
        make_clip(track, entry, i, with_audio=with_audio)
    make(track, "enabled", "TRUE")
    make(track, "locked", "FALSE")
    if with_audio:
        audio = media.find("audio")
        if audio is None:
            raise ValueError("Original XML missing audio section")
        atrack = make(audio, "track")
        count = 0
        for i, entry in enumerate(program, 1):
            if not entry["meta"]["audio"]:
                continue
            make_clip(atrack, entry, i, is_audio=True)
            count += 1
        make(atrack, "enabled", "TRUE")
        make(atrack, "locked", "FALSE")
        print("  extra PROGRAM audio clips: %d (NOT separately decode-verified)" % count)
    seq = root.find("./sequence")
    seq.set("id", "sequence-atem-rescue-program")
    name = "ATEM ISO RESCUE - " + " ".join(
        "%s V%d" % (label, i) for i, label in enumerate(labels, 1))
    name += " PROGRAM V%d" % (len(labels) + 1)
    if with_audio:
        name += " + PROGRAM AUDIO A%d" % (len(labels) + 1)
    make_name = seq.find("name")
    if make_name is None:
        make_name = ET.Element("name")
        seq.insert(0, make_name)
    make_name.text = name


def write_xml(root, path):
    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    i = raw.find(b"?>")
    if i < 0:
        raise ValueError("XML declaration missing")
    raw = raw[:i + 2] + b"\n<!DOCTYPE xmeml>\n" + raw[i + 2:]
    with path.open("xb") as f:
        f.write(raw)
    parsed = ET.parse(str(path)).getroot()
    expected = len(root.findall("./sequence/media/video/track"))
    if len(parsed.findall("./sequence/media/video/track")) != expected:
        raise ValueError("PROGRAM XML track count changed while writing")


def coverage_csv(path, base, program, timeline_frames):
    tracks = base.findall("./sequence/media/video/track")
    labels = camera_labels(base) + ["PROGRAM"]
    source_spans = []
    for track in tracks:
        source_spans.append([(int(x.findtext("start")), int(x.findtext("end")))
                             for x in track.findall("clipitem")])
    source_spans.append([(x["start"], x["record_end"]) for x in program])
    edges = {0, timeline_frames}
    for arr in source_spans:
        for start, end in arr:
            edges.update((start, end))
    points = sorted(edges)
    spans = []
    for start, end in zip(points, points[1:]):
        flags = tuple(any(a <= start and b >= end for a, b in arr) for arr in source_spans)
        if spans and spans[-1][1] == start and spans[-1][2] == flags:
            spans[-1] = (spans[-1][0], end, flags)
        else:
            spans.append((start, end, flags))
    with path.open("x", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["start_frame", "end_frame", "start_s", "end_s", "duration_s"]
                   + labels + ["sources_available"])
        for start, end, flags in spans:
            w.writerow([start, end, "%.3f" % (start / FPS), "%.3f" % (end / FPS),
                        "%.3f" % ((end-start) / FPS)] + list(map(int, flags)) + [sum(flags)])
    time_cam = [sum(b-a for a,b in intervals) for intervals in source_spans]
    union_cams = sum(b-a for a,b,flags in spans if any(flags[:-1]))
    union_all = sum(b-a for a,b,flags in spans if any(flags))
    for label, intervals in zip(labels, source_spans):
        print("  %-7s: %d clips, %.2f min" %
              (label, len(intervals), sum(b-a for a,b in intervals) / FPS / 60))
    print("  ISO cameras coverage: %.2f min" % (union_cams / FPS / 60))
    print("  Added by PROGRAM:     %.2f sec" % ((union_all-union_cams)/FPS))
    print("  %d-source coverage:   %.2f min (%.2f%%)" %
          (len(labels), union_all/FPS/60, 100*union_all/timeline_frames))
    print("  Still missing:     %.2f sec" % ((timeline_frames-union_all)/FPS))
    return spans


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path,
                    default=Path.home() / "Desktop" / "ATEM_RESCUE")
    ap.add_argument("--xml", type=Path, help="existing timecode-fixed camera timeline XML")
    args = ap.parse_args()
    root = args.root.expanduser().resolve()
    existing = args.xml.expanduser().resolve() if args.xml else root / "RECOVERED" / "ATEM_ISO_VERIFIED_WITH_AUDIO_TCFIX.xml"
    program_root = root / "RECOVERED_PROGRAM"
    outdir = root / "RECOVERED"
    oneclip = outdir / "ATEM_ISO_PROGRAM_ONECLIP_TCFIX.xml"
    video = outdir / "ATEM_ISO_WITH_PROGRAM_TCFIX.xml"
    audio = outdir / "ATEM_ISO_WITH_PROGRAM_AUDIO_TCFIX.xml"
    report = outdir / "ATEM_ISO_MULTI_SOURCE_COVERAGE.csv"
    targets = (oneclip, video, audio, report)
    for target in targets:
        if target.exists():
            raise ValueError("Output exists; refusing overwrite: %s" % target)
    if shutil.which("ffprobe") is None:
        raise ValueError("ffprobe missing (brew install ffmpeg)")
    print("Known-good TCFIX XML:", existing)
    original = read_xml(existing)
    camera_count = len(camera_labels(original))
    timeline_frames = int(original.findtext("./sequence/duration"))
    print("PROGRAM manifest:", program_root / "recovery_manifest.csv")
    program = load_program(program_root, timeline_frames)
    print("\n=== PROGRAM CLIP METADATA ===")
    tcs = sorted({entry["meta"]["timecode"] for entry in program})
    print("  clips: %d" % len(program))
    print("  source timecode(s): %s" % (", ".join(tcs)))
    print("  readable AAC: %d/%d" % (sum(x["meta"]["audio"] for x in program), len(program)))
    print("\n=== TIMELINE COVERAGE ===")
    spans = coverage_csv(report, original, program, timeline_frames)

    t = copy.deepcopy(original)
    add_program_track(t, program, with_audio=False)
    write_xml(t, video)
    ta = copy.deepcopy(original)
    add_program_track(ta, program, with_audio=True)
    write_xml(ta, audio)

    small = copy.deepcopy(t)
    for track_index, track in enumerate(small.findall("./sequence/media/video/track")):
        for clip_index, clip in enumerate(list(track.findall("clipitem"))):
            if track_index == camera_count and clip_index == 0:
                continue  # Only the first PROGRAM segment remains in the trial XML.
            track.remove(clip)
    for track in small.findall("./sequence/media/audio/track"):
        for clip in list(track.findall("clipitem")):
            track.remove(clip)
    write_xml(small, oneclip)

    print("\n=== XML READY ===")
    print("First test one PROGRAM clip:", oneclip)
    print("Full %d-video-track XML:    " % (camera_count + 1), video)
    print("With PROGRAM guide audio:  ", audio)
    print("Coverage detail CSV:       ", report)
    print("All original XML/CSV/MP4 files left unchanged.")
    print("NOTE: full Resolve import must still be checked on this Mac.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.TimeoutExpired, ET.ParseError, AssertionError) as exc:
        sys.exit("ERROR: " + str(exc))