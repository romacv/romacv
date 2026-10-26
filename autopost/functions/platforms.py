import hashlib
import html
import json
import mimetypes
from pathlib import Path
import re
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from contextvars import ContextVar
from datetime import datetime, timezone

SITE = 'https://www.resrom.com'
USERNAME = 'romacv777'
IG = 'https://graph.instagram.com/v25.0'
THREADS = 'https://graph.threads.net/v1.0'
TARGETS = {'tg-work', 'tg-personal', 'tg-story', 'ig-post', 'ig-reel', 'ig-story', 'threads', 'blog'}
BANNED = ('seamless', 'unlock', 'elevate', 'effortless', 'game-changer', 'link in bio')
EMOJI = re.compile('[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u200d\u20e3\u2300-\u23ff]')
CONTEXT = ContextVar('publishing_context')

class Error(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code

class ExpiredMaterial(Error):
    pass


def configure(config, store, directory):
    CONTEXT.set((config, store, Path(directory)))


def config():
    return CONTEXT.get()[0]


def key(name, raw=False):
    cfg, store, _ = CONTEXT.get()
    if name == 'telegram-bot-token':
        return cfg['telegram_bot_token']
    provider = {'instagram-token.json': 'instagram', 'threads-token.json': 'threads', 'telegram-business.json': 'business'}[name]
    return store.get('tokens/' + provider) or cfg.get(provider, {})


def local(value):
    root = CONTEXT.get()[2].resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise Error('Invalid media path')
    return path


def fingerprint(spec):
    payload = {'spec': {k:v for k,v in spec.items() if k != 'approval'}, 'media': {k:digest_file(v) for k,v in media_files(spec).items()}}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',', ':')).encode()).hexdigest()


def checkpoint(spec, state, target, step, action):
    value = CONTEXT.get()[1].checkpoint(spec['slug'], target + ':' + step, action)
    state.setdefault('steps', {})[target + ':' + step] = {'status':'done', 'value':value}
    return value

