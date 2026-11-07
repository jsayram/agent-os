#!/usr/bin/env python3
"""Agent OS task list. Configure the path with TODO_FILE or AGENT_OS_VAULT.

One script for every agent, so the file keeps one format. /add-task, /start-task and /close-task
(Claude and Codex versions) call it, and collect.py imports parse() to show the list on Agent OS.

  python3 -I todo.py list [--json]
  python3 -I todo.py add "Title" [--project PATH] [--note TEXT ...]
  python3 -I todo.py show ID
  python3 -I todo.py start ID --session SESSION_ID
  python3 -I todo.py check ID              # git state of the task's project; exit 1 if work is unsaved
  python3 -I todo.py done ID [--summary TEXT]
  python3 -I todo.py reopen ID             # back to To do (a session that was closed too early)
  python3 -I todo.py delete ID --yes       # remove the task from the list for good; prints its old session id
  python3 -I todo.py resume ID             # a Done task goes back to In progress, keeping its session
  python3 -I todo.py back ID               # same, and prints the old session id so the agent can delete that session

Item format (hand edits are fine, keep one item per checkbox line):
  - [ ] T3 **Title** · project: ~/Git/foo · added: 2026-10-25 · session: local_…
    Note lines are indented under the item.
"""
import argparse, json, os, re, subprocess, sys, tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_os_config import TODO_FILE as TODO
SECTIONS = {"to do": "todo", "active": "active", "done": "done"}
HEADING = {"todo": "## To do", "active": "## Active", "done": "## Done"}
ITEM = re.compile(r"^- \[( |x|X)\] (?:(T\d+) )?(?:\*\*(.+?)\*\*|([^·]+?))\s*((?: · [^·]*)*)$")
TEMPLATE = """---
title: "TODO"
aliases: ["Todo list", "Task backlog"]
topics: [index]
status: memory
state: current
scope: global
updated: {today}
---

# TODO

The optional Agent OS task list. Edit one item per checkbox line, with fields after
` · ` and notes indented underneath.

## To do

## Active

## Done
"""


def today():
    return date.today().isoformat()


def read_lines():
    if TODO is None or not TODO.exists():
        return TEMPLATE.format(today=today()).splitlines()
    return TODO.read_text(encoding="utf-8").splitlines()


def write_lines(lines):
    if TODO is None:
        sys.exit("Set TODO_FILE or AGENT_OS_VAULT before editing tasks.")
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines).rstrip("\n") + "\n")
    text = re.sub(r"(?m)^updated: .*$", f"updated: {today()}", text, count=1)
    TODO.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=TODO.parent, prefix=".TODO.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, TODO)


def parse(lines=None):
    """Items in file order: dicts with id, group, done, title, fields, notes, start, end (line span)."""
    lines = read_lines() if lines is None else lines
    items, group, cur = [], None, None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            group, cur = SECTIONS.get(line[3:].strip().lower()), None
            continue
        m = ITEM.match(line) if group else None
        if m:
            fields = {}
            for part in (m.group(5) or "").split(" · ")[1:]:
                k, _, v = part.partition(":")
                if v:
                    fields[k.strip().lower()] = v.strip()
            cur = {"id": m.group(2) or "", "group": group, "done": m.group(1) != " ",
                   "title": (m.group(3) or m.group(4) or "").strip(), "fields": fields,
                   "notes": [], "start": i, "end": i + 1}
            items.append(cur)
        elif cur and (line.startswith("  ") or line.startswith("\t")) and line.strip():
            cur["notes"].append(re.sub(r"^[-*]\s+", "", line.strip()))
            cur["end"] = i + 1
        elif line.strip():
            cur = None
    return items


