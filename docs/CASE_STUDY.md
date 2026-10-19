# Field note: Makovecz Imre lecture recovery (2026)

The toolkit emerged while recovering damaged **Blackmagic ATEM Mini Pro ISO** recordings of a Makovecz Imre lecture, recorded to a **Samsung T7 Shield 2 TB** SSD. The problematic session was **1920×1080, 25 fps, 53 minutes 20.32 seconds** long.

The first recorded session's CAM1, CAM2 and PROGRAM MP4s contained substantial zeroed regions. One camera's original MP4 remained indexable; other files had a zeroed opening header but a surviving `moov` index at EOF. A later recording session's Program MP4 had no all-zero regions in the sparse sample. The evidence suggested a **session-specific shared recording/storage-path problem**, but did not isolate the exact failing component.

A safe header repair (on verified copies), H.264 packet/GOP screening and stream-copy extraction produced:

| Source | Video-decode-verified clips | Verified clip time (approx.) |
| --- | ---: | ---: |
| Camera 1 | 80 | 42:29 |
| Camera 2 | 79 | 43:05 |
| Program | 80 | 41:52 |

Using the original timestamps, the two camera sources together covered approximately **95.8%** of the recording timeline. Adding the Program source uniquely covered another **33 seconds**, for approximately **96.8% total timeline coverage** (about **51:38 / 53:20**). This is **timeline availability**, not an independently reviewed claim that 96.8% of frames/audio are perfect.

The most important Resolve interoperability discovery: recovered CAM1 MP4 segments retained a source timecode starting at `18:03:58:07`, while recovered CAM2 segments exposed `00:00:00:00`. XML imports that omitted the actual source timecode could leave the entire CAM1 track offline with a **timecode extents** error. Explicit source `<timecode>` metadata fixed a one-clip Resolve import test, and the workflow was continued using timecode-aware XML files.

No original recordings, extracted lecture clips, privately identifying paths or rescue logs are included in this repository. The tools are **best-effort incident-recovery utilities**, not a substitute for a clean recording, storage redundancy or a hardware RCA.