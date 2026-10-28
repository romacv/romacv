import contextvars
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import drafter
import geo
import store as ledger
import voice
from drafter import Error, Cancelled

NAMES = ('places', 'media', 'copy', 'merge')
BUDGETS = {'places':180, 'media':120, 'copy':150}
TOKENS = {'places':12000, 'media':10000, 'copy':12000}
LABELS = {'places':'места', 'media':'фото', 'copy':'тексты', 'merge':'сборка'}
CORRECTION = 90
CORRECTIONS = 2
START_WAIT = 10
FLUSH = 2
POLL = 5
ACTION = 120
RESULT = 200
FAILED = 'Черновик не получился: модель не уложила материал в правила форматов.'
TARGET_ERRORS = re.compile(r'needs|overlay|copy|target', re.I)
MEDIA_ERRORS = re.compile(r'media|image|\balt\b|cover|video|slug', re.I)
NAME = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"')
QUERY = re.compile(r'"query"\s*:\s*"((?:[^"\\]|\\.)*)"')
FINISHED = ('done', 'failed', 'cancelled')


def role(name):
    return Path(__file__).with_name('agents').joinpath(name + '.md').read_text()


def parse(raw):
    return json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip()))


def shape(name, value):
    if not isinstance(value, dict):
        return ['Return a JSON object matching the schema']
    if name == 'places':
        ok = isinstance(value.get('places'), list) and isinstance(value.get('unknown', []), list)
    elif name == 'media':
        ok = isinstance(value.get('images'), list) and all(isinstance(x, dict) for x in value['images']) and isinstance(value.get('order'), list)
    else:
        ok = isinstance(value.get('copy'), dict) and bool(value['copy'])
    return [] if ok else ['Return a JSON object matching the schema in the role instructions']


def summary(name, value):
    if name == 'places':
        return str(len(value.get('places', []))) + ' мест, не опознано ' + str(len(value.get('unknown', [])))
    if name == 'media':
        kept = [x for x in value.get('images', []) if x.get('keep', True)]
        return str(len(kept)) + ' фото'
    return 'тексты: ' + ', '.join(value.get('copy', {}))


class Live:
    def __init__(self, store, item, step, revision):
        self.store, self.item, self.step, self.revision = store, item, step, revision
        self.agents, self.dirty, self.cancel = {}, False, None
        self.lock = threading.Lock()
        self.halt = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=contextvars.copy_context().run, args=(self.loop,), daemon=True)
        self.thread.start()

    def loop(self):
        polled = time.monotonic()
        while not self.halt.wait(FLUSH):
            self.flush()
            if time.monotonic() - polled >= POLL:
                polled = time.monotonic()
                self.poll()

    def poll(self):
        try:
            item = self.store.get('items/' + self.item)
            if not item or item['revision'] != self.revision or item.get('status') == 'rejected':
                self.stop_agents()
        except Exception as failure:
            logging.error(json.dumps({'code':'session_poll_failed','error':str(failure)[:200]}, ensure_ascii=False))

    def stop_agents(self):
        if self.cancel is not None:
            self.cancel.set()

    def update(self, name, **patch):
        with self.lock:
            agent = self.agents.setdefault(name, {})
            for field in ('action', 'result'):
                if isinstance(patch.get(field), str):
                    patch[field] = patch[field][:ACTION if field == 'action' else RESULT]
            status = patch.get('status')
            if status == 'running':
                agent.setdefault('started', time.time())
                agent.pop('finished', None)
            if status in FINISHED:
                patch.setdefault('finished', time.time())
            agent.update(patch)
            self.dirty = True

    def status(self, name):
        with self.lock:
            return self.agents.get(name, {}).get('status')

    def flush(self):
        with self.lock:
            if not self.dirty:
                return
            snapshot = {name:dict(agent) for name, agent in self.agents.items()}
            self.dirty = False
        path = 'sessions/' + self.item
        def apply(tx):
            session = self.store.get(path, tx)
            if not session:
                return None
            if session.get('state') != 'open':
                return False
            entries = [e for e in session.get('steps', []) if e.get('id') == self.step]
            if any((e.get('revision') or 0) > self.revision for e in entries):
                return False
            entry = next((e for e in reversed(entries) if e.get('revision') in (None, self.revision)), None)
            if entry is None:
                return None
            if entry.get('state') == 'cancelled':
                return False
            agents = entry.setdefault('agents', {})
            for name, agent in snapshot.items():
                agents.setdefault(name, {}).update(agent)
            entry['usd'] = round(sum(a.get('usd', 0) for a in agents.values()), 6)
            session['updated'] = time.time()
            self.store.put(path, session, tx)
            return True
        try:
            result = self.store.atomic(apply)
            if result is False:
                self.stop_agents()
            elif result is None:
                with self.lock:
                    self.dirty = True
        except Exception as failure:
            with self.lock:
                self.dirty = True
            logging.error(json.dumps({'code':'session_flush_failed','error':str(failure)[:200]}, ensure_ascii=False))

    def stop(self):
        self.halt.set()
        if self.thread:
            self.thread.join(5)
        self.flush()


