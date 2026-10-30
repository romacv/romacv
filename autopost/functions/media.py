import contextvars
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import hashlib
import json
import math
import mimetypes
from pathlib import Path
import re
import subprocess
import threading
import unicodedata
from datetime import timedelta
import urllib.request
from platforms import Error, ExpiredMaterial, telegram, digest_file
from store import imaged

from pillow_heif import register_heif_opener

register_heif_opener()


def signed(bucket, name, config, method='GET', minutes=60, generation=None, content_type='video/mp4'):
    import google.auth
    from google.auth.transport.requests import Request
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    credentials.refresh(Request())
    return bucket.blob(name).generate_signed_url(version='v4', expiration=timedelta(minutes=minutes),method=method,content_type=content_type if method == 'PUT' else None,service_account_email=config['runtime_service_account'], access_token=credentials.token, generation=generation, headers={'x-goog-if-generation-match':'0'} if method == 'PUT' else None)


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


def upload_type(attachment):
    return 'video/mp4' if attachment['kind'] == 'video' else attachment['mime_type']


def attachments(item):
    result = []
    for message in item['messages']:
        message = imaged(message)
        values = [('photo',message['photo'][-1])] if message.get('photo') else []
        values += [(kind,message[kind]) for kind in ('video','voice') if message.get(kind)]
        for kind, value in values:
            suffix = {'photo':'.jpg','video':'.mp4','voice':'.ogg'}[kind]
            result.append({'name':str(message['message_id']) + suffix,'kind':kind,**value})
    return result


INTAKE_WORKERS = 4


def intake(bucket, item_id, item, config, directory, progress=None):
    values = attachments(item)
    if sum(a.get('file_size',0) for a in values) > 1024*1024*1024:
        raise Error('Пакет превышает лимит 1 ГБ.')
    def take(attachment):
        path = directory / attachment['name']
        name = 'inbox/' + item_id + '/' + attachment['name']
        saved = item.get('uploads',{}).get(attachment['name'])
        if saved:
            if attachment['kind'] == 'photo':
                download(bucket,saved,path)
                if saved['size'] > 20*1024*1024:
                    shrink(path)
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
        if attachment['kind'] != 'photo':
            path.unlink(missing_ok=True)
        return {**attachment,'path':path,'manifest':saved}
    done = [0]
    lock = threading.Lock()
    def counted(attachment):
        value = take(attachment)
        with lock:
            done[0] += 1
            number = done[0]
        if progress:
            progress(number,len(values))
        return value
    with ThreadPoolExecutor(max_workers=INTAKE_WORKERS) as pool:
        futures = [pool.submit(contextvars.copy_context().run,counted,a) for a in values]
        try:
            return [future.result() for future in futures]
        except BaseException:
            pool.shutdown(wait=False,cancel_futures=True)
            raise


def shrink(path):
    from PIL import Image, ImageOps
    limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = 150_000_000
    try:
        with Image.open(path) as original:
            original.draft('RGB',(2560,2560))
            original.thumbnail((2560,2560))
            image = ImageOps.exif_transpose(original).convert('RGB')
    except Image.DecompressionBombError:
        raise Error('Картинка больше 300 Мп: уменьши её.') from None
    finally:
        Image.MAX_IMAGE_PIXELS = limit
    image.save(path,'JPEG',quality=90)


def subject_offset(image, side):
    from PIL import Image, ImageFilter
    horizontal = image.width > image.height
    small = image.convert('L')
    small.thumbnail((256,256))
    edges = small.filter(ImageFilter.FIND_EDGES)
    count = edges.width if horizontal else edges.height
    profile = list(edges.resize((count,1) if horizontal else (1,count),Image.BOX).tobytes())
    span = image.width if horizontal else image.height
    window = max(1,round(side/span*count))
    total = [0]
    for value in profile:
        total.append(total[-1]+value)
    middle = (count-window)/2
    best = max(range(count-window+1),key=lambda i:(total[i+window]-total[i])*(1-0.15*abs(i-middle)/max(1,middle)))
    return min(span-side,round(best/count*span))