def slug(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', value) or len(value) > 40:
        raise Error('Slug must be lowercase kebab-case, at most 40 characters')
    return value


def media_files(spec):
    media = spec.get('media', {})
    files = {f'image{i}': local(item['path']) for i, item in enumerate(media.get('images', []))}
    files.update({name: local(media[name]) for name in ('video', 'cover') if media.get(name)})
    return files


def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def when(spec):
    try:
        result = datetime.fromisoformat(spec['at'].replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result
    except (ValueError, TypeError, KeyError):
        raise Error('at must be an ISO timestamp with timezone') from None


def text_for(spec, target):
    value = spec.get('copy', {}).get(target, '')
    if not isinstance(value, str):
        raise Error('Each copy value must be a plain string: ' + target)
    return value


def units(text):
    return len(text.encode('utf-16-le')) // 2


def image_size(path):
    with path.open('rb') as f:
        data = f.read(24)
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            return struct.unpack('>II', data[16:24])
        if data[:2] != b'\xff\xd8':
            raise Error('Unsupported image; use JPEG or PNG')
        f.seek(2)
        while True:
            byte = f.read(1)
            if not byte:
                break
            if byte != b'\xff':
                continue
            marker = f.read(1)
            while marker == b'\xff':
                marker = f.read(1)
            if marker in (b'\xd8', b'\xd9'):
                continue
            length = f.read(2)
            if len(length) != 2:
                break
            length = struct.unpack('>H', length)[0]
            if marker and marker[0] in (0xc0, 0xc1, 0xc2):
                fields = f.read(5)
                height, width = struct.unpack('>HH', fields[1:])
                return width, height
            if length < 2:
                break
            f.seek(length - 2, 1)
    raise Error('Invalid image header')


def multipart(fields, files):
    boundary = 'autopost-' + secrets.token_hex(16)
    parts = []
    for name, value in fields.items():
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, path in files.items():
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="upload{path.suffix}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + path.read_bytes() + b'\r\n')
    return b''.join(parts) + f'--{boundary}--\r\n'.encode(), {'Content-Type': 'multipart/form-data; boundary=' + boundary}


def telegram(method, fields=None, files=None):
    url = 'https://api.telegram.org/bot' + key('telegram-bot-token', raw=True) + '/' + method
    if files:
        data, headers = multipart(fields or {}, files)
        reply = request('POST', url, data=data, headers=headers, timeout=180)
    else:
        reply = request('POST', url, fields={k: json.dumps(v) if isinstance(v, (dict, list, bool)) else v for k, v in (fields or {}).items()}, timeout=60)
    if not reply.get('ok'):
        raise Error('Telegram rejected request; inspect bot permissions')
    return reply['result']


def telegram_identity(target=None):
    cfg = config()
    me = telegram('getMe')
    if not cfg.get('telegram_bot_id') or str(me['id']) != str(cfg['telegram_bot_id']):
        raise Error('Dedicated Telegram bot identity mismatch')
    if target in ('tg-work', 'tg-personal'):
        channel = cfg['channels'][target]
        chat = telegram('getChat', {'chat_id': channel})
        member = telegram('getChatMember', {'chat_id': channel, 'user_id': me['id']})
        if chat.get('type') != 'channel' or str(chat['id']) != str(channel) or member.get('status') != 'administrator' or not member.get('can_post_messages'):
            raise Error('Telegram channel identity or administrator rights mismatch')
        return chat
    if target == 'tg-story':
        saved = key('telegram-business.json')
        business = telegram('getBusinessConnection', {'business_connection_id': saved['business_connection_id']})
        if not business.get('is_enabled') or str(business['user']['id']) != str(cfg['roman_user_id']) or not business.get('rights', {}).get('can_manage_stories'):
            raise Error('Telegram personal business connection identity or story rights mismatch')
        return business
    return me


def personal_identity(provider, token):
    instagram = provider == 'instagram'
    fields = 'user_id,username,account_type' if instagram else 'id,username'
    reply = request('GET', (IG if instagram else THREADS) + '/me', {'fields': fields}, token=token)
    if 'data' in reply:
        if not isinstance(reply['data'], list) or len(reply['data']) != 1:
            raise Error('Expected exactly one personal identity')
        reply = reply['data'][0]
    if instagram:
        if str(reply.get('account_type', '')).upper() != 'MEDIA_CREATOR':
            raise Error('Instagram account must be a Creator')
        reply = dict(reply, id=reply.get('user_id'))
    if reply.get('username') != USERNAME or not re.fullmatch(r'[0-9]+', str(reply.get('id', ''))):
        raise Error('Only the pinned personal username and a valid user ID are allowed')
    return reply


def quota(session, instagram=True):
    token, user, base = session
    edge = 'content_publishing_limit' if instagram else 'threads_publishing_limit'
    rows = request('GET', f'{base}/{user}/{edge}', {'fields': 'quota_usage,config'}, token=token).get('data', [])
    if not rows:
        raise Error('Publishing quota unavailable')
    for row in rows:
        limit = row.get('config', {}).get('quota_total')
        if limit is None:
            raise Error('Publishing quota configuration unavailable')
        if int(row['quota_usage']) >= int(limit):
            raise Error('Publishing quota exhausted; retry later')


def numeric(reply):
    value = str(reply['id'])
    if not re.fullmatch(r'[0-9]+', value):
        raise Error('Invalid returned object ID; reconcile manually')
    return value


def ready(session, container, instagram):
    token, _, base = session
    if not re.fullmatch(r'[0-9]+', str(container)):
        raise Error('Invalid saved container ID')
    field = 'status_code' if instagram else 'status'
    for i in range(6 if instagram else 8):
        value = request('GET', f'{base}/{container}', {'fields': field}, token=token).get(field)
        if value == 'FINISHED':
            return
        if value in ('ERROR', 'EXPIRED', 'PUBLISHED'):
            raise Error('Container ' + str(value) + '; reconcile before retry')
        if i < (5 if instagram else 7):
            time.sleep(30 if instagram else 15)
    raise Error('Container processing timed out; retry polls saved container')


def post_telegram(spec, state, target):
    identity = telegram_identity(target)
    files, text = media_files(spec), text_for(spec, target)
    if target == 'tg-story':
        video = 'video' in files
        field = 'video' if video else 'photo'
        content = {'type': field, field: 'attach://asset'}
        if video:
            content['cover_frame_timestamp'] = 0.0
        result = checkpoint(spec, state, target, 'publish', lambda: telegram('postStory', {
            'business_connection_id': identity['id'], 'content': content, 'active_period': config().get('story_active_period', 21600),
            'post_to_chat_page': True, 'caption': text,
        }, {'asset': files['video' if video else 'image0']}))
        username = identity['user'].get('username')
        return f'https://t.me/{username}/s/{result["id"]}' if username else f'tg://resolve?domain={identity["user"]["id"]}&story={result["id"]}'
    chat_id = identity['id']
    def send():
        if 'video' in files:
            return telegram('sendVideo', {'chat_id': chat_id, 'caption': text, 'cover': 'attach://cover'}, {'video': files['video'], 'cover': files['cover']})
        images = spec.get('media', {}).get('images', [])
        if len(images) > 1:
            media = [{'type': 'photo', 'media': 'attach://image' + str(i), **({'caption': text} if i == 0 else {})} for i in range(len(images))]
            return telegram('sendMediaGroup', {'chat_id': chat_id, 'media': media}, {k: v for k, v in files.items() if k.startswith('image')})[0]
        if images:
            return telegram('sendPhoto', {'chat_id': chat_id, 'caption': text}, {'photo': files['image0']})
        return telegram('sendMessage', {'chat_id': chat_id, 'text': text})
    result = checkpoint(spec, state, target, 'publish', send)
    prefix = identity.get('username') or ('c/' + str(chat_id).removeprefix('-100'))
    return f'https://t.me/{prefix}/{result["message_id"]}'


def post_social(spec, state, target, urls, session):
    instagram = target.startswith('ig-')
    token, user, base = session
    edge = 'media' if instagram else 'threads'
    publish_edge = 'media_publish' if instagram else 'threads_publish'
    def create(step, fields):
        local_files = media_files(spec)
        for field in ('image_url', 'video_url', 'cover_url'):
            if field in fields:
                name = next((name for name, url in urls.items() if url == fields[field]), None)
                if name is None:
                    raise Error('Media must come from this post’s own public bucket objects')
                probe(fields[field], local_files[name])
        return checkpoint(spec, state, target, step, lambda: numeric(request('POST', f'{base}/{user}/{edge}', fields, token=token)))
    published = state['steps'].get(target + ':publish')
    if published and published.get('status') == 'done':
        media_id = published['value']
    else:
        quota(session, instagram)
        images = spec.get('media', {}).get('images', [])
        text = text_for(spec, target)
        caption = {'caption' if instagram else 'text': text}
        if target == 'ig-story':
            caption = {}
        if len(images) > 1:
            children = []
            for i, image in enumerate(images):
                fields = {'image_url': urls[f'image{i}'], 'is_carousel_item': 'true', 'alt_text': image['alt']}
                if not instagram:
                    fields['media_type'] = 'IMAGE'
                child = create('child' + str(i), fields)
                try:
                    ready(session, child, instagram)
                except Error as failure:
                    if 'Container ERROR' in str(failure) or 'Container EXPIRED' in str(failure):
                        CONTEXT.get()[1].invalidate(spec['slug'],target + ':child' + str(i))
                    raise
                children.append(child)
            container = create('container', {'media_type': 'CAROUSEL', 'children': ','.join(children), **caption})
        elif target == 'ig-reel':
            fields = {'media_type': 'REELS', 'video_url': urls['video'],
                      'share_to_feed': 'true', 'cover_url': urls['cover'], **caption}
            container = create('container', fields)
        else:
            video = bool(spec.get('media', {}).get('video'))
            fields = dict(caption)
            if video:
                fields.update(video_url=urls['video'], media_type='STORIES' if instagram else 'VIDEO')
            elif images:
                fields['image_url'] = urls['image0']
                if not instagram:
                    fields['media_type'] = 'IMAGE'
                    fields['alt_text'] = images[0]['alt']
                elif target == 'ig-story':
                    fields['media_type'] = 'STORIES'
                else:
                    fields['alt_text'] = images[0]['alt']
            else:
                fields['media_type'] = 'TEXT'
            container = create('container', fields)
        try:
            ready(session, container, instagram)
        except Error as failure:
            if 'Container ERROR' in str(failure) or 'Container EXPIRED' in str(failure):
                store = CONTEXT.get()[1]
                for name in list(state['steps']):
                    if name.startswith(target + ':') and name != target + ':publish':
                        store.invalidate(spec['slug'], name)
            raise
        quota(session, instagram)
        media_id = checkpoint(spec, state, target, 'publish', lambda: numeric(request('POST', f'{base}/{user}/{publish_edge}', {'creation_id': container}, token=token)))
    if not re.fullmatch(r'[0-9]+', str(media_id)):
        raise Error('Invalid saved media ID; reconcile')
    if instagram:
        result = request('GET', f'{base}/{media_id}', {'fields': 'shortcode'}, token=token)
        code = result.get('shortcode', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', code):
            raise Error('Shortcode unavailable; retain publish checkpoint and reconcile link')
        if target == 'ig-story':
            native = 0
            alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
            for char in code:
                native = native * 64 + alphabet.index(char)
            return f'https://www.instagram.com/stories/{USERNAME}/{native}/'
        return 'https://www.instagram.com/' + ('reel/' if target == 'ig-reel' else 'p/') + code + '/'
    result = request('GET', f'{base}/{media_id}', {'fields': 'shortcode'}, token=token)
    code = result.get('shortcode', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', code):
        raise Error('Shortcode unavailable; retain publish checkpoint and reconcile link')
    return f'https://www.threads.com/@{USERNAME}/post/{code}'


def markdown(body):
    """Small escaped Markdown subset: headings, paragraphs and lists; no raw HTML."""
    blocks = []
    for block in re.split(r'\n\s*\n', body.strip()):
        lines = block.splitlines()
        if all(re.match(r'^#{2,3} ', line) for line in lines):
            for line in lines:
                level = len(line.split(' ', 1)[0])
                blocks.append(f'<h{level}>' + html.escape(line[level + 1:]) + f'</h{level}>')
        elif all(re.match(r'^[-*] ', line) for line in lines):
            blocks.append('<ul>' + ''.join('<li>' + html.escape(l[2:]) + '</li>' for l in lines) + '</ul>')
        else:
            if lines[0].startswith(('## ', '### ')):
                blocks.append(markdown(lines[0]))
                lines = lines[1:]
            if lines:
                blocks.append('<p>' + html.escape(' '.join(lines)) + '</p>')
    return '\n'.join(blocks)


def social_session(provider):
    saved = key(provider + '-token.json')
    token = saved['access_token']
    me = personal_identity(provider, token)
    if str(me['id']) != str(saved['user_id']):
        raise Error('Live personal identity mismatch')
    return token, str(me['id']), IG if provider == 'instagram' else THREADS


def request(method, url, fields=None, token=None, data=None, headers=None, timeout=60):
    import logging
    headers = dict(headers or {})
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if fields is not None:
        encoded = urllib.parse.urlencode(fields)
        if method == 'GET':
            url += '?' + encoded
        else:
            data = encoded.encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
    status = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url,data=data,headers=headers,method=method),timeout=timeout) as response:
            reply = json.loads(response.read())
        if isinstance(reply, dict) and (reply.get('error') or reply.get('ok') is False):
            raise ProviderFailure(reply)
        return reply
    except urllib.error.HTTPError as failure:
        try:
            reply = json.loads(failure.read())
        except (ValueError, OSError):
            reply = {}
        status = failure.code
        safe = safe_error(reply, failure.code)
    except ProviderFailure as failure:
        status = failure.reply.get('error_code')
        safe = safe_error(failure.reply)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        safe = {'code': 'network', 'description': 'Outcome uncertain; inspect checkpoints'}
    logging.error(json.dumps(safe))
    failure = Error('API failure: ' + str(safe['code']), status_code=status)
    failure.not_modified = status == 400 and 'message is not modified' in safe.get('description', '').lower()
    raise failure from None


class ProviderFailure(Exception):
    def __init__(self, reply):
        self.reply = reply


def safe_error(reply, status=None):
    error = reply.get('error', {})
    if not isinstance(error, dict):
        error = {}
    safe = {'code': error.get('code', reply.get('error_code', status or 'provider'))}
    for name in ('error_subcode', 'fbtrace_id'):
        value = str(error.get(name, ''))
        if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
            safe[name] = value
    description = reply.get('description', '')
    if isinstance(description, str):
        cfg, store, _ = CONTEXT.get()
        credentials = [cfg] + ([store.get('tokens/' + provider) or {} for provider in ('instagram','threads','business')] if store else [])
        for secret in [s for value in credentials for s in secret_strings(value)]:
            description = description.replace(secret, '[redacted]')
        description = re.sub(r'https?://\S+', '[url]', description)
        safe['description'] = description[:300]
    return safe


def secret_strings(value):
    if isinstance(value, dict):
        return [s for v in value.values() for s in secret_strings(v)]
    if isinstance(value, str) and len(value) >= 8:
        return [value]
    return []


def probe(url, path, content_type=None):
    try:
        with urllib.request.urlopen(url,timeout=60) as response:
            if response.status != 200 or int(response.headers.get('Content-Length', -1)) != path.stat().st_size:
                raise Error('Media warm-up mismatch')
            expected_type = content_type or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            if response.headers.get('Content-Type') != expected_type:
                raise Error('Media content type changed')
            digest = hashlib.sha256()
            for chunk in iter(lambda: response.read(1024*1024),b''):
                digest.update(chunk)
            if digest.hexdigest() != digest_file(path):
                raise Error('Media bytes changed')
    except (urllib.error.URLError, OSError):
        raise Error('Media warm-up failed') from None


def link_opens(url, title=None):
    try:
        with urllib.request.urlopen(url,timeout=20) as response:
            body = response.read(2000000).decode('utf-8',errors='replace')
            if response.status != 200:
                return False
            return title is None or ('<title>' + html.escape(title) + ' | Roman Resenchuk</title>') in body
    except (urllib.error.URLError, OSError, ValueError):
        return False
