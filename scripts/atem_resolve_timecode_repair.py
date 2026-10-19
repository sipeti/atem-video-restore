#!/usr/bin/env python3
"""Non-destructive FCP7 XML source-timecode repair and diagnostics for ATEM ISO Rescue.

Requires ffprobe. Reads existing verified XML and recovered MP4s on this Mac.
Writes new *_TCFIX.xml and *_TIMECODE_DIAG.csv alongside original XML.
Also writes a one-clip XML test for the first available camera.
Never modifies video, existing XML, or Resolve project.
"""
import argparse
import copy
import csv
import json
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit, quote
from atem_multicam import CAMERA_IDS

FPS = 25
TC_RX = re.compile(r'^([0-9]{2}):([0-9]{2}):([0-9]{2})[:;]([0-9]{2})$')


def tc_frame(tc):
    m = TC_RX.match(tc.strip())
    if not m:
        return None
    h, mnt, sec, frame = map(int, m.groups())
    if mnt >= 60 or sec >= 60 or frame >= FPS:
        return None
    return ((h * 60 + mnt) * 60 + sec) * FPS + frame


def frame_tc(frame):
    h, remain = divmod(frame, 3600 * FPS)
    minute, remain = divmod(remain, 60 * FPS)
    sec, fr = divmod(remain, FPS)
    return '%02d:%02d:%02d:%02d' % (h, minute, sec, fr)


def probe(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=start_time,duration:format_tags=timecode:stream=index,codec_type,start_time,duration,nb_frames,avg_frame_rate:stream_tags=timecode",
           "-of", "json", str(path)]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if p.returncode:
        raise ValueError('ffprobe failed: ' + p.stderr.decode('utf-8', 'replace')[:180])
    d = json.loads(p.stdout.decode('utf-8'))
    video = next((x for x in d.get('streams', []) if x.get('codec_type') == 'video'), None)
    if video is None:
        raise ValueError('No video stream')
    tc = (video.get('tags') or {}).get('timecode')
    if not tc:
        tc = next(((x.get('tags') or {}).get('timecode') for x in d.get('streams', [])
                   if (x.get('tags') or {}).get('timecode')), None)
    if not tc:
        tc = (d.get('format', {}).get('tags') or {}).get('timecode')
    nb = video.get('nb_frames')
    frames = int(nb) if nb and str(nb).isdigit() else None
    duration = video.get('duration') or d.get('format', {}).get('duration')
    dur_frames = int(round(float(duration) * FPS)) if duration not in (None, 'N/A') else None
    return {'tc': tc, 'tc_frame': tc_frame(tc) if tc else None,
            'frames': frames, 'duration_frames': dur_frames,
            'start_time': video.get('start_time', ''),
            'fps': video.get('avg_frame_rate', ''),
            'duration': duration}


def write_xml(root, path):
    xml = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    needle = b'?>'
    idx = xml.find(needle)
    if idx < 0:
        raise ValueError('XML declaration not found')
    xml = xml[:idx + len(needle)] + b'\n<!DOCTYPE xmeml>\n' + xml[idx + len(needle):]
    path.write_bytes(xml)
    ET.parse(str(path))


def get_source_path(file_node, root_dir):
    text = file_node.findtext('pathurl')
    if not text:
        raise ValueError('No pathurl')
    u = urlsplit(text)
    if u.scheme != 'file' or u.netloc not in ('', 'localhost'):
        raise ValueError('Unsupported media URL')
    path = Path(unquote(u.path))
    if path.is_file():
        return path, False
    name = file_node.findtext('name')
    if not name:
        raise ValueError('File not found and no name')
    candidates = [root_dir / ('CAM' + cam) / name for cam in CAMERA_IDS]
    exists = [p for p in candidates if p.is_file()]
    if len(exists) == 1:
        return exists[0], True
    raise ValueError('File missing or ambiguous: ' + str(path))


