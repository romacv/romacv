import copy
import difflib
import hashlib
import hmac
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import tempfile
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
from store import Store, StateError
import drafter
import media
import montage

AUTOPOST = SecretParam('AUTOPOST')
REGION = 'us-central1'
RUNTIME_SERVICE_ACCOUNT = 'autopost-runtime@resromcom-493b4.iam.gserviceaccount.com'


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


def tell(config, text, **extra):
    return platforms.telegram('sendMessage',{'chat_id':config['roman_chat_id'],'text':text,**extra})


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


@https_fn.on_request(region=REGION,service_account=RUNTIME_SERVICE_ACCOUNT,secrets=[AUTOPOST],timeout_sec=60,min_instances=0,max_instances=2,invoker='public')
def webhook(request):
    config, store, bucket = runtime()
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
            if saved.get('revision') != revision or not saved.get('waiting_upload'):
                return False
            saved['upload_complete_requested'] = True
            return True
        if store.change('items/' + item, complete):
            enqueue({'step':'upload-complete','item':item,'revision':revision},'uploaded-' + item + '-' + str(revision) + '-' + secrets.token_hex(8))
        return https_fn.Response('{}',headers=cors,content_type='application/json')
    if not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token','').encode(),config['webhook_secret'].encode()):
        return https_fn.Response('',status=403)
    if not authorized(value,config) or 'update_id' not in value:
        return https_fn.Response('{}',content_type='application/json')
    result = store.intake(value,config)
    store.flush(enqueue)
    query = value.get('callback_query')
    if query:
        return https_fn.Response(json.dumps({'method':'answerCallbackQuery','callback_query_id':query['id'],'text':'Принято' if result else 'Кнопка устарела или уже обработана'}),content_type='application/json')
    text = result.get('text') if isinstance(result,dict) else result
    reply = {'method':'sendMessage','chat_id':config['roman_chat_id'],'text':text} if text else {}
    return https_fn.Response(json.dumps(reply,ensure_ascii=False),content_type='application/json')


