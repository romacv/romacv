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


def title_image(text, path):
    from PIL import Image, ImageDraw, ImageFont
    bold = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
    with Image.new('RGBA',(136,128)) as canvas:
        ImageDraw.Draw(canvas).text((0,0),'\U0001F4CD',font=ImageFont.truetype('/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf',109),embedded_color=True)
        pin = canvas.crop(canvas.getbbox()).resize((56,56))
    padding, gap = 32, 16
    width = ImageFont.truetype(bold,56).getlength(text)
    size = min(56,int(56*(900-2*padding-gap-pin.width)/max(1,width)))
    if size < 24:
        raise RuntimeError('Title cannot fit safe width')
    font = ImageFont.truetype(bold,size)
    text_width = round(font.getlength(text))
    image = Image.new('RGBA',(2*padding+pin.width+gap+text_width,96),(0,0,0,0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0,0,image.width-1,image.height-1),radius=48,fill=(0,0,0,140))
    image.alpha_composite(pin,(padding,(image.height-pin.height)//2))
    draw.text((padding+pin.width+gap,image.height//2),text,font=font,fill='white',anchor='lm',stroke_width=1,stroke_fill=(0,0,0,200))
    image.save(path)


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


def render(bucket, job, root):
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
    for target, timeline in job['edl']['targets'].items():
        parts = []
        for index, clip in enumerate(timeline['clips']):
            source = sources[clip['source']]
            part = root / f'{target}-{index}.mp4'
            filters = 'scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30'
            if job['edl']['fit'] == 'blur':
                filters = 'split=2[bg][fg];[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:10[back];[fg]scale=1080:1920:force_original_aspect_ratio=decrease[front];[back][front]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30'
            probe = subprocess.run(['ffprobe','-v','error','-select_streams','a','-show_entries','stream=index','-of','json',str(source)],capture_output=True,check=True)
            audio = bool(json.loads(probe.stdout).get('streams'))
            arguments = ['-ss',clip['in'],'-t',clip['out']-clip['in'],'-i',source]
            if not audio:
                arguments += ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
            arguments += ['-filter_complex', filters + '[vout]', '-map','[vout]','-map','0:a:0' if audio else '1:a:0','-t',clip['out']-clip['in'],'-c:v','libx264','-preset','fast','-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2','-map_metadata','-1',part]
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
    run(['-ss',settings['time'],'-i',joined_files[settings['target']],'-frames:v','1','-vf','scale=1080:1920','-map_metadata','-1',cover])
    from PIL import Image, ImageCms, ImageFont
    with Image.open(cover) as image:
        clean = Image.new('RGB',image.size)
        clean.paste(image)
        clean.save(cover,'JPEG',quality=90,icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',44)
    for target, timeline in job['edl']['targets'].items():
        subtitles = root / (target + '.ass')
        header = Path('/app/subtitles.ass').read_text()
        lines = []
        for cue in timeline['subtitles']:
            text = '\\N'.join(cue['lines'])
            width = max(font.getlength(line) for line in cue['lines'])
            size = min(44,int(44*780/max(1,width)))
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
            title_inputs += ['-i',image]
            video_filter += f"[t{index}];[t{index}][{index+2}:v]overlay=x=(W-w)/2:y=260:eof_action=repeat:enable='between(t,{cue['start']+1/30:.6f},{cue['end']+1/30:.6f})'"
        if target == 'tg-story':
            video_filter += ',scale=720:1280'
        arguments = ['-i',joined_files[target],'-i',cover,*title_inputs,'-filter_complex',video_filter + '[vout]','-map','[vout]','-map','0:a:0','-af','adelay=33.333:all=1,loudnorm=I=-16:TP=-1.5:LRA=11']
        if target == 'tg-story':
            arguments += ['-c:v','libx265','-tag:v','hvc1','-x265-params','keyint=30:min-keyint=30:scenecut=0','-b:v','2500k','-maxrate','3000k','-bufsize','6000k']
        else:
            arguments += ['-c:v','libx264','-preset','fast','-crf','22']
        arguments += ['-pix_fmt','yuv420p','-r','30','-c:a','aac','-b:a','128k','-movflags','+faststart','-map_metadata','-1','-map_chapters','-1','-metadata','encoder=','-metadata:s:v','encoder=',output]
        run(arguments)
        joined_files[target].unlink()
        limit = 30_000_000 if target == 'tg-story' else 50_000_000
        if output.stat().st_size > limit:
            raise RuntimeError('Video exceeds preview upload limit')
        probe = subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(output)],capture_output=True,check=True)
        measured = json.loads(probe.stdout)
        duration = float(measured['format']['duration'])
        maximum = 60 if target in ('ig-story','tg-story') else 90
        if duration > maximum + 0.01:
            raise RuntimeError('Encoded duration exceeds target limit')
        outputs[target] = put(bucket,output,job['prefix'] + '/' + output.name)
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
