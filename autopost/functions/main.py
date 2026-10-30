import contextvars
from concurrent.futures import ThreadPoolExecutor
import copy
import difflib
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import exceptions as admin_exceptions
from firebase_admin import firestore, storage, functions
from firebase_functions import https_fn, tasks_fn, scheduler_fn, options
from firebase_functions.params import SecretParam

import platforms
from platforms import Error
from store import EXPIRED_TEXT, TOKEN_PREFIX, retryable, table_text, table_rows, tally, SESSION_AGE, contents, FINAL_DEADLINE, stale_limit, overdue, stale, PIN_SPEC_STAGES, newest_states, newest_specs, post_phrase, progress as working, dead_render, pin_stage, TOGGLES, hint_for, Store, StateError, collect_keyboard, collected, bursted, BURST_SECONDS, command, clips, named, feedback, titled, imaged, offered, chosen_targets, unsupported, skipped_lines, UNSUPPORTED, SKIP_NAMES, hinted, NAMES, COMMANDS
import agents
import drafter
import voice
import learning
import progress as progress_view
import media
import montage

AUTOPOST = SecretParam('AUTOPOST')
PROGRESS_URL = 'https://www.resrom.com/p'
PROGRESS_MARKUP = {'inline_keyboard':[[{'text':'Прогресс','web_app':{'url':PROGRESS_URL}},{'text':'Расходы','web_app':{'url':PROGRESS_URL + '?view=costs'}}]]}
REGION = 'us-central1'
RUNTIME_SERVICE_ACCOUNT = 'autopost-runtime@resromcom-493b4.iam.gserviceaccount.com'


RENDER_SETTINGS = 'final-v1'
EDIT_TASK = 'Apply only these new edits to your previous JSON answer; keep every other field exactly as it was. An edit that only asks a question or remarks without requesting a change gets {"answer":"..."} instead; an explicit request to cancel the draft or to move these messages to a new post gets {"action":"cancel"} or {"action":"new-post"}.'
SCREENSHOTS = 'The last %d attached images, oldest first, are screenshots sent with the edits: feedback on the previous preview, never post media. Only the newest %d are new since the last draft; the older ones were already applied.'


def runtime():
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    config = json.loads(AUTOPOST.value)
    store = Store(firestore.client())
    bucket = storage.bucket(config.get('bucket','resromcom-493b4-autopost'))
    return config, store, bucket


def enqueue(payload, identifier, delay=0):
    try:
        functions.task_queue('locations/us-central1/functions/worker').enqueue({'data':payload},functions.TaskOptions(task_id=identifier,schedule_delay_seconds=delay,dispatch_deadline_seconds=540))
    except admin_exceptions.AlreadyExistsError:
        pass


