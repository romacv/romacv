import hashlib
import hmac
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from firebase_functions import https_fn

import skills
from store import Store, backfilled, table_rows, GROUPS, NAMES, PIN_SPEC_STAGES, DRAFT_DEADLINE, FINAL_DEADLINE, UPLOAD_DEADLINE, dead_render, newest_specs, newest_states, overdue, pin_stage, post_title, stale

PROJECT = 'resromcom-493b4'
MAX_AGE = 86400
LIMIT = 10
SESSION_LIMIT = 30
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
AGENT_LABELS = {'places':'места','media':'фото','copy':'тексты','merge':'сборка','montage':'монтаж'}
RENDER_STEPS = ('prepare', 'montage', 'render', 'final')
REASONS = ((r'^API failure: 190$', 'Нужен новый токен'), (r"can't download or generate the file", 'Telegram не принял файл'), (r'Материал истёк', 'материал устарел'))
IDLE_LIMITS = {'draft':DRAFT_DEADLINE,'edit':DRAFT_DEADLINE,'prepare':DRAFT_DEADLINE + FINAL_DEADLINE,'montage':DRAFT_DEADLINE + FINAL_DEADLINE,'render':DRAFT_DEADLINE + FINAL_DEADLINE,'final':DRAFT_DEADLINE + FINAL_DEADLINE,'publish':600,'verify':600}
STOPPED_WHY = 'черновик завис и остановлен'


def stop_reason(item, spec):
    item, spec = item or {}, spec or {}
    if item.get('stop_why') == 'upload':
        return 'файл не загрузился'
    if item.get('stop_why') == 'stalled':
        return STOPPED_WHY
    texts = [item.get('error'), spec.get('error'), *(spec.get('errors') or {}).values()]
    text = next((scrub(t) for t in texts if isinstance(t, str) and t), '')
    return reason_of(text) if text else 'ошибка черновика'
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


def title_of(item, spec=None):
    return post_title(item, spec)


def public(url):
    parts = urlsplit(url) if isinstance(url, str) else None
    return bool(parts and parts.scheme == 'https' and parts.hostname in PUBLIC_HOSTS)


def reason_of(error, target=None):
    if not error:
        return ''
    text = next((text for pattern, text in REASONS if re.search(pattern, error, re.I)), error)
    return text + ' ' + NAMES[target] if text == 'Нужен новый токен' and target in NAMES else text


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
        return [{**row,'stale':True,'at':cached.get('at')} for row in cached['rows']]
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
        known = bool(doc)
        kinds = sorted(({'kind':kind,'usd':round((value or {}).get('usd', 0), 4)} for kind, value in (doc.get('anthropic') or {}).items()), key=lambda row: -row['usd'])
        minutes = round(doc.get('speech_seconds', 0) / 60, 2)
        speech_paid = round(minutes * PRICE['speech'], 4) if speech_over else 0
        render = render or {'vcpu_s':0, 'gib_s':0}
        render_paid = round(render['vcpu_s'] * PRICE['cpu'] + render['gib_s'] * PRICE['mem'], 4) if render_over else 0
        usd = round(sum(row['usd'] for row in kinds), 4)
        costs[key] = {'anthropic':kinds,'anthropic_usd':usd,'speech_min':minutes,'speech_paid':speech_paid,'vcpu_s':round(render['vcpu_s'], 1),'gib_s':round(render['gib_s'], 1),'render_paid':render_paid,'total':round(usd + speech_paid + render_paid, 4) if known else None}
    return costs


def scrub(text):
    if not isinstance(text, str):
        return text
    return ' '.join(re.sub(r'https?://\S+', lambda m: m.group(0) if public(m.group(0).rstrip('.,;:)»”')) else '', text).split())


def sessions_of(store, ids):
    ids = ids[:COST_ITEMS]
    if not ids:
        return {}
    return {s.id:s.to_dict() for s in store.db.get_all([store.db.document('sessions/' + key) for key in ids]) if s.exists}


def spent(since, ended, now):
    return max(0, round((ended or now) - since)) if since else 0


def agents_of(step, now):
    names = sorted(step.get('agents') or {}, key=lambda name: (list(AGENT_LABELS).index(name) if name in AGENT_LABELS else len(AGENT_LABELS), name))
    rows = []
    for name in names:
        agent = step['agents'][name]
        rows.append({'name':name,'label':AGENT_LABELS.get(name, name),'status':agent.get('status'),'action':scrub(agent.get('action')),'started':agent.get('started'),'seconds':spent(agent.get('started'), agent.get('finished'), now),'usd':round(agent.get('usd') or 0, 4),'searches':agent.get('searches') or 0,'result':scrub(agent.get('result'))})
    return rows


