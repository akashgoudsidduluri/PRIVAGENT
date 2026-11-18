"""Print the local operator token (Task 2).

Run from the backend directory with the backend venv::

    venv\\Scripts\\python.exe scripts/print_local_token.py   # Windows
    ./venv/bin/python scripts/print_local_token.py            # Linux/macOS

The token is printed to stdout so the operator can paste it into the web
UI prompt (once, on first use) or into the extension's chrome.storage.
Treat the output like a password: do not paste it into chats, tickets,
or screenshots, and do not leave it in a terminal scrollback on a
shared machine. The token itself is stored encrypted (see app/secrets.py);
this script is the only intended way to read it back in plaintext.

The script loads ``backend/.env`` first (same file, same override
semantics as ``app.config``), so it prints exactly the token the running
backend will accept: the ``PRIVAGENT_LOCAL_TOKEN`` from ``.env`` when
set, else the token from the encrypted store (whose master key may
itself live only in ``.env``). Without that load it would silently
print the store token while the backend honoured the ``.env`` token.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import get_local_token  # noqa: E402


def main() -> int:
    load_dotenv(
        dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True
    )
    print(get_local_token())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