def track(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except Exception as failure:
        logging.error(json.dumps({'code':'session_write_failed','error':str(failure)[:200]}, ensure_ascii=False))


def item_name(item, store=None, item_id=None):
    messages = item['messages'] if item else []
    count = sum(1 for m in messages if imaged(m).get('photo'))
    saved = newest_specs([snapshot.to_dict() for snapshot in store.db.collection('specs').where('item','==',item_id).stream()]).get(item_id) if store and item_id and item else None
    return post_phrase(item,saved) if item and post_phrase(item,saved) else (' ' + clips(item) if any(imaged(m).get('video') for m in messages) else ' ' + str(count) + ' фото' if count else '')


def post_name(store, saved):
    item = store.get('items/' + saved['item']) if saved.get('item') else None
    return named(saved,item) or item_name(item)


def superseded(store, payload):
    item = store.get('items/' + payload['item'])
    return not item or item['revision'] != payload['revision'] or item.get('status') == 'rejected'


ACTIONS = {'draft':'typing','fix':'typing','montage':'upload_video','prepared':'upload_video','rendered':'upload_video','run':'upload_photo','apply-fix':'upload_photo','upload-complete':'upload_document','skills':'typing','skill':'typing','learn':None,'remove-keyboard':None}


def activity(config, kind, stop):
    if not kind:
        return
    def beat():
        while True:
            try:
                platforms.telegram('sendChatAction',{'chat_id':config['roman_chat_id'],'action':kind})
            except Exception:
                pass
            if stop.wait(4):
                return
    threading.Thread(target=contextvars.copy_context().run,args=(beat,),daemon=True).start()


def loader_for(config, record):
    return {'chat_id':config['roman_chat_id'],'action':'upload_video',**({'message_id':record['id'],'text':(record['text'] + '\n' + (record.get('line') or '')).strip()} if record else {})}


def edit_message(config, identifier, text, markup=None, entities=None):
    fields = {'chat_id':config['roman_chat_id'],'message_id':identifier,'text':text}
    if entities:
        fields['entities'] = entities
    if markup is not None:
        fields['reply_markup'] = markup
    try:
        platforms.telegram('editMessageText',fields)
    except Exception:
        pass


def finish_status(config, record, line):
    if record:
        edit_message(config,record['id'],(record['text'] + '\n' + line).strip())


def retire_taps(config, store, taps):
    try:
        platforms.configure(config,store,tempfile.gettempdir())
    except Exception:
        return
    for tap in taps:
        if tap['line'] is None:
            for attempt in range(2 if tap.get('item') else 1):
                try:
                    if tap.get('item'):
                        current = store.get('items/' + tap['item']) or {}
                        if current.get('status') != 'collecting' or current.get('batch_token') != tap['token']:
                            break
                        tap['markup'] = collect_keyboard(tap['item'],tap['token'],tap['offers'],current.get('chosen',[]))
                    platforms.telegram('editMessageReplyMarkup',{'chat_id':config['roman_chat_id'],'message_id':tap['message_id'],'reply_markup':tap['markup']})
                    break
                except Exception:
                    pass
        else:
            struck = tap['text'].strip() if tap.get('strike') else ''
            edit_message(config,tap['message_id'],(tap['text'] + '\n' + tap['line']).strip(),entities=[{'type':'strikethrough','offset':0,'length':len(struck.encode('utf-16-le')) // 2}] if struck else None)


WEBHOOK_EDIT_SECONDS = 8


def bounded(work, seconds):
    thread = threading.Thread(target=contextvars.copy_context().run,args=(work,),daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive()


def resync_keyboard(config, store, query):
    parts = (query.get('data') or '').split(':')
    message = query.get('message') or {}
    if len(parts) != 3 or parts[0] not in TOGGLES or not message.get('message_id'):
        return
    item = store.get('items/' + parts[1]) or {}
    if item.get('status') != 'collecting' or item.get('batch_token') != parts[2]:
        return
    offers = offered(config,store.get('tokens/business') or config.get('business'))
    retire_taps(config,store,[{'message_id':message['message_id'],'line':None,'item':parts[1],'token':parts[2],'offers':offers,'markup':None}])


ALERT_LIMIT = 200


def alert_parts(text):
    lines = text.replace('. ','.\n').split('\n')
    head = ''
    while lines and len((head + '\n' + lines[0]).strip()) <= ALERT_LIMIT:
        head = (head + '\n' + lines.pop(0)).strip()
    if not head:
        head, lines = text[:ALERT_LIMIT], [text[ALERT_LIMIT:]]
    return head, '\n'.join(lines)


def callback_reply(config, store, query, result, visible):
    toast = {'method':'answerCallbackQuery','callback_query_id':query['id']}
    if visible and result is not None or result is True:
        return https_fn.Response(json.dumps(toast),content_type='application/json')
    text = result if isinstance(result,str) else 'Кнопка устарела или уже обработана'
    parts = (query.get('data') or '').split(':')
    item = store.get('items/' + parts[1]) if len(parts) == 3 and parts[1] and '/' not in parts[1] else None
    name = titled(item) if item else ''
    if name and name not in text:
        text = 'Пост' + name + ':\n' + text
    head, rest = alert_parts(text)
    toast.update(text=head,show_alert=True)
    def deliver():
        for attempt in range(2):
            try:
                tell(config,rest)
                return
            except Exception:
                pass
    if rest:
        bounded(deliver,WEBHOOK_EDIT_SECONDS)
    return https_fn.Response(json.dumps(toast,ensure_ascii=False),content_type='application/json')


def collect_reply(config, store, item_id, item, text, burst=False):
    offers = offered(config,store.get('tokens/business') or config.get('business'))
    return {'item':item_id,'text':bursted(item,offers,item.get('chosen')) if burst else hinted(text,offers,item.get('chosen')),'reply_markup':collect_keyboard(item_id,item['batch_token'],offers,item.get('chosen',[]))}


def send_collect(config, store, result, claim=None):
    sent = platforms.telegram('sendMessage',{'chat_id':config['roman_chat_id'],'text':result['text'],'reply_markup':result['reply_markup']})
    identifier = sent.get('message_id') if isinstance(sent,dict) else None
    if claim is not None:
        with claim['lock']:
            late = claim['abandoned']
            claim['sent'] = True
        if late:
            if type(identifier) is int:
                try:
                    platforms.telegram('deleteMessage',{'chat_id':config['roman_chat_id'],'message_id':identifier})
                except Exception:
                    pass
            return
    if type(identifier) is not int:
        return
    def swap(value):
        previous = value.get('keyboard_message')
        value['keyboard_message'] = identifier
        value['keyboard_group'] = result.get('group')
        return previous
    try:
        previous = store.change('items/' + result['item'],swap)
        if previous:
            remove_keyboard(config,{'preview_message':previous})
        current = store.get('items/' + result['item']) or {}
        if current.get('status') == 'collecting' and current.get('keyboard_message') == identifier and current.get('revision') != result.get('revision'):
            fresh = collect_reply(config,store,result['item'],current,collected(current),time.time() - (current.get('added') or {}).get('at',0) <= BURST_SECONDS)
            edit_message(config,identifier,fresh['text'],fresh['reply_markup'])
    except Exception:
        logging.error(json.dumps({'code':'keyboard_retire_failed'}))


def pin_texts(store):
    recent = [(snapshot.id,snapshot.to_dict()) for snapshot in store.db.collection('items').where('created','>=',time.time() - progress_view.WAITING_AGE).stream()]
    if not recent:
        return {}
    key,item = max(recent,key=lambda row:row[1].get('created') or 0)
    specs = [snapshot.to_dict() for snapshot in store.db.collection('specs').where('state','in',[state for state,label in PIN_SPEC_STAGES]).stream()]
    states = newest_states(specs)
    newest = newest_specs(specs).get(key) or {}
    stage = pin_stage(item,states.get(key),bool(newest.get('answered'))) if item.get('status') not in ('rejected','failed') else None
    if stage and states.get(key) in ('previewed','needs-edit') and (item.get('updated') or item.get('created') or 0) < time.time() - progress_view.WAITING_AGE:
        stage = None
    return {key:'Пост' + post_phrase(item,newest) + ': ' + stage} if stage else {}


def sync_pin(config, store):
    try:
        wanted = pin_texts(store)
        chat = config['roman_chat_id']
        def claim(tx):
            record = store.get('sessions/pin',tx) or {}
            posts = dict(record.get('posts') or {})
            if record.get('item'):
                posts.setdefault(record['item'],{k:record[k] for k in ('text','at','message_id','pinned') if k in record})
            plan = {key:('retire',posts.pop(key)) for key in list(posts) if key not in wanted}
            for key,text in wanted.items():
                entry = posts.get(key)
                if entry and entry.get('message_id'):
                    plan[key] = ('keep' if entry.get('pinned') else 'pin',entry)
                    if entry.get('text') != text:
                        posts[key] = {**entry,'text':text}
                elif entry and time.time() - entry.get('at',0) < 60:
                    plan[key] = ('keep',entry)
                else:
                    posts[key] = {'text':text,'at':time.time()}
                    plan[key] = ('create',posts[key])
            if posts != record.get('posts') or 'item' in record:
                store.put('sessions/pin',{'posts':posts},tx)
            return plan
        plan = store.atomic(claim)
        for key,(action,entry) in plan.items():
            try:
                def update(**fields):
                    store.change('sessions/pin',lambda value:value['posts'][key].update(fields) if key in (value.get('posts') or {}) and value['posts'][key].get('at') == entry.get('at') else None)
                if action == 'retire':
                    for method in ('unpinChatMessage','deleteMessage'):
                        if entry.get('message_id'):
                            try:
                                platforms.telegram(method,{'chat_id':chat,'message_id':entry['message_id']})
                            except Error:
                                pass
                    continue
                text = wanted[key]
                if action == 'create':
                    sent = platforms.telegram('sendMessage',{'chat_id':chat,'text':text,'reply_markup':PROGRESS_MARKUP})
                    if not store.change('sessions/pin',lambda value:value['posts'][key].update(message_id=sent['message_id']) or True if key in (value.get('posts') or {}) and value['posts'][key].get('at') == entry.get('at') else None):
                        for method in ('unpinChatMessage','deleteMessage'):
                            try:
                                platforms.telegram(method,{'chat_id':chat,'message_id':sent['message_id']})
                            except Error:
                                pass
                        continue
                    entry = {**entry,'message_id':sent['message_id']}
                    action = 'pin'
                if action == 'pin':
                    try:
                        platforms.telegram('unpinAllChatMessages',{'chat_id':chat})
                    except Error:
                        pass
                    platforms.telegram('pinChatMessage',{'chat_id':chat,'message_id':entry['message_id'],'disable_notification':True})
                    update(pinned=True)
                if action in ('keep','pin') and entry.get('message_id') and entry.get('text') != text:
                    try:
                        platforms.telegram('editMessageText',{'chat_id':chat,'message_id':entry['message_id'],'text':text,'reply_markup':PROGRESS_MARKUP})
                    except Error as failure:
                        if not getattr(failure,'not_modified',False):
                            update(text=entry.get('text'))
                            raise
            except Exception as failure:
                if getattr(failure,'status_code',None) == 400 and any(x in getattr(failure,'description','').lower() for x in ('message to edit not found','message to pin not found')):
                    store.change('sessions/pin',lambda value:value['posts'][key].pop('message_id',None) and value['posts'][key].pop('pinned',None) if key in (value.get('posts') or {}) and value['posts'][key].get('at') == entry.get('at') else None)
                logging.error(json.dumps({'code':'pin_status_failed'}))
        if not wanted and any(action == 'retire' for action,entry in plan.values()):
            try:
                platforms.telegram('unpinAllChatMessages',{'chat_id':chat})
            except Error:
                pass
    except Exception:
        logging.error(json.dumps({'code':'pin_status_failed'}))


def tell(config, text, **extra):
    return platforms.telegram('sendMessage',{'chat_id':config['roman_chat_id'],'text':text,**extra})


def failure_text(failure, target, fallback):
    if str(failure) == 'API failure: 190':
        return 'Нужен новый токен ' + ('Threads' if target == 'threads' else 'Instagram' if target.startswith('ig-') else target) + '.'
    if isinstance(failure,Error) and getattr(failure,'description',''):
        return ('Telegram не принял публикацию: ' if target.startswith('tg-') else 'Платформа не приняла запрос: ') + failure.description
    return str(failure) if isinstance(failure,(Error,StateError)) else fallback


def tell_reliably(config, store, text, **extra):
    try:
        return tell(config,text,**extra)
    except Error as failure:
        if failure.status_code != 429:
            raise
    store.atomic(lambda tx:store.job(tx,{'step':'say','say':text,**({'markup':extra['reply_markup']} if extra.get('reply_markup') else {})},'say-' + secrets.token_hex(8)))
    store.flush(enqueue)


def authorized(update, config):
    if not isinstance(update, dict):
        return False
    if update.get('business_connection'):
        connection = update['business_connection']
        return isinstance(connection,dict) and isinstance(connection.get('user'),dict) and str(connection.get('user',{}).get('id')) == str(config['roman_user_id']) and str(connection.get('user_chat_id')) == str(config['roman_chat_id'])
    source = update.get('callback_query') or update.get('message',{})
    if not isinstance(source,dict):
        return False
    message = source.get('message',source)
    if not isinstance(message,dict):
        return False
    chat = message.get('chat',{})
    sender = source.get('from',{})
    if not isinstance(chat,dict) or not isinstance(sender,dict):
        return False
    return str(sender.get('id')) == str(config['roman_user_id']) and str(chat.get('id')) == str(config['roman_chat_id']) and chat.get('type') == 'private'


def upload_mac(config, item, revision, expires):
    return hmac.new(hmac.new(config['webhook_secret'].encode(), b'upload', hashlib.sha256).digest(),f'{item}:{revision}:{expires}'.encode(),hashlib.sha256).hexdigest()


def first_of_group(value, group):
    seen = value.setdefault('groups', {})
    if group in seen:
        return False
    seen[group] = time.time()
    for old in sorted(seen, key=seen.get)[:-10]:
        del seen[old]
    return True


def received(saved):
    if saved:
        return False
    saved['received'] = time.time()
    return True


@https_fn.on_request(region=REGION,service_account=RUNTIME_SERVICE_ACCOUNT,secrets=[AUTOPOST],timeout_sec=60,min_instances=0,max_instances=2,invoker='public')
def webhook(request):
    stop = threading.Event()
    try:
        return handle_update(request,stop)
    finally:
        stop.set()


def handle_update(request,stop):
    config, store, bucket = runtime()
    platforms.configure(config,store,tempfile.gettempdir())
    cors = {'Access-Control-Allow-Origin':'https://www.resrom.com','Access-Control-Allow-Headers':'Content-Type','Access-Control-Allow-Methods':'POST, OPTIONS','Vary':'Origin'}
    if request.method == 'OPTIONS':
        return https_fn.Response('',status=204,headers=cors)
    if request.method != 'POST':
        return https_fn.Response('',status=405)
    value = request.get_json(silent=True) or {}
    if not isinstance(value,dict):
        return https_fn.Response('',status=400,headers=cors)
    if value.get('action') == 'uploaded':
        if request.headers.get('Origin') != 'https://www.resrom.com':
            return https_fn.Response('',status=403,headers=cors)
        item, revision, expires = value.get('item',''),value.get('revision'),value.get('expires',0)
        if not isinstance(item,str) or not re.fullmatch(r'[a-z0-9-]{1,100}',item) or type(revision) is not int or type(expires) is not int or not time.time() < expires <= time.time()+901:
            return https_fn.Response('',status=403,headers=cors)
        if not hmac.compare_digest(str(value.get('token','')).encode(),upload_mac(config,item,revision,expires).encode()):
            return https_fn.Response('',status=403,headers=cors)
        def complete(saved):
            if not saved.get('waiting_upload'):
                return 'stale' if saved.get('revision') != revision else False
            return {'revision':saved['revision']}
        outcome = store.change('items/' + item, complete)
        if outcome == 'stale':
            return https_fn.Response('',status=409,headers=cors)
        if outcome:
            enqueue({'step':'upload-complete','item':item,'revision':outcome['revision']},'uploaded-' + item + '-' + str(outcome['revision']) + '-' + secrets.token_hex(8))
        return https_fn.Response('{}',headers=cors,content_type='application/json')
    if not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token','').encode(),config['webhook_secret'].encode()):
        return https_fn.Response('',status=403)
    if not authorized(value,config) or 'update_id' not in value:
        return https_fn.Response('{}',content_type='application/json')
    message = value.get('message')
    if isinstance(message,dict) and message.get('message_id'):
        kind = 'upload_photo' if message.get('photo') else 'upload_video' if message.get('video') else 'typing'
        def react():
            try:
                platforms.telegram('setMessageReaction',{'chat_id':config['roman_chat_id'],'message_id':message['message_id'],'reaction':[{'type':'emoji','emoji':'👀'}]})
            except Exception:
                pass
            activity(config,kind,stop)
        group = message.get('media_group_id')
        if not group or store.change('sessions/reacted',lambda saved:first_of_group(saved,group)):
            bounded(react,WEBHOOK_EDIT_SECONDS)
    if isinstance(message,dict) and not any(message.get(key) for key in ('photo','video','voice','audio','document','animation','video_note','media_group_id')):
        name = command(message)
        listed = str(message.get('reply_to_message',{}).get('message_id',''))
        if isinstance(message.get('text'),str) and (name == '/skills' or listed and store.get('skills-messages/' + listed)):
            parts = message['text'].split(None,1)
            enqueue({'step':'skills','text':(parts[1] if len(parts) > 1 else '') if name == '/skills' else message['text']},'skills-' + str(value['update_id']))
            return https_fn.Response('{}',content_type='application/json')
        if isinstance(message.get('text'),str) and name and name not in COMMANDS:
            reply = {'method':'sendMessage','chat_id':config['roman_chat_id'],'text':hint_for(name)} if store.change('updates/' + str(value['update_id']),received) else {}
            return https_fn.Response(json.dumps(reply,ensure_ascii=False),content_type='application/json')
    query = value.get('callback_query')
    if isinstance(query,dict) and str(query.get('data','')).startswith('skill:'):
        enqueue({'step':'skill','skill':query['data'][6:],'query':query['id']},'skill-' + str(query['id']))
        return https_fn.Response('{}',content_type='application/json')
    taps = []
    store.classifier = lambda text,context:drafter.classify(config,text,context,lambda usage:store.record_usage(None,'classify',usage))
    result = store.intake(value,config,taps)
    store.flush(enqueue)
    query = value.get('callback_query')
    bounded(lambda:retire_taps(config,store,taps) if taps or result is not None or not query else resync_keyboard(config,store,query),WEBHOOK_EDIT_SECONDS)
    edited = None if query else getattr(store,'edited',None)
    if edited:
        remount = store.get('items/' + edited)
        if remount and remount.get('montage') and remount.get('status') == 'queued':
            bounded(lambda:acknowledge_remount(config,store,{'item':edited},remount),WEBHOOK_EDIT_SECONDS)
    bounded(lambda:sync_pin(config,store),WEBHOOK_EDIT_SECONDS)
    if query:
        return callback_reply(config,store,query,result,bool(taps))
    refresh = result.get('refresh') if isinstance(result,dict) else None
    if refresh:
        bounded(lambda:edit_message(config,refresh['message_id'],refresh['text'],refresh['reply_markup']),WEBHOOK_EDIT_SECONDS)
        said = {'method':'sendMessage','chat_id':config['roman_chat_id'],'text':result['text']} if result.get('text') else {}
        return https_fn.Response(json.dumps(said,ensure_ascii=False),content_type='application/json')
    text = result.get('text') if isinstance(result,dict) else result
    if text and isinstance(result,dict) and result.get('item') and result.get('reply_markup'):
        claim = {'lock':threading.Lock(),'abandoned':False,'sent':False,'failed':False}
        def attempt():
            try:
                send_collect(config,store,result,claim)
            except Exception:
                claim['failed'] = True
        bounded(attempt,WEBHOOK_EDIT_SECONDS)
        with claim['lock']:
            delivered = claim['sent'] and not claim['failed']
            if not delivered:
                claim['abandoned'] = True
        if delivered:
            return https_fn.Response('{}',content_type='application/json')
    reply = {'method':'sendMessage','chat_id':config['roman_chat_id'],'text':text} if text else {}
    if text and isinstance(result,dict) and result.get('reply_markup'):
        reply['reply_markup'] = result['reply_markup']
    return https_fn.Response(json.dumps(reply,ensure_ascii=False),content_type='application/json')


@https_fn.on_request(region=REGION,service_account=RUNTIME_SERVICE_ACCOUNT,secrets=[AUTOPOST],timeout_sec=30,min_instances=0,max_instances=1,invoker='public')
def progress(request):
    config, store, _ = runtime()
    return progress_view.respond(request,config,store)


def asset_hash(spec, assets):
    return hashlib.sha256(json.dumps({'spec':spec,'assets':assets},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def newp_markup(store, item_id):
    item = store.get('items/' + item_id) or {}
    key = item.get('followup')
    followup = store.get('followups/' + key) if key else None
    if not followup or followup.get('moved') or followup.get('item') != item_id:
        return []
    return [{'text':'Это новый пост','callback_data':'newp:' + key + ':' + followup['token']}]


def buttons(slug, digest, retry=False, published=False, accept=False):
    actions = ([('retry','Повторить')] if retry else []) + [('pfix','Править'),('pdel','Удалить')] + ([('acc','Принять')] if accept else []) if published else [('retry','Повторить')] if retry else [('pub','Опубликовать'),('edit','Править'),('rej','Отклонить')]
    values = [{'text':label,'callback_data':verb + ':' + slug + ':' + digest[:8]} for verb,label in actions]
    if any(len(x['callback_data'].encode()) > 64 for x in values):
        raise Error('Callback exceeds 64 bytes')
    return {'inline_keyboard':[values]}


ROLES = {'tg-channel':('Пост','Telegram'),'threads':('Пост','Threads'),'ig-post':('Пост','Instagram'),'ig-reel':('Reels','Instagram'),'tg-story':('История','Telegram'),'ig-story':('История','Instagram')}


def destinations(targets):
    roles = {}
    for target in targets:
        roles.setdefault(ROLES[target][0],[]).append(ROLES[target][1])
    return ' · '.join(role + ': ' + ', '.join(networks) for role,networks in roles.items())


def pieces(head, body, tag=None, members=()):
    wrap = (lambda text: '<' + tag + '>' + text + '</' + tag + '>') if tag else (lambda text: text)
    chunks, chunk, size = [], '', 0
    for char in body:
        weight = platforms.units(html.escape(char,quote=False))
        if size + weight > 3000:
            chunks.append(chunk)
            chunk, size = '', 0
        chunk += char
        size += weight
    if chunk or not chunks:
        chunks.append(chunk)
    texts = [wrap(html.escape(text,quote=False)) if text else '' for text in chunks]
    texts[0] = '\n'.join(line for line in (head,texts[0]) if line)
    return [(text,list(members),not position) for position,text in enumerate(texts)]


def pages(parts):
    result, current, owners = [], '', []
    for piece, members, fresh in parts:
        joined = current + ('\n──────────\n' if fresh else '\n') + piece if current else piece
        if platforms.units(joined) > 4096:
            result.append((current,owners))
            joined, owners = piece, []
        current = joined
        owners += [t for t in members if t not in owners]
    return result + [(current,owners)]


class Withdrawn(Exception):
    pass


def preview(config, store, bucket, item_id, spec, assets, directory, basis=None, revision=None):
    track(store.step,item_id,'preview',{},revision)
    scoped = {}
    last = []
    quote = []
    def withdrawn():
        item = store.get('items/' + item_id) or {}
        return item.get('status') == 'rejected' or revision is not None and item.get('revision') != revision
    def send(method, fields, files=None, targets=None):
        if withdrawn():
            raise Withdrawn()
        if quote:
            fields = {**fields,'reply_parameters':{'message_id':quote.pop(),'allow_sending_without_reply':True}}
        reply = platforms.telegram(method,fields,files)
        for position, message in enumerate(reply if isinstance(reply,list) else [reply]):
            last[:] = [(message['message_id'],method)]
            store.put('published-messages/' + str(message['message_id']),{'slug':spec['slug']})
            shown_for = targets[position] if targets else []
            if shown_for:
                scoped[str(message['message_id'])] = shown_for
        return reply
    def say(text, targets=None, **extra):
        return send('sendMessage',{'chat_id':config['roman_chat_id'],'text':text,**extra},targets=[targets] if targets else None)
    digest = asset_hash(spec,assets if basis is None else basis)
    saved = {'item':item_id,'spec':spec,'assets':assets,'hash':digest,'state':'drafting','results':{},'errors':{},'attempts':{}}
    if basis is not None:
        saved['basis'] = basis
    started = []
    def begin(tx):
        previous = store.get('specs/' + spec['slug'],tx) or {}
        current = store.get('items/' + item_id,tx) or {}
        if current.get('status') == 'rejected' or previous.get('state') == 'rejected' and not previous.get('operation') or revision is not None and current.get('revision') != revision:
            return False
        if previous.get('state') not in (None,'drafting','needs-edit'):
            raise StateError('Existing preview or publication cannot be overwritten')
        started[:] = [current]
        store.supersede(tx,item_id,{spec['slug']})
        store.put('specs/' + spec['slug'],saved,tx)
        return True
    if not store.atomic(begin):
        return
    current = started[0]
    quote[:] = [m['message_id'] for m in current.get('messages',[]) if imaged(m).get('photo') or imaged(m).get('video')][:1]
    try:
        offer(config,store,bucket,item_id,spec,assets,directory,basis,revision,saved,digest,current,send,say,last,scoped)
    except Withdrawn:
        cancel_draft(config,store,item_id,spec['slug'],digest,last,revision)


def cancel_draft(config, store, item_id, slug, digest, last, revision=None):
    item = store.get('items/' + item_id) or {}
    if item.get('status') != 'rejected' and (revision is None or item.get('revision') == revision):
        return False
    def reject(value):
        if value.get('state') == 'drafting' and value.get('hash') == digest:
            value['state'] = 'rejected'
    store.change('specs/' + slug,reject)
    if last:
        identifier, method = last[0]
        try:
            platforms.telegram('editMessageText' if method == 'sendMessage' else 'editMessageCaption',{'chat_id':config['roman_chat_id'],'message_id':identifier,**({'text':'Черновик отменён.'} if method == 'sendMessage' else {'caption':'Черновик отменён.'}),'reply_markup':{'inline_keyboard':[]}})
        except Error:
            pass
    return True


def copy_blocks(targets, copy):
    blocks = {}
    for target in platforms.live(list(targets)):
        blocks.setdefault(copy[target],[]).append(target)
    return blocks


def show_texts(config, store, item, item_id, targets, copy):
    every = copy_blocks(targets,copy)
    sent = item.get('texts_sent') if isinstance(item.get('texts_sent'),list) else []
    blocks = {text:members for text,members in every.items() if text not in sent}
    if not blocks:
        return
    parts = pieces('<b>' + html.escape('Тексты' + item_name(item,store,item_id) + ':',quote=False) + '</b>','')
    for text,members in blocks.items():
        parts += pieces('<b>' + html.escape(destinations(members),quote=False) + '</b>',text,None,members)
    for text,members in pages(parts):
        tell(config,text,parse_mode='HTML')
    store.change('items/' + item_id,lambda value:value.update(texts_sent=sorted(set(sent) | set(every))))


PHRASES = {'tg-channel':'пост в Telegram-канале','threads':'пост Threads','ig-post':'пост Instagram','ig-reel':'Reels Instagram'}
STORIES = {'tg-story':'Telegram','ig-story':'Instagram'}


def preview_label(targets, title):
    parts = [PHRASES[t] + (' «' + title + '»' if t == 'tg-channel' and title else '') for t in targets if t in PHRASES]
    networks = sorted(STORIES[t] for t in targets if t in STORIES)
    if networks:
        parts.append('сторис ' + ', '.join(networks))
    return 'Это ' + (', '.join(parts[:-1]) + ' и ' if len(parts) > 1 else '') + parts[-1]


def offer(config, store, bucket, item_id, spec, assets, directory, basis, revision, saved, digest, current, send, say, last, scoped):
    platforms.telegram_identity()
    title = config.get('channel_title')
    if not title and 'tg-channel' in spec['targets'] and config.get('channels',{}).get('tg-channel'):
        try:
            title = platforms.telegram('getChat',{'chat_id':config['channels']['tg-channel']}).get('title')
        except Error:
            title = None
    groups = {}
    for target in platforms.live(spec['targets']):
        files = {}
        for name, manifest in assets.get(target,{}).items():
            files.setdefault(manifest.get('sha256') or json.dumps(manifest,sort_keys=True),(name,manifest))
        text = '' if target in STORIES else spec['copy'].get(target,'')
        entry = groups.setdefault((frozenset(files),text),[files,text,[]])
        entry[2].append(target)
    counter = 0
    shown_texts = set()
    chat = config['roman_chat_id']
    for files, text, targets in groups.values():
        shown = []
        for name, manifest in files.values():
            unique = 'preview-' + str(counter) + Path(name).suffix
            counter += 1
            media.materialize(bucket,{unique:manifest},directory)
            shown.append({'path':directory/unique,'video':name.endswith('.mp4'),'size':{key:manifest[key] for key in ('duration','width','height') if manifest.get(key)}})
        if not shown and not text:
            continue
        caption = {'caption':text} if shown and text and platforms.units(text) <= 1024 else {}
        first = None
        for start in range(0,len(shown),10):
            chunk = shown[start:start + 10]
            extra = caption if start == 0 else {}
            if len(chunk) == 1:
                method, field = ('sendVideo','video') if chunk[0]['video'] else ('sendPhoto','photo')
                reply = send(method,{'chat_id':chat,**chunk[0]['size'],**extra},{field:chunk[0]['path']},[targets])
                sent = [reply]
            else:
                attached = {'file' + str(index):value['path'] for index,value in enumerate(chunk)}
                group = [{'type':'video' if value['video'] else 'photo','media':'attach://file' + str(index),**value['size'],**(extra if index == 0 else {})} for index,value in enumerate(chunk)]
                sent = send('sendMediaGroup',{'chat_id':chat,'media':group},attached,[targets] * len(chunk))
            first = first or sent[0]['message_id']
        if text and not caption:
            written = say(text,targets)['message_id']
            first = first or written
        if text:
            shown_texts.add(text)
        say(preview_label(targets,title),targets,reply_parameters={'message_id':first,'allow_sending_without_reply':True})
    blocks = {text:members for text,members in copy_blocks([t for t in spec['targets'] if t != 'ig-story'],spec['copy']).items() if text not in shown_texts}
    note = ' (низкое качество; финальное видео соберу после «Опубликовать»)' if basis is not None else ''
    lines = current.get('skipped',[]) + (['Заметка: ' + spec['notes']] if spec.get('notes') else [])
    heading = '<b>' + html.escape('Черновик' + post_name(store,saved) + ': куда пойдёт',quote=False) + '</b>'
    parts = pieces(heading,'\n'.join(([note.strip()] if note else []) + lines),'i')
    for text,members in blocks.items():
        label = destinations(members) + (' · «' + title + '»' if 'tg-channel' in members and title else '')
        parts += pieces('<b>' + html.escape(label,quote=False) + '</b>',text,None,members)
    messages = pages(parts)
    for text,members in messages[:-1]:
        say(text,members,parse_mode='HTML')
    message = say(messages[-1][0],messages[-1][1],reply_markup=buttons(spec['slug'],digest),parse_mode='HTML')
    shown_status = []
    def commit(tx):
        value = store.get('specs/' + spec['slug'],tx)
        item = store.get('items/' + item_id,tx) or {}
        session = store.get('sessions/inbox',tx) or {}
        if not value or value.get('state') != 'drafting' or value.get('hash') != digest or revision is not None and item.get('revision') != revision:
            return False
        value.update(state='previewed',preview_message=message['message_id'],preview_targets=scoped)
        item.update(status='previewed',edits_applied=len(item.get('edits',[])),photos_applied=len(item.get('edit_photos',[])))
        shown_status.append(item.pop('status_message',None))
        if not session.get('current') or session['current'] == spec['slug']:
            session['current'] = spec['slug']
        store.put('specs/' + spec['slug'],value,tx)
        store.put('items/' + item_id,item,tx)
        store.put('sessions/inbox',session,tx)
        return True
    if not store.atomic(commit):
        if not cancel_draft(config,store,item_id,spec['slug'],digest,last,revision):
            try:
                remove_keyboard(config,{'preview_message':message['message_id']})
            except Error:
                pass
    else:
        track(store.step,item_id,'preview',{'state':'done'},revision)
        track(store.step,item_id,'wait',{},revision)
        finish_status(config,shown_status[0] if shown_status else None,'Черновик' + post_name(store,saved) + ' готов.')


def upload_prompt(config,store,bucket,payload):
    item = store.get('items/' + payload['item'])
    if not item or item['revision'] != payload['revision'] or not item.get('waiting_upload'):
        return
    expires = int(time.time()) + 900
    uploads = []
    for attachment in media.attachments(item):
        if attachment.get('file_size',0) > 20*1024*1024 and attachment['name'] not in item.get('uploads',{}):
            name = 'inbox/' + payload['item'] + '/' + attachment['name']
            uploads.append({'name':attachment['name'],'type':media.upload_type(attachment),'url':media.signed(bucket,name,config,'PUT',15,content_type=media.upload_type(attachment))})
    fragment = {'uploads':uploads,'complete':'https://us-central1-resromcom-493b4.cloudfunctions.net/webhook','receipt':{'action':'uploaded','item':payload['item'],'revision':item['revision'],'expires':expires,'token':upload_mac(config,payload['item'],item['revision'],expires)}}
    tell(config,'Файл больше 20 МБ' + (' для' + item_name(item) if item_name(item) else '') + ', загрузи сюда (ссылка 15 минут):\nhttps://www.resrom.com/u/#' + quote(json.dumps(fragment,separators=(',',':')),safe=''),reply_markup={'inline_keyboard':[[{'text':'Новая ссылка','callback_data':'ulnk:' + payload['item'] + ':' + str(item['revision'])}]]})


def upload_complete(config,store,bucket,payload,directory):
    item = store.get('items/' + payload['item'])
    if not item or item['revision'] != payload['revision'] or not item.get('waiting_upload'):
        return
    uploads = dict(item.get('uploads',{}))
    pending = []
    total = sum(value.get('size',0) for value in uploads.values()) + sum(a.get('file_size',0) for a in media.attachments(item) if a['name'] not in uploads and a.get('file_size',0) <= 20*1024*1024)
    if not all(bucket.blob('inbox/' + payload['item'] + '/' + a['name']).exists() for a in media.attachments(item) if a.get('file_size',0) > 20*1024*1024 and a['name'] not in uploads):
        tell(config,'Загружено не всё: нажми «Новая ссылка».')
        return
    for attachment in media.attachments(item):
        if attachment.get('file_size',0) <= 20*1024*1024 or attachment['name'] in uploads:
            continue
        blob = bucket.blob('inbox/' + payload['item'] + '/' + attachment['name'])
        from google.api_core.exceptions import NotFound
        try:
            blob.reload()
        except NotFound:
            raise platforms.ExpiredMaterial('Материал истёк или не загружен. Пришли файлы заново.') from None
        total += blob.size
        if total > 1024*1024*1024 or blob.size > 512*1024*1024 or blob.content_type != media.upload_type(attachment):
            raise Error('Файл должен быть MP4 или изображением <=512 МБ, пакет <=1 ГБ.')
        pending.append((attachment,blob))
    for attachment, blob in pending:
        generation = blob.generation
        digest = hashlib.sha256()
        with bucket.blob(blob.name,generation=generation).open('rb') as source:
            for chunk in iter(lambda:source.read(1024*1024),b''):
                digest.update(chunk)
        uploads[attachment['name']] = {'object':blob.name,'generation':str(generation),'size':blob.size,'sha256':digest.hexdigest(),'content_type':blob.content_type}
    def finish(tx):
        saved = store.get('items/' + payload['item'],tx)
        if saved['revision'] != payload['revision'] or not saved.get('waiting_upload'):
            return
        saved.update(uploads=uploads,waiting_upload=False,updated=time.time())
        store.put('items/' + payload['item'],saved,tx)
        if saved['status'] == 'queued':
            store.job(tx,{'item':payload['item'],'revision':saved['revision'],'step':'montage' if saved.get('montage') else 'draft'},'upload-ready-' + payload['item'] + '-' + str(saved['revision']))
    store.atomic(finish)
    store.flush(enqueue)
    text = 'Загрузка завершена' + (' для' + item_name(item) if item_name(item) else '') + '.' + (' Нажми «Готово», когда всё отправлено.' if item['status'] == 'collecting' else ' Готовлю черновик.')
    if item['status'] == 'collecting':
        send_collect(config,store,collect_reply(config,store,payload['item'],item,text))
    else:
        tell(config,text)


def hints_of(item):
    return media.speech_hints(*[m.get(k) or '' for m in item.get('messages',[]) for k in ('text','caption')],*[e for e in item.get('edits',[]) if isinstance(e,str)],*item.get('instructions',[]))


def side_voice(config,store,payload,item,name,spoken):
    if name in item.get('voice_routed',{}) or len(spoken.split()) > 15:
        return
    item.setdefault('voice_routed',{})[name] = True
    store.change('items/' + payload['item'],lambda value:value.setdefault('voice_routed',{}).update({name:True}))
    try:
        kind = (drafter.classify(config,spoken,{'collecting':False,'title':None,'status':item.get('status')},lambda usage:store.record_usage(payload['item'],'classify',usage)) or {}).get('kind')
    except Exception:
        return
    if kind == 'rule':
        enqueue({'step':'skills','text':spoken},'skills-voice-' + hashlib.sha256((payload['item'] + name).encode()).hexdigest())
    elif kind == 'question':
        saved = store.latest(('done','partial'))
        tell(config,table_of(config,store,store.get('specs/' + saved)) if saved else 'Опубликованных постов пока нет.')


def transcript(config,store,bucket,payload,item,attachment,key):
    value = item.get('transcripts',{}).get(key)
    if value is None:
        value = media.transcribe(bucket,store,attachment['path'],payload['item'] + '/' + attachment['name'],config,hints=hints_of(item))
        item.setdefault('transcripts',{})[key] = value
        store.change('items/' + payload['item'],lambda saved:saved.setdefault('transcripts',{}).update({key:value}))
    return value


BAR_WIDTH = 10
STAGE_GAP = 5
EDIT_GAP = 2
STORE_GAP = 1
PROJECTS_URL = 'https://raw.githubusercontent.com/romacv/romacv/main/data.json'
PREP = 'подготовка'
AGENT_STATUS = {'done':'готово','waiting':'ждёт','failed':'ошибка','cancelled':'отменён'}


def bar(fraction):
    filled = round(BAR_WIDTH * max(0,min(1,fraction)))
    return '▰' * filled + '▱' * (BAR_WIDTH - filled) + ' ' + str(round(100 * max(0,min(1,fraction)))) + '%'


class Stages:
    def __init__(self, config, record, name, write=None, alive=None):
        self.config, self.record, self.name, self.write, self.alive = config, record, name, write, alive
        self.begun = self.last = time.monotonic()
        self.key = self.shown = self.wrote = None
        self.closed = False
        self.places = ''
        self.state = ('',0,'')
        self.written = self.pushed = 0
        self.lock = threading.Lock()
        self.editing = threading.Lock()
        self.halt = threading.Event()

    def start(self):
        def tick():
            while not self.halt.wait(STAGE_GAP):
                self.push()
        threading.Thread(target=contextvars.copy_context().run,args=(tick,),daemon=True).start()

    def __call__(self, key, text, fraction, extra='', force=False, quiet=False):
        now = time.monotonic()
        with self.lock:
            self.state = (text,fraction,extra)
            changed = key != self.key
            if changed:
                logging.info(json.dumps({'code':'stage','name':self.name,'stage':key,'elapsed':round(now - self.begun,1),'previous':round(now - self.last,1)},ensure_ascii=False))
                self.key, self.last = key, now
            write = self.write is not None and not self.closed and text != self.wrote and (changed or force or now - self.written >= STORE_GAP)
            if write:
                self.wrote, self.written = text, now
        if write:
            self.write(text)
        if not quiet and (changed or force):
            self.push(EDIT_GAP)

    def push(self, gap=STAGE_GAP):
        if not self.record:
            return
        with self.editing:
            with self.lock:
                text,fraction,extra = self.state
                body = (self.record['text'] + '\n' + bar(fraction) + ' · ' + text + (('\n\n' + extra[:3000]) if extra else '')).strip()
                now = time.monotonic()
                if not text or body == self.shown or now - self.pushed < gap:
                    return
                self.pushed, self.shown = now, body
            if self.alive is None or self.alive():
                edit_message(self.config,self.record['id'],body)

    def finish(self, line):
        self.halt.set()
        if not self.closed:
            self.closed = True
            if self.write is not None:
                self.write(None)
        if self.record and line:
            with self.editing:
                if self.alive is None or self.alive():
                    finish_status(self.config,self.record,line)


def agents_line(live):
    with live.lock:
        rows = {name:dict(value) for name,value in live.agents.items()}
    parts = [agents.LABELS.get(name,name) + ' — ' + (AGENT_STATUS.get(value.get('status')) or value.get('action') or 'работаю') for name,value in rows.items() if name in agents.NAMES]
    done = sum(1 for name in agents.NAMES if rows.get(name,{}).get('status') == 'done')
    return ', '.join(parts), done


def watch_agents(stages, live, stop):
    def beat():
        while not stop.wait(1):
            try:
                line, done = agents_line(live)
                stages('agents','Пишу черновик: ' + line if line else 'Пишу черновик',0.4 + 0.4 * done / len(agents.NAMES),stages.places,quiet=True)
            except Exception as failure:
                logging.error(json.dumps({'code':'stage_watch_failed','error':str(failure)[:200]}))
    threading.Thread(target=contextvars.copy_context().run,args=(beat,),daemon=True).start()


def places_text(value):
    rows = []
    for place in (value.get('places') if isinstance(value,dict) else None) or []:
        if isinstance(place,dict) and place.get('name'):
            label = ', '.join(str(x) for x in (place['name'],place.get('city')) if x)
            rows.append('• ' + label + ' — ' + (place.get('maps_url') or 'https://www.google.com/maps/search/?api=1&query=' + quote(label)))
    return ('Места:\n' + '\n'.join(rows[:6])) if rows else ''


def draft_text(spec):
    blocks = copy_blocks(spec['targets'],spec['copy'])
    return '\n\n'.join(destinations(members) + ':\n' + text for text,members in blocks.items())


def draft(config,store,bucket,payload,directory):
    item = store.claim_item(payload['item'],payload['revision'])
    if not item:
        return
    key = 'conversations/' + payload['item']
    early = item.get('edits',[])[store.seen(key,payload['revision']):] if store.get('specs/' + item['slug']) else []
    if not item.get('montage'):
        acknowledge_remount(config,store,payload,item)
    step = 'edit' if early else 'draft'
    plain = item.get('prepared') or all(a['kind'] == 'photo' for a in media.attachments(item))
    record = item.get('status_message') if plain and not item.get('montage') else None
    def write(text):
        track(store.step,payload['item'],step,{'agents':{PREP:{'status':'running' if text else 'done','action':text}}},payload['revision'])
    stages = Stages(config,record,payload['item'],write if plain else None,lambda:not superseded(store,payload))
    stages.start()
    stages('open','Принял',0.0)
    try:
        with ThreadPoolExecutor(max_workers=1) as background:
            projects = background.submit(contextvars.copy_context().run,fetch_projects)
            return draft_body(config,store,bucket,payload,directory,item,stages,projects,key)
    finally:
        stages.finish(None)


def fetch_projects():
    try:
        return platforms.request('GET',PROJECTS_URL,timeout=10)
    except Exception as failure:
        logging.error(json.dumps({'code':'projects_fetch_failed','error':str(failure)[:200]}))
        return []


def draft_body(config,store,bucket,payload,directory,item,stages,projects,key):
    if item.get('montage') and item.get('status_message'):
        edit_message(config,item['status_message']['id'],(item['status_message']['text'] + '\n' + (item['status_message'].get('line') or '') + '\nСоставляю план монтажа · шаг 2 из 3').strip())
    stages('intake','Загружаю материал',0.02)
    word = 'фото' if all(a['kind'] == 'photo' for a in media.attachments(item)) else 'файлы'
    inputs = unique_clips(media.intake(bucket,payload['item'],item,config,directory,lambda done,total:stages('intake','Загружаю ' + word + ' ' + str(done) + '/' + str(total),0.02 + 0.28 * done / max(total,1),quiet=True)))
    if any(a['kind'] != 'photo' for a in inputs) and not item.get('prepared'):
        start_preparation(config,store,bucket,payload,item,inputs)
        return
    config = {**config,'prepared_audio':item.get('prepared',{}).get('audio',{})}
    if item.get('prepared'):
        store.change('items/' + payload['item'],lambda value:value.update(uploads={a['name']:a['manifest'] for a in inputs}))
    transcripts,images,files,clips = [],[],{},[]
    pictures = sum(1 for a in inputs if a['kind'] == 'photo')
    for attachment in inputs:
        if attachment['kind'] == 'photo':
            stages('clean','Обрабатываю фото ' + str(len(images) + 1) + '/' + str(pictures),0.3 + 0.1 * len(images) / max(pictures,1))
            destination = directory / ('clean-' + attachment['name'])
            media.clean_image(attachment['path'],destination)
            images.append(destination)
            files[destination.name] = destination
        elif attachment['kind'] == 'voice':
            heard = transcript(config,store,bucket,payload,item,attachment,attachment['name'])
            side_voice(config,store,payload,item,attachment['name'],heard['text'].strip())
            transcripts.append(heard)
        else:
            clips.append(attachment['path'].stem)
    stages('edits','Читаю правки',0.4)
    heard = edit_media(config,store,bucket,payload,item,directory)
    if heard is None:
        return
    edits,shots = heard
    stages('restrict','Проверяю назначения',0.42)
    allowed = restrict(config,store,payload,item,bool(files),bool(clips))
    source = {'slug':item['slug'],'messages':item['messages'],'edits':edits,'transcripts':transcripts,'available_images':list(files),**({'instructions':item['instructions']} if item.get('instructions') else {}),**({'clips':len(clips)} if clips else {}),**({'chosen_targets':allowed} if allowed else {})}
    source['public_projects'] = projects.result()
    if shots:
        source['edit_screenshots'] = SCREENSHOTS % (len(shots),min(len(shots),max(len(item.get('edit_photos',[])) - item.get('photos_applied',0),0)))
    owner = '\n'.join([m.get('text') or m.get('caption') or '' for m in item['messages']] + edits)
    def validate(value):
        if drafter.answered(value):
            return drafter.answer_errors(value,True) if follow is not None else ['Return a draft, not an answer']
        errors = drafter.validate(value, prepared=False, chosen=allowed, owner=owner, clips=bool(clips))
        if value.get('slug') != item['slug']:
            errors.append('Keep the supplied slug')
        if 'Unknown media fields' not in errors:
            try:
                if any(path.name not in files for path in platforms.media_files(value).values()):
                    errors.append('Use supplied clean media only')
            except Error:
                errors.append('Invalid media path')
        if 'Invalid media path' in errors:
            media_spec = value.get('media') if isinstance(value.get('media'),dict) else {}
            offered_paths = [x.get('path') if isinstance(x,dict) else x for x in media_spec.get('images',[]) if isinstance(media_spec.get('images'),list)] + [media_spec[k] for k in ('video','cover') if media_spec.get(k)]
            wrong = [str(x)[:80] for x in offered_paths if x not in files]
            detail = 'Invalid media path ' + json.dumps(wrong,ensure_ascii=False) + '; use only these available_images paths exactly: ' + json.dumps(list(files))
            errors = [detail if x == 'Invalid media path' else x for x in errors]
        return list(dict.fromkeys(errors))
    fresh = edits[store.seen(key,payload['revision']):] if store.get('specs/' + item['slug']) else []
    follow = {'task':EDIT_TASK,'edits':fresh,**({'chosen_targets':allowed} if allowed else {}),**({'edit_screenshots':SCREENSHOTS % (len(shots),min(len(shots),max(len(item.get('edit_photos',[])) - item.get('photos_applied',0),0)))} if shots else {})} if fresh else None
    track(store.step,payload['item'],'edit' if follow is not None else 'draft',{} if follow is not None else {'agents':{name:{'status':'waiting'} for name in agents.NAMES}},payload['revision'])
    if follow is None:
        live = agents.Live(store,payload['item'],'draft',payload['revision'])
        live.found = lambda name,value:name == 'places' and (setattr(stages,'places',places_text(value)),stages('places','Нашёл места',0.6,stages.places,True))
        live.start()
        stages('agents','Пишу черновик',0.45,force=True)
        halt = threading.Event()
        watch_agents(stages,live,halt)
        try:
            spec = agents.run_draft(config,store,payload['item'],payload['revision'],source,images + shots,clips,validate,lambda kind,usage:store.record_usage(payload['item'],kind,usage),live)
        except agents.Cancelled:
            return
        finally:
            halt.set()
            live.stop()
    else:
        stages('agents','Правлю черновик',0.45,force=True)
        spec = drafter.ask(config,source,images + shots,validate,thread=(store,key,payload['revision'],len(edits),('items/' + payload['item'],'revision',payload['revision'])),follow=follow,rec=lambda usage:store.record_usage(payload['item'],'edit',usage))
    if superseded(store,payload):
        return
    stages.finish(None)
    track(store.step,payload['item'],'edit' if follow is not None else 'draft',{'state':'done'},payload['revision'])
    if drafter.answered(spec):
        stages.finish('Ответил.')
        take_answer(config,store,payload,item,spec)
        return
    shown = (stages.places + '\n\n' if stages.places else '') + draft_text(spec)
    stages('texts','Готовлю файлы',0.9,shown,True)
    if fresh:
        markup = newp_markup(store,payload['item'])
        tell(config,feedback(store.get('specs/' + item['slug']) or {},item,'; '.join(fresh)),reply_markup={'inline_keyboard':([markup] if markup else []) + [cancel_button(payload['item'],payload['revision'])]})
    assets = {}
    composed = {}
    for number,target in enumerate(spec['targets'],1):
        stages('assets','Готовлю файлы ' + str(number) + '/' + str(len(spec['targets'])),0.9 + 0.1 * (number - 1) / max(len(spec['targets']),1),shown)
        target_assets = {}
        for index,image in enumerate([] if target == 'ig-reel' else spec.get('media',{}).get('images',[])):
            path = directory/image['path']
            overlay = spec.get('overlay',{}).get(target)
            if overlay and target in ('ig-story','tg-story') and index == 0:
                key = (json.dumps(overlay,sort_keys=True),index)
                if key not in composed:
                    cleaned = directory/(target + '-overlay-source-' + str(index) + '.jpg')
                    media.clean_image(path,cleaned)
                    composed[key] = directory/(target + '-overlay-' + str(index) + '.jpg')
                    media.compose_story(cleaned,composed[key],overlay)
                path = composed[key]
            elif target in ('ig-story','tg-story','ig-post','threads','tg-channel'):
                converted = directory/(target + '-' + str(index) + '.jpg')
                media.clean_image(path,converted,target in ('ig-story','tg-story'),target not in ('ig-story','tg-story'))
                path = converted
            target_assets['image' + str(index) + '.jpg'] = media.upload(bucket,path,'media/' + item['slug'] + '/' + str(payload['revision']) + '/' + target + '/' + str(index) + '.jpg')
        assets[target] = target_assets
    if clips:
        acknowledge_remount(config,store,payload,item)
        show_texts(config,store,item,payload['item'],spec['targets'],spec['copy'])
        start_clips(config,store,bucket,payload,item,spec,assets,clips)
        return
    preview(config,store,bucket,payload['item'],spec,assets,directory,revision=payload['revision'])


CLIP_FORMATS = {'ig-post':'square','threads':'square','tg-channel':'channel','ig-story':'story-ig','tg-story':'story-tg'}
CLIP_SECONDS = 60
CLIP_MINIMUM = 3
CAROUSEL_LIMIT = 20


def reel_timeline(clips,durations):
    left, timeline = 89.9, []
    for stem in clips:
        length = min(float(durations[stem]),left)
        if length <= 0:
            break
        timeline.append({'source':stem,'in':0.0,'out':round(length,3)})
        left -= length
    return timeline


def start_clips(config,store,bucket,payload,item,spec,assets,clips):
    photos = [(int(Path(image['path']).stem.removeprefix('clean-')),index) for index,image in enumerate(spec['media']['images'])]
    order = sorted([(mid,'image',index) for mid,index in photos] + [(int(stem),'clip',position) for position,stem in enumerate(sorted(clips,key=int))])
    spec['media'] = {**spec['media'],'clips':[{'path':'clip' + str(position) + '.mp4'} for position in range(len(clips))],'sequence':[{'kind':kind,'index':index} for mid,kind,index in order]}
    stems = sorted(clips,key=int)
    prepared = item['prepared']
    edl = None
    if 'ig-reel' in spec['targets']:
        timeline = reel_timeline(stems,prepared['durations'])
        if sum(c['out'] - c['in'] for c in timeline) < CLIP_MINIMUM:
            spec['targets'].remove('ig-reel')
            assets.pop('ig-reel',None)
            if not spec['targets']:
                raise Error('Черновик не готов: для Reels нужно минимум 3 секунды видео.')
            store.change('items/' + payload['item'],lambda value:value.setdefault('skipped',[]).append('Не пойдёт в Reels: клипов меньше 3 секунд.'))
        else:
            edl = {'version':1,'kind':spec['kind'],'fit':'crop','targets':{'ig-reel':{'clips':timeline,'subtitles':[]}},'cover':{'target':'ig-reel','time':0.0},'copy':{}}
    identifier = secrets.token_hex(16)
    project = firebase_admin.get_app().project_id
    formats = sorted({CLIP_FORMATS[t] for t in spec['targets'] if t in CLIP_FORMATS})
    job = {'id':identifier,'mode':'clips','item':payload['item'],'revision':payload['revision'],'sources':{stem:prepared['sources'][stem] for stem in stems},'durations':{stem:prepared['durations'][stem] for stem in stems},'formats':formats,'clips':stems,'spec':spec,'assets':assets,'bucket':bucket.name,'prefix':'media/' + item['slug'] + '/' + identifier,'project':project,'worker_url':config['worker_url'],'service_account':config['runtime_service_account'],'state':'queued','created':time.time(),'loader':loader_for(config,item.get('status_message')),**({'edl':edl} if edl else {})}
    store.put('renders/' + identifier,job)
    store.change('items/' + payload['item'],lambda value:value.update(render=identifier))
    track(store.step,payload['item'],'render',{},payload['revision'])
    start_job(project,identifier)


def unique_clips(inputs):
    seen = set()
    def repeated(a):
        keys = [(a['kind'],a['manifest']['sha256'])] + ([(a['kind'],a['file_unique_id'])] if a['kind'] == 'photo' and a.get('file_unique_id') else [])
        found = any(k in seen for k in keys)
        seen.update(keys)
        return found
    return [a for a in inputs if a['kind'] not in ('video','photo') or not repeated(a)]


def cancel_button(item_id,revision):
    return [{'text':'Отменить черновик','callback_data':'cdraft:' + item_id + ':' + str(revision)}]


def take_answer(config,store,payload,item,value):
    identifier = 'act-' + payload['item'] + '-' + str(payload['revision'])
    if 'answer' in value:
        answer_edit(config,store,payload,item,value['answer'])
    elif value['action'] == 'cancel':
        name = item_name(item,store,payload['item'])
        if store.atomic(lambda tx:store.cancel_post(tx,payload['item'],identifier)):
            tell(config,'Черновик' + name + ' отклонён.')
    elif value['action'] == 'skills':
        enqueue({'step':'skills','text':payload.get('text') or (item.get('edits') or [''])[-1]},'skills-' + identifier)
        answer_edit(config,store,payload,item,'Записываю в навыки, черновик не менял.')
    elif value['action'] == 'edit':
        if not store.atomic(lambda tx:store.edit_from_question(tx,payload['slug'],payload['text'],identifier)):
            tell(config,'Черновик уже менялся: напиши правку ещё раз.')
    else:
        held = store.get('items/' + payload['item']) or {}
        key = held.get('followup')
        followup = store.get('followups/' + key) if key else None
        if not followup or followup.get('moved') or not store.atomic(lambda tx:store.new_post_callback(tx,'updates/act-' + key,'callbacks/act-' + key,key,followup['token'])):
            tell(config,'Не нашёл сообщения для нового поста: пришли /post.')


QUESTION_ACTIONS = ('cancel','new-post','edit','skills')
QUESTION = 'Question mode: Roman asked about the current draft. Return {"answer":"Russian reply, at most 200 characters"}, or {"action":"cancel"} / {"action":"new-post"} only when he explicitly asks to cancel the draft or to make it a new post, or {"action":"skills"} when he orders to record or change a skill, or {"action":"edit"} when the question is really a request to change the draft ("Сделаешь короче?", "Можешь убрать хэштеги?"). No Markdown fence.'


def question(config,store,payload):
    saved = store.get('specs/' + payload['slug'])
    item = store.get('items/' + payload['item'])
    if not saved or not item or item.get('status') == 'rejected' or saved.get('state') not in ('previewed','needs-edit','drafting'):
        return
    if saved['state'] == 'drafting':
        tell(config,working(item,store.get('renders/' + item['render']) if item.get('render') else None,time.time()))
        return
    inner = saved['spec']
    asked = {'question':payload['text'],'state':saved['state'],'targets':inner['targets'],'copy':inner['copy'],'at':inner.get('at')}
    raw = drafter.converse(config,Path(__file__).with_name('draft-agent.md').read_text() + '\n' + QUESTION,json.dumps(asked,ensure_ascii=False),1000,lambda usage:store.record_usage(payload['item'],'edit',usage))
    parsed = None
    try:
        value = parsed = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',raw.strip()))
        if not drafter.answered(value) or drafter.answer_errors(value,QUESTION_ACTIONS):
            raise ValueError
    except ValueError:
        text = parsed.get('answer') if isinstance(parsed, dict) else None
        value = {'answer':' '.join((text if isinstance(text, str) else raw).split())[:200] or 'Не смог ответить.'}
    if 'action' in value:
        take_answer(config,store,{'item':payload['item'],'revision':item['revision'],'slug':payload['slug'],'text':payload['text']},item,value)
        return
    keyboard = buttons(payload['slug'],saved['hash']) if saved['state'] == 'previewed' and saved.get('hash') else {'inline_keyboard':[]}
    newp = newp_markup(store,payload['item'])
    if newp:
        keyboard['inline_keyboard'].append(newp)
    keyboard['inline_keyboard'].append(cancel_button(payload['item'],item['revision']))
    message = tell(config,'Черновик' + item_name(item,store,payload['item']) + ': ' + value['answer'],reply_markup=keyboard)
    store.put('published-messages/' + str(message['message_id']),{'slug':payload['slug']})


def answer_edit(config,store,payload,item,answer):
    slug = item['slug']
    saved = store.get('specs/' + slug) or {}
    if saved.get('state') != 'drafting':
        return
    digest = asset_hash(saved['spec'],saved['basis'] if 'basis' in saved else saved['assets'])
    keyboard = buttons(slug,digest)
    newp = newp_markup(store,payload['item'])
    if newp:
        keyboard['inline_keyboard'].append(newp)
    keyboard['inline_keyboard'].append(cancel_button(payload['item'],payload['revision']))
    name = item_name(item,store,payload['item'])
    message = tell(config,('Черновик' + name + ': ' if name else '') + answer,reply_markup=keyboard)
    shown = []
    def restore(tx):
        shown.clear()
        saved = store.get('specs/' + slug,tx)
        current = store.get('items/' + payload['item'],tx)
        if not saved or not current or saved.get('state') != 'drafting' or current.get('revision') != payload['revision'] or current.get('status') != 'drafting':
            return False
        saved.update(state='previewed',hash=digest,preview_message=message['message_id'],answered=True)
        if newp:
            key = current.get('followup')
            followup = store.get('followups/' + key,tx) if key else None
            if followup:
                followup['answered'] = True
                store.put('followups/' + key,followup,tx)
        current.update(status='previewed',edits_applied=len(current.get('edits',[])),photos_applied=len(current.get('edit_photos',[])))
        shown.append(current.pop('status_message',None))
        store.put('specs/' + slug,saved,tx)
        store.put('items/' + payload['item'],current,tx)
        store.put('published-messages/' + str(message['message_id']),{'slug':slug},tx)
        return True
    if not store.atomic(restore):
        remove_keyboard(config,{'preview_message':message['message_id']})
    elif shown:
        finish_status(config,shown[0],'Ответил ниже.')


def restrict(config,store,payload,item,photos,video):
    chosen = chosen_targets(item.get('chosen',[]),config,store.get('tokens/business') or config.get('business')) if item.get('chosen') else []
    reasons = unsupported(chosen,photos,video)
    store.change('items/' + payload['item'],lambda value:value.update(skipped=skipped_lines(reasons)))
    allowed = [t for t in chosen if t not in reasons]
    if chosen and not allowed:
        raise Error('Черновик не готов: ' + ' '.join(skipped_lines(reasons)))
    return allowed or None


def reeled(allowed):
    return list(dict.fromkeys('ig-reel' if t == 'ig-post' else t for t in allowed)) if allowed else allowed


def edit_media(config,store,bucket,payload,item,directory):
    shots = []
    every = item.get('edit_photos',[])[-20:]
    for attachment in media.intake(bucket,payload['item'],{'messages':every},config,directory) if every else []:
        destination = directory / ('edit-' + attachment['name'])
        media.clean_image(attachment['path'],destination)
        shots.append(destination)
    applied = item.get('edits_applied',0)
    def settle(notes):
        return [note.replace('Правка на приложенном скриншоте.','Правка со скриншотом, уже применена.').replace('[см. приложенный скриншот]','[скриншот уже применён]') if index < applied else note for index,note in enumerate(notes)]
    edits = list(item.get('edits',[]))
    voices = item.get('edit_voices',[])
    if not voices:
        return settle(edits),shots
    attachments = media.intake(bucket,payload['item'],{'messages':voices},config,directory)
    audio = item.get('edit_prepared',{}).get('audio',{})
    missing = [a for a in attachments if a['name'] not in audio]
    if missing:
        start_preparation(config,store,bucket,payload,item,missing,'edit')
        return None
    spoken = {}
    for attachment in attachments:
        name = attachment['name']
        text = item.get('edit_transcripts',{}).get(name)
        if text is None:
            result = media.transcribe(bucket,store,attachment['path'],payload['item'] + '/' + name,{**config,'prepared_audio':audio},hints=hints_of(item))
            text = result['text'].strip() or 'Голосовое сообщение без распознанной речи.'
            store.change('items/' + payload['item'],lambda saved:saved.setdefault('edit_transcripts',{}).update({name:text}))
        spoken[name] = text
    texts = {'[голосовое сообщение ' + str(voice['message_id']) + ']':spoken[str(voice['message_id']) + '.ogg'] for voice in voices}
    def fill(note):
        for placeholder,text in texts.items():
            note = note.replace(placeholder,text)
        return note
    def save(value):
        value['edits'] = [fill(note) for note in value.get('edits',[])]
        value['edit_voices'] = [voice for voice in value.get('edit_voices',[]) if voice['message_id'] not in {v['message_id'] for v in voices}]
        for name in spoken:
            value.get('edit_transcripts',{}).pop(name,None)
    store.change('items/' + payload['item'],save)
    return settle([fill(note) for note in edits]),shots


def start_job(project,identifier):
    from google.cloud import run_v2
    run_v2.JobsClient().run_job(request=run_v2.RunJobRequest(name=f'projects/{project}/locations/us-central1/jobs/autopost-render',overrides=run_v2.RunJobRequest.Overrides(container_overrides=[run_v2.RunJobRequest.Overrides.ContainerOverride(env=[{'name':'RENDER_ID','value':identifier}])],task_count=1)))


def acknowledge_remount(config,store,payload,item):
    if item.get('status_message'):
        return
    remount = len(item.get('edits',[])) > item.get('edits_applied',0) or len(item.get('edit_photos',[])) > item.get('photos_applied',0)
    first = not remount and item.get('montage') and any(imaged(x).get('video') for x in item['messages'])
    record = {'text':('Перемонтирую' if remount else 'Монтирую' if item.get('montage') else 'Готовлю превью') + (' ' + clips(item) + titled(item) if first else item_name(item,store,payload['item'])) + '…','line':'Принял правку · шаг 1 из 3' if remount else 'Принял · шаг 1 из 3' if item.get('montage') else 'Принял'}
    try:
        record['id'] = tell(config,(record['text'] + '\n' + record['line']))['message_id']
    except Exception:
        return
    item['status_message'] = record
    store.change('items/' + payload['item'],lambda value:value.update(status_message=record) if not value.get('status_message') and value.get('revision') == item.get('revision') else None)


def plan_montage(config,store,bucket,payload,directory):
    item = store.claim_item(payload['item'],payload['revision'])
    if not item:
        return
    acknowledge_remount(config,store,payload,item)
    inputs = unique_clips(media.intake(bucket,payload['item'],item,config,directory))
    if any(a['kind'] != 'photo' for a in inputs) and not item.get('prepared'):
        start_preparation(config,store,bucket,payload,item,inputs)
        return
    config = {**config,'prepared_audio':item.get('prepared',{}).get('audio',{})}
    if item.get('status_message'):
        edit_message(config,item['status_message']['id'],(item['status_message']['text'] + '\n' + (item['status_message'].get('line') or '') + '\nСоставляю план монтажа · шаг 2 из 3').strip())
    if item.get('prepared'):
        store.change('items/' + payload['item'],lambda value:value.update(uploads={a['name']:a['manifest'] for a in inputs}))
    sources,transcripts,notes,images,manifests = {},{},[],[],{}
    for attachment in inputs:
        if attachment['kind'] == 'voice':
            spoken = transcript(config,store,bucket,payload,item,attachment,attachment['name'])['text'].strip()
            if spoken:
                side_voice(config,store,payload,item,attachment['name'],spoken)
                notes.append(spoken)
        if attachment['kind'] != 'video':
            continue
        name = attachment['path'].stem
        sources[name] = item['prepared']['durations'][name]
        transcripts[name] = transcript(config,store,bucket,payload,item,attachment,name)
        manifests[name] = item['prepared']['sources'][name]
    for index, manifest in enumerate(item['prepared']['sheets']):
        path = directory / ('sheet-' + str(index) + '.jpg')
        media.download(bucket,manifest,path)
        images.append(path)
    heard = edit_media(config,store,bucket,payload,item,directory)
    if heard is None:
        return
    edits,shots = heard
    images += shots
    if not sources:
        raise Error('Нет клипов для монтажа.')
    if len(images) > 100:
        raise Error('Слишком много кадров. Раздели материал на несколько пакетов.')
    allowed = reeled(restrict(config,store,payload,item,False,True))
    key = 'conversations/' + payload['item'] + '-montage'
    fresh = edits[store.seen(key,payload['revision']):] if item.get('edl') else []
    follow = {'mode':'montage','task':EDIT_TASK,'edits':fresh,**({'chosen_targets':allowed} if allowed else {}),**({'edit_screenshots':SCREENSHOTS % (len(shots),min(len(shots),max(len(item.get('edit_photos',[])) - item.get('photos_applied',0),0)))} if shots else {})} if fresh else None
    spent = [0.0]
    def montage_rec(usage):
        spent[0] += tally(usage)['usd']
        store.record_usage(payload['item'],'montage',usage)
    def montage_agent(**line):
        return {'agents':{'montage':{'action':None,'finished':time.time(),'usd':round(spent[0],6),**line}},'usd':round(spent[0],6)}
    track(store.step,payload['item'],'montage',{'agents':{'montage':{'status':'running','action':'планирую монтаж','started':time.time()}}},payload['revision'])
    try:
        edl = drafter.ask(config,{'mode':'montage',**({'chosen_targets':allowed} if allowed else {}),'sources':sources,'transcripts':transcripts,'messages':item['messages'],**({'instructions':item['instructions']} if item.get('instructions') else {}),**({'notes':notes} if notes else {}),'edits':edits,'previous_edl':item.get('edl'),**({'edit_screenshots':SCREENSHOTS % (len(shots),min(len(shots),max(len(item.get('edit_photos',[])) - item.get('photos_applied',0),0)))} if shots else {})},images,lambda value:(drafter.answer_errors(value,True) if follow is not None else ['Return an EDL, not an answer']) if drafter.answered(value) else montage.validate(value,sources) + (['targets must stay within chosen_targets'] if allowed and isinstance(value,dict) and isinstance(value.get('targets'),dict) and set(value['targets']) - set(allowed) else []),True,(store,key,payload['revision'],len(edits),('items/' + payload['item'],'revision',payload['revision'])),follow,montage_rec)
    except Exception:
        track(store.step,payload['item'],'montage',{**montage_agent(status='failed',result='ошибка'),'state':'failed'},payload['revision'])
        raise
    track(store.step,payload['item'],'montage',{**montage_agent(status='done',result=str(len(sources)) + ' клипов'),'state':'done'},payload['revision'])
    if superseded(store,payload):
        return
    if drafter.answered(edl):
        take_answer(config,store,payload,item,edl)
        return
    show_texts(config,store,item,payload['item'],edl['targets'],edl['copy'])
    identifier = secrets.token_hex(16)
    project = firebase_admin.get_app().project_id
    job = {'id':identifier,'mode':'draft','item':payload['item'],'revision':payload['revision'],'edl':edl,'sources':manifests,'bucket':bucket.name,'prefix':'media/' + item['slug'] + '/' + identifier,'project':project,'worker_url':config['worker_url'],'service_account':config['runtime_service_account'],'state':'queued','created':time.time(),'loader':loader_for(config,item.get('status_message'))}
    store.put('renders/' + identifier,job)
    store.change('items/' + payload['item'],lambda value:value.update(edl=edl,render=identifier))
    track(store.step,payload['item'],'render',{},payload['revision'])
    start_job(project,identifier)
    name = item_name(item,store,payload['item'])
    if edits and item.get('edl'):
        markup = newp_markup(store,payload['item']) if fresh else []
        text = 'Перемонтирую' + name + ' с правкой: ' + (lambda note:note[:200] + '…' if len(note) > 200 else note)(' '.join('; '.join(fresh or edits[-1:]).split())) + '. Рендерю черновик в низком качестве. Превью пришлю сюда.'
        if markup or not item.get('status_message'):
            tell(config,text,reply_markup={'inline_keyboard':([markup] if markup else []) + [cancel_button(payload['item'],payload['revision'])]})
    else:
        tell(config,'Монтирую' + name + ': план готов, рендерю черновик в низком качестве. Превью пришлю сюда.')


def start_preparation(config,store,bucket,payload,item,inputs,target=None):
    identifier = secrets.token_hex(16)
    project = firebase_admin.get_app().project_id
    job = {'id':identifier,'mode':'prepare','item':payload['item'],'revision':payload['revision'],'sources':{a['name']:a['manifest'] for a in inputs if a['kind'] != 'photo'},'bucket':bucket.name,'prefix':'inbox/' + payload['item'] + '/prepared/' + identifier,**({'target':target} if target else {}),**({'montage':True} if item.get('montage') else {}),'project':project,'worker_url':config['worker_url'],'service_account':config['runtime_service_account'],'state':'queued','created':time.time(),'loader':loader_for(config,item.get('status_message'))}
    store.put('renders/' + identifier,job)
    store.change('items/' + payload['item'],lambda value:value.update(render=identifier))
    track(store.step,payload['item'],'prepare',{},payload['revision'])
    start_job(project,identifier)


def prepared(config,store,bucket,payload):
    job = store.get('renders/' + payload['render'])
    if job.get('state') == 'failed':
        store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))
    def finish(tx):
        item = store.get('items/' + payload['item'],tx)
        if not item or item.get('render') != payload['render'] or item['revision'] != payload['revision'] or item.get('status') != 'drafting':
            return
        if job.get('state') != 'done':
            raise Error('Подготовка медиа не завершена. Пришли материал заново.')
        if job.get('target') == 'edit':
            item.setdefault('edit_prepared',{}).setdefault('audio',{}).update(job['outputs']['audio'])
            item.update(status='queued',updated=time.time())
            item.pop('stop_why', None)
        else:
            item.update(prepared=job['outputs'],status='queued',updated=time.time())
            item.pop('stop_why', None)
        store.put('items/' + payload['item'],item,tx)
        store.job(tx,{'item':payload['item'],'revision':payload['revision'],'step':'montage' if item.get('montage') else 'draft'},'prepared-' + payload['render'])
    store.atomic(finish)
    track(store.step,payload['item'],'prepare',{'state':'done' if job.get('state') == 'done' else 'failed'},payload['revision'])
    store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))


def fail_final(config,store,slug,render,after=0):
    def fail(value):
        if value.get('final_render') != render or value.get('state') != 'rendering' or after and time.time() - value.get('started_at',0) < after:
            return False
        value['state'] = 'partial'
        value['published_at'] = time.time()
        value['retry_pending'] = 'final'
        return value
    saved = store.change('specs/' + slug,fail)
    if saved:
        announce_retry(config,store,slug,saved)
        sync_pin(config,store)
    return bool(saved)


def announce_retry(config,store,slug,saved):
    text = 'Финальное видео' + post_name(store,saved) + ' не собралось. Ничего не опубликовано.' if saved['retry_pending'] == 'final' else 'Публикация поста' + post_name(store,saved) + ' прервалась. Готовые цели сохранены; неопределённые исходы требуют сверки.\n' + table_of(config,store,saved)
    tell(config,text,reply_markup=buttons(slug,saved['hash'],True))
    store.talk(saved.get('item'),None,text)
    store.change('specs/' + slug,lambda value:value.pop('retry_pending',None))
    try:
        finish_status(config,saved.get('publish_message'),'Прервано.')
        store.change('specs/' + slug,lambda value:value.pop('publish_message',None))
    except Exception:
        logging.error(json.dumps({'code':'publish_message_not_closed'}))


def finish_final(config,store,payload,item,job):
    slug = item['slug']
    saved = store.get('specs/' + slug)
    if not saved or saved.get('final_render') != payload['render'] or saved.get('state') != 'rendering':
        store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))
        return
    if job.get('state') != 'done':
        fail_final(config,store,slug,payload['render'])
        track(store.step,payload['item'],'final',{'state':'failed'})
        store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))
        return
    assets = {target:{'video.mp4':manifest,'cover.jpg':job['outputs']['cover']} for target,manifest in job['outputs']['videos'].items()}
    def finish(tx):
        value = store.get('specs/' + slug,tx)
        if value.get('final_render') != payload['render'] or value.get('state') != 'rendering':
            return
        value.update(assets=assets,final=True,state='approved')
        store.put('specs/' + slug,value,tx)
        store.job(tx,{'step':'run','slug':slug},'final-' + payload['render'])
    store.atomic(finish)
    track(store.step,payload['item'],'final',{'state':'done'})
    store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))