class Run:
    def __init__(self, config, store, item, revision, live, rec=None):
        self.config, self.store, self.item, self.revision, self.live, self.rec = config, store, item, revision, live, rec
        self.client = drafter.connect(config)
        self.cancel = threading.Event()
        if live is not None:
            live.cancel = self.cancel
        self.guard = ('items/' + item, 'revision', revision)
        self.threads, self.totals, self.started = {}, {}, {name:threading.Event() for name in NAMES}
        self.lock = threading.Lock()
        self.system = None
        self.photos = {}

    def show(self, name, **patch):
        if self.live is not None:
            self.live.update(name, **patch)

    def prefix(self, images):
        with self.lock:
            if self.system is None:
                self.system = drafter.prompt(self.store)
            key = tuple(images)
            if key not in self.photos:
                self.photos[key] = drafter.pictures(list(images))
            return self.photos[key]

    def messages(self, thread, turns=()):
        text = {'type':'text', 'text':thread['text']}
        if not turns:
            text['cache_control'] = {'type':'ephemeral'}
        messages = [{'role':'user', 'content':list(self.prefix(thread['images'])) + [text]}]
        for who, words in turns:
            messages.append({'role':who, 'content':[{'type':'text', 'text':words}]})
        if turns:
            messages[-1]['content'][-1]['cache_control'] = {'type':'ephemeral'}
        return messages

    def record(self, name, usage):
        row = ledger.tally(usage)
        with self.lock:
            total = self.totals.setdefault(name, {'calls':0, 'in':0, 'out':0, 'cache_read':0, 'cache_write':0, 'usd':0.0, 'searches':0})
            total['calls'] += 1
            total['in'] += row['input_tokens']
            total['out'] += row['output_tokens']
            total['cache_read'] += row['cache_read_tokens']
            total['cache_write'] += row['cache_write_tokens']
            total['usd'] += row['usd']
            self.show(name, calls=total['calls'], tokens={k:total[k] for k in ('in', 'out', 'cache_read', 'cache_write')}, usd=round(total['usd'], 6))
        if self.rec:
            self.rec(name, usage)

    def watcher(self, name, count):
        blocks, text = {}, []
        def on_event(event):
            kind = event.type
            if kind == 'message_start':
                self.started[name].set()
                self.show(name, action='начинаю')
            elif kind == 'content_block_start':
                block = event.content_block
                blocks[event.index] = {'type':block.type, 'json':''}
                if block.type == 'thinking':
                    self.show(name, action='думаю')
                elif block.type == 'text':
                    self.show(name, action='пишу ответ')
                elif block.type == 'web_search_tool_result':
                    content = getattr(block, 'content', None)
                    if isinstance(content, list):
                        self.show(name, action='читаю ' + str(len(content)) + ' результатов')
                    elif getattr(content, 'error_code', None) == 'max_uses_exceeded':
                        self.show(name, action='лимит поиска')
            elif kind == 'content_block_delta':
                delta = event.delta
                if delta.type == 'input_json_delta' and event.index in blocks:
                    blocks[event.index]['json'] += delta.partial_json
                elif delta.type == 'text_delta' and name == 'media':
                    text.append(delta.text)
                    found = NAME.findall(''.join(text))
                    if found:
                        self.show(name, action='описываю фото ' + str(len(found)) + ' из ' + str(count))
            elif kind == 'content_block_stop':
                block = blocks.get(event.index)
                if block and block['type'] == 'server_tool_use':
                    found = QUERY.findall(block['json'])
                    if found:
                        with self.lock:
                            total = self.totals.setdefault(name, {'calls':0, 'in':0, 'out':0, 'cache_read':0, 'cache_write':0, 'usd':0.0, 'searches':0})
                            total['searches'] += 1
                            self.show(name, action=('ищу: «' + found[0][:80] + '»'), searches=total['searches'])
        return on_event

    def respond(self, name, messages, tokens, deadline, count):
        searched = 0
        for step in range(drafter.PAUSES + 3):
            message = drafter.stream(self.client, self.system, [{**drafter.SEARCH, 'max_uses':drafter.SEARCH['max_uses'] - searched}] if searched < drafter.SEARCH['max_uses'] else [], messages, self.watcher(name, count), lambda usage:self.record(name, usage), deadline, self.cancel, tokens)
            searched += sum(1 for block in message.content if block.type == 'server_tool_use')
            if message.stop_reason != 'pause_turn':
                return message
            if self.cancel.is_set():
                raise Cancelled()
            messages = messages + [{'role':'assistant', 'content':message.content}]
        return message

    def exchange(self, name, thread, turns, deadline):
        turns = list(turns)
        for attempt in range(2):
            self.prefix(thread['images'])
            message = self.respond(name, self.messages(thread, turns), thread['tokens'], deadline, len(thread['images']))
            raw = drafter.final_text(message.content)
            if not raw:
                raise Error('Черновик не получился: агент не ответил')
            thread['raw'] = raw
            try:
                value = parse(raw)
                errors = shape(name, value)
            except (ValueError, TypeError):
                errors = ['Return a valid JSON object matching the schema']
            if not errors:
                return value
            turns += [('assistant', raw), ('user', 'Correct these validation errors and return JSON only: ' + json.dumps(errors))]
        raise Error(FAILED)

    def failure(self, name, failure):
        import anthropic
        import httpx
        slow = (httpx.TimeoutException,) + tuple(getattr(anthropic, n) for n in ('APITimeoutError',) if hasattr(anthropic, n))
        link = tuple(getattr(anthropic, n) for n in ('APIConnectionError',) if hasattr(anthropic, n))
        if isinstance(failure, (drafter.Timeout,) + slow):
            return Error('Черновик не готов: агент «' + LABELS[name] + '» не уложился во время.')
        if isinstance(failure, Error):
            return failure
        if isinstance(failure, link):
            return Error('Черновик не готов: нет связи с Anthropic (агент «' + LABELS[name] + '»).')
        return Error('Черновик не получился: сбой агента «' + LABELS[name] + '».')

    def finish(self, name, thread, value):
        try:
            self.store.keep('conversations/' + self.item + '-' + name, {'tag':self.revision, 'output':value, 'turns':[{'role':'user', 'text':thread['text']}, {'role':'assistant', 'text':thread['raw']}]}, self.guard)
        except Exception as failure:
            logging.error(json.dumps({'code':'conversation_save_failed', 'error':str(failure)[:200]}, ensure_ascii=False))

    def agent(self, name, payload, images, max_tokens, deadline):
        thread = self.threads[name] = {'text':role(name) + '\n\n' + json.dumps(payload, ensure_ascii=False), 'images':list(images), 'tokens':max_tokens, 'raw':None}
        saved = self.store.get('conversations/' + self.item + '-' + name) or {}
        if saved.get('tag') == self.revision and isinstance(saved.get('output'), dict):
            thread['raw'] = json.dumps(saved['output'], ensure_ascii=False)
            self.show(name, status='done', action=None, result=summary(name, saved['output']) + ' ранее')
            self.started[name].set()
            return saved['output']
        if name == 'media' and not images:
            value = {'images':[], 'order':[]}
            thread['raw'] = json.dumps(value)
            self.show(name, status='done', action=None, result='нет фото')
            self.started[name].set()
            return value
        self.show(name, status='running', action='начинаю')
        try:
            value = self.exchange(name, thread, (), deadline)
        except Cancelled:
            self.show(name, status='cancelled', action=None)
            raise
        except Exception as failure:
            problem = self.failure(name, failure)
            self.show(name, status='failed', action=None, result='ошибка: ' + str(problem))
            raise problem from failure
        finally:
            self.started[name].set()
        self.finish(name, thread, value)
        self.show(name, status='done', action=None, result=summary(name, value))
        return value

    def correct(self, name, errors, deadline):
        thread = self.threads[name]
        turns = [('assistant', thread['raw']), ('user', 'Correct these validation errors and return JSON only: ' + json.dumps(errors, ensure_ascii=False))]
        self.show(name, status='running', action='исправляю')
        try:
            value = self.exchange(name, thread, turns, deadline)
        except Cancelled:
            self.show(name, status='cancelled', action=None)
            raise
        except Exception as failure:
            problem = self.failure(name, failure)
            self.show(name, status='failed', action=None, result='ошибка: ' + str(problem))
            raise problem from failure
        self.finish(name, thread, value)
        self.show(name, status='done', action=None, result=summary(name, value))
        return value


