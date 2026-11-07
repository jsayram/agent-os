#!/usr/bin/env python3
"""Capture Claude Code's supplied status metadata locally; never call a model or API."""
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time

CACHE = Path(__file__).resolve().parent / '.cache' / 'claude-status'

def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None

def capture(data, now=None):
    sid = data.get('session_id', '')
    if not isinstance(sid, str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,80}', sid):
        return None
    cw = data.get('context_window') or {}
    context = {k: number(cw.get(k)) for k in ('used_percentage', 'context_window_size')}
    if context['used_percentage'] is not None and context['used_percentage'] > 100:
        context['used_percentage'] = None
    current = cw.get('current_usage') or {}
    fields = [number(current.get(k)) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')]
    used = sum(v or 0 for v in fields) if fields[0] is not None else number(cw.get('total_input_tokens'))
    context['used_tokens'] = used
    if context['used_percentage'] is None:
        remaining = number(cw.get('remaining_percentage'))
        capacity = context['context_window_size']
        if remaining is not None and remaining <= 100:
            context['used_percentage'] = 100 - remaining
        elif used is not None and capacity:
            context['used_percentage'] = round(used / capacity * 100, 1)
    windows = {}
    for key in ('five_hour', 'seven_day'):
        raw = (data.get('rate_limits') or {}).get(key) or {}
        used, reset = number(raw.get('used_percentage')), number(raw.get('resets_at'))
        if used is not None and used <= 100 and reset is not None:
            windows[key] = {'usedPercent': used, 'resetsAt': reset}
    return {'sessionId': sid, 'observedAt': time.time() if now is None else now,
            'context': context, 'windows': windows}

def main():
    try:
        data = json.loads(sys.stdin.read(1024 * 1024))
        record = capture(data)
        if not record:
            return
        CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=CACHE, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(record, out)
            os.replace(name, CACHE / (record['sessionId'] + '.json'))
        finally:
            if os.path.exists(name):
                os.unlink(name)
        pct = record['context']['used_percentage']
        print('Context ' + (str(round(pct, 1)) + '%' if pct is not None else 'unavailable'))
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # Status display failures must never interrupt Claude Code.

if __name__ == '__main__':
    main()
