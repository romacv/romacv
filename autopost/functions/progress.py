import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from firebase_functions import https_fn

from store import titled

MAX_AGE = 86400
LIMIT = 10


def user_of(init_data, token, now):
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop('hash', '')
    check = '\n'.join(key + '=' + pairs[key] for key in sorted(pairs))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received.encode(), expected.encode()):
        return None, False
    try:
        return json.loads(pairs['user'])['id'], now - MAX_AGE <= int(pairs['auth_date']) <= now + 60
    except (KeyError, ValueError, TypeError):
        return None, False


def title_of(item):
    return titled(item).strip(' «»…') if item else ''


def steps_of(job, now):
    steps = job.get('steps') or []
    ends = [step['since'] for step in steps[1:]] + [now]
    return [{'label':step['label'],'since':step['since'],'seconds':max(0, round(end - step['since'])),'done':index < len(steps) - 1} for index, (step, end) in enumerate(zip(steps, ends))]


def snapshot(store, now):
    jobs = [s.to_dict() for s in store.db.collection('renders').where('state', 'in', ['queued', 'running']).stream()]
    waiting = [(s.id, s.to_dict()) for s in store.db.collection('items').where('status', 'in', ['collecting', 'previewed']).stream()]
    specs = [s.to_dict() for s in store.db.collection('specs').where('published_at', '>=', now - MAX_AGE).stream()]
    specs = sorted((spec for spec in specs if spec.get('state') in ('done', 'partial')), key=lambda spec: -spec['published_at'])[:LIMIT]
    busy = {job.get('item') for job in jobs}
    waiting = sorted((item for key, item in waiting if key not in busy), key=lambda item: -(item.get('updated') or item.get('created') or 0))[:LIMIT]
    wanted = sorted((busy | {spec.get('item') for spec in specs}) - {None})
    items = {s.id: s.to_dict() for s in store.db.get_all([store.db.document('items/' + key) for key in wanted]) if s.exists}
    active = [{'title':title_of(items.get(job.get('item'))),'mode':job.get('mode'),'state':job['state'],'label':(job.get('progress') or {}).get('label',''),'since':(job.get('progress') or {}).get('since', now),'steps':steps_of(job, now)} for job in jobs]
    active.sort(key=lambda row: row['since'])
    return {'now':now,'active':active,'waiting':[{'title':title_of(item),'status':item['status']} for item in waiting],'published':[{'title':title_of(items.get(spec.get('item'))),'at':spec['published_at'],'state':spec['state']} for spec in specs]}


def respond(request, config, store):
    cors = {'Access-Control-Allow-Origin':'https://www.resrom.com','Access-Control-Allow-Headers':'Authorization','Access-Control-Allow-Methods':'GET, OPTIONS','Vary':'Origin'}
    if request.method == 'OPTIONS':
        return https_fn.Response('',status=204,headers=cors)
    if request.method != 'GET':
        return https_fn.Response('',status=405,headers=cors)
    header = request.headers.get('Authorization', '')
    now = time.time()
    user, fresh = user_of(header[4:], config['telegram_bot_token'], now) if header.startswith('tma ') else (None, False)
    if user is None or str(user) != str(config['roman_user_id']):
        return https_fn.Response('',status=403,headers=cors)
    if not fresh:
        return https_fn.Response('',status=401,headers=cors)
    try:
        body = json.dumps(snapshot(store, now))
    except Exception:
        logging.exception('progress snapshot failed')
        return https_fn.Response('',status=503,headers=cors)
    return https_fn.Response(body,status=200,headers={**cors,'Content-Type':'application/json','Cache-Control':'no-store'})
