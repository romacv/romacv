import json
import logging
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import platforms

PAGES = 'https://graph.facebook.com/v25.0/pages/search'
PLACES = 'https://places.googleapis.com/v1/places:searchText'
FIELDS = 'places.displayName,places.formattedAddress,places.location,places.googleMapsUri'
PROJECT = 'resromcom-493b4'
CACHE = {}
FOUND = {}


def query(places):
    rows = [p for p in (places or {}).get('places', []) if isinstance(p, dict) and p.get('confidence') == 'high']
    named = next((', '.join(str(p[k]) for k in ('name', 'city') if p.get(k)) for p in rows if p.get('name') and p.get('city')), None)
    return named or next((str(p['city']) for p in rows if p.get('city')), None)


def maps(url):
    cid = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('cid')
    return 'https://maps.google.com/?cid=' + cid[0] if cid and re.fullmatch(r'[0-9]+', cid[0]) else url


def fetch(text):
    import google.auth
    from google.auth.transport.requests import Request
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    credentials.refresh(Request())
    headers = {'Authorization': 'Bearer ' + credentials.token, 'X-Goog-User-Project': PROJECT, 'X-Goog-FieldMask': FIELDS, 'Content-Type': 'application/json'}
    body = json.dumps({'textQuery': text, 'pageSize': 1, 'languageCode': 'ru'}).encode()
    with urllib.request.urlopen(urllib.request.Request(PLACES, data=body, headers=headers, method='POST'), timeout=8) as response:
        rows = json.loads(response.read()).get('places') or []
    row = rows[0] if rows else {}
    point = row.get('location') or {}
    name = (row.get('displayName') or {}).get('text')
    if not (isinstance(name, str) and name and isinstance(point.get('latitude'), (int, float)) and isinstance(point.get('longitude'), (int, float)) and str(row.get('googleMapsUri', '')).startswith('https://')):
        return None
    return {'name': name[:120], 'address': str(row.get('formattedAddress') or '')[:300], 'lat': float(point['latitude']), 'lng': float(point['longitude']), 'maps_url': maps(row['googleMapsUri'])}


def place(text):
    text = ' '.join(str(text or '').split())
    if not text:
        return None
    key = text.lower()
    if key not in FOUND:
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            FOUND[key] = pool.submit(fetch, text).result(timeout=8)
        except Exception as failure:
            logging.warning('geo place: ' + type(failure).__name__)
            return None
        finally:
            pool.shutdown(wait=False)
    return FOUND[key]


def nearby(found):
    token = platforms.key('threads-token.json')['access_token']
    rows = platforms.request('GET', platforms.THREADS + '/location_search', {'latitude': found['lat'], 'longitude': found['lng'], 'fields': 'id,name,city'}, token=token).get('data', [])
    name = found['name'].lower()
    return next((str(r['id']) for r in rows if re.fullmatch(r'[0-9]+', str(r.get('id', ''))) and str(r.get('name', '')).lower() and (str(r['name']).lower() in name or name in str(r['name']).lower())), None)


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


def location(config, text, network, found=None):
    text = ' '.join(str(text or '').split())
    if not text or network not in ('instagram', 'threads'):
        return None
    if network == 'threads' and found:
        slot = ('threads', '%.5f,%.5f' % (found['lat'], found['lng']))
        if slot not in CACHE:
            try:
                CACHE[slot] = nearby(found)
            except Exception as failure:
                logging.warning('geo threads: ' + type(failure).__name__)
        if CACHE.get(slot):
            return CACHE[slot]
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
