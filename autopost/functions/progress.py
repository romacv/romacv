import hashlib
import hmac
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from firebase_functions import https_fn

from store import GROUPS, NAMES

PROJECT = 'resromcom-493b4'
MAX_AGE = 86400
LIMIT = 10
QUOTA_DEADLINE = 10
PREVIEW_LIMIT = 50
NEAR_FREE = 80
WAITING_AGE = 7 * 86400
PUBLIC_HOSTS = ('t.me', 'www.instagram.com', 'instagram.com', 'www.threads.net', 'www.threads.com', 'threads.net', 'threads.com', 'resrom.com', 'www.resrom.com')
PACIFIC = ZoneInfo('America/Los_Angeles')
RUN_CPU = 'run.googleapis.com/container/cpu/allocation_time'
RUN_MEMORY = 'run.googleapis.com/container/memory/allocation_time'
FIRESTORE_READS = 'firestore.googleapis.com/document/read_count'
FREE = {'cpu':240000,'mem':450000,'reads':50000,'speech':60}
PRICE = {'cpu':0.000018,'mem':0.000002,'reads':0.03 / 100000,'speech':0.024}


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
    texts = [m.get('text') or m.get('caption') for m in (item or {}).get('messages', []) if m.get('text') or m.get('caption')]
    first = next((t for t in texts if not t.startswith(('Место: ', 'Место (координаты): '))), texts[0] if texts else '')
    return ' '.join(first.split())


def public(url):
    parts = urlsplit(url) if isinstance(url, str) else None
    return bool(parts and parts.scheme == 'https' and parts.hostname in PUBLIC_HOSTS)


def steps_of(job, now):
    steps = job.get('steps') or []
    cpu, memory = (job.get('resources') or {}).get('cpu', 2), (job.get('resources') or {}).get('mem_gib', 2)
    ends = [step.get('ended') or (steps[index + 1]['since'] if index + 1 < len(steps) else now) for index, step in enumerate(steps)]
    rows = []
    for index, (step, end) in enumerate(zip(steps, ends)):
        seconds = step['seconds'] if 'seconds' in step else max(0, end - step['since'])
        rows.append({'label':step['label'],'since':step['since'],'seconds':round(seconds),'done':'ended' in step or index < len(steps) - 1,'vcpu_s':step.get('vcpu_s', round(cpu * seconds, 1)),'gib_s':step.get('gib_s', round(memory * seconds, 1))})
    return rows


