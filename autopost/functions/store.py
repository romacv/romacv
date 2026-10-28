import copy
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo



def plural(count, forms):
    if count % 10 == 1 and count % 100 != 11:
        return forms[0]
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return forms[1]
    return forms[2]


NAMES = {'tg-channel':'Telegram-канал','tg-story':'История Telegram','ig-post':'Instagram, пост','ig-reel':'Instagram, Reels','ig-story':'Instagram, история','threads':'Threads'}
GROUPS = (('story','Сторис',('ig-story','tg-story')),('ig-post','Instagram-пост',('ig-post',)),('ig-reel','Instagram Reels',('ig-reel',)),('threads','Threads',('threads',)),('channel','Telegram-канал',('tg-channel',)))
TOGGLES = {'tg' + str(index):group[0] for index,group in enumerate(GROUPS)}
IMAGE_MIMES = ('image/jpeg', 'image/png', 'image/webp', 'image/heic')
CHOICE_HINT = 'Отметь, куда пойдёт пост.'
COMMANDS = ('/post', '/skills')
COMMANDS_HINT = 'Команды: /post — новый пост, /skills — навыки бота.'
POST_HINT = 'Чтобы начать пост, отправь /post.'
MEDIA_KEYS = ('photo', 'video', 'voice', 'audio', 'document', 'animation', 'video_note')


def hint_for(name):
    return ('Привет! ' if name == '/start' else '') + COMMANDS_HINT


def command(message):
    text = message.get('text') or message.get('caption') or ''
    entities = message.get('entities') or message.get('caption_entities') or []
    if isinstance(entities, list) and any(isinstance(x, dict) and x.get('type') == 'bot_command' and x.get('offset') == 0 for x in entities):
        return text.split(None, 1)[0].split('@')[0].lower()
    return None


def without_command(message):
    key = 'text' if 'text' in message else 'caption'
    rest = message.get(key, '').split(None, 1)
    stripped = {k: v for k, v in message.items() if k not in ('entities', 'caption_entities', key)}
    if len(rest) > 1:
        stripped[key] = rest[1]
    return stripped


def imaged(message):
    document = message.get('document')
    if message.get('photo') or message.get('video') or message.get('animation') or not isinstance(document, dict):
        return message
    mime = document.get('mime_type') or ''
    if mime not in IMAGE_MIMES and not mime.startswith('video/'):
        return message
    file = {k:v for k,v in document.items() if k in ('file_id', 'file_unique_id', 'file_size', 'mime_type')}
    return {**{k:v for k,v in message.items() if k != 'document'}, **({'photo':[file]} if mime in IMAGE_MIMES else {'video':file})}


def refused(messages, body):
    sizes = [m.get(k, {}).get('file_size', 0) for m in messages + [body] for k in ('video', 'voice')]
    sizes += [(m.get('photo') or [{}])[-1].get('file_size', 0) for m in messages + [body]]
    return 'Файл больше 512 МБ: сожми его или раздели.' if max(sizes) > 512*1024*1024 else 'Пакет превышает лимит 1 ГБ или 20 файлов. Нажми «Готово» или «Отменить».' if sum(sizes) > 1024*1024*1024 or len(messages) >= 20 else None


def configured(config, business):
    instagram = bool(config.get('instagram'))
    return {'tg-channel':bool((config.get('channels') or {}).get('tg-channel')), 'tg-story':bool(business), 'ig-post':instagram, 'ig-reel':instagram, 'ig-story':instagram, 'threads':bool(config.get('threads'))}


def offered(config, business):
    have = configured(config, business)
    return [key for key, label, targets in GROUPS if any(have[t] for t in targets)]


UNSUPPORTED = {'ig-reel':'нужно видео','ig-post':'нужно фото или видео','ig-story':'нужно фото или видео','tg-story':'нужно фото или видео'}
SKIP_NAMES = {'ig-reel':'Instagram Reels','ig-post':'Instagram-пост','ig-story':'Instagram-сторис','tg-story':'Telegram-сторис','threads':'Threads','tg-channel':'Telegram-канал'}


def unsupported(targets, photos, video):
    missing = {'ig-reel':not video,'ig-post':not (photos or video),'ig-story':not (photos or video),'tg-story':not (photos or video)}
    return {target:UNSUPPORTED[target] for target in targets if missing.get(target)}


def skipped_lines(reasons):
    return ['Не пойдёт в ' + SKIP_NAMES[target] + ': ' + reason + '.' for target,reason in reasons.items()]


def chosen_targets(chosen, config, business):
    have = configured(config, business)
    return [t for key, label, targets in GROUPS if key in chosen for t in targets if have[t]]


def collect_keyboard(item_id, token, offers=(), chosen=()):
    toggles = [{'text':('☑︎ ' if key in chosen else '☐ ') + label, 'callback_data':'tg' + str(index) + ':' + item_id + ':' + token} for index, (key, label, targets) in enumerate(GROUPS) if key in offers]
    values = [{'text':label, 'callback_data':verb + ':' + item_id + ':' + token} for verb, label in (('done', 'Готово'), ('cncl', 'Отменить'))]
    if any(len(x['callback_data'].encode()) > 64 for x in values + toggles):
        raise StateError('Callback exceeds 64 bytes')
    return {'inline_keyboard':[toggles[i:i + 2] for i in range(0, len(toggles), 2)] + [values]}


def hinted(text, offers):
    return text + '\n' + CHOICE_HINT if text and offers else text


def clips(item):
    count = sum(1 for m in item['messages'] if imaged(m).get('video'))
    return str(count) + ' ' + plural(count, ('клип', 'клипа', 'клипов'))


def contents(item, genitive=False):
    messages = item['messages']
    photos = sum(1 for m in messages if imaged(m).get('photo'))
    videos = sum(1 for m in messages if imaged(m).get('video'))
    voices = sum(1 for m in messages if m.get('voice') or m.get('audio'))
    parts = []
    if photos:
        parts.append(str(photos) + ' фото')
    if videos:
        parts.append(clips(item))
    if voices:
        parts.append(str(voices) + ' ' + plural(voices, ('голосового' if genitive else 'голосовое', 'голосовых', 'голосовых')))
    if any(m.get('text') or m.get('caption') for m in messages):
        parts.append('текста' if genitive else 'текст')
    return parts


PIN_SPEC_STAGES = [('running','публикую'),('approved','публикую'),('scheduled','запланирован'),('rendering','монтаж'),('needs-edit','жду правку'),('drafting',None),('previewed','ждёт «Опубликовать»')]