def asset_hash(spec, assets):
    return hashlib.sha256(json.dumps({'spec':spec,'assets':assets},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def buttons(slug, digest, retry=False):
    actions = [('retry','Повторить')] if retry else [('pub','Опубликовать'),('edit','Править'),('rej','Отклонить')]
    values = [{'text':label,'callback_data':verb + ':' + slug + ':' + digest[:8]} for verb,label in actions]
    if any(len(x['callback_data'].encode()) > 64 for x in values):
        raise Error('Callback exceeds 64 bytes')
    return {'inline_keyboard':[values]}


def preview(config, store, bucket, item_id, spec, assets, directory):
    def send(method, fields, files=None):
        message = platforms.telegram(method,fields,files)
        store.put('published-messages/' + str(message['message_id']),{'slug':spec['slug']})
        return message
    def say(text, **extra):
        return send('sendMessage',{'chat_id':config['roman_chat_id'],'text':text,**extra})
    previous = store.get('specs/' + spec['slug']) or {}
    if previous.get('state') not in (None,'drafting','needs-edit'):
        raise StateError('Existing preview or publication cannot be overwritten')
    digest = asset_hash(spec,assets)
    saved = {'item':item_id,'spec':spec,'assets':assets,'hash':digest,'state':'drafting','results':{},'errors':{},'attempts':{}}
    store.put('specs/' + spec['slug'],saved)
    platforms.telegram_identity()
    for target in spec['targets']:
        target_assets = assets.get(target,{})
        media.materialize(bucket,target_assets,directory)
        if 'video.mp4' in target_assets:
            send('sendVideo',{'chat_id':config['roman_chat_id'],'caption':target},{'video':directory/'video.mp4'})
            if 'cover.jpg' in target_assets:
                send('sendPhoto',{'chat_id':config['roman_chat_id'],'caption':target + ' cover'},{'photo':directory/'cover.jpg'})
        else:
            for name in target_assets:
                send('sendPhoto',{'chat_id':config['roman_chat_id'],'caption':target},{'photo':directory/name})
        text = json.dumps(spec['blog'],ensure_ascii=False,indent=2) if target == 'blog' else spec['copy'][target]
        chunk = target + '\n'
        for char in text:
            if platforms.units(chunk + char) > 3500:
                say(chunk)
                chunk = ''
            chunk += char
        if chunk:
            say(chunk)
    message = say('Черновик ' + spec['slug'],reply_markup=buttons(spec['slug'],digest))
    saved.update(state='previewed',preview_message=message['message_id'])
    store.put('specs/' + spec['slug'],saved)
    store.change('items/' + item_id,lambda value:value.update(status='previewed'))


def upload_prompt(config,store,bucket,payload):
    item = store.get('items/' + payload['item'])
    if not item or item['revision'] != payload['revision'] or not item.get('waiting_upload'):
        return
    expires = int(time.time()) + 900
    uploads = []
    for attachment in media.attachments(item):
        if attachment.get('file_size',0) > 20*1024*1024 and attachment['name'] not in item.get('uploads',{}):
            name = 'inbox/' + payload['item'] + '/' + attachment['name']
            uploads.append({'name':attachment['name'],'url':media.signed(bucket,name,config,'PUT',15)})
    fragment = {'uploads':uploads,'complete':'https://us-central1-resromcom-493b4.cloudfunctions.net/webhook','receipt':{'action':'uploaded','item':payload['item'],'revision':item['revision'],'expires':expires,'token':upload_mac(config,payload['item'],item['revision'],expires)}}
    tell(config,'Файл больше 20 МБ, загрузи сюда (ссылка 15 минут):\nhttps://www.resrom.com/u/#' + quote(json.dumps(fragment,separators=(',',':')),safe=''))


def upload_complete(config,store,bucket,payload,directory):
    item = store.get('items/' + payload['item'])
    if not item or item['revision'] != payload['revision'] or not item.get('waiting_upload'):
        return
    uploads = dict(item.get('uploads',{}))
    pending = []
    total = sum(value.get('size',0) for value in uploads.values()) + sum(a.get('file_size',0) for a in media.attachments(item) if a['name'] not in uploads and a.get('file_size',0) <= 20*1024*1024)
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
        if total > 1024*1024*1024 or blob.size > 512*1024*1024 or blob.content_type != 'video/mp4':
            raise Error('Пакет должен быть MP4 <=512 МБ на файл, <=1 ГБ всего.')
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
        saved.update(uploads=uploads,waiting_upload=False)
        store.put('items/' + payload['item'],saved,tx)
        if saved['status'] == 'queued':
            store.job(tx,{'item':payload['item'],'revision':saved['revision'],'step':'montage' if saved.get('montage') else 'draft'},'upload-ready-' + payload['item'] + '-' + str(saved['revision']))
    store.atomic(finish)
    store.flush(enqueue)
    tell(config,'Загрузка завершена.' + (' Напиши «монтаж», когда все клипы отправлены.' if item['status'] == 'collecting' else ' Готовлю черновик.'))


def draft(config,store,bucket,payload,directory):
    item = store.claim_item(payload['item'],payload['revision'])
    if not item:
        return
    inputs = media.intake(bucket,payload['item'],item,config,directory)
    if any(a['kind'] != 'photo' for a in inputs) and not item.get('prepared'):
        start_preparation(config,store,bucket,payload,item,inputs)
        return
    config = {**config,'prepared_audio':item.get('prepared',{}).get('audio',{})}
    if item.get('prepared'):
        store.change('items/' + payload['item'],lambda value:value.update(uploads={a['name']:a['manifest'] for a in inputs}))
    transcripts,images,files = [],[],{}
    for attachment in inputs:
        if attachment['kind'] == 'photo':
            destination = directory / ('clean-' + attachment['name'])
            media.clean_image(attachment['path'],destination)
            images.append(destination)
            files[destination.name] = destination
        elif attachment['kind'] == 'voice':
            transcript = item.get('transcripts',{}).get(attachment['name'])
            if transcript is None:
                transcript = media.transcribe(bucket,store,attachment['path'],payload['item'] + '/' + attachment['name'],config)
                item.setdefault('transcripts',{})[attachment['name']] = transcript
                store.change('items/' + payload['item'],lambda value:value.update(transcripts=item['transcripts']))
            transcripts.append(transcript)
        else:
            raise Error('Пришли «монтаж» для обработки видео.')
    source = {'slug':item['slug'],'messages':item['messages'],'edits':item.get('edits',[]),'transcripts':transcripts,'available_images':list(files)}
    source['public_projects'] = platforms.request('GET','https://raw.githubusercontent.com/romacv/romacv/main/data.json')
    def validate(value):
        errors = drafter.validate(value, prepared=False)
        if value.get('slug') != item['slug']:
            errors.append('Keep the supplied slug')
        try:
            if any(path.name not in files for path in platforms.media_files(value).values()):
                errors.append('Use supplied clean media only')
        except Error:
            errors.append('Invalid media path')
        return errors
    spec = drafter.ask(config,source,images,validate)
    assets = {}
    for target in spec['targets']:
        target_assets = {}
        for index,image in enumerate(spec.get('media',{}).get('images',[])):
            path = directory/image['path']
            if target in ('ig-story','tg-story'):
                converted = directory/(target + '-' + str(index) + '.jpg')
                media.clean_image(path,converted,True)
                path = converted
            target_assets['image' + str(index) + '.jpg'] = media.upload(bucket,path,'media/' + item['slug'] + '/' + str(payload['revision']) + '/' + target + '/' + str(index) + '.jpg')
        assets[target] = target_assets
    preview(config,store,bucket,payload['item'],spec,assets,directory)


def plan_montage(config,store,bucket,payload,directory):
    item = store.claim_item(payload['item'],payload['revision'])
    if not item:
        return
    inputs = media.intake(bucket,payload['item'],item,config,directory)
    if any(a['kind'] != 'photo' for a in inputs) and not item.get('prepared'):
        start_preparation(config,store,bucket,payload,item,inputs)
        return
    config = {**config,'prepared_audio':item.get('prepared',{}).get('audio',{})}
    if item.get('prepared'):
        store.change('items/' + payload['item'],lambda value:value.update(uploads={a['name']:a['manifest'] for a in inputs}))
    sources,transcripts,images,manifests = {},{},[],{}
    for attachment in inputs:
        if attachment['kind'] != 'video':
            continue
        name = attachment['path'].stem
        sources[name] = item['prepared']['durations'][name]
        transcripts[name] = item.get('transcripts',{}).get(name)
        if transcripts[name] is None:
            transcripts[name] = media.transcribe(bucket,store,attachment['path'],payload['item'] + '/' + attachment['name'],config)
            store.change('items/' + payload['item'],lambda value:value.update(transcripts=transcripts))
        manifests[name] = item['prepared']['sources'][name]
    for index, manifest in enumerate(item['prepared']['sheets']):
        path = directory / ('sheet-' + str(index) + '.jpg')
        media.download(bucket,manifest,path)
        images.append(path)
    if not sources:
        raise Error('Нет клипов для монтажа.')
    if len(images) > 100:
        raise Error('Слишком много кадров. Раздели материал на несколько пакетов.')
    edl = drafter.ask(config,{'mode':'montage','sources':sources,'transcripts':transcripts,'messages':item['messages'],'edits':item.get('edits',[]),'previous_edl':item.get('edl')},images,lambda value:montage.validate(value,sources),True)
    identifier = secrets.token_hex(16)
    project = firebase_admin.get_app().project_id
    job = {'id':identifier,'item':payload['item'],'revision':payload['revision'],'edl':edl,'sources':manifests,'bucket':bucket.name,'prefix':'media/' + item['slug'] + '/' + identifier,'project':project,'worker_url':config['worker_url'],'service_account':config['runtime_service_account'],'state':'queued'}
    store.put('renders/' + identifier,job)
    store.change('items/' + payload['item'],lambda value:value.update(edl=edl,render=identifier))
    from google.cloud import run_v2
    client = run_v2.JobsClient()
    client.run_job(request=run_v2.RunJobRequest(name=f'projects/{project}/locations/us-central1/jobs/autopost-render',overrides=run_v2.RunJobRequest.Overrides(container_overrides=[run_v2.RunJobRequest.Overrides.ContainerOverride(env=[{'name':'RENDER_ID','value':identifier}])],task_count=1)))
    tell(config,'План монтажа проверен. Видео рендерится.')


def start_preparation(config,store,bucket,payload,item,inputs):
    identifier = secrets.token_hex(16)
    project = firebase_admin.get_app().project_id
    job = {'id':identifier,'mode':'prepare','item':payload['item'],'revision':payload['revision'],'sources':{a['name']:a['manifest'] for a in inputs if a['kind'] != 'photo'},'bucket':bucket.name,'prefix':'inbox/' + payload['item'] + '/prepared/' + identifier,'project':project,'worker_url':config['worker_url'],'service_account':config['runtime_service_account'],'state':'queued'}
    store.put('renders/' + identifier,job)
    store.change('items/' + payload['item'],lambda value:value.update(render=identifier))
    from google.cloud import run_v2
    run_v2.JobsClient().run_job(request=run_v2.RunJobRequest(name=f'projects/{project}/locations/us-central1/jobs/autopost-render',overrides=run_v2.RunJobRequest.Overrides(container_overrides=[run_v2.RunJobRequest.Overrides.ContainerOverride(env=[{'name':'RENDER_ID','value':identifier}])],task_count=1)))


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
        item.update(prepared=job['outputs'],status='queued')
        store.put('items/' + payload['item'],item,tx)
        store.job(tx,{'item':payload['item'],'revision':payload['revision'],'step':'montage' if item.get('montage') else 'draft'},'prepared-' + payload['render'])
    store.atomic(finish)
    store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))


