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


def collect_keyboard(item_id, token):
    values = [{'text':label,'callback_data':verb + ':' + item_id + ':' + token} for verb,label in (('done','Готово'),('cncl','Отменить'))]
    if any(len(x['callback_data'].encode()) > 64 for x in values):
        raise StateError('Callback exceeds 64 bytes')
    return {'inline_keyboard':[values]}


def clips(item):
    count = sum(1 for m in item['messages'] if m.get('video'))
    return str(count) + ' ' + plural(count, ('клип', 'клипа', 'клипов'))


def contents(item, genitive=False):
    messages = item['messages']
    photos = sum(1 for m in messages if m.get('photo'))
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


def titled(item):
    return snippet(next((m.get('text') or m.get('caption') for m in item.get('messages', []) if m.get('text') or m.get('caption')), ''))


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

    def intake(self, update, config):
        message = update.get('message', {})
        query = update.get('callback_query')
        identifier = str(update['update_id'])
        def apply(tx):
            update_path = 'updates/' + identifier
            if self.get(update_path, tx):
                return None
            if query:
                return self.callback(tx, update_path, query)
            if 'business_connection' in update:
                connection = update['business_connection']
                self.put(update_path, {'received':time.time()}, tx)
                self.put('tokens/business', {'business_connection_id':connection['id']}, tx)
                return 'Подключение Business сохранено; права проверяются перед публикацией.'
            session = self.get('sessions/inbox', tx) or {}
            reply_id = str(message.get('reply_to_message', {}).get('message_id', ''))
            text = message.get('text', '')
            replied = self.get('published-messages/' + reply_id, tx) if reply_id and text else None
            spec = self.get('specs/' + replied['slug'], tx) if replied else None
            if spec and spec['state'] == 'previewed' and not spec.get('operation'):
                head = (message['reply_to_message'].get('text') or message['reply_to_message'].get('caption', '')).split('\n', 1)[0]
                names = [x.strip() for x in head.replace(' cover', '').split(',') if x.strip() in spec['spec']['targets']]
                quote = message.get('quote', {}).get('text')
                note = ('[' + ', '.join(names) + '] ' if names else '') + ('цитата: "' + quote + '" — правка: ' if quote else '') + text
                item = self.queue_edit(tx, replied['slug'], spec, note, None, identifier, session)
                if spec.get('preview_message'):
                    self.job(tx, {'slug':replied['slug'], 'step':'remove-keyboard', 'message':spec['preview_message']}, 'unkey-' + identifier)
                self.put('sessions/inbox', session, tx)
                self.put(update_path, {'received':time.time()}, tx)
                return editing(spec, item)
            published = self.published_match(reply_id, tx) if text and reply_id else None
            if published:
                item = self.get('items/' + published[1]['item'], tx)
                self.put(update_path, {'received':time.time()}, tx)
                self.queue_fix(tx, published[0], published[1], 'fix', text, identifier)
                return fixing(published[1], item)
            for key, statuses in (('collecting', ('collecting',)), ('upload_item', ('queued', 'collecting'))):
                current = self.get('items/' + session[key], tx) if session.get(key) else None
                if not current or current.get('status') not in statuses:
                    session.pop(key, None)
            name = command(message)
            if name not in COMMANDS:
                name = None
            text = message.get('text', message.get('caption', ''))
            note = text.strip()
            photo = message.get('photo')
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
            if name == '/post' or opened:
                item_id = opened or 'item-' + identifier
                item = self.get('items/' + item_id, tx) or {'messages':[], 'revision':0, 'status':'collecting', 'created':time.time(), 'slug':item_id[:40], 'batch_token':uuid.uuid4().hex[:8]}
                body = without_command(message) if name == '/post' else message
                filled = bool(body.get('text') or body.get('caption') or any(body.get(k) for k in MEDIA_KEYS))
                sizes = [m.get(k, {}).get('file_size', 0) for m in item['messages'] + [body] for k in ('video', 'voice')]
                if filled and (sum(sizes) > 1024*1024*1024 or len(item['messages']) >= 20):
                    self.put(update_path, {'received':time.time()}, tx)
                    return {'item':item_id, 'revision':item['revision'], 'text':'Пакет превышает лимит 1 ГБ или 20 файлов. Нажми «Готово» или «Отменить».', 'reply_markup':collect_keyboard(item_id, item['batch_token'])}
                group = message.get('media_group_id')
                joined = bool(group) and bool(item['messages']) and item['messages'][-1].get('media_group_id') == group
                if filled:
                    item['messages'].append(body)
                    item['revision'] += 1
                    item['updated'] = time.time()
                session['collecting'] = item_id
                large = any(m.get('video', {}).get('file_size', 0) > 20*1024*1024 and str(m['message_id']) + '.mp4' not in item.get('uploads', {}) for m in item['messages'])
                if large:
                    item['waiting_upload'] = True
                    session['upload_item'] = item_id
                    self.job(tx, {'item':item_id, 'step':'upload-prompt', 'revision':item['revision']}, 'upload-' + identifier, 10)
                self.put(update_path, {'received':time.time()}, tx)
                self.put('items/' + item_id, item, tx)
                self.put('sessions/inbox', session, tx)
                if name == '/post' and not opened:
                    reply = 'Новый пост. Присылай текст, фото, видео или голос, потом нажми кнопку.' if not filled else collected(item)
                elif filled:
                    reply = None if joined else collected(item)
                else:
                    reply = 'Пост' + titled(item) + ' уже собирается. Присылай материал и нажми «Готово».'
                return {'item':item_id, 'revision':item['revision'], 'text':reply, 'reply_markup':collect_keyboard(item_id, item['batch_token'])}
            slug = session.get('current')
            target = self.get('specs/' + slug, tx) if slug else None
            if not target or target.get('operation') or target.get('state') not in ('previewed', 'drafting', 'needs-edit') or not note and not photo and not message.get('voice'):
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

    def edit_note(self, message):
        note = (message.get('text') or message.get('caption') or '').strip()
        photo = message.get('photo')
        voice = message.get('voice')
        if voice and not note:
            placeholder = '[голосовое сообщение ' + str(message['message_id']) + ']'
            return placeholder, {'message_id':message['message_id'], 'voice':voice, 'note':placeholder}
        if not photo:
            return note, None
        return (note + ' [см. приложенный скриншот]' if note else 'Правка на приложенном скриншоте.'), {'message_id':message['message_id'], 'photo':photo[-1:]}

    def queue_edit(self, tx, slug, spec, note, shot, identifier, session):
        item = self.get('items/' + spec['item'], tx)
        item.setdefault('edits', []).append(note)
        if shot:
            item.setdefault('edit_voices' if shot.get('voice') else 'edit_photos', []).append(shot)
        item['status'] = 'queued'
        item['revision'] += 1
        spec['state'] = 'drafting'
        spec['hash'] = ''
        self.put('specs/' + slug, spec, tx)
        self.put('items/' + spec['item'], item, tx)
        self.put('specs/' + slug + '/edits/' + identifier, {'text':note}, tx)
        self.job(tx, {'item':spec['item'], 'step':'montage' if item.get('montage') else 'draft', 'revision':item['revision']}, 'edit-' + identifier, 10)
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

    def callback(self, tx, update_path, query):
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
            return self.new_post_callback(tx, update_path, callback_path, slug, short_hash)
        batch = verb in ('done','cncl')
        path = ('items/' if batch else 'specs/') + slug
        saved = self.get(path, tx)
        item = self.get('items/' + saved['item'], tx) if saved and saved.get('item') and not batch else None
        session = self.get('sessions/inbox', tx) or {}
        identifier = update_path.split('/', 1)[1]
        self.put(update_path, {'received':time.time()}, tx)
        self.put(callback_path, {'received':time.time()}, tx)
        if batch:
            return self.collect_callback(tx, verb, slug, short_hash, saved, session, identifier)
        if not saved or len(short_hash) != 8 or saved.get('hash', '')[:8] != short_hash:
            return None
        payload = {'slug':slug, 'step':'apply-fix' if saved.get('operation') else 'run'}
        if verb in ('fix','del') and saved.get('operation') == ('fix' if verb == 'fix' else 'delete') and saved['state'] == 'previewed':
            saved['state'] = 'approved'
            saved['approved_hash'] = saved['hash']
        elif verb == 'pub' and not saved.get('operation') and saved['state'] == 'previewed':
            saved['state'] = 'approved'
            saved['approved_hash'] = saved['hash']
        elif verb == 'edit' and not saved.get('operation') and saved['state'] == 'previewed':
            saved['state'] = 'needs-edit'
            payload['step'] = 'edit-prompt'
            session['awaiting'] = {'kind':'edit','slug':slug}
            self.put('sessions/inbox', session, tx)
        elif verb == 'pdel' and not saved.get('operation') and self.fixable(saved):
            self.queue_fix(tx, slug, saved, 'delete', 'delete', identifier)
            return deleting(saved, item)
        elif verb == 'pfix' and not saved.get('operation') and self.fixable(saved):
            session['awaiting'] = {'kind':'fix','slug':slug}
            self.put('sessions/inbox', session, tx)
            payload['step'] = 'fix-prompt'
        elif verb == 'rej' and saved['state'] == 'previewed':
            saved['state'] = 'rejected'
            payload['step'] = 'remove-keyboard'
        elif verb == 'retry' and saved['state'] == 'partial':
            saved['state'] = 'approved'
        else:
            return None
        self.put(path, saved, tx)
        self.job(tx, payload, 'callback-' + hashlib.sha256(query['id'].encode()).hexdigest())
        return True

    def new_post_callback(self, tx, update_path, callback_path, identifier, token):
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
        return 'Новый пост открыт.'

    def collect_callback(self, tx, verb, item_id, token, item, session, identifier):
        if not item or item.get('status') != 'collecting' or item.get('batch_token') != token:
            return None
        if verb == 'done' and not item['messages']:
            return 'Пост пустой. Пришли текст, фото, видео или голос.'
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
        else:
            video = any(m.get('video') for m in item['messages'])
            item['status'] = 'queued'
            item['montage'] = video
            self.job(tx, {'item':item_id, 'step':'montage' if video else 'draft', 'revision':item['revision']}, 'draft-' + identifier)
            result = 'Монтирую ' + clips(item) + titled(item) + '. Превью пришлю сюда.' if video else taken(item)
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
