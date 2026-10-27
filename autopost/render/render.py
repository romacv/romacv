import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from google.cloud import storage, firestore, tasks_v2


def run(arguments):
    result = subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y',*map(str,arguments)],capture_output=True,timeout=1800)
    if result.returncode:
        raise RuntimeError('Render failed')


def ass_time(seconds):
    centiseconds = round(seconds*100)
    return f'{centiseconds//360000}:{centiseconds//6000%60:02}:{centiseconds//100%60:02}.{centiseconds%100:02}'


def title_pin(size, color, hole):
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


def title_shadow(layer, radius, alpha):
    from PIL import Image, ImageFilter
    mask = layer.split()[3].point(lambda value: value*alpha//255)
    shadow = Image.new('RGBA',layer.size,(0,0,0,0))
    shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(radius)))
    image = Image.new('RGBA',layer.size,(0,0,0,0))
    image.alpha_composite(shadow,(0,6))
    image.alpha_composite(layer)
    return image


def title_image(text, path):
    from PIL import Image, ImageDraw, ImageFont
    semibold = str(Path(__file__).parent / 'fonts' / 'InterDisplay-SemiBold.otf')
    pin = title_pin(34,(235,64,52,255),(255,255,255,255))
    padding, gap, height = 30, 14, 92
    width = ImageFont.truetype(semibold,50).getlength(text)
    size = min(50,int(50*(900-2*padding-gap-pin.width)/max(1,width)))
    if size < round(50/76*44):
        raise RuntimeError('Title cannot fit safe width')
    font = ImageFont.truetype(semibold,size)
    text_width = round(font.getlength(text))
    image = Image.new('RGBA',(2*padding+pin.width+gap+text_width,height),(0,0,0,0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0,0,image.width-1,height-1),radius=22,fill=(255,255,255,255))
    image.alpha_composite(pin,(padding,(height-pin.height)//2))
    draw.text((padding+pin.width+gap,height//2),text,font=font,fill=(20,20,20),anchor='lm')
    title_shadow(image,18,90).save(path)


def title_origin(image_size, frame):
    return (frame[0]-image_size[0])//2, round(590*frame[0]/1080)


def title_position(image_size, draft):
    return title_origin(image_size,frame_size(draft))


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
    from PIL import Image, ImageDraw
    outputs = {'sources':{},'durations':{},'audio':{},'sheets':[]}
    if sum(m['size'] for m in job['sources'].values()) > 1024*1024*1024:
        raise RuntimeError('Batch exceeds 1 GiB')
    for name, manifest in job['sources'].items():
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
            run(['-i',path,'-vn','-ac','1','-ar','16000','-map_metadata','-1',flac])
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
            run(['-i',path,'-vf','fps=1,scale=320:-2','-q:v','4',frames/'%06d.jpg'])
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
    outputs, joined_files = {}, {}
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
            run(arguments)
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
    run(['-ss',settings['time'],'-i',joined_files[leaders[settings['target']]],'-frames:v','1','-vf',f'scale={width}:{height}','-map_metadata','-1',cover])
    from PIL import Image, ImageCms, ImageFont
    with Image.open(cover) as image:
        clean = Image.new('RGB',image.size)
        clean.paste(image)
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
            with Image.open(image) as placed:
                x, y = title_position(placed.size,draft)
            title_inputs += ['-i',image]
            video_filter += f"[t{index}];[t{index}][{index+2}:v]overlay=x={x}:y={y}:eof_action=repeat:enable='between(t,{cue['start']+1/30:.6f},{cue['end']+1/30:.6f})'"
        if target == 'tg-story' and not draft:
            video_filter += ',scale=720:1280'
        arguments = ['-i',joined_files[target],'-i',cover,*title_inputs,'-filter_complex',video_filter + '[vout]','-map','[vout]','-map','0:a:0','-af',audio_filter(draft),*encode_arguments(target,draft)]
        arguments += ['-movflags','+faststart','-map_metadata','-1','-map_chapters','-1','-metadata','encoder=','-metadata:s:v','encoder=',output]
        run(arguments)
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
    success = False
    try:
        with tempfile.TemporaryDirectory() as directory:
            outputs = (prepare if job.get('mode') == 'prepare' else render)(storage.Client().bucket(job['bucket']),job,Path(directory))
        reference.update({'state':'done','outputs':outputs})
        success = True
    except Exception:
        reference.update({'state':'failed','error':'Render failed; inspect inputs before retry'})
    enqueue(job,success)


if __name__ == '__main__':
    main()
