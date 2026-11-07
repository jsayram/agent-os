"""Local, incremental token accounting. No inference, credentials or network requests."""
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from agent_os_config import CLAUDE_DIR, CODEX_DIR

PRICE_DATE = '2026-10-29'
PRICE_SOURCES = ['https://developers.openai.com/api/docs/pricing',
                 'https://platform.claude.com/docs/en/about-claude/pricing']
# USD per million: uncached input, cache reads, short writes, long writes, output.
PRICES = {
    'claude-opus-5-5': (4, .2, 5, 8, 20),
    'claude-sonnet-5-5': (2, .1, 2.5, 4, 10),
    'claude-haiku-5-5': (.1, .01, .125, .2, .5),
    'claude-fable-5-1': (10, .25, 12.5, 20, 50),
    'claude-fable-5': (10, 1, 12.5, 20, 50),
    'claude-opus-5': (5, .5, 6.25, 10, 25),
    'claude-opus-4-8': (5, .5, 6.25, 10, 25),
    'claude-opus-4-7': (5, .5, 6.25, 10, 25),
    'claude-opus-4-6': (5, .5, 6.25, 10, 25),
    'claude-opus-4-5': (5, .5, 6.25, 10, 25),
    'claude-sonnet-5': (2, .2, 2.5, 4, 10),
    'claude-sonnet-4-6': (3, .3, 3.75, 6, 15),
    'claude-sonnet-4-5': (3, .3, 3.75, 6, 15),
    'claude-haiku-4-5': (1, .1, 1.25, 2, 5),
    'gpt-6.1-sol': (2, .1, 2.5, 5, 10),
    'gpt-6.1-terra': (1, .05, 1.25, 2.5, 5),
    'gpt-6-astra': (10, 1, 12.5, 25, 50),
    'gpt-6-sol': (2, .2, 2.5, 5, 10),
    'gpt-6-luna': (.1, .01, .125, .25, .5),
    'gpt-5.6-sol': (4, .4, 5, 10, 20),
    'gpt-5.6-terra': (2, .2, 2.5, 5, 12),
    'gpt-5.6-luna': (.2, .02, .25, .5, 1.2),
    'gpt-5.5': (5, .5, 0, 0, 30),
    'gpt-5.4': (2.5, .25, 0, 0, 15),
    'gpt-5.2': (1.75, .175, 0, 0, 14),
    'gpt-5.1': (1.25, .125, 0, 0, 10),
}
INDEX_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
COUNTS = ('inputTokens', 'cachedTokens', 'writeTokens', 'longWriteTokens', 'outputTokens')


def count(value):
    return int(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else 0


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def model_name(value, clean):
    return clean(value, 100) if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9._:-]{1,100}', value) else 'unknown'


def claude_record(raw, clean):
    if raw.get('type') != 'assistant': return None
    msg = raw.get('message') or {}; u = msg.get('usage') or {}; at = timestamp(raw.get('timestamp'))
    if not at or not isinstance(u, dict): return None
    writes = count(u.get('cache_creation_input_tokens')); detail = u.get('cache_creation') or {}
    long_write = min(writes, count(detail.get('ephemeral_1h_input_tokens')))
    short_write = writes - long_write
    counts = [count(u.get('input_tokens')), count(u.get('cache_read_input_tokens')), short_write, long_write, count(u.get('output_tokens'))]
    if not sum(counts): return None
    identity = msg.get('id') or raw.get('uuid')
    if not isinstance(identity, str): return None
    return digest('Claude:'+identity), [at, model_name(msg.get('model'), clean), *counts,
                                      writes if not detail else 0, sum(counts[:4])]


def codex_counts(u):
    total = count(u.get('input_tokens')); cached = min(total, count(u.get('cached_input_tokens')))
    writes = min(total-cached, count(u.get('cache_write_input_tokens')))
    return [total-cached-writes, cached, writes, 0, count(u.get('output_tokens'))]


def merge_record(records, key, row):
    old = records.get(key)
    if old:
        # Streaming chunks repeat the same response; retain the final usage in each category.
        row = [min(old[0], row[0]), row[1] if row[1] != 'unknown' else old[1],
               *[max(a,b) for a,b in zip(old[2:], row[2:])]]
    records[key] = row