def targets_in(step, now):
    rows = []
    for target, value in sorted((step.get('targets') or {}).items()):
        runs = value.get('runs') or []
        last = runs[-1] if runs else {}
        link = value.get('link')
        rows.append({'target':target,'name':NAMES.get(target, 'Сайт' if target == 'blog' else target),'status':value.get('status'),'attempt':value.get('attempt', value.get('attempts')),'url':value.get('url') if public(value.get('url')) else None,'error':scrub(value.get('error')),'api':scrub(value.get('api')),'link':link if isinstance(link, (bool, type(None))) else link if public(link) else None,'why':scrub(value.get('why')),'reason':reason_of(scrub(value.get('error')), target),'seconds':spent(last.get('since'), last.get('ended'), now)})
    return rows


def timeline(session, now, render=None):
    steps = [{**step, 'state':step.get('state') or 'planned'} for step in session.get('steps') or []]
    running = [step for step in steps if step.get('state') == 'running']
    step = running[-1] if running else None
    flat = len({(s.get('since'), s.get('ended')) for s in steps if s.get('ended')}) == 1 and sum(1 for s in steps if s.get('ended')) > 1
    past = []
    for s in steps:
        if s.get('state') not in ('done', 'failed', 'cancelled'):
            continue
        row = {'id':s['id'],'label':s.get('label', s['id']),'seconds':None if flat else spent(s.get('since'), s.get('ended'), now) if s.get('ended') else 0,'usd':round(s.get('usd') or 0, 4),'state':s['state'],'targets':targets_in(s, now),'attempts':1,'cancelled':int(s['state'] == 'cancelled')}
        last = past[-1] if past else None
        if last and last['id'] == row['id']:
            row = {**row,'seconds':None if flat else last['seconds'] + row['seconds'],'usd':round(last['usd'] + row['usd'], 4),'attempts':last['attempts'] + 1,'cancelled':last['cancelled'] + row['cancelled']}
            past[-1] = row
        else:
            past.append(row)
    current = None
    if step:
        current = {'id':step['id'],'label':step.get('label', step['id']),'since':step.get('since'),'seconds':spent(step.get('since'), None, now),'usd':round(step.get('usd') or 0, 4),'agents':agents_of(step, now),'targets':targets_in(step, now),'render':list(render or []) if step['id'] in RENDER_STEPS else []}
    return {'state':session.get('state'),'opened':session.get('opened'),'ended':session.get('ended'),'outcome':{**session['outcome'], 'why':scrub(session['outcome'].get('why'))} if isinstance(session.get('outcome'), dict) else session.get('outcome'),'usd':round(session.get('usd') or 0, 4),'past':past,'current':current,'planned':[{'id':s['id'],'label':s.get('label', s['id'])} for s in steps if s.get('state') == 'planned']}


def percent_of(session, state):
    if state == 'published':
        return 100
    if state == 'cancelled':
        return 0
    steps = [s for s in session.get('steps') or [] if s.get('state')]
    running = [s for s in steps if s.get('state') == 'running']
    rows = list((running[-1].get('targets') or {}).values()) if running else []
    fraction = sum(1 for row in rows if row.get('status') in ('done', 'failed', 'verified', 'unverified', 'unverifiable')) / len(rows) if rows else 0.5 if running else 0
    return max(0, min(100, round(100 * (sum(1 for s in steps if s['state'] == 'done') + fraction) / max(len(steps), 1))))


