#!/usr/bin/env python3
"""Agent OS, local and free: reruns collect.py as soon as session logs, skills or the page files
change (at most every MIN_GAP seconds), at least every minute, and right away when you press
Refresh. Open pages hear about each run over /events and redraw, or reload if their code changed.
Serves the page on http://127.0.0.1:8765.

No Claude runs, no tokens. Only listens on this Mac (127.0.0.1).
Run: python3 -I serve.py      (Ctrl+C to stop)
"""
import hashlib, http.server, json, os, re, subprocess, sys, threading, time
from urllib.parse import urlsplit
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from agent_os_config import CLAUDE_DIR, CODEX_DIR, TODO_FILE

try:
    PORT = int(os.environ.get('AGENT_OS_PORT', '8765'))
    if not 1 <= PORT <= 65535:
        raise ValueError
except ValueError:
    sys.exit('AGENT_OS_PORT must be an integer from 1 to 65535.')
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
EVERY = 60         # full collect at least this often (CPU and memory change without any file changing)
WATCH_EVERY = 3    # how often to check whether anything changed; a check costs about 0.1 s
MIN_GAP = 10       # a collect costs ~2.5 s of CPU, so busy agents trigger one at most this often
MAX_LISTENERS = 20
HOME = Path.home()
LOCK = threading.Lock()
LIVE = threading.Condition()  # guards STATE; notified after every collect
STATE = {"version": 0, "last_run": float("-inf"), "listeners": 0}
WATCH_TREES = [CLAUDE_DIR / "projects", CLAUDE_DIR / "sessions", CODEX_DIR / "sessions"]
WATCH_GLOBS = [(CLAUDE_DIR / "skills", "*/SKILL.md"), (CLAUDE_DIR / "commands", "*.md"),
               (CODEX_DIR / "skills", "*/SKILL.md"), (HOME / ".agents/skills", "*/SKILL.md"),
               (HERE, "*.html"), (HERE, "collect.py"), (HERE, "todo.py"),
               (HERE / "vendor", "d3.min.js")]
if TODO_FILE is not None:
    WATCH_GLOBS.append((TODO_FILE.parent, TODO_FILE.name))
# Only the link shapes collect.py writes (open a session, open a thread, start a TODO task). The in-app browser can't hand these schemes to macOS,
# so the page asks this server to run `open` for it.
OPENABLE = re.compile(r"claude://code/continue\?session=local_[A-Za-z0-9-]{1,64}|codex://threads/[A-Za-z0-9-]{1,64}"
                      r"|claude://code/new\?(?:folder=[A-Za-z0-9%._~-]{1,600}&)?q=[A-Za-z0-9%._~-]{1,14000}")

PAGE_ALIASES = {"/": "/local.html", "/index.html": "/local.html", "/commands": "/commands.html"}
DATASETS = ('agents', 'commands', 'tasks', 'timeline', 'usage', 'processes', 'snapshot', 'skills', 'todos', 'testruns')
PUBLIC_FILES = {"/local.html", "/commands.html"} | {f"/data/{name}.json" for name in DATASETS}

def build_page():
    shell = (HERE / "local-shell.html").read_text()
    page = (HERE / "index.html").read_text()
    d3 = (HERE / "vendor/d3.min.js").read_text()
    shell = shell.replace("<!--D3-->", "<script>\n" + d3 + "\n</script>")
    out, built = HERE / "local.html", shell.replace("<!--PAGE-->", page)
    if not out.exists() or out.read_text() != built:  # rewrite only on change, so open pages reload only then
        out.write_text(built)

def page_hashes():
    """Short content hashes of the two pages; a page reloads itself when its own hash changes."""
    h = lambda name: hashlib.sha1((HERE / name).read_bytes()).hexdigest()[:12]
    return {"local": h("local.html"), "commands": h("commands.html")}

