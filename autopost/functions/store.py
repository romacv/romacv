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


def batch_keyboard(item_id, token):
    values = [{'text':label,'callback_data':verb + ':' + item_id + ':' + token} for verb,label in (('mont','Смонтировать'),('cncl','Отменить'))]
    if any(len(x['callback_data'].encode()) > 64 for x in values):
        raise StateError('Callback exceeds 64 bytes')
    return {'inline_keyboard':[values]}


def clips(item):
    count = sum(1 for m in item['messages'] if m.get('video'))
    return str(count) + ' ' + plural(count, ('клип', 'клипа', 'клипов'))


def taken(item):
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
        parts.append(str(voices) + ' ' + plural(voices, ('голосового', 'голосовых', 'голосовых')))
    if any(m.get('text') or m.get('caption') for m in messages):
        parts.append('текста')
    if not parts:
        return 'Готовлю пост из материала. Превью пришлю сюда.'
    text = parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' и ' + parts[-1]
    return 'Готовлю пост из ' + text + titled(item) + '. Превью пришлю сюда.'


class StateError(Exception):
    pass


def snippet(text):
    text = ' '.join((text or '').split())
    return ' «' + (text[:40] + '…' if len(text) > 40 else text) + '»' if text else ''


def named(saved, item=None):
    return (titled(item) if item else '') or snippet(next(iter((saved.get('spec') or {}).get('copy', {}).values()), ''))


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
                item = self.queue_edit(tx, replied['slug'], spec, note, identifier, session)
                self.job(tx, {'slug':replied['slug'], 'step':'remove-keyboard', 'message':spec.get('preview_message')}, 'unkey-' + identifier)
                self.put('sessions/inbox', session, tx)
                self.put(update_path, {'received':time.time()}, tx)
                return editing(spec, item)
            published = self.published_match(reply_id, tx) if text and reply_id else None
            if published:
                item = self.get('items/' + published[1]['item'], tx)
                self.put(update_path, {'received':time.time()}, tx)
                self.queue_fix(tx, published[0], published[1], 'fix', text, identifier)
                return fixing(published[1], item)
            for key in ('video_batch', 'upload_item'):
                current = self.get('items/' + session[key], tx) if session.get(key) else None
                if not current or current.get('status') not in ('queued', 'collecting'):
                    session.pop(key, None)
            text = message.get('text', message.get('caption', ''))
            waiting = session.get('awaiting')
            if waiting and message.get('text') and text.strip() != '/upload':
                session.pop('awaiting')
                target = self.get('specs/' + waiting['slug'], tx)
                if waiting['kind'] == 'edit' and target and target['state'] == 'needs-edit':
                    item = self.queue_edit(tx, waiting['slug'], target, message['text'], identifier, session)
                    self.put('sessions/inbox', session, tx)
                    self.put(update_path, {'received':time.time()}, tx)
                    return editing(target, item)
                if waiting['kind'] == 'fix' and self.fixable(target):
                    item = self.get('items/' + target['item'], tx)
                    self.queue_fix(tx, waiting['slug'], target, 'fix', message['text'], identifier)
                    self.put('sessions/inbox', session, tx)
                    self.put(update_path, {'received':time.time()}, tx)
                    return fixing(target, item)
            window = session.get('editing')
            if window and (window['until'] <= time.time() or message.get('text') is None or reply_id or session.get('video_batch') or text.strip() == '/upload'):
                if window['until'] <= time.time():
                    session.pop('editing')
                window = None
            if window:
                target = self.get('specs/' + window['slug'], tx)
                if target and target['state'] == 'drafting' and not target.get('operation'):
                    token = uuid.uuid4().hex[:8]
                    markup = {'inline_keyboard':[[{'text':'Это новый пост','callback_data':'newp:' + identifier + ':' + token}]]}
                    if len(markup['inline_keyboard'][0][0]['callback_data'].encode()) > 64:
                        raise StateError('Callback exceeds 64 bytes')
                    item = self.queue_edit(tx, window['slug'], target, message['text'], identifier, session)
                    self.put('followups/' + identifier, {'slug':window['slug'], 'item':target['item'], 'note':message['text'], 'message':message, 'token':token}, tx)
                    self.put('sessions/inbox', session, tx)
                    self.put(update_path, {'received':time.time()}, tx)
                    return {'text':'Добавил к правке черновика' + named(target, item) + '.', 'reply_markup':markup}
                session.pop('editing')
            if text.strip() == '/upload' and session.get('upload_item'):
                waiting = self.get('items/' + session['upload_item'], tx)
                self.put(update_path, {'received':time.time()}, tx)
                if waiting and waiting.get('waiting_upload'):
                    self.job(tx, {'step':'upload-prompt','item':session['upload_item'],'revision':waiting['revision']}, 'upload-link-' + identifier)
                return 'Готовлю ссылку на загрузку.'
            video = message.get('video')
            group = message.get('media_group_id')
            attached = not video and bool(session.get('video_batch')) and bool(message.get('text') or message.get('voice') or message.get('audio'))
            item_id = session.get('video_batch') if video or attached else None
            item_id = item_id or ('album-' + str(group) if group else 'item-' + identifier)
            item = self.get('items/' + item_id, tx) or {'messages':[], 'revision':0, 'status':'queued', 'created':time.time(), 'slug':item_id[:40]}
            if item['status'] not in ('queued', 'collecting'):
                self.put(update_path, {'received':time.time()}, tx)
                return 'Материал уже обрабатывается. Пришли новый пакет.'
            sizes = [m.get(k, {}).get('file_size', 0) for m in item['messages'] + [message] for k in ('video', 'voice')]
            if sum(sizes) > 1024*1024*1024 or len(item['messages']) >= 20:
                self.put(update_path, {'received':time.time()}, tx)
                return 'Пакет превышает лимит 1 ГБ или 20 файлов. Раздели материал.'
            joined = item_id.startswith('album-') and bool(item['messages'])
            item['messages'].append(message)
            item['revision'] += 1
            item['updated'] = time.time()
            if video or attached:
                item['status'] = 'collecting'
                item.setdefault('batch_token', uuid.uuid4().hex[:8])
                session['video_batch'] = item_id
            else:
                item['status'] = 'queued'
                self.job(tx, {'item':item_id, 'step':'montage' if item.get('montage') else 'draft', 'revision':item['revision']}, 'draft-' + identifier, 10)
            large = any(m.get('video', {}).get('file_size', 0) > 20*1024*1024 and str(m['message_id']) + '.mp4' not in item.get('uploads', {}) for m in item['messages'])
            if large:
                item['waiting_upload'] = True
                session['upload_item'] = item_id
                self.job(tx, {'item':item_id, 'step':'upload-prompt', 'revision':item['revision']}, 'upload-' + identifier, 10)
            self.put(update_path, {'received':time.time()}, tx)
            self.put('items/' + item_id, item, tx)
            self.put('sessions/inbox', session, tx)
            if item['status'] == 'collecting':
                return {'item':item_id, 'revision':item['revision'], 'text':'Добавил к клипам' + snippet(message.get('text')) + '. Нажми «Смонтировать».' if attached else 'Клипы сохранены. Пришли ещё или нажми кнопку.', 'reply_markup':batch_keyboard(item_id, item['batch_token'])}
            return {'item':item_id, 'revision':item['revision'], 'text':None if joined else taken(item)}
        return self.atomic(apply)

    def queue_edit(self, tx, slug, spec, note, identifier, session):
        item = self.get('items/' + spec['item'], tx)
        item.setdefault('edits', []).append(note)
        item['status'] = 'queued'
        item['revision'] += 1
        spec['state'] = 'drafting'
        spec['hash'] = ''
        self.put('specs/' + slug, spec, tx)
        self.put('items/' + spec['item'], item, tx)
        self.put('specs/' + slug + '/edits/' + identifier, {'text':note}, tx)
        self.job(tx, {'item':spec['item'], 'step':'montage' if item.get('montage') else 'draft', 'revision':item['revision']}, 'edit-' + identifier, 10)
        session['editing'] = {'slug':slug, 'until':time.time() + 600}
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
        batch = verb in ('mont','cncl')
        path = ('items/' if batch else 'specs/') + slug
        saved = self.get(path, tx)
        item = self.get('items/' + saved['item'], tx) if saved and saved.get('item') and not batch else None
        session = self.get('sessions/inbox', tx) or {}
        identifier = update_path.split('/', 1)[1]
        self.put(update_path, {'received':time.time()}, tx)
        self.put(callback_path, {'received':time.time()}, tx)
        if batch:
            return self.batch_callback(tx, verb, slug, short_hash, saved, session, identifier)
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
        self.put(update_path, {'received':time.time()}, tx)
        self.put(callback_path, {'received':time.time()}, tx)
        if not valid or fresh or not item or not spec or spec['state'] != 'drafting' or item.get('status') not in ('queued', 'drafting') or followup['note'] not in item.get('edits', []):
            return None
        edits = item['edits']
        edits.pop(len(edits) - 1 - edits[::-1].index(followup['note']))
        item['status'] = 'queued'
        item['revision'] += 1
        followup['moved'] = True
        step = 'montage' if item.get('montage') else 'draft'
        self.job(tx, {'item':followup['item'], 'step':step, 'revision':item['revision']}, 'unmove-' + identifier, 10)
        created = {'messages':[followup['message']], 'revision':1, 'status':'queued', 'created':time.time(), 'updated':time.time(), 'slug':('item-' + identifier)[:40]}
        self.job(tx, {'item':'item-' + identifier, 'step':'draft', 'revision':1}, 'draft-' + identifier, 10)
        self.put('items/' + followup['item'], item, tx)
        self.put('items/item-' + identifier, created, tx)
        self.put('followups/' + identifier, followup, tx)
        return taken(created)

    def batch_callback(self, tx, verb, item_id, token, item, session, identifier):
        if not item or item.get('status') != 'collecting' or item.get('batch_token') != token:
            return None
        if session.get('video_batch') == item_id:
            session.pop('video_batch')
        item['revision'] += 1
        item['updated'] = time.time()
        if verb == 'cncl':
            item['status'] = 'rejected'
            item['waiting_upload'] = False
            if session.get('upload_item') == item_id:
                session.pop('upload_item')
            result = 'Клипы отменены.'
        else:
            item['status'] = 'queued'
            item['montage'] = True
            self.job(tx, {'item':item_id, 'step':'montage', 'revision':item['revision']}, 'draft-' + identifier)
            result = 'Монтирую ' + clips(item) + titled(item) + '. Превью пришлю сюда.'
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
