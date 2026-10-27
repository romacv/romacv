import base64
import json
import logging
import re
import struct
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from platforms import Error, slug, when, media_files, image_size, text_for, units, TARGETS, BANNED, EMOJI

HISTORY_BYTES = 200_000


NOTE_TERMS = re.compile(r'schema|render|json|overlay|field|slug|api\b|\b(?:ig|tg)-[a-z]+|threads_tag|crop|validat', re.I)


def foreign_emoji(text, owner):
    return [e for e in EMOJI.findall(text) if e not in owner]


def validate(spec, prepared=True, copy_only=False, chosen=None, owner=''):
    """Offline: no keys, HTTP, external programs or state mutation."""
    errors = []
    size = None
    try:
        slug(spec.get('slug'))
        if when(spec) > datetime.now(timezone.utc) + timedelta(hours=36):
            errors.append('at must be within 36 hours')
        if spec.get('kind') not in ('work', 'personal'):
            errors.append('kind must be work or personal')
        targets = spec.get('targets')
        if not isinstance(targets, list) or not targets or any(t not in TARGETS for t in targets) or len(set(targets)) != len(targets):
            raise Error('targets must be a nonempty unique list of supported targets')
        if chosen is not None and any(t not in chosen for t in targets):
            errors.append('targets must be within the chosen targets: ' + ', '.join(chosen))
        if set(spec) - {'slug', 'kind', 'at', 'targets', 'media', 'copy', 'blog', 'approval', 'overlay', 'notes', 'threads_tag'}:
            errors.append('Unknown spec fields')
        if 'notes' in spec:
            note = spec['notes']
            letters = [c for c in note if c.isalpha()] if isinstance(note, str) else []
            if not isinstance(note, str) or len(note) > 200:
                errors.append('notes must be a string of at most 200 characters')
            elif NOTE_TERMS.search(note) or letters and sum('\u0400' <= c <= '\u052f' for c in letters) / len(letters) < 0.8:
                errors.append('notes must be short plain Russian with no technical words, or omitted')
        tag = spec.get('threads_tag')
        if 'threads' in targets and tag is None and not copy_only:
            errors.append('threads needs threads_tag')
        if tag is not None and (not isinstance(tag, str) or not 1 <= len(tag) <= 50 or re.search(r'[.&#\s]', tag)):
            errors.append('threads_tag must be 1–50 characters without #, spaces, periods or ampersands')
        if not isinstance(spec.get('approval', {}), dict):
            errors.append('approval must be an object')
        media = spec.get('media', {})
        if set(media) - {'images', 'video', 'cover'}:
            errors.append('Unknown media fields')
        images = media.get('images', [])
        if not isinstance(images, list) or len(images) > 10:
            raise Error('media.images must be a list of up to 10 images')
        for item in images:
            if not isinstance(item, dict) or set(item) != {'path', 'alt'} or not isinstance(item['alt'], str) or not 1 <= len(item['alt']) <= 1000:
                raise Error('Each image needs path and alt (1–1000 characters)')
        if not copy_only:
            files = media_files(spec)
            if images and 'image0' in files:
                size = image_size(files['image0'])
            if images and media.get('video'):
                errors.append('Choose images or video for a spec, not both')
            if media.get('cover') and not media.get('video'):
                errors.append('cover requires video')
            for name, path in files.items():
                if path.stat().st_size == 0:
                    errors.append(name + ': empty media')
                if path.stat().st_size > (50_000_000 if name == 'video' else 10_000_000):
                    errors.append(name + ': exceeds dedicated bot preview upload limit')
                if name == 'video':
                    with path.open('rb') as f:
                        header = f.read(12)
                    if path.suffix.lower() != '.mp4' or header[4:8] != b'ftyp':
                        errors.append('video must be an MP4 file')
                else:
                    image_size(path)
            if media.get('video'):
                if 'cover' not in files:
                    errors.append('Every video requires a designed 9:16 sRGB JPEG cover')
                else:
                    w, h = image_size(files['cover'])
                    data = files['cover'].read_bytes()
                    if (w, h) != (1080, 1920) or data[:3] != b'\xff\xd8\xff' or b'sRGB' not in data:
                        errors.append('cover must be 1080x1920 JPEG with embedded sRGB profile')
            if 'ig-post' in targets and (not images or media.get('video')):
                errors.append('ig-post needs 1 image or 2–10 carousel images')
            if 'ig-reel' in targets and not media.get('video'):
                errors.append('ig-reel needs video')
            for t in ('ig-story', 'tg-story'):
                if t in targets and not (media.get('video') or images):
                    errors.append(t + ' needs an image or video')
            if any(t.startswith('ig-') for t in targets):
                for name, path in files.items():
                    if name != 'video' and (path.read_bytes()[:3] != b'\xff\xd8\xff' or path.stat().st_size > 8_000_000):
                        errors.append(name + ': Instagram requires JPEG at most 8 MB')
            if 'threads' in targets:
                for i in range(len(images)):
                    if files[f'image{i}'].stat().st_size > 8_000_000:
                        errors.append('threads: image exceeds 8 MB')
            if 'tg-story' in targets and prepared:
                if media.get('video'):
                    if files['video'].stat().st_size > 30_000_000:
                        errors.append('tg-story video exceeds 30 MB')
                elif images:
                    if image_size(files['image0']) != (1080, 1920) or files['image0'].stat().st_size > 10_000_000:
                        errors.append('tg-story photo must be 1080x1920, at most 10 MB')
            if 'tg-channel' in targets:
                for name, path in files.items():
                    if path.stat().st_size > (50_000_000 if name == 'video' else 10_000_000):
                        errors.append(name + ': exceeds Telegram upload limit')
        for t in targets:
            if t == 'blog':
                continue
            text = text_for(spec, t)
            if not text.strip():
                errors.append(t + ': copy is required')
            limit = 500 if t == 'threads' else 2200 if t.startswith('ig-') else 2048 if t == 'tg-story' else 1024 if images or media.get('video') else 4096
            length = units(text) if t.startswith('tg-') else len(text.encode('utf-8')) if t == 'threads' else len(text)
            if length > limit:
                errors.append(f'{t}: copy {length} exceeds {limit}')
            if any(phrase in text.lower() for phrase in BANNED):
                errors.append(t + ': banned phrase')
            tags = re.findall(r'(?<!\w)#\w+', text)
            if any(tag.lower() in ('#fyp', '#viral') for tag in tags):
                errors.append(t + ': generic hashtags forbidden')
            if t in ('ig-post', 'ig-reel') and not 3 <= len(tags) <= 5:
                errors.append(t + ': use 3–5 specific hashtags')
            if t == 'threads':
                if len(tags) > 1 or not text.rstrip().endswith('?'):
                    errors.append('threads: at most one hashtag and end with a question')
                first = text.strip().splitlines()[0] if text.strip() else ''
                if first.startswith(('#', 'http', '@')) or first.endswith('?'):
                    errors.append('threads: first line must be a situation or fact')
            if foreign_emoji(text, owner):
                errors.append(t + ': no emoji unless Roman wrote it')
            if t == 'threads' and len(set(re.findall(r'(?:https?://|www\.)\S+', text))) > 5:
                errors.append('threads: at most 5 unique links')
        if spec.get('kind') == 'personal' and 'tg-channel' in targets and 'threads' in targets and (spec.get('copy') or {}).get('tg-channel') != (spec.get('copy') or {}).get('threads'):
            errors.append('personal: tg-channel and threads copy must be identical')
        errors.extend(overlay_errors(spec, targets, images, size))
        errors.extend(language_errors(spec, owner))
        blog = spec.get('blog')
        if blog and spec.get('kind') != 'work':
            errors.append('blog is for work only')
        if 'blog' in targets:
            if spec.get('kind') != 'work' or not isinstance(blog, dict):
                raise Error('blog target requires kind work and a blog object')
            title, desc, body = (blog.get(k, '') for k in ('title', 'description', 'body'))
            if blog.get('language') != 'en' or re.search('[\u0400-\u052f\u4e00-\u9fff]', title + desc + body):
                errors.append('blog must be English, with language en')
            if not 1 <= len(title) <= 65:
                errors.append('blog title must be 1–65 characters')
            if not 120 <= len(desc) <= 160:
                errors.append('blog description must be 120–160 characters')
            if not 400 <= len(re.findall(r'\S+', body)) <= 1200:
                errors.append('blog body must be 400–1200 words')
            if len(re.findall(r'^## .+', body, re.M)) < 2:
                errors.append('blog needs at least 2 H2 headings (##)')
            combined = '\n'.join((title, desc, body))
            if EMOJI.search(combined):
                errors.append('blog: no emoji')
            if any(p in combined.lower() for p in BANNED):
                errors.append('blog contains a banned phrase')
    except (Error, TypeError, KeyError, ValueError, AttributeError, struct.error) as e:
        errors.append(str(e) if isinstance(e, Error) else 'Invalid spec field type or media header')
    return errors