def clips_rendered(config,store,bucket,payload,directory,job):
    spec,stems = job['spec'],job['clips']
    made = job['outputs'].get('clips',{})
    lines,assets = [],{}
    total = len(spec['media']['sequence'])
    for target in platforms.live(spec['targets']):
        if target == 'ig-reel':
            assets[target] = {'video.mp4':job['outputs']['videos'][target],'cover.jpg':job['outputs']['cover']}
            continue
        mine = dict(job['assets'][target])
        fmt = CLIP_FORMATS[target]
        if target in ('ig-post','threads') and total > CAROUSEL_LIMIT:
            lines.append('Не пойдёт в ' + SKIP_NAMES[target] + ': больше ' + str(CAROUSEL_LIMIT) + ' файлов в карусели.')
            spec['targets'].remove(target)
            spec['copy'].pop(target,None)
            continue
        for position,stem in enumerate(stems):
            info = made[fmt][stem]
            if target.startswith('ig-') and info['seconds'] < CLIP_MINIMUM:
                lines.append('Клип ' + str(position + 1) + ' не пойдёт в ' + SKIP_NAMES[target] + ': короче ' + str(CLIP_MINIMUM) + ' секунд.')
                continue
            if info['trimmed']:
                note = 'Клип ' + str(position + 1) + ' длиннее ' + str(CLIP_SECONDS) + ' секунд: обрезан до ' + str(CLIP_SECONDS) + '.'
                if note not in lines:
                    lines.append(note)
            mine['clip' + str(position) + '.mp4'] = {**{key:info[key] for key in ('object','generation','sha256','size','content_type')},'duration':max(1,round(info['seconds'])),**{key:info[key] for key in ('width','height') if info.get(key)}}
        photos = sum(name.startswith('image') for name in mine)
        moving = sum(name.startswith('clip') for name in mine)
        if not photos + moving or not photos and moving == 1 and target in ('ig-post','threads'):
            lines.append('Не пойдёт в ' + SKIP_NAMES[target] + ': для публикации не осталось файлов.')
            spec['targets'].remove(target)
            spec['copy'].pop(target,None)
            continue
        assets[target] = mine
    if not spec['targets']:
        raise Error('Черновик не готов: ' + ' '.join(lines))
    if lines:
        store.change('items/' + payload['item'],lambda value:value.update(skipped=value.get('skipped',[]) + [line for line in lines if line not in value.get('skipped',[])]))
    preview(config,store,bucket,payload['item'],spec,assets,directory,revision=payload['revision'])
    store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))