def rendered(config,store,bucket,payload,directory):
    item = store.get('items/' + payload['item'])
    job = store.get('renders/' + payload['render'])
    if job.get('state') == 'failed':
        store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))
    if not item or item.get('render') != payload['render'] or item['revision'] != payload['revision'] or item.get('status') != 'drafting':
        store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))
        return
    if job.get('state') != 'done':
        raise Error('Рендер не завершён. Материал сохранён.')
    edl = job['edl']
    spec = {'slug':item['slug'],'kind':edl['kind'],'at':datetime.now(timezone.utc).isoformat(),'targets':list(edl['targets']),'media':{'video':'video.mp4','cover':'cover.jpg'},'copy':edl['copy'],'approval':{}}
    assets = {target:{'video.mp4':manifest,'cover.jpg':job['outputs']['cover']} for target,manifest in job['outputs']['videos'].items()}
    preview(config,store,bucket,payload['item'],spec,assets,directory)
    store.change('renders/' + payload['render'],lambda value:value.update(delivered=True))


def remove_keyboard(config,saved):
    if saved.get('preview_message'):
        try:
            platforms.telegram('editMessageReplyMarkup',{'chat_id':config['roman_chat_id'],'message_id':saved['preview_message'],'reply_markup':{'inline_keyboard':[]}})
        except Error as failure:
            if not getattr(failure, 'not_modified', False):
                raise


