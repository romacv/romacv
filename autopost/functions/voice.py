import contextvars
import logging
import re
import threading
import time
from datetime import datetime, timezone

import platforms
import skills

KEY = 'voice-texts'
PATH = 'skills/' + KEY
TITLE = 'Мои тексты'
PER_NETWORK = 30
FIRST = 30
LATER = 10
MAX_AGE = 86400
BACKOFF = 6 * 3600
CAP = 8
STATE = 'meta/voice'
NETWORKS = {'tg-channel': 'telegram', 'threads': 'threads', 'ig-post': 'instagram', 'ig-reel': 'instagram'}
TAGS = re.compile(r'(?<!\w)#\w+')
HEAD = "Roman's own recent posts (examples follow under the skill): the voice to match in length, rhythm, vocabulary, humour and first-person directness. Never copy a phrase."


def clean(text):
    text = TAGS.sub('', text if isinstance(text, str) else '')
    return re.sub(r'[ \t]+\n', '\n', text).strip()


def stamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00').replace('+0000', '+00:00')).timestamp()
    except ValueError:
        return 0


def graph(provider, edge, field, count, timeout=20):
    token, user, base = platforms.social_session(provider)
    reply = platforms.request('GET', f'{base}/{user}/{edge}', {'fields': field + ',timestamp', 'limit': count}, token=token, timeout=timeout)
    return [(row.get(field), stamp(row.get('timestamp'))) for row in reply.get('data', [])]


def channel(store, after):
    found = []
    for snapshot in store.db.collection('specs').where('published_at', '>=', after).stream():
        saved = snapshot.to_dict()
        if saved.get('operation') or saved.get('deleted') or saved.get('state') not in ('done', 'partial') or 'tg-channel' not in (saved.get('results') or {}):
            continue
        text = ((saved.get('spec') or {}).get('copy') or {}).get('tg-channel')
        if isinstance(text, str):
            found.append((text, saved['published_at']))
    return found


def merge(store, added, cursors):
    if not added:
        return 0
    skills.load(store)
    now = time.time()
    def write(doc):
        if not doc.get('title'):
            doc.update(title=TITLE, body=HEAD, examples=[], revision=1)
        known = {(row['network'], row['text']) for row in doc.get('texts', [])}
        texts = list(doc.get('texts', []))
        for network, text, at in added:
            text = clean(text)
            if text and (network, text) not in known:
                known.add((network, text))
                texts.append({'network': network, 'text': text, 'at': datetime.fromtimestamp(at or now, timezone.utc).isoformat()})
        kept = []
        for network in {row['network'] for row in texts}:
            kept += sorted((row for row in texts if row['network'] == network), key=lambda row: row['at'], reverse=True)[:PER_NETWORK]
        doc['texts'] = kept
        doc['cursors'] = {**doc.get('cursors', {}), **cursors}
        doc['updated_at'] = now
        return len(kept)
    return store.change(PATH, write)


def published(store, spec, results):
    added = [(NETWORKS[target], text, time.time()) for target, text in (spec.get('copy') or {}).items() if target in NETWORKS and target in results and isinstance(text, str)]
    if added:
        merge(store, added, {})


def refresh(config, store, timeout=20, networks=None):
    saved = store.get(PATH) or {}
    state = store.get(STATE) or {}
    cursors = saved.get('cursors', {})
    ok, failed = dict(state.get('ok', {})), dict(state.get('failed', {}))
    now = time.time()
    added, moved = [], {}
    sources = {
        'telegram': lambda: channel(store, cursors.get('telegram', 0)),
        'threads': lambda: graph('threads', 'threads', 'text', LATER if cursors.get('threads') else FIRST, timeout),
        'instagram': lambda: graph('instagram', 'media', 'caption', LATER if cursors.get('instagram') else FIRST, timeout),
    }
    for network, fetch in sources.items():
        if networks is not None and network not in networks:
            continue
        try:
            rows = fetch()
        except Exception as failure:
            logging.error('voice_source_failed %s %s', network, str(failure)[:100])
            failed[network] = now
            continue
        ok[network] = now
        failed.pop(network, None)
        fresh = [(network, text, at) for text, at in rows if at > cursors.get(network, 0)]
        if fresh:
            added += fresh
            moved[network] = max(at for _, _, at in fresh)
    merge(store, added, moved)
    store.put(STATE, {'ok': ok, 'failed': failed})
    return len(added)


def due(store):
    state = store.get(STATE) or {}
    now = time.time()
    return {n for n in ('telegram', 'threads', 'instagram') if now - state.get('ok', {}).get(n, 0) > MAX_AGE and now - state.get('failed', {}).get(n, 0) > BACKOFF}


def ensure(config, store):
    try:
        networks = due(store)
        if networks:
            worker = threading.Thread(target=contextvars.copy_context().run, args=(refresh, config, store, CAP, networks), daemon=True)
            worker.start()
            worker.join(CAP)
    except Exception as failure:
        logging.error('voice_refresh_failed %s', str(failure)[:100])
