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

import skills
from store import GROUPS, NAMES, PIN_SPEC_STAGES, newest_states, pin_stage

PROJECT = 'resromcom-493b4'
MAX_AGE = 86400
LIMIT = 10
QUOTA_DEADLINE = 10
PREVIEW_LIMIT = 50
NEAR_FREE = 80
WAITING_AGE = 7 * 86400
WORKING = ('готовлю черновик','монтаж','публикую')
PUBLIC_HOSTS = ('t.me', 'www.instagram.com', 'instagram.com', 'www.threads.net', 'www.threads.com', 'threads.net', 'threads.com', 'resrom.com', 'www.resrom.com')
PACIFIC = ZoneInfo('America/Los_Angeles')
RUN_CPU = 'run.googleapis.com/container/cpu/allocation_time'
RUN_MEMORY = 'run.googleapis.com/container/memory/allocation_time'
RUN_REQUESTS = 'run.googleapis.com/request_count'
FIRESTORE_READS = 'firestore.googleapis.com/document/read_ops_count'
FIRESTORE_WRITES = 'firestore.googleapis.com/document/write_ops_count'
FIRESTORE_DELETES = 'firestore.googleapis.com/document/delete_ops_count'
FIRESTORE_DATABASE = 'firestore.googleapis.com/Database'
GIB = 2 ** 30
AR_LOCATION = 'us-central1'
GB = 10 ** 9
QUOTA_TTL = 600
BILLING_TTL = 1800
BILLING_DATASET = PROJECT + '.billing_export'
BILLING_PREFIX = 'gcp_billing_export_v1_'
BILLING_BYTES = 100 * 2 ** 20
COST_ITEMS = 30
RENDER_LIMIT = 200
ANTHROPIC_FIELDS = ('calls', 'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'web_searches')
SPECS = (
    ('svc_cpu','Cloud Run (сервисы), vCPU','с',RUN_CPU,'cloud_run_revision','month',1),
    ('svc_mem','Cloud Run (сервисы), память','ГиБ·с',RUN_MEMORY,'cloud_run_revision','month',1),
    ('svc_req','Cloud Run (сервисы), запросы','шт',RUN_REQUESTS,'cloud_run_revision','month',1),
    ('cpu','Cloud Run (render-задачи), vCPU','с',RUN_CPU,'cloud_run_job','month',1),
    ('mem','Cloud Run (render-задачи), память','ГиБ·с',RUN_MEMORY,'cloud_run_job','month',1),
    ('reads','Firestore, чтения за сутки','шт',FIRESTORE_READS,FIRESTORE_DATABASE,'day',1),
    ('writes','Firestore, записи за сутки','шт',FIRESTORE_WRITES,FIRESTORE_DATABASE,'day',1),
    ('deletes','Firestore, удаления за сутки','шт',FIRESTORE_DELETES,FIRESTORE_DATABASE,'day',1),
    ('gcs_bytes','Cloud Storage, объём','ГиБ','storage.googleapis.com/storage/v2/total_bytes','gcs_bucket','gauge',GIB),
    ('gcs_egress','Cloud Storage, исходящий трафик (оценка)','ГиБ','storage.googleapis.com/network/sent_bytes_count','gcs_bucket','month',GIB),
    ('tasks','Cloud Tasks, операции','шт','cloudtasks.googleapis.com/api/request_count','cloud_tasks_queue','month',1),
    ('log','Cloud Logging, приём','ГиБ','logging.googleapis.com/billing/bytes_ingested',None,'month',GIB),
    ('host_out','Firebase Hosting, трафик','ГБ','firebasehosting.googleapis.com/network/sent_bytes_count','firebase_domain','month',GB),
    ('host_size','Firebase Hosting, хранилище','ГБ','firebasehosting.googleapis.com/storage/total_bytes','firebase_domain','gauge',GB),
)
RATE_KEYS = ('gcs_bytes','ar','host_size')
PERIODS = {'month':'month','day':'day','gauge':'now'}
NO_METRIC = (('scheduler','Cloud Scheduler','заданий',3),('secrets','Secret Manager','версий',6),('build','Cloud Build','мин',2500),('pubsub','Pub/Sub','ГиБ',10))
FREE = {'svc_cpu':180000,'svc_mem':360000,'svc_req':2000000,'cpu':240000,'mem':450000,'reads':50000,'writes':20000,'deletes':20000,'gcs_bytes':5,'gcs_egress':100,'tasks':1000000,'ar':0.5,'log':50,'host_out':10,'host_size':10,'speech':60,'mon_reads':1000000}
PRICE = {'svc_cpu':0.000024,'svc_mem':0.0000025,'svc_req':0.4 / 1000000,'cpu':0.000018,'mem':0.000002,'reads':0.03 / 100000,'writes':0.09 / 100000,'deletes':0.01 / 100000,'gcs_bytes':0.02,'gcs_egress':0.12,'tasks':0.4 / 1000000,'ar':0.1,'log':0.5,'host_out':0.15,'host_size':0.026,'speech':0.024,'mon_reads':0.5 / 1000000}


BALANCE = {'start':214.81,'at':1793113080,'limit':97,'month_spent':5.28,'auto_reload':False,'credits':[
    {'kind':'purchased','name':'Paid credit grant','left':2.74,'granted_total':5.44,'granted':'2026-10-20','expires':None,'applies':['API','Claude Code','Playground']},
    {'kind':'promo','name':'API credit (Max 5x)','left':96.97,'granted_total':100.00,'granted':'2026-10-19','expires':'2026-11-19','applies':['Agent SDK','API','Batch API','Playground','Managed Agents']},
    {'kind':'promo','name':'API credit (Max 20x)','left':115.08,'granted_total':115.08,'granted':'2026-10-21','expires':'2026-11-24','applies':['Agent SDK','API','Batch API','Playground','Managed Agents']}]}


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


def series_total(client, project, metric, resource, start, now, period, gauge=False):
    from google.cloud import monitoring_v3
    from google.protobuf import duration_pb2
    interval = monitoring_v3.TimeInterval({'start_time':{'seconds':int(start)},'end_time':{'seconds':int(now)}})
    aligner = monitoring_v3.Aggregation.Aligner.ALIGN_MEAN if gauge else monitoring_v3.Aggregation.Aligner.ALIGN_SUM
    aggregation = monitoring_v3.Aggregation({'alignment_period':duration_pb2.Duration(seconds=period),'per_series_aligner':aligner,'cross_series_reducer':monitoring_v3.Aggregation.Reducer.REDUCE_SUM})
    request = {'name':'projects/' + project,'filter':'metric.type="' + metric + '"' + (' AND resource.type="' + resource + '"' if resource else ''),'interval':interval,'view':monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,'aggregation':aggregation}
    total = 0
    for series in client.list_time_series(request=request, timeout=8):
        if gauge:
            if series.points:
                total += series.points[0].value.double_value or series.points[0].value.int64_value
            continue
        for point in series.points:
            total += point.value.double_value or point.value.int64_value
    return total


def quota_row(key, label, unit, used, div=1, period='month'):
    used = used / div
    return {'key':key,'period':period,'label':label,'unit':unit,'used':round(used, 3),'free':FREE[key],'percent':round(100 * used / FREE[key], 1),'paid':round(max(0, used - FREE[key]) * PRICE[key], 4)}


def monitoring_client():
    from google.cloud import monitoring_v3
    return monitoring_v3.MetricServiceClient()


def registry_bytes(project):
    from google.cloud import artifactregistry_v1
    client = artifactregistry_v1.ArtifactRegistryClient()
    return sum(repo.size_bytes for repo in client.list_repositories(parent='projects/' + project + '/locations/' + AR_LOCATION, timeout=8))


def window_of(window, now):
    return {'month':(month_start(now), 86400),'day':(day_start(now), 3600),'gauge':(now - 172800, 86400)}[window]


def quota(store, now):
    try:
        cached = store.get('sessions/quota') or {}
    except Exception:
        logging.exception('quota cache failed')
        cached = {}
    if cached.get('rows') and now - cached.get('at', 0) < QUOTA_TTL:
        return cached['rows']
    month = time.strftime('%Y-%m', time.gmtime(now))
    rows = []
    try:
        client = monitoring_client()
        project = os.environ.get('GOOGLE_CLOUD_PROJECT') or os.environ.get('GCLOUD_PROJECT') or PROJECT
    except Exception:
        logging.exception('monitoring client failed')
        client = None
    if client:
        pool = ThreadPoolExecutor(max_workers=8)
        jobs = [(key, label, unit, div, metric, window, pool.submit(series_total, client, project, metric, resource, window_of(window, now)[0], now, window_of(window, now)[1], window == 'gauge')) for key, label, unit, metric, resource, window, div in SPECS]
        jobs.append(('ar', 'Artifact Registry, объём', 'ГиБ', GIB, 'repositories', 'gauge', pool.submit(registry_bytes, project)))
        wait([job for *_, job in jobs], timeout=QUOTA_DEADLINE)
        pool.shutdown(wait=False, cancel_futures=True)
        for key, label, unit, div, metric, window, job in jobs:
            try:
                if job.done():
                    rows.append({**quota_row(key, label, unit, job.result(), div, PERIODS[window]),**({'estimate':True} if key == 'gcs_egress' else {}),**({'rate':'month'} if key in RATE_KEYS else {})})
            except Exception:
                logging.exception('monitoring read failed: ' + key + ' ' + metric)
    measured = bool(rows)
    if not measured and cached.get('rows'):
        return [{**row,'stale':True} for row in cached['rows']]
    try:
        seconds = (store.get('usage/' + month) or {}).get('seconds', 0)
        rows.append(quota_row('speech', 'Распознавание речи', 'мин', seconds / 60))
    except Exception:
        logging.exception('speech usage failed')
    series = cached.get('series') or {}
    count = (series.get('count', 0) if series.get('month') == month else 0) + len(SPECS)
    if measured:
        rows.append({**quota_row('mon_reads', 'Cloud Monitoring, чтение рядов (оценка)', 'рядов', count), 'estimate':True})
    rows.extend({'key':key,'label':label,'unit':unit,'used':None,'free':free,'percent':None,'paid':None,'note':'нет метрики'} for key, label, unit, free in NO_METRIC)
    if measured:
        try:
            store.put('sessions/quota', {'at':now,'rows':rows,'series':{'month':month,'count':count}})
        except Exception:
            logging.exception('quota cache write failed')
    return rows


def billing_client():
    from google.cloud import bigquery
    return bigquery.Client(project=PROJECT)


def invoice_month(now):
    return datetime.fromtimestamp(now, PACIFIC).strftime('%Y%m')


def billing_rows(now):
    from google.cloud import bigquery
    client = billing_client()
    table = next((t.table_id for t in sorted(client.list_tables(BILLING_DATASET), key=lambda t: t.table_id) if t.table_id.startswith(BILLING_PREFIX)), None)
    if not table:
        return None
    query = 'SELECT service.description AS service_name, currency, SUM(cost) + SUM((SELECT IFNULL(SUM(c.amount), 0) FROM UNNEST(credits) c)) AS cost FROM `' + BILLING_DATASET + '.' + table + '` WHERE project.id = @project AND invoice.month = @month GROUP BY service_name, currency ORDER BY cost DESC'
    config = bigquery.QueryJobConfig(maximum_bytes_billed=BILLING_BYTES, query_parameters=[bigquery.ScalarQueryParameter('project', 'STRING', PROJECT), bigquery.ScalarQueryParameter('month', 'STRING', invoice_month(now))])
    return [{'service':row['service_name'],'currency':row['currency'],'cost':round(float(row['cost'] or 0), 4)} for row in client.query(query, job_config=config).result(timeout=QUOTA_DEADLINE)]


def billing(store, now):
    try:
        cached = store.get('sessions/billing')
        if cached and cached.get('month') == invoice_month(now) and now - cached.get('at', 0) < BILLING_TTL:
            return cached
        rows = billing_rows(now)
        value = {'at':now,'month':invoice_month(now),'state':'ok' if rows is not None else 'pending','rows':rows or []}
        store.put('sessions/billing', value)
        return value
    except Exception:
        logging.exception('billing read failed')
        return None


def months_between(start, now):
    first = time.gmtime(start)
    last = time.gmtime(now)
    year, month = first.tm_year, first.tm_mon
    names = []
    while (year, month) <= (last.tm_year, last.tm_mon):
        names.append('%04d-%02d' % (year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return names


def anthropic_of(store, now):
    names = months_between(BALANCE['at'], now)
    docs = {s.id: s.to_dict() or {} for s in store.db.get_all([store.db.document('usage/anthropic-' + name) for name in names]) if s.exists}
    doc = docs.get('anthropic-' + time.strftime('%Y-%m', time.gmtime(now))) or {}
    spent = round(sum(docs.get('anthropic-' + name, {}).get('usd', 0) for name in names), 4)
    balance = {'start':BALANCE['start'],'at':BALANCE['at'],'spent':spent,'left':round(BALANCE['start'] - spent, 4),'limit':BALANCE['limit'],'month_spent':BALANCE['month_spent'],'auto_reload':BALANCE['auto_reload'],'credits':BALANCE['credits']}
    return {**{field:doc.get(field, 0) for field in ANTHROPIC_FIELDS},'usd':round(doc.get('usd', 0), 4),'balance':balance}


def costs_of(store, ids, by_key):
    ids = ids[:COST_ITEMS]
    if not ids:
        return {}
    docs = {s.id[5:]:s.to_dict() for s in store.db.get_all([store.db.document('usage/item-' + key) for key in ids]) if s.exists}
    renders = {}
    seen = 0
    for s in store.db.collection('renders').where('item', 'in', ids).limit(RENDER_LIMIT).stream():
        seen += 1
        row = s.to_dict()
        total = renders.setdefault(row.get('item'), {'vcpu_s':0, 'gib_s':0})
        total['vcpu_s'] += (row.get('usage') or {}).get('vcpu_s', 0)
        total['gib_s'] += (row.get('usage') or {}).get('gib_s', 0)
    if seen >= RENDER_LIMIT:
        logging.warning('render usage limit hit: ' + str(RENDER_LIMIT))
    render_over = bool(by_key.get('cpu') and by_key.get('mem') and (by_key['cpu']['used'] >= FREE['cpu'] or by_key['mem']['used'] >= FREE['mem']))
    speech_over = bool(by_key.get('speech') and by_key['speech']['used'] >= FREE['speech'])
    costs = {}
    for key in ids:
        doc, render = docs.get(key, {}), renders.get(key)
        if not doc and not render:
            continue
        kinds = sorted(({'kind':kind,'usd':round((value or {}).get('usd', 0), 4)} for kind, value in (doc.get('anthropic') or {}).items()), key=lambda row: -row['usd'])
        minutes = round(doc.get('speech_seconds', 0) / 60, 2)
        speech_paid = round(minutes * PRICE['speech'], 4) if speech_over else 0
        render = render or {'vcpu_s':0, 'gib_s':0}
        render_paid = round(render['vcpu_s'] * PRICE['cpu'] + render['gib_s'] * PRICE['mem'], 4) if render_over else 0
        usd = round(sum(row['usd'] for row in kinds), 4)
        costs[key] = {'anthropic':kinds,'anthropic_usd':usd,'speech_min':minutes,'speech_paid':speech_paid,'vcpu_s':round(render['vcpu_s'], 1),'gib_s':round(render['gib_s'], 1),'render_paid':render_paid,'total':round(usd + speech_paid + render_paid, 4)}
    return costs


def budget_of(store):
    value = store.get('sessions/budget')
    if not value:
        return None
    return {'cost':value.get('costAmount'),'budget':value.get('budgetAmount'),'start':value.get('costIntervalStart'),'currency':value.get('currencyCode') or 'USD','at':value.get('at')}


def targets_of(spec):
    return [NAMES.get(target, 'Сайт' if target == 'blog' else target) for target in spec.get('targets', [])]


def costs_view(store, now):
    bills = ThreadPoolExecutor(max_workers=1)
    bill = bills.submit(billing, store, now)
    began = time.time()
    rows = quota(store, now)
    try:
        invoice = bill.result(timeout=max(0, QUOTA_DEADLINE - (time.time() - began)))
    except Exception:
        logging.exception('billing wait failed')
        invoice = None
    bills.shutdown(wait=False, cancel_futures=True)
    try:
        spent = anthropic_of(store, now)
    except Exception:
        logging.exception('anthropic usage failed')
        spent = None
    return {'now':now,'view':'costs','quota':rows,'budget':budget_of(store),'billing':invoice,'anthropic':spent}


def skills_view(store, now, skill=None):
    found = skills.load(store)
    keys = [skill] if skill in found else [] if skill else sorted(found)
    return {'now':now,'view':'skills','skill':skill,'skills':[{'id':key,'title':found[key]['title'],'body':found[key]['body'],'examples':[{name:example.get(name, '') for name in ('verdict', 'feedback', 'before', 'after')} for example in found[key].get('examples', [])]} for key in keys]}


def snapshot(store, now, view='progress', skill=None):
    if view == 'costs':
        return costs_view(store, now)
    if view == 'skills':
        return skills_view(store, now, skill)
    jobs = [{**s.to_dict(),'id':s.id} for s in store.db.collection('renders').where('state', 'in', ['queued', 'running']).stream()]
    collecting = [(s.id, s.to_dict()) for s in store.db.collection('items').where('status', '==', 'collecting').stream()]
    previews = [s.to_dict() for s in store.db.collection('specs').where('state', 'in', ['previewed', 'needs-edit']).limit(PREVIEW_LIMIT).stream()]
    specs = [s.to_dict() for s in store.db.collection('specs').where('published_at', '>=', now - MAX_AGE).stream()]
    specs = sorted((spec for spec in specs if spec.get('state') in ('done', 'partial')), key=lambda spec: -spec['published_at'])[:LIMIT]
    busy = {job.get('item') for job in jobs}
    working = {s.id: s.to_dict() for s in store.db.collection('items').where('status', 'in', ['queued', 'drafting']).stream()}
    states = newest_states(s.to_dict() for s in store.db.collection('specs').where('state', 'in', [state for state, label in PIN_SPEC_STAGES]).stream())
    wanted = sorted((busy | set(working) | set(states) | {spec.get('item') for spec in specs} | {spec.get('item') for spec in previews}) - {None})
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
    active = [{'id':job['id'],'item':job.get('item'),'title':title_of(items.get(job.get('item'))),'mode':job.get('mode'),'state':job['state'],'label':(job.get('progress') or {}).get('label',''),'since':(job.get('progress') or {}).get('since', now),'steps':steps_of(job, now)} for job in jobs]
    for key in sorted(set(working) | set(states)):
        item = items.get(key)
        stage = pin_stage(item, states.get(key)) if item and item.get('status') not in ('rejected', 'failed') and key not in busy else None
        if stage in WORKING:
            active.append({'id':key,'item':key,'title':title_of(item),'mode':None,'state':'running','label':stage[0].upper() + stage[1:],'since':item.get('updated') or item.get('created') or now,'steps':[]})
    active.sort(key=lambda row: row['since'])
    published = [{'id':spec.get('slug') or spec.get('item'),'item':spec.get('item'),'title':title_of(items.get(spec.get('item'))),'at':spec['published_at'],'state':spec['state'],'links':[{'name':NAMES.get(target, 'Сайт' if target == 'blog' else target),'url':(result or {}).get('url')} for target, result in {**spec.get('results', {})}.items() if public((result or {}).get('url'))]} for spec in specs]
    try:
        rows = (store.get('sessions/quota') or {}).get('rows') or []
    except Exception:
        logging.exception('quota cache failed')
        rows = []
    by_key = {row['key']:row for row in rows}
    for job in active:
        for step in job['steps']:
            if by_key.get('cpu') and by_key.get('mem'):
                over = by_key['cpu']['used'] >= FREE['cpu'] or by_key['mem']['used'] >= FREE['mem']
                step['paid'] = round(step['vcpu_s'] * PRICE['cpu'] + step['gib_s'] * PRICE['mem'], 4) if over else 0
                step['near'] = not over and max(by_key['cpu']['percent'], by_key['mem']['percent']) >= NEAR_FREE
    try:
        costs = costs_of(store, list(dict.fromkeys(row.get('item') or row['id'] for row in active + waiting + published)), by_key)
    except Exception:
        logging.exception('item costs failed')
        costs = {}
    for row in active + waiting + published:
        row['cost'] = costs.get(row.get('item') or row['id'])
    return {'now':now,'view':'progress','active':active,'waiting':waiting,'published':published}


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
        view = request.args.get('view')
        view = view if view in ('costs', 'skills') else 'progress'
        body = json.dumps(snapshot(store, now, view, request.args.get('skill')))
    except Exception:
        logging.exception('progress snapshot failed')
        return https_fn.Response('',status=503,headers=cors)
    return https_fn.Response(body,status=200,headers={**cors,'Content-Type':'application/json','Cache-Control':'no-store'})
