# ATEM Video Restore

**Non-destructive recovery toolkit for damaged Blackmagic ATEM Mini Pro ISO MP4 recordings, with DaVinci Resolve XML timeline reconstruction.**

This toolkit was developed during a real recovery of a Makovecz Imre lecture. It rescued usable H.264 segments from damaged camera ISO and program recordings, then rebuilt their original recording-time positions in Resolve. **It cannot recreate overwritten or zeroed video bytes.**

## What it does

1. **Inspect damage:** sample all-zero regions without altering a recording (`atem_zero_scan.py`).
2. **Repair a zeroed MP4 opening:** use an intact, structurally compatible ATEM MP4 as a reference, on a **new copy** (`atem_mp4_header_repair.py`).
3. **Salvage camera ISO video:** check length-prefixed H.264 samples and complete GOPs; stream-copy keyframe-aligned runs into independent MP4 clips; decode-verify them (`atem_iso_rescue.py`).
4. **Recreate camera timelines:** place verified CAM1/CAM2 clips at the correct original timestamp in Final Cut Pro 7 XML for Resolve (`atem_resolve_timeline.py`).
5. **Fix Resolve timecode linking:** read real MP4 timecodes and emit a corrected timeline plus a single-clip trial (`atem_resolve_timecode_repair.py`).
6. **Salvage the program recording:** same guarded recovery pipeline, separate outputs (`atem_program_rescue.py`).
7. **Build a three-video-track Resolve timeline:** CAM1 V1, CAM2 V2, PROGRAM V3; create a coverage report and an optional program guide-audio track (`atem_resolve_three_track.py`).

**Scope:** tested workflow targets **H.264/AVCC, 1920×1080, 25 fps, unfragmented ATEM-style MP4 files** and DaVinci Resolve's FCP7 XML import. Other formats, frame rates and containers are not claimed to work. Requires **Python 3.10+**, `ffmpeg`, `ffprobe` (available with `brew install ffmpeg` on macOS) and DaVinci Resolve for timeline import. No Python third-party packages are required.

## Safety first

- **Stop recording to the affected SSD.** Avoid Disk Utility repairs, reformatting or any operations that write to it until a separate forensic/byte-for-byte copy has been made.
- **Work on verified copies on a different drive.** The scripts open inputs read-only, but no script can protect data against an already-failing disk or a user-supplied output path that targets the original volume.
- **Keep an intact reference MP4 from the same ATEM recording mode and header layout.** The header fixer refuses unknown structures rather than inventing bytes.
- **Use a fresh job directory for each attempt.** Recovery CSV reports and Resolve XML outputs refuse to overwrite existing report files. Extracted MP4s are not overwritten either.
- **Video-only verification:** `VIDEO_DECODE_PASS` means FFmpeg decoded the extracted video without a reported error and the first packet matched the source. It is *not* a visual inspection, audio validation, or guarantee of frame-perfect continuity.
- Recovered media, log files and recording-specific XML/CSV are intentionally gitignored. **Do not upload copyrighted recordings or private paths/metadata.**

## Quick start (macOS)

Clone this repository and prepare a job folder on a **healthy disk**. All commands below assume an independent, pre-verified copy of each recording.

```bash
brew install ffmpeg
git clone https://github.com/sipeti/atem-video-restore.git
cd atem-video-restore
JOB="$HOME/ATEM_RESTORE_JOB"
mkdir -p "$JOB"
```

### 1. Assess files

```bash
python3 scripts/atem_zero_scan.py \
  "$JOB/CAM1_original.mp4" "$JOB/CAM2_original.mp4" "$JOB/PROGRAM_original.mp4"
```

A low all-zero percentage does **not** prove a file is healthy. This is sparse sampling, not exhaustive integrity checking.

### 2. If needed, restore the MP4 header on a copy

If `ffprobe` says `moov atom not found` but an intact `moov` atom survives at EOF, and the beginning is entirely zeroed, try:

```bash
python3 scripts/atem_mp4_header_repair.py \
  --damaged "$JOB/CAM2_original.mp4" \
  --reference "$JOB/CAM1_original.mp4" \
  --output "$JOB/CAM2_header_fixed.mp4"
```

**Do not use an arbitrary MP4 as reference:** it must have the **same opening atom layout** (including `wide`, `mdat` length width, etc.). The fixer checks the reference's `mdat`/`moov` boundary, validates an EOF `moov` in the damaged file and requires the damaged header area to be completely zero. It writes a **new file only** and can use APFS cloning on macOS.

If the file opens in `ffprobe` already, skip this step. Do not repeatedly rewrite the header, and never repair in-place.

### 3. Recover the two ISO streams

```bash
python3 scripts/atem_iso_rescue.py \
  --root "$JOB" --cam1 "$JOB/CAM1_original.mp4" \
  --cam2 "$JOB/CAM2_header_fixed.mp4"
```

