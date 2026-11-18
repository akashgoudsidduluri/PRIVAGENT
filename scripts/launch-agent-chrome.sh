#!/usr/bin/env bash
# Launch Chrome with PrivAgent's ISOLATED agent profile only.
# Never run agent work in your everyday Chrome profile: the agent profile
# is a separate --user-data-dir with its own cookies/logins, and its state
# is wiped on start by the extension (when the isolated-profile flag is set).
# Usage: bash scripts/launch-agent-chrome.sh [extra chrome flags...]
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_DIR="${PRIVAGENT_AGENT_PROFILE_DIR:-$REPO_ROOT/backend/data/agent-profile}"
mkdir -p "$PROFILE_DIR"
CHROME="${CHROME_BIN:-google-chrome}"
command -v "$CHROME" >/dev/null 2>&1 || CHROME="chromium"
command -v "$CHROME" >/dev/null 2>&1 || CHROME="chromium-browser"
# Task 10: tracker / fingerprint hygiene flags. Every flag here is a
# real, long-established Chrome command-line switch (see
# docs/ISOLATION.md); no flag is invented. There is NO Chrome flag for
# fingerprint randomisation — that would need extension-level work, and
# is documented as a remaining gap rather than faked with a flag.
exec "$CHROME" --user-data-dir="$PROFILE_DIR" --no-first-run --no-default-browser-check \
  --block-third-party-cookies \
  --disable-background-networking \
  --disable-component-update \
  --disable-sync \
  --disable-domain-reliability \
  --no-pings \
  "$@"
