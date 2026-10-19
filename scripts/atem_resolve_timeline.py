#!/usr/bin/env python3
"""Generate DaVinci Resolve-compatible FCP7 XML timelines from ATEM ISO Rescue.

Reads ~/Desktop/ATEM_RESCUE/RECOVERED/recovery_manifest.csv and references
already-extracted clips in CAM1/ and CAM2/. Does not change any media.

Outputs:
  ATEM_ISO_VERIFIED_VIDEO.xml        V1 CAM1, V2 CAM2 (decode-pass only)
  ATEM_ISO_VERIFIED_WITH_AUDIO.xml   plus guide audio tracks A1 CAM1, A2 CAM2
  ATEM_ISO_REVIEW_ALL.xml            all existing extracted clips (unchecked ones
                                    labelled REVIEW); video only
  ATEM_ISO_TIMELINE_REPORT.csv       imported/skipped clips and placement

The original recording timeline starts at 00:00:00:00, 25 fps, 1920x1080.
No created media is overwritten, and XML is not a replacement for human QA.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote
import xml.etree.ElementTree as ET

FPS = 25
VALID_STATUSES = {"VIDEO_DECODE_PASS"}
REVIEW_STATUSES = {"VIDEO_DECODE_PASS", "VIDEO_DECODE_FAIL", "START_ALIGNMENT_UNCERTAIN",
                   "START_ALIGNED_NOT_DECODED", "EXISTS_NOT_OVERWRITTEN", "EXTRACTED"}


def frame_number(seconds: str) -> int:
    return int((Decimal(str(seconds)) * FPS).to_integral_value(rounding=ROUND_HALF_UP))


def make(parent: ET.Element, tag: str, value: object | None = None, **attrs: str) -> ET.Element:
    child = ET.SubElement(parent, tag, attrs)
    if value is not None:
        child.text = str(value)
    return child


def rate(parent: ET.Element) -> None:
    r = make(parent, "rate")
    make(r, "timebase", FPS)
    make(r, "ntsc", "FALSE")


def add_video_format(parent: ET.Element) -> None:
    v = make(parent, "format")
    s = make(v, "samplecharacteristics")
    rate(s)
    make(s, "width", 1920)
    make(s, "height", 1080)
    make(s, "anamorphic", "FALSE")
    make(s, "pixelaspectratio", "square")
    make(s, "fielddominance", "none")


def file_uri(path: Path) -> str:
    # Apple interchange documentation uses file://localhost/absolute/path.
    # Quote special characters (spaces, diacritics, #, etc) but not slashes.
    return "file://localhost" + quote(path.resolve().as_posix(), safe="/")


def probe_clip(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,nb_frames,duration,channels,sample_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if proc.returncode:
        raise ValueError("ffprobe failed: " + proc.stderr.strip()[:220])
    streams = json.loads(proc.stdout).get("streams", [])
    vid = next((s for s in streams if s.get("codec_type") == "video"), None)
    aud = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not vid:
        raise ValueError("No video stream")
    if vid.get("width") != 1920 or vid.get("height") != 1080:
        raise ValueError("Unexpected video dimensions: " + str((vid.get("width"), vid.get("height"))))
    if vid.get("avg_frame_rate") not in (None, "25/1", "50/2"):
        raise ValueError("Unexpected video FPS: " + str(vid.get("avg_frame_rate")))
    n = vid.get("nb_frames")
    if n is not None and str(n).isdigit() and int(n) > 0:
        frames = int(n)
    else:
        duration = vid.get("duration") or json.loads(proc.stdout).get("format", {}).get("duration")
        if duration is None:
            raise ValueError("Unknown number of source video frames")
        frames = frame_number(str(duration))
    if frames < 1:
        raise ValueError("Zero video frames")
    return {"frames": frames, "audio": aud is not None,
            "channels": int(aud.get("channels", 2)) if aud else 0,
            "sample_rate": int(aud.get("sample_rate", 48000)) if aud else 48000}


def read_manifest(root: Path) -> tuple[list[dict], list[str]]:
    manifest = root / "recovery_manifest.csv"
    if not manifest.is_file():
        raise ValueError(f"No rescue manifest at: {manifest}")
    with manifest.open("r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    needed = {"camera", "output", "status", "original_start_s", "original_end_s"}
    if not rows:
        raise ValueError("Manifest contains no rows")
    if not needed.issubset(rows[0]):
        raise ValueError("Manifest missing columns: " + ", ".join(sorted(needed - rows[0].keys())))
    usable = []
    warnings = []
    for idx, row in enumerate(rows, 1):
        cam = row["camera"].strip()
        if cam not in {"1", "2"}:
            warnings.append(f"Row {idx}: unknown camera, skipping")
            continue
        rel = Path(row["output"])
        if rel.is_absolute() or ".." in rel.parts:
            warnings.append(f"Row {idx}: unsafe path, skipping")
            continue
        path = root / rel
        try:
            start = frame_number(row["original_start_s"])
            end = frame_number(row["original_end_s"])
        except Exception:
            warnings.append(f"Row {idx}: malformed timestamps")
            continue
        if start < 0 or end <= start:
            warnings.append(f"Row {idx}: invalid timestamp span")
            continue
        item = {"row": idx, "camera": cam, "path": path, "start": start,
                "end": end, "status": row["status"], "output": row["output"]}
        usable.append(item)
    return usable, warnings


def prepare_entries(all_rows: list[dict], allowed: set[str], *, cache: dict,
                    report: list[dict], label: str) -> list[dict]:
    picked = []
    for item in all_rows:
        report_base = {"timeline": label, "camera": item["camera"],
                       "filename": item["output"], "original_start_frame": item["start"],
                       "original_end_frame": item["end"], "rescue_status": item["status"]}
        if item["status"] not in allowed:
            report.append({**report_base, "action": "SKIP_STATUS", "timeline_start_frame": "",
                           "timeline_end_frame": "", "details": "Not in accepted status set"})
            continue
        if not item["path"].is_file():
            report.append({**report_base, "action": "SKIP_MISSING", "timeline_start_frame": "",
                           "timeline_end_frame": "", "details": "Extracted MP4 not found"})
            continue
        try:
            # Avoid probing the same file for every XML version.
            if item["path"] not in cache:
                cache[item["path"]] = probe_clip(item["path"])
            meta = cache[item["path"]]
        except Exception as exc:
            report.append({**report_base, "action": "SKIP_INVALID", "timeline_start_frame": "",
                           "timeline_end_frame": "", "details": str(exc)[:240]})
            continue
        desired = item["end"] - item["start"]
        length = min(desired, meta["frames"])
        if length <= 0:
            report.append({**report_base, "action": "SKIP_ZERO", "timeline_start_frame": "",
                           "timeline_end_frame": "", "details": "Zero available length"})
            continue
        picked.append({**item, "meta": meta, "length": length, "source_in": 0})

    # Prevent overlapping clips within a camera track; if rounded intervals collide,
    # trim the later clip's head, keeping the original timeline mapping intact.
    result = []
    for cam in ("1", "2"):
        prev_end = 0
        for e in sorted((e for e in picked if e["camera"] == cam),
                        key=lambda e: (e["start"], e["row"])):
            overlap = max(0, prev_end - e["start"])
            adj_start = e["start"] + overlap
            length = e["length"] - overlap
            if length <= 0:
                report.append({"timeline": label, "camera": cam, "filename": e["output"],
                               "original_start_frame": e["start"], "original_end_frame": e["end"],
                               "rescue_status": e["status"], "action": "SKIP_OVERLAP",
                               "timeline_start_frame": "", "timeline_end_frame": "",
                               "details": "Entire clip overlaps previous segment"})
                continue
            clipped = {**e, "timeline_start": adj_start, "timeline_end": adj_start + length,
                       "source_in": overlap, "length": length}
            result.append(clipped)
            prev_end = clipped["timeline_end"]
            note = []
            if length < (e["end"] - e["start"]):
                note.append("shorter than manifest span")
            if overlap:
                note.append(f"trimmed {overlap} leading frame(s) because of overlap")
            if e["status"] != "VIDEO_DECODE_PASS":
                note.append("NOT video-decode-verified")
            if e["meta"]["audio"]:
                note.append("audio not separately verified")
            report.append({"timeline": label, "camera": cam, "filename": e["output"],
                           "original_start_frame": e["start"], "original_end_frame": e["end"],
                           "rescue_status": e["status"], "action": "PLACED",
                           "timeline_start_frame": clipped["timeline_start"],
                           "timeline_end_frame": clipped["timeline_end"],
                           "details": "; ".join(note)})
    return result


def file_element(clip_item: ET.Element, entry: dict, fid: str, include_audio_description: bool) -> None:
    mediafile = make(clip_item, "file", id=fid)
    make(mediafile, "name", entry["path"].name)
    make(mediafile, "pathurl", file_uri(entry["path"]))
    rate(mediafile)
    make(mediafile, "duration", entry["meta"]["frames"])
    med = make(mediafile, "media")
    videomed = make(med, "video")
    make(videomed, "duration", entry["meta"]["frames"])
    sample = make(videomed, "samplecharacteristics")
    rate(sample)
    make(sample, "width", 1920)
    make(sample, "height", 1080)
    make(sample, "anamorphic", "FALSE")
    make(sample, "pixelaspectratio", "square")
    make(sample, "fielddominance", "none")
    if include_audio_description and entry["meta"]["audio"]:
        am = make(med, "audio")
        ac = make(am, "samplecharacteristics")
        make(ac, "samplerate", entry["meta"]["sample_rate"])
        make(ac, "depth", 16)
        make(am, "channelcount", entry["meta"]["channels"])
        if entry["meta"]["channels"] == 2:
            make(am, "layout", "stereo")


def add_clip(track: ET.Element, entry: dict, fid: str, cid: str, mediatype: str,
             *, include_audio_description: bool) -> None:
    e = make(track, "clipitem", id=cid)
    name = entry["path"].name
    if entry["status"] != "VIDEO_DECODE_PASS":
        name = "REVIEW_" + name
    make(e, "name", name)
    make(e, "duration", entry["meta"]["frames"])
    rate(e)
    make(e, "start", entry["timeline_start"])
    make(e, "end", entry["timeline_end"])
    make(e, "in", entry["source_in"])
    make(e, "out", entry["source_in"] + entry["length"])
    make(e, "enabled", "TRUE")
    if mediatype == "video":
        make(e, "anamorphic", "FALSE")
        make(e, "alphatype", "none")
        file_element(e, entry, fid, include_audio_description)
    else:
        # Reuse the media file description from the video clip; FCP7 XML permits
        # file id references for corresponding audio/video clipitems.
        make(e, "file", id=fid)
    s = make(e, "sourcetrack")
    make(s, "mediatype", mediatype)
    make(s, "trackindex", 1)


def make_timeline(path: Path, entries: list[dict], *, with_audio: bool,
                  name: str, duration: int) -> None:
    root = ET.Element("xmeml", version="5")
    seq = make(root, "sequence", id="sequence-atem-iso-rescue")
    make(seq, "name", name)
    make(seq, "duration", duration)
    rate(seq)
    tc = make(seq, "timecode")
    rate(tc)
    make(tc, "string", "00:00:00:00")
    make(tc, "frame", 0)
    make(tc, "displayformat", "NDF")
    media = make(seq, "media")
    video = make(media, "video")
    add_video_format(video)
    camera_entries = {cam: [e for e in entries if e["camera"] == cam]
                      for cam in ("1", "2")}
    file_ids = {}
    for cam in ("1", "2"):
        track = make(video, "track")
        for ix, entry in enumerate(camera_entries[cam], start=1):
            fid = f"file_cam{cam}_{ix}"
            cid = f"video_cam{cam}_{ix}"
            file_ids[(cam, entry["row"])] = fid
            add_clip(track, entry, fid, cid, "video", include_audio_description=with_audio)
        make(track, "enabled", "TRUE")
        make(track, "locked", "FALSE")
    if with_audio:
        audio = make(media, "audio")
        fmt = make(audio, "format")
        sc = make(fmt, "samplecharacteristics")
        make(sc, "depth", 16)
        make(sc, "samplerate", 48000)
        outputs = make(audio, "outputs")
        outgroup = make(outputs, "group")
        make(outgroup, "index", 1)
        make(outgroup, "numchannels", 2)
        for cam in ("1", "2"):
            track = make(audio, "track")
            for ix, entry in enumerate(camera_entries[cam], 1):
                if not entry["meta"]["audio"]:
                    continue
                add_clip(track, entry, file_ids[(cam, entry["row"])],
                         f"audio_cam{cam}_{ix}", "audio", include_audio_description=True)
            make(track, "enabled", "TRUE")
            make(track, "locked", "FALSE")
    tree = ET.ElementTree(root)
    # Pretty-printing is optional (ElementTree.indent is unavailable on Python 3.8).
    if hasattr(ET, "indent"):
        ET.indent(tree, space="  ")
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    # Apple's interchange format convention includes a DOCTYPE declaration.
    marker = b'?>\n'
    if marker not in body[:120]:
        raise ValueError("Unexpected XML declaration")
    body = body.replace(marker, marker + b'<!DOCTYPE xmeml>\n', 1)
    path.write_bytes(body)
    # Check that the generated XML parses and has exactly 2 video tracks.
    verify = ET.parse(path)
    if len(verify.findall("./sequence/media/video/track")) != 2:
        raise ValueError("Generated XML video track count incorrect")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path,
                    default=Path.home() / "Desktop/ATEM_RESCUE/RECOVERED",
                    help="RECOVERED folder containing recovery_manifest.csv")
    ap.add_argument("--duration-seconds", default="0",
                    help="full original recording timeline length")
    args = ap.parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"ERROR: folder not found: {root}")
    rows, warnings = read_manifest(root)
    cache = {}
    report = []
    pass_entries = prepare_entries(rows, VALID_STATUSES, cache=cache,
                                   report=report, label="verified")
    review_entries = prepare_entries(rows, REVIEW_STATUSES, cache=cache,
                                     report=report, label="review_all")
    if not pass_entries and not review_entries:
        sys.exit("ERROR: No recovered clips could be placed. Check manifest statuses and paths.")
    duration = max(frame_number(args.duration_seconds),
                   max((x["timeline_end"] for x in review_entries), default=0))
    verified_video = root / "ATEM_ISO_VERIFIED_VIDEO.xml"
    verified_av = root / "ATEM_ISO_VERIFIED_WITH_AUDIO.xml"
    review_xml = root / "ATEM_ISO_REVIEW_ALL.xml"
    report_path = root / "ATEM_ISO_TIMELINE_REPORT.csv"
    for dest in (verified_video, verified_av, review_xml, report_path):
        if dest.exists():
            raise ValueError("Output exists; refusing overwrite: " + str(dest))
    make_timeline(verified_video, pass_entries, with_audio=False,
                  name="ATEM ISO RESCUE - VERIFIED VIDEO - CAM1 V1 CAM2 V2",
                  duration=duration)
    make_timeline(verified_av, pass_entries, with_audio=True,
                  name="ATEM ISO RESCUE - VERIFIED WITH AUDIO - CAM1 V1 CAM2 V2",
                  duration=duration)
    make_timeline(review_xml, review_entries, with_audio=False,
                  name="ATEM ISO RESCUE - ALL RECOVERED REVIEW - CAM1 V1 CAM2 V2",
                  duration=duration)
    with report_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timeline", "camera", "filename", "original_start_frame",
                                              "original_end_frame", "rescue_status", "action",
                                              "timeline_start_frame", "timeline_end_frame", "details"])
        writer.writeheader()
        writer.writerows(report)
    for message in warnings:
        print("WARNING:", message)
    print("\n=== XML TIMELINES READY ===")
    print(f"Timeline: {duration / FPS:.3f} seconds ({duration} frames) at {FPS} fps")
    for cam in ("1", "2"):
        clips = [e for e in pass_entries if e["camera"] == cam]
        seconds = sum(e["length"] for e in clips) / FPS
        print(f"Verified CAM {cam}: {len(clips)} clips, {seconds / 60:.2f} min (individual clip sum)")
    print("Video only:      ", verified_video)
    print("Video + audio:   ", verified_av)
    print("All for review:  ", review_xml)
    print("Detailed report: ", report_path)
    print("\nOriginal files and recovered MP4 clips remain unchanged.")
    print("IMPORTANT: the source audio has NOT been separately verified; inspect sync in Resolve.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        sys.exit("ERROR: " + str(exc))