def rendered(config,store,bucket,payload,directory):
    item = store.get('items/' + payload['item'])
    job = store.get('renders/' + payload['render'])
    if job.get('mode') == 'final':
        if item:
            try:
                finish_final(config,store,payload,item,job)
            except Exception:
                if not fail_final(config,store,item['slug'],payload['render']):
                    raise
                logging.error(json.dumps({'code':'final_failed'}))
        else:
            store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))
        return
    if job.get('state') == 'failed':
        store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))
    if not item or item.get('render') != payload['render'] or item['revision'] != payload['revision'] or item.get('status') != 'drafting':
        store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))
        return
    if job.get('state') != 'done':
        raise Error('Рендер не завершён. Материал сохранён.')
    track(store.step,payload['item'],'render',{'state':'done'},payload['revision'])
    if job.get('mode') == 'clips':
        clips_rendered(config,store,bucket,payload,directory,job)
        return
    edl = job['edl']
    if 'ig-post' in item.get('chosen',[]) and 'ig-reel' not in item['chosen'] and 'ig-reel' in edl['targets']:
        store.change('items/' + payload['item'],lambda value:value.update(skipped=value.get('skipped',[]) + ['Instagram-пост выйдет как Reels в ленте.']))
    spec = {'slug':item['slug'],'kind':edl['kind'],'at':datetime.now(ZoneInfo(config.get('timezone','Asia/Makassar'))).isoformat(),'targets':list(edl['targets']),'media':{'video':'video.mp4','cover':'cover.jpg'},'copy':edl['copy'],**({'threads_tag':edl['threads_tag']} if 'threads_tag' in edl else {}),'approval':{}}
    assets = {target:{'video.mp4':manifest,'cover.jpg':job['outputs']['cover']} for target,manifest in job['outputs']['videos'].items()}
    preview(config,store,bucket,payload['item'],spec,assets,directory,{'edl':edl,'render':RENDER_SETTINGS} if job.get('mode') == 'draft' else None,payload['revision'])
    store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))