def fingerprint():
    """File count and newest change time across everything the dashboard reads."""
    n, newest = 0, 0.0
    for tree in WATCH_TREES:
        for d, _, files in os.walk(tree):
            for f in files:
                try:
                    newest = max(newest, os.stat(os.path.join(d, f)).st_mtime); n += 1
                except OSError:
                    pass
    for base, pattern in WATCH_GLOBS:
        for f in base.glob(pattern):
            try:
                newest = max(newest, f.stat().st_mtime); n += 1
            except OSError:
                pass
    return n, newest

def collect(full=False):
    """Rebuild the page and rerun the collector. Returns an error string, or None.
    full=True is the hard refresh: the collector ignores its cache and re-reads every log."""
    with LOCK:  # a second click while one run is going waits for it instead of starting another
        try:
            build_page()
            env = dict(os.environ, AGENT_OS_FULL="1") if full else None
            subprocess.run([sys.executable, "-I", str(HERE / "collect.py")], cwd=HERE, env=env,
                           stdout=subprocess.DEVNULL, timeout=120 if full else 50, check=True)
        except Exception as e:
            print("collect failed:", e, file=sys.stderr)
            return str(e)
        finally:
            STATE["last_run"] = time.monotonic()
    with LIVE:
        STATE["version"] += 1
        LIVE.notify_all()
    return None

def collect_forever():
    seen = None
    while True:
        try:
            now = fingerprint()
        except Exception as e:
            print("watch failed:", e, file=sys.stderr); now = seen
        since = time.monotonic() - STATE["last_run"]
        if since >= EVERY or (now != seen and since >= MIN_GAP):
            seen = now  # taken before the run, so a change during it triggers the next one
            collect()
        time.sleep(WATCH_EVERY)

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(HERE), **k)
    def get_page(self, head=False):
        if self.headers.get("Host") not in HOSTS:
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        if path == "/events" and not head:
            self.events()
            return
        path = PAGE_ALIASES.get(path, path)
        if path not in PUBLIC_FILES:
            self.send_error(404)
            return
        target = HERE / path.lstrip('/')
        if not target.is_file() or target.resolve() != target:
            self.send_error(404)
            return
        self.path = path
        if head:
            super().do_HEAD()
        else:
            super().do_GET()

    def do_GET(self):
        self.get_page()

    def do_HEAD(self):
        self.get_page(head=True)
    def do_POST(self):
        # The custom header can't be sent cross-site without a CORS preflight this server never
        # answers, and the Host check stops DNS rebinding, so only this page can trigger a run.
        if (self.path not in ("/refresh", "/rescan", "/open") or self.headers.get("X-Agent-OS") != "1"
                or self.headers.get("Host") not in HOSTS):
            self.send_error(403); return
        if self.path == "/open":
            n = int(self.headers.get("Content-Length") or 0)
            url = self.rfile.read(min(n, 16384)).decode("utf-8", "replace")
            if not OPENABLE.fullmatch(url):
                self.send_error(400); return
            err = None if subprocess.run(["/usr/bin/open", url]).returncode == 0 else "open failed"
        else:
            err = collect(full=self.path == "/rescan")
        body = json.dumps({"ok": err is None, "error": err}).encode()
        self.send_response(200 if err is None else 500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def events(self):
        """Server-sent events: `hello` on connect, `update` after each collect, a ping every 25 s."""
        with LIVE:
            full = STATE["listeners"] >= MAX_LISTENERS
            if not full:
                STATE["listeners"] += 1
            seen = STATE["version"]
        if full:
            self.send_error(503); return
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(f"event: hello\ndata: {json.dumps(page_hashes())}\n\n".encode()); self.wfile.flush()
            while True:
                with LIVE:
                    LIVE.wait_for(lambda: STATE["version"] != seen, timeout=25)
                    changed, seen = STATE["version"] != seen, STATE["version"]
                msg = f"event: update\ndata: {json.dumps(page_hashes())}\n\n" if changed else ": ping\n\n"
                self.wfile.write(msg.encode()); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the page closed or reloaded
        finally:
            with LIVE:
                STATE["listeners"] -= 1
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    os.umask(0o077)
    threading.Thread(target=collect_forever, daemon=True).start()
    print(f"Agent OS on http://127.0.0.1:{PORT}  (live: collects within {MIN_GAP}s of a change, at least every {EVERY}s)")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