def square_crop(image):
    side = min(image.size)
    if image.width == image.height:
        return image
    offset = subject_offset(image,side)
    box = (offset,0,offset+side,side) if image.width > image.height else (0,offset,side,offset+side)
    return image.crop(box)


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
            if crop:
                ratio = min(1,1440/min(image.size))
                if ratio < 1:
                    image = image.resize((round(image.width*ratio),round(image.height*ratio)),Image.LANCZOS)
                image = square_crop(image)
            else:
                image.thumbnail((1440,1800))
            if image.width < 320:
                image = image.resize((320,round(image.height*320/image.width)))
        clean = Image.new('RGB',image.size)
        clean.paste(image)
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        clean.save(destination,'JPEG',quality=90,icc_profile=profile)
    if destination.stat().st_size > 8_000_000:
        raise Error('Clean image exceeds 8 MB')

FONTS = Path(__file__).with_name('fonts')
STORY_SAFE = (80, 250, 1000, 1500)
STORY_FONT = 64
STORY_WIDTH = 872
STORY_PAD = 24
STORY_PLATE = 8
EMOJI_BASE = '[\U0001f000-\U0001faff☀-➿⌀-⏿]'
EMOJI_MARK = '[️\U0001f3fb-\U0001f3ff]*'
EMOJI_CLUSTER = re.compile('[0-9#*]\ufe0f?\u20e3|[\U0001f1e6-\U0001f1ff]{2}|' + EMOJI_BASE + EMOJI_MARK + '(?:‍' + EMOJI_BASE + EMOJI_MARK + ')*')
PIN = '\U0001f4cd'


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


def segments(text):
    result, last = [], 0
    def add(kind, value):
        if kind == 'text' and result and result[-1][0] == 'text':
            result[-1] = ('text',result[-1][1] + value)
        elif value:
            result.append((kind,value))
    for found in EMOJI_CLUSTER.finditer(text):
        add('text',text[last:found.start()])
        cluster = found.group()
        if cluster.rstrip('\ufe0f') == PIN:
            add('pin',cluster)
        elif has_emoji(cluster):
            add('emoji',cluster)
        else:
            add('text',cluster.replace('\ufe0f','').replace('\u200d',''))
        last = found.end()
    add('text',text[last:])
    return result


@lru_cache(maxsize=256)
def has_emoji(cluster):
    return emoji_strike(cluster).getbbox() is not None


def emoji_strike(cluster):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(str(FONTS/'NotoColorEmoji.ttf'),109)
    ascent, descent = font.getmetrics()
    strike = Image.new('RGBA',(round(font.getlength(cluster)),ascent+descent),(0,0,0,0))
    ImageDraw.Draw(strike).text((0,0),cluster,font=font,embedded_color=True)
    return strike


@lru_cache(maxsize=64)
def emoji_glyph(cluster, height):
    from PIL import Image
    strike = emoji_strike(cluster)
    return strike.resize((round(strike.width*height/strike.height),height),Image.LANCZOS)


def run_image(kind, value, size):
    if kind == 'pin':
        return pin_icon(round(size*0.62),(235,64,52,255),(255,255,255,255))
    return emoji_glyph(value,round(size*1.18))


def run_advance(kind, value, size):
    image = run_image(kind,value,size)
    return image.width + (round(size*0.12) if kind == 'pin' else 0)


def measure(text, font):
    return sum(font.getlength(value) if kind == 'text' else run_advance(kind,value,font.size) for kind, value in segments(text))


def draw_runs(layer, x, baseline, text, font, fill, outline=None):
    from PIL import ImageDraw
    draw = ImageDraw.Draw(layer)
    for kind, value in segments(text):
        if kind == 'text':
            draw.text((x,baseline),value,font=font,fill=fill,anchor='ls',**({'stroke_width':2,'stroke_fill':outline} if outline else {}))
            x += font.getlength(value)
            continue
        image = run_image(kind,value,font.size)
        top = baseline - image.height if kind == 'pin' else baseline - round(image.height*0.79)
        layer.alpha_composite(image,(round(x),round(top)))
        x += run_advance(kind,value,font.size)