def owner_of(error):
    return 'media' if not TARGET_ERRORS.search(error) and MEDIA_ERRORS.search(error) else 'copy'


def merge(item, source, places, media, copy, clips, chosen, owner):
    entries = {x.get('name'):x for x in (media or {}).get('images', []) if isinstance(x, dict)}
    order = [n for n in dict.fromkeys((media or {}).get('order') or list(entries)) if n in entries and n in source.get('available_images', []) and entries[n].get('keep', True)][:10]
    images = [{'path':n, 'alt':str(entries[n].get('alt') or '')[:1000]} for n in order]
    blocks = copy.get('copy') if isinstance(copy.get('copy'), dict) else {}
    spec = {'slug':source['slug'], 'kind':copy.get('kind'), 'at':copy.get('at') or source.get('current_time'), 'targets':list(blocks), 'media':{'images':images}, 'copy':dict(blocks), 'approval':{}}
    if isinstance(copy.get('notes'), str) and copy['notes']:
        spec['notes'] = copy['notes']
    if copy.get('threads_tag') is not None:
        spec['threads_tag'] = copy['threads_tag']
    where = geo.query(places)
    if where:
        spec['location'] = where[:120]
    stories = [t for t in spec['targets'] if t in ('ig-story', 'tg-story') and isinstance(blocks.get(t), str)]
    if images and stories:
        place = next((p['name'] for p in (places or {}).get('places', []) if isinstance(p, dict) and isinstance(p.get('name'), str) and 1 <= len(p['name']) <= 40 and p.get('confidence') == 'high'), None)
        spec['overlay'] = {t:{'text':blocks[t], 'place':place, 'anchor':[0.5, 0.5], 'align':'center'} for t in stories}
    errors = {}
    for error in drafter.validate(spec, prepared=False, chosen=chosen, owner=owner, clips=bool(clips)):
        errors.setdefault(owner_of(error), []).append(error)
    return spec, errors