def remove_keyboard(config,saved,line=None):
    if saved.get('preview_message'):
        try:
            if line:
                platforms.telegram('editMessageText',{'chat_id':config['roman_chat_id'],'message_id':saved['preview_message'],'text':line})
            else:
                platforms.telegram('editMessageReplyMarkup',{'chat_id':config['roman_chat_id'],'message_id':saved['preview_message'],'reply_markup':{'inline_keyboard':[]}})
        except Error as failure:
            if not getattr(failure, 'not_modified', False):
                raise


def start_final(config,store,bucket,saved):
    slug = saved['spec']['slug']
    item = store.get('items/' + saved['item'])
    draft = store.get('renders/' + item['render'])
    identifier = secrets.token_hex(16)
    job = {**{key:draft[key] for key in ('item','revision','sources','bucket','project','worker_url','service_account')},'id':identifier,'mode':'final','edl':saved['basis']['edl'],'prefix':'media/' + slug + '/' + identifier,'state':'queued','created':time.time(),'loader':loader_for(config,saved.get('status_message'))}
    store.put('renders/' + identifier,job)
    store.change('specs/' + slug,lambda value:value.update(state='rendering',final_render=identifier,started_at=time.time()))
    track(store.step,saved['item'],'final',{})
    start_job(job['project'],identifier)
    try:
        tell(config,'Собираю' + post_name(store,saved) + ' в полном качестве, потом опубликую.')
    except Error:
        pass


