import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from google.cloud import firestore

import drafter
import platforms

NETWORKS = (('telegram', 'Telegram'), ('instagram', 'Instagram'), ('threads', 'Threads'))
DAYS = 7
SYSTEM = """You write for Roman, who tracks follower goals of his own social accounts. Input JSON: for each network the goal (may be null), the follower counts of the last days (oldest first, null when unknown), yesterday's change and followers left to the goal. Write for each network one short encouraging phrase in Russian, at most 220 characters: yesterday's fact, how much is left to the goal (skip when no goal), one concrete tip for the next days. No emoji, no closing question, no hashtags, no greeting. Return only JSON {"telegram":"...","instagram":"...","threads":"..."} with the networks present in the input."""


def fetch(config, store, network):
    if network == 'telegram':
        return int(platforms.telegram('getChatMemberCount', {'chat_id': config['channels']['tg-channel']}))
    saved = platforms.token(config, store, network)
    if network == 'instagram':
        reply = platforms.request('GET', platforms.IG + '/me', {'fields': 'followers_count'}, token=saved['access_token'])
        return int(reply['followers_count'])
    reply = platforms.request('GET', platforms.THREADS + '/' + str(saved['user_id']) + '/threads_insights', {'metric': 'followers_count'}, token=saved['access_token'])
    return int(reply['data'][0]['total_value']['value'])


def snapshots(store, limit=DAYS + 1):
    rows = store.db.collection('goal-snaps').order_by('date', direction=firestore.Query.DESCENDING).limit(limit).stream()
    return sorted((row.to_dict() for row in rows), key=lambda row: row['date'])


def targets(config, store):
    return {name: value for name, value in {**(config.get('goals') or {}), **(store.get('goals/targets') or {})}.items() if isinstance(value, int) and value > 0}


def set_goal(store, text):
    parts = text.split()[1:]
    names = {'tg': 'telegram', 'telegram': 'telegram', 'тг': 'telegram', 'телеграм': 'telegram', 'ig': 'instagram', 'instagram': 'instagram', 'инста': 'instagram', 'инстаграм': 'instagram', 'threads': 'threads', 'тредс': 'threads'}
    network = names.get(parts[0].lower()) if len(parts) == 2 else None
    if not network or not re.fullmatch(r'\d{1,9}', parts[1]):
        return 'Формат: /goal telegram 1000 (telegram, instagram или threads; 0 снимает цель).'
    def apply(value):
        if int(parts[1]):
            value[network] = int(parts[1])
        else:
            value.pop(network, None)
    store.change('goals/targets', apply)
    label = dict(NETWORKS)[network]
    return label + ': цель ' + parts[1] + '.' if int(parts[1]) else label + ': цель снята.'


def plain(text):
    text = platforms.EMOJI.sub('', str(text)).strip()
    return re.sub(r'[^.!]*\?\s*$', '', text).strip()


def phrases(config, store, rows):
    goal = targets(config, store)
    brief = {}
    for network, label in NETWORKS:
        counts = [row.get('counts', {}).get(network) for row in rows]
        known = [count for count in counts if count is not None]
        if not known:
            continue
        brief[network] = {'goal': goal.get(network), 'counts': counts, 'yesterday_change': known[-1] - known[-2] if len(known) > 1 else None, 'left': goal[network] - known[-1] if network in goal else None}
    if not brief:
        return {}
    raw = drafter.converse(config, SYSTEM, json.dumps(brief, ensure_ascii=False), 800, lambda usage: store.record_usage(None, 'goals', usage))
    value = json.loads(raw[raw.index('{'):raw.rindex('}') + 1])
    return {network: plain(value[network]) for network in brief if isinstance(value.get(network), str) and plain(value[network])}


def run(config, store):
    day = datetime.now(ZoneInfo(config.get('timezone', 'Asia/Makassar'))).date().isoformat()
    counts = {}
    for network, _ in NETWORKS:
        try:
            counts[network] = fetch(config, store, network)
        except Exception as failure:
            logging.error(json.dumps({'code': 'goals_fetch_failed', 'network': network, 'error': str(failure)[:200]}, ensure_ascii=False))
    if counts:
        store.put('goal-snaps/' + day, {'date': day, 'counts': counts})
    try:
        said = phrases(config, store, snapshots(store, DAYS))
        if said:
            store.put('goals/phrases', {'date': day, 'text': said})
    except Exception as failure:
        logging.error(json.dumps({'code': 'goals_phrase_failed', 'error': str(failure)[:200]}, ensure_ascii=False))


def view(config, store, now):
    rows = snapshots(store)
    goal = targets(config, store)
    said = store.get('goals/phrases') or {}
    networks = []
    for network, label in NETWORKS:
        seen = [(row['date'], row['counts'][network]) for row in rows if network in row.get('counts', {})]
        count = seen[-1][1] if seen else None
        networks.append({'id': network, 'name': label, 'count': count, 'goal': goal.get(network), 'day': count - seen[-2][1] if len(seen) > 1 else None, 'week': count - seen[0][1] if len(seen) > 1 else None, 'since': seen[0][0] if seen else None, 'phrase': (said.get('text') or {}).get(network), 'phrase_date': said.get('date')})
    return {'now': now, 'view': 'goals', 'networks': networks}