def run(config,store,bucket,slug,directory):
    saved = store.claim_run(slug)
    if not saved:
        return
    if saved.get('deleted'):
        store.change('specs/' + slug,lambda value:value.update(state='rejected'))
        tell(config,'Публикация уже удалена. Повторная публикация запрещена.')
        return
    if not saved.get('keyboard_removed'):
        remove_keyboard(config,saved)
        store.change('specs/' + slug,lambda value:value.update(keyboard_removed=True))
    spec,assets = saved['spec'],saved['assets']
    if asset_hash(spec,assets) != saved['approved_hash']:
        raise StateError('Approval hash mismatch')
    for manifests in assets.values():
        for manifest in manifests.values():
            if not bucket.blob(manifest['object'],generation=int(manifest['generation'])).exists():
                raise platforms.ExpiredMaterial('Материал истёк. Пришли файлы заново.')
    due = platforms.when(spec)
    if due > datetime.now(timezone.utc):
        store.change('specs/' + slug,lambda value:value.update(state='approved'))
        enqueue({'step':'run','slug':slug},'due-' + slug + '-' + secrets.token_hex(8),max(1,int((due-datetime.now(timezone.utc)).total_seconds())))
        return
    published_targets = {**saved['results'],**saved.get('published',{})}
    for target in spec['targets']:
        if target in published_targets:
            continue
        attempts = saved.setdefault('attempts',{})
        if attempts.get(target,0) >= 3:
            continue
        attempts[target] = attempts.get(target,0) + 1
        store.change('specs/' + slug,lambda value:value.update(attempts=attempts))
        state = {'steps':{}}
        target_assets = assets.get(target,{})
        try:
            media.materialize(bucket,target_assets,directory)
            target_spec = json.loads(json.dumps(spec))
            if 'video.mp4' in target_assets:
                target_spec['media'] = {'video':'video.mp4','cover':'cover.jpg'}
            else:
                originals = spec.get('media',{}).get('images',[])
                target_spec['media'] = {'images':[{'path':name,'alt':originals[index]['alt']} for index,name in enumerate(target_assets)]}
            state = {'steps':{}}
            for checkpoint in store.db.collection('specs/' + slug + '/checkpoints').stream():
                step = checkpoint.to_dict()
                state['steps'][step['name']] = {'status':step['state'],'value':step.get('value')}
            if target == 'blog':
                from blog_commit import GitHub
                github = GitHub(config)
                result = github.publish(target_spec,state,directory)
                verified = github.deployed(spec,result,state)
            elif target.startswith('tg-'):
                result = platforms.post_telegram(target_spec,state,target)
                verified = platforms.link_opens(result)
            else:
                provider = 'instagram' if target.startswith('ig-') else 'threads'
                session = platforms.social_session(provider)
                urls = {name.removesuffix('.mp4').removesuffix('.jpg'):media.signed(bucket,manifest['object'],config,generation=manifest['generation']) for name,manifest in target_assets.items()}
                for name,url in urls.items():
                    path = directory/(name + ('.mp4' if name == 'video' else '.jpg'))
                    manifest = target_assets[path.name]
                    platforms.probe(url,path,manifest.get('content_type') or mimetypes.guess_type(manifest['object'])[0])
                result = platforms.post_social(target_spec,state,target,urls,session)
                verified = platforms.link_opens(result)
            publication = state['steps'].get(target + ':publish',{}).get('value')
            published = {'url':result,'verified':verified,'copy':spec.get('copy',{}).get(target)}
            published.update(published_ids(config,spec,target,target_assets,publication))
            saved['results'][target] = published
            published_targets[target] = published
            saved['errors'].pop(target,None)
        except platforms.ExpiredMaterial:
            raise
        except Exception as failure:
            saved['errors'][target] = str(failure) if isinstance(failure,(Error,StateError)) else 'Target failed; reconcile saved checkpoints'
        publication = state['steps'].get(target + ':publish',{})
        if target not in published_targets and publication.get('status') == 'done':
            published_targets[target] = {'url':'','verified':False,'copy':spec.get('copy',{}).get(target),**published_ids(config,spec,target,target_assets,publication.get('value'))}
        store.change('specs/' + slug,lambda value:value.update(results=saved['results'],errors=saved['errors'],published=published_targets))
        store.change('items/' + saved['item'],lambda value:value.update(published=published_targets))
    state = 'done' if len(saved['results']) == len(spec['targets']) else 'partial'
    store.change('specs/' + slug,lambda value:value.update(state=state,published_at=time.time()))
    lines = []
    for index,target in enumerate(spec['targets'],1):
        result = saved['results'].get(target)
        text = result['url'] if result and result['verified'] else 'опубликовано, ссылка ещё не проверена' if result else saved['errors'].get(target,'Ошибка')
        lines.append(f'{index}. {target}: {text}')
    if any(target.startswith('ig-') for target in spec['targets']):
        lines.append('Facebook: поделиться вручную из Instagram, если автоматическая публикация не появилась.')
    extra = {'reply_markup':buttons(slug,saved['hash'],True)} if state == 'partial' else {}
    message = tell(config,'\n'.join(lines),**extra)
    store.put('published-messages/' + str(message['message_id']),{'slug':slug})


