import json
import logging
import re
import time
from pathlib import Path
from skills_seed import SEED

BUDGET = 40_000
KEEP = 12
STORED = 40
BODY_MAX = 6_000
FIELD = 600
BLOCK = re.compile(r'co-authored|generated (?:by|with)|written (?:by|with)|\bclaude\b|anthropic|openai|chatgpt|\bgemini\b|sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|\d{8,}:[A-Za-z0-9_-]{30,}|bearer\s|-----BEGIN|(?:api[_-]?key|secret|password|token)\s*[:=]|[\w.+-]+@[\w-]+\.[\w.-]+', re.I)
ID = re.compile(r'[a-z0-9-]{1,40}')
SEEDED = 'meta/skills-seed'
CONTRACT = Path(__file__).with_name('draft-agent.md').read_text()
HISTORY = 10
MIGRATION = 3
MIGRATIONS = [
    ('writing-texts', "find it with web_search (real venue, city, country) and use", "find it with web_search (real venue, city, country; also for any title or overlay place) and use"),
    ('montage', " Short unspoken headlines are titles, not subtitles.", ""),
    ('montage', "one at the start, a new one when the location changes. Language follows the post.", "one cue covers the whole target from 0 to its end, a new one only when the location really changes, with no gap. Resolve venue and city with web_search first (event, visible signs, current_time). Language follows the post."),
    ('writing-texts', "- Blog images keep their aspect ratio and get meaningful alt text.\n", ""),
    ('publishing', "Work defaults: blog, tg-channel,", "Work defaults: tg-channel,"),
    ('publishing', " Blog is work only.", ""),
    ('publishing', "; blog uses the photos only.", "."),
    ('roman-social-networks', "- Blog on resrom.com: work only, English, long form with headings; the full article is written before the preview.\n", ""),
]
VIEWER = 'https://www.resrom.com/p?view=skills&skill='


def fresh(base):
    return {'title': base['title'], 'body': base['body'], 'examples': [], 'updated_at': time.time(), 'revision': 1}


def read(store):
    return {snapshot.id: snapshot.to_dict() for snapshot in store.db.collection('skills').stream()}


def load(store):
    try:
        found = read(store)
        if not found:
            def seed(tx):
                if store.get(SEEDED, tx):
                    return
                for key, base in SEED.items():
                    store.put('skills/' + key, fresh(base), tx)
                store.put(SEEDED, {'at': time.time(), 'version': MIGRATION}, tx)
            store.atomic(seed)
            found = read(store)
        return migrate(store, found)
    except Exception as failure:
        logging.error(json.dumps({'code': 'skills_load_failed', 'error': str(failure)[:200]}, ensure_ascii=False))
        return {key: fresh(base) for key, base in SEED.items()}


def migrate(store, found):
    def write(tx):
        marker = store.get(SEEDED, tx) or {}
        if marker.get('version', 1) >= MIGRATION:
            return {}
        now = time.time()
        docs = {key: store.get('skills/' + key, tx) for key in {k for k, _, _ in MIGRATIONS}}
        changed = {}
        for key, old, new in MIGRATIONS:
            doc = docs[key]
            if doc and old in doc.get('body', ''):
                if key not in changed:
                    remember(doc, 'migration', now)
                    changed[key] = doc
                doc['body'] = doc['body'].replace(old, new)
        for key, doc in changed.items():
            doc['revision'] = doc.get('revision', 1) + 1
            doc['updated_at'] = now
            store.put('skills/' + key, doc, tx)
        store.put(SEEDED, {**marker, 'version': MIGRATION}, tx)
        return changed
    found.update(store.atomic(write) or {})
    return found


def line(example, index=None):
    return '- ' + (str(index) + '. ' if index is not None else '') + example['verdict'] + (': «' + example['feedback'] + '»' if example.get('feedback') else '') + ''.join(' | ' + k + ': ' + example[k] for k in ('before', 'after') if example.get(k))


