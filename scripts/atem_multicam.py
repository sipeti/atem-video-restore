"""Fail-closed ATEM ISO source selection and camera labels (CAM1 through CAM8).

Auto discovery considers only immediate files in one directory and a single
physical recording part (01 by default). Never silently mixes recording prefixes,
multiple files for the same camera, or separate ATEM recording sessions.
"""
import re
from pathlib import Path

CAMERA_IDS = tuple(str(i) for i in range(1, 9))
# Typical ATEM ISO: "My Show CAM 3 01.mp4"; no recursion, so generated clips
# inside RECOVERED/CAM3 are not mistaken for source recordings.
ATEM_FILE = re.compile(
    r"^(?P<prefix>.*?)\bCAM[ _-]*(?P<camera>[1-8])[ _-]+(?P<part>\d{2,})\.(?:mp4|mov)$",
    re.IGNORECASE,
)


def camera_ids(rows):
    """Sorted, distinct recovered camera IDs (PROGRAM is intentionally excluded)."""
    seen = {str(row['camera']) for row in rows if str(row['camera']) in CAMERA_IDS}
    return sorted(seen, key=int)


def discover_sources(input_dir, part='01'):
    """Return CAM ID -> file for a single physical part; reject mixed sessions."""
    folder = Path(input_dir).expanduser().resolve(strict=True)
    if not folder.is_dir():
        raise ValueError('Not a directory: %s' % folder)
    if not re.fullmatch(r'\d{2,}', str(part)):
        raise ValueError('Part must be a numeric recording suffix such as 01 or 02')
    found = {}
    prefixes = set()
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        m = ATEM_FILE.fullmatch(path.name)
        if m is None or int(m.group('part')) != int(part):
            continue
        cam = m.group('camera')
        if cam in found:
            raise ValueError('Ambiguous CAM%s sources (%s, %s). Use --cam CAM%s=PATH.' %
                             (cam, found[cam].name, path.name, cam))
        found[cam] = path
        prefixes.add(m.group('prefix').strip(' _-').casefold())
    if len(prefixes) > 1:
        raise ValueError('Multiple recording prefixes in %s; separate the recordings or use explicit --cam mappings' % folder)
    if not found:
        raise ValueError('No ATEM CAM1..CAM8 part %s MP4/MOV files found in %s' % (part, folder))
    return found


def parse_cam_mapping(raw):
    """Accept --cam 3=/path, --cam CAM3=/path or --cam 3:/path."""
    m = re.fullmatch(r'(?:CAM)?([1-8])\s*[:=]\s*(.+)', raw.strip(), re.IGNORECASE)
    if m is None:
        raise ValueError('Expected --cam CAM3=/path/to/CAM3.mp4; got %r' % raw)
    return m.group(1), Path(m.group(2)).expanduser()


def select_sources(root, camera='auto', input_dir=None, part='01', mappings=(), cam1=None, cam2=None):
    """Combine safe autodiscovery and explicit overrides; return sorted mappings.

    Existing --cam1/--cam2 remain supported for legacy two-camera invocations.
    --camera auto/all uses all available mappings, --camera both limits 1+2,
    otherwise a comma-separated list of requested camera numbers.
    """
    root = Path(root).expanduser().resolve()
    explicit = {}
    for cam, path in (('1', cam1), ('2', cam2)):
        if path is not None:
            explicit[cam] = Path(path)
    for raw in mappings:
        cam, path = parse_cam_mapping(raw)
        if cam in explicit and explicit[cam].expanduser().resolve() != path.expanduser().resolve():
            raise ValueError('Conflicting explicit source paths for CAM%s' % cam)
        explicit[cam] = path
    if input_dir is None and not explicit:
        input_dir = root / 'forrasmasolat'
    discovered = {}
    if input_dir is not None:
        discovered = discover_sources(input_dir, part)
    sources = dict(discovered)
    sources.update(explicit)  # e.g. the header-repaired CAM2 overrides the damaged original
    sources = {k: v.expanduser().resolve() for k, v in sources.items()}
    if not sources:
        raise ValueError('No cameras selected; provide --input-dir or --cam CAMn=PATH')
    selector = camera.strip().lower()
    if selector in ('auto', 'all'):
        selected = sorted(sources, key=int)
    elif selector == 'both':
        selected = ['1', '2']
    else:
        selected = [x.strip().removeprefix('cam') for x in selector.split(',')]
        if not selected or any(x not in CAMERA_IDS for x in selected):
            raise ValueError('Use --camera auto, all, both or comma-separated 1..8 IDs')
        if len(selected) != len(set(selected)):
            raise ValueError('Repeated camera ID in --camera')
        selected.sort(key=int)
    missing = [x for x in selected if x not in sources]
    if missing:
        raise ValueError('Requested CAM%s has no matching input; available cameras: %s' %
                         (','.join(missing), ','.join(sorted(sources, key=int))))
    for cam in selected:
        if not sources[cam].is_file():
            raise ValueError('CAM%s input does not exist: %s' % (cam, sources[cam]))
    return {cam: sources[cam] for cam in selected}
