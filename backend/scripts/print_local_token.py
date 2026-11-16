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
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import get_local_token  # noqa: E402


def main() -> int:
    print(get_local_token())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
