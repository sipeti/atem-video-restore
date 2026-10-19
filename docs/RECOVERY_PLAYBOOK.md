# Recovery playbook

This is an **operational checklist**, not a filesystem repair tool.

## Decision sequence

1. Stop writes to the suspect SSD. Document device model, cables, ATEM firmware, recording mode, filesystem, relevant start/stop times and any on-device error indications.
2. If the drive itself may be physically failing, obtain a professional disk image before running repeated reads. Otherwise copy each suspect video to a healthy volume and verify with `cmp` or checksums.
3. Use `ffprobe` and `atem_zero_scan.py` on the *copies*. Sparse zero sampling is a clue, never conclusive proof.
4. Look for a valid tail `moov` box. If the first header bytes are all zero and a compatible reference exists, use `atem_mp4_header_repair.py`. Do not borrow a header from a different layout; fail closed when in doubt.
5. Run the H.264/AVCC scanner. It validates each indexed sample's NAL-unit lengths and rejects zeroed NAL payloads, then selects contiguous complete GOPs. `ffmpeg -c copy` extracts and decode-checks them. Keep manifests and warnings.
6. Create the ISO XML using original timestamps, **not** clip concatenation. Insert actual MP4 source timecode metadata before importing into Resolve. Test one clip first.
7. If a Program MP4 exists, repeat the safe rescue into a separate folder; calculate the extra unique coverage of its surviving sections.
8. Assemble CAM1 on V1, CAM2 on V2, PROGRAM on V3 in Resolve. Check the beginning, middle, end and several cut boundaries. Confirm audio separately and export a Resolve project (`.drp`) to the healthy disk.
9. Save a recovery report separately. Do not publish the recordings, original paths, or manifests unless you have explicit rights and have checked for sensitive metadata.

## How to read a manifest

`original_start_s`, `original_end_s`: placement relative to the original recording start. `status=VIDEO_DECODE_PASS`: video passed AVCC sample screening, stream-copy first-packet alignment and FFmpeg decode validation. `status=START_ALIGNMENT_UNCERTAIN`: the export may start on a different GOP than requested and **must not** be assumed correctly aligned. `SCAN_ONLY`, `LOW_SPACE` and `EXTRACT_FAILED` are not verified recovered clips.

Guide audio can exist even when the source AAC is damaged; the current pipeline does **not** separately validate AAC continuity or lip-sync. Use an independent reference recording when possible.

## Root-cause caution

Similar zeroed regions across Program and ISO recordings suggest a shared recording/storage-path event, **not** proof of a specific component fault. Possible causes include the ATEM recording path, USB negotiation/link issues, SSD/controller/firmware behavior and filesystem/host interactions. Controlled testing on expendable media and device logs are needed to distinguish them. Do not conclude an SSD is defective solely from these sample statistics.

## Multi-camera use (Mini Extreme ISO)

The ISO recovery command supports CAM1..CAM8 and scans only the immediate children
of one selected input directory. Filenames like `event CAM 8 01.mp4` are detected;
missing cameras are allowed, and arbitrary recorded subsets retain their identities
on separate Resolve video tracks (e.g. CAM1, CAM4 and CAM8 become V1, V2 and V3).
Choose `--part 02` for later ATEM parts *only after confirming that the part belongs
to the intended recording session*. Use explicit `--cam CAMn=/path` mapping for
header-repaired inputs. Detecting the source is not evidence that it decodes.

After rebuilding `ATEM_ISO_VERIFIED_WITH_AUDIO.xml`, always run the timecode
repair; a source may have a real start timecode such as `18:03:58:07` even when
a recovered MP4's first video sample starts at relative zero. A one-clip Resolve
import test is the first acceptance gate. The PROGRAM overlay, if available, is
placed on V(number_of_cameras+1), and a coverage CSV includes all camera sources.

For a multi-session recording, do not concatenate `01` and `02` manifests without
explicit timeline offsets and session boundaries. The automation intentionally
fails closed on multiple filename prefixes or duplicate CAMn inputs.
