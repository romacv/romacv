#!/bin/bash
set +x
set -euo pipefail
umask 077
cd "$(dirname "$0")/../.."
exec python3 - "$@" <<'PY'
import fcntl
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
PROJECT = 'resromcom-493b4'
REGION = 'us-central1'
BUCKET = 'gs://' + PROJECT + '-autopost'
RUNTIME = 'autopost-runtime@' + PROJECT + '.iam.gserviceaccount.com'
RENDER = 'autopost-render@' + PROJECT + '.iam.gserviceaccount.com'
BUDGET = 'autopost-budget@' + PROJECT + '.iam.gserviceaccount.com'
DATASET = 'billing_export'
KEYS = Path.home() / 'Documents/keys/autopost'
ENV = dict(os.environ, CLOUDSDK_CORE_DISABLE_FILE_LOGGING='true', CLOUDSDK_CORE_LOG_HTTP='false', CLOUDSDK_CORE_VERBOSITY='error', CLOUDSDK_CORE_DISABLE_USAGE_REPORTING='true')


def say(message):
    print(message, flush=True)


def cloud(*args, data=None, missing=False):
    result = subprocess.run(['gcloud', '--project=' + PROJECT, '--quiet', '--verbosity=error', *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
    if result.returncode:
        if missing and (b'NOT_FOUND' in result.stderr or b'not found' in result.stderr.lower() or b'cannot find' in result.stderr.lower()):
            return None
        raise RuntimeError('gcloud operation failed; output suppressed. Check owner access and prerequisites.')
    return result.stdout


def ensure(describe, create):
    if cloud(*describe, missing=True) is None:
        say('Creating ' + ' '.join(create[:4]))
        cloud(*create)
    else:
        say('Already exists: ' + ' '.join(describe[:4]))


def role(name, permissions):
    action = 'create' if cloud('iam', 'roles', 'describe', name, missing=True) is None else 'update'
    cloud('iam', 'roles', action, name, '--title=' + name, '--stage=GA', '--permissions=' + ','.join(permissions))
    return 'projects/' + PROJECT + '/roles/' + name


def grant(account, role_name):
    say('Grant ' + role_name + ' to ' + account + ' on project')
    cloud('projects', 'add-iam-policy-binding', PROJECT, '--member=serviceAccount:' + account, '--role=' + role_name, '--condition=None')


def bind(group, account, role_name):
    say('Grant ' + role_name + ' to ' + account + ' on ' + group[-1])
    condition = [] if group[0] == 'tasks' or group[:2] == ('run', 'jobs') else ['--condition=None']
    cloud(*group, '--member=serviceAccount:' + account, '--role=' + role_name, *condition)


def dataset_reader(account):
    say('Grant roles/bigquery.dataViewer to ' + account + ' on ' + DATASET)
    grant = 'GRANT `roles/bigquery.dataViewer` ON SCHEMA `' + PROJECT + '.' + DATASET + '` TO "serviceAccount:' + account + '"'
    result = subprocess.run(['bq', '--project_id=' + PROJECT, 'query', '--use_legacy_sql=false', '--location=US', grant], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
    if result.returncode:
        raise RuntimeError('bq operation failed; output suppressed. Check owner access and prerequisites.')


def policies():
    with tempfile.TemporaryDirectory(prefix='autopost-setup-') as directory:
        root = Path(directory)
        files = {
            'lifecycle.json': {'rule': [{'action': {'type': 'Delete'}, 'condition': {'age': 2}}]},
            'cors.json': [{'origin': ['https://www.resrom.com'], 'method': ['PUT'], 'responseHeader': ['Content-Type', 'x-goog-if-generation-match'], 'maxAgeSeconds': 3600}],
            'cleanup.json': [{'name': 'autopost-keep-latest', 'action': {'type': 'Keep'}, 'mostRecentVersions': {'keepCount': 1}}, {'name': 'autopost-delete-old', 'action': {'type': 'Delete'}, 'condition': {'tagState': 'any', 'olderThan': '3600s'}}],
        }
        for name, value in files.items():
            (root / name).write_text(json.dumps(value))
        say('Set uniform access, enforced public access prevention, two-day lifecycle and browser PUT CORS')
        cloud('storage', 'buckets', 'update', BUCKET, '--uniform-bucket-level-access', '--public-access-prevention', '--soft-delete-duration=0', '--lifecycle-file=' + str(root / 'lifecycle.json'), '--cors-file=' + str(root / 'cors.json'))
        for repo in ('gcf-artifacts', 'cloud-run-source-deploy'):
            ensure(('artifacts', 'repositories', 'describe', repo, '--location=' + REGION), ('artifacts', 'repositories', 'create', repo, '--location=' + REGION, '--repository-format=docker'))
            say('Keep only the newest image on ' + repo)
            cloud('artifacts', 'repositories', 'set-cleanup-policies', repo, '--location=' + REGION, '--policy=' + str(root / 'cleanup.json'))
            cloud('artifacts', 'repositories', 'delete-cleanup-policies', repo, '--location=' + REGION, '--policynames=autopost-delete-after-day', missing=True)


def apis():
    services = ['serviceusage', 'firebase', 'firebasehosting', 'firebaserules', 'firebasestorage', 'cloudfunctions', 'run', 'cloudbuild', 'artifactregistry', 'eventarc', 'pubsub', 'cloudtasks', 'cloudscheduler', 'secretmanager', 'speech', 'firestore', 'storage', 'iam', 'iamcredentials', 'cloudresourcemanager', 'cloudbilling', 'billingbudgets', 'logging', 'monitoring', 'bigquery']
    say('Enable APIs: ' + ', '.join(services))
    for start in range(0, len(services), 20):
        cloud('services', 'enable', *[service + '.googleapis.com' for service in services[start:start + 20]])


def infra():
    ensure(('firestore', 'databases', 'describe', '--database=(default)'), ('firestore', 'databases', 'create', '--database=(default)', '--location=' + REGION, '--type=firestore-native'))
    database = json.loads(cloud('firestore', 'databases', 'describe', '--database=(default)', '--format=json'))
    if database.get('locationId') != REGION or database.get('type') != 'FIRESTORE_NATIVE':
        raise RuntimeError('Existing default Firestore must be Native in us-central1; no migration attempted.')
    ensure(('storage', 'buckets', 'describe', BUCKET), ('storage', 'buckets', 'create', BUCKET, '--location=' + REGION, '--uniform-bucket-level-access', '--public-access-prevention'))
    bucket = json.loads(cloud('storage', 'buckets', 'describe', BUCKET, '--raw', '--format=json'))
    if bucket.get('location', '').lower() != REGION or str(bucket.get('projectNumber')) != cloud('projects', 'describe', PROJECT, '--format=value(projectNumber)').decode().strip():
        raise RuntimeError('Bucket must belong to this project and be in us-central1.')
    ensure(('tasks', 'queues', 'describe', 'worker', '--location=' + REGION), ('tasks', 'queues', 'create', 'worker', '--location=' + REGION))
    say('Set worker queue to one attempt and one concurrent dispatch')
    cloud('tasks', 'queues', 'update', 'worker', '--location=' + REGION, '--max-attempts=1', '--max-concurrent-dispatches=1')
    ensure(('pubsub', 'topics', 'describe', 'budget-alerts'), ('pubsub', 'topics', 'create', 'budget-alerts'))
    ensure(('secrets', 'describe', 'AUTOPOST'), ('secrets', 'create', 'AUTOPOST', '--replication-policy=automatic'))
    ensure(('secrets', 'describe', 'AUTOPOST_NOTIFY'), ('secrets', 'create', 'AUTOPOST_NOTIFY', '--replication-policy=automatic'))
    policies()


def retire_role(name, group):
    role_name = 'projects/' + PROJECT + '/roles/' + name
    policy = json.loads(cloud(group[0], 'get-iam-policy', group[1], '--format=json'))
    member = 'serviceAccount:' + RUNTIME
    if any(binding.get('role') == role_name and member in binding.get('members', []) for binding in policy.get('bindings', [])):
        cloud(group[0], 'remove-iam-policy-binding', group[1], '--member=' + member, '--role=' + role_name, '--condition=None')
    existing = cloud('iam', 'roles', 'describe', name, '--format=json', missing=True)
    if existing is not None and not json.loads(existing).get('deleted'):
        cloud('iam', 'roles', 'delete', name)


def iam():
    for name in ('autopost-runtime', 'autopost-render', 'autopost-budget'):
        ensure(('iam', 'service-accounts', 'describe', name + '@' + PROJECT + '.iam.gserviceaccount.com'), ('iam', 'service-accounts', 'create', name))
    retire_role('autopostMaintenance', ('projects', PROJECT))
    retire_role('autopostSecretMaintenance', ('secrets', 'AUTOPOST'))
    retire_role('autopostSpeech', ('projects', PROJECT))
    signer = role('autopostSignBlob', ['iam.serviceAccounts.signBlob'])
    run_job = role('autopostRunJob', ['run.jobs.run', 'run.jobs.runWithOverrides'])
    monitoring = role('autopostMonitoringRead', ['monitoring.timeSeries.list'])
    budget = role('autopostBudget', ['resourcemanager.projects.get', 'resourcemanager.projects.deleteBillingAssignment', 'logging.logEntries.create'])
    for account in (RUNTIME, RENDER):
        grant(account, 'roles/datastore.user')
        bind(('storage', 'buckets', 'add-iam-policy-binding', BUCKET), account, 'roles/storage.objectAdmin')
        bind(('tasks', 'queues', 'add-iam-policy-binding', 'worker', '--location=' + REGION), account, 'roles/cloudtasks.enqueuer')
        bind(('iam', 'service-accounts', 'add-iam-policy-binding', RUNTIME), account, 'roles/iam.serviceAccountUser')
    grant(BUDGET, budget)
    grant(BUDGET, 'roles/datastore.user')
    grant(RUNTIME, monitoring)
    grant(RUNTIME, 'roles/bigquery.jobUser')
    grant(RUNTIME, 'roles/artifactregistry.reader')
    dataset_reader(RUNTIME)
    bind(('secrets', 'add-iam-policy-binding', 'AUTOPOST'), RUNTIME, 'roles/secretmanager.secretAccessor')
    bind(('secrets', 'add-iam-policy-binding', 'AUTOPOST_NOTIFY'), RENDER, 'roles/secretmanager.secretAccessor')
    bind(('iam', 'service-accounts', 'add-iam-policy-binding', RUNTIME), RUNTIME, signer)
    if cloud('run', 'services', 'describe', 'worker', '--region=' + REGION, missing=True) is not None:
        bind(('run', 'services', 'add-iam-policy-binding', 'worker', '--region=' + REGION), RUNTIME, 'roles/run.invoker')
    else:
        say('worker is not deployed yet: rerun iam after owner deploy to bind private invocation.')
    if cloud('run', 'services', 'describe', 'budget-kill', '--region=' + REGION, missing=True) is not None:
        bind(('run', 'services', 'add-iam-policy-binding', 'budget-kill', '--region=' + REGION), BUDGET, 'roles/run.invoker')
    else:
        say('budget-kill is not deployed yet: rerun iam after deploy to bind its Pub/Sub trigger.')
    if cloud('run', 'jobs', 'describe', 'autopost-render', '--region=' + REGION, missing=True) is not None:
        bind(('run', 'jobs', 'add-iam-policy-binding', 'autopost-render', '--region=' + REGION), RUNTIME, run_job)
    else:
        say('autopost-render does not exist yet: rerun iam after render to bind job execution.')


def key(name, structured=False):
    path = KEYS / name
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise RuntimeError('Key files must be owner-owned regular files, mode 600, without symlinks.')
    value = path.read_text().strip()
    if not value:
        raise RuntimeError('A required key file is empty.')
    return json.loads(value) if structured else value


class IdsError(RuntimeError):
    pass


def write_id(name, value):
    path = KEYS / name
    structured = name.endswith('.json')
    if os.path.lexists(path):
        if key(name, structured) != value:
            raise IdsError(name + ' differs; refusing to overwrite.')
        return
    data = json.dumps(value) if structured else str(value)
    descriptor, temporary = tempfile.mkstemp(dir=KEYS)
    try:
        with os.fdopen(descriptor, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data + '\n')
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if key(name, structured) != value:
                raise IdsError(name + ' differs; refusing to overwrite.')
            return
        say(name)
    finally:
        os.unlink(temporary)


def ids():
    directory = KEYS.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
        raise IdsError('Keys directory must be owner-owned, mode 700, without symlinks.')
    token = key('telegram_bot_token.txt')

    def telegram(method, **payload):
        request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise IdsError('Telegram ' + method + ' rejected.')
        return result['result']

    bot_id = telegram('getMe')['id']
    write_id('telegram_bot_id.txt', str(bot_id))
    if telegram('getWebhookInfo').get('url'):
        raise IdsError('A webhook is set; getUpdates would fail. Remove the webhook before running ids.')
    users = {}
    for update in telegram('getUpdates'):
        message = update.get('message', {})
        if message.get('chat', {}).get('type') == 'private' and 'id' in message.get('from', {}):
            users[message['from']['id']] = message['chat']['id']
    if not users:
        raise IdsError('Send /start to the bot, then rerun.')
    if len(users) != 1:
        raise IdsError('Several users wrote to the bot; refusing to guess.')
    user_id, chat_id = next(iter(users.items()))
    write_id('roman_user_id.txt', str(user_id))
    write_id('roman_chat_id.txt', str(chat_id))
    channel = telegram('getChat', chat_id='@romacv777')
    if channel.get('type') != 'channel':
        raise IdsError('Telegram chat must be type channel.')
    member = telegram('getChatMember', chat_id=channel['id'], user_id=bot_id)
    if member.get('status') != 'administrator':
        raise IdsError('Bot is missing administrator status.')
    for right in ('can_post_messages', 'can_edit_messages', 'can_delete_messages'):
        if not member.get(right):
            raise IdsError('Bot is missing ' + right + '.')
    write_id('channels.json', {'tg-channel': channel['id']})


def meta_request(provider, url, **params):
    request = urllib.request.Request(url + '?' + urllib.parse.urlencode(params))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except Exception:
        raise IdsError(provider + ' API request failed; check token, permissions and network.') from None
    if not isinstance(result, dict) or 'error' in result:
        raise IdsError(provider + ' API request rejected.')
    return result


def meta_identity(provider, base, token):
    field = 'user_id' if provider == 'instagram' else 'id'
    result = meta_request(provider, base + '/me', fields=field + ',username', access_token=token)
    if 'data' in result:
        if not isinstance(result['data'], list) or len(result['data']) != 1:
            raise IdsError(provider + ' must return exactly one identity.')
        result = result['data'][0]
    if not isinstance(result, dict) or result.get('username') != 'romacv777' or not re.fullmatch(r'[0-9]+', str(result.get(field, ''))):
        raise IdsError(provider + ' must belong to romacv777 with a valid user ID.')
    return str(result[field])


def meta_times(provider, metadata, now):
    issued = metadata.get('issued_at')
    expires = metadata.get('expires_at')
    if type(issued) is not int or type(expires) is not int or not 0 < issued <= now < expires:
        raise IdsError(provider + ' token issue/expiry times are missing, invalid or expired.')
    return issued, expires


def meta():
    directory = KEYS.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
        raise IdsError('Keys directory must be owner-owned, mode 700, without symlinks.')
    platforms = Path('autopost/functions/platforms.py').read_text()
    instagram_base = re.search(r"^IG = '(https://graph\.instagram\.com/v[0-9]+\.[0-9]+)'$", platforms, re.M)
    if not instagram_base:
        raise IdsError('Cannot determine Instagram API version from platforms.py.')
    present = False
    for provider, base, prefix in (('threads', 'https://graph.threads.net/v1.0', 'th'), ('instagram', instagram_base[1], 'ig')):
        raw_name = provider + '_token.txt'
        name = provider + '.json'
        if not os.path.lexists(KEYS / raw_name):
            continue
        present = True
        token = key(raw_name)
        original_token = token
        saved = key(name, True) if os.path.lexists(KEYS / name) else None
        if saved is not None and (not isinstance(saved, dict) or saved.get('access_token') != token):
            raise IdsError(name + ' differs; refusing to overwrite. Delete it before rerunning meta to replace the token.')
        user_id = meta_identity(provider, base, token)
        now = int(time.time())
        if provider == 'threads':
            metadata = meta_request(provider, base + '/debug_token', input_token=token, access_token=token).get('data', {})
            if not isinstance(metadata, dict) or metadata.get('is_valid') is not True or str(metadata.get('user_id')) != user_id:
                raise IdsError('threads token metadata is invalid or belongs to another user.')
            issued, expires = meta_times(provider, metadata, now)
        elif saved is not None:
            issued, expires = meta_times(provider, saved, now)
        else:
            issued, expires = now, now + 60 * 86400
        if expires - issued <= 3600:
            if saved is not None:
                raise IdsError(name + ' contains a short-lived token; delete it before rerunning meta.')
            secret_name = provider + '_app_secret.txt'
            if not os.path.lexists(KEYS / secret_name):
                raise IdsError('Short-lived ' + provider + ' token requires ' + secret_name + '; save it privately and rerun meta.')
            secret_value = key(secret_name)
            issued = int(time.time())
            reply = meta_request(provider, base.rsplit('/', 1)[0] + '/access_token', grant_type=prefix + '_exchange_token', client_secret=secret_value, access_token=token)
            new_token = reply.get('access_token')
            lifetime = reply.get('expires_in')
            if not isinstance(new_token, str) or not new_token or type(lifetime) is not int or lifetime <= 3600:
                raise IdsError(provider + ' exchange did not return a long-lived token and expiry.')
            token = new_token
            expires = issued + lifetime
            if meta_identity(provider, base, token) != user_id:
                raise IdsError(provider + ' exchanged token identity changed.')
        value = {'access_token': token, 'user_id': user_id, 'issued_at': issued, 'expires_at': expires}
        if provider == 'threads':
            value['scopes'] = metadata.get('scopes', [])
        write_id(name, value)
        if key(name, True).get('access_token') == token and key(raw_name) == original_token:
            (KEYS / raw_name).unlink()
        say(provider + ' setup complete.')
    if not present:
        say('No raw Meta token files present; save threads_token.txt or instagram_token.txt privately, then rerun meta.')


def config():
    result = {name: key(name + '.txt') for name in ('telegram_bot_token', 'webhook_secret', 'github_pat', 'github_noreply', 'anthropic_key', 'webhook_url', 'worker_url')}
    result.update({name: key(name + '.json', True) for name in ('channels', 'instagram', 'threads')})
    result.update({name: int(key(name + '.txt')) for name in ('telegram_bot_id', 'roman_user_id', 'roman_chat_id')})
    result.update(runtime_service_account=RUNTIME, bucket=PROJECT + '-autopost', timezone='Asia/Makassar', speech_language='ru-RU')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', result['webhook_secret']):
        raise RuntimeError('Invalid webhook secret format.')
    if not result['github_noreply'].endswith('@users.noreply.github.com'):
        raise RuntimeError('GitHub noreply address required.')
    for name in ('webhook_url', 'worker_url'):
        if not result[name].startswith('https://'):
            raise RuntimeError('HTTPS function URLs required.')
    for provider in ('instagram', 'threads'):
        fields = set(result[provider])
        required = {'access_token', 'user_id', 'issued_at', 'expires_at'}
        if not required <= fields or fields - required - ({'scopes'} if provider == 'threads' else set()):
            raise RuntimeError('Invalid provider key fields.')
    if set(result['channels']) != {'tg-channel'}:
        raise RuntimeError('Telegram channel ID required.')
    return result


def secret():
    say('Read owner-supplied key files privately; add a new AUTOPOST version without reading existing values')
    value = config()
    cloud('secrets', 'versions', 'add', 'AUTOPOST', '--data-file=-', data=json.dumps(value, sort_keys=True).encode())
    say('AUTOPOST updated. Owner must redeploy webhook, worker and daily to bind this version.')
    say('After verifying all deployed bindings, destroy the previous unused version manually in Console; free tier allows 6 active versions: https://console.cloud.google.com/security/secret-manager/secret/AUTOPOST/versions?project=resromcom-493b4')


def notify():
    say('Add an AUTOPOST_NOTIFY version from the owner telegram_bot_token.txt without printing it; AUTOPOST stays unchanged')
    cloud('secrets', 'versions', 'add', 'AUTOPOST_NOTIFY', '--data-file=-', data=key('telegram_bot_token.txt').encode())
    say('AUTOPOST_NOTIFY updated. Order: infra, notify, iam, render. Rerun notify only if the bot token changes, then redeploy render.')


def render():
    say('Build source and create/update private autopost-render job: 2 CPU, 2 GiB, 1800 s, one task, no retries; do not execute')
    cloud('run', 'jobs', 'deploy', 'autopost-render', '--source=autopost/render', '--region=' + REGION, '--service-account=' + RENDER, '--cpu=2', '--memory=2Gi', '--task-timeout=1800s', '--tasks=1', '--parallelism=1', '--max-retries=0', '--set-secrets=TELEGRAM_BOT_TOKEN=AUTOPOST_NOTIFY:latest')
    say('Owner: rerun infra for cleanup and iam for job invocation grants.')


def webhook():
    say('Register Telegram webhook with secret_token and message/callback_query/business_connection updates; retain pending updates; set the commands and the Автопостер menu button')
    token = key('telegram_bot_token.txt')
    secret_token = key('webhook_secret.txt')
    url = key('webhook_url.txt')
    if not url.startswith('https://') or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', secret_token):
        raise RuntimeError('Invalid webhook settings.')
    menu = [{'command': 'post', 'description': 'Новый пост'}, {'command': 'skills', 'description': 'Навыки бота'}]
    for method, body in (('setWebhook', {'url': url, 'secret_token': secret_token, 'allowed_updates': ['message', 'callback_query', 'business_connection']}), ('setMyCommands', {'commands': menu}), ('setChatMenuButton', {'menu_button': {'type': 'web_app', 'text': 'Автопостер', 'web_app': {'url': 'https://www.resrom.com/p'}}})):
        request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            if not json.load(response).get('ok'):
                raise RuntimeError(method + ' rejected.')
    say('Webhook, commands and menu button registered.')


def budget_test():
    receipt = Path.home() / 'Library/Application Support/autopost/setup/budget-test.sent'
    receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with receipt.open('a+') as marker:
        fcntl.flock(marker, fcntl.LOCK_EX)
        marker.seek(0)
        if marker.read().strip() == 'published':
            say('Sample already published; skip. Owner can remove the documented receipt for a new drill.')
            return
        publish_budget_sample()
        marker.write('published\n')
        marker.flush()
        os.fsync(marker.fileno())


def publish_budget_sample():
    say('Publish Google sample budget notification to budget-alerts')
    payload = {'budgetDisplayName': 'name-of-budget', 'alertThresholdExceeded': 1.0, 'costAmount': 100.01, 'costIntervalStart': '2019-01-01T00:00:00Z', 'budgetAmount': 100.00, 'budgetAmountType': 'SPECIFIED_AMOUNT', 'currencyCode': 'USD'}
    cloud('pubsub', 'topics', 'publish', 'budget-alerts', '--message=' + json.dumps(payload), '--attribute=billingAccountId=012345-567890-ABCDEF,budgetId=defff-abcde-12345-2134,schemaVersion=1.0')
    say('Sample published. Inspect disable-billing logs for the simulated result; local source does not prove deployed simulation mode.')


commands = {'apis': apis, 'infra': infra, 'iam': iam, 'ids': ids, 'meta': meta, 'secret': secret, 'notify': notify, 'render': render, 'webhook': webhook, 'budget-test': budget_test}
if len(sys.argv) != 2 or sys.argv[1] not in commands:
    sys.exit('Usage: autopost/setup/setup.sh {apis|infra|iam|ids|meta|secret|notify|render|webhook|budget-test}')
try:
    commands[sys.argv[1]]()
except IdsError as error:
    sys.exit(str(error))
except Exception:
    sys.exit('Setup failed; details suppressed to keep credentials private. Check key-file schema, owner permissions and documented prerequisites.')
PY