def run_draft(config, store, item, revision, source, images, clips, validate, rec, live):
    zone = config.get('timezone', 'Asia/Makassar')
    base = {**source, 'timezone':zone, 'current_time':datetime.now(ZoneInfo(zone)).isoformat()}
    voice.ensure(config, store)
    run = Run(config, store, item, revision, live, rec)
    found = getattr(live, 'found', None)
    chosen = source.get('chosen_targets')
    owner = '\n'.join([m.get('text') or m.get('caption') or '' for m in source.get('messages', [])] + source.get('edits', []))
    for name in NAMES:
        run.show(name, status='waiting')
    def keys(*fields):
        return {k:base[k] for k in fields if k in base}
    def submit(pool, name, payload, bound):
        return pool.submit(contextvars.copy_context().run, run.agent, name, payload, images, TOKENS[name], time.monotonic() + bound)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = submit(pool, 'places', keys('slug', 'messages', 'edits', 'transcripts', 'available_images', 'clips', 'chosen_targets', 'timezone', 'current_time'), BUDGETS['places'])
            run.started['places'].wait(START_WAIT)
            second = submit(pool, 'media', keys('slug', 'messages', 'edits', 'available_images', 'clips', 'chosen_targets', 'timezone', 'current_time'), BUDGETS['media'])
            outputs, failure = {}, None
            for name, future in (('places', first), ('media', second)):
                try:
                    outputs[name] = future.result()
                    if found is not None:
                        try:
                            found(name, outputs[name])
                        except Exception as problem:
                            logging.error(json.dumps({'code':'found_callback_failed', 'error':str(problem)[:200]}))
                except Cancelled:
                    failure = failure if isinstance(failure, Cancelled) else Cancelled()
                except Exception as problem:
                    failure = failure or problem
            if failure:
                raise failure
            if run.cancel.is_set():
                raise Cancelled()
            written = submit(pool, 'copy', {**keys('slug', 'messages', 'edits', 'transcripts', 'clips', 'chosen_targets', 'public_projects', 'edit_screenshots', 'timezone', 'current_time'), 'places':outputs['places'], 'media':outputs['media']}, BUDGETS['copy']).result()
        if run.cancel.is_set():
            raise Cancelled()
        run.show('merge', status='running', action='собираю черновик')
        limit = time.monotonic() + CORRECTION
        for attempt in range(CORRECTIONS + 1):
            spec, found = merge(item, base, outputs['places'], outputs['media'], written, clips, chosen, owner)
            known = {e for errs in found.values() for e in errs}
            for error in validate(spec):
                if error not in known:
                    found.setdefault(owner_of(error), []).append(error)
            if not found:
                break
            if attempt == CORRECTIONS:
                raise Error(FAILED)
            for name, errors in found.items():
                value = run.correct(name, errors, limit)
                if name == 'copy':
                    written = value
                else:
                    outputs['media'] = value
        if run.cancel.is_set():
            raise Cancelled()
        drafter.seed(store, 'conversations/' + item, base, images, spec, run.guard)
        run.show('merge', status='done', action=None, result='черновик собран')
        return spec
    except Exception as failure:
        if live is not None:
            for name in NAMES:
                status = live.status(name)
                if status == 'waiting' or isinstance(failure, Cancelled) and status == 'running':
                    live.update(name, status='cancelled', action=None)
                elif status == 'running' and name == 'merge':
                    live.update(name, status='failed', action=None, result='ошибка: ' + str(failure)[:150])
        if isinstance(failure, (Error, Cancelled)):
            raise
        raise Error('Черновик не получился: сбой агента ' + type(failure).__name__) from failure
