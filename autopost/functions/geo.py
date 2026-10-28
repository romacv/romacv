import logging
import re

import platforms

PAGES = 'https://graph.facebook.com/v25.0/pages/search'
CACHE = {}


def query(places):
    rows = [p for p in (places or {}).get('places', []) if isinstance(p, dict) and p.get('confidence') == 'high']
    named = next((', '.join(str(p[k]) for k in ('name', 'city') if p.get(k)) for p in rows if p.get('name') and p.get('city')), None)
    return named or next((str(p['city']) for p in rows if p.get('city')), None)


def search(config, text, network):
    if network == 'threads':
        token = platforms.key('threads-token.json')['access_token']
        found = platforms.request('GET', platforms.THREADS + '/location_search', {'q': text, 'fields': 'id,name,city'}, token=token)
        return next((str(r['id']) for r in found.get('data', []) if re.fullmatch(r'[0-9]+', str(r.get('id', '')))), None)
    token = config.get('pages_search_token')
    if not token:
        return None
    found = platforms.request('GET', PAGES, {'q': text, 'fields': 'id,name,location'}, token=token)
    return next((str(r['id']) for r in found.get('data', []) if r.get('location') and re.fullmatch(r'[0-9]+', str(r.get('id', '')))), None)


def location(config, text, network):
    text = ' '.join(str(text or '').split())
    if not text or network not in ('instagram', 'threads'):
        return None
    candidates = [text] + ([text.rsplit(',', 1)[1].strip()] if ',' in text else [])
    for candidate in candidates:
        slot = (network, candidate.lower())
        if slot not in CACHE:
            fixed = ((config or {}).get('locations') or {}).get(network, {}).get(candidate.lower())
            try:
                CACHE[slot] = str(fixed) if fixed else search(config or {}, candidate, network)
            except Exception as failure:
                logging.warning('geo ' + network + ': ' + type(failure).__name__)
                continue
        if CACHE[slot]:
            return CACHE[slot]
    return None