def session_view(key, session, item, spec, now, zone, render=None):
    state = session.get('state')
    if spec and spec.get('state') in ('done', 'partial') and state in ('open', 'published') and (spec.get('results') or spec.get('published')):
        state = 'published' if spec['state'] == 'done' else 'partial'
    stopped = state == 'open' and (bool(spec and spec.get('state') == 'failed' and not spec.get('operation')) or bool(item and item.get('status') == 'failed'))
    steps = timeline(session, now, render)
    ordered = [s for s in session.get('steps') or [] if s.get('state') == 'running']
    position = next((n for n, s in enumerate(session.get('steps') or [], 1) if s is ordered[-1]), None) if ordered else None
    changed = max([session.get('updated') or 0] + [max(e.get('since') or 0, e.get('ended') or 0) for e in session.get('steps') or [] if e.get('state')])
    idle = max(0, round(now - changed)) if changed else None
    busy = any(not r.get('done') for r in render or [])
    limit = UPLOAD_DEADLINE if (item or {}).get('waiting_upload') else DRAFT_DEADLINE + FINAL_DEADLINE if busy and ordered and ordered[-1]['id'] in ('draft', 'edit') else IDLE_LIMITS.get(ordered[-1]['id']) if ordered else None
    stalled = state == 'open' and not stopped and bool(ordered) and idle is not None and limit is not None and idle > limit
    outcome = {'kind':'stopped','at':changed or now,'why':stop_reason(item, spec)} if stopped else steps['outcome']
    if stopped:
        state = 'stopped'
    return {'idle':idle,'stalled':stalled,'outcome':outcome,'step':(steps['current'] or {}).get('label'),'step_index':position,'step_count':len([e for e in session.get('steps') or [] if e.get('state')]),'item':key,'title':title_of(item, spec),'state':state,'opened':session.get('opened'),'ended':session.get('ended') or (spec or {}).get('published_at') if state != 'open' else None,'percent':percent_of(session, state),'steps':steps,'agents':(steps['current'] or {}).get('agents') or [],'dialog':[{'at':row.get('at'),'who':row.get('who'),'text':scrub(row.get('text'))} for row in session.get('dialog') or []],'table':[{**{k:(scrub(v) if k == 'status' else v if k != 'url' or public(v) else None) for k, v in row.items()},'reason':reason_of(scrub(row['status'])[8:], row.get('target')) if row['status'].startswith('ошибка: ') else ''} for row in table_rows(spec, item, zone)] if spec else []}


def session_sub(session):
    current = session['current']
    if not current:
        return ''
    running = [agent for agent in current['agents'] if agent['status'] == 'running' and agent['action']]
    if running:
        agent = min(running, key=lambda agent: agent['started'] or 0)
        return agent['label'] + ': ' + agent['action']
    flying = [target for target in current['targets'] if target['status'] in ('running', 'checking')]
    if flying:
        target = flying[0]
        return target['name'] + ': ' + ('проверяю' if target['status'] == 'checking' else 'попытка ' + str(target['attempt'] or 1))
    return ''


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


def latest_specs(store, keys):
    latest = {}
    for start in range(0, len(keys), 30):
        for s in store.db.collection('specs').where('item', 'in', keys[start:start + 30]).stream():
            spec = s.to_dict()
            rank = (spec.get('updated', 0), spec.get('state') in ('done', 'partial', 'rejected', 'failed'))
            if spec['item'] not in latest or rank >= latest[spec['item']][0]:
                latest[spec['item']] = (rank, s.id)
    return {key:value[1] for key, value in latest.items()}


def due_text(spec, zone):
    try:
        return ' на ' + datetime.fromisoformat(spec['at'].replace('Z', '+00:00')).astimezone(ZoneInfo(zone)).strftime('%d.%m %H:%M')
    except (ValueError, TypeError, KeyError, AttributeError):
        return ''


def job_stage(item, state, answered, fallback):
    stage = pin_stage(item, state, answered) if item else None
    return stage[0].upper() + stage[1:] if stage else fallback


