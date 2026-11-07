# Agent OS

Agent OS is a macOS dashboard for local Codex and Claude Code activity. The pixel office shows recent sessions, requests for input, test runs, and machine activity. It reads existing logs and process metadata.

## Run locally

Use macOS and Python 3.11 or newer. In the checkout, run:

```sh
python3 -I serve.py
```

Open [Agent OS](http://127.0.0.1:8765/) in your browser. Give the first collection a moment to finish. Press Ctrl-C in the terminal to stop the server. If port 8765 is in use, run `AGENT_OS_PORT=8766 python3 -I serve.py` and open the corresponding local address.

Codex and Claude Code populate the dashboard as they write local session logs. Agent OS defaults to `~/.codex` and `~/.claude` and honors `CODEX_HOME` and `CLAUDE_CONFIG_DIR` when set. To override those paths for Agent OS alone, set `AGENT_OS_CODEX_DIR` or `AGENT_OS_CLAUDE_DIR`. Activity without accessible local logs may be unavailable. The browser assets are bundled and the Python code uses the standard library.

## Optional integrations

- Set `AGENT_OS_VAULT` to your Obsidian vault to see note modification history. The task board looks for `Brain/TODO.md` inside that vault. `TODO_FILE` points to a different task file or enables the task board without a vault.
- The task board reads the configured file by default. Set `AGENT_OS_SYNC_TODOS=1` if you want Agent OS to mark matching tasks active when it observes a session started from the board. You can also use `python3 -I todo.py --help` for explicit task edits.
- Set `AGENT_OS_LOCAL_STATUS=1` to enable read-only status probes for supported local AI tools. Process observation works without this option.
- If you use a Claude Code status line, `claude_status.py` can capture the context and quota fields supplied to that status line. Existing local Codex quota monitor metadata is read when available. These are optional sources for the Session Observatory.

## Local data and Git

The server binds to `127.0.0.1`. It creates `local.html`, `data/`, and `.cache/` inside the checkout. Generated data can include commands, task text, session summaries, paths, and usage history. The Git allowlist excludes those paths, backups, local configuration, and build notes. Keep them in your local installation to retain its history. A fresh clone builds its own data from the machine where it runs.

## Assets and licensing

The office sprites come from [Pixel Office Asset Pack by 2dPig](https://2dpig.itch.io/pixel-office) under CC0. The bundled [D3 7.9.0](https://github.com/d3/d3/tree/v7.9.0) distribution includes its [ISC license](vendor/d3.LICENSE). A license for Agent OS itself has yet to be selected.