def target_media(spec,names):
    if 'video.mp4' in names:
        return {'video':'video.mp4','cover':'cover.jpg'},{'video':'video.mp4','cover':'cover.jpg'}
    source = spec.get('media',{})
    images = sorted((n for n in names if n.startswith('image')),key=lambda n:int(n[5:-4]))
    clips = sorted((n for n in names if n.startswith('clip')),key=lambda n:int(n[4:-4]))
    result = {'images':[{'path':n,'alt':source['images'][int(n[5:-4])]['alt']} for n in images]}
    keys = {f'image{i}':n for i,n in enumerate(images)}
    if source.get('sequence'):
        if clips:
            result['clips'] = [{'path':n} for n in clips]
            keys.update({f'clip{i}':n for i,n in enumerate(clips)})
        position = {'image':{int(n[5:-4]):i for i,n in enumerate(images)},'clip':{int(n[4:-4]):i for i,n in enumerate(clips)}}
        result['sequence'] = [{'kind':e['kind'],'index':position[e['kind']][e['index']]} for e in source['sequence'] if e['index'] in position[e['kind']]]
    return result,keys


def publish_order(targets):
    return sorted(targets,key=lambda target:target == 'tg-story' and 'tg-channel' in targets)


RETRY_TEXT = 'Прервано по таймауту, повторю.'


def pending_targets(saved):
    done = {**saved.get('results',{}),**saved.get('published',{})}
    return [t for t in publish_order(platforms.live(saved.get('spec',{}).get('targets',[]))) if t not in done and saved.get('attempts',{}).get(t,0) < 3]


def missing_errors(saved,timeout=False):
    errors = saved.setdefault('errors',{})
    for target in platforms.live(saved.get('spec',{}).get('targets',[])):
        attempted = saved.get('attempts',{}).get(target,0)
        current = errors.get(target)
        if target in saved.get('results',{}) or (current and current != RETRY_TEXT and (not timeout or attempted >= 3)):
            continue
        if timeout:
            errors[target] = ('Прервано по таймауту, повторю.' if attempted < 3 else 'Прервано по таймауту.') if attempted else 'Не начато.'
        else:
            errors[target] = 'Прервано по таймауту.' if current == RETRY_TEXT else 'Операция остановлена.'


def table_of(config,store,saved):
    return table_text(saved,store.get('items/' + saved['item']) if saved.get('item') else None,config.get('timezone','Asia/Makassar'))


def target_lines(saved):
    lines = []
    for index,target in enumerate(platforms.live(saved.get('spec',{}).get('targets',[])),1):
        result = saved.get('results',{}).get(target)
        text = result['url'] if result and result['verified'] else 'опубликовано, ссылка ещё не проверена' if result else saved.get('errors',{}).get(target,'Ошибка')
        lines.append(f'{index}. {target}: {text}')
    return lines


def open_publish_status(config,store,slug):
    saved = store.get('specs/' + slug) or {}
    if saved.get('publish_message'):
        return saved['publish_message']
    record = {'text':'Публикую' + post_name(store,saved) + '…'}
    record['id'] = tell(config,record['text'])['message_id']
    kept = []
    def keep(value):
        if value.get('publish_message'):
            kept.append(value['publish_message'])
        else:
            value['publish_message'] = record
    try:
        store.change('specs/' + slug,keep)
    except Exception:
        logging.error(json.dumps({'code':'publish_message_not_saved'}))
        return record
    return kept[0] if kept else record


def publish_lines(saved,running=None):
    rows = []
    for target in publish_order(platforms.live(saved['spec']['targets'])):
        result = saved.get('results',{}).get(target)
        failure = saved.get('errors',{}).get(target)
        exhausted = saved.get('attempts',{}).get(target,0) >= 3
        status = 'готово ' + (result['url'] or 'ссылка не получена') if result else 'публикую' if target == running else ('ошибка: ' + failure if exhausted else 'ждёт, повторю: ' + failure) if failure else 'ждёт'
        rows.append(destinations([target]) + ' — ' + status)
    return '\n'.join(rows)


def run(config,store,bucket,slug,directory,resume=False):
    saved = store.claim_run(slug,resume)
    if not saved:
        return
    if saved.get('deleted'):
        store.change('specs/' + slug,lambda value:value.update(state='rejected'))
        tell(config,'Публикация поста' + post_name(store,saved) + ' уже удалена. Повторная публикация запрещена.')
        return
    if not saved.get('keyboard_removed'):
        remove_keyboard(config,saved)
        store.change('specs/' + slug,lambda value:value.update(keyboard_removed=True))
    spec,assets = saved['spec'],saved['assets']
    if asset_hash(spec,saved.get('basis',assets)) != saved['approved_hash']:
        raise StateError('Approval hash mismatch')
    if saved.get('basis') and not saved.get('final'):
        start_final(config,store,bucket,saved)
        return
    due = platforms.when(spec)
    if due > datetime.now(timezone.utc):
        store.change('specs/' + slug,lambda value:value.update(state='approved'))
        enqueue({'step':'run','slug':slug},'due-' + slug + '-' + secrets.token_hex(8),max(1,int((due-datetime.now(timezone.utc)).total_seconds())))
        return
    published_targets = {**saved['results'],**saved.get('published',{})}
    track(store.publish_progress,slug,None,{'targets':pending_targets(saved)})
    live_targets = platforms.live(spec['targets'])
    try:
        record = open_publish_status(config,store,slug)
    except Exception:
        record = saved.get('publish_message')
    progress = Stages(config,record,slug,None,lambda:(store.get('specs/' + slug) or {}).get('state') == 'running')
    progress.start()
    try:
        for target in pending_targets(saved)[:1]:
            attempts = saved.setdefault('attempts',{})
            progress('target-' + target,'Публикую: ' + destinations([target]),len(saved['results']) / max(len(live_targets),1),publish_lines(saved,target),True)
            attempts[target] = attempts.get(target,0) + 1
            store.change('specs/' + slug,lambda value:value.update(attempts=attempts))
            track(store.publish_progress,slug,target,{'status':'running','attempt':attempts[target],'run':True})
            state = {'steps':{}}
            target_assets = assets.get(target,{})
            try:
                for manifest in target_assets.values():
                    if not bucket.blob(manifest['object'],generation=int(manifest['generation'])).exists():
                        raise platforms.ExpiredMaterial(EXPIRED_TEXT)
                media.materialize(bucket,target_assets,directory)
                target_spec = json.loads(json.dumps(spec))
                target_spec['media'],keys = target_media(spec,target_assets)
                state = {'steps':{}}
                for checkpoint in store.db.collection('specs/' + slug + '/checkpoints').stream():
                    step = checkpoint.to_dict()
                    state['steps'][step['name']] = {'status':step['state'],'value':step.get('value')}
                if target.startswith('tg-'):
                    result = platforms.post_telegram(target_spec,state,target)
                    verified = platforms.link_opens(result)
                else:
                    provider = 'instagram' if target.startswith('ig-') else 'threads'
                    session = platforms.social_session(provider)
                    urls = {key:media.signed(bucket,target_assets[name]['object'],config,generation=target_assets[name]['generation']) for key,name in keys.items()}
                    for key,url in urls.items():
                        manifest = target_assets[keys[key]]
                        platforms.probe(url,directory/keys[key],manifest.get('content_type') or mimetypes.guess_type(manifest['object'])[0])
                    result = platforms.post_social(target_spec,state,target,urls,session)
                    verified = platforms.link_opens(result)
                publication = state['steps'].get(target + ':publish',{}).get('value')
                published = {'url':result,'verified':verified,'at':time.time(),'copy':spec.get('copy',{}).get(target)}
                published.update(published_ids(config,spec,target,target_assets,publication))
                saved['results'][target] = published
                published_targets[target] = published
                saved['errors'].pop(target,None)
            except platforms.ExpiredMaterial as failure:
                saved['errors'][target] = str(failure)
                attempts[target] = 3
            except Exception as failure:
                saved['errors'][target] = failure_text(failure,target,'Target failed; reconcile saved checkpoints')
                if saved['errors'][target].startswith(TOKEN_PREFIX):
                    attempts[target] = 3
            publication = state['steps'].get(target + ':publish',{})
            if target not in published_targets and publication.get('status') == 'done':
                published_targets[target] = {'url':'','verified':False,'at':time.time(),'copy':spec.get('copy',{}).get(target),**published_ids(config,spec,target,target_assets,publication.get('value'))}
            store.change('specs/' + slug,lambda value:value.update(results=saved['results'],errors=saved['errors'],published=published_targets,attempts=attempts))
            store.change('items/' + saved['item'],lambda value:value.update(published=published_targets))
            sent = target in saved['results']
            track(store.publish_progress,slug,target,{'status':'done' if sent else 'failed' if attempts[target] >= 3 else 'planned','run_end':'done' if sent else 'failed','url':saved['results'][target]['url'] if sent else None,'error':None if sent else saved['errors'].get(target)})
            progress('after-' + target,'Опубликовано ' + str(len(saved['results'])) + ' из ' + str(len(live_targets)),len(saved['results']) / max(len(live_targets),1),publish_lines(saved),True)
    finally:
        progress.finish(None)
    saved['published'] = published_targets
    remaining = pending_targets(saved)
    if remaining:
        tries = saved['attempts'].get(remaining[0],0)
        store.change('specs/' + slug,lambda value:value.update(started_at=time.time()))
        enqueue({'step':'run','slug':slug,'resume':True},'run-' + slug + '-' + remaining[0] + '-' + str(tries + 1) + '-' + secrets.token_hex(4),60 * tries)
        return
    state = 'done' if all(t in saved['results'] for t in platforms.live(spec['targets'])) else 'partial'
    if state == 'partial':
        missing_errors(saved)
        store.change('specs/' + slug,lambda value:value.update(errors=saved['errors']))
    store.change('specs/' + slug,lambda value:value.update(state=state,published_at=time.time()))
    saved['state'] = state
    try:
        voice.published(store,spec,saved.get('results',{}))
    except Exception:
        logging.error(json.dumps({'code':'voice_append_failed'}))
    any_published = bool(saved['results'] or saved.get('published'))
    track(store.step,saved['item'],'publish',{'state':'done' if any_published else 'failed'})
    if any_published:
        learning.schedule(store,saved['item'],slug,'published')
        track(store.step,saved['item'],'learn',{})
        store.forget(saved['item'],fix=False)
        enqueue({'step':'verify','slug':slug,'attempt':1},'verify-' + slug + '-1-' + secrets.token_hex(4))
    lines = [table_of(config,store,saved)]
    if any(target.startswith('ig-') for target in platforms.live(spec['targets'])):
        lines.append('Facebook: посты бота туда сами не попадают, поделись вручную из Instagram.')
    can_retry = retryable(saved)
    if state == 'partial' and not can_retry and any(saved.get('errors',{}).get(t) == EXPIRED_TEXT for t in spec['targets']):
        lines.append(EXPIRED_TEXT)
    lost = [ROLES[t][0] + ': ' + ROLES[t][1] + ' — ' + saved['errors'][t] for t in platforms.live(spec['targets']) if t not in saved['results'] and saved.get('errors',{}).get(t) and saved['errors'][t] != EXPIRED_TEXT and not saved['errors'][t].startswith(('Прервано','Операция остановлена','Не начато'))]
    if lost:
        lines.append('Не опубликовано:\n' + '\n'.join(lost) + ('\nПовторить попробует снова.' if can_retry else ''))
    message = tell(config,'\n'.join(lines),reply_markup=buttons(slug,saved['hash'],state == 'partial' and can_retry,True,bool(saved['results'])))
    store.put('published-messages/' + str(message['message_id']),{'slug':slug})
    store.talk(saved['item'],None,'\n'.join(lines))
    finish_status(config,saved.get('status_message'),'Опубликовано.' if state == 'done' else 'Опубликовано не везде.' if any_published else 'Ничего не опубликовано.')
    finish_status(config,record,'Опубликовано.' if state == 'done' else 'Опубликовано не везде.' if any_published else 'Ничего не опубликовано.')
    store.change('specs/' + slug,lambda value:(value.pop('status_message',None),value.pop('publish_message',None)))


VERIFY_ATTEMPTS = 3
VERIFY_DELAY = 60


def verify(config,store,slug,attempt):
    saved = store.get('specs/' + slug)
    if not saved:
        return
    item = saved['item']
    if not saved.get('results'):
        if saved.get('published'):
            track(store.step,item,'verify',{'state':'done'})
            track(store.close_session,item,'published','опубликовано' if saved.get('state') == 'done' else 'опубликовано не везде')
        return
    rows,passed,checked = {},{},time.time()
    track(store.step,item,'verify',{'targets':{target:{'status':'checking','attempts':attempt} for target in saved['results']}})
    for target,result in saved['results'].items():
        try:
            found = platforms.confirm(target,saved.get('published',{}).get(target,result),result['url'])
        except Exception as failure:
            found = {'api':'error: ' + type(failure).__name__,'link':None,'why':'error: ' + type(failure).__name__}
        if target == 'tg-story':
            status = 'unverifiable'
        elif found['api'] in ('ok','n/a') and (found['link'] is not False or target in ('ig-post','ig-reel')):
            status = 'verified'
        else:
            status = 'checking' if attempt < VERIFY_ATTEMPTS else 'unverified'
        rows[target] = {'status':status,'api':found['api'],'link':found['link'],'attempts':attempt,'checked':checked,'why':found['why']}
        if status != 'unverifiable':
            passed[target] = status == 'verified'
    def record(value):
        for target,ok in passed.items():
            for group in ('results','published'):
                if target in value.get(group,{}):
                    value[group][target]['verified'] = ok
    store.change('specs/' + slug,record)
    def record_item(value):
        for target,ok in passed.items():
            if target in value.get('published',{}):
                value['published'][target]['verified'] = ok
    store.change('items/' + item,record_item)
    last = attempt >= VERIFY_ATTEMPTS or all(row['status'] != 'checking' for row in rows.values())
    track(store.step,item,'verify',{'targets':rows,**({'state':'done'} if last else {})})
    if last:
        track(store.close_session,item,'published','опубликовано' if saved.get('state') == 'done' else 'опубликовано не везде')
    else:
        enqueue({'step':'verify','slug':slug,'attempt':attempt + 1},'verify-' + slug + '-' + str(attempt + 1) + '-' + secrets.token_hex(4),VERIFY_DELAY * attempt)


def published_ids(config,spec,target,assets,publication):
    if publication is None:
        return {}
    if target == 'tg-channel':
        messages = publication if isinstance(publication,list) else [publication]
        return {'chat_id':messages[0].get('chat',{}).get('id') or config['channels'][target],'message_ids':[m['message_id'] for m in messages],'caption':bool(spec.get('media',{}).get('images') or spec.get('media',{}).get('clips') or spec.get('media',{}).get('video'))}
    if target == 'tg-story' and isinstance(publication,list):
        return {'id':publication[0]['id'],'ids':[story['id'] for story in publication],'business_connection_id':platforms.key('telegram-business.json')['business_connection_id'],'assets':assets}
    if target == 'tg-story':
        return {'id':publication['id'],'business_connection_id':platforms.key('telegram-business.json')['business_connection_id'],'assets':assets}
    if isinstance(publication,list):
        return {'id':publication[0],'ids':publication,'assets':assets}
    return {'id':publication,'assets':assets}


