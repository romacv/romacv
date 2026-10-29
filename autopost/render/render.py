import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

from google.cloud import storage, firestore, tasks_v2


def run(arguments, seconds=None, span=(0,1)):
    command = ['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y',*(['-progress','pipe:1','-nostats'] if seconds else []),*map(str,arguments)]
    if not seconds:
        if subprocess.run(command,capture_output=True,timeout=1800).returncode:
            raise RuntimeError('Render failed')
        return
    process = subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    timer = threading.Timer(1800,process.kill)
    timer.start()
    try:
        for line in process.stdout:
            name, _, value = line.strip().partition('=')
            if name == 'out_time_us' and value.isdigit():
                progress(span[0] + (span[1] - span[0])*min(1,int(value)/1e6/max(seconds,0.001)))
        code = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
        timer.cancel()
    if code:
        raise RuntimeError('Render failed')


def bar(fraction, width=10):
    filled = round(max(0,min(1,fraction))*width)
    return '▰'*filled + '▱'*(width-filled) + f' {round(max(0,min(1,fraction))*100)}%'


def ass_time(seconds):
    centiseconds = round(seconds*100)
    return f'{centiseconds//360000}:{centiseconds//6000%60:02}:{centiseconds//100%60:02}.{centiseconds%100:02}'


TITLE_MARGIN = 60
TITLE_SHRINK_FLOOR = 44


def bold_font(size):
    from PIL import ImageFont
    return ImageFont.truetype(str(Path(__file__).parent / 'fonts' / 'InterDisplay-Bold.otf'),size)


def tracked_width(text, font, tracking):
    return sum(font.getlength(character) for character in text) + tracking*(len(text)-1)


def draw_tracked(draw, origin, text, font, tracking, fill):
    x, y = origin
    for character in text:
        draw.text((x,y),character,font=font,fill=fill)
        x += font.getlength(character) + tracking


def title_parts(text):
    name, _, city = text.rpartition(',')
    return (name.strip(),city.strip().upper()) if name.strip() else (city.strip(),'')


def title_image(text, path):
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    name, city = title_parts(text)
    size = 76
    while size > TITLE_SHRINK_FLOOR and tracked_width(name,bold_font(size),-1) > 780:
        size -= 2
    lines = [name] if tracked_width(name,bold_font(size),-1) <= 780 else wrap_lines(name,bold_font(size),780)[:2]
    line_height = round(size*1.1)
    kicker = ImageFont.truetype(str(Path(__file__).parent / 'fonts' / 'InterDisplay-SemiBold.otf'),28)
    name_top = 66 if city else 0
    margin = TITLE_MARGIN
    canvas = (round(max(max(tracked_width(line,bold_font(size),-1) for line in lines),tracked_width(city,kicker,8) if city else 0))+2*margin,name_top+(len(lines)-1)*line_height+round(size*1.3)+2*margin)
    shade = Image.new('RGBA',canvas,(0,0,0,0))
    front = Image.new('RGBA',canvas,(0,0,0,0))
    for layer, kicker_fill, name_fill, offset in ((shade,(0,0,0,140),(0,0,0,150),(2,3)),(front,(255,255,255,210),(255,255,255,255),(0,0))):
        draw = ImageDraw.Draw(layer)
        if city:
            draw_tracked(draw,(margin+offset[0],margin+offset[1]),city,kicker,8,kicker_fill)
            draw.rectangle((margin+offset[0],margin+48+offset[1],margin+40+offset[0],margin+50+offset[1]),fill=kicker_fill if layer is shade else (255,255,255,255))
        for index, line in enumerate(lines):
            draw_tracked(draw,(margin+offset[0],margin+name_top+index*line_height+offset[1]),line,bold_font(size),-1,name_fill)
    image = Image.new('RGBA',canvas,(0,0,0,0))
    image.alpha_composite(shade.filter(ImageFilter.GaussianBlur(12)))
    image.alpha_composite(front)
    image.save(path)


def title_position(frame):
    scale = frame[0]/1080
    return round((150-TITLE_MARGIN)*scale), round((330-TITLE_MARGIN)*scale)


