"""Per-task observation cache (Task 11).

The loop re-observes the page far more often than the page changes:
after an invalid plan, after a redundant-action penalty, after a
loop warning — none of those execute an action, so the page the agent
sees cannot have changed because of the agent. Re-collecting the
whole accessibility tree + page text + screenshot for those
re-plans is a wasted round trip.

The cache holds the last observation (keyed by tab + URL, with a DOM
fingerprint) and serves it only while the loop can *prove* no
mutation happened since it was stored. It is deliberately
conservative:

* The cache is per-task and in-memory only. It is never persisted to
  a checkpoint, a trajectory, or disk — zero retention (Task 10) is
  unaffected.
* A hit requires the same tab, the same URL, and the ``valid`` flag
  still set. The flag is cleared on anything that can change the
  page: a failed action, a tab change, a takeover (the operator acted
  in the browser), a pause/resume, or a stale element ref.
* The fingerprint (URL + title + element count + first element ids)
  is the same digest the loop already uses for loop detection. The
  backend cannot know the DOM changed without asking the extension,
  so the fingerprint is recorded with the entry and verified when a
  fresh observation eventually arrives: if the URL/tab match but the
  fingerprint differs, the page changed underneath us and the miss
  was necessary (counted as ``stale_detected`` by the caller).
* A cache HIT does not verify the fingerprint — there is nothing to
  verify it against without a fresh observation, which is the round
  trip the cache exists to avoid. So a page that changes on its own
  (a timer, a live feed, an ad) between the store and the hit is
  served stale. That is a deliberate, documented limit, not an
  oversight: the cache is only consulted on iterations where no
  action executed, the entry is invalidated on every agent- or
  operator-caused mutation the loop knows about, and every step the
  loop executes is still resolved live by the extension (name/role
  agreement on refs), so a stale tree can mislead the planner but
  cannot silently retarget an action. The fingerprint IS consulted
  by the batch gate (``agentLoop``), which ends a batch when the
  post-action observation's fingerprint differs from the plan's.
* A cache hit is still re-run through ``classify_and_redact`` on
  every use, exactly like the loop's existing post-action observation
  reuse — the cache never bypasses redaction.
* ``lookup`` returns a copy of the stored entry: the loop mutates
  the observation it is handed (screenshot stripping), and returning
  the stored object would let that corrupt the entry.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional


def _obs_field(obs: Any, name: str) -> Any:
    if obs is None:
        return None
    if isinstance(obs, dict):
        return obs.get(name)
    return getattr(obs, name, None)


def _obs_url(obs: Any) -> str:
    return str(_obs_field(obs, "url") or "")


def _obs_tab_id(obs: Any) -> Optional[int]:
    tab = _obs_field(obs, "tabId")
    if tab is None:
        tab = _obs_field(obs, "tab_id")
    try:
        return int(tab) if tab is not None else None
    except (TypeError, ValueError):
        return None


# Public aliases for the loop (the underscore names stay for internal use).
observation_url = _obs_url
observation_tab_id = _obs_tab_id


def observation_fingerprint(obs: Any) -> str:
    """Short DOM fingerprint: URL + title + element count + first ids.

    Same ingredients as the loop's state digest (``agentLoop.
    _state_digest``), hashed so the cache entry carries a fixed-size
    fingerprint instead of the raw digest string.
    """
    url = _obs_url(obs)
    title = str(_obs_field(obs, "title") or "")
    interactive = _obs_field(obs, "interactive") or []
    try:
        count = len(interactive)
    except TypeError:
        count = 0
    ids: list[str] = []
    try:
        for el in list(interactive)[:12]:
            if isinstance(el, dict):
                ids.append(str(el.get("targetId") or el.get("target_id") or ""))
            else:
                ids.append(str(getattr(el, "target_id", "") or ""))
    except Exception:
        pass
    raw = f"{url}|{title}|{count}|{','.join(ids)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _copy_obs(obs: Any) -> Any:
    """Shallow copy so later in-loop mutation of an observation (e.g.
    screenshot stripping) cannot corrupt the cached copy."""
    if isinstance(obs, dict):
        return dict(obs)
    copy = getattr(obs, "model_copy", None)
    if callable(copy):
        try:
            return copy()
        except Exception:
            return obs
    return obs


class ObservationCache:
    """One observation, served only while provably unmutated."""

    def __init__(self) -> None:
        self._entry: Optional[dict[str, Any]] = None
        self.hits = 0
        self.misses = 0

    def store(self, obs: Any) -> None:
        """Store ``obs`` as the current page state and mark it valid."""
        if obs is None:
            return
        self._entry = {
            "obs": _copy_obs(obs),
            "url": _obs_url(obs),
            "tab_id": _obs_tab_id(obs),
            "fingerprint": observation_fingerprint(obs),
            "valid": True,
        }

    def lookup(self, url: str, tab_id: Optional[int]) -> Optional[Any]:
        """Return the cached observation for this tab+URL, or None.

        A miss is counted whenever the entry is absent, invalid, or
        for a different tab/URL — the caller then observes live.

        REVIEW (Task 11 audit): the returned observation is a COPY of
        the stored entry, not the entry itself. The loop mutates the
        observation it is handed — screenshot stripping (zero
        retention, post-takeover) sets ``screenshot = None`` on it —
        and returning the stored object let that mutation corrupt the
        entry, so a second hit served an observation with the
        screenshot already stripped. The copy is shallow (the same
        guarantee ``store`` gives): top-level fields are independent,
        nested element lists are shared read-only — the loop never
        mutates those in place.
        """
        entry = self._entry
        if entry is None or not entry.get("valid"):
            self.misses += 1
            return None
        if entry.get("url") != (url or "") or entry.get("tab_id") != tab_id:
            self.misses += 1
            return None
        self.hits += 1
        return _copy_obs(entry["obs"])

    def invalidate(self) -> None:
        """Mark the entry stale: the page may have changed. The entry
        is kept (for fingerprint comparison) but never served again."""
        if self._entry is not None:
            self._entry["valid"] = False

    def check_fresh(self, obs: Any) -> bool:
        """Compare a freshly observed page against the cached entry.

        Returns True when the fresh observation matches the cached
        fingerprint (same tab+URL and same DOM fingerprint). Returns
        False — and leaves the entry invalid — when the page changed
        while the cache was invalid, i.e. the miss was necessary.
        Callers use the False case to count ``stale_detected``.
        """
        entry = self._entry
        if entry is None or obs is None:
            return False
        if entry.get("url") != _obs_url(obs) or entry.get("tab_id") != _obs_tab_id(obs):
            return False
        return entry.get("fingerprint") == observation_fingerprint(obs)

    def has_entry_for(self, url: str, tab_id: Optional[int]) -> bool:
        """True when an entry exists for this tab+URL (valid or not).

        Lets the caller tell "the page changed while the cache was
        invalid" (entry exists, fingerprint differs) apart from "no
        entry ever stored" (nothing to compare against).
        """
        entry = self._entry
        if entry is None:
            return False
        return entry.get("url") == (url or "") and entry.get("tab_id") == tab_id

    @property
    def has_valid_entry(self) -> bool:
        return bool(self._entry and self._entry.get("valid"))