def render(item):
    box = "x" if item["group"] == "done" else " "
    head = f"- [{box}] {item['id']} **{item['title']}**".replace("  ", " ")
    order = ["project", "added", "started", "session", "done", "summary"]
    keys = order + [k for k in item["fields"] if k not in order]
    head += "".join(f" · {k}: {item['fields'][k]}" for k in keys if item["fields"].get(k))
    return [head] + [f"  - {n}" for n in item["notes"]]


def section_end(lines, group):
    """Index to insert a new item at the end of a section, creating the section if it's missing."""
    want = HEADING[group]
    try:
        start = next(i for i, l in enumerate(lines) if l.strip().lower() == want.lower())
    except StopIteration:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", want, ""]
        return len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def put(lines, item, group):
    """Move or insert item into group, keeping a blank line after each section's list."""
    if "start" in item:
        del lines[item["start"]:item["end"]]
    item["group"] = group
    at = section_end(lines, group)
    block = render(item)
    if lines[at - 1].startswith("## "):
        block.insert(0, "")
    if at == len(lines) or lines[at].startswith("## "):
        block.append("")
    lines[at:at] = block


def find(items, tid):
    tid = tid.upper() if re.fullmatch(r"[tT]\d+", tid) else tid
    hits = [t for t in items if t["id"] == tid] or [t for t in items if tid.lower() in t["title"].lower()]
    if len(hits) != 1:
        sys.exit(f"{'No task' if not hits else 'More than one task'} matches {tid!r}. Run `todo.py list`.")
    return hits[0]


def public(t):
    return {k: v for k, v in t.items() if k not in ("start", "end")}


def project_dir(t):
    p = t["fields"].get("project", "")
    return Path(os.path.expanduser(p)) if p else None


def git(d, *args):
    r = subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else None


def check(t):
    """Unsaved work in the task's project: uncommitted files, unpushed commits, no remote.
    For a folder of repos (like ~/Git) it checks each repo one level down."""
    d = project_dir(t)
    if not d or not d.is_dir():
        return {"ok": True, "repos": [], "note": "No project folder on this task, so there is no git state to check."}
    repos = [d] if (d / ".git").exists() else sorted(p for p in d.iterdir() if (p / ".git").exists())
    out = []
    for r in repos:
        changed = git(r, "status", "--porcelain")
        remote = git(r, "remote")
        unpushed = git(r, "log", "--branches", "--not", "--remotes", "--oneline") if remote else None
        row = {"repo": str(r), "changedFiles": len(changed.splitlines()) if changed else 0,
               "unpushedCommits": len(unpushed.splitlines()) if unpushed else 0, "hasRemote": bool(remote)}
        if row["changedFiles"] or row["unpushedCommits"] or not row["hasRemote"]:
            out.append(row)
    return {"ok": not out, "repos": out}


