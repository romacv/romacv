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


def clean_image(source, destination, story=False, crop=False):
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageCms
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert('RGB')
        if story:
            background = ImageEnhance.Brightness(ImageOps.fit(image,(1080,1920)).filter(ImageFilter.GaussianBlur(40))).enhance(0.6)
            front = ImageOps.contain(image,(1080,1920))
            background.paste(front,((1080-front.width)//2,(1920-front.height)//2))
            image = background
        else:
            image.thumbnail((1440,1800))
            if crop:
                if image.width / image.height < 0.8:
                    height = math.floor(image.width/0.8)
                    top = (image.height-height)//2
                    image = image.crop((0,top,image.width,top+height))
                elif image.width / image.height > 1.91:
                    width = math.floor(image.height*1.91)
                    left = (image.width-width)//2
                    image = image.crop((left,0,left+width,image.height))
            if image.width < 320:
                image = image.resize((320,round(image.height*320/image.width)))
        clean = Image.new('RGB',image.size)
        clean.paste(image)
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        clean.save(destination,'JPEG',quality=90,icc_profile=profile)
    if destination.stat().st_size > 8_000_000:
        raise Error('Clean image exceeds 8 MB')

FONTS = Path(__file__).with_name('fonts')
STORY_SAFE = (60, 250, 1020, 1500)


def pin_icon(size, color, hole):
    from PIL import Image, ImageDraw
    big = size*4
    image = Image.new('RGBA',(big,int(big*1.3)),(0,0,0,0))
    draw = ImageDraw.Draw(image)
    radius = big//2
    draw.ellipse((0,0,big,big),fill=color)
    draw.polygon([(big*0.12,radius*1.45),(big*0.88,radius*1.45),(radius,int(big*1.3))],fill=color)
    inner = big*0.19
    draw.ellipse((radius-inner,radius-inner,radius+inner,radius+inner),fill=hole)
    return image.resize((size,int(size*1.3)),Image.LANCZOS)


def soft_shadow(layer, radius, alpha, color=(0,0,0), offset=6):
    from PIL import Image, ImageFilter
    mask = layer.split()[3].point(lambda value: value*alpha//255)
    shadow = Image.new('RGBA',layer.size,(*color,0))
    shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(radius)))
    image = Image.new('RGBA',layer.size,(0,0,0,0))
    image.alpha_composite(shadow,(0,offset))
    image.alpha_composite(layer)
    return image


def place_pill(text):
    from PIL import Image, ImageDraw, ImageFont
    semibold = str(FONTS/'InterDisplay-SemiBold.otf')
    pin = pin_icon(34,(235,64,52,255),(255,255,255,255))
    padding, gap, height = 30, 14, 92
    width = ImageFont.truetype(semibold,50).getlength(text)
    size = min(50,int(50*(900-2*padding-gap-pin.width)/max(1,width)))
    if size < round(50/76*44):
        raise Error('Place name cannot fit the story safe width')
    font = ImageFont.truetype(semibold,size)
    text_width = round(font.getlength(text))
    image = Image.new('RGBA',(2*padding+pin.width+gap+text_width,height),(0,0,0,0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0,0,image.width-1,height-1),radius=22,fill=(255,255,255,255))
    image.alpha_composite(pin,(padding,(height-pin.height)//2))
    draw.text((padding+pin.width+gap,height//2),text,font=font,fill=(20,20,20),anchor='lm')
    return soft_shadow(image,18,90)


def wrap_balanced(text, font, limit=900):
    from itertools import combinations
    words = text.split()
    for count in (1, 2, 3):
        best = None
        for cuts in combinations(range(1, len(words)), count-1):
            edges = (0, *cuts, len(words))
            lines = [' '.join(words[i:j]) for i, j in zip(edges, edges[1:])]
            widest = max(font.getlength(line) for line in lines)
            if widest <= limit and (best is None or widest < best[0]):
                best = (widest, lines)
        if best:
            return best[1]
    return None


def is_light(region):
    from PIL import ImageStat
    return ImageStat.Stat(region.convert('L')).mean[0] > 140


def story_line(text, align, dark=False):
    from PIL import Image, ImageDraw, ImageFont
    bold = str(FONTS/'InterDisplay-Bold.otf')
    size = 64
    while True:
        font = ImageFont.truetype(bold,size)
        lines = wrap_balanced(text,font)
        if lines:
            break
        size -= 4
        if size < 32:
            raise Error('Story text cannot fit the safe width')
    pad, step = 24, round(size*1.2)
    width = round(max(font.getlength(line) for line in lines))
    layer = Image.new('RGBA',(width+2*pad,step*len(lines)+2*pad),(0,0,0,0))
    draw = ImageDraw.Draw(layer)
    for i, line in enumerate(lines):
        x = {'left':pad,'center':pad+width/2,'right':pad+width}[align]
        draw.text((x,pad+step*i),line,font=font,fill=(20,20,20) if dark else (255,255,255),anchor={'left':'la','center':'ma','right':'ra'}[align])
    if dark:
        return soft_shadow(layer,14,200,(255,255,255),0)
    return soft_shadow(layer,18,90)


def story_block(overlay, dark):
    from PIL import Image
    align = overlay['align']
    parts = ([place_pill(overlay['place'])] if overlay.get('place') else []) + [story_line(overlay['text'],align,dark)]
    gap = 8
    width = max(part.width for part in parts)
    height = sum(part.height for part in parts) + gap*(len(parts)-1)
    block = Image.new('RGBA',(width,height),(0,0,0,0))
    y = 0
    for part in parts:
        x = {'left':0,'center':(width-part.width)//2,'right':width-part.width}[align]
        block.alpha_composite(part,(x,y))
        y += part.height + gap
    return block, height - parts[-1].height, parts[-1]


def compose_story(source, destination, overlay):
    from PIL import Image, ImageOps, ImageCms
    source, destination = Path(source), Path(destination)
    with Image.open(source) as original:
        photo = ImageOps.exif_transpose(original).convert('RGB')
    scale = max(1080/photo.width,1920/photo.height)
    left, top = (photo.width*scale-1080)/2, (photo.height*scale-1920)/2
    canvas = ImageOps.fit(photo,(1080,1920)).convert('RGBA')
    align = overlay['align']
    block, text_top, text = story_block(overlay,False)
    width, height = block.size
    ax = overlay['anchor'][0]*photo.width*scale - left
    ay = overlay['anchor'][1]*photo.height*scale - top
    x = ax - {'left':0,'center':width/2,'right':width}[align]
    x = round(min(max(x,STORY_SAFE[0]),STORY_SAFE[2]-width))
    y = round(min(max(ay-height/2,STORY_SAFE[1]),STORY_SAFE[3]-height))
    text_x = x + {'left':0,'center':(width-text.width)//2,'right':width-text.width}[align]
    if is_light(canvas.crop((text_x,y+text_top,text_x+text.width,y+height))):
        block = story_block(overlay,True)[0]
    canvas.alpha_composite(block,(x,y))
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    canvas.convert('RGB').save(destination,'JPEG',quality=90,icc_profile=profile)
    if destination.stat().st_size > 8_000_000:
        raise Error('Story image exceeds 8 MB')
    return x, y, x+width, y+height


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