def emptiest_title_y(frame, x, size, scale):
    from PIL import Image, ImageFilter
    cell = max(1,round(10*scale))
    with Image.open(frame) as image:
        small = image.convert('L').filter(ImageFilter.FIND_EDGES).resize((image.width//cell,image.height//cell),Image.BOX)
    columns, rows = small.size
    data = small.tobytes()
    low, high = round((300-TITLE_MARGIN)*scale), round(1250*scale) - size[1]
    c0, c1 = max(0,x//cell), min(columns,-(-(x+size[0])//cell))
    span = max(1,-(-size[1]//cell))
    best = (None,low)
    for top in range(low,max(low,high)+1,cell):
        r0 = top//cell
        r1 = min(rows,r0+span)
        values = [data[r*columns+c] for r in range(r0,r1) for c in range(c0,c1)]
        cost = sum(values)/max(1,len(values)) + 14*(top-low)/max(1,high-low)
        if best[0] is None or cost < best[0]:
            best = (cost,top)
    return best[1]


def edge_energy(image):
    from PIL import ImageFilter
    return image.convert('L').filter(ImageFilter.FIND_EDGES)


def sharpest_frame(sources, clips, time, root):
    from PIL import Image, ImageStat
    start = 0
    for clip in clips:
        span = clip['out'] - clip['in']
        if time < start + span or clip is clips[-1]:
            break
        start += span
    origin = clip['in'] + min(max(0,time-start),span)
    best, score = None, -1
    for index in range(6):
        moment = max(clip['in'],min(clip['out']-1/30,origin-0.5+index*0.2))
        candidate = root / f'cover-candidate-{index}.jpg'
        try:
            run(['-ss',moment,'-i',sources[clip['source']],'-frames:v','1','-map_metadata','-1',candidate])
            with Image.open(candidate) as image:
                sharpness = ImageStat.Stat(edge_energy(image.resize((270,round(270*image.height/image.width))))).var[0]
        except (RuntimeError, OSError):
            continue
        if sharpness > score:
            best, score = candidate, sharpness
    if best:
        return best
    plain = root / 'cover-candidate-plain.jpg'
    try:
        run(['-ss',origin,'-i',sources[clip['source']],'-frames:v','1','-map_metadata','-1',plain])
        Image.open(plain).close()
    except (RuntimeError, OSError):
        raise RuntimeError('Cover frame unavailable')
    return plain


def cover_blur(image, width, height):
    from PIL import Image, ImageFilter
    back = cover_crop(image,width,height).filter(ImageFilter.GaussianBlur(20*width/1080))
    ratio = min(width/image.width,height/image.height)
    front = image.resize((round(image.width*ratio),round(image.height*ratio)),Image.LANCZOS)
    back.paste(front,((width-front.width)//2,(height-front.height)//2))
    return back


def cover_crop(image, width, height):
    from PIL import Image
    wide = image.width*height > image.height*width
    scaled = image.resize((round(image.width*height/image.height),height) if wide else (width,round(image.height*width/image.width)),Image.LANCZOS)
    window = width if wide else height
    energy = edge_energy(scaled).resize((scaled.width,1) if wide else (1,scaled.height),Image.BOX)
    values = list(energy.getdata())
    total = sum(values[:window])
    best, offset = total, 0
    for index in range(1,len(values)-window+1):
        total += values[index+window-1] - values[index-1]
        if total > best:
            best, offset = total, index
    return scaled.crop((offset,0,offset+window,height) if wide else (0,offset,width,offset+window))


def wrap_lines(text, font, limit):
    lines, current = [], ''
    for word in text.split():
        while font.getlength(word) > limit:
            cut = max(1,next((index for index in range(1,len(word)+1) if font.getlength(word[:index]) > limit),len(word))-1)
            if current:
                lines.append(current)
                current = ''
            lines.append(word[:cut])
            word = word[cut:]
        trial = (current + ' ' + word).strip()
        if current and font.getlength(trial) > limit:
            lines.append(current)
            current = word
        else:
            current = trial
    return lines + ([current] if current else [])


def cover_headline(image, text):
    from PIL import Image, ImageDraw, ImageFilter
    scale = image.width/1080
    pad = round(34*scale)
    limit = 780*scale - 2*pad
    size = 80
    while True:
        font = bold_font(round(size*scale))
        lines = wrap_lines(text,font,limit)
        if lines and len(lines) <= 2:
            break
        size -= 2
        if size < TITLE_SHRINK_FLOOR:
            print('Cover headline dropped: does not fit',flush=True)
            return image
    line_height = round(size*1.05*scale)
    top, bottom = font.getbbox('Ду')[1::2]
    width = round(max(font.getlength(line) for line in lines)) + 2*pad
    height = (len(lines)-1)*line_height + bottom - top + 2*pad
    radius = round(line_height*0.45)
    x = round(150*scale)
    y = round(max(1250,1500-height/scale)*scale)
    reach = round(80*scale)
    base = image.convert('RGB')
    region = base.crop((x-reach,y-reach,x+width+reach,y+height+reach)).filter(ImageFilter.GaussianBlur(40*scale))
    region = Image.blend(region.point(lambda value: value*8//10),Image.new('RGB',region.size,(8,10,14)),0.45)
    grow = 4
    mask = Image.new('L',(width*grow,height*grow),0)
    ImageDraw.Draw(mask).rounded_rectangle((0,0,width*grow-1,height*grow-1),radius=radius*grow,fill=255)
    mask = mask.resize((width,height),Image.LANCZOS)
    result = base.convert('RGBA')
    shadow = Image.new('RGBA',result.size,(0,0,0,0))
    ImageDraw.Draw(shadow).rounded_rectangle((x,y+round(10*scale),x+width,y+height+round(10*scale)),radius=radius,fill=(0,0,0,90))
    result.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(22*scale)))
    plate = region.crop((reach,reach,reach+width,reach+height)).convert('RGBA')
    plate.putalpha(mask)
    result.alpha_composite(plate,(x,y))
    ring = Image.new('RGBA',(width*grow,height*grow),(0,0,0,0))
    ImageDraw.Draw(ring).rounded_rectangle((0,0,width*grow-1,height*grow-1),radius=radius*grow,outline=(255,255,255,70),width=grow)
    result.alpha_composite(ring.resize((width,height),Image.LANCZOS),(x,y))
    draw = ImageDraw.Draw(result)
    for index, line in enumerate(lines):
        draw.text((x+pad,y+pad-top+index*line_height),line,font=font,fill=(255,255,255,255))
    return result.convert('RGB')


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def put(bucket, path, name):
    blob = bucket.blob(name)
    content_type = 'image/jpeg' if path.suffix == '.jpg' else 'audio/flac' if path.suffix == '.flac' else 'video/mp4'
    blob.upload_from_filename(str(path),content_type=content_type,if_generation_match=0)
    blob.reload()
    return {'object':name,'generation':str(blob.generation),'sha256':digest_file(path),'size':path.stat().st_size,'content_type':content_type}


def prepare(bucket,job,root):
    stage('Готовлю звук и кадры')
    from PIL import Image, ImageDraw
    outputs = {'sources':{},'durations':{},'audio':{},'sheets':[]}
    if sum(m['size'] for m in job['sources'].values()) > 1024*1024*1024:
        raise RuntimeError('Batch exceeds 1 GiB')
    count = len(job['sources'])
    for position, (name, manifest) in enumerate(job['sources'].items()):
        path = root / name
        bucket.blob(manifest['object'],generation=int(manifest['generation'])).download_to_filename(str(path))
        if digest_file(path) != manifest['sha256']:
            raise RuntimeError('Source hash changed')
        probe = subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)],capture_output=True,check=True)
        info = json.loads(probe.stdout)
        duration = float(info['format']['duration'])
        audio = any(stream['codec_type'] == 'audio' for stream in info['streams'])
        outputs['audio'][name] = {'duration':duration,'audio':audio}
        if audio:
            flac = root / (path.stem + '.flac')
            run(['-i',path,'-vn','-ac','1','-ar','16000','-map_metadata','-1',flac],duration,(position/count,(position+0.4)/count))
            outputs['audio'][name].update(put(bucket,flac,job['prefix'] + '/' + flac.name))
            flac.unlink()
        if path.suffix == '.mp4':
            if duration > 600:
                raise RuntimeError('Split clips longer than ten minutes')
            clean = root / ('clean-' + name)
            run(['-i',path,'-map','0:v:0','-map','0:a?','-c','copy','-map_metadata','-1','-map_chapters','-1',clean])
            outputs['sources'][path.stem] = put(bucket,clean,job['prefix'] + '/' + clean.name)
            outputs['durations'][path.stem] = duration
            clean.unlink()
            frames = root / path.stem
            frames.mkdir()
            run(['-i',path,'-vf','fps=1,scale=320:-2','-q:v','4',frames/'%06d.jpg'],duration,((position+0.4)/count,(position+0.9)/count))
            images = sorted(frames.glob('*.jpg'))
            for offset in range(0,len(images),16):
                sheet = Image.new('RGB',(1280,880),'black')
                draw = ImageDraw.Draw(sheet)
                for index, frame in enumerate(images[offset:offset+16]):
                    with Image.open(frame) as image:
                        image.thumbnail((320,190))
                        x,y = index%4*320,index//4*220
                        sheet.paste(image,(x,y))
                        draw.text((x+4,y+193),path.stem + ' ' + str(offset+index) + 's',fill='white')
                    frame.unlink()
                output = root / (path.stem + '-sheet-' + str(offset) + '.jpg')
                sheet.save(output,quality=80)
                outputs['sheets'].append(put(bucket,output,job['prefix'] + '/' + output.name))
                output.unlink()
                if len(outputs['sheets']) > 100:
                    raise RuntimeError('Too many frames; split batch')
        path.unlink()
        progress((position+1)/count)
    return outputs


def frame_size(draft):
    return (540,960) if draft else (1080,1920)


def clip_filters(fit, draft):
    width, height = frame_size(draft)
    if fit == 'blur':
        return f'split=2[bg][fg];[bg]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=30:10[back];[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[front];[back][front]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30'
    return f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=30'


def part_codec(draft):
    return ['-c:v','libx264','-preset','ultrafast','-crf','28'] if draft else ['-c:v','libx264','-preset','fast']


def audio_filter(draft):
    return 'adelay=33.333:all=1' if draft else 'adelay=33.333:all=1,loudnorm=I=-16:TP=-1.5:LRA=11'


def encode_arguments(target, draft):
    if draft:
        return ['-c:v','libx264','-preset','ultrafast','-crf','32','-pix_fmt','yuv420p','-r','30','-c:a','aac','-b:a','96k']
    video = ['-c:v','libx265','-tag:v','hvc1','-x265-params','keyint=30:min-keyint=30:scenecut=0','-b:v','2500k','-maxrate','3000k','-bufsize','6000k'] if target == 'tg-story' else ['-c:v','libx264','-preset','fast','-crf','22']
    return video + ['-pix_fmt','yuv420p','-r','30','-c:a','aac','-b:a','128k']


def timeline_groups(targets, draft):
    groups = {}
    for target, timeline in targets.items():
        key = json.dumps({name:timeline.get(name,[]) for name in ('clips','subtitles','titles')},sort_keys=True) if draft else target
        groups.setdefault(key,[]).append(target)
    return {names[0]:names for names in groups.values()}


def render(bucket, job, root):
    draft = job.get('mode') == 'draft'
    stage('Скачиваю материал')
    width, height = frame_size(draft)
    groups = timeline_groups(job['edl']['targets'],draft)
    leaders = {name:leader for leader,names in groups.items() for name in names}
    sources = {}
    referenced = {clip['source'] for timeline in job['edl']['targets'].values() for clip in timeline['clips']}
    for source in sorted(referenced):
        manifest = job['sources'][source]
        path = root / (source + '.mp4')
        bucket.blob(manifest['object'],generation=int(manifest['generation'])).download_to_filename(str(path))
        if digest_file(path) != manifest['sha256']:
            raise RuntimeError('Source hash changed')
        sources[source] = path
        progress(len(sources)/len(referenced))
    stage('Рендерю черновик' if draft else 'Собираю финал')
    outputs, joined_files = {}, {}
    length = {target:sum(clip['out']-clip['in'] for clip in job['edl']['targets'][target]['clips']) for target in groups}
    work, finished = max(0.001,2*sum(length.values())), 0
    for target in groups:
        timeline = job['edl']['targets'][target]
        parts = []
        for index, clip in enumerate(timeline['clips']):
            source = sources[clip['source']]
            part = root / f'{target}-{index}.mp4'
            filters = clip_filters(job['edl']['fit'],draft)
            probe = subprocess.run(['ffprobe','-v','error','-select_streams','a','-show_entries','stream=index','-of','json',str(source)],capture_output=True,check=True)
            audio = bool(json.loads(probe.stdout).get('streams'))
            arguments = ['-ss',clip['in'],'-t',clip['out']-clip['in'],'-i',source]
            if not audio:
                arguments += ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
            arguments += ['-filter_complex', filters + '[vout]', '-map','[vout]','-map','0:a:0' if audio else '1:a:0','-t',clip['out']-clip['in'],*part_codec(draft),'-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2','-map_metadata','-1',part]
            span = max(0.001,clip['out']-clip['in'])
            run(arguments,span,(finished/work,(finished+span)/work))
            finished += span
            parts.append(part)
        concat = root / (target + '.txt')
        concat.write_text(''.join("file '" + str(p) + "'\n" for p in parts))
        joined = root / (target + '-joined.mp4')
        run(['-f','concat','-safe','0','-i',concat,'-c','copy',joined])
        for part in parts:
            part.unlink()
        concat.unlink()
        joined_files[target] = joined
    cover = root / 'cover.jpg'
    settings = job['edl']['cover']
    from PIL import Image, ImageCms, ImageFont
    chosen = sharpest_frame(sources,job['edl']['targets'][settings['target']]['clips'],settings['time'],root)
    with Image.open(chosen) as image:
        clean = (cover_blur if job['edl']['fit'] == 'blur' else cover_crop)(image.convert('RGB'),width,height)
        if settings.get('text'):
            clean = cover_headline(clean,settings['text'])
        clean.save(cover,'JPEG',quality=90,icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',44)
    for target in groups:
        timeline = job['edl']['targets'][target]
        subtitles = root / (target + '.ass')
        header = Path('/app/subtitles.ass').read_text()
        lines = []
        for cue in timeline['subtitles']:
            text = '\\N'.join(cue['lines'])
            text_width = max(font.getlength(line) for line in cue['lines'])
            size = min(44,int(44*780/max(1,text_width)))
            if size < 20:
                raise RuntimeError('Subtitle cannot fit safe width')
            lines.append(f"Dialogue: 0,{ass_time(cue['start']+1/30)},{ass_time(cue['end']+1/30)},Default,,0,0,0,,{{\\pos(540,1050)\\fs{size}}}{text}\n")
        subtitles.write_text(header + ''.join(lines))
        output = root / (target + '.mp4')
        video_filter = "[0:v]tpad=start_duration=0.033333333:start_mode=clone[base];[base][1:v]overlay=enable='lt(t,0.033333333)':eof_action=repeat,ass=" + str(subtitles)
        title_inputs = []
        for index, cue in enumerate(timeline.get('titles',[])):
            image = root / f'{target}-title-{index}.png'
            title_image(cue['text'],image)
            if draft:
                with Image.open(image) as full:
                    full.resize((full.width//2,full.height//2)).save(image)
            x, y = title_position((width,height))
            grab = root / f'{target}-title-{index}.jpg'
            try:
                run(['-ss',f"{(cue['start']+cue['end'])/2:.3f}",'-i',joined_files[target],'-frames:v','1','-q:v','3',grab])
                with Image.open(image) as full:
                    y = emptiest_title_y(grab,x,full.size,width/1080)
            except (RuntimeError,OSError):
                pass
            grab.unlink(missing_ok=True)
            title_inputs += ['-i',image]
            video_filter += f"[t{index}];[t{index}][{index+2}:v]overlay=x={x}:y={y}:eof_action=repeat:enable='between(t,{cue['start']+1/30:.6f},{cue['end']+1/30:.6f})'"
        if target == 'tg-story' and not draft:
            video_filter += ',scale=720:1280'
        arguments = ['-i',joined_files[target],'-i',cover,*title_inputs,'-filter_complex',video_filter + '[vout]','-map','[vout]','-map','0:a:0','-af',audio_filter(draft),*encode_arguments(target,draft)]
        arguments += ['-movflags','+faststart','-map_metadata','-1','-map_chapters','-1','-metadata','encoder=','-metadata:s:v','encoder=',output]
        run(arguments,max(0.001,length[target]),(finished/work,(finished+length[target])/work))
        finished += length[target]
        joined_files[target].unlink()
        limit = min(30_000_000 if name == 'tg-story' else 50_000_000 for name in groups[target])
        if output.stat().st_size > limit:
            raise RuntimeError('Video exceeds preview upload limit')
        probe = subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(output)],capture_output=True,check=True)
        measured = json.loads(probe.stdout)
        duration = float(measured['format']['duration'])
        maximum = min(60 if name in ('ig-story','tg-story') else 90 for name in groups[target])
        if duration > maximum + 0.01:
            raise RuntimeError('Encoded duration exceeds target limit')
        manifest = put(bucket,output,job['prefix'] + '/' + output.name)
        for name in groups[target]:
            outputs[name] = manifest
    return {'videos':outputs,'cover':put(bucket,cover,job['prefix'] + '/cover.jpg')}


CLIP_LIMIT = 60
CLIP_FORMATS = {'square':50_000_000, 'channel':50_000_000, 'story-ig':50_000_000, 'story-tg':30_000_000}


def clip_arguments(source, fmt, output):
    seconds = min(CLIP_LIMIT, float(json.loads(subprocess.run(['ffprobe','-v','error','-show_format','-of','json',str(source)],capture_output=True,check=True).stdout)['format']['duration']))
    if fmt == 'square':
        graph = 'scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080,setsar=1,fps=30'
    elif fmt == 'channel':
        graph = 'scale=1280:1280:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,fps=30'
    else:
        graph = clip_filters('blur',False) + (',scale=720:1280' if fmt == 'story-tg' else '')
    codec = ['-c:v','libx265','-tag:v','hvc1','-x265-params','keyint=30:min-keyint=30:scenecut=0','-b:v','2500k','-maxrate','3000k','-bufsize','6000k'] if fmt == 'story-tg' else ['-c:v','libx264','-preset','fast','-crf','22','-maxrate','5M','-bufsize','10M','-g','30']
    audio = bool(json.loads(subprocess.run(['ffprobe','-v','error','-select_streams','a','-show_entries','stream=index','-of','json',str(source)],capture_output=True,check=True).stdout).get('streams'))
    silence = [] if audio else ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
    return ['-i',source,*silence,'-t',seconds,'-filter_complex',graph + '[vout]','-map','[vout]','-map','0:a:0' if audio else '1:a:0',*codec,'-pix_fmt','yuv420p','-r','30','-c:a','aac','-b:a','128k','-ar','48000','-ac','2','-movflags','+faststart','-map_metadata','-1','-map_chapters','-1','-metadata','encoder=','-metadata:s:v','encoder=',output]


def render_clips(bucket, job, root):
    stage('Режу клипы')
    outputs = {'clips':{}}
    units, made = max(1,len(job['sources'])*len(job['formats'])), 0
    for stem, manifest in job['sources'].items():
        source = root / (stem + '.mp4')
        bucket.blob(manifest['object'],generation=int(manifest['generation'])).download_to_filename(str(source))
        if digest_file(source) != manifest['sha256']:
            raise RuntimeError('Source hash changed')
        for fmt in job['formats']:
            output = root / (fmt + '-' + stem + '.mp4')
            run(clip_arguments(source,fmt,output),max(0.001,min(CLIP_LIMIT,float(job['durations'][stem]))),(made/units,(made+1)/units))
            made += 1
            if output.stat().st_size > CLIP_FORMATS[fmt]:
                raise RuntimeError('Clip exceeds upload limit')
            measured = json.loads(subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-select_streams','v:0','-of','json',str(output)],capture_output=True,check=True).stdout)
            seconds = float(measured['format']['duration'])
            outputs['clips'].setdefault(fmt,{})[stem] = {**put(bucket,output,job['prefix'] + '/' + output.name),'seconds':seconds,'width':measured['streams'][0]['width'],'height':measured['streams'][0]['height'],'trimmed':float(job['durations'][stem]) > CLIP_LIMIT + 0.01}
            output.unlink()
        source.unlink()
    if job.get('edl'):
        outputs.update(render(bucket,job,root))
    return outputs


CPU = 2
MEMORY_GIB = 2


class Loader:
    def __init__(self, job, reference):
        self.settings = job.get('loader') or {}
        self.token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.reference = reference
        self.label = ''
        self.percent = None
        mode = job.get('mode')
        self.step = {'prepare':1,'draft':3}.get(mode) if mode == 'draft' or mode == 'prepare' and job.get('montage') and not job.get('target') else None
        self.steps = []
        self.began = time.time()
        self.shown = None
        self.done = threading.Event()
        self.thread = None

    def call(self, method, fields):
        data = urllib.parse.urlencode({'chat_id':self.settings['chat_id'],**fields}).encode()
        urllib.request.urlopen('https://api.telegram.org/bot' + self.token + '/' + method,data=data,timeout=10).close()

    def text(self):
        seconds = int(time.time() - self.began)
        line = self.label + ': ' + f'{seconds//60}:{seconds%60:02}' + (f' · шаг {self.step} из 3' if self.step else '')
        return (self.settings.get('text','') + '\n' + line + ('\n' + bar(self.percent) if self.percent is not None else '')).strip()

    def progress(self, fraction):
        self.percent = fraction

    def edit(self, text):
        if self.settings.get('message_id') and text != self.shown:
            self.call('editMessageText',{'message_id':self.settings['message_id'],'text':text})
            self.shown = text

    def beat(self):
        ticks = 0
        while not self.done.wait(4):
            ticks += 1
            try:
                if self.done.is_set():
                    return
                self.call('sendChatAction',{'action':self.settings.get('action','upload_video')})
                if ticks % 2 == 0 and self.label and not self.done.is_set():
                    self.edit(self.text())
            except Exception:
                pass

    def close(self, now):
        if self.steps and 'ended' not in self.steps[-1]:
            step = self.steps[-1]
            seconds = max(0, now - step['since'])
            step.update(ended=now,seconds=round(seconds,1),vcpu_s=round(CPU*seconds,1),gib_s=round(MEMORY_GIB*seconds,1))

    def usage(self):
        return {'seconds':round(sum(step.get('seconds',0) for step in self.steps),1),'vcpu_s':round(sum(step.get('vcpu_s',0) for step in self.steps),1),'gib_s':round(sum(step.get('gib_s',0) for step in self.steps),1)}

    def finish(self):
        now = time.time()
        self.close(now)
        return {'steps':self.steps,'usage':self.usage(),'ended':now}

    def stage(self, label):
        self.label = label
        self.percent = None
        now = time.time()
        self.close(now)
        self.steps.append({'label':label,'since':now})
        try:
            self.reference.update({'progress':{'label':label,'since':self.began,**({'step':self.step,'total':3} if self.step else {})},'steps':self.steps,'started':self.began,'resources':{'cpu':CPU,'mem_gib':MEMORY_GIB},'usage':self.usage()})
        except Exception:
            pass
        try:
            if self.token and self.settings.get('chat_id'):
                self.edit(self.text())
        except Exception:
            pass

    def start(self):
        if self.token and self.settings.get('chat_id'):
            self.thread = threading.Thread(target=self.beat,daemon=True)
            self.thread.start()

    def stop(self, failed=False):
        self.done.set()
        if self.thread:
            self.thread.join(25)
        if failed:
            try:
                self.edit((self.settings.get('text','') + '\nМонтаж не удался.').strip())
            except Exception:
                pass


LOADER = None


def stage(label):
    if LOADER:
        LOADER.stage(label)


def progress(fraction):
    if LOADER:
        LOADER.progress(fraction)


def enqueue(job, success):
    client = tasks_v2.CloudTasksClient()
    payload = {'data':{'step':'prepared' if job.get('mode') == 'prepare' else 'rendered','item':job['item'],'revision':job['revision'],'render':job['id'],'success':success}}
    task = {'name':client.task_path(job['project'],'us-central1','worker','rendered-' + job['id'] + '-' + uuid.uuid4().hex),'http_request':{'http_method':tasks_v2.HttpMethod.POST,'url':job['worker_url'],'headers':{'Content-Type':'application/json'},'body':json.dumps(payload).encode(),'oidc_token':{'service_account_email':job['service_account'],'audience':job['worker_url']}}}
    client.create_task(parent=client.queue_path(job['project'],'us-central1','worker'),task=task)


def main():
    db = firestore.Client()
    identifier = os.environ['RENDER_ID']
    reference = db.document('renders/' + identifier)
    job = reference.get().to_dict()
    if job.get('state') != 'queued':
        return
    transaction = db.transaction()
    @firestore.transactional
    def claim(tx):
        current = reference.get(transaction=tx).to_dict()
        if current.get('state') != 'queued':
            return False
        tx.update(reference,{'state':'running'})
        return True
    if not claim(transaction):
        return
    global LOADER
    LOADER = Loader(job,reference)
    LOADER.start()
    success = False
    try:
        with tempfile.TemporaryDirectory() as directory:
            outputs = {'prepare':prepare,'clips':render_clips}.get(job.get('mode'),render)(storage.Client().bucket(job['bucket']),job,Path(directory))
        LOADER.stop()
        reference.update({'state':'done','outputs':outputs,**LOADER.finish()})
        success = True
    except Exception:
        LOADER.stop(True)
        reference.update({'state':'failed','error':'Render failed; inspect inputs before retry',**LOADER.finish()})
    enqueue(job,success)


if __name__ == '__main__':
    main()