def published_ids(config,spec,target,assets,publication):
    if target == 'blog':
        return {'path':'blog/' + spec['slug'] + '.html','blog':spec['blog']}
    if publication is None:
        return {}
    if target == 'tg-channel':
        messages = publication if isinstance(publication,list) else [publication]
        return {'chat_id':messages[0].get('chat',{}).get('id') or config['channels'][target],'message_ids':[m['message_id'] for m in messages],'caption':bool(spec.get('media',{}).get('images') or spec.get('media',{}).get('video'))}
    if target == 'tg-story':
        return {'id':publication['id'],'business_connection_id':platforms.key('telegram-business.json')['business_connection_id'],'assets':assets}
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
        required = 'path' if target == 'blog' else 'message_ids' if target == 'tg-channel' else 'id'
        if required not in published:
            name = target + ':publish'
            checkpoint = store.get('specs/' + saved['source'] + '/checkpoints/' + hashlib.sha256(name.encode()).hexdigest()) or {}
            published.update(published_ids(config,source['spec'],target,source.get('assets',{}).get(target,{}),checkpoint.get('value') if checkpoint.get('state') == 'done' else None))
        if target == 'threads':
            published.setdefault('assets',source.get('assets',{}).get(target,{}))
        if target != 'blog':
            published.setdefault('copy',source['spec']['copy'][target])
    store.change('specs/' + saved['source'],lambda value:value.update(published=published_targets))
    store.change('items/' + source['item'],lambda value:value.update(published=published_targets))
    before = copy.deepcopy(published_targets)
    spec = copy.deepcopy(source['spec'])
    spec['targets'] = [t for t in spec['targets'] if t in before and not before[t].get('deleted')]
    spec['copy'] = {t:before[t].get('copy') or spec['copy'][t] for t in spec['targets'] if t != 'blog'}
    if 'blog' in spec['targets']:
        spec['blog'] = before['blog'].get('blog',spec['blog'])
    if saved['operation'] == 'fix':
        def validate(value):
            if not isinstance(value,dict) or set(value) != {'copy','blog'}:
                return ['Return exactly copy and blog']
            if not isinstance(value['copy'],dict) or set(value['copy']) != set(spec['copy']):
                return ['Keep exactly the published copy targets']
            if 'blog' not in spec['targets'] and value['blog'] is not None:
                return ['No blog was published']
            candidate = {**spec,'copy':value['copy'],'blog':value['blog'] if value['blog'] is not None else spec.get('blog')}
            return drafter.validate(candidate,prepared=False,copy_only=True)
        corrected = drafter.ask(config,{'mode':'fix','published':{'copy':spec['copy'],'blog':spec.get('blog')},'instruction':saved['instruction']},[],validate)
        after = {**spec,'copy':corrected['copy']}
        if corrected['blog'] is not None:
            after['blog'] = corrected['blog']
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
            old = '\n'.join(key + ':\n' + str(value) for key,value in spec['blog'].items()) if target == 'blog' else spec['copy'][target]
            new = '\n'.join(key + ':\n' + str(value) for key,value in after['blog'].items()) if target == 'blog' else after['copy'][target]
            changes = [line for line in difflib.ndiff(old.splitlines(),new.splitlines()) if line.startswith(('- ','+ '))]
            if changes:
                warning = '\nбудет удалён и опубликован заново, лайки и комментарии пропадут' if target == 'threads' else '\nправь вручную' if target.startswith('ig-') else ''
                lines.append(label + warning + '\n' + '\n'.join(changes))
    text = '\n'.join(lines) or 'Изменений нет.'
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
    message = tell(config,'Удаление ' + source['spec']['slug'] if saved['operation'] == 'delete' else 'Правка ' + source['spec']['slug'],reply_markup=markup)
    store.change('specs/' + slug,lambda value:value.update(state='previewed',preview_message=message['message_id']))
    store.put('published-messages/' + str(message['message_id']),{'slug':saved['source']})