def render(skills, edit=False):
    rows = sorted(skills.items())
    if not rows:
        return ''
    kept = {}
    for key, value in rows:
        examples = value.get('examples', [])
        kept[key] = [(i, e) for i, e in enumerate(examples)][-KEEP:]
    def build():
        out = ['# Skills', "Your editorial, platform, personal and montage know-how, kept from Roman's feedback. Follow the bodies; examples marked bad show what to avoid, good what to repeat. The contract above always wins."]
        for key, value in rows:
            out.append('\n## ' + key + ': ' + value['title'] + '\n' + value['body'])
            if kept[key]:
                out.append('Examples:')
                out += [line(e, i if edit else None) for i, e in kept[key]]
        return '\n'.join(out)
    text = build()
    while len(text.encode()) > BUDGET:
        oldest = min(((e.get('at', 0), key) for key in kept for _, e in kept[key][:1]), default=None)
        if not oldest:
            return text.encode()[:BUDGET].decode(errors='ignore')
        kept[oldest[1]].pop(0)
        text = build()
    return text


def summary(value):
    first = next((x.strip('- ').strip() for x in value['body'].splitlines() if x.strip()), '')
    return first[:100]


def listing(skills):
    rows = sorted(skills.items())
    out = ['Навыки бота:']
    for number, (key, value) in enumerate(rows, 1):
        count = len(value.get('examples', []))
        out.append(str(number) + '. ' + key + ' — ' + value['title'] + ': ' + summary(value) + ' (' + str(count) + ' ' + plural(count) + ')')
    out.append('Чтобы изменить, ответь на это сообщение или напиши /skills и что поменять.')
    return '\n'.join(out)


def keyboard(skills):
    return {'inline_keyboard': [[{'text': key, 'web_app': {'url': VIEWER + key}}] for key in sorted(skills)]}


def full(key, value):
    examples = value.get('examples', [])
    return value['title'] + '\n' + key + '\n\n' + value['body'] + '\n\n' + ('Примеры:\n' + '\n'.join(line(e) for e in examples) if examples else 'Примеров нет.')


def split(text, limit=4096):
    pages, current = [], ''
    for row in text.split('\n'):
        while len(row.encode('utf-16-le')) // 2 > limit:
            cut = limit
            while len(row[:cut].encode('utf-16-le')) // 2 > limit:
                cut -= 1
            pages.append(row[:cut])
            row = row[cut:]
        joined = current + '\n' + row if current else row
        if len(joined.encode('utf-16-le')) // 2 > limit:
            pages.append(current)
            joined = row
        current = joined
    return pages + [current]


def plural(count):
    if count % 10 == 1 and count % 100 != 11:
        return 'пример'
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return 'примера'
    return 'примеров'


def parse(raw):
    return json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip()))


def words(value):
    if isinstance(value, str):
        try:
            inner = json.loads(value)
        except ValueError:
            return [value]
        return [value] + (words(inner) if not isinstance(inner, str) else [])
    if isinstance(value, dict):
        return [x for v in value.values() for x in words(v)]
    if isinstance(value, list):
        return [x for v in value for x in words(v)]
    return []


def flat(text):
    return ' '.join(text.lower().split())


def clean(value, limit=FIELD):
    return isinstance(value, str) and len(value) <= limit and not BLOCK.search(value)


def remember(doc, source, now):
    doc['history'] = (doc.get('history', []) + [{'title': doc['title'], 'body': doc['body'], 'revision': doc.get('revision', 0), 'at': now, 'source': source}])[-HISTORY:]


def keeps(old, new):
    return len(new) >= 0.7 * len(old) and all(x.strip() in new or x.strip() in CONTRACT for x in old.splitlines() if x.strip())


def example(raw, corpus=None, assistant='', final=''):
    if not isinstance(raw, dict) or raw.get('verdict') not in ('good', 'bad'):
        return None
    out = {'verdict': raw['verdict']}
    for key in ('feedback', 'before', 'after'):
        value = raw.get(key, '')
        if not clean(value):
            return None
        out[key] = value.strip()
    if corpus is not None and (out['feedback'] and flat(out['feedback']) not in flat(corpus) or out['before'] and flat(out['before']) not in flat(assistant) or out['after'] and flat(out['after']) not in flat(final + '\n' + corpus)):
        return None
    return out