def read_file(path, vendor, hit, clean):
    stat = path.stat(); stamp = [stat.st_ino, stat.st_size, stat.st_mtime_ns]
    if hit and hit.get('v') == INDEX_VERSION and hit.get('stamp') == stamp: return hit
    reuse = hit and hit.get('v') == INDEX_VERSION and hit.get('stamp', [None])[0] == stat.st_ino and stat.st_size > hit.get('offset', 0)
    records = dict(hit.get('records', {})) if reuse else {}
    model = hit.get('model', 'unknown') if reuse else 'unknown'
    previous = hit.get('total') if reuse else None
    with path.open('rb') as stream:
        head = hashlib.sha256(stream.read(256)).hexdigest()
        if reuse and head != hit.get('head'):
            reuse = False; records = {}; model = 'unknown'; previous = None
        stream.seek(hit.get('offset', 0) if reuse else 0)
        offset = stream.tell()
        while True:
            line = stream.readline()
            if not line: break
            if not line.endswith(b'\n'): break  # Retry an incomplete write next time.
            offset = stream.tell()
            if vendor == 'Claude' and b'"usage"' not in line: continue
            if vendor == 'Codex' and b'"token_count"' not in line and b'"turn_context"' not in line: continue
            try:
                raw = json.loads(line)
                if vendor == 'Claude':
                    record = claude_record(raw, clean)
                    if record: merge_record(records, *record)
                    continue
                p = raw.get('payload') or {}
                if raw.get('type') == 'turn_context':
                    model = model_name(p.get('model'), clean); continue
                if raw.get('type') != 'event_msg' or p.get('type') != 'token_count': continue
                info = p.get('info') or {}; last = info.get('last_token_usage') or {}; total = info.get('total_token_usage')
                at = timestamp(raw.get('timestamp'))
                if not at or not last: continue
                current = codex_counts(total) if isinstance(total, dict) else None
                if current is not None and current == previous: continue
                values = codex_counts(last)
                if current is not None and previous is not None and all(a>=b for a,b in zip(current,previous)):
                    values = [a-b for a,b in zip(current, previous)]
                previous = current
                if not sum(values): continue
                key = digest('Codex:'+str(raw.get('timestamp'))+':'+str(current)+':'+str(codex_counts(last)))
                merge_record(records, key, [at, model, *values, 0, sum(codex_counts(last)[:4])])
            except (ValueError, TypeError, AttributeError): continue
    return {'v': INDEX_VERSION, 'stamp': stamp, 'offset': offset, 'head': head,
            'model': model, 'total': previous, 'records': records}


def price(model, values, prompt=None):
    # Explicit current-rate valuation, not a reconstruction of past invoices.
    canonical = re.sub(r'-\d{8}$', '', model)
    rates = PRICES.get(canonical)
    if rates is None: return None
    rates = list(rates); prompt = sum(values[:4]) if prompt is None else prompt
    if (canonical.startswith(('gpt-6', 'gpt-5.6')) or canonical in ('gpt-5.5','gpt-5.4')) and prompt > 272000:
        rates = [v*(1.5 if i == 4 else 2) for i,v in enumerate(rates)]
    if canonical == 'claude-haiku-5-5' and prompt > 100000:
        rates = [v*5 for v in rates]
    if (values[2] and rates[2] == 0) or (values[3] and rates[3] == 0): return None
    return sum(n*r for n,r in zip(values, rates))/1000000


def empty():
    return {**dict.fromkeys(COUNTS, 0), 'tokens': 0, 'apiUsd': 0.0,
            'unpricedTokens': 0, 'requests': 0, 'unknownWriteLifetimeTokens': 0}


def add(row, values, cost, uncertain):
    for key,n in zip(COUNTS, values): row[key] += n
    row['tokens'] += sum(values); row['requests'] += 1; row['unknownWriteLifetimeTokens'] += uncertain
    if cost is None: row['unpricedTokens'] += sum(values)
    else: row['apiUsd'] += cost


def scan(now, cache, used, clean):
    today = datetime.fromtimestamp(now).date().isoformat()
    records = {}; files = errors = 0
    for vendor, folder in [('Claude', CLAUDE_DIR / 'projects'), ('Codex', CODEX_DIR / 'sessions')]:
        for path in folder.rglob('*.jsonl'):
            if path.is_symlink(): continue
            key = '_token_history:'+vendor+':'+str(path)
            try:
                hit = read_file(path, vendor, cache.get(key), clean); used[key] = hit; files += 1
                for identity,row in hit['records'].items(): merge_record(records, vendor+':'+identity, row)
            except OSError: errors += 1
    daily = {}; models = {}; vendors = {v:empty() for v in ('Claude','Codex')}; total = empty(); earliest = None
    for key,row in records.items():
        at,model,*rest = row
        if at > now+60: continue
        values,uncertain = rest[:5],rest[5]; vendor = key.split(':',1)[0]
        date = datetime.fromtimestamp(at).date().isoformat(); earliest = date if earliest is None else min(earliest,date)
        cost = price(model,values,rest[6])
        day = daily.setdefault(date, {'date':date, **empty(), 'vendors':{v:empty() for v in vendors}})
        by_model = models.setdefault((vendor,model), {'vendor':vendor,'model':model,**empty()})
        for bucket in [day, day['vendors'][vendor], by_model, vendors[vendor], total]:add(bucket,values,cost,uncertain)
    observed_days = len(daily); past = [d for day,d in daily.items() if day < today]
    current = daily.get(today, {'date':today, **empty(), 'vendors':{v:empty() for v in vendors}})
    for row in [total,current,*daily.values(),*models.values(),*vendors.values()]: row['apiUsd'] = round(row['apiUsd'],4)
    return {'status':'ok' if records else 'unavailable', 'asOf':datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'timezone':'Local time', 'today':current, 'total':total, 'vendors':vendors,
            'daily':sorted(daily.values(), key=lambda d:d['date'], reverse=True),
            'models':sorted(models.values(),key=lambda d:-d['tokens']), 'since':earliest,
            'observedDays':observed_days,'averageTokensPerDay':round(sum(d['tokens'] for d in past)/len(past)) if past else None,
            'averageDayCount':len(past), 'filesRead':files,'unreadableFiles':errors,
            'priceDate':PRICE_DATE,'priceSources':PRICE_SOURCES,
            'basis':'Current standard global API token prices; excludes tool fees, fast/batch/regional modifiers and subscription charges. Missing cache-write lifetime uses the short write rate. Unknown models are unpriced.',
            'coverage':'Available local Claude/Codex logs, deduplicated; missing history is unknown, not zero. Average uses past calendar dates with recorded usage, excluding today.'}