def apply_fix(config,store,bucket,slug,directory):
    saved = store.claim_run(slug)
    if not saved:
        return
    if not saved.get('keyboard_removed'):
        remove_keyboard(config,saved)
        store.change('specs/' + slug,lambda value:value.update(keyboard_removed=True))
    for target in saved['spec']['targets']:
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
            unchanged = saved['operation'] == 'fix' and (saved['spec']['blog'] == published.get('blog') if target == 'blog' else saved['spec']['copy'][target] == published.get('copy'))
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
                    elif target == 'blog':
                        item['blog'] = saved['spec']['blog']
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
            saved['errors'][target] = str(failure) if isinstance(failure,(Error,StateError)) else 'Ошибка; сверь контрольные точки'
        store.change('specs/' + slug,lambda value:value.update(results=saved['results'],errors=saved['errors']))
    state = 'done' if len(saved['results']) == len(saved['spec']['targets']) else 'partial'
    if fix_hash(saved) != saved['approved_hash']:
        state = 'rejected'
    store.change('specs/' + slug,lambda value:value.update(state=state))
    source = store.get('specs/' + saved['source'])
    published_targets = {**source.get('results',{}),**source.get('published',{})}
    deleted = saved['operation'] == 'delete' and state == 'done' and all(x.get('deleted') or platforms.manual_change(t,x,saved['spec'],'delete') for t,x in published_targets.items())
    store.change('specs/' + saved['source'],lambda value:value.update(deleted=deleted))
    store.change('items/' + saved['item'],lambda value:value.update(published=published_targets,deleted=deleted))
    lines = []
    for target in saved['spec']['targets']:
        label = 'Instagram' if target.startswith('ig-') else 'Threads' if target == 'threads' else target
        result = saved['results'].get(target,saved['errors'].get(target,'Ошибка'))
        status = result['status'] if isinstance(result,dict) else result
        url = published_targets[target].get('url','')
        if published_targets[target].get('awaiting_republish'):
            status += '; удалено, ожидает повторной публикации'
        lines.append(label + ': ' + status + ' ' + (url or 'ссылка не получена'))
    extra = {'reply_markup':buttons(slug,saved['hash'],True)} if state == 'partial' else {}
    message = tell(config,'\n'.join(lines),**extra)
    store.put('published-messages/' + str(message['message_id']),{'slug':saved['source']})


