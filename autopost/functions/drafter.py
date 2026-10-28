import base64
import json
import logging
import re
import struct
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import skills
from platforms import Error, slug, when, media_files, image_size, text_for, units, TARGETS, BANNED, EMOJI

HISTORY_BYTES = 200_000
MODEL = 'claude-sonnet-5-5'
SEARCH = {'type':'web_search_20250305','name':'web_search','max_uses':4}
PAUSES = 1
SEARCHES = 6


NOTE_TERMS = re.compile(r'schema|render|json|overlay|field|slug|api\b|\b(?:ig|tg)-[a-z]+|threads_tag|crop|validat', re.I)


def foreign_emoji(text, owner):
    return [e for e in EMOJI.findall(text) if e not in owner]


def validate(spec, prepared=True, copy_only=False, chosen=None, owner='', clips=False):
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
        if set(spec) - {'slug', 'kind', 'at', 'targets', 'media', 'copy', 'approval', 'overlay', 'notes', 'threads_tag'}:
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
        if set(media) - ({'images', 'clips', 'video', 'cover', 'sequence'} if copy_only else {'images', 'video', 'cover'}):
            errors.append('Unknown media fields')
            return errors
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
            if 'ig-reel' in targets and not media.get('video') and not clips:
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
    for target, text in blocks.items():
        if not isinstance(text, str):
            errors.append(target + ': text required')
            continue
        letters = [c for c in re.sub(r'https?://\S+|(?<!\w)#\w+', '', text) if c.isalpha()]
        if not letters or text.strip() and text.strip() in owner:
            continue
        ratio = sum('\u0400' <= c <= '\u052f' for c in letters) / len(letters)
        russian = target.startswith('tg-') or spec.get('kind') == 'personal'
        if russian and ratio < 0.5 or not russian and ratio > 0.1:
            errors.append(target + ': copy language mismatch')
    return errors


def answered(value):
    return isinstance(value, dict) and set(value) in ({'answer'}, {'action'})


def answer_errors(value, actions=False, limit=200):
    if set(value) == {'action'}:
        return [] if actions and value['action'] in (actions if isinstance(actions, tuple) else ('cancel', 'new-post')) else ['action is not allowed here']
    if isinstance(value.get('answer'), str) and 1 <= len(value['answer']) <= limit and sum('\u0400' <= c <= '\u052f' for c in value['answer']) >= len(value['answer']) / 4:
        return []
    return ['answer must be a short Russian string of at most ' + str(limit) + ' characters']


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


def final_text(content):
    parts = []
    for block in content:
        if block.type in ('server_tool_use', 'web_search_tool_result'):
            parts = []
        elif block.type == 'text':
            parts.append(block.text)
    return ''.join(parts).strip()


def note(rec, response):
    if rec:
        try:
            usage = response.usage
            rec({'model':response.model, **{name:getattr(usage, name, None) for name in ('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens','cache_creation','server_tool_use')}})
        except Exception as failure:
            logging.error(json.dumps({'code':'usage_record_failed','error':str(failure)[:200]},ensure_ascii=False))


class Cancelled(Exception):
    pass


class Timeout(Error):
    pass


def pictures(paths):
    blocks = [{'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':base64.b64encode(path.read_bytes()).decode()}} for path in paths]
    if blocks:
        blocks[-1]['cache_control'] = {'type':'ephemeral'}
    return blocks


def prompt(store=None):
    system = [{'type':'text','text':Path(__file__).with_name('draft-agent.md').read_text() + '\nYou run as Claude Sonnet 5.5 by Anthropic, model id ' + MODEL + '.','cache_control':{'type':'ephemeral'}}]
    book = skills.render(skills.load(store)) if store else ''
    if book:
        system.append({'type':'text','text':book,'cache_control':{'type':'ephemeral'}})
    return system


def first_turn(payload, pool, known=()):
    return {'role':'user','text':json.dumps(payload,ensure_ascii=False),'images':[name for name in pool if name not in known]}


def seed(store, key, payload, images, spec, guard):
    turns = [first_turn(payload,{path.name:path for path in images}),{'role':'assistant','text':json.dumps(spec,ensure_ascii=False)}]
    try:
        store.keep(key,{'turns':turns,'base':0,'tag':guard[2],'edits':len(payload.get('edits',[])),'before':0},guard)
    except Exception as failure:
        logging.error(json.dumps({'code':'conversation_save_failed','error':str(failure)[:200]},ensure_ascii=False))