def overlay_errors(spec, targets, images, size=None):
    overlay = spec.get('overlay', {})
    if not isinstance(overlay, dict):
        return ['overlay must be an object']
    errors = []
    for target, value in overlay.items():
        if target not in ('ig-story', 'tg-story') or target not in targets or not images:
            errors.append('overlay: only for photo story targets in targets')
            continue
        if not isinstance(value, dict) or set(value) != {'text', 'place', 'anchor', 'align'}:
            errors.append(target + ': overlay needs text, place, anchor, align')
            continue
        text, place, anchor = value['text'], value['place'], value['anchor']
        if not isinstance(text, str) or not 1 <= len(text) <= 60 or re.search(r'[\x00-\x1f{}\\]', text):
            errors.append(target + ': overlay text must be 1–60 plain characters')
        if isinstance(text, str) and target in spec.get('copy', {}) and text != spec['copy'][target]:
            errors.append(target + ': overlay text must equal the target copy')
        if place is not None and (not isinstance(place, str) or not 1 <= len(place) <= 40 or re.search(r'[\x00-\x1f{}\\]', place)):
            errors.append(target + ': overlay place must be null or 1–40 plain characters')
        if not isinstance(anchor, list) or len(anchor) != 2 or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not 0 <= n <= 1 for n in anchor):
            errors.append(target + ': overlay anchor must be [x, y] within 0..1')
        elif size:
            width, height = size
            if width * 16 > height * 9:
                low = (1 - height * 9 / 16 / width) / 2
                inside = low - 1e-9 <= anchor[0] <= 1 - low + 1e-9
            else:
                low = (1 - width * 16 / 9 / height) / 2
                inside = low - 1e-9 <= anchor[1] <= 1 - low + 1e-9
            if not inside:
                errors.append(target + ': overlay anchor must lie inside the centered 9:16 crop of the photo (' + ('x' if width * 16 > height * 9 else 'y') + ' from %.2f to %.2f)' % (low, 1 - low))
        if value['align'] not in ('left', 'center', 'right'):
            errors.append(target + ': overlay align must be left, center or right')
    return errors