@tasks_fn.on_task_dispatched(region=REGION,service_account=RUNTIME_SERVICE_ACCOUNT,secrets=[AUTOPOST],retry_config=options.RetryConfig(max_attempts=1),rate_limits=options.RateLimits(max_concurrent_dispatches=1),memory=options.MemoryOption.GB_2,timeout_sec=540,min_instances=0,max_instances=1)
def worker(request):
    return handle_worker(request)


def handle_worker(request):
    config,store,bucket = runtime()
    payload = request.data
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        platforms.configure(config,store,directory)
        try:
            step = payload['step']
            if step == 'draft':
                draft(config,store,bucket,payload,directory)
            elif step == 'montage':
                plan_montage(config,store,bucket,payload,directory)
            elif step == 'prepared':
                prepared(config,store,bucket,payload)
            elif step == 'rendered':
                rendered(config,store,bucket,payload,directory)
            elif step == 'fix':
                fix(config,store,payload['slug'])
            elif step == 'apply-fix':
                apply_fix(config,store,bucket,payload['slug'],directory)
            elif step == 'run':
                run(config,store,bucket,payload['slug'],directory)
            elif step == 'upload-prompt':
                upload_prompt(config,store,bucket,payload)
            elif step == 'upload-complete':
                upload_complete(config,store,bucket,payload,directory)
            elif step in ('edit-prompt','remove-keyboard'):
                saved = store.get('specs/' + payload['slug'])
                remove_keyboard(config,saved)
                if step == 'edit-prompt' and saved['state'] == 'needs-edit':
                    message = tell(config,'Что поправить? Ответь на это сообщение.',reply_markup={'force_reply':True,'selective':True})
                    reply_id = str(message['message_id'])
                    store.put('edits/' + reply_id,{'slug':payload['slug']})
                    store.change('specs/' + payload['slug'],lambda value:value.update(edit_prompt=reply_id))
            store.flush(enqueue)
        except Exception as failure:
            logging.error(json.dumps({'code':'worker_failed','step':payload.get('step')}))
            if payload.get('item'):
                store.change('items/' + payload['item'],lambda value:value.update(status='failed'))
            if payload.get('step') in ('run','apply-fix') and payload.get('slug'):
                store.change('specs/' + payload['slug'],lambda value:value.update(state='failed' if isinstance(failure,platforms.ExpiredMaterial) else 'partial'))
            text = str(failure) if isinstance(failure,(Error,StateError)) else 'Операция остановлена. Материал и контрольные точки сохранены.'
            extra = {}
            if payload.get('step') in ('run','apply-fix') and payload.get('slug') and not isinstance(failure,platforms.ExpiredMaterial):
                saved = store.get('specs/' + payload['slug'])
                if saved and saved.get('hash'):
                    extra['reply_markup'] = buttons(payload['slug'],saved['hash'],True)
            tell(config,text,**extra)