def snapshot(store, now, view='progress', skill=None, zone='Asia/Makassar'):
    if view == 'costs':
        return costs_view(store, now)
    if view == 'skills':
        return skills_view(store, now, skill)
    jobs = [{**s.to_dict(),'id':s.id} for s in store.db.collection('renders').where('state', 'in', ['queued', 'running']).stream()]
    collecting = [(s.id, s.to_dict()) for s in store.db.collection('items').where('status', '==', 'collecting').stream()]
    previews = [(s.id, s.to_dict()) for s in store.db.collection('specs').where('state', 'in', ['previewed', 'needs-edit']).limit(PREVIEW_LIMIT).stream()]
    specs = {s.id:s.to_dict() for s in store.db.collection('specs').where('published_at', '>=', now - MAX_AGE).stream()}
    for s in store.db.collection('specs').where('state', '==', 'partial').stream():
        spec = s.to_dict()
        spec['published_at'] = spec.get('published_at') or spec.get('started_at') or spec.get('updated') or 0
        if spec['published_at'] >= now - MAX_AGE and not spec.get('operation'):
            specs.setdefault(s.id, spec)
    specs = sorted((spec for spec in specs.values() if spec.get('state') in ('done', 'partial')), key=lambda spec: -spec['published_at'])[:LIMIT]
    busy = {job.get('item') for job in jobs}
    working = {s.id: s.to_dict() for s in store.db.collection('items').where('status', 'in', ['queued', 'drafting']).stream()}
    newest = newest_specs(s.to_dict() for s in store.db.collection('specs').where('state', 'in', [state for state, label in PIN_SPEC_STAGES]).stream())
    states = newest_states(spec for spec in newest.values())
    wanted = sorted((busy | set(working) | set(states) | {spec.get('item') for spec in specs} | {spec.get('item') for slug, spec in previews}) - {None})
    items = {s.id: s.to_dict() for s in store.db.get_all([store.db.document('items/' + key) for key in wanted]) if s.exists}
    live = {job.get('item') for job in jobs}
    jobs = [job for job in jobs if not dead_render(job, items.get(job.get('item'))) and not overdue(job, now)]
    busy = {job.get('item') for job in jobs}
    waiting = []
    for key, item in collecting:
        if key not in busy:
            count = len([m for m in item.get('messages', []) if m.get('photo') or m.get('video') or m.get('document')])
            waiting.append({'id':key,'title':title_of(item, newest.get(key)),'status':'collecting','wait':'Собираю материал' + (': ' + str(count) + ' влож.' if count else '') + '; жду «Готово»','targets':[label for name, label, _ in GROUPS if name in item.get('chosen', [])],'at':item.get('updated') or item.get('created') or 0})
    latest = latest_specs(store, sorted({spec.get('item') for slug, spec in previews} - {None}))
    for slug, spec in previews:
        item = items.get(spec.get('item'))
        at = (item or {}).get('updated') or (item or {}).get('created') or 0
        if not item or latest.get(spec.get('item')) != slug or spec.get('item') in busy or spec.get('deleted') or not spec.get('operation') and at < now - WAITING_AGE:
            continue
        operation = {'fix':'исправление','delete':'удаление'}.get(spec.get('operation'))
        stage = pin_stage(item, spec['state'], bool(spec.get('answered'))) or 'черновик показан'
        wait = 'Жду подтверждения: ' + operation if operation else stage[0].upper() + stage[1:]
        waiting.append({'id':spec.get('item'),'title':title_of(item, spec),'status':spec['state'],'wait':wait,'targets':targets_of(spec),'at':at})
    for key in sorted(working):
        item = items.get(key)
        if item and item.get('waiting_upload') and item.get('status') in ('queued', 'drafting') and key not in busy:
            waiting.append({'id':key,'title':title_of(item, newest.get(key)),'status':item['status'],'wait':'Жду загрузку большого файла','targets':[label for name, label, _ in GROUPS if name in item.get('chosen', [])],'at':item.get('updated') or item.get('created') or 0})
    for key in sorted(states):
        item = items.get(key)
        if states[key] == 'scheduled' and item and item.get('status') not in ('rejected', 'failed') and key not in busy:
            waiting.append({'id':key,'title':title_of(item, newest.get(key)),'status':'scheduled','wait':'Запланирован' + due_text(newest[key].get('spec') or {}, zone),'targets':targets_of(newest[key].get('spec') or newest[key]),'at':newest[key].get('updated') or 0})
    waiting = sorted(waiting, key=lambda row: (row['status'] != 'collecting', -row['at']))[:LIMIT]
    active = [{'id':job['id'],'item':job.get('item'),'title':title_of(items.get(job.get('item')), newest.get(job.get('item'))),'mode':job.get('mode'),'state':job['state'],'label':job_stage(items.get(job.get('item')), states.get(job.get('item')), bool((newest.get(job.get('item')) or {}).get('answered')), (job.get('progress') or {}).get('label','')),'sub':(job.get('progress') or {}).get('label',''),'since':(job.get('progress') or {}).get('since', now),'steps':steps_of(job, now)} for job in jobs]
    for key in sorted(set(working) | set(states)):
        item = items.get(key)
        stage = pin_stage(item, states.get(key), bool((newest.get(key) or {}).get('answered'))) if item and item.get('status') not in ('rejected', 'failed') and key not in busy and not stale(item, now, key in live) else None
        if stage in WORKING:
            active.append({'id':key,'item':key,'title':title_of(item, newest.get(key)),'mode':None,'state':'running','label':stage[0].upper() + stage[1:],'since':(((newest.get(key) or {}).get('started_at') if states.get(key) in ('running','rendering') else None) or (newest.get(key) or {}).get('updated') if states.get(key) in ('approved','running','rendering') else None) or item.get('updated') or item.get('created') or now,'steps':[]})
    published = [{'id':spec.get('slug') or spec.get('item'),'item':spec.get('item'),'title':title_of(items.get(spec.get('item')), spec),'at':spec['published_at'],'state':spec['state'],'links':[{'name':NAMES.get(target, 'Сайт' if target == 'blog' else target),'url':(result or {}).get('url')} for target, result in {**spec.get('results', {})}.items() if public((result or {}).get('url'))],'unfinished':[{'target':row['target'],'name':row['name'],'status':scrub(row['status'])} for row in table_rows(spec, items.get(spec.get('item')), zone) if row['url'] is None and row['status'] != 'опубликовано']} for spec in specs]
    try:
        sessions = sessions_of(store, list(dict.fromkeys(row.get('item') or row['id'] for row in active + waiting + published)))
    except Exception:
        logging.exception('sessions failed')
        sessions = {}
    merged = []
    for row in active:
        first = next((other for other in merged if other.get('item') == row.get('item') and row.get('item') in sessions), None)
        if first:
            first['steps'] = first['steps'] + row['steps']
            first['since'] = min(first['since'], row['since'])
        else:
            merged.append(row)
    active = merged
    active.sort(key=lambda row: row['since'])
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
        found = sessions.get(row.get('item') or row['id'])
        try:
            row['session'] = timeline(found, now, row.get('steps')) if found else None
            sub = session_sub(row['session']) if row['session'] else ''
        except Exception:
            logging.exception('session timeline failed')
            row['session'], sub = None, ''
        if sub and row in active:
            row['sub'] = sub
        if row in published and not row.get('at') and row['session']:
            row['at'] = row['session']['ended'] or row['session']['opened']
    try:
        listed = sorted(((snap.id, snap.to_dict()) for snap in store.db.collection('sessions').stream()), key=lambda row: -((row[1] or {}).get('opened') or 0))
        listed = [(key, doc) for key, doc in listed if isinstance(doc.get('steps'), list) and doc.get('item')]
        shown = [key for key, doc in listed[:SESSION_LIMIT]]
        have = {key: doc for key, doc in listed}
        extra = [key for key in dict.fromkeys(row.get('item') or row['id'] for row in active + waiting + published) if key and key not in shown]
        ids = shown + extra
        owners = {snap.id: snap.to_dict() for snap in store.db.get_all([store.db.document('items/' + key) for key in ids]) if snap.exists}
        slugs = latest_specs(store, sorted(ids))
        pairs = {key: store.get('specs/' + slugs[key]) for key in ids if key in slugs}
        views = []
        for key in ids:
            doc, item, spec = have.get(key), owners.get(key), pairs.get(key)
            if doc is None and spec and spec.get('state') in ('done', 'partial') and (spec.get('results') or spec.get('published')):
                doc = backfilled(key, item, spec)
            if doc is None and item and item.get('status') in ('collecting', 'queued', 'drafting'):
                doc = Store.session(None, key, item.get('created') or now, item)
            if doc is None:
                continue
            row = next((r for r in active if r.get('item') == key), None)
            try:
                view = session_view(key, doc, item, spec, now, zone, row.get('steps') if row else None)
                view['stage'] = pin_stage(item, states.get(key), bool((newest.get(key) or {}).get('answered'))) if item and view['state'] == 'open' else None
                views.append(view)
            except Exception:
                logging.exception('session view failed')
        sessions_list = sorted(views, key=lambda view: -(view['opened'] or 0))
    except Exception:
        logging.exception('sessions list failed')
        sessions_list = []
    return {'now':now,'view':'progress','active':active,'waiting':waiting,'published':published,'sessions':sessions_list}


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
        body = json.dumps(snapshot(store, now, view, request.args.get('skill'), config.get('timezone', 'Asia/Makassar')))
    except Exception:
        logging.exception('progress snapshot failed')
        return https_fn.Response('',status=503,headers=cors)
    return https_fn.Response(body,status=200,headers={**cors,'Content-Type':'application/json','Cache-Control':'no-store'})