def language_errors(spec, owner=''):
    errors = []
    blocks = dict(spec.get('copy', {}))
    if spec.get('blog'):
        blocks.update({'blog.' + k:v for k,v in spec['blog'].items() if k != 'language'})
    for target, text in blocks.items():
        if not isinstance(text, str):
            errors.append(target + ': text required')
            continue
        letters = [c for c in re.sub(r'https?://\S+|(?<!\w)#\w+', '', text) if c.isalpha()]
        if not letters or text.strip() and text.strip() in owner:
            continue
        ratio = sum('\u0400' <= c <= '\u052f' for c in letters) / len(letters)
        russian = target.startswith('tg-') or (spec.get('kind') == 'personal' and not target.startswith('blog'))
        if russian and ratio < 0.5 or not russian and ratio > 0.1:
            errors.append(target + ': copy language mismatch')
    return errors


def answered(value):
    return isinstance(value, dict) and set(value) == {'answer'}


def answer_errors(value):
    if isinstance(value.get('answer'), str) and 1 <= len(value['answer']) <= 600 and sum('\u0400' <= c <= '\u052f' for c in value['answer']) >= len(value['answer']) / 4:
        return []
    return ['answer must be a short Russian string of at most 600 characters']


def wire(turns, pool):
    messages = []
    for turn in turns:
        content = [{'type':'text','text':turn['text']}]
        for name in turn.get('images', []):
            if name in pool:
                content.append({'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':base64.b64encode(pool[name].read_bytes()).decode()}})
            else:
                content.append({'type':'text','text':'(An image attached to this message is no longer available.)'})
        messages.append({'role':turn['role'],'content':content})
    messages[-1]['content'][-1]['cache_control'] = {'type':'ephemeral'}
    return messages