def maintenance(config,store,bucket):
    info = platforms.telegram('getWebhookInfo')
    if info.get('url') != config['webhook_url'] or info.get('last_error_date'):
        platforms.telegram('setWebhook',{'url':config['webhook_url'],'secret_token':config['webhook_secret'],'allowed_updates':['message','callback_query','business_connection']})
    now = int(time.time())
    for provider,host,prefix in [('instagram','https://graph.instagram.com','ig'),('threads','https://graph.threads.net','th')]:
        token = store.get('tokens/' + provider) or config.get(provider,{})
        if not token:
            continue
        expiry = token.get('expires_at',0)
        try:
            if expiry - now < 30*86400 and now-token.get('refreshed_at',token.get('issued_at',0)) >= 86400:
                reply = platforms.request('GET',host + '/refresh_access_token',{'grant_type':prefix + '_refresh_token','access_token':token['access_token']})
                store.put('tokens/' + provider,{**token,'access_token':reply['access_token'],'expires_at':now + int(reply['expires_in']),'refreshed_at':now})
        except Error:
            if expiry - now <= 7*86400:
                tell(config,provider + ': токен нужно обновить вручную в течение 7 дней.')
    import urllib.request
    try:
        request = urllib.request.Request('https://api.github.com/repos/romacv/romacv',headers={'Authorization':'Bearer ' + config['github_pat'],'Accept':'application/vnd.github+json'})
        with urllib.request.urlopen(request,timeout=30) as response:
            expires = response.headers.get('github-authentication-token-expiration')
        if expires and datetime.fromisoformat(expires.replace('Z','+00:00'))-datetime.now(timezone.utc) < __import__('datetime').timedelta(days=14):
            tell(config,'GitHub PAT истекает в течение 14 дней.')
    except Exception:
        tell(config,'Не удалось проверить срок GitHub PAT.')
    for render_snapshot in store.db.collection('renders').stream():
        render_job = render_snapshot.to_dict()
        if render_job.get('state') in ('done','failed') and not render_job.get('delivered'):
            enqueue({'step':'prepared' if render_job.get('mode') == 'prepare' else 'rendered','item':render_job['item'],'revision':render_job['revision'],'render':render_snapshot.id},'render-recovery-' + render_snapshot.id + '-' + secrets.token_hex(8))
            store.change('renders/' + render_snapshot.id,lambda value:value.update(delivered=True))
    for snapshot in store.db.collection('specs').stream():
        saved = snapshot.to_dict()
        if saved.get('state') == 'rejected' and not saved.get('operation'):
            for prefix in ('inbox/' + saved['item'] + '/','media/' + snapshot.id + '/'):
                for blob in bucket.list_blobs(prefix=prefix):
                    blob.delete(if_generation_match=blob.generation)
    store.flush(enqueue)


def recover_running(config,store):
    for snapshot in store.db.collection('specs').where('state', '==', 'running').stream():
        def recover(value):
            if value.get('state') != 'running' or time.time() - value.get('started_at',time.time()) <= 600:
                return False
            value['state'] = 'partial'
            return value
        saved = store.change('specs/' + snapshot.id,recover)
        if saved:
            tell(config,'Публикация прервалась. Готовые цели сохранены; неопределённые исходы требуют сверки.',reply_markup=buttons(snapshot.id,saved['hash'],True))


@scheduler_fn.on_schedule(schedule='every 15 minutes',timezone='UTC',region=REGION,service_account=RUNTIME_SERVICE_ACCOUNT,secrets=[AUTOPOST],timeout_sec=540,min_instances=0,max_instances=1)
def daily(event):
    config,store,bucket = runtime()
    platforms.configure(config,store,Path('/tmp'))
    recover_running(config,store)
    store.flush(enqueue)
    local = datetime.now(ZoneInfo(config.get('timezone','UTC')))
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