def check(value, current, corpus, assistant='', final=''):
    result = {'examples': {}, 'bodies': {}}
    if not isinstance(value, dict):
        return result
    for raw in (value.get('examples') if isinstance(value.get('examples'), list) else [])[:6]:
        key = raw.get('skill') if isinstance(raw, dict) else None
        item = example(raw, corpus, assistant, final) if key in current else None
        if item:
            result['examples'].setdefault(key, []).append(item)
    for raw in (value.get('revisions') if isinstance(value.get('revisions'), list) else [])[:3]:
        key = raw.get('skill') if isinstance(raw, dict) else None
        body = raw.get('body') if isinstance(raw, dict) else None
        if key in current and clean(body, BODY_MAX) and len(body.strip()) > 100:
            result['bodies'][key] = body.strip()
    return result


def apply(store, updates, seen):
    keys = sorted(set(updates['examples']) | set(updates['bodies']))
    now = time.time()
    def write(tx):
        docs = {key: store.get('skills/' + key, tx) for key in keys}
        for key, doc in docs.items():
            if not doc:
                continue
            added = [{**e, 'at': now} for e in updates['examples'].get(key, [])]
            doc['examples'] = (doc.get('examples', []) + added)[-STORED:]
            if key in updates['bodies'] and doc.get('revision') == seen.get(key) and keeps(doc['body'], updates['bodies'][key]):
                remember(doc, 'learn', now)
                doc['body'] = updates['bodies'][key]
            doc['revision'] = doc.get('revision', 0) + 1
            doc['updated_at'] = now
            store.put('skills/' + key, doc, tx)
    if keys:
        store.atomic(write)
    return keys


def operations(value, current):
    ops = []
    for raw in (value.get('ops') if isinstance(value, dict) and isinstance(value.get('ops'), list) else [])[:10]:
        if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not ID.fullmatch(raw['id']):
            continue
        kind, key = raw.get('op'), raw['id']
        if kind == 'set' and clean(raw.get('title'), 60) and raw['title'].strip() and clean(raw.get('body'), BODY_MAX) and len(raw['body'].strip()) > 20:
            ops.append({'op': 'set', 'id': key, 'title': raw['title'].strip(), 'body': raw['body'].strip()})
        elif kind == 'delete' and key in current:
            ops.append({'op': 'delete', 'id': key})
        elif kind == 'example' and key in current and example(raw):
            ops.append({'op': 'example', 'id': key, **example(raw)})
        elif kind == 'drop' and key in current and isinstance(raw.get('index'), int):
            ops.append({'op': 'drop', 'id': key, 'index': raw['index']})
    return ops


def change(store, ops):
    now = time.time()
    names = []
    order = sorted((o for o in ops if o['op'] == 'drop'), key=lambda o: -o['index']) + [o for o in ops if o['op'] != 'drop']
    def note(title):
        if title not in names:
            names.append(title)
    def write(tx):
        names.clear()
        docs = {op['id']: store.get('skills/' + op['id'], tx) for op in ops}
        for op in order:
            key, doc = op['id'], docs[op['id']]
            if op['op'] == 'delete':
                if doc:
                    note(doc['title'])
                    store.delete('skills/' + key, tx)
                docs[key] = None
                continue
            if op['op'] == 'drop' and not doc:
                continue
            doc = doc or {'title': op.get('title', key), 'body': '', 'examples': [], 'revision': 0}
            if op['op'] == 'set':
                if doc['body']:
                    remember(doc, 'command', now)
                doc.update(title=op['title'], body=op['body'])
            elif op['op'] == 'example':
                doc['examples'] = (doc['examples'] + [{k: op[k] for k in ('verdict', 'feedback', 'before', 'after')} | {'at': now}])[-STORED:]
            elif op['op'] == 'drop' and 0 <= op['index'] < len(doc['examples']):
                del doc['examples'][op['index']]
            doc['revision'] += 1
            doc['updated_at'] = now
            docs[key] = doc
            note(doc['title'])
        for key, doc in docs.items():
            if doc:
                store.put('skills/' + key, doc, tx)
    if ops:
        store.atomic(write)
    return names