def ask(config, payload, images, validator, montage=False, thread=None, follow=None):
    import anthropic
    client = anthropic.Anthropic(api_key=config['anthropic_key'], max_retries=0, timeout=180)
    local = datetime.now(ZoneInfo(config.get('timezone','Asia/Makassar')))
    stamp = {'timezone':config.get('timezone','Asia/Makassar'),'current_time':local.isoformat()}
    pool = {path.name:path for path in images}
    turns, before = [], 0
    if thread:
        store, key, tag, count, guard = thread
    if thread and follow is not None:
        saved = store.get(key) or {}
        turns = saved.get('turns', [])
        before = saved.get('edits', 0)
        if saved.get('tag') == tag:
            turns = turns[:saved.get('base', 0)]
            before = saved.get('before', 0)
    base = len(turns)
    mark = base
    known = {name for turn in turns for name in turn.get('images', [])}
    turns.append({'role':'user','text':json.dumps({**(follow if turns else payload),**stamp},ensure_ascii=False),'images':[name for name in pool if name not in known]})
    system = [{'type':'text','text':Path(__file__).with_name('draft-agent.md').read_text() + '\n' + Path(__file__).with_name('EXPERIENCE.md').read_text(),'cache_control':{'type':'ephemeral'}}]
    for attempt in range(2):
        try:
            response = client.messages.create(model='claude-sonnet-5-5', max_tokens=12000, thinking={'type':'adaptive'}, system=system, messages=wire(turns, pool))
        except anthropic.APIStatusError as failure:
            if failure.status_code == 402:
                raise Error('черновик не готов: баланс Anthropic') from None
            raise Error('Draft provider rejected request: ' + str(failure.status_code)) from None
        raw = ''.join(block.text for block in response.content if block.type == 'text').strip()
        turns.append({'role':'assistant','text':raw})
        try:
            value = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', raw))
            errors = validator(value)
        except (ValueError, TypeError, KeyError):
            errors = ['Return a valid JSON object matching the schema']
        if not errors:
            if isinstance(value, dict) and set(value) == {'answer'}:
                del turns[mark:]
            if thread:
                while len(turns) > 4 and len(json.dumps(turns, ensure_ascii=False).encode()) > HISTORY_BYTES:
                    del turns[2:4]
                    base = max(base - 2, min(base, 2))
                try:
                    store.keep(key, {'turns':turns,'base':base,'tag':tag,'edits':count,'before':before}, guard)
                except Exception as failure:
                    logging.error(json.dumps({'code':'conversation_save_failed','error':str(failure)[:200]},ensure_ascii=False))
            return value
        turns.append({'role':'user','text':'Correct these validation errors and return JSON only: ' + json.dumps(errors),'images':[]})
    errors = [str(e)[:200] for e in errors]
    logging.error(json.dumps({'code':'draft_invalid','errors':errors},ensure_ascii=False))
    raise Error('Черновик не готов: ' + '; '.join(errors[:2]))
