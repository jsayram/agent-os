"""Local paths for a standalone Agent OS checkout."""

import os
from pathlib import Path


def optional_path(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser().resolve()
    return None


CLAUDE_DIR = optional_path('AGENT_OS_CLAUDE_DIR', 'CLAUDE_CONFIG_DIR') or Path.home() / '.claude'
CODEX_DIR = optional_path('AGENT_OS_CODEX_DIR', 'CODEX_HOME') or Path.home() / '.codex'
VAULT_DIR = optional_path('AGENT_OS_VAULT')
TODO_FILE = optional_path('TODO_FILE')
if TODO_FILE is None and VAULT_DIR is not None:
    TODO_FILE = VAULT_DIR / 'Brain' / 'TODO.md'

SYNC_TODOS = os.environ.get('AGENT_OS_SYNC_TODOS') == '1'
LOCAL_STATUS = os.environ.get('AGENT_OS_LOCAL_STATUS') == '1'