def fix_hash(saved):
    return hashlib.sha256(json.dumps({k:saved[k] for k in ('operation','source','before','spec')},sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def fix(config,store,slug):
    saved = store.get('specs/' + slug)
    if not saved or saved['state'] != 'drafting':
        return
    source = store.get('specs/' + saved['source'])
    if source.get('state') not in ('done','partial') or source.get('deleted'):
        raise StateError('Публикация ещё не завершена или уже удалена.')
    published_targets = {**source.get('results',{}),**source.get('published',{})}
    for target, published in published_targets.items():
        if target not in platforms.TARGETS:
            continue
        required = 'message_ids' if target == 'tg-channel' else 'id'
        if required not in published:
            name = target + ':publish'
            checkpoint = store.get('specs/' + saved['source'] + '/checkpoints/' + hashlib.sha256(name.encode()).hexdigest()) or {}
            published.update(published_ids(config,source['spec'],target,source.get('assets',{}).get(target,{}),checkpoint.get('value') if checkpoint.get('state') == 'done' else None))
        if target == 'threads':
            published.setdefault('assets',source.get('assets',{}).get(target,{}))
        published.setdefault('copy',source['spec']['copy'][target])
    store.change('specs/' + saved['source'],lambda value:value.update(published=published_targets))
    store.change('items/' + source['item'],lambda value:value.update(published=published_targets))
    before = copy.deepcopy(published_targets)
    spec = copy.deepcopy(source['spec'])
    spec.pop('blog',None)
    spec['targets'] = [t for t in platforms.live(spec['targets']) if t in before and not before[t].get('deleted')]
    if 'overlay' in spec:
        spec['overlay'] = {t:v for t,v in spec['overlay'].items() if t in spec['targets']}
    spec['copy'] = {t:before[t].get('copy') or spec['copy'][t] for t in spec['targets']}
    if saved['operation'] == 'fix':
        def validate(value):
            if drafter.answered(value):
                return drafter.answer_errors(value,('table','skills'))
            if not isinstance(value,dict) or set(value) != {'copy'}:
                return ['Return exactly copy']
            if not isinstance(value['copy'],dict) or set(value['copy']) != set(spec['copy']):
                return ['Keep exactly the published copy targets']
            candidate = {**spec,'copy':value['copy']}
            return drafter.validate(candidate,prepared=False,copy_only=True,owner=saved['instruction'])
        status = {t:{'url':source.get('results',{}).get(t,{}).get('url'),'error':source.get('errors',{}).get(t)} for t in platforms.live(source['spec']['targets'])}
        retry = source.get('state') == 'partial' and bool(source.get('hash')) and retryable(source) and any(source.get('errors',{}).get(t) and t not in source.get('results',{}) for t in source['spec']['targets'])
        asked = {'mode':'fix','published':{'copy':spec['copy']},'status':status,'table':table_rows(source,store.get('items/' + source['item']),config.get('timezone','Asia/Makassar')),'instruction':saved['instruction'],**({'retry_available':True} if retry else {})}
        corrected = drafter.ask(config,asked,[],validate,thread=(store,'conversations/' + saved['item'] + '-fix',slug,0,('specs/' + slug,'state','drafting')),follow=asked,rec=lambda usage:store.record_usage(saved['item'],'fix',usage))
        if drafter.answered(corrected):
            if corrected.get('action') == 'skills':
                enqueue({'step':'skills','text':saved['instruction']},'skills-' + slug)
                store.change('specs/' + slug,lambda value:value.update(state='rejected'))
                return
            message = tell(config,table_of(config,store,source) if corrected.get('action') == 'table' else corrected['answer'],**({'reply_markup':buttons(saved['source'],source['hash'],True)} if retry else {}))
            store.change('specs/' + slug,lambda value:value.update(state='rejected'))
            store.put('published-messages/' + str(message['message_id']),{'slug':saved['source']})
            return
        after = {**spec,'copy':corrected['copy']}
    else:
        after = spec
    saved.update(before=before,spec=after)
    saved['hash'] = fix_hash(saved)
    store.put('specs/' + slug,saved)
    lines = []
    for target in spec['targets']:
        label = 'Instagram' if target.startswith('ig-') else 'Threads' if target == 'threads' else target
        if saved['operation'] == 'delete':
            capability = platforms.manual_change(target,before[target],spec,'delete') or 'удалить'
            lines.append(label + ': ' + capability + ' ' + (before[target]['url'] or 'ссылка не получена'))
        else:
            old = spec['copy'][target]
            new = after['copy'][target]
            changes = [line for line in difflib.ndiff(old.splitlines(),new.splitlines()) if line.startswith(('- ','+ '))]
            if changes:
                warning = '\nбудет удалён и опубликован заново, лайки и комментарии пропадут' if target == 'threads' else '\nправь вручную' if target.startswith('ig-') else ''
                lines.append(label + warning + '\n' + '\n'.join(changes))
    if not lines:
        message = tell(config,'Удалять здесь нечего.' if saved['operation'] == 'delete' else 'Менять нечего: текст уже такой.')
        store.change('specs/' + slug,lambda value:value.update(state='rejected'))
        store.put('published-messages/' + str(message['message_id']),{'slug':saved['source']})
        return
    text = '\n'.join(lines)
    def say(text):
        message = tell(config,text)
        store.put('published-messages/' + str(message['message_id']),{'slug':saved['source']})
    chunk = ''
    for char in text:
        if platforms.units(chunk + char) > 3500:
            say(chunk)
            chunk = ''
        chunk += char
    if chunk:
        say(chunk)
    verb, label = ('del','Подтвердить удаление') if saved['operation'] == 'delete' else ('fix','Опубликовать правку')
    markup = {'inline_keyboard':[[{'text':label,'callback_data':verb + ':' + slug + ':' + saved['hash'][:8]},{'text':'Отклонить','callback_data':'rej:' + slug + ':' + saved['hash'][:8]}]]}
    message = tell(config,('Удаление поста' if saved['operation'] == 'delete' else 'Правка поста') + post_name(store,source),reply_markup=markup)
    def show(tx):
        value = store.get('specs/' + slug,tx) or {}
        store.supersede(tx,saved['item'],{slug})
        value.update(state='previewed',preview_message=message['message_id'])
        store.put('specs/' + slug,value,tx)
    store.atomic(show)
    store.put('published-messages/' + str(message['message_id']),{'slug':saved['source']})


def apply_fix(config,store,bucket,slug,directory):
    saved = store.claim_run(slug)
    if not saved:
        return
    if not saved.get('keyboard_removed'):
        remove_keyboard(config,saved)
        store.change('specs/' + slug,lambda value:value.update(keyboard_removed=True))
    for target in platforms.live(saved['spec']['targets']):
        if target in saved['results']:
            continue
        try:
            if fix_hash(saved) != saved['approved_hash']:
                raise StateError('Approval hash mismatch; запроси новое превью')
            published = saved['before'][target]
            source = store.get('specs/' + saved['source'])
            current = {**source.get('results',{}),**source.get('published',{})}[target]
            awaiting = {**published,'deleted':True,'awaiting_republish':slug,'url':''}
            if current != published and current != awaiting:
                raise StateError('Пост изменился после превью. Запроси новую правку.')
            unchanged = saved['operation'] == 'fix' and saved['spec']['copy'][target] == published.get('copy')
            if target == 'threads' and saved['operation'] == 'fix' and not unchanged and not platforms.manual_change(target,published,saved['spec'],'fix'):
                replacement = copy.deepcopy(saved['spec'])
                replacement['slug'] = slug
                assets = published.get('assets',{})
                media.materialize(bucket,assets,directory)
                if 'video.mp4' in assets:
                    replacement['media'] = {'video':'video.mp4','cover':'cover.jpg'}
                else:
                    originals = saved['spec'].get('media',{}).get('images',[])
                    if originals and not assets:
                        raise platforms.ExpiredMaterial('Материал истёк. Пришли файлы заново.')
                    replacement['media'] = {'images':[{'path':name,'alt':originals[index]['alt']} for index,name in enumerate(assets)]}
                session = platforms.social_session('threads',deleting=True)
                platforms.quota(session,False)
                urls = {name.removesuffix('.mp4').removesuffix('.jpg'):media.signed(bucket,manifest['object'],config,generation=manifest['generation']) for name,manifest in assets.items()}
                for name,url in urls.items():
                    filename = name + ('.mp4' if name == 'video' else '.jpg')
                    manifest = assets[filename]
                    platforms.probe(url,directory / filename,manifest.get('content_type') or mimetypes.guess_type(manifest['object'])[0])
                def deleted(tx):
                    original = store.get('specs/' + saved['source'],tx)
                    original.setdefault('published',{})[target] = awaiting
                    if target in original.get('results',{}):
                        original['results'][target] = awaiting
                    store.put('specs/' + saved['source'],original,tx)
                store.checkpoint(slug,target + ':delete',lambda:platforms.change_published(target,published,replacement,'delete',directory,bucket),deleted)
                state = {'steps':{}}
                for snapshot in store.db.collection('specs/' + slug + '/checkpoints').stream():
                    step = snapshot.to_dict()
                    state['steps'][step['name']] = {'status':step['state'],'value':step.get('value')}
                url = platforms.post_social(replacement,state,target,urls,session)
                result = {'status':'исправлено','id':state['steps'][target + ':publish']['value'],'url':url,'assets':assets}
            else:
                result = store.checkpoint(slug,target + ':apply',lambda:'без изменений' if unchanged else platforms.change_published(target,published,saved['spec'],saved['operation'],directory,bucket))
            if isinstance(result,dict) or result in ('исправлено','удалено'):
                def update(tx):
                    original = store.get('specs/' + saved['source'],tx)
                    mutation = store.get('specs/' + slug,tx)
                    item = copy.deepcopy({**original.get('results',{}),**original.get('published',{})}[target])
                    if isinstance(result,dict):
                        item.update({k:v for k,v in result.items() if k != 'status'})
                        item.pop('deleted',None)
                        item.pop('awaiting_republish',None)
                    if saved['operation'] == 'delete':
                        item['deleted'] = True
                    else:
                        item['copy'] = saved['spec']['copy'][target]
                    original.setdefault('published',{})[target] = item
                    if target in original.get('results',{}):
                        original['results'][target] = item
                    mutation.setdefault('results',{})[target] = result
                    mutation.setdefault('errors',{}).pop(target,None)
                    store.put('specs/' + saved['source'],original,tx)
                    store.put('specs/' + slug,mutation,tx)
                store.atomic(update)
            saved['results'][target] = result
            saved['errors'].pop(target,None)
        except Exception as failure:
            saved['errors'][target] = failure_text(failure,target,'Ошибка; сверь контрольные точки')
        store.change('specs/' + slug,lambda value:value.update(results=saved['results'],errors=saved['errors']))
    state = 'done' if all(t in saved['results'] for t in platforms.live(saved['spec']['targets'])) else 'partial'
    if fix_hash(saved) != saved['approved_hash']:
        state = 'rejected'
    store.change('specs/' + slug,lambda value:value.update(state=state))
    if state != 'rejected' and saved['operation'] == 'fix':
        try:
            voice.published(store,saved['spec'],saved.get('results',{}))
        except Exception:
            logging.error(json.dumps({'code':'voice_append_failed'}))
    source = store.get('specs/' + saved['source'])
    published_targets = {**source.get('results',{}),**source.get('published',{})}
    deleted = saved['operation'] == 'delete' and state == 'done' and all(x.get('deleted') or platforms.manual_change(t,x,saved['spec'],'delete') for t,x in published_targets.items())
    store.change('specs/' + saved['source'],lambda value:value.update(deleted=deleted))
    store.change('items/' + saved['item'],lambda value:value.update(published=published_targets,deleted=deleted))
    if state == 'done':
        learning.schedule(store,saved['item'],slug,'fixed')
    if deleted:
        store.forget(saved['item'])
    lines = []
    for target in platforms.live(saved['spec']['targets']):
        label = 'Instagram' if target.startswith('ig-') else 'Threads' if target == 'threads' else target
        result = saved['results'].get(target,saved['errors'].get(target,'Ошибка'))
        status = result['status'] if isinstance(result,dict) else result
        url = published_targets[target].get('url','')
        if published_targets[target].get('awaiting_republish'):
            status += '; удалено, ожидает повторной публикации'
        lines.append(label + ': ' + status + ' ' + (url or 'ссылка не получена'))
    extra = {}
    if not deleted:
        markup = buttons(saved['source'],source['hash'],published=True,accept=bool(source.get('results')) and not source.get('accepted'))
        if state == 'partial':
            markup['inline_keyboard'][0] = buttons(slug,saved['hash'],True)['inline_keyboard'][0] + markup['inline_keyboard'][0]
        extra = {'reply_markup':markup}
    message = tell(config,'\n'.join(lines),**extra)
    store.put('published-messages/' + str(message['message_id']),{'slug':saved['source']})
    finish_status(config,saved.get('status_message'),'Готово.' if state == 'done' else 'Готово не везде.')
    store.change('specs/' + slug,lambda value:value.pop('status_message',None))


@tasks_fn.on_task_dispatched(region=REGION,service_account=RUNTIME_SERVICE_ACCOUNT,secrets=[AUTOPOST],retry_config=options.RetryConfig(max_attempts=1),rate_limits=options.RateLimits(max_concurrent_dispatches=1),memory=options.MemoryOption.GB_2,timeout_sec=540,min_instances=0,max_instances=1)
def worker(request):
    return handle_worker(request)


def handle_worker(request):
    config,store,bucket = runtime()
    payload = request.data
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        platforms.configure(config,store,directory)
        stop = threading.Event()
        try:
            step = payload['step']
            action = ACTIONS.get(step,'typing')
            if step in ('run','apply-fix') and payload.get('slug'):
                media_kind = ((store.get('specs/' + payload['slug']) or {}).get('spec') or {}).get('media') or {}
                action = 'upload_video' if media_kind.get('video') or media_kind.get('clips') else action
            activity(config,action,stop)
            if payload.get('say') and step == 'run' and payload.get('slug'):
                try:
                    claimable = store.get('specs/' + payload['slug']) or {}
                    if claimable.get('state') in ('approved','running') and not claimable.get('deleted'):
                        open_publish_status(config,store,payload['slug'])
                except Exception:
                    logging.error(json.dumps({'code':'say_failed','step':payload.get('step')}))
                    try:
                        tell(config,payload['say'])
                    except Exception:
                        pass
            elif payload.get('say'):
                try:
                    tell(config,payload['say'],**({'reply_markup':payload['markup']} if payload.get('markup') else {}))
                except Exception:
                    logging.error(json.dumps({'code':'say_failed','step':payload.get('step')}))
            sync_pin(config,store)
            if step == 'draft':
                draft(config,store,bucket,payload,directory)
            elif step == 'montage':
                plan_montage(config,store,bucket,payload,directory)
            elif step == 'prepared':
                prepared(config,store,bucket,payload)
            elif step == 'rendered':
                rendered(config,store,bucket,payload,directory)
            elif step == 'answer':
                question(config,store,payload)
            elif step == 'fix':
                fix(config,store,payload['slug'])
            elif step == 'apply-fix':
                apply_fix(config,store,bucket,payload['slug'],directory)
            elif step == 'run':
                run(config,store,bucket,payload['slug'],directory,payload.get('resume',False))
            elif step == 'upload-prompt':
                upload_prompt(config,store,bucket,payload)
            elif step == 'upload-complete':
                upload_complete(config,store,bucket,payload,directory)
            elif step == 'finish-status':
                finish_status(config,payload['status'],payload['line'])
            elif step in ('edit-prompt','remove-keyboard'):
                saved = store.get('specs/' + payload['slug']) or {}
                remove_keyboard(config,{'preview_message':payload['message']} if payload.get('message') else saved,payload.get('line'))
                saved = store.get('specs/' + payload['slug']) or {}
                if step == 'edit-prompt' and saved and saved['state'] == 'needs-edit':
                    tell(config,'Напиши, что поправить в черновике' + post_name(store,saved) + '.')
            elif step == 'collect-prompt':
                collecting = store.get('items/' + payload['item'])
                if collecting and collecting.get('status') == 'collecting':
                    send_collect(config,store,collect_reply(config,store,payload['item'],collecting,collected(collecting)))
            elif step == 'accept':
                accepted = store.get('specs/' + payload['slug']) or {}
                track(store.close_session,accepted.get('item'),'published','принято')
                sync_pin(config,store)
                missing = [ROLES[t][0] + ': ' + ROLES[t][1] + ' — ' + accepted.get('errors',{}).get(t,'не опубликовано') for t in platforms.live((accepted.get('spec') or {}).get('targets',[])) if t not in accepted.get('results',{})]
                tell(config,'Принято' + post_name(store,accepted) + '.' + (' Не опубликовано:\n' + '\n'.join(missing) if missing else ''))
            elif step == 'verify':
                verify(config,store,payload['slug'],payload.get('attempt',1))
            elif step == 'learn':
                learned = payload.get('item') or (store.get(payload['record']) or {}).get('item') if payload.get('record') else None
                learning.step(config,store,payload)
                if learned:
                    track(store.step,learned,'learn',{'state':'done'})
            elif step == 'skills':
                learning.command(config,store,payload,tell)
            elif step == 'skill':
                learning.show(config,store,payload,tell)
            elif step == 'fix-prompt':
                tell(config,'Напиши, что поправить в посте' + post_name(store,store.get('specs/' + payload['slug']) or {}) + '.')
            store.flush(enqueue)
        except Exception as failure:
            logging.error(json.dumps({'code':'worker_failed','step':payload.get('step'),'type':type(failure).__name__,'error':str(failure)[:200]},ensure_ascii=False))
            current = True
            if payload.get('item') and payload.get('step') in ('draft','montage','prepared','rendered','upload-complete'):
                def fail_item(tx):
                    value = store.get('items/' + payload['item'],tx)
                    if value and value.get('revision') == payload.get('revision') and value.get('status') in ('queued','drafting'):
                        value['status'] = 'failed'
                        store.put('items/' + payload['item'],value,tx)
                        return True
                    return False
                current = store.atomic(fail_item)
                if current:
                    follow = payload['step'] == 'draft' and any(e['id'] == 'edit' and e['state'] == 'running' and e.get('revision') == payload.get('revision') for e in (store.get('sessions/' + payload['item']) or {}).get('steps',[]))
                    track(store.step,payload['item'],'edit' if follow else {'prepared':'prepare','rendered':'render'}.get(payload['step'],payload['step']),{'state':'failed'},payload.get('revision'))
                    try:
                        finish_status(config,(store.get('items/' + payload['item']) or {}).get('status_message'),'Остановлено.')
                    except Exception:
                        pass
            if payload.get('step') == 'fix' and payload.get('slug'):
                def fail_fix(tx):
                    value = store.get('specs/' + payload['slug'],tx)
                    if value and value.get('state') == 'drafting':
                        value['state'] = 'failed'
                        store.put('specs/' + payload['slug'],value,tx)
                store.atomic(fail_fix)
            if payload.get('step') in ('run','apply-fix') and payload.get('slug'):
                def stopped(value):
                    value.update(state='failed' if isinstance(failure,platforms.ExpiredMaterial) else 'partial')
                    if value['state'] == 'partial' and value.get('spec'):
                        missing_errors(value)
                        value['published_at'] = time.time()
                store.change('specs/' + payload['slug'],stopped)
                try:
                    closing = (store.get('specs/' + payload['slug']) or {}).get('publish_message')
                    finish_status(config,closing,'Остановлено.')
                    store.change('specs/' + payload['slug'],lambda value:value.pop('publish_message',None))
                except Exception:
                    logging.error(json.dumps({'code':'publish_message_not_closed'}))
            text = str(failure) if isinstance(failure,(Error,StateError)) else 'Операция остановлена. Материал и контрольные точки сохранены.'
            extra = {}
            if payload.get('step') in ('run','apply-fix') and payload.get('slug') and not isinstance(failure,platforms.ExpiredMaterial):
                saved = store.get('specs/' + payload['slug'])
                if saved and saved.get('hash'):
                    extra['reply_markup'] = buttons(payload['slug'],saved['hash'],True)
            if current and payload.get('step') in ('draft','montage') and payload.get('item') and payload.get('revision') is not None:
                extra['reply_markup'] = {'inline_keyboard':[[{'text':'Повторить','callback_data':'again:' + payload['item'] + ':' + str(payload['revision'])}]]}
            name = ''
            try:
                if payload.get('item'):
                    name = item_name(store.get('items/' + payload['item']),store,payload['item'])
                elif payload.get('slug'):
                    saved = store.get('specs/' + payload['slug']) or {}
                    name = post_name(store,saved)
            except Exception:
                name = ''
            if name and text.startswith(('Черновик не готов','Черновик не получился')):
                text = 'Черновик' + name + text[len('Черновик'):]
            elif name:
                text =('Пост' if payload.get('step') in ('run','apply-fix','fix') else 'Черновик') + name + ': ' + text
            if current:
                tell_reliably(config,store,text,**extra)
        finally:
            sync_pin(config,store)
            stop.set()


def mend_sessions(store):
    now = int(time.time())
    owned = {snapshot.id for snapshot in store.db.collection('sessions').stream()}
    for snapshot in store.db.collection('specs').where('state','==','partial').stream():
        def unmended(value):
            return bool(value.get('spec')) and not value.get('operation') and (not value.get('published_at') or any(t not in value.get('results',{}) and not value.get('errors',{}).get(t) for t in platforms.live(value['spec'].get('targets',[]))))
        def mend(value):
            missing_errors(value,True)
            value.setdefault('published_at',value.get('started_at') or value.get('updated') or now)
        if unmended(snapshot.to_dict()):
            store.change('specs/' + snapshot.id,mend)
    for snapshot in store.db.collection('specs').stream():
        saved = snapshot.to_dict()
        if saved.get('item') and saved['item'] not in owned and not saved.get('operation') and saved.get('state') in ('done','partial') and (saved.get('results') or saved.get('published')) and (saved.get('published_at') or saved.get('updated') or now) > now - SESSION_AGE:
            if store.backfill(saved['item'],store.get('items/' + saved['item']),saved):
                owned.add(saved['item'])
    store.flush(enqueue)


def maintenance(config,store,bucket):
    info = platforms.telegram('getWebhookInfo')
    if info.get('url') != config['webhook_url'] or info.get('last_error_date'):
        platforms.telegram('setWebhook',{'url':config['webhook_url'],'secret_token':config['webhook_secret'],'allowed_updates':['message','callback_query','business_connection']})
    now = int(time.time())
    sync_pin(config,store)
    try:
        voice.refresh(config,store)
    except Exception:
        logging.error(json.dumps({'code':'voice_refresh_failed'}))
    for provider,host,prefix in [('instagram','https://graph.instagram.com','ig'),('threads','https://graph.threads.net','th')]:
        token = platforms.token(config,store,provider)
        if not token:
            continue
        expiry = token.get('expires_at',0)
        if expiry <= now:
            def notify(value):
                if value.get('day') == now // 86400:
                    return False
                value['day'] = now // 86400
                return True
            if store.change('token-notifications/' + provider,notify):
                tell(config,provider + ': нужен новый токен: Generate, setup.sh meta, secret')
            continue
        try:
            if expiry - now < 30*86400 and now-token.get('refreshed_at',token.get('issued_at',0)) >= 86400:
                reply = platforms.request('GET',host + '/refresh_access_token',{'grant_type':prefix + '_refresh_token','access_token':token['access_token']})
                store.put('tokens/' + provider,{**token,'access_token':reply['access_token'],'expires_at':now + int(reply['expires_in']),'refreshed_at':now})
        except Error:
            if expiry - now <= 7*86400:
                tell(config,provider + ': токен нужно обновить вручную в течение 7 дней.')
    for render_snapshot in store.db.collection('renders').stream():
        render_job = render_snapshot.to_dict()
        if render_job.get('state') in ('done','failed') and not render_job.get('delivered'):
            enqueue({'step':'prepared' if render_job.get('mode') == 'prepare' else 'rendered','item':render_job['item'],'revision':render_job['revision'],'render':render_snapshot.id},'render-recovery-' + render_snapshot.id)
    for snapshot in store.db.collection('specs').stream():
        saved = snapshot.to_dict()
        if saved.get('state') == 'rejected' and not saved.get('operation'):
            for prefix in ('inbox/' + saved['item'] + '/','media/' + snapshot.id + '/'):
                for blob in bucket.list_blobs(prefix=prefix):
                    blob.delete(if_generation_match=blob.generation)
    for snapshot in store.db.collection('items').stream():
        if snapshot.to_dict().get('status') == 'rejected':
            store.forget(snapshot.id)
            for blob in bucket.list_blobs(prefix='inbox/' + snapshot.id + '/'):
                blob.delete(if_generation_match=blob.generation)
    for snapshot in store.db.collection('sessions').stream():
        journal = snapshot.to_dict()
        if journal.get('state') != 'open':
            if journal.get('ended') and journal['ended'] < now - SESSION_AGE:
                store.delete('sessions/' + snapshot.id)
            continue
        owner = store.get('items/' + snapshot.id)
        if not owner or owner.get('status') == 'rejected' or owner.get('status') == 'failed' and now - owner.get('updated',now) > 7*86400:
            store.close_session(snapshot.id,'cancelled','отменён' if owner and owner.get('status') == 'rejected' else 'без ответа 7 дней')
            continue
        saved = store.get('specs/' + str(journal.get('slug'))) or {}
        if saved.get('state') in ('done','partial') and (saved.get('results') or saved.get('published')) and now - saved.get('published_at',now) > 3600:
            if not any(e['id'] == 'verify' and e['state'] != 'planned' for e in journal['steps']):
                enqueue({'step':'verify','slug':journal['slug'],'attempt':1},'verify-sweep-' + journal['slug'] + '-' + str(now // 86400))
            elif now - saved.get('published_at',now) > 86400:
                store.close_session(snapshot.id,'published','опубликовано' if saved['state'] == 'done' else 'опубликовано не везде')
    store.flush(enqueue)


def reject_stale_fixes(store):
    fixes = {}
    for snapshot in store.db.collection('specs').where('state','==','previewed').stream():
        saved = snapshot.to_dict()
        if saved.get('operation') == 'fix' and saved.get('item'):
            fixes.setdefault(saved['item'],[]).append(snapshot.id)
    for slugs in fixes.values():
        for slug in sorted(slugs,key=lambda name:int(name[4:]) if name[4:].isdigit() else 0)[:-1]:
            def reject(tx):
                saved = store.get('specs/' + slug,tx)
                if saved and saved.get('state') == 'previewed':
                    saved['state'] = 'rejected'
                    store.put('specs/' + slug,saved,tx)
                    if saved.get('preview_message'):
                        store.job(tx,{'slug':slug,'step':'remove-keyboard','message':saved['preview_message']},'superseded-' + slug)
            store.atomic(reject)


def recover_running(config,store):
    reject_stale_fixes(store)
    for snapshot in store.db.collection('specs').where('state', '==', 'running').stream():
        def recover(value):
            if value.get('state') != 'running' or time.time() - value.get('started_at',time.time()) <= 600:
                return False
            missing_errors(value,True)
            if pending_targets(value) and not value.get('operation'):
                value['started_at'] = time.time()
                return value
            value['state'] = 'partial'
            value['published_at'] = time.time()
            value['retry_pending'] = 'run'
            return value
        saved = store.change('specs/' + snapshot.id,recover)
        if saved:
            track(store.publish_progress,snapshot.id,None,{'timeout':True})
        if saved and saved['state'] == 'running':
            enqueue({'step':'run','slug':snapshot.id,'resume':True},'recover-' + snapshot.id + '-' + secrets.token_hex(4))
        elif saved:
            announce_retry(config,store,snapshot.id,saved)
    for snapshot in store.db.collection('specs').where('state', '==', 'partial').stream():
        pending = snapshot.to_dict()
        if pending.get('retry_pending'):
            announce_retry(config,store,snapshot.id,pending)
    for snapshot in store.db.collection('specs').where('state', '==', 'rendering').stream():
        spec = snapshot.to_dict()
        render = spec.get('final_render')
        job = store.get('renders/' + render) if render else None
        if (job or {}).get('state') == 'done' and not job.get('delivered'):
            continue
        if job and job.get('state') == 'done':
            fail_final(config,store,snapshot.id,render)
            continue
        if job and job.get('state') in ('queued','running') and not spec.get('started_at'):
            if (job.get('created') or (job.get('progress') or {}).get('since')) and not overdue(job,time.time()):
                continue
        fail_final(config,store,snapshot.id,render,FINAL_DEADLINE)
    now = time.time()
    for snapshot in store.db.collection('renders').where('state','in',['queued','running']).stream():
        job = snapshot.to_dict()
        if dead_render(job,store.get('items/' + str(job.get('item')))) or overdue(job,now):
            store.change('renders/' + snapshot.id,lambda value:value.update(state='failed',delivered=True) if value.get('state') in ('queued','running') else None)
    stop_stale(config,store)
    sync_pin(config,store)


COLLECT_DEADLINE = 6 * 3600


def stop_stale(config,store):
    now = time.time()
    renders = [snapshot.to_dict() for snapshot in store.db.collection('renders').where('state','in',['queued','running','done']).stream()]
    busy = {job.get('item') for job in renders if job['state'] != 'done' or not job.get('delivered')}
    drafting = [(snapshot.id,snapshot.to_dict()) for snapshot in store.db.collection('specs').where('state','==','drafting').stream()]
    drafting = [(slug,spec) for slug,spec in drafting if spec.get('item') and not spec.get('operation')]
    keys = {snapshot.id for snapshot in store.db.collection('items').where('status','in',['queued','drafting']).stream()} | {spec['item'] for slug,spec in drafting}
    for key in sorted(keys):
        item = store.get('items/' + key)
        if not item or item.get('status') == 'rejected':
            continue
        limit = stale_limit(item,key in busy)
        since = item.get('updated') or item.get('created') or 0
        def stop(value):
            if value.get('status') not in ('queued','drafting') or bool(value.get('waiting_upload')) != bool(item.get('waiting_upload')) or not stale(value,now,key in busy):
                return False
            value.update(status='failed',stalled=True,stop_why='upload' if value.get('waiting_upload') else 'stalled',**({'upload_timeout':True} if value.get('waiting_upload') else {}))
            return True
        stopped = store.change('items/' + key,stop) if item.get('status') in ('queued','drafting') else False
        held = item.get('status') in ('queued','drafting') and not stopped
        newest = newest_states(snapshot.to_dict() for snapshot in store.db.collection('specs').where('item','==',key).stream())
        for slug,spec in drafting:
            if spec['item'] == key and not held and (stopped or now - (spec.get('updated') or since) > limit):
                if not stopped and item.get('status') != 'failed' and newest.get(key) == 'drafting':
                    store.change('items/' + key,lambda value:value.update(stalled=True,stop_why='stalled'))
                store.change('specs/' + slug,lambda value:value.update(state='failed') if value.get('state') == 'drafting' else None)
    for snapshot in store.db.collection('items').where('status','==','collecting').stream():
        item = snapshot.to_dict()
        if now - (item.get('updated') or item.get('created') or now) > COLLECT_DEADLINE:
            def close(value):
                if value.get('status') != 'collecting' or now - (value.get('updated') or value.get('created') or now) <= COLLECT_DEADLINE:
                    return False
                value.update(status='rejected',waiting_upload=False,updated=now,revision=value.get('revision',0) + 1)
                return True
            if store.change('items/' + snapshot.id,close):
                def release(value):
                    if value.get('collecting') == snapshot.id:
                        value.pop('collecting')
                store.change('sessions/inbox',release)
                track(store.close_session,snapshot.id,'cancelled','без нового материала 6 часов')
                if item.get('keyboard_message'):
                    remove_keyboard(config,{'preview_message':item['keyboard_message']})
                try:
                    tell(config,'Пост' + (item_name(item) or ' без названия') + ': 6 часов без нового материала, отменён. Начни /post.')
                except Exception:
                    logging.error(json.dumps({'code':'stalled_notice_failed'}))
    for snapshot in store.db.collection('items').where('stalled','==',True).stream():
        item = snapshot.to_dict()
        try:
            tell(config,('Пост' + (item_name(item) or ' без названия') + ': файл так и не загрузился, остановлен. Начни /post.') if item.get('upload_timeout') else 'Черновик' + (item_name(item) or ' без названия') + ' завис и остановлен. Начни /post.')
            store.change('items/' + snapshot.id,lambda value:(value.pop('stalled',None),value.pop('upload_timeout',None)))
        except Exception:
            logging.error(json.dumps({'code':'stalled_notice_failed'}))


@scheduler_fn.on_schedule(schedule='every 15 minutes',timezone='UTC',region=REGION,service_account=RUNTIME_SERVICE_ACCOUNT,secrets=[AUTOPOST],timeout_sec=540,min_instances=0,max_instances=1)
def daily(event):
    config,store,bucket = runtime()
    platforms.configure(config,store,Path('/tmp'))
    recover_running(config,store)
    store.flush(enqueue)
    try:
        mend_sessions(store)
    except Exception:
        logging.error(json.dumps({'code':'mend_failed'}))
    local = datetime.now(ZoneInfo(config.get('timezone','Asia/Makassar')))
    if local.hour != 6 or local.minute >= 15:
        return
    def claim(value):
        if value.get('date') == local.date().isoformat():
            return False
        value['date'] = local.date().isoformat()
        return True
    if not store.change('maintenance/daily',claim):
        return
    platforms.configure(config,store,Path('/tmp'))
    try:
        maintenance(config,store,bucket)
    except Exception:
        logging.error(json.dumps({'code':'daily_failed'}))
        tell(config,'Ежедневная проверка не завершена. Проверь доступы сервисного аккаунта.')
