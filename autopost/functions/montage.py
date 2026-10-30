import math
import re
from drafter import language_errors
from platforms import EMOJI, BANNED, units


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(edl, sources):
    errors = []
    if not isinstance(edl, dict) or not isinstance(sources, dict):
        return ['EDL and sources must be objects']
    if not {'version', 'kind', 'targets', 'copy', 'cover', 'fit'} <= set(edl) <= {'version', 'kind', 'targets', 'copy', 'cover', 'fit', 'threads_tag'} or type(edl.get('version')) is not int or edl['version'] != 1:
        errors.append('EDL requires version=1, kind, targets, copy, cover, fit')
    if edl.get('kind') not in ('work', 'personal') or edl.get('fit') not in ('crop', 'blur'):
        errors.append('Invalid kind or fit')
    tag = edl.get('threads_tag')
    if 'threads_tag' in edl and (not isinstance(tag, str) or not 1 <= len(tag) <= 50 or re.search(r'[#\s.&]', tag)):
        errors.append('threads_tag must be 1–50 characters without #, spaces, periods or ampersands')
    targets = edl.get('targets')
    if isinstance(targets, dict) and 'threads' in targets and 'threads_tag' not in edl:
        errors.append('threads needs threads_tag')
    if not isinstance(targets, dict) or not targets:
        return errors + ['targets must be nonempty object']
    allowed = {'ig-reel':90, 'ig-story':60, 'tg-story':60, 'tg-channel':90, 'threads':90}
    totals = {}
    for target, timeline in targets.items():
        if target not in allowed or not isinstance(timeline, dict) or not {'clips', 'subtitles'} <= set(timeline) <= {'clips', 'subtitles', 'titles'}:
            errors.append('Invalid target timeline')
            continue
        total = 0
        clips = timeline.get('clips')
        if not isinstance(clips, list) or not 1 <= len(clips) <= 100:
            errors.append(target + ': clips required, maximum 100')
            continue
        for clip in clips:
            if not isinstance(clip, dict) or set(clip) != {'source', 'in', 'out'} or not isinstance(clip.get('source'), str):
                errors.append('Clip requires source,in,out')
                continue
            duration = sources.get(clip['source'])
            start, end = clip['in'], clip['out']
            if not number(duration) or not number(start) or not number(end) or not 0 <= start < end <= duration:
                errors.append(target + ': clip outside source duration')
            else:
                total += end - start
        totals[target] = total
        if total < 3 or total + 1/30 > allowed[target]:
            errors.append(target + ': total must be >=3s and fit target length including cover frame')
        cues = timeline.get('subtitles')
        if not isinstance(cues, list) or len(cues) > 300:
            errors.append('Invalid subtitle cues')
            continue
        previous_end = 0
        for cue in cues:
            if not isinstance(cue, dict) or set(cue) != {'start', 'end', 'lines'}:
                errors.append('Cue requires start,end,lines')
                continue
            start, end, lines = cue['start'], cue['end'], cue['lines']
            if not number(start) or not number(end) or not previous_end <= start < end <= total:
                errors.append('Subtitle times overlap or exceed timeline')
            else:
                previous_end = end
            if not isinstance(lines, list) or not 1 <= len(lines) <= 2 or any(not isinstance(line,str) or not 1 <= len(line) <= 32 or any(c in line for c in '\n\r{}\\') or EMOJI.search(line) for line in lines):
                errors.append('Subtitles require 1–2 plain lines, <=32 characters each')
        headers = timeline.get('titles', [])
        if not isinstance(headers, list) or len(headers) > 20:
            errors.append('Invalid title cues')
            continue
        previous_end = 0
        for cue in headers:
            if not isinstance(cue, dict) or set(cue) != {'start', 'end', 'text'}:
                errors.append('Title requires start,end,text')
                continue
            start, end, text = cue['start'], cue['end'], cue['text']
            if not number(start) or not number(end) or not previous_end <= start < end <= total:
                errors.append('Title times overlap or exceed timeline')
            else:
                previous_end = end
            if not isinstance(text, str) or not 1 <= len(text) <= 28 or any(c in text for c in '\n\r{}\\'):
                errors.append('Titles require text without line breaks, <=28 characters')
        if headers and all(isinstance(cue, dict) and number(cue.get('start')) and number(cue.get('end')) for cue in headers):
            edges = [0] + [x for cue in headers for x in (cue['start'], cue['end'])] + [total]
            if any(abs(edges[i] - edges[i + 1]) > 1/30 for i in range(0, len(edges), 2)):
                errors.append(target + ': titles must cover the whole timeline from 0 to the total length without gaps')
    stories = [targets[name] for name in ('ig-story', 'tg-story') if isinstance(targets.get(name), dict)]
    if len(stories) == 2 and any(stories[0].get(key, []) != stories[1].get(key, []) for key in ('clips', 'subtitles', 'titles')):
        errors.append('ig-story and tg-story must share one story timeline')
    cover = edl.get('cover')
    if not isinstance(cover, dict) or set(cover) != {'target', 'time', 'text'} or not isinstance(cover.get('target'),str) or cover.get('target') not in totals:
        errors.append('Invalid cover target')
    elif not number(cover['time']) or not 0 <= cover['time'] < totals[cover['target']]:
        errors.append('Cover time outside timeline')
    if isinstance(cover, dict) and (not isinstance(cover.get('text'),str) or not 1 <= len(cover['text'].strip()) <= 40 or any(c in cover['text'] for c in '\n\r{}\\') or EMOJI.search(cover['text'])):
        errors.append('Cover requires plain headline text, 1–40 characters')
    copy = edl.get('copy')
    if not isinstance(copy, dict) or set(copy) != set(targets):
        errors.append('Copy required per target')
    else:
        errors.extend(language_errors(edl))
        for target, text in copy.items():
            if not isinstance(text,str) or not text.strip():
                errors.append('Empty copy')
                continue
            limit = 500 if target == 'threads' else 2200 if target.startswith('ig-') else 2048 if target == 'tg-story' else 1024
            length = len(text.encode()) if target == 'threads' else units(text) if target.startswith('tg-') else len(text)
            if length > limit or EMOJI.search(text) or any(x in text.lower() for x in BANNED):
                errors.append(target + ': invalid copy')
            tags = re.findall(r'(?<!\w)#\w+',text)
            if any(tag.lower() in ('#fyp','#viral') for tag in tags):
                errors.append('Generic hashtags forbidden')
            if target == 'ig-reel' and not 3 <= len(tags) <= 5:
                errors.append('Reel needs 3–5 specific hashtags')
            if target == 'threads':
                first = text.strip().splitlines()[0]
                if len(tags) > 1 or not text.rstrip().endswith('?') or first.endswith('?') or first.startswith(('#','http','@')):
                    errors.append('Threads requires fact first and question last')
                if len(set(re.findall(r'(?:https?://|www\.)\S+',text))) > 5:
                    errors.append('Threads allows at most five links')
    return errors
