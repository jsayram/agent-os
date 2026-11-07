"""Bounded metadata-only run history; independent of the disposable parse cache."""
import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

LIMIT, DAYS = 500, 30

def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()

def update_history(directory, observations, status, observed_at):
    directory = Path(directory)
    path, lock = directory / 'run-history.json', directory / 'run-history.lock'
    result = {'status': 'unavailable', 'observedAt': observed_at, 'sources': status,
              'retention': '30 days or 500 total observations; active observations retained', 'runs': [], 'truncated': False}
    try:
        directory.mkdir(exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'r+') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            if path.exists():
                data = json.loads(path.read_text())
                if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('runs'), list):
                    raise ValueError('unsupported or invalid history')
                if any(not isinstance(r, dict) or not isinstance(r.get('id'), str) or r.get('state') not in ('running','ended-unknown','passed','failed','cancelled') for r in data['runs']):
                    raise ValueError('invalid run record')
            else:
                data = {'version': 1, 'runs': []}
            records = {r['id']: r for r in data['runs']}
            seen = set()
            for item in observations:
                seen.add(item['id'])
                old = records.get(item['id'])
                if old and epoch(old['lastObservedAt']) > epoch(observed_at): continue
                if old and old.get('state') != 'running': continue
                row = dict(item)
                if old and old.get('evidence') == 'Playwright final test summary' and item.get('evidence') != 'Playwright final test summary':
                    row.update(evidence=old['evidence'], counts=old.get('counts'), sourceObservedAt=old.get('sourceObservedAt'))
                row['firstObservedAt'] = old['firstObservedAt'] if old else observed_at
                row['lastObservedAt'] = observed_at
                row['endedObservedAt'] = None
                row['state'] = 'running'
                records[row['id']] = row
            for row in records.values():
                source = 'ios' if row['kind'].startswith('ios-') else 'scripts'
                if row['state'] == 'running' and row['id'] not in seen and status.get(source) == 'ok' and epoch(row['lastObservedAt']) < epoch(observed_at):
                    row['endedObservedAt'] = observed_at
                    if row.get('evidence') == 'Playwright final test summary' and row.get('counts') and row['counts'].get('total', 0) > 0:
                        row['state'] = 'failed' if row['counts']['failed'] else 'passed'
                    else:
                        row['state'] = 'ended-unknown'
                        row['evidence'] = 'Process no longer observed; outcome unknown'
                        row['sourceObservedAt'] = observed_at
            cutoff = epoch(observed_at) - DAYS * 86400
            active = [r for r in records.values() if r['state'] == 'running']
            finished = sorted((r for r in records.values() if r['state'] != 'running' and epoch(r.get('endedObservedAt') or r['lastObservedAt']) >= cutoff), key=lambda r:epoch(r.get('endedObservedAt') or r['lastObservedAt']), reverse=True)
            capacity = max(0, LIMIT - len(active))
            result['truncated'] = len(finished) > capacity or len(active) + len(finished) < len(records)
            data['runs'] = active + finished[:capacity]
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(prefix='.run-history-', dir=directory)
                with os.fdopen(fd, 'w') as out:
                    json.dump(data, out, ensure_ascii=False, separators=(',',':'))
                    out.flush(); os.fsync(out.fileno())
                os.replace(tmp, path)
            finally:
                if tmp and os.path.exists(tmp): os.unlink(tmp)
            result.update(status='ok', runs=sorted(data['runs'], key=lambda r:epoch(r.get('endedObservedAt') or r['lastObservedAt']), reverse=True))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
        result['error'] = 'History storage unavailable or invalid; existing file preserved'
    return result
