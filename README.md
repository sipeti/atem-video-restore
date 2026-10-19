# ATEM Video Restore

**Non-destructive recovery toolkit for Blackmagic ATEM Mini Pro ISO / ATEM Mini Extreme ISO recordings: automatically discover CAM1–CAM8, salvage valid H.264 GOPs and reconstruct DaVinci Resolve FCP7 XML timelines.**

This toolkit was developed during a real recovery of a Makovecz Imre lecture. It rescued usable H.264 segments from damaged camera ISO and program recordings, then rebuilt their original recording-time positions in Resolve. **It cannot recreate overwritten or zeroed video bytes.**

## What it does

1. **Inspect damage:** sample all-zero regions without altering a recording (`atem_zero_scan.py`).
2. **Repair a zeroed MP4 opening:** use an intact, structurally compatible ATEM MP4 as a reference, on a **new copy** (`atem_mp4_header_repair.py`).
3. **Salvage camera ISO video:** automatically discover up to eight ATEM ISO MP4s from a *single recording part*, or map arbitrary camera source paths with `--cam CAMn=PATH`; stream-copy healthy GOPs and decode-verify them (`atem_iso_rescue.py`).
4. **Recreate camera timelines:** place whichever CAM1–CAM8 clips were actually rescued in order on V1..Vn (one video track and optional guide-audio track per camera), at the original recording timestamps (`atem_resolve_timeline.py`).
5. **Fix Resolve timecode linking:** read real MP4 timecodes and emit a corrected timeline plus a single-clip trial (`atem_resolve_timecode_repair.py`).
6. **Salvage the program recording:** same guarded recovery pipeline, separate outputs (`atem_program_rescue.py`).
7. **Overlay PROGRAM above all cameras:** PROGRAM goes to V(n+1): V3 for two cameras, V9 for eight. Coverage CSV columns adapt to the available sources, including non-contiguous cameras (`atem_resolve_three_track.py`, historical script name).

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

### 3. Recover 1–8 ISO streams (auto detection or explicit mapping)

For ordinary ATEM exports, place **verified copies** of a single session's video ISO files in one folder (e.g. `show CAM 1 01.mp4`, ... `show CAM 8 01.mp4`):

```bash
python3 scripts/atem_iso_rescue.py \
  --root "$JOB" --input-dir "$JOB/forrasmasolat" --camera auto --scan-only
# Review the scan-only report, then run with a new, fresh --root for extraction:
python3 scripts/atem_iso_rescue.py \
  --root "$JOB_EXTRACT" --input-dir "$JOB/forrasmasolat" --camera auto
```

Autodiscovery selects CAM1 through CAM8 **only for `01` (default)**, with a single shared recording filename prefix. Use `--part 02` for a different physical recording part, but **do not combine multiple ATEM start/stop sessions in one timeline**: recover them separately. A missing camera number is fine; an ambiguous or mixed recording is a hard error.

For repaired or unusually named source copies, map cameras explicitly; these override autodiscovery for the specified camera:

```bash
python3 scripts/atem_iso_rescue.py --root "$JOB" \
  --cam CAM1="$JOB/CAM1_original.mp4" \
  --cam CAM2="$JOB/CAM2_header_fixed.mp4" \
  --cam CAM8="$JOB/CAM8_original.mp4"
```

`--camera 1,4,8` restricts the selection; `--camera both` and `--cam1`/`--cam2` remain supported for old two-camera workflows. Outputs are `RECOVERED/CAMn/`, `RECOVERED/recovery_manifest.csv`, and per-camera diagnostic CSV files. `--scan-only` generates reports **and refuses to overwrite them on a second run**; do the extraction under a new `--root` (or archive/move the scan-only output first).

**Warning:** long inputs may require substantial disk space and time. `caffeinate -i python3 ...` is useful on a Mac.

### 4. Rebuild the discovered ISO camera tracks in Resolve

```bash
python3 scripts/atem_resolve_timeline.py \
  --root "$JOB/RECOVERED" --duration-seconds 3200.320
python3 scripts/atem_resolve_timecode_repair.py --root "$JOB/RECOVERED"
```

Replace `3200.320` with the **original recording duration in seconds**, not the sum of the rescued segments. Without `--duration-seconds`, the timeline ends at the last known extracted position.

The timecode tool uses each camera's real source MP4 timecodes, then creates:

- `ATEM_ISO_CAMn_ONECLIP_TCFIX.xml` — first recovered camera's one-clip import test (CAM1 when present).
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

### 6. Add PROGRAM on V(n+1) (up to V9)

```bash
python3 scripts/atem_resolve_three_track.py --root "$JOB"
```

This consumes the **timecode-fixed** 1–8 camera XML and the Program recovery manifest. The generator validates each Program clip's real timecode and produces:

- `ATEM_ISO_PROGRAM_ONECLIP_TCFIX.xml` — one-clip Program test.
- `ATEM_ISO_WITH_PROGRAM_TCFIX.xml` — complete V1..V(n+1) sequence.
- `ATEM_ISO_WITH_PROGRAM_AUDIO_TCFIX.xml` — optional guide audio on A(n+1).
- `ATEM_ISO_MULTI_SOURCE_COVERAGE.csv` — columns for each present CAMn plus PROGRAM, including missing spans.

First import the **one-clip Program test** into Resolve. If it links correctly, import the full multitrack XML. The PROGRAM track is the top video layer (V(n+1)): disable that track when you want to inspect the underlying camera ISO footage. **Program audio is not separately decode-verified.**

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

Synthetic tests exercise CAM1–CAM8 autodiscovery (including ambiguous input rejection), the two-camera regression, sparse camera sets, eight-video-ISO + PROGRAM XML up to V9, a real CAM8 H.264 extraction, byte scanner, AVCC integrity gate, timecode injection and an MP4 header restoration round trip (when FFmpeg is installed). **The real recovery was validated against recorded clips and Resolve separately; the CI tests do not simulate a failing SSD or guarantee identical behavior in every Resolve release.**

See [Recovery playbook](docs/RECOVERY_PLAYBOOK.md) and the [real-world recovery note](docs/CASE_STUDY.md).

## License

[MIT](LICENSE). Contributions and reproducible, **non-copyrighted** synthetic damaged-file test cases are welcome.

## Current limitations

- **1080p25, H.264/AVCC only.** Extreme ISO support here means *up to eight 1080p25 H.264 camera sources*, not an assurance of all recording modes/codecs on every ATEM model.
- One **physical recording part** per invocation. The ATEM's `01` and `02` naming can signal either file rollover or a new recording session; `--part` selects which files, not an automatic timing stitcher.
- Program MP4 is a **separate recovery**; the optional overlay is built after camera recovery and source-timecode fixing.
- Each camera's video is decoded for acceptance; **audio still requires independent verification**. No corrupt byte reconstruction, hardware diagnostics or automated Resolve GUI import is provided.
