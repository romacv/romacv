import hashlib
import json
import math
import mimetypes
from pathlib import Path
import re
import subprocess
from datetime import timedelta
import urllib.request
from platforms import Error, ExpiredMaterial, telegram, digest_file


def signed(bucket, name, config, method='GET', minutes=60, generation=None):
    import google.auth
    from google.auth.transport.requests import Request
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    credentials.refresh(Request())
    return bucket.blob(name).generate_signed_url(version='v4', expiration=timedelta(minutes=minutes),method=method,content_type='video/mp4' if method == 'PUT' else None,service_account_email=config['runtime_service_account'], access_token=credentials.token, generation=generation, headers={'x-goog-if-generation-match':'0'} if method == 'PUT' else None)


def upload(bucket, path, name):
    blob = bucket.blob(name)
    from google.api_core.exceptions import PreconditionFailed
    digest = digest_file(path)
    blob.metadata = {'sha256':digest}
    try:
        blob.upload_from_filename(str(path),content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream',if_generation_match=0)
    except PreconditionFailed:
        blob.reload()
        if blob.size != path.stat().st_size or (blob.metadata or {}).get('sha256') != digest:
            raise Error('Existing object differs; never overwrite media')
    blob.reload()
    return {'object':name,'generation':str(blob.generation),'sha256':digest_file(path),'size':path.stat().st_size,'content_type':mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}


def download(bucket, manifest, destination):
    blob = bucket.blob(manifest['object'],generation=int(manifest['generation']))
    from google.api_core.exceptions import NotFound
    try:
        blob.download_to_filename(str(destination))
    except NotFound:
        raise ExpiredMaterial('Материал истёк. Пришли файлы заново.') from None
    if digest_file(destination) != manifest['sha256']:
        raise Error('Stored media hash mismatch')


def attachments(item):
    result = []
    for message in item['messages']:
        values = [('photo',message['photo'][-1])] if message.get('photo') else []
        values += [(kind,message[kind]) for kind in ('video','voice') if message.get(kind)]
        for kind, value in values:
            suffix = {'photo':'.jpg','video':'.mp4','voice':'.ogg'}[kind]
            result.append({'name':str(message['message_id']) + suffix,'kind':kind,**value})
    return result


def intake(bucket, item_id, item, config, directory):
    result = []
    values = attachments(item)
    if sum(a.get('file_size',0) for a in values) > 1024*1024*1024:
        raise Error('Пакет превышает лимит 1 ГБ.')
    for attachment in values:
        path = directory / attachment['name']
        name = 'inbox/' + item_id + '/' + attachment['name']
        saved = item.get('uploads',{}).get(attachment['name'])
        if saved:
            if attachment['kind'] == 'photo':
                download(bucket,saved,path)
        elif attachment.get('file_size',0) > 20*1024*1024:
            raise Error('Large media upload not complete')
        else:
            info = telegram('getFile',{'file_id':attachment['file_id']})
            remote = info['file_path']
            if '..' in Path(remote).parts or remote.startswith('/'):
                raise Error('Invalid Telegram file path')
            try:
                with urllib.request.urlopen('https://api.telegram.org/file/bot' + config['telegram_bot_token'] + '/' + remote,timeout=120) as response, path.open('wb') as output:
                    total = 0
                    while chunk := response.read(1024*1024):
                        total += len(chunk)
                        if total > 20*1024*1024:
                            raise Error('Telegram download limit exceeded')
                        output.write(chunk)
            except OSError:
                raise Error('Telegram download failed') from None
            saved = upload(bucket,path,name)
        result.append({**attachment,'path':path,'manifest':saved})
        if attachment['kind'] != 'photo':
            path.unlink(missing_ok=True)
    return result


def clean_image(source, destination, story=False):
    from PIL import Image, ImageOps, ImageCms
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert('RGB')
        if story:
            image = ImageOps.pad(image,(1080,1920),color='black')
        else:
            image.thumbnail((1440,1800))
            if image.width / image.height < 0.8:
                image = ImageOps.pad(image,(max(320,image.width),int(max(320,image.width)/0.8)),color='black')
            if image.width / image.height > 1.91:
                image = ImageOps.pad(image,(image.width,math.ceil(image.width/1.91)),color='black')
            if image.width < 320:
                image = image.resize((320,round(image.height*320/image.width)))
        clean = Image.new('RGB',image.size)
        clean.paste(image)
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        clean.save(destination,'JPEG',quality=90,icc_profile=profile)
    if destination.stat().st_size > 8_000_000:
        raise Error('Clean image exceeds 8 MB')


def transcribe(bucket, store, path, source_id, config):
    from google.cloud import speech
    info = config['prepared_audio'][path.name]
    if not info['audio']:
        return {'text':'','words':[]}
    seconds = math.ceil(info['duration'])
    store.reserve_speech(seconds, source_id)
    name = info['object']
    language = config.get('speech_language','ru-RU')
    if language not in ('ru-RU','en-US'):
        raise Error('Unsupported speech language')
    client = speech.SpeechClient()
    operation = client.long_running_recognize(config=speech.RecognitionConfig(encoding=speech.RecognitionConfig.AudioEncoding.FLAC,sample_rate_hertz=16000,language_code=language,alternative_language_codes=['en-US' if language == 'ru-RU' else 'ru-RU'],enable_word_time_offsets=True,enable_automatic_punctuation=True),audio=speech.RecognitionAudio(uri='gs://' + bucket.name + '/' + name))
    response = operation.result(timeout=240)
    text, words = [], []
    for result in response.results:
        best = result.alternatives[0]
        text.append(best.transcript)
        words.extend({'word':w.word,'start':w.start_time.total_seconds(),'end':w.end_time.total_seconds()} for w in best.words)
    return {'text':' '.join(text),'words':words}


def materialize(bucket, manifests, directory):
    for name, manifest in manifests.items():
        path = directory / name
        path.parent.mkdir(parents=True,exist_ok=True)
        download(bucket,manifest,path)