def month_start(now):
    local = datetime.fromtimestamp(now, PACIFIC)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def day_start(now):
    return datetime.fromtimestamp(now, PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def series_total(client, project, metric, resource, start, now, period):
    from google.cloud import monitoring_v3
    from google.protobuf import duration_pb2
    interval = monitoring_v3.TimeInterval({'start_time':{'seconds':int(start)},'end_time':{'seconds':int(now)}})
    aggregation = monitoring_v3.Aggregation({'alignment_period':duration_pb2.Duration(seconds=period),'per_series_aligner':monitoring_v3.Aggregation.Aligner.ALIGN_SUM,'cross_series_reducer':monitoring_v3.Aggregation.Reducer.REDUCE_SUM})
    request = {'name':'projects/' + project,'filter':'metric.type="' + metric + '"' + (' AND resource.type="' + resource + '"' if resource else ''),'interval':interval,'view':monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,'aggregation':aggregation}
    total = 0
    for series in client.list_time_series(request=request, timeout=8):
        for point in series.points:
            total += point.value.double_value or point.value.int64_value
    return total


def quota_row(key, label, unit, used):
    return {'key':key,'label':label,'unit':unit,'used':round(used, 1),'free':FREE[key],'percent':round(100 * used / FREE[key], 1),'paid':round(max(0, used - FREE[key]) * PRICE[key], 4)}


def monitoring_client():
    from google.cloud import monitoring_v3
    return monitoring_v3.MetricServiceClient()


def quota(store, now):
    rows = []
    try:
        client = monitoring_client()
        project = os.environ.get('GOOGLE_CLOUD_PROJECT') or os.environ.get('GCLOUD_PROJECT') or PROJECT
    except Exception:
        logging.exception('monitoring client failed')
        client = None
    if client:
        pool = ThreadPoolExecutor(max_workers=3)
        jobs = [(key, label, unit, pool.submit(series_total, client, project, metric, resource, start, now, period)) for key, label, unit, metric, resource, start, period in (('cpu','render (Cloud Run jobs), vCPU','с',RUN_CPU,'cloud_run_job',month_start(now),86400),('mem','render (Cloud Run jobs), память','ГиБ·с',RUN_MEMORY,'cloud_run_job',month_start(now),86400),('reads','Firestore, чтения за сутки','шт',FIRESTORE_READS,None,day_start(now),3600))]
        wait([job for *_, job in jobs], timeout=QUOTA_DEADLINE)
        pool.shutdown(wait=False, cancel_futures=True)
        for key, label, unit, job in jobs:
            try:
                if job.done():
                    rows.append(quota_row(key, label, unit, job.result()))
            except Exception:
                logging.exception('monitoring read failed')
    try:
        seconds = (store.get('usage/' + time.strftime('%Y-%m', time.gmtime(now))) or {}).get('seconds', 0)
        rows.append(quota_row('speech', 'Распознавание речи', 'мин', seconds / 60))
    except Exception:
        logging.exception('speech usage failed')
    return rows


def budget_of(store):
    value = store.get('sessions/budget')
    if not value:
        return None
    return {'cost':value.get('costAmount'),'budget':value.get('budgetAmount'),'start':value.get('costIntervalStart'),'currency':value.get('currencyCode') or 'USD','at':value.get('at')}


def targets_of(spec):
    return [NAMES.get(target, target) for target in spec.get('targets', [])]


def snapshot(store, now):
    jobs = [{**s.to_dict(),'id':s.id} for s in store.db.collection('renders').where('state', 'in', ['queued', 'running']).stream()]
    collecting = [(s.id, s.to_dict()) for s in store.db.collection('items').where('status', '==', 'collecting').stream()]
    previews = [s.to_dict() for s in store.db.collection('specs').where('state', 'in', ['previewed', 'needs-edit']).limit(PREVIEW_LIMIT).stream()]
    specs = [s.to_dict() for s in store.db.collection('specs').where('published_at', '>=', now - MAX_AGE).stream()]
    specs = sorted((spec for spec in specs if spec.get('state') in ('done', 'partial')), key=lambda spec: -spec['published_at'])[:LIMIT]
    busy = {job.get('item') for job in jobs}
    wanted = sorted((busy | {spec.get('item') for spec in specs} | {spec.get('item') for spec in previews}) - {None})
    items = {s.id: s.to_dict() for s in store.db.get_all([store.db.document('items/' + key) for key in wanted]) if s.exists}
    waiting = []
    for key, item in collecting:
        if key not in busy:
            count = len([m for m in item.get('messages', []) if m.get('photo') or m.get('video') or m.get('document')])
            waiting.append({'id':key,'title':title_of(item),'status':'collecting','wait':'Собираю материал' + (': ' + str(count) + ' влож.' if count else '') + '; жду «Готово»','targets':[label for name, label, _ in GROUPS if name in item.get('chosen', [])],'at':item.get('updated') or item.get('created') or 0})
    for spec in previews:
        item = items.get(spec.get('item'))
        at = (item or {}).get('updated') or (item or {}).get('created') or 0
        if not item or spec.get('item') in busy or spec.get('deleted') or not spec.get('operation') and at < now - WAITING_AGE:
            continue
        operation = {'fix':'исправление','delete':'удаление'}.get(spec.get('operation'))
        wait = 'Нужна правка черновика' if spec['state'] == 'needs-edit' else 'Жду подтверждения: ' + operation if operation else 'Черновик показан; жду «Опубликовать»'
        waiting.append({'id':spec.get('item'),'title':title_of(item),'status':spec['state'],'wait':wait,'targets':targets_of(spec),'at':at})
    waiting = sorted(waiting, key=lambda row: (row['status'] != 'collecting', -row['at']))[:LIMIT]
    active = [{'id':job['id'],'title':title_of(items.get(job.get('item'))),'mode':job.get('mode'),'state':job['state'],'label':(job.get('progress') or {}).get('label',''),'since':(job.get('progress') or {}).get('since', now),'steps':steps_of(job, now)} for job in jobs]
    active.sort(key=lambda row: row['since'])
    published = [{'id':spec.get('slug') or spec.get('item'),'title':title_of(items.get(spec.get('item'))),'at':spec['published_at'],'state':spec['state'],'links':[{'name':NAMES.get(target, target),'url':(result or {}).get('url')} for target, result in {**spec.get('results', {})}.items() if public((result or {}).get('url'))]} for spec in specs]
    rows = quota(store, now)
    by_key = {row['key']:row for row in rows}
    for job in active:
        for step in job['steps']:
            if by_key.get('cpu') and by_key.get('mem'):
                over = by_key['cpu']['used'] >= FREE['cpu'] or by_key['mem']['used'] >= FREE['mem']
                step['paid'] = round(step['vcpu_s'] * PRICE['cpu'] + step['gib_s'] * PRICE['mem'], 4) if over else 0
                step['near'] = not over and max(by_key['cpu']['percent'], by_key['mem']['percent']) >= NEAR_FREE
    return {'now':now,'active':active,'waiting':waiting,'published':published,'quota':rows,'budget':budget_of(store)}


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
