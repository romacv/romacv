import copy
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone



def plural(count, forms):
    if count % 10 == 1 and count % 100 != 11:
        return forms[0]
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return forms[1]
    return forms[2]


NAMES = {'tg-channel':'Telegram-канал','tg-story':'История Telegram','ig-post':'Instagram, пост','ig-reel':'Instagram, Reels','ig-story':'Instagram, история','threads':'Threads','blog':'Блог resrom.com'}
GROUPS = (('story','Сторис',('ig-story','tg-story')),('ig-post','Instagram-пост',('ig-post',)),('ig-reel','Instagram Reels',('ig-reel',)),('threads','Threads',('threads',)),('channel','Telegram-канал',('tg-channel',)),('site','Сайт',('blog',)))
TOGGLES = {'tg' + str(index):group[0] for index,group in enumerate(GROUPS)}
IMAGE_MIMES = ('image/jpeg', 'image/png', 'image/webp', 'image/heic')
CHOICE_HINT = 'Отметь, куда пойдёт пост.'
COMMANDS = ('/post', '/upload')
COMMANDS_HINT = 'Команды: /post — новый пост, /upload — загрузить большое видео.'
POST_HINT = 'Чтобы начать пост, отправь /post.'
MEDIA_KEYS = ('photo', 'video', 'voice', 'audio', 'document', 'animation', 'video_note')


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
    if message.get('photo') or not isinstance(document, dict) or document.get('mime_type') not in IMAGE_MIMES:
        return message
    photo = {k:v for k,v in document.items() if k in ('file_id', 'file_unique_id', 'file_size')}
    return {**{k:v for k,v in message.items() if k != 'document'}, 'photo':[photo]}


def configured(config, business):
    instagram = bool(config.get('instagram'))
    return {'tg-channel':bool((config.get('channels') or {}).get('tg-channel')), 'tg-story':bool(business), 'ig-post':instagram, 'ig-reel':instagram, 'ig-story':instagram, 'threads':bool(config.get('threads')), 'blog':bool(config.get('github_pat'))}


def offered(config, business):
    have = configured(config, business)
    return [key for key, label, targets in GROUPS if any(have[t] for t in targets)]


UNSUPPORTED = {'ig-reel':'нужно видео','ig-post':'нужно фото','ig-story':'нужно фото или видео','tg-story':'нужно фото или видео','blog':'нужен текст без видео'}
SKIP_NAMES = {'ig-reel':'Instagram Reels','ig-post':'Instagram-пост','ig-story':'Instagram-сторис','tg-story':'Telegram-сторис','blog':'Сайт','threads':'Threads','tg-channel':'Telegram-канал'}


def unsupported(targets, photos, video):
    missing = {'ig-reel':not video,'ig-post':video or not photos,'ig-story':not (photos or video),'tg-story':not (photos or video),'blog':video}
    return {target:UNSUPPORTED[target] for target in targets if missing.get(target)}


def skipped_lines(reasons):
    return ['Не пойдёт в ' + SKIP_NAMES[target] + ': ' + reason + '.' for target,reason in reasons.items()]


def chosen_targets(chosen, config, business):
    have = configured(config, business)
    return [t for key, label, targets in GROUPS if key in chosen for t in targets if have[t]]


def collect_keyboard(item_id, token, offers=(), chosen=()):
    toggles = [{'text':('✓ ' if key in chosen else '') + label, 'callback_data':'tg' + str(index) + ':' + item_id + ':' + token} for index, (key, label, targets) in enumerate(GROUPS) if key in offers]
    values = [{'text':label, 'callback_data':verb + ':' + item_id + ':' + token} for verb, label in (('done', 'Готово'), ('cncl', 'Отменить'))]
    if any(len(x['callback_data'].encode()) > 64 for x in values + toggles):
        raise StateError('Callback exceeds 64 bytes')
    return {'inline_keyboard':[toggles[i:i + 2] for i in range(0, len(toggles), 2)] + [values]}


def hinted(text, offers):
    return text + '\n' + CHOICE_HINT if text and offers else text


def clips(item):
    count = sum(1 for m in item['messages'] if m.get('video'))
    return str(count) + ' ' + plural(count, ('клип', 'клипа', 'клипов'))


def contents(item, genitive=False):
    messages = item['messages']
    photos = sum(1 for m in messages if imaged(m).get('photo'))
    videos = sum(1 for m in messages if m.get('video'))
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
    return 'Правлю черновик' + named(saved, item) + ': ' + (note[:80] + '…' if len(note) > 80 else note) + '.'