def main():
    ap = argparse.ArgumentParser(description="Edit the configured Agent OS task list.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("add"); p.add_argument("title"); p.add_argument("--project", default=""); p.add_argument("--agent", default="")
    p.add_argument("--note", action="append", default=[])
    p = sub.add_parser("show"); p.add_argument("id")
    p = sub.add_parser("start"); p.add_argument("id"); p.add_argument("--session", required=True); p.add_argument("--agent", default="")
    p = sub.add_parser("check"); p.add_argument("id")
    p = sub.add_parser("done"); p.add_argument("id"); p.add_argument("--summary", default="")
    p = sub.add_parser("reopen"); p.add_argument("id")
    p = sub.add_parser("back"); p.add_argument("id")
    p = sub.add_parser("resume"); p.add_argument("id")
    p = sub.add_parser("delete"); p.add_argument("id"); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("path")
    a = ap.parse_args()

    lines = read_lines()
    items = parse(lines)

    if a.cmd == "path":
        print(TODO if TODO is not None else "TODO file not configured")
    elif a.cmd == "list":
        if a.json:
            print(json.dumps([public(t) for t in items], ensure_ascii=False, indent=1))
        else:
            for g in ("active", "todo", "done"):
                rows = [t for t in items if t["group"] == g]
                print(f"{HEADING[g][3:]} ({len(rows)})")
                for t in rows:
                    extra = " · ".join(f"{k}: {v}" for k, v in t["fields"].items() if k in ("project", "session", "added"))
                    print(f"  {t['id'] or '—'}  {t['title']}" + (f"  ({extra})" if extra else ""))
    elif a.cmd == "add":
        title = " ".join(a.title.split()).replace("**", "").replace(" · ", " - ")
        if not title:
            sys.exit("A task needs a title.")
        # Ids are never reused: a deleted task's id could otherwise re-link to an old session that carries it.
        seen = next((int(m.group(1)) for l in lines if (m := re.match(r"next_id: (\d+)$", l))), 1)
        n = max([int(t["id"][1:]) for t in items if t["id"]] + [0]) + 1
        n = max(n, seen)
        fields = {"added": today()}
        if a.agent:
            fields["agent"] = a.agent.strip().lower()
        if a.project:
            fields["project"] = a.project.replace(str(Path.home()), "~", 1)
        item = {"id": f"T{n}", "group": "todo", "title": title, "fields": fields,
                "notes": [" ".join(x.split()) for x in a.note if x.strip()]}
        put(lines, item, "todo")
        lines[:] = [l for l in lines if not re.match(r"next_id: \d+$", l)]
        at = next((i for i, l in enumerate(lines) if l.startswith("updated: ")), 0)
        lines.insert(at + 1, f"next_id: {n + 1}")
        write_lines(lines)
        print(f"Added T{n}: {title}")
    else:
        t = find(items, a.id)
        if a.cmd == "show":
            print(json.dumps(public(t), ensure_ascii=False, indent=1))
        elif a.cmd == "check":
            res = check(t)
            print(json.dumps(res, indent=1))
            sys.exit(0 if res["ok"] else 1)
        elif a.cmd == "start":
            if not re.fullmatch(r"(local_)?[A-Za-z0-9-]{6,64}", a.session):
                sys.exit("That doesn't look like a session id.")
            t["fields"].update(started=today(), session=a.session)
            if a.agent:
                t["fields"]["agent"] = a.agent.strip().lower()
            put(lines, t, "active"); write_lines(lines)
            print(f"{t['id']} is active, linked to {a.session}")
        elif a.cmd == "done":
            t["fields"]["done"] = today()
            if a.summary:
                t["fields"]["summary"] = " ".join(a.summary.split()).replace(" · ", " - ")
            put(lines, t, "done"); write_lines(lines)
            print(f"{t['id']} is done")
        elif a.cmd == "reopen":
            for k in ("started", "session", "done", "summary"):
                t["fields"].pop(k, None)
            put(lines, t, "todo"); write_lines(lines)
            print(f"{t['id']} is back in To do")
        elif a.cmd == "resume":
            if not t["fields"].get("session"):
                sys.exit(f"{t['id']} has no session to go back to. Use `start` with a session id.")
            for k in ("done", "summary"):
                t["fields"].pop(k, None)
            t["fields"]["started"] = today()
            put(lines, t, "active"); write_lines(lines)
            print(f"{t['id']} is back in progress, session {t['fields']['session']}")
        elif a.cmd == "delete":
            if not a.yes:
                sys.exit(f"Deleting {t['id']} removes it from the list. Re-run with --yes to confirm.")
            old = t["fields"].get("session")
            del lines[t["start"]:t["end"]]; write_lines(lines)
            print(f"{t['id']} deleted: {t['title']}")
            if old:
                print(f"old session: {old}")
        elif a.cmd == "back":
            old = t["fields"].get("session")
            for k in ("started", "session", "done", "summary"):
                t["fields"].pop(k, None)
            put(lines, t, "todo"); write_lines(lines)
            print(f"{t['id']} is back in To do")
            if old:
                print(f"old session: {old}")


if __name__ == "__main__":
    main()