def scheduled(spec):
    try:
        return spec.get('state') == 'approved' and datetime.fromisoformat(spec['spec']['at'].replace('Z', '+00:00')) > datetime.now(timezone.utc)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def newest_specs(specs):
    tie = ['drafting','needs-edit','previewed','rendering','approved','running']
    newest = {}
    for spec in specs:
        if spec.get('item') and not spec.get('operation') and spec.get('state') in tie:
            rank = (spec.get('updated', 0), tie.index(spec['state']))
            if spec['item'] not in newest or rank >= newest[spec['item']][0]:
                newest[spec['item']] = (rank, spec)
    return {key:value[1] for key,value in newest.items()}


def newest_states(specs):
    return {key:'scheduled' if scheduled(spec) else spec['state'] for key,spec in newest_specs(specs).items()}


FINAL_DEADLINE = 2400
UPLOAD_DEADLINE = 86400


DRAFT_DEADLINE = 1800


def stale_limit(item, busy):
    return UPLOAD_DEADLINE if item.get('waiting_upload') else DRAFT_DEADLINE + (FINAL_DEADLINE if busy else 0)


def stale(item, now, busy):
    return item.get('status') in ('queued','drafting') and now - (item.get('updated') or item.get('created') or 0) > stale_limit(item, busy)


def overdue(job, now):
    since = job.get('created') or (job.get('progress') or {}).get('since')
    return bool(since) and now - since > FINAL_DEADLINE


def dead_render(job, item):
    return not item or item.get('status') in ('rejected', 'failed') or bool(item.get('published')) or job.get('mode') != 'final' and 'revision' in job and job['revision'] != item.get('revision')


def pin_stage(item, state):
    working = 'монтаж' if item.get('montage') else 'готовлю черновик'
    if item.get('waiting_upload') and item.get('status') in ('queued','drafting'):
        return 'жду загрузку файла'
    if item.get('status') == 'collecting':
        parts = contents(item)
        return 'собираю' + (' · ' + ', '.join(parts) if parts else '')
    labels = dict(PIN_SPEC_STAGES)
    if state in labels:
        return labels[state] or working
    return working if item.get('status') in ('queued','drafting') else None


def taken(item):
    parts = contents(item, True)
    if not parts:
        return 'Готовлю пост из материала. Превью пришлю сюда.'
    text = parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' и ' + parts[-1]
    return 'Готовлю пост из ' + text + titled(item) + '. Превью пришлю сюда.'


def collected(item):
    parts = contents(item)
    return 'Добавил в пост' + titled(item) + (': ' + ', '.join(parts) if parts else '') + '.'


class StateError(Exception):
    pass


def snippet(text):
    text = ' '.join((text or '').split())
    return ' «' + (text[:40] + '…' if len(text) > 40 else text) + '»' if text else ''


def named(saved, item=None):
    return (titled(item) if item else '') or snippet(next(iter((saved.get('spec') or {}).get('copy', {}).values()), ''))


def feedback(saved, item, note):
    note = ' '.join(note.split())
    return 'Правлю' + named(saved, item) + ': ' + (note[:80] + '…' if len(note) > 80 else note) + '.'


def asking(saved, item):
    return 'Читаю по посту' + named(saved, item) + ': отвечу или пришлю превью правки.'


def publishing(saved, item=None):
    targets = [x for x in (saved.get('spec') or {}).get('targets', []) if x in NAMES]
    if saved.get('state') == 'rendering':
        return 'Собираю пост' + named(saved, item) + ' в полном качестве, потом опубликую.'
    lines = ['Публикую пост' + named(saved, item) + ': готово ' + str(sum(1 for x in targets if x in saved.get('results', {}))) + ' из ' + str(len(targets)) + '.']
    lines += [NAMES[x] + ': ' + saved['errors'][x] for x in targets if x in saved.get('errors', {}) and x not in saved.get('results', {})]
    return '\n'.join(lines)


def due_line(saved, item, zone):
    try:
        due = datetime.fromisoformat(saved['spec']['at'].replace('Z', '+00:00')).astimezone(ZoneInfo(zone)).strftime('%d.%m %H:%M')
    except (ValueError, TypeError, KeyError, AttributeError):
        return 'Пост' + named(saved, item) + ' запланирован.'
    return 'Пост' + named(saved, item) + ' запланирован на ' + due + '.'


def fixing(saved, item):
    return 'Правлю опубликованный пост' + named(saved, item) + '. Превью изменений пришлю сюда.'


def deleting(saved, item):
    return 'Готовлю удаление поста' + named(saved, item) + '. Превью пришлю сюда.'