def stream(client, system, tools, messages, on_event, rec=None, deadline=None, cancel=None, tokens=12000):
    import anthropic
    import httpx
    waited = False
    names = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
    while True:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise Timeout()
        seen, aborted, done = {}, [], threading.Event()
        def track(event):
            source = getattr(getattr(event, 'message', None), 'usage', None) if event.type == 'message_start' else getattr(event, 'usage', None) if event.type == 'message_delta' else None
            for name in names:
                value = getattr(source, name, None)
                if isinstance(value, int) and not isinstance(value, bool):
                    seen[name] = value
        try:
            with (client if remaining is None else client.with_options(timeout=httpx.Timeout(remaining, read=min(remaining, 60)))).messages.stream(model=MODEL, max_tokens=tokens, thinking={'type':'adaptive'}, system=system, tools=tools, messages=messages) as events:
                def watch():
                    while not done.wait(0.25):
                        late = deadline is not None and time.monotonic() > deadline
                        if done.is_set():
                            return
                        if late or cancel is not None and cancel.is_set():
                            aborted.append('late' if late and not (cancel is not None and cancel.is_set()) else 'cancel')
                            try:
                                events.close()
                            except Exception:
                                pass
                            return
                threading.Thread(target=watch, daemon=True).start()
                try:
                    for event in events:
                        if cancel is not None and cancel.is_set():
                            aborted.append('cancel')
                        elif deadline is not None and time.monotonic() > deadline:
                            aborted.append('late')
                        if aborted:
                            break
                        track(event)
                        on_event(event)
                    if not aborted:
                        message = events.get_final_message()
                except Exception:
                    if not aborted:
                        raise
                finally:
                    done.set()
            if aborted:
                if rec and seen:
                    note_usage(rec, seen)
                raise Cancelled() if aborted[0] == 'cancel' else Timeout()
            note(rec, message)
            return message
        except anthropic.APIStatusError as failure:
            text = str(failure)
            if failure.status_code == 402 or 'enforced_spend_limit_reached' in text:
                raise Error('черновик не готов: баланс Anthropic') from None
            if failure.status_code == 429 and not waited:
                waited = True
                try:
                    pause = min(float(failure.response.headers.get('retry-after')), 30)
                except (AttributeError, TypeError, ValueError):
                    pause = None
                if pause is not None and (deadline is None or time.monotonic() + pause < deadline):
                    if cancel is not None and cancel.wait(pause):
                        raise Cancelled() from None
                    elif cancel is None:
                        time.sleep(pause)
                    continue
            raise Error('Черновик не готов: Anthropic отклонил запрос (' + str(failure.status_code) + ').') from None


def note_usage(rec, seen):
    try:
        rec({'model':MODEL, **seen})
    except Exception as failure:
        logging.error(json.dumps({'code':'usage_record_failed','error':str(failure)[:200]},ensure_ascii=False))


def create(client, system, messages, rec=None):
    import anthropic
    tools, used = [SEARCH], 0
    for attempt in range(PAUSES + 2):
        if attempt > PAUSES:
            tools = [{**SEARCH,'max_uses':1}]
        elif attempt:
            tools = [{**SEARCH,'max_uses':SEARCHES - used}] if used < SEARCHES else []
        try:
            response = client.messages.create(model=MODEL, max_tokens=12000, thinking={'type':'adaptive'}, system=system, tools=tools, messages=messages)
            note(rec, response)
        except anthropic.BadRequestError as failure:
            if attempt or not any(word in str(failure) for word in ('web_search','tool')):
                raise
            logging.warning('web_search rejected by the API (400); retrying without tools')
            tools = []
            response = client.messages.create(model=MODEL, max_tokens=12000, thinking={'type':'adaptive'}, system=system, tools=tools, messages=messages)
            note(rec, response)
        if response.stop_reason != 'pause_turn':
            break
        used += sum(1 for block in response.content if block.type == 'server_tool_use')
        messages = messages + [{'role':'assistant','content':response.content}]
    return response


def connect(config):
    import anthropic
    return anthropic.Anthropic(api_key=config['anthropic_key'], max_retries=0, timeout=180)


def converse(config, system, text, tokens=4000, rec=None):
    import anthropic
    try:
        response = connect(config).messages.create(model=MODEL, max_tokens=tokens, system=system, messages=[{'role':'user','content':text}])
    except anthropic.APIStatusError as failure:
        raise Error('Provider rejected request: ' + str(failure.status_code)) from None
    note(rec, response)
    return final_text(response.content)


def ask(config, payload, images, validator, montage=False, thread=None, follow=None, rec=None):
    import anthropic
    client = connect(config)
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
    turns.append(first_turn({**(follow if turns else payload),**stamp},pool,known))
    system = prompt(thread[0] if thread else None)
    for attempt in range(4):
        try:
            response = create(client, system, wire(turns, pool), rec)
        except anthropic.APIStatusError as failure:
            if failure.status_code == 402:
                raise Error('черновик не готов: баланс Anthropic') from None
            raise Error('Draft provider rejected request: ' + str(failure.status_code)) from None
        raw = final_text(response.content)
        turns.append({'role':'assistant','text':raw})
        try:
            value = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', raw))
            errors = validator(value)
        except (ValueError, TypeError, KeyError):
            errors = ['Return a valid JSON object matching the schema']
        if not errors:
            if answered(value):
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
        if attempt < 3:
            turns.append({'role':'user','text':'Correct these validation errors and return JSON only: ' + json.dumps(errors),'images':[]})
    errors = [str(e)[:200] for e in errors]
    logging.error(json.dumps({'code':'draft_invalid','errors':errors},ensure_ascii=False))
    raise Error('Черновик не получился: модель не уложила материал в правила форматов.')