def soft_shadow(layer, radius, alpha, color=(0,0,0), offset=6):
    from PIL import Image, ImageFilter
    margin = 3*radius + offset
    padded = Image.new('RGBA',(layer.width+2*margin,layer.height+2*margin),(0,0,0,0))
    padded.alpha_composite(layer,(margin,margin))
    mask = padded.split()[3].point(lambda value: value*alpha//255)
    shadow = Image.new('RGBA',padded.size,(*color,0))
    shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(radius)))
    image = Image.new('RGBA',padded.size,(0,0,0,0))
    image.alpha_composite(shadow,(0,offset))
    image.alpha_composite(padded)
    image.info['margin'] = margin
    return image


def place_pill(text):
    from PIL import Image, ImageDraw, ImageFont
    semibold = str(FONTS/'InterDisplay-SemiBold.otf')
    pin = pin_icon(34,(235,64,52,255),(255,255,255,255))
    padding, gap, height = 30, 14, 92
    width = measure(text,ImageFont.truetype(semibold,50))
    size = min(50,int(50*(900-2*padding-gap-pin.width)/max(1,width)))
    if size < round(50/76*44):
        raise Error('Place name cannot fit the story safe width')
    font = ImageFont.truetype(semibold,size)
    text_width = round(measure(text,font))
    full = (2*padding+pin.width+gap+text_width,height)
    shape = Image.new('RGBA',(full[0]*4,full[1]*4),(0,0,0,0))
    ImageDraw.Draw(shape).rounded_rectangle((0,0,full[0]*4-1,full[1]*4-1),radius=88,fill=(255,255,255,255))
    image = shape.resize(full,Image.LANCZOS)
    image.alpha_composite(pin,(padding,(height-pin.height)//2))
    ascent, descent = font.getmetrics()
    draw_runs(image,padding+pin.width+gap,height//2+(ascent-descent)//2,text,font,(20,20,20))
    return soft_shadow(image,18,90)


def wrap_balanced(text, font, limit=900, most=3):
    from itertools import combinations
    words = text.split()
    for count in range(1, most+1):
        best = None
        for cuts in combinations(range(1, len(words)), count-1):
            edges = (0, *cuts, len(words))
            lines = [' '.join(words[i:j]) for i, j in zip(edges, edges[1:])]
            widest = max(measure(line,font) for line in lines)
            if widest <= limit and (best is None or widest < best[0]):
                best = (widest, lines)
        if best:
            return best[1]
    return None


def is_light(region):
    from PIL import ImageStat
    return ImageStat.Stat(region.convert('L')).mean[0] > 140


def story_line(text, align, dark=False, limit=STORY_WIDTH):
    from PIL import Image, ImageFont
    bold = str(FONTS/'InterDisplay-Bold.otf')
    size = STORY_FONT
    font = ImageFont.truetype(bold,size)
    lines = wrap_balanced(text,font,limit,3)
    if not lines:
        raise Error('Story text cannot fit the safe width')
    pad, step = STORY_PAD, round(size*1.2)
    width = round(max(measure(line,font) for line in lines))
    layer = Image.new('RGBA',(width+2*pad,step*len(lines)+2*pad),(0,0,0,0))
    ascent = font.getmetrics()[0]
    for i, line in enumerate(lines):
        room = width - measure(line,font)
        x = pad + {'left':0,'center':room/2,'right':room}[align]
        draw_runs(layer,x,pad+step*i+ascent,line,font,(20,20,20) if dark else (255,255,255),(255,255,255,200) if dark else (0,0,0,150))
    if dark:
        return soft_shadow(layer,14,200,(255,255,255),0)
    return soft_shadow(layer,18,90)


def story_block(overlay, dark, text=None, place=True, limit=STORY_WIDTH):
    from PIL import Image
    align = 'center'
    parts = ([place_pill(overlay['place'])] if place and overlay.get('place') else []) + [story_line(overlay['text'] if text is None else text,align,dark,limit)]
    gap = 8
    margin = max(part.info['margin'] for part in parts)
    width = max(part.width - 2*part.info['margin'] for part in parts) + 2*margin
    tops, y = [], margin
    for part in parts:
        tops.append(y - part.info['margin'])
        y += part.height - 2*part.info['margin'] + gap
    height = y - gap + margin
    block = Image.new('RGBA',(width,height),(0,0,0,0))
    inner_max = width - 2*margin
    for part, top in zip(parts,tops):
        inner = part.width - 2*part.info['margin']
        x = margin + {'left':0,'center':(inner_max-inner)//2,'right':inner_max-inner}[align] - part.info['margin']
        block.alpha_composite(part,(x,top))
    text = parts[-1]
    box = (x + text.info['margin'], tops[-1] + text.info['margin'], x + text.width - text.info['margin'], tops[-1] + text.height - text.info['margin'])
    block.info['margin'] = margin
    glyph = (box[0]+STORY_PAD,box[1]+STORY_PAD,box[2]-STORY_PAD,box[3]-STORY_PAD)
    plate = (glyph[0]-24,glyph[1]-STORY_PLATE,glyph[2]+24,glyph[3]+STORY_PLATE)
    shapes = [plate]
    if len(parts) > 1:
        pill = parts[0]
        pill_left = margin + (inner_max-(pill.width-2*pill.info['margin']))//2
        shapes.append((pill_left,margin,pill_left+pill.width-2*pill.info['margin'],margin+pill.height-2*pill.info['margin']))
    foot = (min(r[0] for r in shapes),min(r[1] for r in shapes),max(r[2] for r in shapes),max(r[3] for r in shapes))
    return block, box, foot


def integral(cells, columns, rows):
    total = [[0]*(columns+1) for _ in range(rows+1)]
    for row in range(rows):
        for col in range(columns):
            total[row+1][col+1] = cells[row*columns+col] + total[row][col+1] + total[row+1][col] - total[row][col]
    return total


def edge_map(canvas):
    from PIL import Image, ImageFilter
    small = canvas.convert('L').filter(ImageFilter.FIND_EDGES).resize((108,192),Image.BOX)
    return integral(list(small.tobytes()),108,192)


KEEP_COLUMNS, KEEP_ROWS, KEEP_CELL = 54, 96, 20
HEAD_ROOM = 1


def main_blobs(grown):
    seen, blobs = set(), []
    for start, found in enumerate(grown):
        if found and start not in seen:
            seen.add(start)
            cells, pending = [], [start]
            while pending:
                i = pending.pop()
                cells.append(i)
                x, y = i % KEEP_COLUMNS, i//KEEP_COLUMNS
                for n, ok in ((i+1,x+1 < KEEP_COLUMNS),(i-1,x > 0),(i+KEEP_COLUMNS,y+1 < KEEP_ROWS),(i-KEEP_COLUMNS,y > 0)):
                    if ok and grown[n] and n not in seen:
                        seen.add(n)
                        pending.append(n)
            blobs.append(cells)
    biggest = max((len(b) for b in blobs),default=0)
    kept = [False]*len(grown)
    for cells in blobs:
        if len(cells)*10 >= biggest:
            for i in cells:
                kept[i] = True
    return kept


def subject_keepout(canvas):
    from PIL import Image, ImageFilter
    small = canvas.convert('RGB').resize((KEEP_COLUMNS,KEEP_ROWS),Image.BOX)
    hsv = small.convert('HSV')
    saturation, value = hsv.getchannel(1).tobytes(), hsv.getchannel(2).tobytes()
    fine = small.filter(ImageFilter.GaussianBlur(0.7)).tobytes()
    surround = small.filter(ImageFilter.GaussianBlur(9)).tobytes()
    contrast = []
    for i in range(KEEP_COLUMNS*KEEP_ROWS):
        x, y = i % KEEP_COLUMNS/KEEP_COLUMNS - 0.5, i//KEEP_COLUMNS/KEEP_ROWS - 0.55
        centre = math.exp(-(x*x/0.09 + y*y/0.12))
        contrast.append(sum(abs(fine[3*i+k]-surround[3*i+k]) for k in range(3))*centre)
    cut = sorted(contrast)[int(len(contrast)*0.8)]
    marked = [saturation[i] > 165 and value[i] > 120 or cut > 0 and contrast[i] >= cut for i in range(len(contrast))]
    grown = marked[:]
    for i, found in enumerate(marked):
        if found:
            x, y = i % KEEP_COLUMNS, i//KEEP_COLUMNS
            for ny in range(max(0,y-HEAD_ROOM),min(KEEP_ROWS,y+2)):
                for nx in range(max(0,x-1),min(KEEP_COLUMNS,x+2)):
                    grown[ny*KEEP_COLUMNS+nx] = True
    return integral([int(v) for v in main_blobs(grown)],KEEP_COLUMNS,KEEP_ROWS)


def box_sum(total, scale, left, top, width, height):
    columns, rows = len(total[0])-1, len(total)-1
    c0, c1 = max(0,left//scale), min(columns,-(-(left+width)//scale))
    r0, r1 = max(0,top//scale), min(rows,-(-(top+height)//scale))
    if c1 <= c0 or r1 <= r0:
        return 0, 1
    return total[r1][c1]-total[r0][c1]-total[r1][c0]+total[r0][c0], (r1-r0)*(c1-c0)


def subject_rows(keeps, low, high):
    rows = [row*KEEP_CELL for _, keep in keeps for row in range(KEEP_ROWS) if box_sum(keep,KEEP_CELL,0,row*KEEP_CELL,1080,KEEP_CELL)[0]]
    return (min(rows),max(rows)+KEEP_CELL) if rows else (high,high)


def free_slot(frames, layouts, low, high):
    top_edge, bottom_edge = subject_rows(frames,low,high)
    def options(width, height):
        left = (1080-width)//2
        for top in range(low,high-height+1,10):
            density = 0
            for busy, keep in frames:
                if box_sum(keep,KEEP_CELL,left,top,width,height)[0]:
                    break
                cells, count = box_sum(busy,10,left,top,width,height)
                density = max(density,cells/count)
            else:
                yield 0 if top+height/2 <= (top_edge+bottom_edge)/2 else 2 if top >= bottom_edge else 1, density + 14*(top-low)/max(1,high-low), top
    for variant, (width, height) in enumerate(layouts):
        above = [(cost,top) for where, cost, top in options(width,height) if where == 0]
        if above:
            return variant, (1080-width)//2, min(above)[1]
    width, height = layouts[-1]
    rest = sorted(options(width,height))
    if rest:
        return len(layouts)-1, (1080-width)//2, rest[0][2]
    return None


def backdrop(canvas, box):
    from PIL import Image, ImageDraw, ImageFilter
    box = (max(0,box[0]),max(0,box[1]),min(1080,box[2]),min(1920,box[3]))
    blurred = canvas.crop(box).filter(ImageFilter.GaussianBlur(14))
    mask = Image.new('L',blurred.size,0)
    ImageDraw.Draw(mask).rounded_rectangle((0,0,blurred.width-1,blurred.height-1),radius=28,fill=200)
    canvas.paste(blurred,box[:2],mask.filter(ImageFilter.GaussianBlur(8)))


def story_variants(overlay):
    text, place = overlay['text'], overlay.get('place') or None
    bare = re.sub(r'\s+',' ',re.sub(r'\s*@[A-Za-z0-9._]{2,30}','',text)).strip(' -–—,:;')
    cut = re.split(r'\s[-–—]\s|[,;:.!?]\s',bare,1)[0].strip()
    words = bare.split()
    short = cut if cut and cut != bare else ' '.join(words[:max(1,len(words)//2)])
    variants = [(text,place),(bare,place),(bare,None),(short,None)]
    return [v for i, v in enumerate(variants) if v[0] and v not in variants[:i]]


def compose_story(source, destination, overlay):
    from PIL import Image, ImageOps, ImageCms
    source, destination = Path(source), Path(destination)
    with Image.open(source) as original:
        photo = ImageOps.exif_transpose(original).convert('RGB')
    scale = max(1080/photo.width,1920/photo.height)
    left = (photo.width*scale-1080)/2
    canvas = ImageOps.fit(photo,(1080,1920)).convert('RGBA')
    frames = [(edge_map(canvas),subject_keepout(canvas))]
    def place(dark):
        blocks = []
        for text, pill in story_variants(overlay):
            try:
                blocks.append(story_block(overlay,dark,text,pill))
            except Error:
                pass
        if not blocks:
            raise Error('Story text cannot fit the safe width')
        layouts = [(foot[2]-foot[0],foot[3]-foot[1]) for _, _, foot in blocks]
        found = free_slot(frames,layouts,STORY_SAFE[1],STORY_SAFE[3])
        if not found:
            raise Error('No free space for the story text')
        variant, left, top = found
        block, box, foot = blocks[variant]
        return block, box, left - foot[0], top - foot[1], foot
    block, box, x, y, foot = place(False)
    if is_light(canvas.crop((x+box[0],y+box[1],x+box[2],y+box[3]))):
        block, box, x, y, foot = place(True)
    backdrop(canvas,(x+box[0],y+box[1]+STORY_PAD-STORY_PLATE,x+box[2],y+box[3]-STORY_PAD+STORY_PLATE))
    canvas.alpha_composite(block,(x,y))
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    canvas.convert('RGB').save(destination,'JPEG',quality=90,icc_profile=profile)
    if destination.stat().st_size > 8_000_000:
        raise Error('Story image exceeds 8 MB')
    return x+foot[0], y+foot[1], x+foot[2], y+foot[3]


def speech_hints(*texts):
    words = []
    for text in (unicodedata.normalize('NFC',re.sub(r'https?://\S+|www\.\S+','',t)) for t in texts if isinstance(t,str)):
        run, end = [], 0
        def flush():
            if len(run) > 1:
                add(' '.join(run))
            run.clear()
        def add(value):
            value = value[:100].strip()
            if value and value.lower() not in (w.lower() for w in words):
                words.append(value)
        for found in re.finditer(r"[A-Za-zА-Яа-яЁё0-9][\w'’-]{2,}",text):
            word = found.group()
            starts = not re.search(r'[^\s"«(]',text[:found.start()]) or re.search(r'[.!?…\n]["»)]*\s*$',text[:found.start()])
            if re.search(r'[A-Za-z]',word) or word[0].isupper() and not starts:
                if run and not re.fullmatch(r'[ \t]+',text[end:found.start()]):
                    flush()
                run.append(word)
                end = found.end()
                add(word)
            else:
                flush()
        flush()
    return words[:100]


def transcribe(bucket, store, path, source_id, config, hints=()):
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
    phrases = speech_hints(*hints,*config.get('speech_phrases',()))
    client = speech.SpeechClient()
    operation = client.long_running_recognize(config=speech.RecognitionConfig(encoding=speech.RecognitionConfig.AudioEncoding.FLAC,sample_rate_hertz=16000,language_code=language,alternative_language_codes=['en-US' if language == 'ru-RU' else 'ru-RU'],enable_word_time_offsets=True,enable_automatic_punctuation=True,speech_contexts=[speech.SpeechContext(phrases=phrases,boost=15.0)] if phrases else []),audio=speech.RecognitionAudio(uri='gs://' + bucket.name + '/' + name))
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