def inject_tc(file_node, frame):
    old = file_node.find('timecode')
    if old is not None:
        file_node.remove(old)
    tc = ET.Element('timecode')
    rt = ET.SubElement(tc, 'rate')
    ET.SubElement(rt, 'timebase').text = str(FPS)
    ET.SubElement(rt, 'ntsc').text = 'FALSE'
    ET.SubElement(tc, 'string').text = frame_tc(frame)
    ET.SubElement(tc, 'frame').text = str(frame)
    ET.SubElement(tc, 'displayformat').text = 'NDF'
    media = file_node.find('media')
    if media is None:
        file_node.append(tc)
    else:
        file_node.insert(list(file_node).index(media), tc)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path.home() / 'Desktop/ATEM_RESCUE/RECOVERED')
    ap.add_argument('--xml', type=Path, default=None)
    args = ap.parse_args()
    root_dir = args.root.expanduser().resolve()
    xml_path = args.xml.expanduser().resolve() if args.xml else root_dir / 'ATEM_ISO_VERIFIED_WITH_AUDIO.xml'
    if not xml_path.is_file():
        raise ValueError('XML not found: ' + str(xml_path))
    tree = ET.parse(str(xml_path))
    xml_root = tree.getroot()
    vidtracks = xml_root.findall('./sequence/media/video/track')
    if not 1 <= len(vidtracks) <= 8:
        raise ValueError('Expected 1..8 camera video tracks, found %s' % len(vidtracks))
    # Source-clip file elements are complete only once per file id. Audio usually references the id.
    files = {node.get('id'): node for node in xml_root.findall('.//file') if node.find('pathurl') is not None}
    if not files:
        raise ValueError('No media paths found in XML')
    by_file_out = {}
    for node in xml_root.findall('.//clipitem'):
        ref = node.find('file')
        if ref is None or not ref.get('id'):
            continue
        fid = ref.get('id')
        try:
            n = int(node.findtext('out'))
            by_file_out[fid] = max(n, by_file_out.get(fid, 0))
        except (TypeError, ValueError):
            continue
    rows = []
    errors = []
    for ix, (fid, filenode) in enumerate(files.items(), 1):
        name = filenode.findtext('name') or ''
        match = re.match(r'CAM([1-8])_', name, re.I)
        camera = match.group(1) if match else '?'
        info = {'camera': camera, 'filename': name, 'file_id': fid, 'media_path': '',
                'path_repaired': '', 'media_timecode': '', 'media_tc_frame': '',
                'video_start_time': '', 'media_nb_frames': '', 'media_duration_frames': '',
                'xml_file_duration': filenode.findtext('duration') or '',
                'xml_max_out_frame': by_file_out.get(fid, ''), 'issue': ''}
        try:
            path, corrected = get_source_path(filenode, root_dir)
            info['media_path'] = str(path)
            info['path_repaired'] = 'YES' if corrected else 'NO'
            if corrected:
                filenode.find('pathurl').text = 'file://localhost' + quote(path.as_posix(), safe='/')
            meta = probe(path)
            info['media_timecode'] = meta['tc'] or ''
            info['media_tc_frame'] = meta['tc_frame'] if meta['tc_frame'] is not None else ''
            info['video_start_time'] = meta['start_time']
            info['media_nb_frames'] = meta['frames'] if meta['frames'] is not None else ''
            info['media_duration_frames'] = meta['duration_frames'] if meta['duration_frames'] is not None else ''
            if meta['fps'] not in ('25/1', '50/2'):
                info['issue'] = 'UNEXPECTED_FPS'
            if meta['tc'] and meta['tc_frame'] is None:
                info['issue'] = 'INVALID_TIMECODE_' + meta['tc']
            elif meta['tc_frame'] is not None:
                inject_tc(filenode, meta['tc_frame'])
            else:
                inject_tc(filenode, 0)
            nout = by_file_out.get(fid)
            if nout is not None and meta['frames'] is not None and nout > meta['frames']:
                info['issue'] += ' XML_OUT_EXCEEDS_NB_FRAMES'
            if nout is not None and meta['duration_frames'] is not None and nout > meta['duration_frames'] + 1:
                info['issue'] += ' XML_OUT_EXCEEDS_DURATION'
        except Exception as ex:
            info['issue'] += ' ERROR: ' + str(ex)
            errors.append('%s: %s' % (name, ex))
        rows.append(info)
        if ix % 40 == 0:
            print('Probed %s/%s clips' % (ix, len(files)), flush=True)
    report = xml_path.with_name(xml_path.stem + '_TIMECODE_DIAG.csv')
    new_xml = xml_path.with_name(xml_path.stem + '_TCFIX.xml')
    first_clip = next((x for t in vidtracks for x in t.findall('clipitem')), None)
    if first_clip is None:
        raise ValueError('No video clip to test')
    matched = re.match(r'CAM([1-8])_', first_clip.findtext('name') or '', re.I)
    first_camera = matched.group(1) if matched else 'UNKNOWN'
    trial = xml_path.with_name('ATEM_ISO_CAM%s_ONECLIP_TCFIX.xml' % first_camera)
    for target in (report, new_xml, trial):
        if target.exists():
            raise ValueError('Output exists; refusing overwrite: ' + str(target))
    with report.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    if errors:
        print('STOP: %s errors. No XML written, see %s' % (len(errors), report))
        for e in errors[:4]:
            print('  ' + e)
        return 2
    write_xml(xml_root, new_xml)
    # Standalone first camera clip, while keeping original track positions.
    tiny = copy.deepcopy(xml_root)
    vid = tiny.findall('./sequence/media/video/track')
    preserved = False
    for track in vid:
        for node in list(track.findall('clipitem')):
            if not preserved:
                preserved = True
                continue
            track.remove(node)
    for atrack in tiny.findall('./sequence/media/audio/track'):
        for node in atrack.findall('clipitem'):
            atrack.remove(node)
    write_xml(tiny, trial)
    print('\n=== SOURCE TC DIAG ===')
    for camera in sorted({r['camera'] for r in rows if r['camera'] in CAMERA_IDS}, key=int):
        cr = [r for r in rows if r['camera'] == camera]
        times = sorted(set(r['media_timecode'] or 'NONE' for r in cr))
        issues = [r for r in cr if r['issue']]
        print('CAM%s: files=%s, timecodes=%s, issues=%s' % (camera, len(cr), ', '.join(times[:5]), len(issues)))
        for r in cr[:2]:
            print('  %s | tc=%s | src_frames=%s | duration_frames=%s | xml_out=%s | %s' %
                  (r['filename'], r['media_timecode'] or 'NONE', r['media_nb_frames'],
                   r['media_duration_frames'], r['xml_max_out_frame'], r['issue'] or 'OK'))
    print('\nDiagnostic:', report)
    print('Patched XML:', new_xml)
    print('One CAM1 clip test:', trial)
    print('Original XML and all MP4s unchanged.')
    print('IMPORTANT: This is a metadata hypothesis test, not a confirmed Resolve fix.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        sys.exit('ERROR: ' + str(exc))