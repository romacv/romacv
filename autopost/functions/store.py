import copy
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone


class StateError(Exception):
    pass


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
            reply_id = str(message.get('reply_to_message', {}).get('message_id', ''))
            edit = self.get('edits/' + reply_id, tx) if reply_id else None
            if edit and message.get('text'):
                path = 'specs/' + edit['slug']
                spec = self.get(path, tx)
                accepted = False
                if spec and spec['state'] == 'needs-edit' and spec.get('edit_prompt') == reply_id:
                    accepted = True
                    item = self.get('items/' + spec['item'], tx)
                    item.setdefault('edits', []).append(message['text'])
                    item['status'] = 'queued'
                    item['revision'] += 1
                    spec['state'] = 'drafting'
                    spec['hash'] = ''
                    self.put(path, spec, tx)
                    self.put('items/' + spec['item'], item, tx)
                    self.put('specs/' + edit['slug'] + '/edits/' + identifier, {'text':message['text']}, tx)
                    self.job(tx, {'item':spec['item'], 'step':'montage' if item.get('montage') else 'draft', 'revision':item['revision']}, 'edit-' + identifier)
                self.put(update_path, {'received':time.time()}, tx)
                return 'Правки приняты.' if accepted else 'Кнопка устарела'
            session = self.get('sessions/inbox', tx) or {}
            for key in ('video_batch', 'upload_item'):
                current = self.get('items/' + session[key], tx) if session.get(key) else None
                if not current or current.get('status') not in ('queued', 'collecting'):
                    session.pop(key, None)
            text = message.get('text', message.get('caption', ''))
            if text.strip() == '/upload' and session.get('upload_item'):
                waiting = self.get('items/' + session['upload_item'], tx)
                self.put(update_path, {'received':time.time()}, tx)
                if waiting and waiting.get('waiting_upload'):
                    self.job(tx, {'step':'upload-prompt','item':session['upload_item'],'revision':waiting['revision']}, 'upload-link-' + identifier)
                return 'Готовлю ссылку на загрузку.'
            video = message.get('video')
            montage = 'монтаж' in text.lower()
            group = message.get('media_group_id')
            item_id = session.get('video_batch') if (video or montage) else None
            item_id = item_id or ('album-' + str(group) if group else 'item-' + identifier)
            item = self.get('items/' + item_id, tx) or {'messages':[], 'revision':0, 'status':'queued', 'created':time.time(), 'slug':item_id[:40]}
            if item['status'] not in ('queued', 'collecting'):
                self.put(update_path, {'received':time.time()}, tx)
                return 'Материал уже обрабатывается. Пришли новый пакет.'
            sizes = [m.get(k, {}).get('file_size', 0) for m in item['messages'] + [message] for k in ('video', 'voice')]
            if sum(sizes) > 1024*1024*1024 or len(item['messages']) >= 20:
                self.put(update_path, {'received':time.time()}, tx)
                return 'Пакет превышает лимит 1 ГБ или 20 файлов. Раздели материал.'
            item['messages'].append(message)
            item['revision'] += 1
            item['updated'] = time.time()
            item['montage'] = item.get('montage', False) or montage
            if video and not montage:
                item['status'] = 'collecting'
                session['video_batch'] = item_id
            else:
                item['status'] = 'queued'
                if montage:
                    session.pop('video_batch', None)
                self.job(tx, {'item':item_id, 'step':'montage' if item['montage'] else 'draft', 'revision':item['revision']}, 'draft-' + identifier, 10)
            large = any(m.get('video', {}).get('file_size', 0) > 20*1024*1024 and str(m['message_id']) + '.mp4' not in item.get('uploads', {}) for m in item['messages'])
            if large:
                item['waiting_upload'] = True
                session['upload_item'] = item_id
                self.job(tx, {'item':item_id, 'step':'upload-prompt', 'revision':item['revision']}, 'upload-' + identifier, 10)
            self.put(update_path, {'received':time.time()}, tx)
            self.put('items/' + item_id, item, tx)
            self.put('sessions/inbox', session, tx)
            return {'item':item_id, 'revision':item['revision'], 'text':'Клипы сохранены. Пришли ещё или напиши «монтаж».' if item['status'] == 'collecting' else 'Принял, готовлю черновик.'}
        return self.atomic(apply)

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
        path = 'specs/' + slug
        saved = self.get(path, tx)
        self.put(update_path, {'received':time.time()}, tx)
        self.put(callback_path, {'received':time.time()}, tx)
        if not saved or len(short_hash) != 8 or saved.get('hash', '')[:8] != short_hash:
            return None
        payload = {'slug':slug, 'step':'run'}
        if verb == 'pub' and saved['state'] == 'previewed':
            saved['state'] = 'approved'
            saved['approved_hash'] = saved['hash']
        elif verb == 'edit' and saved['state'] == 'previewed':
            saved['state'] = 'needs-edit'
            payload['step'] = 'edit-prompt'
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

    def checkpoint(self, slug, name, action):
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
        self.put(path, {'state':'done', 'name':name, 'value':result})
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
