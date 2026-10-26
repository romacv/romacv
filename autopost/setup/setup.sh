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
import subprocess
import sys
import tempfile
import urllib.request

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
PROJECT = 'resromcom-493b4'
REGION = 'us-central1'
BUCKET = 'gs://' + PROJECT + '-autopost'
RUNTIME = 'autopost-runtime@' + PROJECT + '.iam.gserviceaccount.com'
RENDER = 'autopost-render@' + PROJECT + '.iam.gserviceaccount.com'
BUDGET = 'autopost-budget@' + PROJECT + '.iam.gserviceaccount.com'
KEYS = Path.home() / 'Documents/keys/autopost'
ENV = dict(os.environ, CLOUDSDK_CORE_DISABLE_FILE_LOGGING='true', CLOUDSDK_CORE_LOG_HTTP='false', CLOUDSDK_CORE_VERBOSITY='error', CLOUDSDK_CORE_DISABLE_USAGE_REPORTING='true')


def say(message):
    print(message, flush=True)


def cloud(*args, data=None, missing=False):
    result = subprocess.run(['gcloud', '--project=' + PROJECT, '--quiet', '--verbosity=error', *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
    if result.returncode:
        if missing and (b'NOT_FOUND' in result.stderr or b'not found' in result.stderr.lower()):
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


def policies():
    with tempfile.TemporaryDirectory(prefix='autopost-setup-') as directory:
        root = Path(directory)
        files = {
            'lifecycle.json': {'rule': [{'action': {'type': 'Delete'}, 'condition': {'age': 2}}]},
            'cors.json': [{'origin': ['https://www.resrom.com'], 'method': ['PUT'], 'responseHeader': ['Content-Type', 'x-goog-if-generation-match'], 'maxAgeSeconds': 3600}],
            'cleanup.json': [{'name': 'autopost-delete-after-day', 'action': {'type': 'Delete'}, 'condition': {'tagState': 'any', 'olderThan': '86400s'}}],
        }
        for name, value in files.items():
            (root / name).write_text(json.dumps(value))
        say('Set uniform access, enforced public access prevention, two-day lifecycle and browser PUT CORS')
        cloud('storage', 'buckets', 'update', BUCKET, '--uniform-bucket-level-access', '--public-access-prevention', '--soft-delete-duration=0', '--lifecycle-file=' + str(root / 'lifecycle.json'), '--cors-file=' + str(root / 'cors.json'))
        for repo in ('gcf-artifacts', 'cloud-run-source-deploy'):
            ensure(('artifacts', 'repositories', 'describe', repo, '--location=' + REGION), ('artifacts', 'repositories', 'create', repo, '--location=' + REGION, '--repository-format=docker'))
            say('Set active one-day image cleanup on ' + repo)
            cloud('artifacts', 'repositories', 'set-cleanup-policies', repo, '--location=' + REGION, '--policy=' + str(root / 'cleanup.json'))


def apis():
    services = ['serviceusage', 'firebase', 'firebasehosting', 'firebaserules', 'firebasestorage', 'cloudfunctions', 'run', 'cloudbuild', 'artifactregistry', 'eventarc', 'pubsub', 'cloudtasks', 'cloudscheduler', 'secretmanager', 'speech', 'firestore', 'storage', 'iam', 'iamcredentials', 'cloudresourcemanager', 'cloudbilling', 'billingbudgets', 'logging']
    say('Enable APIs: ' + ', '.join(services))
    cloud('services', 'enable', *[service + '.googleapis.com' for service in services])


def infra():
    ensure(('firestore', 'databases', 'describe', '--database=(default)'), ('firestore', 'databases', 'create', '--database=(default)', '--location=' + REGION, '--type=firestore-native'))
    database = json.loads(cloud('firestore', 'databases', 'describe', '--database=(default)', '--format=json'))
    if database.get('locationId') != REGION or database.get('type') != 'FIRESTORE_NATIVE':
        raise RuntimeError('Existing default Firestore must be Native in us-central1; no migration attempted.')
    ensure(('storage', 'buckets', 'describe', BUCKET), ('storage', 'buckets', 'create', BUCKET, '--location=' + REGION, '--uniform-bucket-level-access', '--public-access-prevention'))
    bucket = json.loads(cloud('storage', 'buckets', 'describe', BUCKET, '--format=json'))
    if bucket.get('location', '').lower() != REGION or str(bucket.get('project_number')) != cloud('projects', 'describe', PROJECT, '--format=value(projectNumber)').decode().strip():
        raise RuntimeError('Bucket must belong to this project and be in us-central1.')
    ensure(('tasks', 'queues', 'describe', 'worker', '--location=' + REGION), ('tasks', 'queues', 'create', 'worker', '--location=' + REGION))
    say('Set worker queue to one attempt and one concurrent dispatch')
    cloud('tasks', 'queues', 'update', 'worker', '--location=' + REGION, '--max-attempts=1', '--max-concurrent-dispatches=1')
    ensure(('pubsub', 'topics', 'describe', 'budget-alerts'), ('pubsub', 'topics', 'create', 'budget-alerts'))
    ensure(('secrets', 'describe', 'AUTOPOST'), ('secrets', 'create', 'AUTOPOST', '--replication-policy=automatic'))
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
    budget = role('autopostBudget', ['resourcemanager.projects.get', 'resourcemanager.projects.deleteBillingAssignment', 'logging.logEntries.create'])
    for account in (RUNTIME, RENDER):
        grant(account, 'roles/datastore.user')
        bind(('storage', 'buckets', 'add-iam-policy-binding', BUCKET), account, 'roles/storage.objectAdmin')
        bind(('tasks', 'queues', 'add-iam-policy-binding', 'worker', '--location=' + REGION), account, 'roles/cloudtasks.enqueuer')
        bind(('iam', 'service-accounts', 'add-iam-policy-binding', RUNTIME), account, 'roles/iam.serviceAccountUser')
    grant(BUDGET, budget)
    bind(('secrets', 'add-iam-policy-binding', 'AUTOPOST'), RUNTIME, 'roles/secretmanager.secretAccessor')
    bind(('iam', 'service-accounts', 'add-iam-policy-binding', RUNTIME), RUNTIME, signer)
    if cloud('run', 'services', 'describe', 'worker', '--region=' + REGION, missing=True) is not None:
        bind(('run', 'services', 'add-iam-policy-binding', 'worker', '--region=' + REGION), RUNTIME, 'roles/run.invoker')
    else:
        say('worker is not deployed yet: rerun iam after owner deploy to bind private invocation.')
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


def config():
    result = {name: key(name + '.txt') for name in ('telegram_bot_token', 'webhook_secret', 'github_pat', 'github_noreply', 'anthropic_key', 'webhook_url', 'worker_url')}
    result.update({name: key(name + '.json', True) for name in ('channels', 'instagram', 'threads')})
    result.update({name: int(key(name + '.txt')) for name in ('telegram_bot_id', 'roman_user_id', 'roman_chat_id')})
    result.update(runtime_service_account=RUNTIME, bucket=PROJECT + '-autopost', timezone='UTC', speech_language='ru-RU')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', result['webhook_secret']):
        raise RuntimeError('Invalid webhook secret format.')
    if not result['github_noreply'].endswith('@users.noreply.github.com'):
        raise RuntimeError('GitHub noreply address required.')
    for name in ('webhook_url', 'worker_url'):
        if not result[name].startswith('https://'):
            raise RuntimeError('HTTPS function URLs required.')
    for provider in ('instagram', 'threads'):
        if set(result[provider]) != {'access_token', 'user_id', 'issued_at', 'expires_at'}:
            raise RuntimeError('Invalid provider key fields.')
    if set(result['channels']) != {'tg-work', 'tg-personal'}:
        raise RuntimeError('Both channel IDs required.')
    return result


def secret():
    say('Read owner-supplied key files privately; add a new AUTOPOST version without reading existing values')
    value = config()
    cloud('secrets', 'versions', 'add', 'AUTOPOST', '--data-file=-', data=json.dumps(value, sort_keys=True).encode())
    say('AUTOPOST updated. Owner must redeploy webhook, worker and daily to bind this version.')
    say('After verifying all deployed bindings, destroy the previous unused version manually in Console; free tier allows 6 active versions: https://console.cloud.google.com/security/secret-manager/secret/AUTOPOST/versions?project=resromcom-493b4')


def render():
    say('Build source and create/update private autopost-render job: 2 CPU, 2 GiB, 1800 s, one task, no retries; do not execute')
    cloud('run', 'jobs', 'deploy', 'autopost-render', '--source=autopost/render', '--region=' + REGION, '--service-account=' + RENDER, '--cpu=2', '--memory=2Gi', '--task-timeout=1800s', '--tasks=1', '--parallelism=1', '--max-retries=0')
    say('Owner: rerun infra for cleanup and iam for job invocation grants.')


def webhook():
    say('Register Telegram webhook with secret_token and message/callback_query/business_connection updates; retain pending updates')
    token = key('telegram_bot_token.txt')
    secret_token = key('webhook_secret.txt')
    url = key('webhook_url.txt')
    if not url.startswith('https://') or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', secret_token):
        raise RuntimeError('Invalid webhook settings.')
    payload = json.dumps({'url': url, 'secret_token': secret_token, 'allowed_updates': ['message', 'callback_query', 'business_connection']}).encode()
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/setWebhook', data=payload, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        if not json.load(response).get('ok'):
            raise RuntimeError('Webhook registration rejected.')
    say('Webhook registered.')


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


commands = {'apis': apis, 'infra': infra, 'iam': iam, 'secret': secret, 'render': render, 'webhook': webhook, 'budget-test': budget_test}
if len(sys.argv) != 2 or sys.argv[1] not in commands:
    sys.exit('Usage: autopost/setup/setup.sh {apis|infra|iam|secret|render|webhook|budget-test}')
try:
    commands[sys.argv[1]]()
except Exception:
    sys.exit('Setup failed; details suppressed to keep credentials private. Check key-file schema, owner permissions and documented prerequisites.')
PY