The run writes `RECOVERED/CAM1/`, `RECOVERED/CAM2/`, `RECOVERED/recovery_manifest.csv` and diagnostics. Use `--scan-only` before expensive extraction, or `--camera 1` / `--camera 2` to process only one source. For a single source, supply the matching `--cam1` or `--cam2` parameter. Output files contain the original time interval in their names and in the manifest.

**Warning:** long inputs may require substantial disk space and time. `caffeinate -i python3 ...` is useful on a Mac.

### 4. Rebuild the CAM1 + CAM2 Resolve timeline

```bash
python3 scripts/atem_resolve_timeline.py \
  --root "$JOB/RECOVERED" --duration-seconds 3200.320
python3 scripts/atem_resolve_timecode_repair.py --root "$JOB/RECOVERED"
```

Replace `3200.320` with the **original recording duration in seconds**, not the sum of the rescued segments. Without `--duration-seconds`, the timeline ends at the last known extracted position.

The timecode tool uses the real source MP4 timecodes, then creates:

- `ATEM_ISO_CAM1_ONECLIP_TCFIX.xml` — the safest first import test.
- `ATEM_ISO_VERIFIED_WITH_AUDIO_TCFIX.xml` — all decode-verified ISO clips, with guide audio.
- `ATEM_ISO_VERIFIED_WITH_AUDIO_TIMECODE_DIAG.csv` — reconciliation of source/sequence metadata.

**In Resolve:** `File > Import > Timeline > Import AAF, EDL, XML` (not *Import Project*). Start with the one-clip XML in an empty **25 fps** project. For the full XML, leave **Automatically import source clips into media pool** enabled. If Resolve reports **"timecode extents do not match"**, the original XML was likely missing the MP4's real source timecode; use the `_TCFIX.xml` version.

### 5. Recover the optional Program MP4

If the program file is damaged, first inspect and, if necessary, header-repair it **using a compatible program-recording reference** (not necessarily a camera ISO file). Then run:

```bash
python3 scripts/atem_program_rescue.py \
  --root "$JOB" --input "$JOB/PROGRAM_header_fixed.mp4"
```

The output is isolated under `RECOVERED_PROGRAM/`, with a `PROGRAM/` clip folder, a separate manifest and suspect-packet report. This does **not** modify the camera rescue.

### 6. Add PROGRAM on V3

```bash
python3 scripts/atem_resolve_three_track.py --root "$JOB"
```

This consumes the **timecode-fixed** dual-camera XML and the Program recovery manifest. The generator validates each Program clip's real timecode and produces:

- `ATEM_ISO_PROGRAM_ONECLIP_TCFIX.xml` — one-clip Program test.
- `ATEM_ISO_CAM1_CAM2_PROGRAM_V3_TCFIX.xml` — complete three-video-track sequence.
- `ATEM_ISO_CAM1_CAM2_PROGRAM_V3_WITH_AUDIO_TCFIX.xml` — optional guide audio on A3.
- `ATEM_ISO_THREE_SOURCE_COVERAGE.csv` — original-time coverage and missing spans.

First import the **one-clip Program test** into Resolve. If it links correctly, import the full three-track XML. V3 is the top video layer: disable that track when you want to inspect the underlying camera ISO footage. **Program audio is not separately decode-verified.**

## Troubleshooting

| Symptom | Meaning / next step |
| --- | --- |
| `moov atom not found` | The opening MP4 header may be damaged, or `moov` is genuinely gone. Run the header diagnostic; don't assume it is repairable. |
| Entire CAM1 track offline, CAM2 online | Check source timecode. An MP4 may start at `18:03:58:07` while another starts at zero. Use `atem_resolve_timecode_repair.py`. |
| `Invalid NAL unit size` | The video payload may contain overwritten/zeroed regions. Header repair alone is insufficient; scan and extract healthy GOPs. |
| `VIDEO_DECODE_FAIL` | The extracted clip did not pass the video decoder validation; do not treat it as verified. |
| Some sections have no picture | Multiple sources can be damaged at the same original time. Missing bytes cannot be inferred. |
| Unexpected script refusal on existing output | Deliberate fail-closed behavior. Use a fresh `--root` and preserve the earlier attempt. |

## Tests

```bash
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -v
```

Synthetic unit tests exercise the byte scanner, AVCC integrity gate, GOP health check, source timecode injection, timeline coverage and a real small MP4 header-restoration round trip (when FFmpeg is installed). **The real recovery was validated against recorded clips and Resolve separately; the CI tests do not simulate a failing SSD or guarantee identical behavior in every Resolve release.**

See [Recovery playbook](docs/RECOVERY_PLAYBOOK.md) and the [real-world recovery note](docs/CASE_STUDY.md).

## License

[MIT](LICENSE). Contributions and reproducible, **non-copyrighted** synthetic damaged-file test cases are welcome.