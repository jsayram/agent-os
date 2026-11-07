#!/usr/bin/env python3
"""Collect what Claude and Codex agents are doing on this Mac, for the Agent OS dashboard.

Reads local session logs only (~/.claude/projects, ~/.claude/sessions, ~/.codex/sessions)
and `ps`. Writes small JSON files to ./data. Secrets are redacted and long text is cut short.
Run: python3 -I collect.py [hours]
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
import importlib.util

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_os_config import CLAUDE_DIR, CODEX_DIR, LOCAL_STATUS, SYNC_TODOS, VAULT_DIR
_history_spec = importlib.util.spec_from_file_location('agent_os_run_history', Path(__file__).resolve().parent / 'run_history.py')
_history_module = importlib.util.module_from_spec(_history_spec)
_history_spec.loader.exec_module(_history_module)
update_history = _history_module.update_history

HOME = Path.home()
OUT = Path(__file__).resolve().parent / "data"
HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 24
NOW = time.time()
CUTOFF = NOW - HOURS * 3600
HEALTH_HOLD_SECONDS = 120
HEALTH_GAP_SECONDS = 150

HELPER_WINDOW = 3 * 3600          # subagents / reviewers shown as cards if active in the last 3 h
WORKING_WINDOW = 15 * 60          # mid-turn and active this recently = working
TAIL_BYTES = 8 * 1024 * 1024

# ---------- text hygiene ----------
SECRET_PATTERNS = [
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASS(?:WORD)?|AUTH)[A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)"), r"\1=•••"),
    (re.compile(r"(?i)(authorization|x-api-key|api[-_]?key|bearer|token|password|secret)([\"']?\s*[:=]\s*|\s+)(\"[^\"]*\"|'[^']*'|[^\s,}]+)"), r"\1\2•••"),
    (re.compile(r"\b(sk|pk|rk)[-_][A-Za-z0-9_\-]{12,}"), "•••"),
    (re.compile(r"\b(ghp|gho|ghs|github_pat|xox[abprs])_?[A-Za-z0-9_\-]{12,}"), "•••"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "•••"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "•••"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "•••"),
    (re.compile(r"\b(?=[A-Za-z0-9+_]*\d)(?=[A-Za-z0-9+_]*[A-Za-z])[A-Za-z0-9+_]{40,}={0,2}"), "•••"),
    (re.compile(r"(https?://[^\s?\"']+)\?[^\s\"']*"), r"\1?…"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "•••@•••"),
]

def clean(text, limit=160):
    if not isinstance(text, str):
        return ""
    t = text.strip().splitlines()[0] + (" …" if "\n" in text.strip() else "") if text.strip() else ""
    t = t.replace(str(HOME), "~")
    t = re.sub(r"-Users-[A-Za-z0-9._-]{12,}", "…", t)
    t = re.sub(r"~/Library/Application Support/Claude/scratch-workspaces/[^\s\"']*", "~/…/scratch", t)
    for pat, rep in SECRET_PATTERNS:
        t = pat.sub(rep, t)
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None

def parse_ts(s):
    if isinstance(s, (int, float)):
        return s / 1000 if s > 1e11 else s
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def project_of(cwd):
    if not cwd:
        return "Unknown"
    if "scratch-workspaces" in cwd:
        return "No folder"
    m = re.search(r"/([^/]+)/\.claude/worktrees/([^/]+)", cwd)
    if m:
        return f"{m.group(1)} · {m.group(2)}"
    p = cwd.rstrip("/").split("/")[-1]
    return "Home" if cwd.rstrip("/") == str(HOME) else p

def read_lines(path):
    """Head (first 400 lines) plus the last TAIL_BYTES of a JSONL file, parsed."""
    size = path.stat().st_size
    head, tail = [], []
    with open(path, "rb") as f:
        for i, line in enumerate(f):
            if i >= 400:
                break
            head.append(line)
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.readline()
            tail = f.readlines()
        else:
            f.seek(0)
            tail = f.readlines()
            head = []
    def parse(lines):
        out = []
        for l in lines:
            try:
                out.append(json.loads(l))
            except Exception:
                pass
        return out
    return parse(head), parse(tail)

# ---------- session budgets (passive metadata only) ----------
SESSION_QUOTAS = []
SESSION_STATUS_MAX_AGE = 5 * 60

def status_number(value):
    import math
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None

def session_context(used, capacity, at, source, estimated=False):
    used, capacity = status_number(used), status_number(capacity)
    return {'usedTokens': used, 'capacityTokens': capacity,
            'usedPercent': round(used / capacity * 100, 1) if used is not None and capacity else None,
            'observedAt': iso(at), 'source': clean(source, 100), 'estimated': estimated}

def claude_catalog_windows():
    """Exact capacities from Claude's own local catalog, never model-name heuristics."""
    entries = {}
    for path in (CLAUDE_DIR / 'cache/model-catalog').glob('*.json'):
        try:
            if path.stat().st_size > 4 * 1024 * 1024: continue
            raw = json.loads(path.read_text()); catalog = raw.get('catalog') or {}
            surface = catalog.get('surface')
            if surface not in ('ccd', 'cc'): continue
            at = parse_ts(raw.get('fetchedAt'))
            if not at or at > NOW + 60: continue
            for model in (catalog.get('config') or {}).get('models', []):
                name = model.get('id'); capacity = status_number((model.get('runtime') or {}).get('max_input_tokens'))
                if not isinstance(name, str) or not capacity: continue
                key = (surface, clean(name, 100))
                if key not in entries or at > entries[key]['at']:
                    entries[key] = {'capacity': capacity, 'at': at,
                                    'stale': (parse_ts(raw.get('staleAt')) or 0) <= NOW}
        except (OSError, ValueError, TypeError, AttributeError): continue
    return entries


def apply_claude_catalog(context, app, catalog):
    entry = catalog.get(('ccd' if app == 'Claude desktop' else 'cc', context.get('modelId')))
    if context.get('capacityTokens') or not entry: return context
    used = status_number(context.get('usedTokens'))
    return {**context, 'capacityTokens': entry['capacity'],
            'usedPercent': round(used / entry['capacity'] * 100, 1) if used is not None else None,
            'estimated': True, 'capacitySource': 'Claude local model catalog runtime.max_input_tokens',
            'capacityObservedAt': iso(entry['at']), 'catalogStale': entry['stale'],
            'source': clean(context['source'] + ' + local catalog capacity', 140)}

def codex_windows(raw, at):
    if not isinstance(raw, dict) or raw.get('limit_id') not in (None, 'codex'):
        return None
    windows = {}
    for value in (raw.get('primary'), raw.get('secondary')):
        if not isinstance(value, dict): continue
        duration = value.get('window_minutes')
        key = {300: 'five_hour', 10080: 'seven_day'}.get(duration)
        used, reset = status_number(value.get('used_percent')), status_number(value.get('resets_at'))
        if key and used is not None and used <= 100 and reset:
            windows[key] = {'usedPercent': used, 'resetsAt': reset}
    return {'vendor': 'Codex', 'observedAt': iso(at), 'source': 'Codex session rate-limit metadata', 'windows': windows} if windows else None