def editing(saved, item):
    return 'Правлю черновик' + named(saved, item) + '. Новое превью пришлю сюда.'


def fixing(saved, item):
    return 'Правлю опубликованный пост' + named(saved, item) + '. Превью изменений пришлю сюда.'


def deleting(saved, item):
    return 'Готовлю удаление поста' + named(saved, item) + '. Превью пришлю сюда.'


def added(item, note):
    note = ' '.join(note.split())
    return 'Добавил к черновику' + titled(item) + ': ' + (note[:80] + '…' if len(note) > 80 else note) + '.'


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


class Store:
    def __init__(self, db):
        self.db = db

    def get(self, path, transaction=None):
        snapshot = self.db.document(path).get(transaction=transaction)
        return snapshot.to_dict() if snapshot.exists else None

    def put(self, path, data, transaction=None):
        reference = self.db.document(path)
        if transaction is None:
            reference.set(data)
        else:
            transaction.set(reference, data)

    def atomic(self, operation):
        if hasattr(self.db, 'run_transaction'):
            return self.db.run_transaction(operation)
        from google.cloud import firestore
        return firestore.transactional(operation)(self.db.transaction())

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
        message = imaged(located(update.get('message', {})))
        query = update.get('callback_query')
        identifier = str(update['update_id'])
        def apply(tx):
            if taps is not None:
                del taps[:]
            update_path = 'updates/' + identifier
            if self.get(update_path, tx):
                return None
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
            if name not in COMMANDS:
                name = None
            replied = self.get('published-messages/' + reply_id, tx) if reply_id else None
            spec = self.get('specs/' + replied['slug'], tx) if replied else None
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
                return editing(spec, item)
            published = self.published_match(reply_id, tx) if text and reply_id and name != '/post' else None
            if published:
                item = self.get('items/' + published[1]['item'], tx)
                self.put(update_path, {'received':time.time()}, tx)
                self.queue_fix(tx, published[0], published[1], 'fix', text, identifier)
                return fixing(published[1], item)
            for key, statuses in (('collecting', ('collecting',)), ('upload_item', ('queued', 'collecting'))):
                current = self.get('items/' + session[key], tx) if session.get(key) else None
                if not current or current.get('status') not in statuses:
                    session.pop(key, None)
            text = message.get('text', message.get('caption', ''))
            note = text.strip()
            photo = message.get('photo')
            dropped = []
            if name == '/post':
                session.pop('awaiting', None)
                dropped = self.open_drafts(tx, session)
                if dropped:
                    session.pop('current', None)
                    session.pop('collecting', None)
            waiting = session.get('awaiting')
            if waiting and not name and (message.get('text') or waiting['kind'] == 'edit' and (photo or message.get('voice'))):
                session.pop('awaiting')
                target = self.get('specs/' + waiting['slug'], tx)
                if waiting['kind'] == 'edit' and target and target['state'] == 'needs-edit':
                    item = self.queue_edit(tx, waiting['slug'], target, *self.edit_note(message), identifier, session)
                    self.put('sessions/inbox', session, tx)
                    self.put(update_path, {'received':time.time()}, tx)
                    return editing(target, item)
                if waiting['kind'] == 'fix' and self.fixable(target) and message.get('text'):
                    item = self.get('items/' + target['item'], tx)
                    self.queue_fix(tx, waiting['slug'], target, 'fix', message['text'], identifier)
                    self.put('sessions/inbox', session, tx)
                    self.put(update_path, {'received':time.time()}, tx)
                    return fixing(target, item)
            if name == '/upload':
                waiting = self.get('items/' + session['upload_item'], tx) if session.get('upload_item') else None
                self.put(update_path, {'received':time.time()}, tx)
                if not waiting:
                    return COMMANDS_HINT
                if waiting.get('waiting_upload'):
                    self.job(tx, {'step':'upload-prompt','item':session['upload_item'],'revision':waiting['revision']}, 'upload-link-' + identifier)
                return 'Готовлю ссылку на загрузку.'
            opened = session.get('collecting')
            offers = offered(config, business)
            if name == '/post' or opened:
                item_id = opened or 'item-' + identifier
                item = self.get('items/' + item_id, tx) or {'messages':[], 'revision':0, 'status':'collecting', 'created':time.time(), 'slug':item_id[:40], 'batch_token':uuid.uuid4().hex[:8]}
                body = without_command(message) if name == '/post' else message
                filled = bool(body.get('text') or body.get('caption') or any(body.get(k) for k in MEDIA_KEYS))
                sizes = [m.get(k, {}).get('file_size', 0) for m in item['messages'] + [body] for k in ('video', 'voice')]
                if filled and (sum(sizes) > 1024*1024*1024 or len(item['messages']) >= 20):
                    self.put(update_path, {'received':time.time()}, tx)
                    return {'item':item_id, 'revision':item['revision'], 'text':'Пакет превышает лимит 1 ГБ или 20 файлов. Нажми «Готово» или «Отменить».', 'reply_markup':collect_keyboard(item_id, item['batch_token'], offers, item.get('chosen', []))} if opened else 'Пакет превышает лимит 1 ГБ или 20 файлов. Нажми «Готово» или «Отменить».'
                group = message.get('media_group_id')
                joined = bool(group) and bool(item['messages']) and item['messages'][-1].get('media_group_id') == group
                if filled and isinstance(body.get('photo'), list) and body['photo'] and isinstance(update.get('message', {}).get('document'), dict) and body['photo'][-1].get('file_size', 0) > 20*1024*1024:
                    self.put(update_path, {'received':time.time()}, tx)
                    return {'item':item_id, 'revision':item['revision'], 'text':'Картинка больше 20 МБ: пришли её как фото или сожми.', 'reply_markup':collect_keyboard(item_id, item['batch_token'], offers, item.get('chosen', []))} if opened else 'Картинка больше 20 МБ: пришли её как фото или сожми.'
                if filled:
                    item['messages'].append(body)
                    item['revision'] += 1
                    item['updated'] = time.time()
                session['collecting'] = item_id
                large = any(v.get('file_size', 0) > 20*1024*1024 and str(m['message_id']) + suffix not in item.get('uploads', {}) for m in item['messages'] for suffix, v in (('.mp4', m.get('video') or {}),))
                if large:
                    item['waiting_upload'] = True
                    session['upload_item'] = item_id
                    self.job(tx, {'item':item_id, 'step':'upload-prompt', 'revision':item['revision']}, 'upload-' + identifier, 10)
                self.discard(tx, dropped, session, identifier)
                self.put(update_path, {'received':time.time()}, tx)
                self.put('items/' + item_id, item, tx)
                self.put('sessions/inbox', session, tx)
                if name == '/post' and not opened:
                    reply = 'Новый пост. Присылай текст, фото, видео или голос, потом нажми кнопку.' if not filled else collected(item)
                elif filled:
                    reply = None if joined else collected(item)
                else:
                    reply = 'Пост' + titled(item) + ' уже собирается. Присылай материал и нажми «Готово».'
                if dropped:
                    reply = 'Отменяю старые черновики: ' + ', '.join(label for entry, label in dropped) + '.\n' + reply
                return {'item':item_id, 'revision':item['revision'], 'text':hinted(reply, offers), 'reply_markup':collect_keyboard(item_id, item['batch_token'], offers, item.get('chosen', []))}
            slug = session.get('current')
            target = self.get('specs/' + slug, tx) if slug else None
            pending = self.get('items/' + slug, tx) if slug and not target else None
            if pending and pending.get('status') in ('queued', 'drafting') and not pending.get('waiting_upload') and (note or photo or message.get('voice')) and not any(message.get(k) for k in ('video', 'video_note', 'animation', 'document', 'audio')):
                edit, shot = self.edit_note(message)
                self.add_edit(tx, pending, slug, edit, shot, identifier)
                self.put('sessions/inbox', session, tx)
                self.put(update_path, {'received':time.time()}, tx)
                return added(pending, edit)
            if not target or target.get('operation') or target.get('state') not in ('previewed', 'drafting', 'needs-edit') or not note and not photo and not message.get('voice') or any(message.get(k) for k in ('video', 'video_note', 'animation', 'document', 'audio')):
                self.put(update_path, {'received':time.time()}, tx)
                return 'К черновику принимаю текст, фото и голос. Новый пост начинается с /post.' if target and not target.get('operation') and target.get('state') in ('previewed', 'drafting', 'needs-edit') else POST_HINT
            edit, shot = self.edit_note(message)
            label = note or ('голосовое' if message.get('voice') else 'скриншот')
            token = uuid.uuid4().hex[:8]
            markup = {'inline_keyboard':[[{'text':'Это новый пост','callback_data':'newp:' + identifier + ':' + token}]]}
            if len(markup['inline_keyboard'][0][0]['callback_data'].encode()) > 64:
                raise StateError('Callback exceeds 64 bytes')
            previewed = target['state'] == 'previewed'
            item = self.queue_edit(tx, slug, target, edit, shot, identifier, session)
            if previewed and target.get('preview_message'):
                self.job(tx, {'slug':slug, 'step':'remove-keyboard', 'message':target['preview_message']}, 'unkey-' + identifier)
            self.put('followups/' + identifier, {'slug':slug, 'item':target['item'], 'note':edit, 'message':message, 'token':token}, tx)
            self.put('sessions/inbox', session, tx)
            self.put(update_path, {'received':time.time()}, tx)
            return {'text':feedback(target, item, label), 'reply_markup':markup}
        return self.atomic(apply)

    def open_drafts(self, tx, session):
        posts = {}
        def post(item_id, item):
            return posts.setdefault(item_id, {'id':item_id, 'item':item, 'specs':[]})
        opened = self.get('items/' + session['collecting'], tx) if session.get('collecting') else None
        if opened and opened.get('messages'):
            post(session['collecting'], opened)
        for status in ('queued', 'drafting'):
            for snapshot in self.db.collection('items').where('status', '==', status).stream():
                post(snapshot.id, snapshot.to_dict())
        for state in ('drafting', 'previewed', 'needs-edit'):
            for snapshot in self.db.collection('specs').where('state', '==', state).stream():
                saved = snapshot.to_dict()
                if not saved.get('operation') and saved.get('item'):
                    post(saved['item'], None)['specs'].append((snapshot.id, saved))
        for entry in posts.values():
            if entry['item'] is None:
                entry['item'] = self.get('items/' + entry['id'], tx)
        posts = {k: v for k, v in posts.items() if (v['item'] or {}).get('status') != 'rejected'}
        return [(entry, (named(entry['specs'][0][1] if entry['specs'] else {}, entry['item']) or ' без названия').strip()) for entry in posts.values()]

    def discard(self, tx, dropped, session, identifier):
        for entry, label in dropped:
            item = entry['item']
            if item:
                item.update(status='rejected', waiting_upload=False, updated=time.time(), revision=item.get('revision', 0) + 1)
                if session.get('upload_item') == entry['id']:
                    session.pop('upload_item')
                self.put('items/' + entry['id'], item, tx)
                if item.get('keyboard_message'):
                    self.job(tx, {'step':'remove-keyboard', 'slug':entry['id'], 'message':item['keyboard_message']}, 'discard-' + identifier + '-' + entry['id'] + '-keyboard')
            for slug, saved in entry['specs']:
                saved['state'] = 'rejected'
                self.put('specs/' + slug, saved, tx)
                if saved.get('preview_message'):
                    self.job(tx, {'slug':slug, 'step':'remove-keyboard', 'message':saved['preview_message'], 'line':'Черновик отменён.'}, 'discard-' + identifier + '-' + slug)

    def retire(self, taps, query, line, markup=None):
        message = query.get('message') or {}
        if taps is None or not message.get('message_id'):
            return None
        base = (message.get('text') or '').replace('\n' + CHOICE_HINT, '')
        taps.append({'message_id':message['message_id'], 'text':base, 'line':line, 'markup':markup})
        return {'id':message['message_id'], 'text':base}

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

    def add_edit(self, tx, item, item_id, note, shot, identifier):
        item.setdefault('edits', []).append(note)
        if shot:
            item.setdefault('edit_voices' if shot.get('voice') else 'edit_photos', []).append(shot)
        item['status'] = 'queued'
        item['revision'] += 1
        self.put('items/' + item_id, item, tx)
        self.job(tx, {'item':item_id, 'step':'montage' if item.get('montage') else 'draft', 'revision':item['revision']}, 'edit-' + identifier, 10)

    def queue_edit(self, tx, slug, spec, note, shot, identifier, session):
        item = self.get('items/' + spec['item'], tx)
        self.add_edit(tx, item, spec['item'], note, shot, identifier)
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
        batch = verb in ('done','cncl') or verb in TOGGLES
        path = ('items/' if batch else 'specs/') + slug
        saved = self.get(path, tx)
        item = self.get('items/' + saved['item'], tx) if saved and saved.get('item') and not batch else None
        session = self.get('sessions/inbox', tx) or {}
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
            pending = self.get('specs/' + saved['deleting'], tx) if saved.get('deleting') else None
            if pending and pending.get('state') in ('drafting', 'previewed', 'approved'):
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
            record = self.retire(taps, query, line)
            if record and status:
                saved['status_message'] = record
        self.put(path, saved, tx)
        self.job(tx, payload, 'callback-' + hashlib.sha256(query['id'].encode()).hexdigest())
        if verb == 'edit':
            return 'Напиши, что поменять в черновике' + named(saved, item) + '.'
        return True

    def new_post_callback(self, tx, update_path, callback_path, identifier, token, query=None, taps=None):
        followup = self.get('followups/' + identifier, tx)
        valid = bool(followup) and followup['token'] == token and not followup.get('moved')
        item = self.get('items/' + followup['item'], tx) if valid else None
        spec = self.get('specs/' + followup['slug'], tx) if valid else None
        fresh = self.get('items/item-' + identifier, tx) if valid else None
        session = self.get('sessions/inbox', tx) or {}
        opened = self.get('items/' + session['collecting'], tx) if session.get('collecting') else None
        self.put(update_path, {'received':time.time()}, tx)
        self.put(callback_path, {'received':time.time()}, tx)
        if not valid or fresh or not item or not spec or spec['state'] != 'drafting' or item.get('status') not in ('queued', 'drafting') or followup['note'] not in item.get('edits', []):
            return None
        edits = item['edits']
        edits.pop(len(edits) - 1 - edits[::-1].index(followup['note']))
        for key in ('edit_photos', 'edit_voices'):
            item[key] = [x for x in item.get(key, []) if x['message_id'] != followup['message']['message_id']]
        item['status'] = 'queued'
        item['revision'] += 1
        followup['moved'] = True
        step = 'montage' if item.get('montage') else 'draft'
        self.job(tx, {'item':followup['item'], 'step':step, 'revision':item['revision']}, 'unmove-' + identifier, 10)
        target = session['collecting'] if opened and opened.get('status') == 'collecting' else 'item-' + identifier
        created = opened if target == session.get('collecting') else {'messages':[], 'revision':0, 'status':'collecting', 'created':time.time(), 'slug':target[:40], 'batch_token':uuid.uuid4().hex[:8]}
        created['messages'].append(followup['message'])
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
            self.retire(taps, query, None, collect_keyboard(item_id, token, offers, item['chosen']))
            label = next(x[1] for x in GROUPS if x[0] == key)
            return ('Отмечено: ' if key in item['chosen'] else 'Снято: ') + label
        if verb == 'done' and not item['messages']:
            return 'Пост пустой. Пришли текст, фото, видео или голос.'
        if verb == 'done' and offers and not [x for x in item.get('chosen', []) if x in offers]:
            return CHOICE_HINT
        video = any(m.get('video') for m in item['messages'])
        if verb == 'done' and item.get('chosen'):
            chosen = chosen_targets(item['chosen'], config or {}, business)
            reasons = unsupported(chosen, not video and any(imaged(m).get('photo') for m in item['messages']), video)
            if chosen and len(reasons) == len(chosen):
                return ' '.join(skipped_lines(reasons))
        if verb == 'done' and video and any(m.get('photo') for m in item['messages']):
            return 'В монтаж идут видео, текст и голос. Фото пришли отдельным /post.'
        if session.get('collecting') == item_id:
            session.pop('collecting')
        item['revision'] += 1
        item['updated'] = time.time()
        if verb == 'cncl':
            item['status'] = 'rejected'
            item['waiting_upload'] = False
            if session.get('upload_item') == item_id:
                session.pop('upload_item')
            result = 'Пост отменён.'
            self.retire(taps, query, 'Отменено.')
            self.job(tx, {'step':'say', 'say':'Пост' + titled(item) + ' отменён.'}, 'say-' + identifier)
        else:
            session['current'] = item['slug']
            item['status'] = 'queued'
            item['montage'] = video
            self.job(tx, {'item':item_id, 'step':'montage' if video else 'draft', 'revision':item['revision']}, 'draft-' + identifier)
            result = 'Монтирую ' + clips(item) + titled(item) + '. Превью пришлю сюда.' if video else taken(item)
            record = self.retire(taps, query, ('Монтирую ' + clips(item) if video else 'Готовлю превью') + titled(item) + '…')
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
            return copy.deepcopy(value)
        return self.change('items/' + identifier, claim)

    def claim_run(self, slug):
        def claim(value):
            if value.get('state') != 'approved' or value.get('hash') != value.get('approved_hash'):
                return False
            value['state'] = 'running'
            value['started_at'] = time.time()
            return copy.deepcopy(value)
        return self.change('specs/' + slug, claim)

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