def progress(item, job, now):
    name = 'Монтаж' if item.get('montage') else 'Превью'
    hint = ' Если нужна правка, напиши её утверждением.'
    state = (job or {}).get('state')
    if state == 'failed':
        return name + titled(item) + ': сборка не удалась, скоро напишу.' + hint
    if state == 'done':
        return name + titled(item) + ' идёт: собираю превью.' + hint
    stage = (job or {}).get('progress')
    if not stage or state not in ('queued', 'running'):
        return name + titled(item) + ' идёт: ' + ('составляю план.' if item.get('montage') else 'готовлю превью.') + hint
    seconds = int(now - stage['since'])
    return name + titled(item) + ' идёт: ' + stage['label'].lower() + ', ' + str(seconds // 60) + ':' + format(seconds % 60, '02') + '.' + hint


def located(message):
    venue, point = message.get('venue'), message.get('location')
    if not venue and not point:
        return message
    if venue:
        text = 'Место: ' + ', '.join(x for x in (venue.get('title'), venue.get('address')) if x)
    else:
        text = 'Место (координаты): ' + str(point['latitude']) + ', ' + str(point['longitude'])
    return {**{k: v for k, v in message.items() if k not in ('venue', 'location')}, 'text': text}


def titled(item):
    texts = [m.get('text') or m.get('caption') for m in item.get('messages', []) if m.get('text') or m.get('caption')]
    return snippet(next((t for t in texts if not t.startswith(('Место: ', 'Место (координаты): '))), texts[0] if texts else ''))


PRICED = 'claude-sonnet-5-5'
PRICE = {'input':2.0, 'write5m':2.5, 'write1h':4.0, 'read':0.1, 'output':10.0, 'search':0.01}


def tally(usage):
    def number(source, name):
        value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    created = number(usage, 'cache_creation_input_tokens')
    split = usage.get('cache_creation') if isinstance(usage, dict) else getattr(usage, 'cache_creation', None)
    short, long = (number(split, 'ephemeral_5m_input_tokens'), number(split, 'ephemeral_1h_input_tokens')) if split else (created, 0)
    tools = usage.get('server_tool_use') if isinstance(usage, dict) else getattr(usage, 'server_tool_use', None)
    row = {'calls':1, 'input_tokens':number(usage, 'input_tokens'), 'output_tokens':number(usage, 'output_tokens'), 'cache_read_tokens':number(usage, 'cache_read_input_tokens'), 'cache_write_tokens':short + long, 'web_searches':number(tools, 'web_search_requests') if tools else 0}
    row['usd'] = (row['input_tokens'] * PRICE['input'] + short * PRICE['write5m'] + long * PRICE['write1h'] + row['cache_read_tokens'] * PRICE['read'] + row['output_tokens'] * PRICE['output']) / 1e6 + row['web_searches'] * PRICE['search']
    return row


class Store:
    def __init__(self, db):
        self.db = db

    def get(self, path, transaction=None):
        snapshot = self.db.document(path).get(transaction=transaction)
        return snapshot.to_dict() if snapshot.exists else None

    def put(self, path, data, transaction=None):
        if path.startswith('specs/') and path.count('/') == 1 and isinstance(data, dict):
            data['updated'] = time.time()
        reference = self.db.document(path)
        if transaction is None:
            reference.set(data)
        else:
            transaction.set(reference, data)

    def atomic(self, operation):
        if hasattr(self.db, 'run_transaction'):
            return self.db.run_transaction(operation)
        from google.api_core import exceptions
        from google.cloud import firestore
        for attempt in range(3):
            try:
                return firestore.transactional(operation)(self.db.transaction())
            except exceptions.InvalidArgument as failure:
                if 'transaction' not in str(failure).lower() or attempt == 2:
                    raise

    def delete(self, path, transaction=None):
        reference = self.db.document(path)
        if transaction is None:
            reference.delete()
        else:
            transaction.delete(reference)

    def keep(self, key, value, guard):
        path, field, expected = guard
        def save(tx):
            if (self.get(path, tx) or {}).get(field) != expected:
                return False
            self.put(key, value, tx)
            return True
        return self.atomic(save)

    def seen(self, key, tag):
        saved = self.get(key) or {}
        return saved.get('before', 0) if saved.get('tag') == tag else saved.get('edits', 0)

    def forget(self, item_id, transaction=None, fix=True):
        for suffix in ('', '-montage') + (('-fix',) if fix else ()):
            self.delete('conversations/' + item_id + suffix, transaction)

    def change(self, path, operation):
        def apply(tx):
            value = self.get(path, tx) or {}
            result = operation(value)
            self.put(path, value, tx)
            return result
        return self.atomic(apply)

    def job(self, tx, payload, identifier, delay=0):
        self.put('outbox/' + identifier, {'payload':payload, 'delay':delay, 'sent':False}, tx)

    def flush(self, enqueue):
        for snapshot in self.db.collection('outbox').where('sent', '==', False).stream():
            job = snapshot.to_dict()
            if not job.get('sent'):
                enqueue(job['payload'], snapshot.id, job.get('delay', 0))
                self.db.document('outbox/' + snapshot.id).delete()

    def intake(self, update, config, taps=None):
        self.edited = None
        message = imaged(located(update.get('message', {})))
        query = update.get('callback_query')
        identifier = str(update['update_id'])
        found = self.draft_ids() if command(message) == '/post' else None
        def handle(tx):
            self.edited = None
            if taps is not None:
                del taps[:]
            update_path = 'updates/' + identifier
            seen = self.get(update_path, tx)
            if seen:
                return None if query else seen.get('reply')
            business = self.get('tokens/business', tx) or config.get('business')
            if query:
                return self.callback(tx, update_path, query, config, business, taps)
            if 'business_connection' in update:
                connection = update['business_connection']
                self.put(update_path, {'received':time.time()}, tx)
                self.put('tokens/business', {'business_connection_id':connection['id']}, tx)
                return 'Подключение Business сохранено; права проверяются перед публикацией.'
            session = self.get('sessions/inbox', tx) or {}
            reply_id = str(message.get('reply_to_message', {}).get('message_id', ''))
            text = message.get('text', '')
            name = command(message)
            if name != '/post':
                name = None
            replied = self.get('published-messages/' + reply_id, tx) if reply_id else None
            spec = self.get('specs/' + replied['slug'], tx) if replied else None
            current = self.get('items/' + session['collecting'], tx) if session.get('collecting') else None
            if not current or current.get('status') != 'collecting':
                session.pop('collecting', None)
            if name != '/post' and (replied or not session.get('collecting')) and isinstance(message.get('photo'), list) and message['photo'] and message['photo'][-1].get('file_size', 0) > 20*1024*1024:
                self.put(update_path, {'received':time.time()}, tx)
                return 'Скриншот больше 20 МБ: пришли его как фото, не файлом.'
            if name != '/post' and spec and spec['state'] == 'previewed' and not spec.get('operation') and (text or message.get('photo') or message.get('voice')) and not any(message.get(k) for k in ('video', 'video_note', 'animation', 'document', 'audio')):
                head = (message['reply_to_message'].get('text') or message['reply_to_message'].get('caption', '')).split('\n', 1)[0]
                names = spec['preview_targets'].get(reply_id, []) if 'preview_targets' in spec else [x.strip() for x in head.replace(' cover', '').split(',') if x.strip() in spec['spec']['targets']]
                quote = message.get('quote', {}).get('text')
                body, shot = self.edit_note(message)
                note = ('[' + ', '.join(names) + '] ' if names else '') + ('цитата: "' + quote + '" — правка: ' if quote else '') + body
                item = self.queue_edit(tx, replied['slug'], spec, note, shot, identifier, session)
                if spec.get('preview_message'):
                    self.job(tx, {'slug':replied['slug'], 'step':'remove-keyboard', 'message':spec['preview_message']}, 'unkey-' + identifier)
                self.put('sessions/inbox', session, tx)
                self.put(update_path, {'received':time.time()}, tx)
                return None
            published = self.published_match(reply_id, tx) if text and reply_id and name != '/post' else None
            if published:
                item = self.get('items/' + published[1]['item'], tx)
                self.put(update_path, {'received':time.time()}, tx)
                self.queue_fix(tx, published[0], published[1], 'fix', text, identifier)
                return fixing(published[1], item)
            text = message.get('text', message.get('caption', ''))
            note = text.strip()
            photo = message.get('photo')
            dropped = []
            if name == '/post':
                session.pop('awaiting', None)
                dropped = self.open_drafts(tx, session, found)
                if dropped:
                    session.pop('current', None)
                    if session.get('collecting') in {entry['id'] for entry, label in dropped}:
                        session.pop('collecting', None)
            waiting = session.get('awaiting')
            if waiting and not name and (message.get('text') or waiting['kind'] == 'edit' and (photo or message.get('voice'))):
                session.pop('awaiting')
                target = self.get('specs/' + waiting['slug'], tx)
                if waiting['kind'] == 'edit' and target and target['state'] == 'needs-edit':
                    item = self.queue_edit(tx, waiting['slug'], target, *self.edit_note(message), identifier, session)
                    self.put('sessions/inbox', session, tx)
                    self.put(update_path, {'received':time.time()}, tx)
                    return None
                if waiting['kind'] == 'fix' and self.fixable(target) and message.get('text'):
                    item = self.get('items/' + target['item'], tx)
                    self.queue_fix(tx, waiting['slug'], target, 'fix', message['text'], identifier)
                    self.put('sessions/inbox', session, tx)
                    self.put(update_path, {'received':time.time()}, tx)
                    return fixing(target, item)
            opened = session.get('collecting')
            offers = offered(config, business)
            slug = session.get('current')
            target = self.get('specs/' + slug, tx) if slug else None
            pending = self.get('items/' + slug, tx) if slug and not target else None
            group = message.get('media_group_id')
            drafting = bool(pending and pending.get('status') in ('queued', 'drafting')) or bool(target and not target.get('operation') and target.get('state') in ('previewed', 'drafting', 'needs-edit'))
            running = bool(target and not target.get('operation') and target.get('state') in ('approved', 'running', 'rendering') and not scheduled(target))
            starts = not name and not opened and not drafting and not running and not (group and session.get('answered_group') == group) and any(message.get(k) for k in MEDIA_KEYS)
            if starts:
                session.pop('awaiting', None)
            def once(write=True):
                if group and session.get('answered_group') == group:
                    return True
                if group:
                    session['answered_group'] = group
                    if write:
                        self.put('sessions/inbox', session, tx)
                return False
            if name == '/post' or opened or starts:
                item_id = opened or 'item-' + identifier
                item = self.get('items/' + item_id, tx) or {'messages':[], 'revision':0, 'status':'collecting', 'created':time.time(), 'slug':item_id[:40], 'batch_token':uuid.uuid4().hex[:8]}
                body = without_command(message) if name == '/post' else message
                filled = bool(body.get('text') or body.get('caption') or any(body.get(k) for k in MEDIA_KEYS))
                refusal = refused(item['messages'], body) if filled else None
                if refusal:
                    self.put(update_path, {'received':time.time()}, tx)
                    if once():
                        return None
                    return {'item':item_id, 'revision':item['revision'], 'text':refusal, 'reply_markup':collect_keyboard(item_id, item['batch_token'], offers, item.get('chosen', []))} if opened else refusal
                continuing = False
                if filled:
                    item['messages'].append(body)
                    item['revision'] += 1
                    item['updated'] = time.time()
                    if group:
                        continuing = item.get('collect_group') == group
                        item['collect_group'] = group
                session['collecting'] = item_id
                large = any(v.get('file_size', 0) > 20*1024*1024 and str(m['message_id']) + suffix not in item.get('uploads', {}) for m in item['messages'] for suffix, v in (('.mp4', m.get('video') or {}), ('.jpg', (m.get('photo') or [{}])[-1])))
                if large:
                    item['waiting_upload'] = True
                    self.job(tx, {'item':item_id, 'step':'upload-prompt', 'revision':item['revision']}, 'upload-' + identifier, 10)
                self.discard(tx, dropped, session, identifier)
                self.put(update_path, {'received':time.time()}, tx)
                self.put('items/' + item_id, item, tx)
                self.put('sessions/inbox', session, tx)
                if name == '/post' and not opened:
                    reply = 'Новый пост. Присылай текст, фото, видео или голос, потом нажми кнопку.' if not filled else collected(item)
                elif filled:
                    reply = None if continuing else collected(item)
                else:
                    reply = 'Пост' + titled(item) + ' уже собирается. Присылай материал и нажми «Готово».'
                if dropped:
                    reply = 'Отменяю старые черновики: ' + ', '.join(label for entry, label in dropped) + '.\n' + reply
                markup = collect_keyboard(item_id, item['batch_token'], offers, item.get('chosen', []))
                if continuing and not dropped:
                    if item.get('keyboard_message') and item.get('keyboard_group') == group:
                        return {'item':item_id, 'revision':item['revision'], 'text':None, 'refresh':{'message_id':item['keyboard_message'], 'text':hinted(collected(item), offers), 'reply_markup':markup}}
                    return {'item':item_id, 'revision':item['revision'], 'text':None, 'reply_markup':markup}
                return {'item':item_id, 'revision':item['revision'], 'group':group, 'text':hinted(reply, offers), 'reply_markup':markup}
            if message.get('video') and not name and (pending and pending.get('status') in ('queued', 'drafting') or target and not target.get('operation') and target.get('state') in ('previewed', 'drafting', 'needs-edit')):
                held = pending or self.get('items/' + target['item'], tx)
                limit = refused(held['messages'], message)
                if limit:
                    self.put(update_path, {'received':time.time()}, tx)
                    return None if once() else limit
                added = (note + ' ' if note else '') + '[добавлен клип]'
                if target:
                    previewed = target['state'] == 'previewed'
                    item = self.queue_edit(tx, slug, target, added, None, identifier, session, message)
                    if previewed and target.get('preview_message'):
                        self.job(tx, {'slug':slug, 'step':'remove-keyboard', 'message':target['preview_message']}, 'unkey-' + identifier)
                else:
                    item = pending
                    self.add_edit(tx, item, slug, added, None, identifier, message)
                self.put(update_path, {'received':time.time()}, tx)
                self.put('sessions/inbox', session, tx)
                return None if once() else 'Добавил в черновик' + named(target or {}, item) + ': клип.'
            if pending and pending.get('status') in ('queued', 'drafting') and not pending.get('waiting_upload') and (note or photo or message.get('voice')) and not any(message.get(k) for k in ('video', 'video_note', 'animation', 'document', 'audio')):
                if note and note.rstrip().endswith('?') and not photo and not message.get('voice'):
                    job = self.get('renders/' + pending['render'], tx) if pending.get('render') else None
                    self.put(update_path, {'received':time.time()}, tx)
                    return None if once() else progress(pending, job, time.time())
                edit, shot = self.edit_note(message)
                key = 'g' + group if group else identifier
                followup = self.get('followups/' + key, tx) if group else None
                if not followup or followup.get('moved') or followup.get('slug') != slug:
                    followup = {'slug':slug, 'item':slug, 'notes':[], 'messages':[], 'token':uuid.uuid4().hex[:8]}
                if len(('newp:' + key + ':' + followup['token']).encode()) > 64:
                    raise StateError('Callback exceeds 64 bytes')
                once(False)
                self.add_edit(tx, pending, slug, edit, shot, identifier)
                pending['followup'] = key
                self.put('items/' + slug, pending, tx)
                followup['notes'].append(edit)
                followup['messages'].append(message)
                self.put('followups/' + key, followup, tx)
                self.put('sessions/inbox', session, tx)
                self.put(update_path, {'received':time.time()}, tx)
                return None
            if target and not target.get('operation') and target.get('state') in ('approved', 'running', 'rendering', 'done', 'partial'):
                held = self.get('items/' + target['item'], tx) if target.get('item') else None
                self.put(update_path, {'received':time.time()}, tx)
                if target['state'] in ('done', 'partial') and self.fixable(target) and note and not any(message.get(k) for k in ('video', 'video_note', 'animation', 'document', 'audio')):
                    self.queue_fix(tx, slug, target, 'fix', note, identifier)
                    return asking(target, held)
                if scheduled(target):
                    return None if once() else due_line(target, held, (config or {}).get('timezone', 'Asia/Makassar'))
                if target['state'] in ('approved', 'running', 'rendering'):
                    return None if once() else publishing(target, held)
                return None if once() else 'Пост' + named(target, held) + ' уже опубликован. Правка текстом, новый пост: /post.'
            if pending and pending.get('status') in ('queued', 'drafting') and pending.get('waiting_upload') and (note or photo or message.get('voice')):
                self.put(update_path, {'received':time.time()}, tx)
                return None if once() else 'Пост' + titled(pending) + ': ' + pin_stage(pending, None) + '.'
            if not target or target.get('operation') or target.get('state') not in ('previewed', 'drafting', 'needs-edit') or not note and not photo and not message.get('voice') or any(message.get(k) for k in ('video', 'video_note', 'animation', 'document', 'audio')):
                self.put(update_path, {'received':time.time()}, tx)
                return None if once() else 'К черновику принимаю текст, фото, видео и голос. Новый пост начинается с /post.' if target and not target.get('operation') and target.get('state') in ('previewed', 'drafting', 'needs-edit') else POST_HINT
            edit, shot = self.edit_note(message)
            key = 'g' + group if group else identifier
            followup = self.get('followups/' + key, tx) if group else None
            if not followup or followup.get('moved') or followup.get('slug') != slug:
                followup = {'slug':slug, 'item':target['item'], 'notes':[], 'messages':[], 'token':uuid.uuid4().hex[:8]}
            if len(('newp:' + key + ':' + followup['token']).encode()) > 64:
                raise StateError('Callback exceeds 64 bytes')
            once(False)
            previewed = target['state'] == 'previewed'
            item = self.queue_edit(tx, slug, target, edit, shot, identifier, session)
            item['followup'] = key
            self.put('items/' + target['item'], item, tx)
            if previewed and target.get('preview_message'):
                self.job(tx, {'slug':slug, 'step':'remove-keyboard', 'message':target['preview_message']}, 'unkey-' + identifier)
            followup['notes'].append(edit)
            followup['messages'].append(message)
            self.put('followups/' + key, followup, tx)
            self.put('sessions/inbox', session, tx)
            self.put(update_path, {'received':time.time()}, tx)
            return None
        def apply(tx):
            result = handle(tx)
            if isinstance(result, str) and not query:
                self.put('updates/' + identifier, {'received':time.time(), 'reply':result}, tx)
            return result
        return self.atomic(apply)

    def draft_ids(self):
        items, specs = set(), set()
        for status in ('collecting', 'queued', 'drafting'):
            items.update(snapshot.id for snapshot in self.db.collection('items').where('status', '==', status).stream())
        for state in ('drafting', 'previewed', 'needs-edit'):
            specs.update(snapshot.id for snapshot in self.db.collection('specs').where('state', '==', state).stream())
        return items, specs

    def open_drafts(self, tx, session, found):
        posts = {}
        def post(item_id, item):
            return posts.setdefault(item_id, {'id':item_id, 'item':item, 'specs':[]})
        opened = self.get('items/' + session['collecting'], tx) if session.get('collecting') else None
        if opened and opened.get('messages'):
            post(session['collecting'], opened)
        current = {session['current']} if session.get('current') else set()
        for item_id in sorted(found[0] | current):
            saved = self.get('items/' + item_id, tx)
            if saved and (saved.get('status') in ('queued', 'drafting') or saved.get('status') == 'collecting' and (saved.get('messages') or item_id != session.get('collecting'))):
                post(item_id, saved)
        for slug in sorted(found[1] | current):
            saved = self.get('specs/' + slug, tx)
            if saved and saved.get('state') in ('drafting', 'previewed', 'needs-edit') and not saved.get('operation') and saved.get('item'):
                post(saved['item'], None)['specs'].append((slug, saved))
        for entry in posts.values():
            if entry['item'] is None:
                entry['item'] = self.get('items/' + entry['id'], tx)
        posts = {k: v for k, v in posts.items() if (v['item'] or {}).get('status') != 'rejected'}
        return [(entry, (named(entry['specs'][0][1] if entry['specs'] else {}, entry['item']) or ' без названия').strip()) for entry in posts.values()]

    def discard(self, tx, dropped, session, identifier):
        for entry, label in dropped:
            item = entry['item']
            self.forget(entry['id'], tx)
            if item:
                item.update(status='rejected', waiting_upload=False, updated=time.time(), revision=item.get('revision', 0) + 1)
                self.put('items/' + entry['id'], item, tx)
                if item.get('keyboard_message'):
                    self.job(tx, {'step':'remove-keyboard', 'slug':entry['id'], 'message':item['keyboard_message']}, 'discard-' + identifier + '-' + entry['id'] + '-keyboard')
            for slug, saved in entry['specs']:
                saved['state'] = 'rejected'
                self.put('specs/' + slug, saved, tx)
                if saved.get('preview_message'):
                    self.job(tx, {'slug':slug, 'step':'remove-keyboard', 'message':saved['preview_message'], 'line':'Черновик отменён.'}, 'discard-' + identifier + '-' + slug)

    def retire(self, taps, query, line, markup=None, strike=False):
        message = query.get('message') or {}
        if taps is None or not message.get('message_id'):
            return None
        base = (message.get('text') or '').replace('\n' + CHOICE_HINT, '')
        taps.append({'message_id':message['message_id'], 'text':base, 'line':line, 'markup':markup, **({'strike':True} if strike else {})})
        return {'id':message['message_id'], 'text':base, 'line':line}

    def edit_note(self, message):
        note = (message.get('text') or message.get('caption') or '').strip()
        photo = message.get('photo')
        voice = message.get('voice')
        if voice:
            placeholder = '[голосовое сообщение ' + str(message['message_id']) + ']'
            return (note + ' ' + placeholder if note else placeholder), {'message_id':message['message_id'], 'voice':voice, 'note':placeholder}
        if not photo:
            return note, None
        return (note + ' [см. приложенный скриншот]' if note else 'Правка на приложенном скриншоте.'), {'message_id':message['message_id'], 'photo':photo[-1:]}

    def add_edit(self, tx, item, item_id, note, shot, identifier, clip=None):
        item.setdefault('edits', []).append(note)
        if clip:
            item['messages'].append(clip)
            item.pop('prepared', None)
            item['montage'] = any(imaged(m).get('video') for m in item['messages']) and not any(imaged(m).get('photo') for m in item['messages'])
            if any(v.get('file_size', 0) > 20*1024*1024 and str(m['message_id']) + suffix not in item.get('uploads', {}) for m in item['messages'] for suffix, v in (('.mp4', imaged(m).get('video') or {}), ('.jpg', (imaged(m).get('photo') or [{}])[-1]))):
                item['waiting_upload'] = True
        if shot:
            item.setdefault('edit_voices' if shot.get('voice') else 'edit_photos', []).append(shot)
        item['status'] = 'queued'
        item['revision'] += 1
        item['updated'] = time.time()
        item.pop('followup', None)
        self.edited = item_id
        self.put('items/' + item_id, item, tx)
        self.job(tx, {'item':item_id, 'step':'montage' if item.get('montage') else 'draft', 'revision':item['revision']}, 'edit-' + identifier, 10)
        if item.get('waiting_upload'):
            self.job(tx, {'item':item_id, 'step':'upload-prompt', 'revision':item['revision']}, 'upload-' + identifier, 10)

    def queue_edit(self, tx, slug, spec, note, shot, identifier, session, clip=None):
        item = self.get('items/' + spec['item'], tx)
        self.add_edit(tx, item, spec['item'], note, shot, identifier, clip)
        spec['state'] = 'drafting'
        spec['hash'] = ''
        self.put('specs/' + slug, spec, tx)
        self.put('specs/' + slug + '/edits/' + identifier, {'text':note}, tx)
        session['current'] = slug
        return item

    def fixable(self, saved):
        return bool(saved and saved.get('state') in ('done','partial') and not saved.get('deleted'))

    def queue_fix(self, tx, slug, source, operation, text, identifier):
        name = 'fix-' + identifier
        self.put('specs/' + name, {'operation':operation,'source':slug,'item':source['item'], 'instruction':text,'state':'drafting','results':{},'errors':{},'hash':''}, tx)
        self.job(tx, {'step':'fix','slug':name}, 'fix-' + identifier)

    def supersede(self, tx, item_id, keep=(), drafts=False):
        if not item_id:
            return
        found = [snapshot.id for snapshot in self.db.collection('specs').where('item', '==', item_id).stream() if snapshot.id not in keep and snapshot.to_dict().get('state') in ('previewed', 'needs-edit') and not (drafts and snapshot.to_dict().get('operation'))]
        current = [(slug, self.get('specs/' + slug, tx)) for slug in found]
        for slug, saved in current:
            if saved and saved.get('state') in ('previewed', 'needs-edit'):
                saved['state'] = 'rejected'
                self.put('specs/' + slug, saved, tx)
                if saved.get('preview_message'):
                    self.job(tx, {'slug':slug, 'step':'remove-keyboard', 'message':saved['preview_message']}, 'superseded-' + slug)

    def published_match(self, reply, tx):
        linked = self.get('published-messages/' + reply, tx)
        if not linked:
            return None
        saved = self.get('specs/' + linked['slug'], tx)
        return (linked['slug'], saved) if self.fixable(saved) else None

    def callback(self, tx, update_path, query, config=None, business=None, taps=None):
        callback_path = 'callbacks/' + query['id']
        if self.get(callback_path, tx):
            return None
        parts = query.get('data', '').split(':')
        if len(parts) != 3 or len(query.get('data', '').encode()) > 64:
            return None
        verb, slug, short_hash = parts
        if not slug or '/' in slug:
            return None
        if verb == 'newp':
            return self.new_post_callback(tx, update_path, callback_path, slug, short_hash, query, taps)
        if verb == 'again':
            return self.again_callback(tx, update_path, callback_path, slug, short_hash, query, taps)
        if verb == 'ulnk':
            return self.upload_link_callback(tx, update_path, callback_path, slug, query, taps)
        batch = verb in ('done','cncl') or verb in TOGGLES
        path = ('items/' if batch else 'specs/') + slug
        saved = self.get(path, tx)
        item = self.get('items/' + saved['item'], tx) if saved and saved.get('item') and not batch else None
        session = self.get('sessions/inbox', tx) or {}
        pending = self.get('specs/' + saved['deleting'], tx) if verb == 'pdel' and not batch and saved and saved.get('deleting') else None
        identifier = update_path.split('/', 1)[1]
        self.put(update_path, {'received':time.time()}, tx)
        self.put(callback_path, {'received':time.time()}, tx)
        if batch:
            return self.collect_callback(tx, verb, slug, short_hash, saved, session, identifier, query, offered(config or {}, business), taps, config or {}, business)
        if not saved or len(short_hash) != 8 or saved.get('hash', '')[:8] != short_hash:
            return None
        payload = {'slug':slug, 'step':'apply-fix' if saved.get('operation') else 'run'}
        line, status = None, False
        if verb in ('fix','del') and saved.get('operation') == ('fix' if verb == 'fix' else 'delete') and saved['state'] == 'previewed':
            saved['state'] = 'approved'
            saved['approved_hash'] = saved['hash']
            line, status = ('Публикую правку…' if verb == 'fix' else 'Удаляю…'), True
        elif verb == 'pub' and not saved.get('operation') and saved['state'] == 'previewed':
            saved['state'] = 'approved'
            saved['approved_hash'] = saved['hash']
            payload['say'] = 'Публикую' + named(saved, item) + '.'
            line, status = 'Публикую' + named(saved, item) + '…', True
        elif verb == 'edit' and not saved.get('operation') and saved['state'] == 'previewed':
            saved['state'] = 'needs-edit'
            payload['step'] = 'edit-prompt'
            session['awaiting'] = {'kind':'edit','slug':slug}
            self.put('sessions/inbox', session, tx)
            line = 'Жду правку.'
        elif verb == 'pdel' and not saved.get('operation') and self.fixable(saved):
            if pending and pending.get('state') in ('drafting', 'previewed', 'approved', 'running'):
                return 'Удаление уже готовится.'
            saved['deleting'] = 'fix-' + identifier
            self.put(path, saved, tx)
            self.queue_fix(tx, slug, saved, 'delete', 'delete', identifier)
            return deleting(saved, item)
        elif verb == 'pfix' and not saved.get('operation') and session.get('awaiting') == {'kind':'fix','slug':slug}:
            return 'Жду правку: напиши, что поменять.'
        elif verb == 'pfix' and not saved.get('operation') and self.fixable(saved):
            session['awaiting'] = {'kind':'fix','slug':slug}
            self.put('sessions/inbox', session, tx)
            payload['step'] = 'fix-prompt'
        elif verb == 'rej' and saved['state'] == 'previewed':
            saved['state'] = 'rejected'
            if saved.get('item'):
                self.job(tx, {'step':'learn', 'item':saved['item'], 'slug':slug, 'outcome':'rejected', 'forget':True}, 'learn-' + hashlib.sha256(query['id'].encode()).hexdigest())
            payload['step'] = 'remove-keyboard'
            payload['say'] = 'Черновик' + named(saved, item) + ' отклонён.'
            line = 'Отклонено.'
        elif verb == 'retry' and saved['state'] == 'partial':
            saved['state'] = 'approved'
            line, status = 'Повторяю…', True
        elif verb == 'edit' and not saved.get('operation') and saved['state'] == 'needs-edit' and session.get('awaiting') == {'kind':'edit','slug':slug}:
            return 'Жду правку: напиши, что поменять.'
        else:
            return None
        if line:
            record = self.retire(taps, query, line, strike=verb == 'edit')
            if record and status:
                saved['status_message'] = record
        self.put(path, saved, tx)
        self.job(tx, payload, 'callback-' + hashlib.sha256(query['id'].encode()).hexdigest())
        if verb == 'edit':
            return 'Напиши, что поменять в черновике' + named(saved, item) + '.'
        return True

    def again_callback(self, tx, update_path, callback_path, item_id, revision, query, taps):
        item = self.get('items/' + item_id, tx)
        self.put(update_path, {'received':time.time()}, tx)
        self.put(callback_path, {'received':time.time()}, tx)
        if not item or item.get('status') != 'failed' or str(item.get('revision')) != revision:
            self.retire(taps, query, 'Повтор больше не нужен.')
            return None
        self.retire(taps, query, 'Повторяю…')
        item.update(status='queued', updated=time.time())
        item.pop('status_message', None)
        self.put('items/' + item_id, item, tx)
        self.job(tx, {'item':item_id, 'step':'montage' if item.get('montage') else 'draft', 'revision':item['revision']}, 'again-' + update_path.split('/', 1)[1])
        return True

    def upload_link_callback(self, tx, update_path, callback_path, item_id, query, taps):
        item = self.get('items/' + item_id, tx)
        self.put(update_path, {'received':time.time()}, tx)
        self.put(callback_path, {'received':time.time()}, tx)
        if not item or not item.get('waiting_upload') or item.get('status') not in ('collecting', 'queued'):
            self.retire(taps, query, 'Ссылка больше не нужна.')
            return None
        self.retire(taps, query, 'Готовлю новую ссылку…')
        item['updated'] = time.time()
        self.put('items/' + item_id, item, tx)
        self.job(tx, {'step':'upload-prompt', 'item':item_id, 'revision':item['revision']}, 'upload-link-' + update_path.split('/', 1)[1])
        return True

    def new_post_callback(self, tx, update_path, callback_path, identifier, token, query=None, taps=None):
        followup = self.get('followups/' + identifier, tx)
        valid = bool(followup) and followup['token'] == token and not followup.get('moved')
        item = self.get('items/' + followup['item'], tx) if valid else None
        spec = self.get('specs/' + followup['slug'], tx) if valid else None
        fresh = self.get('items/item-' + identifier, tx) if valid else None
        session = self.get('sessions/inbox', tx) or {}
        opened = self.get('items/' + session['collecting'], tx) if session.get('collecting') else None
        answered = valid and bool(followup.get('answered'))
        thread_path = 'conversations/' + followup['item'] + ('-montage' if item and item.get('montage') else '') if valid else None
        thread = self.get(thread_path, tx) if answered and item else None
        self.put(update_path, {'received':time.time()}, tx)
        self.put(callback_path, {'received':time.time()}, tx)
        if answered:
            live = bool(item and spec) and spec['state'] == 'previewed' and item.get('status') == 'previewed'
        else:
            live = bool(item) and item.get('status') in ('queued', 'drafting') and (not spec or spec['state'] == 'drafting')
        if not valid or fresh or not live:
            return None
        edits = item.get('edits', [])
        removed = 0
        for note in followup.get('notes') or [followup['note']]:
            if note in edits:
                edits.pop(len(edits) - 1 - edits[::-1].index(note))
                removed += 1
        moved = {m['message_id'] for m in followup.get('messages') or [followup['message']]}
        for key in ('edit_photos', 'edit_voices'):
            item[key] = [x for x in item.get(key, []) if x['message_id'] not in moved]
        followup['moved'] = True
        if answered:
            item['edits_applied'] = len(edits)
            item['photos_applied'] = len(item.get('edit_photos', []))
            if thread:
                thread['edits'] = max(0, thread.get('edits', 0) - removed)
                self.put(thread_path, thread, tx)
        else:
            item['status'] = 'queued'
            item['revision'] += 1
            item['updated'] = time.time()
            self.job(tx, {'item':followup['item'], 'step':'montage' if item.get('montage') else 'draft', 'revision':item['revision']}, 'unmove-' + identifier, 10)
        target = session['collecting'] if opened and opened.get('status') == 'collecting' else 'item-' + identifier
        created = opened if target == session.get('collecting') else {'messages':[], 'revision':0, 'status':'collecting', 'created':time.time(), 'slug':target[:40], 'batch_token':uuid.uuid4().hex[:8]}
        created['messages'].extend(followup.get('messages') or [followup['message']])
        created['revision'] += 1
        created['updated'] = time.time()
        session['collecting'] = target
        self.job(tx, {'item':target, 'step':'collect-prompt', 'revision':created['revision']}, 'collect-' + identifier)
        self.put('items/' + followup['item'], item, tx)
        self.put('items/' + target, created, tx)
        self.put('sessions/inbox', session, tx)
        self.put('followups/' + identifier, followup, tx)
        self.retire(taps, query or {}, 'Новый пост открыт.')
        return 'Новый пост открыт.'

    def collect_callback(self, tx, verb, item_id, token, item, session, identifier, query=None, offers=(), taps=None, config=None, business=None):
        if not item or item.get('status') != 'collecting' or item.get('batch_token') != token:
            return None
        query = query or {}
        if verb in TOGGLES:
            key = TOGGLES[verb]
            if key not in offers:
                return None
            chosen = item.get('chosen', [])
            item['chosen'] = [x for x in chosen if x != key] if key in chosen else chosen + [key]
            self.put('items/' + item_id, item, tx)
            if self.retire(taps, query, None, collect_keyboard(item_id, token, offers, item['chosen'])):
                taps[-1].update(item=item_id, token=token, offers=list(offers))
            label = next(x[1] for x in GROUPS if x[0] == key)
            return ('Отмечено: ' if key in item['chosen'] else 'Снято: ') + label
        if verb == 'done' and not item['messages']:
            return 'Пост пустой. Пришли текст, фото, видео или голос.'
        if verb == 'done' and offers and not [x for x in item.get('chosen', []) if x in offers]:
            return CHOICE_HINT
        video = any(imaged(m).get('video') for m in item['messages'])
        photos = any(imaged(m).get('photo') for m in item['messages'])
        if verb == 'done' and item.get('chosen'):
            chosen = chosen_targets(item['chosen'], config or {}, business)
            reasons = unsupported(chosen, photos, video)
            if chosen and len(reasons) == len(chosen):
                return ' '.join(skipped_lines(reasons))
        montage = video and not photos
        if session.get('collecting') == item_id:
            session.pop('collecting')
        item['revision'] += 1
        item['updated'] = time.time()
        if verb == 'cncl':
            item['status'] = 'rejected'
            item['waiting_upload'] = False
            result = 'Пост отменён.'
            self.retire(taps, query, 'Отменено.')
            self.job(tx, {'step':'say', 'say':'Пост' + titled(item) + ' отменён.'}, 'say-' + identifier)
        else:
            session['current'] = item['slug']
            item['status'] = 'queued'
            item['montage'] = montage
            self.job(tx, {'item':item_id, 'step':'montage' if montage else 'draft', 'revision':item['revision']}, 'draft-' + identifier)
            result = 'Монтирую ' + clips(item) + titled(item) + '. Превью пришлю сюда.' if montage else taken(item)
            record = self.retire(taps, query, ('Монтирую ' + clips(item) if montage else 'Готовлю превью') + titled(item) + '…')
            if record:
                item['status_message'] = record
        self.put('items/' + item_id, item, tx)
        self.put('sessions/inbox', session, tx)
        return result

    def claim_item(self, identifier, revision):
        def claim(value):
            if value.get('status') != 'queued' or value.get('revision') != revision or value.get('waiting_upload'):
                return False
            value['status'] = 'drafting'
            value['updated'] = time.time()
            return copy.deepcopy(value)
        return self.change('items/' + identifier, claim)

    def claim_run(self, slug):
        def claim(tx):
            value = self.get('specs/' + slug, tx) or {}
            claimed = value.get('state') == 'approved' and value.get('hash') == value.get('approved_hash')
            if claimed:
                value['state'] = 'running'
                value['started_at'] = time.time()
                self.supersede(tx, value.get('item'), {slug}, True)
            self.put('specs/' + slug, value, tx)
            return copy.deepcopy(value) if claimed else False
        return self.atomic(claim)

    def checkpoint(self, slug, name, action, complete=None):
        path = 'specs/' + slug + '/checkpoints/' + hashlib.sha256(name.encode()).hexdigest()
        def claim(value):
            if value.get('state') == 'done':
                return ('done', value['value'])
            if value.get('state') == 'pending':
                raise StateError('Uncertain outcome: reconcile saved checkpoint before retry')
            value.update(state='pending', name=name)
            return ('new', None)
        status, result = self.change(path, claim)
        if status == 'done':
            return result
        try:
            result = action()
        except Exception as failure:
            status = getattr(failure, 'status_code', None)
            if isinstance(status, int) and 400 <= status < 500:
                self.put(path, {'state':'failed', 'name':name})
            raise
        def finish(tx):
            if complete:
                complete(tx)
            self.put(path, {'state':'done', 'name':name, 'value':result}, tx)
        self.atomic(finish)
        return result

    def invalidate(self, slug, name):
        path = 'specs/' + slug + '/checkpoints/' + hashlib.sha256(name.encode()).hexdigest()
        self.put(path, {'state':'failed', 'name':name})

    def record_usage(self, item, kind, usage):
        try:
            row = tally(usage)
            model = usage.get('model') if isinstance(usage, dict) else getattr(usage, 'model', None)
            if isinstance(model, str) and not model.startswith(PRICED):
                logging.error(json.dumps({'code':'usage_model_unpriced','model':model[:100]}, ensure_ascii=False))
            month = datetime.now(timezone.utc).strftime('%Y-%m')
            def apply(tx):
                paths = ['usage/anthropic-' + month] + (['usage/item-' + item] if item else [])
                values = [self.get(path, tx) or {} for path in paths]
                targets = [values[0]] + ([values[1].setdefault('anthropic', {}).setdefault(kind, {})] if item else [])
                for target in targets:
                    for name, amount in row.items():
                        target[name] = target.get(name, 0) + amount
                if item:
                    values[1]['updated'] = time.time()
                for path, value in zip(paths, values):
                    self.put(path, value, tx)
            self.atomic(apply)
        except Exception as failure:
            logging.error(json.dumps({'code':'usage_record_failed','error':str(failure)[:200]}, ensure_ascii=False))

    def reserve_speech(self, seconds, reservation):
        month = datetime.now(timezone.utc).strftime('%Y-%m')
        def reserve(value):
            if reservation in value.get('reservations', []):
                raise StateError('Speech reservation already used; no automatic paid replay')
            if value.get('seconds', 0) + seconds > 55 * 60:
                raise StateError('Лимит распознавания 55 минут. Пришли текст.')
            value['seconds'] = value.get('seconds', 0) + seconds
            value.setdefault('reservations', []).append(reservation)
        self.change('usage/' + month, reserve)
        if '/' in reservation:
            try:
                def add(value):
                    value['speech_seconds'] = value.get('speech_seconds', 0) + seconds
                    value['updated'] = time.time()
                self.change('usage/item-' + reservation.split('/')[0], add)
            except Exception as failure:
                logging.error(json.dumps({'code':'usage_record_failed','error':str(failure)[:200]}, ensure_ascii=False))