def scan_token_economy():
    import importlib.util
    path = Path(__file__).resolve().parent / 'token_usage.py'
    spec = importlib.util.spec_from_file_location('agent_os_token_usage', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.scan(NOW, _cache, _used, clean)


def scan_session_budgets(agents):
    providers = []
    catalog = claude_catalog_windows()
    for vendor in ('Claude', 'Codex'):
        contexts = [{'id': a['id'], 'project': clean(a['project'], 80), 'title': clean(a['title'], 90),
                     'status': a['status'], 'lastActive': a['lastActive'], **a['context']}
                    for a in agents if a['vendor'] == vendor and a['kind'] == 'Session' and a.get('context')]
        if vendor == 'Claude':
            contexts = [apply_claude_catalog(c, next(a['app'] for a in agents if a['id'] == c['id']), catalog) for c in contexts]
        quota = max((q for q in SESSION_QUOTAS if q['vendor'] == vendor),
                    key=lambda q: parse_ts(q['observedAt']) or 0, default=None)
        if vendor == 'Codex':
            try:
                monitor = json.loads((HOME / 'Library/Application Support/CodexUsageMonitor/status.json').read_text())
                at = parse_ts(monitor.get('observedAt')); used = status_number(monitor.get('usedPercent'))
                reset = status_number(monitor.get('resetsAt'))
                if monitor.get('status') == 'ok' and at and used is not None and used <= 100 and reset:
                    if quota is None or at > (parse_ts(quota['observedAt']) or 0):
                        old = dict(quota['windows']) if quota else {}
                        old['five_hour'] = {'usedPercent': used, 'resetsAt': reset, 'observedAt': iso(at), 'source': 'Local Codex Python quota monitor'}
                        quota = {**(quota or {}), 'vendor': vendor, 'windows': old}
            except (OSError, ValueError, TypeError): pass
        else:
            # This directory is populated only by the user-approved local status-line capture.
            for path in (OUT.parent / '.cache/claude-status').glob('*.json'):
                try:
                    raw = json.loads(path.read_text()); at = status_number(raw.get('observedAt'))
                    sid = raw.get('sessionId'); cw = raw.get('context') or {}
                    if not at or at < CUTOFF or at > NOW + 60 or not isinstance(sid, str): continue
                    windows = {}
                    for key in ('five_hour', 'seven_day'):
                        w = (raw.get('windows') or {}).get(key) or {}
                        used, reset = status_number(w.get('usedPercent')), status_number(w.get('resetsAt'))
                        if used is not None and used <= 100 and reset:
                            windows[key] = {'usedPercent': used, 'resetsAt': reset}
                    if windows and (quota is None or at > (parse_ts(quota.get('observedAt')) or 0)):
                        quota = {'vendor': vendor, 'observedAt': iso(at), 'source': 'Claude Code local status line', 'windows': windows}
                    context = next((c for c in contexts if c['id'] == sid), None)
                    agent = next((a for a in agents if a['id'] == sid and a['vendor'] == 'Claude' and a['kind'] == 'Session'), None)
                    pct, capacity, used = (status_number(cw.get(k)) for k in ('used_percentage', 'context_window_size', 'used_tokens'))
                    if not capacity and pct is None and used is None: continue
                    if context is None and agent:
                        context = {'id': sid, 'project': clean(agent['project'], 80), 'title': clean(agent['title'], 90),
                                   'status': agent['status'], 'lastActive': agent['lastActive']}
                        contexts.append(context)
                    if context is not None and at >= (parse_ts(context.get('observedAt')) or 0):
                        direct_capacity = capacity
                        capacity = capacity or context.get('capacityTokens')
                        used = used if used is not None else context.get('usedTokens')
                        supplied = pct is not None and pct <= 100
                        context.update({'usedPercent': pct if supplied else round(used / capacity * 100, 1) if used is not None and capacity else None,
                                        'capacityTokens': capacity, 'usedTokens': used,
                                        'observedAt': iso(at), 'source': 'Claude Code local status line',
                                        'capacitySource': 'Claude Code status-line context_window_size' if direct_capacity else context.get('capacitySource'),
                                        'estimated': not supplied})
                except (OSError, ValueError, TypeError, AttributeError): continue
        windows = {}
        for key, value in (quota or {}).get('windows', {}).items():
            at = value.get('observedAt') or quota.get('observedAt')
            windows[key] = {**value, 'observedAt': at, 'source': clean(value.get('source') or quota.get('source', ''), 100),
                            'stale': NOW - (parse_ts(at) or 0) > SESSION_STATUS_MAX_AGE,
                            'expired': value['resetsAt'] <= NOW}
        contexts.sort(key=lambda c: (c['status'] != 'working', -(parse_ts(c['observedAt']) or 0)))
        providers.append({'vendor': vendor, 'windows': windows, 'contexts': contexts,
                          'sessionCount': sum(1 for a in agents if a['vendor'] == vendor and a['kind'] == 'Session'),
                          'note': 'Account quota is shared across sessions. Context is the last observed request, not a live token counter.',
                          'unavailableReason': 'No local quota metadata yet. Claude Code status-line capture supplies this when supported.' if vendor == 'Claude' else 'No current local rate-limit metadata.'})
    return providers

# ---------- roles (the Nook Crew role set; hats and colours from that board) ----------
CATALOG = {
    "Chief of staff": ("crown", "#c9a227"), "Account setup": ("cap", "#3f7cac"),
    "AI stylist (on-device)": ("beret", "#d97757"), "Board": ("visor", "#3a7bb4"),
    "Business analyst": ("cap", "#4f8a5b"), "CI build / DevOps": ("hardhat", "#e0a325"),
    "Debugger (flaky tests)": ("visor", "#9a4f8a"), "Decisions for you": ("tophat", "#8a5bb8"),
    "DevOps (CI)": ("cap", "#7a6a3a"), "DevOps (local runner)": ("cap", "#5a6a8a"),
    "Documentation worker": ("chef", "#f4f1ea"), "Engineer": ("helmet", "#5a6f7a"),
    "Full-stack dev": ("hardhat", "#ee342f"), "Git worker": ("cap", "#f05133"),
    "iOS dev": ("bandana", "#d2553f"), "iOS dev (images)": ("cap", "#4f9d69"),
    "iOS dev (network)": ("visor", "#c27a2c"), "Learning": ("grad", "#3a4f8f"),
    "Legal": ("tophat", "#e9e4d8"), "Monitoring": ("cap", "#3b7dd8"),
    "Payments": ("tophat", "#2f6b58"), "Performance": ("visor", "#7a63c7"),
    "Release audit": ("tophat", "#33413b"), "Release cleanup": ("visor", "#b4543a"),
    "Research": ("deerstalker", "#6b5b95"), "Security": ("helmet", "#a3261f"),
    "Security ops": ("helmet", "#e2632b"), "Security testing": ("deerstalker", "#8a6a3f"),
    "SME (styling)": ("beret", "#c98f3a"), "Tester (device prep)": ("visor", "#2e9a8a"),
    "Toolchain update": ("hardhat", "#6b7280"), "Try-on (images)": ("beret", "#d4789c"),
    "UI/UX": ("beret", "#b04a7a"), "Chef": ("chef", "#f4f1ea"), "Producer": ("headset", "#3b7dd8"),
    "Generalist": ("cap", "#6b7d74"),
}
ROLES = [
    (r"guardian|safety review|threat|secret|permission", "Security"),
    (r"security test|idor|pen ?test", "Security testing"),
    (r"backup|incident|rotate|security ops", "Security ops"),
    (r"release audit|35a|compliance|app review", "Release audit"),
    (r"release cleanup|sample photo", "Release cleanup"),
    (r"decision|questions for you|needs approval", "Decisions for you"),
    (r"account|clerk|convex project|sign up", "Account setup"),
    (r"stylist", "AI stylist (on-device)"),
    (r"try-?on|flux", "Try-on (images)"),
    (r"\bstyl|outfit rules|fit rules", "SME (styling)"),
    (r"board|dashboard|agent os|crew", "Board"),
    (r"acceptance|business|spec audit|prd", "Business analyst"),
    (r"xcode|toolchain", "Toolchain update"),
    (r"local runner|self-hosted", "DevOps (local runner)"),
    (r"\bci\b|workflow|batch timeout|runner|deploy|cloudflare|mcp", "DevOps (CI)"),
    (r"flaky|debug|root.?cause|crash|\bfix\b|bug", "Debugger (flaky tests)"),
    (r"payment|purchase|subscription|pric|paywall", "Payments"),
    (r"perf|load test|scale|memory|latency", "Performance"),
    (r"monitor|metric|telemetry|usage", "Monitoring"),
    (r"legal|privacy policy|terms|trademark", "Legal"),
    (r"device check|checklist|tester|qa\b", "Tester (device prep)"),
    (r"image|photo|thumbnail|camera", "iOS dev (images)"),
    (r"network|retry|offline|timeout", "iOS dev (network)"),
    (r"backend|full.?stack|endpoint|convex|api\b", "Full-stack dev"),
    (r"\bgit\b|commit|merge|\bpr\b|branch|rebuild", "Git worker"),
    (r"learn|lesson|vault|video|knowledge|wwdc|obsidian|memory note", "Learning"),
    (r"doc|readme|playbook|progress log|handoff|write-?up", "Documentation worker"),
    (r"research|compare|options|explore|find|search|audit|investigat|roi", "Research"),
    (r"architect|design doc|engine|\bplan\b|migration", "Engineer"),
    (r"\bui\b|ux|atlas|redesign|website|page|figma|screen", "UI/UX"),
    (r"icloud|sync|swift|ios|simulator|sign-in|iphone|app\b|nook|closet|lily", "iOS dev"),
    (r"grocer|food|meal|recipe|prep|kitchen", "Chef"),
    (r"youtube|shorts?|avatar|pipeline|render", "Producer"),
]

def role_for(*texts):
    blob = " ".join(t for t in texts if t).lower()
    role = next((r for pat, r in ROLES if re.search(pat, blob)), "Generalist")
    return (role,) + CATALOG[role]

def model_label(m):
    if not m:
        return ""
    m = str(m)
    mm = re.match(r"claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?", m)
    if mm:
        v = mm.group(2) + ("." + mm.group(3) if mm.group(3) and len(mm.group(3)) <= 2 else "")
        return f"{mm.group(1).title()} {v}"
    if m in ("opus", "sonnet", "haiku", "fable"):
        return m.title()
    return m

# ---------- Claude ----------
def tool_summary(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return clean(inp.get("description") or ("Running " + inp.get("command", "")), 90)
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        fp = str(inp.get("file_path", "")).split("/")[-1]
        return f"{ {'Read': 'Reading', 'Edit': 'Editing', 'Write': 'Writing', 'NotebookEdit': 'Editing'}[name] } {clean(fp, 60)}"
    if name in ("Agent", "Task"):
        return "Delegating: " + clean(inp.get("description", ""), 70)
    if name in ("Grep", "Glob"):
        return "Searching code"
    if name in ("WebSearch", "WebFetch"):
        return "Researching the web"
    if name.startswith("mcp__"):
        parts = name.split("__")
        return "Using " + clean(parts[-1].replace("_", " "), 60)
    return "Using " + name

# ---------- Jev (TypeSafe) ----------
# A Jev call is counted from what came back, in the result of a tool call that mentions the helper:
# a Jev response printed by jev-decisions/scripts/decide.py, its "Jev API returned HTTP …" error (the
# request was made and failed), or the summary line jev-filter prints after scoring hits (one per run,
# however many requests it sent). --check, --dry-run, a missing key and output sent elsewhere show none of these.
# Only times, counts, the model name and input tokens are kept; nothing is read from the Keychain or the request.
JEV_REPLY = re.compile(r"\"model\":\s*\"(jev-[\w.-]{1,30})\",\s*\"answers\"")
JEV_TOKENS = re.compile(r"\"input_tokens\":\s*(\d+)")
JEV_FAILED = re.compile(r"Jev API returned HTTP \d{3}")
JEV_FILTER = re.compile(r"jev-filter: \d+ hits -> \d+ kept\b.*?\. (jev-[\w.-]{1,30}), ([\d,]+) input tokens")
JEV_MENTION = ("decide.py", "jev_filter.py", "jev-filter")
JEV_SKILLS = {"typesafe-ai", "typesafe:typesafe-ai", "jev-decisions", "jev-filter"}

def result_text(c):
    if isinstance(c, list):
        return " ".join(result_text(x) for x in c)
    if isinstance(c, dict):
        return str(c.get("text") or c.get("output") or "")
    return c if isinstance(c, str) else ""

class JevLog:
    """Jev calls seen in one log as [ts, model, input tokens], plus Skill loads of the Jev and TypeSafe skills."""
    def __init__(self):
        self.calls, self.skill, self.pending = [], [], {}
    def call(self, cid, ts, text):
        if cid and any(m in text for m in JEV_MENTION):
            self.pending[cid] = ts
    def result(self, cid, ts, content):
        if cid not in self.pending:
            return
        ts = self.pending.pop(cid) or ts
        text = result_text(content)
        found = [(m.group(1), int(m.group(2).replace(",", ""))) for m in JEV_FILTER.finditer(text)]
        if not found:  # jev-filter repeats its failed requests' errors above its summary, so only one kind counts
            for m in JEV_REPLY.finditer(text):
                t = JEV_TOKENS.search(text, m.end(), m.end() + 4000)
                found.append((m.group(1), int(t.group(1)) if t else 0))
            found += [("", 0) for _ in JEV_FAILED.finditer(text)]
        if ts:
            self.calls += [[ts, model, tokens] for model, tokens in found]

def jev_fields(f):
    """What the dashboard shows about Jev for one agent, in the time window."""
    calls = sorted(x for x in f.get("jev") or [] if x[0] >= CUTOFF)
    models = [x[1] for x in calls if x[1]]
    return {"jevCalls": len(calls), "jevFirst": iso(calls[0][0]) if calls else None,
            "jevLast": iso(calls[-1][0]) if calls else None, "jevModel": models[-1] if models else "",
            "jevTokens": sum(x[2] for x in calls),
            "jevSkill": sum(1 for ts in f.get("jevSkill") or [] if ts >= CUTOFF)}

def claude_live_map():
    live = {}
    for f in (CLAUDE_DIR / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            os.kill(int(d["pid"]), 0)
            live[d["sessionId"]] = {"pid": d["pid"], "entry": d.get("entrypoint", "")}
        except Exception:
            continue
    return live

def desktop_map():
    """Claude desktop's own record of each Code session: its local_ id, title and last-turn summary."""
    out = {}
    root = HOME / "Library/Application Support/Claude/claude-code-sessions"
    for f in root.rglob("local_*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        cli = d.get("cliSessionId")
        if not cli:
            continue
        summ = d.get("postTurnSummary")
        if isinstance(summ, str):
            try: summ = json.loads(summ)
            except Exception: summ = None
        out[cli] = {"local": d.get("sessionId"), "title": d.get("title"),
                    "summary": (summ or {}).get("status_detail") if isinstance(summ, dict) else None,
                    "category": (summ or {}).get("status_category") if isinstance(summ, dict) else None,
                    "action": (summ or {}).get("needs_action") if isinstance(summ, dict) else None}
    return out

# ---------- parse cache ----------
# Reading the logs is most of a run's cost, and most logs haven't changed since the last run.
# Each file's facts (everything that depends only on the file) are cached by size and mtime;
# the time windows, live state and status are applied fresh on every run.
CACHE_FILE = Path(__file__).resolve().parent / ".cache" / "parse.json"
# Any edit to this file resets the cache, so facts parsed by older code are never reused.
import hashlib
CACHE_VERSION = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:16]
_cache, _used = {}, {}

def cache_load():
    global _cache
    if os.environ.get("AGENT_OS_FULL") == "1":  # hard refresh: ignore the cache, re-read every log
        _cache = {}
        return
    try:
        d = json.loads(CACHE_FILE.read_text())
        _cache = d.get("files", {}) if d.get("v") == CACHE_VERSION else {}
    except Exception:
        _cache = {}

def cache_save():
    try:
        CACHE_FILE.parent.mkdir(exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": CACHE_VERSION, "files": _used}, separators=(",", ":")))
        tmp.replace(CACHE_FILE)  # atomic, so a crash mid-write never leaves half a cache
    except Exception as e:
        print("cache: could not save:", e, file=sys.stderr)

def cached_facts(path, st, parse):
    key, stamp = str(path), [st.st_size, st.st_mtime_ns]
    meta = path.with_suffix(".meta.json")  # a helper's description lives beside its log
    if "subagents" in path.parts:
        try:
            stamp.append(meta.stat().st_mtime_ns)
        except OSError:
            stamp.append(0)
    hit = _cache.get(key)
    facts = hit["facts"] if hit and hit.get("stamp") == stamp else parse(path)
    _used[key] = {"stamp": stamp, "facts": facts}
    return facts

def last_trail(trail):
    return sorted(trail)[-6:]

def input_question(args):
    questions = args.get('questions') if isinstance(args, dict) else None
    if not isinstance(questions, list): return ''
    return clean(' · '.join(str(q.get('question') or q.get('title') or '') for q in questions if isinstance(q, dict)), 400)

def parse_claude(path):
    """Facts from one Claude log that depend only on the file's content."""
    head, tail = read_lines(path)
    is_sub = "subagents" in path.parts
    desc = agent_type = sub_model = None
    if is_sub:
        meta = path.with_suffix(".meta.json")
        if meta.exists():
            try:
                m = json.loads(meta.read_text())
                desc, agent_type, sub_model = m.get("description"), m.get("agentType"), m.get("model")
            except Exception:
                pass
    title = cwd = model = first_ts = last_ts = effort = None
    last_kind = current = entry = first_prompt = todo_state = last_reply = None
    task_items = {}; trail = []; tools = []; cmds = []; toks = []
    seen_ids = set(); jev = JevLog(); pending_input = None; pending_question = ""; context = None
    for d in head + tail:
        t = d.get("type")
        if t in ("custom-title", "ai-title"):
            title = d.get("customTitle") or d.get("aiTitle") or d.get("title") or title
            continue
        if t == 'system' and d.get('subtype') == 'compact_boundary':
            post = status_number((d.get('compactMetadata') or {}).get('postTokens'))
            at = parse_ts(d.get('timestamp'))
            if post is not None and at:
                context = {**session_context(post, None, at, 'Claude post-compaction tokens', True), 'modelId': clean(model or '', 100)}
        if t not in ("user", "assistant"):
            continue
        uid = d.get("uuid")
        if uid in seen_ids:
            continue
        seen_ids.add(uid)
        ts = parse_ts(d.get("timestamp"))
        if ts:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        cwd = d.get("cwd") or cwd
        entry = d.get("entrypoint") or entry
        effort = d.get("effort") or effort
        msg = d.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if t == "assistant":
            model = msg.get("model") or model
            u = msg.get("usage") or {}
            inputs = [status_number(u.get(k)) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')]
            if ts and inputs[0] is not None and sum(v or 0 for v in inputs) > 0:
                context = {**session_context(sum(v or 0 for v in inputs), None, ts, 'Claude last-response input incl. cache'), 'modelId': clean(model or '', 100)}
            if ts and u.get("output_tokens"):
                toks.append([ts, int(u.get("output_tokens") or 0)])
        if isinstance(content, str):
            if t == "user" and not d.get("isMeta"):
                pending_input = None
                last_kind = "prompt"
                if first_prompt is None and not content.startswith("<"):
                    first_prompt = content
            continue
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "tool_use":
                name = b.get("name", ""); inp = b.get("input") or {}
                if name in ("AskUserQuestion", "ExitPlanMode"):
                    pending_input = b.get("id"); pending_question = input_question(inp) or ("Plan ready for your approval" if name == "ExitPlanMode" else "")
                last_kind = "tool"; current = tool_summary(name, inp)
                if ts:
                    trail.append([ts, current])
                    tools.append([ts, model_label(model or sub_model) or "Unknown", effort or "default"])
                    if name == "Bash":
                        cmds.append([ts, clean(inp.get("command", ""), 160), clean(inp.get("description", ""), 80)])
                    if name == "Skill" and str(inp.get("skill", "")) in JEV_SKILLS:
                        jev.skill.append(ts)
                jev.call(b.get("id"), ts, json.dumps(inp))
                if name == "TodoWrite" and isinstance(inp.get("todos"), list):
                    todo_state = [[clean(x.get("content", ""), 90), x.get("status", "pending")] for x in inp["todos"] if isinstance(x, dict)]
                if name == "TaskCreate":
                    task_items[str(len(task_items) + 1)] = [clean(inp.get("subject") or inp.get("description", ""), 90), "pending"]
                if name == "TaskUpdate" and str(inp.get("taskId")) in task_items and inp.get("status"):
                    task_items[str(inp.get("taskId"))][1] = inp["status"]
            elif bt == "tool_result":
                if b.get("tool_use_id") == pending_input: pending_input = None
                last_kind = "tool"
                jev.result(b.get("tool_use_id"), ts, b.get("content"))
            elif bt == "text" and t == "assistant":
                last_kind = "reply"
                if str(b.get("text", "")).strip(): last_reply = b.get("text")
            elif bt == "text" and t == "user" and not d.get("isMeta"):
                pending_input = None
                last_kind = "prompt"
                if first_prompt is None and not str(b.get("text", "")).startswith("<"):
                    first_prompt = b.get("text")
    return {"title": title, "desc": desc, "agentType": agent_type, "subModel": sub_model, "cwd": cwd,
            "model": model, "effort": effort, "entry": entry, "first": first_ts, "last": last_ts,
            "needsInput": bool(pending_input), "inputQuestion": pending_question if pending_input else "", "lastKind": last_kind, "current": current, "firstPrompt": first_prompt, "lastReply": last_reply,
            "context": context, "plan": todo_state or list(task_items.values()), "trail": last_trail(trail),
            "tools": tools, "cmds": cmds, "toks": toks, "jev": jev.calls, "jevSkill": jev.skill}

def scan_claude(agents, commands, tasks, events):
    live = claude_live_map()
    desk = desktop_map()
    root = CLAUDE_DIR / "projects"
    helper_jev = {}  # session id -> [calls, last] made by its helpers, counted even when the helper card is hidden
    for path in root.rglob("*.jsonl"):
        try:
            st = path.stat()
        except Exception:
            continue
        is_sub = "subagents" in path.parts
        if st.st_mtime < CUTOFF and (is_sub or path.stem not in live):
            continue
        f = cached_facts(path, st, parse_claude)
        sid = path.stem
        parent = path.parent.parent.name if is_sub else None
        agent_type, last_ts, cwd = f["agentType"], f["last"], f["cwd"]
        n_tools = n_cmds = 0
        for ts, mlabel, eff in f["tools"]:
            if ts >= CUTOFF:
                n_tools += 1
                events.append((ts, "Claude", mlabel, eff))
        for ts, cmd, why in f["cmds"]:
            if ts >= CUTOFF:
                n_cmds += 1
                commands.append({"at": iso(ts), "agent": sid, "vendor": "Claude", "command": cmd, "why": why})
        out_tokens = sum(n for ts, n in f["toks"] if ts >= CUTOFF)
        jev = jev_fields(f)
        if is_sub and jev["jevCalls"]:
            h = helper_jev.setdefault(parent, [0, ""])
            h[0] += jev["jevCalls"]; h[1] = max(h[1], jev["jevLast"])
        if last_ts is None or (last_ts < CUTOFF and (is_sub or sid not in live)):
            continue
        age = NOW - last_ts
        last_kind, last_reply, current = f["lastKind"], f["lastReply"], f["current"]
        if is_sub:
            if last_ts < NOW - HELPER_WINDOW:
                agents.append(None)  # counted only
                continue
            status = "working" if age < 300 and last_kind != "reply" else "done"
            kind = "Helper"
            app = "Claude subagent"
            task_title = f["desc"] or agent_type or "Subagent task"
        else:
            lv = live.get(sid)
            mid_turn = last_kind in ("tool", "prompt")
            if f.get("needsInput") and lv:
                status = "waiting"
            elif mid_turn and age < WORKING_WINDOW:
                status = "working"
            elif lv:
                cat = (desk.get(sid) or {}).get("category")
                if cat == "blocked" or (cat is None and asks_you(last_reply)):
                    status = "waiting"
                else:
                    status = "idle"
            else:
                status = "done"
            kind = "Session"
            entry = f["entry"]
            app = "Claude desktop" if (lv and "desktop" in lv["entry"]) or (entry and "desktop" in entry) else "Claude Code CLI"
            dt = (desk.get(sid) or {}).get("title")
            task_title = (dt if dt and dt.lower() != "placeholder" else None) or f["title"] or f["firstPrompt"] or "Untitled session"
        role, hat, color = role_for(task_title, "debug" if agent_type == "root-cause-debugger" else "", "research" if agent_type == "Explore" else "", "plan" if agent_type == "Plan" else "", project_of(cwd))
        add_steps(tasks, sid, [tuple(x) for x in f["trail"]], status, [tuple(x) for x in f["plan"]])
        model, sub_model, effort = f["model"], f["subModel"], f["effort"]
        # Desktop's own "blocked" verdict counts for a day; older ones stay Waiting so a forgotten session can't hold an alert on.
        d = desk.get(sid) or {}
        blocked = d.get("category") == "blocked" and age < 24 * 3600
        step = d.get("action") or d.get("summary")
        answer = bool(f.get("needsInput")) or (last_kind == "reply" and needs_you(last_reply)) or (blocked and wants_answer(step))
        agents.append({
            "id": sid, "vendor": "Claude", "app": app, "kind": kind,
            "parent": parent, "project": project_of(cwd), "title": clean(task_title, 90),
            "role": role, "hat": hat, "hatColor": color, "status": status,
            "needsAttention": status == "waiting" and answer,  # can't go on until the person answers: the whole office panics
            "needsAction": status == "waiting" and blocked and not answer,  # a next step for the person: the Session Observatory only
            "model": model_label(model or sub_model), "context": f.get("context"),
            "now": clean(current or "", 90) if status == "working" else "",
            "lastStep": clean(current or "", 90),
            "started": iso(f["first"]), "lastActive": iso(last_ts),
            "toolCalls": n_tools, "commands": n_cmds, "tokensOut": out_tokens,
            "pid": (live.get(sid) or {}).get("pid"),
            "effort": effort or "default",
            "spawned": len(list((path.parent / sid / "subagents").glob("*.meta.json"))) if not is_sub else 0,
            "ask": clean(f.get("inputQuestion") or (step if blocked else None) or d.get("summary") or last_reply or "", 400),
            "openUrl": claude_link(desk, parent if is_sub else sid),
            "taskId": task_of(f["firstPrompt"]) if kind == "Session" else "",
            **jev,
        })
    for a in agents:
        if a and a["vendor"] == "Claude" and a["id"] in helper_jev:
            a["jevHelperCalls"], a["jevHelperLast"] = helper_jev[a["id"]]

TASK_RE = re.compile(r"works on task (T\d+) from [^\r\n]{1,80} TODO list")

def task_of(prompt):
    """The TODO id a session was started for: the Start button's brief opens with it."""
    m = TASK_RE.search(prompt or "") if isinstance(prompt, str) else None
    return m.group(1) if m else ""

ASK_RE = re.compile(r"\?(?=[\s*_)\]]|$)|\b(should i|shall i|do you want|want me to|would you like|let me know|which (one|option)|"
                    r"can you|could you|please (confirm|choose|pick|approve|decide)|your (call|decision|approval)|"
                    r"waiting (for|on) you|needs? your)\b", re.I)

def asks_you(text):
    """True when an agent's last message ends by asking the person something."""
    if not text:
        return False
    t = re.sub(r"\]\([^)]*\)|https?://\S+|`[^`]*`", " ", str(text))  # links and code don't count
    return bool(ASK_RE.search(t[-500:]))

ANSWER_RE = re.compile(r"\?|\b(answers?|decide|decisions?|pick|choose|confirm|approve|approval|questions?|choices?)\b", re.I)

def wants_answer(step):
    """A stopped session's next step is a decision only the person can make, not a job to go and do."""
    return bool(ANSWER_RE.search(str(step or "")))

def needs_you(text):
    """A direct request for the person, rather than a blocked job or an optional sign-off."""
    if not text:
        return False
    t = re.sub(r"```[\s\S]*?```|`[^`]*`|\]\([^)]*\)|https?://\S+", " ", str(text))
    tail = t[-650:]
    request = r"\b(?:please (?:confirm|choose|pick|approve|decide|provide|send|answer)|(?:need|require|awaiting|waiting for) your (?:approval|answer|decision|confirmation|permission|input))\b"
    question = r"(?:^|[\n.!]\s*)(?:can you|could you|will you|should i|shall i|do you want|which\b|what\b|where\b|how should\b|is it okay\b|may i\b|any preference\b)[^?\n]{0,240}\?"
    return bool(re.search(request, tail, re.I) or re.search(question, tail, re.I))


def claude_link(desk, sid):
    d = desk.get(sid) or {}
    if d.get("local") and re.match(r"^local_[A-Za-z0-9-]{1,64}$", d["local"]):
        return "claude://code/continue?session=" + d["local"]
    return ""

def add_steps(tasks, aid, trail, status, plan):
    """A plan (todo list) if the agent kept one, else its last 6 actions as a trail."""
    if plan:
        for i, (txt, stt) in enumerate(plan[:12]):
            tasks.append({"agent": aid, "n": i + 1, "at": None, "step": txt, "kind": "plan",
                          "state": {"completed": "done", "in_progress": "doing"}.get(stt, "todo")})
        return
    trail = sorted(trail)[-6:]
    for i, (ts, txt) in enumerate(trail):
        last = i == len(trail) - 1
        tasks.append({"agent": aid, "n": i + 1, "at": iso(ts), "step": txt, "kind": "trail",
                      "state": "doing" if last and status == "working" else "done"})

# ---------- Codex ----------
CMD_RE = re.compile(r"cmd\s*:\s*\"((?:[^\"\\]|\\.)*)\"")

def parse_codex(path):
    """Facts from one Codex log that depend only on the file's content."""
    head, tail = read_lines(path)
    recs = head + tail
    meta = next((r.get("payload") for r in recs if r.get("type") == "session_meta"), {}) or {}
    cwd = meta.get("cwd"); model = first_ts = last_ts = state = current = plan = effort = None
    first_prompt = last_reply = None
    trail = []; tools = []; cmds_out = []; toks = []
    seen = set(); jev = JevLog(); pending_input = None; pending_question = ""; async_input = False; context = quota = None
    for r in recs:
        key = (r.get("timestamp"), r.get("ordinal"), r.get("type"))
        if key in seen:
            continue
        seen.add(key)
        ts = parse_ts(r.get("timestamp"))
        if ts:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        p = r.get("payload") or {}
        t, pt = r.get("type"), p.get("type")
        if t == "turn_context":
            model = p.get("model") or model
            effort = p.get("effort") or p.get("reasoning_effort") or effort
            cwd = p.get("cwd") or cwd
        elif t == "event_msg" and pt == "task_started":
            state = "running"
        elif t == "event_msg" and pt == "task_complete":
            state = "complete"
        elif t == "event_msg" and pt == "token_count":
            info = p.get('info') or {}
            last = info.get('last_token_usage') or {}
            if ts:
                if info:
                    context = session_context(last.get('total_tokens'), info.get('model_context_window'), ts, 'Codex last-request tokens', True)
                quota = codex_windows(p.get('rate_limits'), ts) or quota
            if ts and last.get("output_tokens"):
                toks.append([ts, int(last.get("output_tokens") or 0)])
        elif t == "response_item" and pt == "message" and p.get("role") == "assistant":
            for c in p.get("content") or []:
                txt = c.get("text") if isinstance(c, dict) else None
                if txt and txt.strip(): last_reply = txt
        elif t == "response_item" and pt == "message" and p.get("role") == "user":
            pending_input = None
            if first_prompt is not None: continue
            for c in p.get("content") or []:
                txt = c.get("text") if isinstance(c, dict) else None
                if txt and not txt.lstrip().startswith("<"):
                    first_prompt = txt; break
        elif t == "response_item" and pt in ("function_call", "custom_tool_call", "local_shell_call"):
            name = p.get("name") or pt
            input_tool = name.rsplit(".", 1)[-1]
            if input_tool in ("request_user_input", "request_user_input_async"):
                pending_input = p.get("call_id"); async_input = input_tool.endswith("_async")
            raw = p.get("arguments") or p.get("input") or ""
            args = {}
            if isinstance(raw, str) and raw.startswith("{"):
                try: args = json.loads(raw)
                except Exception: args = {}
            if input_tool in ("request_user_input", "request_user_input_async"): pending_question = input_question(args)
            label = args.get("title") if isinstance(args, dict) else None
            cmds = []
            if name in ("exec_command", "shell", "local_shell_call"):
                c = args.get("cmd") or args.get("command")
                cmds = [" ".join(c) if isinstance(c, list) else c] if c else []
            elif isinstance(raw, str):
                cmds = [bytes(m, "utf-8").decode("unicode_escape", "ignore") for m in CMD_RE.findall(raw)]
            if name == "update_plan" and isinstance(args.get("plan"), list):
                plan = [[clean(x.get("step", ""), 90), x.get("status", "pending")] for x in args["plan"] if isinstance(x, dict)]
            jev.call(p.get("call_id"), ts, raw if isinstance(raw, str) else json.dumps(raw))
            current = clean(label or (("Running " + cmds[0]) if cmds else "Using " + name), 90)
            if ts:
                trail.append([ts, current])
                tools.append([ts, model or "Unknown", effort or "default"])
                for c in cmds[:6]:
                    cmds_out.append([ts, clean(c, 160), clean(label or "", 80)])
        elif t == "response_item" and pt in ("function_call_output", "custom_tool_call_output"):
            if p.get("call_id") == pending_input and not async_input: pending_input = None
            jev.result(p.get("call_id"), ts, p.get("output"))
    return {"id": meta.get("id"), "parentThread": meta.get("parent_thread_id"),
            "threadSource": str(meta.get("thread_source", "")), "cwd": cwd, "model": model, "context": context, "quota": quota,
            "needsInput": bool(pending_input), "asyncInput": bool(pending_input) and async_input, "inputQuestion": pending_question if pending_input else "", "effort": effort, "first": first_ts, "last": last_ts, "state": state, "current": current,
            "plan": plan, "firstPrompt": first_prompt, "lastReply": last_reply,
            "trail": last_trail(trail), "tools": tools, "cmds": cmds_out, "toks": toks,
            "jev": jev.calls, "jevSkill": jev.skill}

def scan_codex(agents, commands, tasks, events):
    names = {}
    idx = CODEX_DIR / "session_index.jsonl"
    if idx.exists():
        for l in idx.read_text().splitlines():
            try:
                d = json.loads(l); names[d["id"]] = d.get("thread_name")
            except Exception:
                pass
    for path in (CODEX_DIR / "sessions").rglob("*.jsonl"):
        try:
            st = path.stat()
        except Exception:
            continue
        if st.st_mtime < CUTOFF:
            continue
        f = cached_facts(path, st, parse_codex)
        tid = f["id"] or path.stem[-36:]
        if f.get('quota'): SESSION_QUOTAS.append(f['quota'])
        reviewer = bool(f["parentThread"]) or "guardian" in f["threadSource"]
        n_tools = n_cmds = 0
        for ts, m, eff in f["tools"]:
            if ts >= CUTOFF:
                n_tools += 1
                events.append((ts, "Codex", m, eff))
        for ts, cmd, why in f["cmds"]:
            if ts >= CUTOFF:
                n_cmds += 1
                commands.append({"at": iso(ts), "agent": tid, "vendor": "Codex", "command": cmd, "why": why})
        out_tokens = sum(n for ts, n in f["toks"] if ts >= CUTOFF)
        last_ts, cwd, current, last_reply = f["last"], f["cwd"], f["current"], f["lastReply"]
        if last_ts is None or last_ts < CUTOFF:
            continue
        age = NOW - last_ts
        if reviewer and last_ts < NOW - HELPER_WINDOW:
            agents.append(None)
            continue
        if f.get("needsInput") and not f.get("asyncInput") and not reviewer and age < 24 * 3600:
            status = "waiting"
        elif f["state"] == "running" and age < WORKING_WINDOW:
            status = "working"
        elif f.get("needsInput") and not reviewer and age < 24 * 3600:
            status = "waiting"
        elif reviewer:
            status = "done"
        elif asks_you(last_reply) and age < 24 * 3600:
            status = "waiting"
        else:
            status = "idle" if age < 24 * 3600 else "done"
        title = names.get(tid) or f["firstPrompt"] or "Codex thread"
        role, hat, color = role_for(title, "guardian" if reviewer else "", project_of(cwd))
        add_steps(tasks, tid, [tuple(x) for x in f["trail"]], status, [tuple(x) for x in (f["plan"] or [])])
        agents.append({
            "id": tid, "vendor": "Codex", "app": "Codex reviewer" if reviewer else "Codex desktop",
            "kind": "Helper" if reviewer else "Session", "parent": f["parentThread"],
            "project": project_of(cwd), "title": clean(title, 90),
            "role": role, "hat": hat, "hatColor": color, "status": status,
            "needsAttention": status in ("working", "waiting") and (bool(f.get("needsInput")) or (status == "waiting" and needs_you(last_reply))),
            "model": f["model"] or "", "context": f.get("context"), "now": current if status == "working" else "",
            "lastStep": current or "", "started": iso(f["first"]), "lastActive": iso(last_ts),
            "toolCalls": n_tools, "commands": n_cmds, "tokensOut": out_tokens, "pid": None,
            "ask": "" if reviewer else clean(f.get("inputQuestion") or last_reply or "", 400),
            "effort": f["effort"] or "default", "spawned": 0,
            "openUrl": "codex://threads/" + str(f["parentThread"] if reviewer else tid),
            **jev_fields(f),
        })

# ---------- processes ----------
def scan_processes():
    out = subprocess.run(["ps", "-axo", "pid=,etime=,%cpu=,rss=,comm="], capture_output=True, text=True).stdout
    groups = {}
    def secs(et):
        d, _, rest = et.partition("-") if "-" in et else ("0", "", et)
        parts = [int(x) for x in rest.split(":")]
        while len(parts) < 3: parts.insert(0, 0)
        return int(d) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    for line in out.splitlines():
        bits = line.split(None, 4)
        if len(bits) < 5:
            continue
        pid, et, cpu, rss, comm = bits
        c = comm
        if "/claude-code/" in c and c.endswith("/claude"):
            g = ("Claude", "Claude Code engine")
        elif c.startswith("/Applications/Claude.app"):
            g = ("Claude", "Claude desktop app")
        elif "codex-cli" in c and c.endswith("/codex"):
            g = ("Codex", "Codex CLI runtime")
        elif "ChatGPT.app" in c and ("node" in c.split("/")[-1]):
            g = ("Codex", "Codex tool runners (node)")
        elif "ChatGPT.app" in c or "/.codex/" in c:
            g = ("Codex", "ChatGPT / Codex app")
        elif "Xcode.app" in c or "CoreSimulator" in c or "Simulator.app" in c:
            g = ("Tools", "Xcode and simulators")
        elif re.search(r"/(node|bun|deno|python3?)$", c) and "Visual Studio" not in c:
            g = ("Tools", "Dev servers and scripts")
        else:
            continue
        x = groups.setdefault(g, {"vendor": g[0], "group": g[1], "processes": 0, "cpu": 0.0, "memMB": 0.0, "upHours": 0.0})
        x["processes"] += 1
        x["cpu"] += float(cpu)
        x["memMB"] += int(rss) / 1024
        x["upHours"] = max(x["upHours"], secs(et) / 3600)
    rows = sorted(groups.values(), key=lambda r: (-r["memMB"]))
    for r in rows:
        r["cpu"] = round(r["cpu"], 1); r["memMB"] = round(r["memMB"]); r["upHours"] = round(r["upHours"], 1)
    return rows

def frontmatter(path):
    """The few YAML fields a SKILL.md or command .md starts with (name, description, argument-hint)."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    out, key = {}, None
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"([A-Za-z][\w-]*):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            out[key] = "" if val in ("|", ">", "|-", ">-") else val.strip("\"'")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out

def scan_skills():
    """Claude and Codex skills (yours, plugins', and each project's), for the Commands page."""
    rows = []
    def add(path, source, prefix="", fallback="", project="", app="Claude"):
        fm = frontmatter(path)
        name = fm.get("name") or fallback
        if not name:
            return
        desc = re.sub(r"\s+", " ", fm.get("description", "").replace('\\"', '"')).strip()
        # Claude runs a skill as /name; Codex mentions one as $name
        rows.append({"name": ("$" if app == "Codex" else "/") + prefix + name, "description": desc[:600],
                     "source": source, "project": project, "app": app,
                     "args": fm.get("argument-hint", "").strip("\"'")[:80],
                     "manualOnly": fm.get("disable-model-invocation", "").lower() == "true"})
    for p in sorted((CLAUDE_DIR / "skills").glob("*/SKILL.md")):
        add(p, "My skill", fallback=p.parent.name)
    for p in sorted((CLAUDE_DIR / "commands").glob("*.md")):
        add(p, "My command", fallback=p.stem)
    try:
        settings = json.loads((CLAUDE_DIR / "settings.json").read_text())
        installed = json.loads((CLAUDE_DIR / "plugins/installed_plugins.json").read_text())
    except Exception:
        settings, installed = {}, {}
    enabled = {k for k, v in (settings.get("enabledPlugins") or {}).items() if v}
    for key, installs in (installed.get("plugins") or {}).items():
        if key not in enabled or not installs:
            continue
        plugin, root = key.split("@")[0], Path(installs[-1].get("installPath", ""))
        for p in sorted(root.glob("skills/*/SKILL.md")):
            add(p, "Plugin: " + plugin, plugin + ":", p.parent.name)
        for p in sorted(root.glob("commands/*.md")):
            add(p, "Plugin: " + plugin, plugin + ":", p.stem)
    # Codex: your skills, the shared ~/.agents folder, and the ones Codex ships with
    for p in sorted((CODEX_DIR / "skills").glob("*/SKILL.md")):
        add(p, "Codex skill", fallback=p.parent.name, app="Codex")
    for p in sorted((HOME / ".agents/skills").glob("*/SKILL.md")):
        add(p, "Codex skill (shared)", fallback=p.parent.name, app="Codex")
    for p in sorted((CODEX_DIR / "skills/.system").glob("*/SKILL.md")):
        add(p, "Codex built-in", fallback=p.parent.name, app="Codex")
    # project-level skills, in every folder Claude Code (~/.claude.json) or Codex (config.toml) has opened
    projects = set()
    try:
        projects |= set((json.loads((HOME / ".claude.json").read_text()).get("projects") or {}).keys())
    except Exception:
        pass
    try:
        import tomllib
        projects |= set((tomllib.loads((CODEX_DIR / "config.toml").read_text()).get("projects") or {}).keys())
    except Exception:
        pass
    # also every sibling of a known project (all of ~/Git, say), so a repo you haven't opened yet still counts
    for parent in {Path(f).parent for f in projects} | {HOME / "Git"}:
        try:
            projects |= {str(c) for c in parent.iterdir() if c.is_dir() and not c.name.startswith(".")}
        except OSError:
            pass
    for folder in sorted(projects):
        root = Path(folder)
        if (root in (HOME, Path("/")) or "/scratch-workspaces/" in folder or "/.claude/worktrees/" in folder
                or folder.startswith(str(HOME / ".codex"))):
            continue  # home holds the global skills; scratch and worktree copies would repeat a project
        for p in sorted(root.glob(".claude/skills/*/SKILL.md")):
            add(p, "Project: " + root.name, fallback=p.parent.name, project=root.name)
        for p in sorted(root.glob(".claude/commands/*.md")):
            add(p, "Project: " + root.name, fallback=p.stem, project=root.name)
        for sub in (".agents/skills", ".codex/skills"):
            for p in sorted(root.glob(sub + "/*/SKILL.md")):
                add(p, "Project: " + root.name, fallback=p.parent.name, project=root.name, app="Codex")
    seen, unique = set(), []
    for r in rows:
        if (r["name"], r["project"]) not in seen:
            seen.add((r["name"], r["project"])); unique.append(r)
    return unique

def start_link(t):
    """claude://code/new opens a new Claude Code session in the task's folder with the brief typed in."""
    from urllib.parse import quote
    f = t["fields"]
    brief = [f"This session works on task {t['id']} from the TODO list configured for Agent OS.",
             "", f"Task: {t['title']}"]
    if t["notes"]:
        brief += ["", "Notes:"] + [f"- {n}" for n in t["notes"]]
    brief += ["", "Rules: ask the user before pushing, creating remotes, deleting anything or spending money. "
              "Follow the project's CLAUDE.md or AGENTS.md. Start by reading the project and proposing a plan. "
              "Ask the user before marking the task complete."]
    url = "claude://code/new?"
    folder = Path(os.path.expanduser(f.get("project", ""))) if f.get("project") else None
    if folder and folder.is_dir() and str(folder.resolve()).startswith(str(HOME)):
        url += "folder=" + quote(str(folder.resolve()), safe="") + "&"
    return url + "q=" + quote("\n".join(brief)[:4000], safe="")

def scan_todos(agents):
    """The configured task list, read through todo.py so both share one parser.
    Active tasks pick up their session's live status. Done keeps the 8 most recent."""
    try:
        import todo
        if todo.TODO is None:
            return []
        items = todo.parse()
    except Exception as e:
        print("todos: could not read Brain/TODO.md:", e, file=sys.stderr)
        return []
    status, started, known, vendor = {}, {}, set(), {}
    for a in agents:
        m = re.search(r"session=(local_[A-Za-z0-9-]+)|threads/([A-Za-z0-9-]+)", a.get("openUrl") or "")
        if m and a["kind"] == "Session":
            sid = m.group(1) or m.group(2)
            status[sid] = a["status"]
            known.add(sid)
            vendor[sid] = a["vendor"].lower()
            if a.get("taskId"):
                started.setdefault(a["taskId"], sid)  # agents are sorted newest-first within a status
    # A task started from the board's Start button becomes Active once its session shows up.
    link = [t for t in items if t["group"] == "todo" and t["id"] in started]
    if link and SYNC_TODOS:
        try:
            lines = todo.read_lines()
            for t in link:
                t = next(x for x in todo.parse(lines) if x["id"] == t["id"])
                t["fields"].update(started=todo.today(), session=started[t["id"]], agent=vendor.get(started[t["id"]], ""))
                todo.put(lines, t, "active")
            todo.write_lines(lines)
            items = todo.parse()
        except Exception as e:
            print("todos: could not mark a started task active:", e, file=sys.stderr)
    rows, done = [], 0
    for i, t in enumerate(items):
        f = t["fields"]
        if t["group"] == "done":
            done += 1
            if done > 8:
                continue
        sid = f.get("session", "")
        url = ("claude://code/continue?session=" + sid if re.fullmatch(r"local_[A-Za-z0-9-]{1,64}", sid)
               else "codex://threads/" + sid if re.fullmatch(r"[A-Za-z0-9-]{8,64}", sid) else "")
        rows.append({"key": t["id"] or f"row{i}", "id": t["id"], "group": t["group"], "title": clean(t["title"], 120),
                     "project": clean(f.get("project", ""), 60), "added": f.get("added", ""),
                     "started": f.get("started", ""), "done": f.get("done", ""),
                     "summary": clean(f.get("summary", ""), 200), "openUrl": url,
                     "sessionStatus": status.get(sid, "ended" if sid else ""),
                     "sessionGone": bool(sid and sid not in known),
                     "agent": f.get("agent") or vendor.get(sid, ""),
                     "startUrl": start_link(t) if t["group"] == "todo" else "",
                     "notes": [clean(n, 240) for n in t["notes"][:6]], "order": i})
    return rows

# ---------- office departments ----------
LOCAL_TOOLS = (
    ('Ollama', 'Language models', r'(?:/ollama(?:\s|$)|/ollama/)'),
    ('LM Studio', 'Language models', r'(?:lm studio\.app/|/\.lmstudio/|(?:^|/)llmster(?:\s|$))'),
    ('ComfyUI', 'Image / video generation', r'(?:comfyui|comfy-ui)'),
    ('Diffusion', 'Image / video generation', r'(?:stable.diffusion|diffusionbee|draw.things|invokeai|automatic1111|/forge/)'),
    ('Voicebox', 'Voice / audio', r'voicebox'),
    ('Phonon', 'Voice / audio', r'phonon'),
    ('Whisper', 'Transcription', r'(?:whisper(?:-cli|-server|\.cpp)?(?:\s|/|$))'),
    ('MLX / tensor jobs', 'AI compute', r'(?:mlx_lm|mlx\.lm|mlx_audio|torchrun|accelerate launch|llama-server|llama-cli|vllm)'),
)
# Optional read-only loopback status. No inference routes are allowed.
LOCAL_STATUS_ROUTES = {
    'Ollama': ('/api/ps',),
    'ComfyUI': ('/queue', '/system_stats'),
    'Voicebox': ('/health', '/models/status', '/tasks/active'),
}
LOCAL_STATUS_PORTS = {'Ollama': 11434, 'ComfyUI': 8188, 'Voicebox': 17493}
LOCAL_STATUS_TIMEOUT = 1.5
LOCAL_STATUS_BYTES = 2 * 1024 * 1024
LOCAL_STATUS_TTL = 15
LOCAL_STATUS_RETRY = 60
LMS_CLI = HOME / '.lmstudio/bin/lms'


def local_status_json(provider, port, path):
    import urllib.request
    if path not in LOCAL_STATUS_ROUTES.get(provider, ()) or type(port) is not int or not 1 <= port <= 65535:
        return None
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    # Numeric loopback prevents DNS changes; no proxy or redirect can leave this Mac.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request('http://127.0.0.1:' + str(port) + path, method='GET', headers={'Accept': 'application/json'})
    try:
        with opener.open(request, timeout=LOCAL_STATUS_TIMEOUT) as response:
            raw = response.read(LOCAL_STATUS_BYTES + 1)
        if len(raw) > LOCAL_STATUS_BYTES:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TimeoutError):
        return None


def status_number(value, scale=1):
    import math
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return round(value / scale, 1)


def loaded_models(items, name_key, size_key=None, vram_key=None):
    if not isinstance(items, list) or any(not isinstance(r, dict) or not isinstance(r.get(name_key), str) for r in items):
        return None
    models = []
    for row in items[:128]:
        models.append({'name': clean(row[name_key], 100),
                       'sizeMB': status_number(row.get(size_key), 1024**2) if size_key else None,
                       'deviceMB': status_number(row.get(vram_key), 1024**2) if vram_key else None})
    return models


def comfy_metadata(queue, stats):
    result = {}
    if isinstance(queue, dict) and all(isinstance(queue.get(k), list) and all(isinstance(j, list) and len(j) >= 2 and isinstance(j[1], str) for j in queue[k]) for k in ('queue_running', 'queue_pending')):
        running, pending = queue['queue_running'], queue['queue_pending']
        result.update(activeJobs=len(running), queuedJobs=len(pending), generationState='running' if running else 'idle', workflowModels=[])
        # Only loader model names leave memory; workflow text, inputs and output paths never do.
        for job in running[:32]:
            workflow = job[2] if isinstance(job, list) and len(job) > 2 and isinstance(job[2], dict) else {}
            for node in workflow.values():
                if not isinstance(node, dict) or not re.fullmatch(r'(?:CheckpointLoaderSimple|CheckpointLoader|UNETLoader|VAELoader|LoraLoader|LoraLoaderModelOnly)', str(node.get('class_type', ''))):
                    continue
                inputs = node.get('inputs') or {}
                if not isinstance(inputs, dict): continue
                for key in ('ckpt_name', 'unet_name', 'vae_name', 'lora_name'):
                    if isinstance(inputs.get(key), str):
                        name = clean(Path(inputs[key]).name, 100)
                        if name and name not in result['workflowModels']: result['workflowModels'].append(name)
        result['workflowModels'] = result['workflowModels'][:32]
    if isinstance(stats, dict) and isinstance(stats.get('devices'), list):
        result['devices'] = []
        for d in stats['devices'][:8]:
            if not isinstance(d, dict): continue
            result['devices'].append({'name': clean(d.get('name', 'Device'), 90), 'type': clean(d.get('type', 'Unknown'), 30),
                                      'totalMB': status_number(d.get('vram_total'), 1024**2), 'freeMB': status_number(d.get('vram_free'), 1024**2)})
    result['metadataStatus'] = 'ok' if 'activeJobs' in result else 'partial' if 'devices' in result else 'unavailable'
    return result


def voicebox_metadata(health, model_status, tasks):
    result = {}
    if isinstance(health, dict) and isinstance(health.get('model_loaded'), bool):
        result['modelLoaded'] = health['model_loaded']
        result['backend'] = clean(health.get('backend_type', ''), 40)
        result['deviceUsedMB'] = status_number(health.get('vram_used_mb'))
    if isinstance(model_status, dict) and isinstance(model_status.get('models'), list):
        records = model_status['models']
        if all(isinstance(r, dict) and isinstance(r.get('loaded'), bool) and isinstance(r.get('model_name'), str) for r in records):
            result['loadedModels'] = [{'name': clean(r.get('display_name') or r['model_name'], 100), 'sizeMB': status_number(r.get('size_mb')), 'deviceMB': None} for r in records if r['loaded']][:128]
    if isinstance(tasks, dict) and all(isinstance(tasks.get(k), list) for k in ('generations', 'downloads')) and all(isinstance(g, dict) and isinstance(g.get('task_id'), str) for g in tasks['generations']):
        result.update(activeJobs=len(tasks['generations']), downloads=[])
        for d in tasks['downloads'][:32]:
            if not isinstance(d, dict): continue
            raw_status = d.get('status')
            state = raw_status if raw_status in ('pending', 'queued', 'downloading', 'running', 'completed', 'failed', 'error', 'cancelled') else 'unknown'
            result['downloads'].append({'model': clean(d.get('model_name', 'Model'), 100), 'state': state,
                                        'current': status_number(d.get('current')), 'total': status_number(d.get('total'))})
        result['generationState'] = 'running' if result['activeJobs'] else 'idle'
        # text_preview, profile IDs, audio and unfiltered errors are deliberately omitted.
    result['metadataStatus'] = 'ok' if 'activeJobs' in result and 'loadedModels' in result else 'partial' if result else 'unavailable'
    return result


def lm_studio_metadata(provider, commands):
    import os
    if not LMS_CLI.is_file(): return {'metadataStatus': 'unavailable'}
    # The installed CLI uses the already running local daemon; don't start a server.
    if not any('llmster' in value for pid, value in commands.items() if pid in provider['pids']):
        return {'metadataStatus': 'unavailable'}
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith('_PROXY') and not k.startswith(('LMSTUDIO_', 'LMS_'))}
    try:
        p = subprocess.run([str(LMS_CLI), 'ps', '--json'], capture_output=True, text=True, timeout=4, env=env)
        rows = json.loads(p.stdout) if p.returncode == 0 and len(p.stdout) <= LOCAL_STATUS_BYTES else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        rows = None
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        return {'metadataStatus': 'unavailable'}
    models = []
    for row in rows[:128]:
        name = row.get('identifier') or row.get('modelKey') or row.get('displayName') or row.get('path')
        if not isinstance(name, str): return {'metadataStatus': 'unavailable'}
        models.append({'name': clean(name, 100), 'sizeMB': status_number(row.get('sizeBytes'), 1024**2), 'deviceMB': None})
    return {'metadataStatus': 'ok', 'loadedModels': models}


def enrich_local_ai(result, services, commands):
    from concurrent.futures import ThreadPoolExecutor
    by_name = {r['name']: r for r in result['providers']}
    def probe(name):
        provider = by_name[name]
        hit = _cache.get('_local_status:' + name)
        ttl = LOCAL_STATUS_RETRY if hit and hit.get('data', {}).get('metadataStatus') == 'unavailable' else LOCAL_STATUS_TTL
        if hit and hit.get('pids') == provider['pids'] and 0 <= NOW - hit.get('at', 0) < ttl:
            _used['_local_status:' + name] = hit
            return hit['data']
        if name == 'LM Studio':
            data = lm_studio_metadata(provider, commands)
            source = 'Local daemon: lms ps --json'
        else:
            ports = sorted({s['port'] for s in services if s['pid'] in provider['pids']}) or [LOCAL_STATUS_PORTS[name]]
            port = LOCAL_STATUS_PORTS[name] if LOCAL_STATUS_PORTS[name] in ports else ports[0]
            source = 'Read-only 127.0.0.1:' + str(port)
            if name == 'Ollama':
                raw = local_status_json(name, port, '/api/ps')
                models = loaded_models(raw.get('models'), 'name', 'size', 'size_vram') if raw else None
                data = {'metadataStatus': 'ok', 'loadedModels': models} if models is not None else {'metadataStatus': 'unavailable'}
            elif name == 'ComfyUI':
                data = comfy_metadata(local_status_json(name, port, '/queue'), local_status_json(name, port, '/system_stats'))
            else:
                # Sequential requests avoid flooding the single Voicebox backend during model work.
                health = local_status_json(name, port, '/health')
                tasks = local_status_json(name, port, '/tasks/active')
                models = local_status_json(name, port, '/models/status')
                data = voicebox_metadata(health, models, tasks)
        data.update(observedAt=iso(NOW), source=source)
        _used['_local_status:' + name] = {'at': NOW, 'pids': provider['pids'], 'data': data}
        return data
    names = [name for name in ('Ollama', 'LM Studio', 'ComfyUI', 'Voicebox') if by_name[name]['processes'] > 0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        probes = list(pool.map(probe, names))
    jobs = []
    for provider in result['providers']:
        provider.update(metadataStatus='unsupported' if provider['processes'] else 'not-running', loadedModels=None,
                        activeJobs=None, queuedJobs=None, generationState='unknown', source='Process metadata')
    for name, data in zip(names, probes):
        p = by_name[name]; p.update(data)
        loaded = p.get('loadedModels')
        if isinstance(loaded, list): p['models'] = [m['name'] for m in loaded]
        if p.get('activeJobs') is not None:
            count, queued = p['activeJobs'], p.get('queuedJobs')
            p['state'] = str(count) + ' generating' if count else str(queued) + ' queued' if queued else 'Idle · no active jobs'
            if count: jobs.append({'tool': name, 'kind': 'Voice generation' if name == 'Voicebox' else 'Workflow generation', 'state': 'running', 'count': count})
            if queued: jobs.append({'tool': name, 'kind': 'Workflow generation', 'state': 'queued', 'count': queued})
            downloads = [d for d in p.get('downloads', []) if d['state'] not in ('completed', 'cancelled')]
            download_states = []
            for state, source_states in (('failed', ('error', 'failed')), ('downloading', ('downloading', 'running')), ('queued', ('pending', 'queued')), ('unknown', ('unknown',))):
                total = sum(d['state'] in source_states for d in downloads)
                if total:
                    jobs.append({'tool': name, 'kind': 'Model download', 'state': state, 'count': total})
                    download_states.append(str(total) + ' ' + state)
            if download_states and not count: p['state'] = 'Downloads · ' + ', '.join(download_states)
        elif isinstance(loaded, list):
            p['state'] = str(len(loaded)) + ' models loaded · generation unknown' if loaded else 'Ready · no models loaded'
        elif p['metadataStatus'] == 'unavailable':
            p['state'] = 'Runtime present · status unavailable'
    result.update(source='Passive processes + approved read-only localhost status; no inference requests', jobs=jobs,
                  activeJobs=sum(p['activeJobs'] or 0 for p in result['providers']), queuedJobs=sum(p['queuedJobs'] or 0 for p in result['providers']),
                  loadedModelCount=sum(len(p['loadedModels']) for p in result['providers'] if isinstance(p['loadedModels'], list)),
                  activityStatus='running' if any(j['state'] == 'running' for j in jobs) else 'partial')
    return result



VAULT_PATH = VAULT_DIR
DEPARTMENT_LIMIT = 16


def local_output(args, timeout=4):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def process_records(text):
    rows = []
    for line in (text or '').splitlines():
        bits = line.split(None, 5)
        if len(bits) != 6:
            continue
        try:
            pid, parent, elapsed, cpu, rss, name = bits
            rows.append({'pid': int(pid), 'parent': int(parent), 'cpu': float(cpu),
                         'memMB': round(int(rss) / 1024, 1), 'name': name})
        except ValueError:
            continue
    return rows


def office_processes():
    text = local_output(['ps', '-axo', 'pid=,ppid=,etime=,%cpu=,rss=,comm='])
    args = local_output(['ps', '-axo', 'pid=,args='])
    # Arguments are matched in memory, never copied into a dataset.
    commands = {}
    for line in (args or '').splitlines():
        bits = line.strip().split(None, 1)
        if len(bits) == 2 and bits[0].isdigit():
            commands[int(bits[0])] = bits[1]
    return text is not None and args is not None, process_records(text), commands


def scan_machine(rows, available):
    import shutil
    disk = shutil.disk_usage(HOME)
    result = {'status': 'ok' if available else 'unavailable', 'processCount': len(rows),
              'logicalCpus': os.cpu_count(), 'cpuPercent': round(sum(r['cpu'] for r in rows), 1),
              'load': [round(v, 2) for v in os.getloadavg()],
              'diskFreeGB': round(disk.free / 1024**3, 1), 'diskTotalGB': round(disk.total / 1024**3, 1),
              'top': [], 'processList': [], 'services': [], 'servicesStatus': 'unavailable'}
    result['processList'] = [{'pid': r['pid'], 'name': clean(Path(r['name']).name, 80), 'cpu': r['cpu'], 'memMB': r['memMB']}
                             for r in sorted(rows, key=lambda r: (-r['cpu'], -r['memMB']))]
    result['top'] = result['processList'][:DEPARTMENT_LIMIT]
    total = local_output(['sysctl', '-n', 'hw.memsize'])
    result['ramTotalMB'] = round(int(total.strip()) / 1024**2) if total and total.strip().isdigit() else None
    vm = local_output(['vm_stat'])
    result['ramAvailableMB'] = None
    if vm:
        page = re.search(r'page size of (\d+) bytes', vm)
        counts = dict(re.findall(r'^(Pages [^:]+):\s+(\d+)\.', vm, re.M))
        if page:
            pages = sum(int(counts.get(k, 0)) for k in ('Pages free', 'Pages inactive', 'Pages speculative'))
            result['ramAvailableMB'] = round(pages * int(page.group(1)) / 1024**2)
    listeners = local_output(['lsof', '-nP', '-iTCP', '-sTCP:LISTEN', '-Fpcn'], timeout=3)
    if listeners is not None:
        result['servicesStatus'] = 'ok'
        pid, name = None, ''
        seen = set()
        for line in listeners.splitlines():
            if line.startswith('p') and line[1:].isdigit(): pid = int(line[1:])
            elif line.startswith('c'): name = clean(line[1:], 60)
            elif line.startswith('n') and pid is not None:
                port = re.search(r':(\d+)$', line)
                if port and (pid, int(port.group(1))) not in seen:
                    seen.add((pid, int(port.group(1))))
                    result['services'].append({'pid': pid, 'name': name, 'port': int(port.group(1))})
        result['services'].sort(key=lambda r: (r['port'], r['pid']))
    return result


def scan_local_ai(rows, commands, available):
    providers = []
    used = set()
    for name, category, pattern in LOCAL_TOOLS:
        matches = []
        for r in rows:
            if r['pid'] in used: continue
            # Match the executable or known runtime arguments; exclude this collector's shell commands.
            executable = r['name'].lower()
            args = commands.get(r['pid'], '')
            is_script = re.search(r'/(?:python[\d.]*|node|uv)$', executable)
            signal = executable
            if is_script:
                import shlex
                try:
                    argv = shlex.split(args)
                except ValueError:
                    argv = []
                if '-c' not in argv and '-e' not in argv:
                    scripts = [token for token in argv[1:] if token.lower().endswith(('.py', '.js', '.mjs', '.cjs'))]
                    if '-m' in argv and argv.index('-m') + 1 < len(argv): scripts.append(argv[argv.index('-m') + 1])
                    signal += ' ' + ' '.join(scripts)
            if re.search(pattern, signal, re.I):
                matches.append(r); used.add(r['pid'])
        models = []
        for r in matches:
            arg = commands.get(r['pid'], '')
            m = re.search(r'(?:--model(?:-path)?|-m)\s+(?:"([^"]+)"|\x27([^\x27]+)\x27|(\S+))', arg)
            if m:
                model = clean(Path(next(v for v in m.groups() if v is not None)).name, 80)
                if model and model not in models: models.append(model)
        providers.append({'name': name, 'category': category, 'processes': len(matches),
                          'pids': [r['pid'] for r in matches], 'cpu': round(sum(r['cpu'] for r in matches), 1),
                          'memMB': round(sum(r['memMB'] for r in matches), 1), 'models': models,
                          'state': 'Runtime present; generation unknown' if matches else 'No matching process observed'})
    return {'status': 'ok' if available else 'unavailable', 'providers': providers,
            'source': 'Passive process metadata; no generation or network probes',
            'activityStatus': 'unknown', 'jobs': []}


def scan_ios(rows, commands, available, include_private=False):
    xcode = [r for r in rows if r['name'].endswith('/Contents/MacOS/Xcode')]
    simulator = [r for r in rows if r['name'].endswith('/Simulator.app/Contents/MacOS/Simulator')]
    builds = []
    device_ids = {}
    for r in rows:
        name = Path(r['name']).name
        command = commands.get(r['pid'], '')
        # Xcode-launched UI test runners are simulator apps, usually parented by launchd_sim.
        runner = bool(re.search(r'(?i)(?:^|/)[^/]*(?:uitests|xctest)[^/]*-runner\.app/', r['name']))
        if name in ('xcodebuild', 'xctest', 'XCTRunner', 'swift-frontend', 'swiftc', 'ibtool', 'actool', 'metal') or '.xctest/' in r['name'] or runner:
            testing = runner or name in ('xctest', 'XCTRunner') or '.xctest/' in r['name'] or name == 'xcodebuild' and re.search(r'\b(test|test-without-building)\b', command)
            project = re.search(r'-(?:project|workspace)\s+(?:"([^"]+)"|\x27([^\x27]+)\x27|(\S+))', command)
            project = clean(Path(next(v for v in project.groups() if v is not None)).stem, 80) if project else 'Unknown'
            device = re.search(r'/CoreSimulator/Devices/([0-9A-Fa-f-]{36})/', r['name']) or re.search(r'(?i)(?:destination\s+)?id=([0-9a-f-]{36})', command)
            if not device and (runner or name in ('xctest', 'XCTRunner')):
                device = re.search(r'/CoreSimulator/Devices/([0-9A-Fa-f-]{36})/', lsof_path(r['pid'], 'cwd'))
            if device: device_ids[r['pid']] = device.group(1).upper()
            builds.append({'pid': r['pid'], 'name': clean(name), 'project': project, 'state': 'Test command running' if testing else 'Build / compiler process running',
                           'source': 'Simulator test runner' if runner else 'Process command', 'device': None,
                           'cpu': r['cpu'], 'memMB': r['memMB']})
    devices = None
    device_names = {}
    # Query only an already running simulator service; never boot devices to monitor them.
    if any('CoreSimulatorService' in r['name'] for r in rows):
        raw = local_output(['xcrun', 'simctl', 'list', 'devices', 'booted', '--json'], timeout=4)
        if raw:
            try:
                data = json.loads(raw)
                device_names = {str(d.get('udid', '')).upper(): clean(d.get('name', 'Simulator'), 70) for group in data.get('devices', {}).values() for d in group if d.get('state') == 'Booted'}
                devices = [{'name': clean(d.get('name', 'Simulator'), 70),
                            'runtime': clean(runtime.rsplit('.', 1)[-1], 60), 'state': clean(d.get('state', 'Unknown'), 40)}
                           for runtime, group in data.get('devices', {}).items() for d in group if d.get('state') == 'Booted']
            except (ValueError, TypeError):
                pass
    elif available:
        devices = []
    for build in builds:
        build['device'] = device_names.get(device_ids.get(build['pid'], ''))
    build_pids = {r['pid'] for r in builds}
    related = [r for r in rows if 'Xcode.app/' in r['name'] or 'CoreSimulator' in r['name'] or r['pid'] in build_pids]
    result = {'status': 'ok' if available else 'unavailable', 'xcodeOpen': bool(xcode),
            'simulatorAppOpen': bool(simulator), 'builds': builds, 'devices': devices,
            'processes': len(related), 'cpu': round(sum(r['cpu'] for r in related), 1),
            'memMB': round(sum(r['memMB'] for r in related), 1),
            'source': 'Process presence and read-only simulator status; no builds or devices started'}
    private = {'devices': device_ids, 'parents': {r['pid']: r['parent'] for r in rows}}
    return (result, private) if include_private else result


def vault_history(files, complete):
    old = _cache.get('_vault_history', {})
    if not complete:
        if old: _used['_vault_history'] = old
        return {'status': 'unavailable', 'events': [], 'source': 'Incomplete filesystem scan; previous baseline preserved'}
    previous = old.get('files', {})
    events = [r for r in old.get('events', []) if NOW - 30 * 86400 <= (parse_ts(r.get('observedAt')) or 0) <= NOW]
    if old:
        for key, row in files.items():
            before = previous.get(key)
            if before is None or row['mtime'] != before['mtime']:
                events.append({'path': row['path'], 'event': 'Newly observed' if before is None else 'Modification time changed', 'modifiedAt': iso(row['mtime']), 'observedAt': iso(NOW)})
        for key, row in previous.items():
            if key not in files:
                events.append({'path': row['path'], 'event': 'No longer observed', 'modifiedAt': iso(row['mtime']), 'observedAt': iso(NOW)})
    events.sort(key=lambda r:r['observedAt'], reverse=True)
    truncated = len(events) > 500 or bool(old.get('truncated'))
    baseline = old.get('baselineAt') or iso(NOW)
    _used['_vault_history'] = {'files': files, 'events': events[:500], 'baselineAt': baseline, 'truncated': truncated}
    return {'status': 'ok', 'events': events[:500], 'baselineAt': baseline, 'truncated': truncated,
            'source': 'Metadata observations since collector cache baseline; rescan or source edit resets history; up to 500 events / 30 days'}


def scan_vault():
    # Metadata fallback: Obsidian CLI did not respond during local discovery. Never read note bodies.
    recent, count, changed = [], 0, 0
    files_seen, errors = {}, []
    vault_history({}, False)  # Preserve the baseline even if the root or traversal is unavailable.
    try:
        if VAULT_PATH is None or not VAULT_PATH.is_dir(): return {'status': 'unavailable', 'recent': []}
        for base, dirs, files in os.walk(VAULT_PATH, onerror=errors.append):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', 'Backups')]
            for name in files:
                if not name.endswith('.md') or name.startswith('.'): continue
                p = Path(base) / name
                try:
                    stat = p.stat()
                    if p.is_symlink(): continue
                except OSError as error:
                    errors.append(error)
                    continue
                relative = str(p.relative_to(VAULT_PATH))
                files_seen[hashlib.sha256(relative.encode()).hexdigest()] = {'path': clean(relative, 150), 'mtime': stat.st_mtime}
                count += 1
                if stat.st_mtime >= CUTOFF:
                    changed += 1
                    recent.append({'path': clean(str(p.relative_to(VAULT_PATH)), 150), 'modifiedAt': iso(stat.st_mtime)})
        recent.sort(key=lambda r: r['modifiedAt'], reverse=True)
        return {'status': 'ok', 'noteCount': count, 'changedCount': changed, 'windowHours': HOURS,
                'recent': recent[:DEPARTMENT_LIMIT], 'scanIncomplete': bool(errors), 'history': vault_history(files_seen, not errors), 'source': 'Filesystem modification times; no note contents or sync verification'}
    except OSError:
        return {'status': 'unavailable', 'recent': []}


def machine_warnings(machine, local_ai):
    # Disposable sampling state resets on rescan/source changes; it is not durable incident history.
    previous = _cache.get('_machine_warnings', {})
    gap = NOW - previous.get('at', 0)
    prior = previous.get('checks', {}) if 0 < gap <= HEALTH_GAP_SECONDS else {}
    saved, checks = {}, []
    def check(key, title, bad, reading, threshold):
        old = prior.get(key, {})
        since = old.get('since', NOW) if bad is True else None
        samples = old.get('samples', 0) + 1 if bad is True else 0
        duration = max(0, NOW - since) if since is not None else 0
        state = 'unavailable' if bad is None else 'warning' if bad and duration >= HEALTH_HOLD_SECONDS and samples >= 3 else 'watching' if bad else 'within threshold'
        if bad is True: saved[key] = {'since': since, 'samples': samples}
        checks.append({'key': key, 'title': title, 'state': state, 'reading': reading, 'threshold': threshold,
                       'observedSeconds': round(duration), 'samples': samples})
    available = machine.get('status') == 'ok'
    cpu, cores = machine.get('cpuPercent'), machine.get('logicalCpus')
    cpu_pct = cpu / cores if available and type(cpu) in (int, float) and type(cores) is int and cores > 0 else None
    check('cpu', 'CPU load', None if cpu_pct is None else cpu_pct >= 85,
          None if cpu_pct is None else str(round(cpu_pct, 1)) + '% of logical CPU capacity', '85% of summed logical CPU capacity')
    free, total = machine.get('ramAvailableMB'), machine.get('ramTotalMB')
    mem_pct = free / total * 100 if available and type(free) in (int, float) and type(total) in (int, float) and total > 0 else None
    check('memory', 'Available memory estimate', None if mem_pct is None else mem_pct <= 10,
          None if mem_pct is None else str(round(mem_pct, 1)) + '% available (includes reclaimable pages)', '10% available or less; estimate, not OS memory-pressure status')
    disk, disk_total = machine.get('diskFreeGB'), machine.get('diskTotalGB')
    disk_pct = disk / disk_total * 100 if available and type(disk) in (int, float) and type(disk_total) in (int, float) and disk_total > 0 else None
    check('disk', 'Free disk', None if disk_pct is None else disk <= 20 or disk_pct <= 5,
          None if disk_pct is None else str(disk) + ' GB / ' + str(round(disk_pct, 1)) + '% free', '20 GB free or less, or 5% free or less')
    for provider in local_ai.get('providers', []):
        if provider['name'] not in ('Ollama', 'LM Studio', 'ComfyUI', 'Voicebox'): continue
        metadata = provider.get('metadataStatus')
        if local_ai.get('status') != 'ok' or metadata not in ('ok', 'unavailable', 'not-running'):
            bad, reading = None, 'Status unavailable'
        elif not provider.get('processes'):
            # Absence is not an outage for a service the user may have intentionally stopped.
            bad, reading = False, 'Runtime not observed; not a health check'
        else:
            bad, reading = metadata != 'ok', 'Status reads unavailable' if metadata != 'ok' else 'Approved status read succeeded'
        check('service:' + provider['name'], clean(provider['name'] + ' status'), bad, reading, 'Repeated unavailable status reads while runtime is observed; not proof of a crashed service')
    _used['_machine_warnings'] = {'at': NOW, 'checks': saved}
    return {'checks': checks, 'observedAt': iso(NOW), 'holdSeconds': HEALTH_HOLD_SECONDS, 'maxGapSeconds': HEALTH_GAP_SECONDS,
            'source': 'Observed samples in disposable collector cache; rescan, source edit or telemetry gap resets duration'}


def scan_departments():
    available, rows, commands = office_processes()
    machine = scan_machine(rows, available)
    local_ai = scan_local_ai(rows, commands, available)
    if available and LOCAL_STATUS:
        local_ai = enrich_local_ai(local_ai, machine['services'], commands)
    machine['health'] = machine_warnings(machine, local_ai)
    ios, private = scan_ios(rows, commands, available, include_private=True)
    return {'machine': machine, 'localAi': local_ai, 'ios': ios, 'vault': scan_vault()}, private


# ---------- test runs ----------
TEST_RUNNER = re.compile(r"/node_modules/(?:\.bin/(playwright|vitest)|(playwright|vitest)/[^ ]*cli[^ ]*)\s+(test|run)\b")
ISOLATED_PORT = re.compile(r"start-isolated-server\.mjs\s+(\d{4,5})\b")
SLOT_BASE = 3100                  # test slot n serves its app on 3100 + n
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOG_TAIL = 4 * 1024 * 1024
PASS_LINE = re.compile(r"^\s*(✓|√|ok\s+\d)")
FAIL_LINE = re.compile(r"^\s*(✘|×|x\s+\d)\s")
PROGRESS = re.compile(r"\[(\d+)/(\d+)\]")
TOTAL = re.compile(r"Running (\d+) tests? using")
LINE_FAIL = re.compile(r"^\s+\d+\) \[")      # --reporter=line prints only numbered failures

def etime_secs(et):
    d, _, rest = et.partition("-") if "-" in et else ("0", "", et)
    parts = [int(x) for x in rest.split(":")]
    while len(parts) < 3: parts.insert(0, 0)
    return int(d) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]

def lsof_path(pid, fd):
    """Path behind one file descriptor of a process, or '' (only our own user's processes answer)."""
    out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", fd, "-Fn"], capture_output=True, text=True).stdout
    names = [l[1:] for l in out.splitlines() if l.startswith("n")]
    return names[0] if names else ""

def log_counts(path):
    """Pass/fail counts from a runner's output file. Only counts leave this function, never log text."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - LOG_TAIL))
            text = ANSI.sub("", f.read().decode("utf-8", "replace"))
    except OSError:
        return None
    passed = failed = done = total = numbered = 0
    for line in text.splitlines():
        if PASS_LINE.match(line): passed += 1
        elif FAIL_LINE.match(line): failed += 1
        elif LINE_FAIL.match(line): numbered += 1
        m = PROGRESS.search(line)
        if m: done, total = int(m.group(1)), int(m.group(2))
        m = TOTAL.search(line)
        if m: total = int(m.group(1))
    if passed + failed == 0 and done:
        failed, passed = numbered, max(0, done - numbered)
    return {"passed": passed, "failed": failed, "done": max(done, passed + failed), "total": total}

def playwright_summary(path, start):
    # Only a new, uniquely owned output file can attribute a final test summary to this process.
    try:
        stat = os.stat(path)
        started = parse_ts(process_start_iso(start))
        if started is None or not getattr(stat, 'st_birthtime', 0) or stat.st_birthtime < started - 1: return None
        with open(path, 'rb') as f:
            f.seek(max(0, stat.st_size - LOG_TAIL))
            lines = ANSI.sub('', f.read().decode('utf-8', 'replace')).splitlines()[-30:]
        matches = [re.match(r'^\s*(\d+)\s+(passed|failed)\b', line) for line in lines]
        matches = [m for m in matches if m]
        if not matches or any(re.search(r'(?i)interrupted|cancelled|canceled', line) for line in lines): return None
        counts = {'passed': 0, 'failed': 0, 'total': 0}
        for m in matches: counts[m.group(2)] = int(m.group(1))
        counts['total'] = counts['passed'] + counts['failed']
        return counts, iso(stat.st_mtime)
    except (OSError, ValueError): return None


def scan_tests(starts):
    """Playwright and Vitest runs going on right now: where, which branch, which test slot, how far."""
    out = local_output(["ps", "-axo", "pid=,ppid=,etime=,%cpu=,args="], timeout=8)
    if out is None: return "unavailable", [], []
    procs = []
    for line in out.splitlines():
        bits = line.split(None, 4)
        if len(bits) == 5:
            procs.append((int(bits[0]), int(bits[1]), bits[2], float(bits[3]), bits[4]))
    rows, observations = [], []
    matched = {p[0] for p in procs if TEST_RUNNER.search(p[4])}
    parents = {p[0]: p[1] for p in procs}
    def has_runner_ancestor(pid):
        seen = set()
        while pid in parents and pid not in seen:
            seen.add(pid); pid = parents[pid]
            if pid in matched: return True
        return False
    roots = {pid for pid in matched if not has_runner_ancestor(pid)}
    logs = {p[0]: next((q for q in (lsof_path(p[0], '1'), lsof_path(p[1], '1')) if q.startswith('/') and os.path.isfile(q)), '') for p in procs if p[0] in roots}
    for pid, ppid, et, cpu, args in procs:
        m = TEST_RUNNER.search(args)
        if not m or pid not in roots:
            continue
        if pid not in starts: return "unavailable", [], []
        tool = (m.group(1) or m.group(2)).capitalize()
        cwd = lsof_path(pid, "cwd")
        branch = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip() if cwd else ""
        port = next((int(p.group(1)) for c in procs if c[1] == pid for p in [ISOLATED_PORT.search(c[4])] if p), None)
        log = logs.get(pid, '')
        counts = log_counts(log) if log else None
        identity = hashlib.sha256(("script|" + str(pid) + "|" + starts[pid] + "|" + cwd + "|" + tool).encode()).hexdigest()
        rows.append({
            "id": identity,
            "tool": tool, "project": project_of(cwd), "folder": clean(cwd.rstrip("/").split("/")[-1], 60),
            "branch": clean(branch, 80) or "—", "port": port,
            "slot": port - SLOT_BASE if port and SLOT_BASE <= port < SLOT_BASE + 100 else None,
            "runningMin": round(etime_secs(et) / 60, 1), "cpu": round(cpu, 1), "startedAt": process_start_iso(starts[pid]),
            "hasLog": counts is not None, **(counts or {"passed": 0, "failed": 0, "done": 0, "total": 0}),
        })
        summary = playwright_summary(log, starts[pid]) if tool == 'Playwright' and log and list(logs.values()).count(log) == 1 else None
        observations.append({'id': identity, 'kind': 'script-test', 'tool': clean(tool), 'project': clean(project_of(cwd)),
                             'folder': clean(cwd.rstrip('/').split('/')[-1], 60), 'branch': clean(branch,80) or '—',
                             'slot': port-SLOT_BASE if port and SLOT_BASE <= port < SLOT_BASE+100 else None,
                             'startedAt': process_start_iso(starts[pid]), 'evidence': 'Playwright final test summary' if summary else 'Observed test process',
                             'sourceObservedAt': summary[1] if summary else iso(time.time()), 'counts': summary[0] if summary else None})
    rows.sort(key=lambda r: -r["runningMin"])
    return "ok", rows, observations


SCRIPT_BUILD = re.compile(r'^(?:\S*/)?(npm|pnpm|yarn)\s+(?:run\s+)?build(?:\s|$)')


def scan_script_builds(starts):
    raw = local_output(['ps', '-axo', 'pid=,ppid=,args='], timeout=8)
    if raw is None or starts is None: return 'unavailable', []
    procs = []
    for line in raw.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            procs.append((int(parts[0]), int(parts[1]), parts[2]))
    parents = {pid: ppid for pid, ppid, _ in procs}
    matched = {pid for pid, _, args in procs if SCRIPT_BUILD.match(args)}
    def has_build_parent(pid):
        seen = set()
        while pid in parents and pid not in seen:
            seen.add(pid); pid = parents[pid]
            if pid in matched: return True
        return False
    rows = []
    for pid, _, args in procs:
        match = SCRIPT_BUILD.match(args)
        if not match or has_build_parent(pid): continue
        if pid not in starts: return 'unavailable', []
        cwd = lsof_path(pid, 'cwd')
        branch = subprocess.run(['git','-C',cwd,'rev-parse','--abbrev-ref','HEAD'],capture_output=True,text=True).stdout.strip() if cwd else ''
        identity = hashlib.sha256(('build|' + str(pid) + '|' + starts[pid] + '|' + cwd).encode()).hexdigest()
        rows.append({'id':identity,'kind':'script-build','tool':clean(match.group(1)),'project':clean(project_of(cwd)),
                     'folder':clean(cwd.rstrip('/').split('/')[-1],60),'branch':clean(branch,80) or '—',
                     'slot':None,'startedAt':process_start_iso(starts[pid]),'evidence':'Observed build command',
                     'sourceObservedAt':iso(time.time()),'counts':None})
    return 'ok', rows


def process_starts():
    raw = local_output(['ps', '-axo', 'pid=,lstart='], timeout=8)
    if raw is None: return None
    return {int(a[0]): ' '.join(a[1:]) for line in raw.splitlines() if len(a := line.split()) == 6 and a[0].isdigit()}


def process_start_iso(value):
    try: return iso(datetime.strptime(value, '%a %b %d %H:%M:%S %Y').astimezone().timestamp())
    except ValueError: return None


def ios_observations(ios, starts, private=None):
    if ios['status'] != 'ok' or starts is None: return 'unavailable', []
    builds = ios['builds']
    private = private or {}
    devices, parents = private.get('devices', {}), private.get('parents', {})
    relevant = {r['pid']: r for r in builds if r.get('source') == 'Simulator test runner' or r['name'] in ('xcodebuild','xctest','XCTRunner')}
    def has_linked_ancestor(pid):
        seen, original = set(), pid
        while pid in parents and pid not in seen:
            seen.add(pid); pid = parents[pid]
            if pid in relevant:
                # Contradictory actual device identities defeat a parent-based grouping.
                a, b = devices.get(original), devices.get(pid)
                same_activity = relevant[original]['state'] == relevant[pid]['state']
                return same_activity and not (a and b and a != b)
        return False
    rows = []
    for r in builds:
        runner = r.get('source') == 'Simulator test runner'
        if not runner and r['name'] not in ('xcodebuild','xctest','XCTRunner'): continue
        # Device identity or a matching display name alone does not prove a shared run.
        if has_linked_ancestor(r['pid']): continue
        if r['pid'] not in starts: return 'unavailable', []
        testing = r['state'] == 'Test command running'
        ident = hashlib.sha256(('ios|' + str(r['pid']) + '|' + starts[r['pid']]).encode()).hexdigest()
        rows.append({'id': ident, 'kind': 'ios-test' if testing else 'ios-build', 'tool': clean(r['name']),
                     'project': clean(r.get('project') or 'Unknown'), 'folder': '', 'branch': '', 'slot': None,
                     'startedAt': process_start_iso(starts[r['pid']]), 'evidence': clean(r.get('source') or 'Process command'),
                     'sourceObservedAt': iso(time.time()), 'counts': None})
    return 'ok', rows

def main():
    OUT.mkdir(exist_ok=True)
    agents, commands, tasks, events = [], [], [], []
    cache_load()
    scan_claude(agents, commands, tasks, events)
    scan_codex(agents, commands, tasks, events)
    hidden = sum(1 for a in agents if a is None)
    agents = [a for a in agents if a and (a["kind"] == "Session" or not (a["toolCalls"] == 0 and a["status"] == "done"))]  # every session stays listed
    # Org chart: the busiest live orchestrator is Chief of staff; other sessions report to it, helpers to their parent.
    sessions = [a for a in agents if a["kind"] == "Session"]
    chiefs = sorted([a for a in sessions if a["pid"] and a["spawned"] >= 5], key=lambda a: -a["spawned"])
    chief = chiefs[0] if chiefs else None
    if chief:
        chief["role"], (chief["hat"], chief["hatColor"]) = "Chief of staff", CATALOG["Chief of staff"]
    ids = {a["id"] for a in agents}
    for a in agents:
        if a is chief:
            a["reportsTo"] = "you"
        elif a["kind"] == "Helper" and a.get("parent") in ids:
            a["reportsTo"] = a["parent"]
        else:
            a["reportsTo"] = chief["id"] if chief else "you"
    order = {"working": 0, "waiting": 1, "idle": 2, "done": 3}
    agents.sort(key=lambda a: (order.get(a["status"], 9), a["kind"] != "Session", -(parse_ts(a["lastActive"]) or 0)))
    shown = {a["id"] for a in agents}
    tasks = [t for t in tasks if t["agent"] in shown]
    total_cmds = len(commands)
    commands.sort(key=lambda c: c["at"], reverse=True)
    commands = commands[:250]
    for i, c in enumerate(commands):
        c["n"] = i + 1
    # hourly tool calls per vendor, last 24 h
    start_hour = int(NOW // 3600) * 3600 - 23 * 3600
    buckets = {}
    for h in range(24):
        for v in ("Claude", "Codex"):
            buckets[(start_hour + h * 3600, v)] = 0
    usage = {}
    for ts, v, m, e in events:
        u = usage.setdefault((v, m, e), {"vendor": v, "model": m, "effort": e, "toolCalls": 0})
        u["toolCalls"] += 1
    usage = sorted(usage.values(), key=lambda r: -r["toolCalls"])
    for ts, v, *_ in events:
        k = (int(ts // 3600) * 3600, v)
        if k in buckets:
            buckets[k] += 1
    timeline = [{"hour": iso(h), "vendor": v, "toolCalls": n} for (h, v), n in sorted(buckets.items())]
    procs = scan_processes()
    observed = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
    starts = process_starts()
    script_status, test_rows, script_history = scan_tests(starts or {}) if starts is not None else ('unavailable', [], [])
    build_status, build_history = scan_script_builds(starts)
    if build_status != 'ok': script_status = 'unavailable'
    departments, ios_private = scan_departments()
    ios_status, ios_history = ios_observations(departments['ios'], starts, ios_private)
    history = update_history(Path(__file__).resolve().parent / '.cache', script_history + build_history + ios_history,
                             {'scripts': script_status, 'ios': ios_status}, observed)
    summary = [{"collectedAt": iso(NOW), "windowHours": HOURS, "olderHelpersHidden": hidden,
                "toolCalls": len(events), "commands": total_cmds,
                "claudeToolCalls": sum(1 for e in events if e[1] == "Claude"),
                "codexToolCalls": sum(1 for e in events if e[1] == "Codex"), "sessionBudgets": scan_session_budgets(agents), "tokenEconomy": scan_token_economy(), "runHistory": history, **departments}]
    cache_save()
    for name, rows in (("agents", agents), ("commands", commands), ("tasks", tasks),
                       ("timeline", timeline), ("usage", usage), ("processes", procs), ("snapshot", summary),
                       ("skills", scan_skills()), ("todos", scan_todos(agents)), ("testruns", test_rows)):
        (OUT / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
        print(name, len(rows), (OUT / f"{name}.json").stat().st_size, "bytes")

if __name__ == "__main__":
    os.umask(0o077)
    main()
