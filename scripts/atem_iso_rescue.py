#!/usr/bin/env python3
"""Read-only source scanner and loss-minimizing ATEM ISO MP4 segment recovery.

Camera inputs are selected explicitly or autodiscovered from one ATEM recording
part in a folder containing verified copies. Supports CAM1..CAM8.
Uses ffprobe's MP4 sample index, checks H.264/AVCC sample integrity,
extracts only complete keyframe-aligned runs, then decodes each output
as a check. CSV keeps original recording timestamps for each segment.

This cannot restore overwritten bytes. An AVCC-valid packet may still
contain damaged compressed video: validation is necessary, not sufficient.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional
from atem_multicam import select_sources

GIB = 1024 ** 3
ZERO_RUN = b"\0" * 4096
FRAME = 1.0 / 25.0


@dataclass
class Packet:
    pos: int
    size: int
    pts: float
    dts: float
    duration: float
    key: bool
    ok: bool = False
    reason: str = ""


def run(command, log_path=None):
    if log_path:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            return subprocess.run(command, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=log,
                                  check=False).returncode
    return subprocess.run(command, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=False)


def ffprobe_packets(path):
    cmd = ["ffprobe", "-hide_banner", "-v", "quiet", "-select_streams", "v:0",
           "-show_packets", "-show_entries",
           "packet=pos,size,pts_time,dts_time,duration_time,flags",
           "-of", "json", str(path)]
    result = run(cmd)
    if result.returncode:
        raise RuntimeError("ffprobe failed with code %s" % result.returncode)
    try:
        raw = json.loads(result.stdout)
    except Exception as exc:
        raise RuntimeError("ffprobe did not produce valid JSON: %s" % exc) from exc
    packets = []
    for item in raw.get("packets", []):
        try:
            pos = int(item["pos"])
            size = int(item["size"])
            pts = float(item["pts_time"])
            dts = float(item.get("dts_time", item["pts_time"]))
            dur = float(item.get("duration_time", FRAME))
            key = "K" in item.get("flags", "")
            packets.append(Packet(pos, size, pts, dts, dur or FRAME, key))
        except (KeyError, ValueError, TypeError):
            continue
    if not packets:
        raise RuntimeError("No indexable video packets were returned")
    return packets


def validate_avcc(data):
    if len(data) < 5:
        return "short-packet"
    if not any(data):
        return "zero-packet"
    if ZERO_RUN in data:
        return "zero-run-inside-packet"
    pos = 0
    found_slice = False
    found_any = False
    while pos < len(data):
        if pos + 4 > len(data):
            return "truncated-nal-length"
        length = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if length == 0 or length > len(data) - pos:
            return "invalid-nal-length"
        header = data[pos]
        nal_type = header & 31
        if (header & 128) or not (1 <= nal_type <= 23):
            return "invalid-nal-header"
        found_any = True
        if nal_type in (1, 5):
            found_slice = True
        pos += length
    if not found_any or not found_slice:
        return "no-vcl-slice"
    return ""


def validate_packets(path, packets, progress):
    bad_counts = {}
    n = len(packets)
    file_size = path.stat().st_size
    with open(path, "rb", buffering=0) as f:
        for i, p in enumerate(packets, 1):
            if p.pos < 0 or p.size <= 0 or p.pos + p.size > file_size:
                p.reason = "outside-file"
            elif p.size > 25_000_000:
                p.reason = "suspicious-packet-size"
            else:
                f.seek(p.pos)
                data = f.read(p.size)
                p.reason = validate_avcc(data)
            p.ok = not p.reason
            if p.reason:
                bad_counts[p.reason] = bad_counts.get(p.reason, 0) + 1
            if progress and i % 10000 == 0:
                print(f"    checked {i:,}/{n:,} video packets", flush=True)
    return bad_counts


def group_gops(packets):
    groups = []
    active = []
    for p in packets:
        if p.key:
            if active:
                groups.append(active)
            active = [p]
        elif active:
            active.append(p)
    if active:
        groups.append(active)
    return groups


def gop_healthy(gop):
    if not gop or not gop[0].key or any(not p.ok for p in gop):
        return False
    for prev, nxt in zip(gop, gop[1:]):
        # Detect gaps within a nominal 25-fps GOP by decode timestamp.
        dt = nxt.dts - prev.dts
        if not (-0.001 <= dt <= 0.09):
            return False
    return True


def gop_bounds(gop):
    lo = min(p.pts for p in gop)
    hi = max(p.pts + p.duration for p in gop)
    return lo, hi


def build_segments(groups, min_seconds, max_seconds):
    segments, dropped = [], []
    current = []
    previous_end_dts = None

    def flush():
        nonlocal current
        if not current:
            return
        packets = [p for group in current for p in group]
        start = min(p.pts for p in packets)
        end = max(p.pts + p.duration for p in packets)
        item = (start, end, len(packets), sum(p.size for p in packets), current[0][0])
        if end - start >= min_seconds:
            segments.append(item)
        else:
            dropped.append(item)
        current = []

    for group in groups:
        valid = gop_healthy(group)
        if not valid:
            flush()
            previous_end_dts = None
            continue
        next_start, next_end = gop_bounds(group)
        start_dts = group[0].dts
        current_start = min(p.pts for g in current for p in g) if current else 0
        # GOPs must be immediately adjacent on the decode timeline.
        continuous = (previous_end_dts is not None and
                      abs(start_dts - previous_end_dts) <= 0.09)
        if current and (not continuous or next_end - current_start > max_seconds):
            flush()
        current.append(group)
        previous_end_dts = group[-1].dts + group[-1].duration
    flush()
    return segments, dropped


def time_label(t):
    t = max(0., t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    sec = t % 60
    return f"{h:02}-{m:02}-{sec:06.3f}"


def fmt_clock(t):
    return time_label(t).replace("-", ":", 2)


def disk_space_ok(path, estimate_bytes, reserve_gib):
    available = shutil.disk_usage(path).free
    return available - estimate_bytes > reserve_gib * GIB, available


def ffmpeg_copy(source, start, end, output, log):
    # Seek from indexed keyframes. Input -ss is fast; negative timestamp
    # adjustment is handled by MP4 muxer, and decoded output is verified.
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
           "-n", "-ss", f"{start:.6f}", "-i", str(source),
           "-t", f"{end - start:.6f}",
           "-map", "0:v:0", "-map", "0:a:0?", "-dn", "-sn",
           "-c", "copy", "-movflags", "+faststart", str(output)]
    return run(cmd, log)


def verify_first_packet(source, output, first_pkt):
    # A stream-copy cut can silently seek to an earlier keyframe. Comparing
    # the first actual encoded sample guards original-timecode mapping.
    cmd = ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
           "-read_intervals", "%+#1", "-show_packets",
           "-show_entries", "packet=pos,size,flags", "-of", "json", str(output)]
    result = run(cmd)
    if result.returncode:
        return False, "ffprobe-export-failed"
    try:
        packets = json.loads(result.stdout).get("packets", [])
        first = packets[0]
        out_pos, out_size = int(first["pos"]), int(first["size"])
        if out_size != first_pkt.size or "K" not in first.get("flags", ""):
            return False, "first-keyframe-different"
        with open(source, "rb") as src, open(output, "rb") as dst:
            src.seek(first_pkt.pos)
            dst.seek(out_pos)
            if src.read(first_pkt.size) != dst.read(out_size):
                return False, "first-packet-payload-different"
        return True, ""
    except (ValueError, KeyError, IndexError, OSError):
        return False, "first-packet-inspection-failed"


def decode_verify(path, log):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror",
           "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
           "-f", "null", "-"]
    code = run(cmd, log)
    # Some decoders warn on corrupted frames even when process reports success.
    return code == 0 and (not log.exists() or log.stat().st_size == 0)


def write_csv(path, rows, columns):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path.home() / "Desktop/ATEM_RESCUE")
    ap.add_argument("--camera", default="auto",
                    help="auto/all, both, or comma-separated camera IDs (e.g. 1,3,8)")
    ap.add_argument("--input-dir", type=Path,
                    help="directory of ATEM CAM 1..8 XX.mp4 copies for automatic detection")
    ap.add_argument("--part", default="01", help="source recording part/session suffix, default 01")
    ap.add_argument("--cam", action="append", default=[],
                    help="explicit source override CAMn=PATH, repeat for CAM1..CAM8")
    ap.add_argument("--cam1", type=Path, help="legacy explicit CAM1 source MP4")
    ap.add_argument("--cam2", type=Path, help="legacy explicit CAM2 source MP4")
    ap.add_argument("--scan-only", action="store_true", help="scan and CSV only, no video extraction")
    ap.add_argument("--min-seconds", type=float, default=1., help="minimum clip length")
    ap.add_argument("--max-seconds", type=float, default=180., help="maximum clip length")
    ap.add_argument("--reserve-gib", type=float, default=8.)
    ap.add_argument("--limit-clips", type=int, default=0, help="extract at most N clips per camera; 0=all")
    ap.add_argument("--no-verify", action="store_true", help="skip full decode verification")
    args = ap.parse_args()
    root = args.root.expanduser().resolve()
    dest = root / "RECOVERED"
    sources = select_sources(root, camera=args.camera, input_dir=args.input_dir,
                             part=args.part, mappings=args.cam,
                             cam1=args.cam1, cam2=args.cam2)
    wanted = list(sources)
    for report in ("recovery_manifest.csv", "scan_summary.csv") + tuple(
            "CAM%s_suspect_packets.csv" % cam for cam in wanted):
        if (dest / report).exists():
            sys.exit("ERROR: existing report; choose fresh --root: " + report)
    dest.mkdir(exist_ok=True, parents=True)
    for exe in ("ffmpeg", "ffprobe"):
        if not shutil.which(exe):
            sys.exit(f"ERROR: {exe} not found. Install with: brew install ffmpeg")
    if args.min_seconds < 0 or args.max_seconds <= 0:
        sys.exit("ERROR: Invalid duration options")


    manifest_rows = []
    summary_rows = []
    for cam in wanted:
        source = sources[cam]
        print(f"\n=== CAM {cam}: scanning {source.name} ===", flush=True)
        packets = ffprobe_packets(source)
        print(f"  indexed packets: {len(packets):,}", flush=True)
        bad = validate_packets(source, packets, progress=True)
        healthy = sum(p.ok for p in packets)
        print(f"  structurally valid H.264 samples: {healthy:,}/{len(packets):,}", flush=True)
        print(f"  suspect sample reasons: {bad}", flush=True)
        groups = group_gops(packets)
        ok_groups = sum(gop_healthy(group) for group in groups)
        segments, tiny = build_segments(groups, args.min_seconds, args.max_seconds)
        print(f"  good GOPs: {ok_groups:,}/{len(groups):,}")
        print(f"  clip candidates: {len(segments)}; short omitted spans: {len(tiny)}")
        print(f"  candidate time: {sum(end-start for start,end,_,_,_ in segments)/60:.1f} min", flush=True)

        camera_folder = dest / f"CAM{cam}"
        camera_folder.mkdir(parents=True, exist_ok=True)
        scan_rows = [{"time_s": f"{p.pts:.3f}", "file_offset": p.pos,
                      "size": p.size, "keyframe": int(p.key), "status": "ok" if p.ok else p.reason}
                     for p in packets if not p.ok]
        write_csv(dest / f"CAM{cam}_suspect_packets.csv", scan_rows,
                  ["time_s", "file_offset", "size", "keyframe", "status"])

        for i, (start, end, n_pkts, bytes_sum, first_pkt) in enumerate(segments, 1):
            name = f"CAM{cam}_{time_label(start)}_to_{time_label(end)}.mp4"
            output = camera_folder / name
            log = camera_folder / (name + ".extract.log")
            verify_log = camera_folder / (name + ".verify.log")
            row = {"camera": cam, "source": source.name, "original_start_s": f"{start:.3f}",
                   "original_end_s": f"{end:.3f}", "original_start_time": fmt_clock(start),
                   "original_end_time": fmt_clock(end), "expected_duration_s": f"{end-start:.3f}",
                   "video_packets": n_pkts, "est_video_bytes": bytes_sum,
                   "output": str(output.relative_to(dest)), "status": "NOT_RUN", "bytes_written": 0}
            manifest_rows.append(row)
            if args.scan_only:
                row["status"] = "SCAN_ONLY"
                continue
            if args.limit_clips and i > args.limit_clips:
                row["status"] = "CLIP_LIMIT"
                continue
            # Leave substantial disk reserve; estimated bytes exclude audio & mux metadata.
            good_space, free_bytes = disk_space_ok(dest, int(bytes_sum * 1.15), args.reserve_gib)
            if not good_space:
                row["status"] = "LOW_SPACE"
                print(f"  stopping extraction (free {free_bytes / GIB:.1f} GiB)", flush=True)
                continue
            if output.exists():
                row["status"] = "EXISTS_NOT_OVERWRITTEN"
            else:
                print(f"  writing {i}/{len(segments)}: {name}", flush=True)
                code = ffmpeg_copy(source, start, end, output, log)
                if code != 0 or not output.exists() or output.stat().st_size == 0:
                    row["status"] = "EXTRACT_FAILED"
                    continue
                row["status"] = "EXTRACTED"
            row["bytes_written"] = output.stat().st_size
            aligned, alignment_reason = verify_first_packet(source, output, first_pkt)
            if not aligned:
                row["status"] = "START_ALIGNMENT_UNCERTAIN"
                print(f"    WARNING: {name}: {alignment_reason}", flush=True)
            elif args.no_verify:
                row["status"] = "START_ALIGNED_NOT_DECODED"
            elif decode_verify(output, verify_log):
                row["status"] = "VIDEO_DECODE_PASS"
            else:
                row["status"] = "VIDEO_DECODE_FAIL"
                print(f"    WARNING: decode failed for {name}", flush=True)

        summary_rows.append({"camera": cam, "source": source.name,
                             "total_video_packets": len(packets), "valid_avcc_packets": healthy,
                             "total_gops": len(groups), "valid_gops": ok_groups,
                             "candidate_clips": len(segments),
                             "candidate_seconds": f"{sum(e-s for s,e,_,_,_ in segments):.3f}",
                             "short_omitted": len(tiny), "packet_error_details": str(bad)})
        # Persist results after each camera so Ctrl+C does not lose completed camera.
        write_csv(dest / "recovery_manifest.csv", manifest_rows,
                  ["camera", "source", "original_start_s", "original_end_s",
                   "original_start_time", "original_end_time", "expected_duration_s",
                   "video_packets", "est_video_bytes", "output", "status", "bytes_written"])
        write_csv(dest / "scan_summary.csv", summary_rows,
                  ["camera", "source", "total_video_packets", "valid_avcc_packets",
                   "total_gops", "valid_gops", "candidate_clips", "candidate_seconds",
                   "short_omitted", "packet_error_details"])

    print("\n=== COMPLETE ===")
    print("Output:", dest)
    print("Manifest:", dest / "recovery_manifest.csv")
    print("Only VIDEO_DECODE_PASS clips are decode-verified for video.")
    print("Audio is included when present, but audio is not verified.")
    print("All source files remain unchanged.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Existing outputs and sources were not deleted.", file=sys.stderr)
        sys.exit(130)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)