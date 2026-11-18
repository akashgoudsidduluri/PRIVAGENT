# Generalisation (Task 8)

The agent used to work by matching a hard-coded site list: the planner
carried per-site playbooks (Flipkart, Amazon, YouTube, LeetCode, WhatsApp,
Google Flights), the resolver built one search URL per retailer, intent
routing enumerated site names, and the extension collected elements with
per-site class names (`._2KpZ6l`, `.ytp-*`, `data-testid="cell-frame-…"`)
and per-site ids (`twotabsearchtextbox`).

That meant every new site needed a code change, and the model trusted the
playbook over the live page. Task 8 replaces it with one general path:

**read the accessibility tree, find the element by role + accessible
name, act by element ref (Task 7), verify the result from the new
observation, never assume a site's layout.**

## Inventory: before / after

Counts are lines in `git diff` mentioning a site-specific token
(brand name, site class, site data-testid, site input id, per-site URL
builder): **209 removed, 0 added** in tracked files. Brand names now
live only in two data files (`known_domains.json`, `site_hints.json`).

| Area | Before (Task 7 tree) | After (Task 8) |
|---|---|---|
| `planner.py` SYSTEM_FRAME | 7 site playbook sections: §2 multi-stage (WhatsApp), §3 chat/messaging (WhatsApp Web mechanics, `data-tab="3"`/`"10"`), §6 YouTube ads ("Skip Ad"), §7 e-commerce (Flipkart/Amazon placeholders, `/p/` `/dp/` patterns), §9 completion (Flipkart/Amazon/YouTube homepages), §10 LeetCode auth/report, §12 Google Flights/Hotels (hard-coded dates 27/29 Nov 2026, 2 guests) | 1 general section, "HOW TO ACT ON ANY SITE", before CORE OPERATING PRINCIPLES. Zero site-specific strings in the file. |
| `planner.py` code | `_DIRECT_SITE_TARGETS` (10 entries, dead code), `_MESSAGING_HOSTS` (4 hosts), `_SEARCH_HOSTS` (5 hosts), `"/s?k="` evidence exclusion | All deleted. `_missing_chat_composer` and `_uncompleted_search_recovery` are generic (any host; search state via `/search`, `results`, `/s`, `watch` or `q=`/`query=`/`search=`/`k=`/`keyword=`). |
| `website_resolver.py` | `KNOWN_DOMAINS_REGISTRY` (19 entries) as a code literal; shopping hubs with per-retailer search URL templates (`amazon.in/s?k=`, `flipkart.com/search?q=`, `croma.com/searchB?q=`, `reliancedigital.in/search?q=`); travel hub `google.com/travel/flights?q=`; shopping keywords included brand word "samsung" | Registry loaded from `backend/app/known_domains.json` at import (same in-memory shape, all callers unchanged). Shopping + travel answered by generic search hubs (Google Search, DuckDuckGo, Wikipedia). Brand word dropped from keywords. |
| `intent_router.py` | 6 site-name enumerations: `USE_TAB_PATTERNS`, `DIRECT_SITE_TRIGGER` (~40 names), `SEARCH_WEB_TRIGGER`, `PRODUCT_SEARCH_TRIGGER`, `COMPARISON_BROWSER_TRIGGER`, `BROWSER_TRIGGERS` ×2 | One data-driven `_site_token_pattern()`: registry names + keys (longest-first) + service words + a generic domain token. Any site the user names routes to the browser, whether or not the codebase has heard of it. |
| `extractors/research.py` | `KNOWN_RETAILERS` literal (8 entries) — a second copy of the registry; a `samsung`-only special case; retailer-name regexes in `extract_target_product_query` and title cleaning; `amazon`/`flipkart` seller branches | Retailers derived from the registry (`_retailers_from_registry()`); generic `_generic_required_sites` parses comparison lists and resolves domain-like tokens (`ExampleStore.com` → required site, bare unknown names → skipped); query/title stripping uses registry names + capitalised names + domains; seller is the registry display name. |
| `extension/lib/content.ts` | ~30 site-specific selectors/blocks: `.ytp-*` ×6, `._2KpZ6l`/`._2doB4z`, `ytd-thumbnail`, `#video-title`, `#thumbnail`, `[data-testid="compose-btn-send"]`, `[data-icon="send"/"ptt"/"cancel"]`, `#onetrust-accept-btn-handler`, Flipkart classes (`._1AtVbE`, `._75nlfW`, `._30jeq3`, `.Nx9bqj`, …), Amazon (`.s-result-item`, `[data-asin]`, `.a-price`, `.priceToPay`, `[data-feature-name="corePrice"]`, `#centerCol`, `#dp`, `#productTitle`, `#nav-main`), Google hashed classes, WhatsApp `data-testid`/`data-tab` blocks | Zero site-specific selectors. Ad-skip by aria-label ("Skip Ad"), close by aria-label ("Close"/"Dismiss"), search by `role="searchbox"` / `type="search"` / accessible name, chat rows by `role="listitem"`/`"row"` + title text, product price by first currency-looking text in the card (`priceTextIn`). |
| `extension/src/targeting.ts` | `field-keywords\|twotabsearchtextbox` id regex; "Flipkart exposes its search input…" comment | Generic search-field ids only (`q`, `query`, `search`, …); comment reworded to "some sites". |
| `extension/src/actionExecutor.ts` | `"Connecting to WhatsApp"` / `"Loading your chats"`, `ytd-thumbnail` in the interactive count, `._2KpZ6l` modal + close selectors, "SPAs (Flipkart) may route…" comment | Generic connecting text (`/connecting\|loading your chats/i`), generic modal/close selectors, generic SPA comment. |
| `extension/src/companion/companionDrawer.ts` | Banter "across Amazon, Flipkart, and Croma…" | "across retailers…" |

## Site hints: the thin, optional, data-driven layer

Playbooks are not deleted into nothing: the genuinely useful knowledge
("a messaging app usually has a Groups filter and a chat search box")
moves into `backend/app/site_hints.json` — human-readable prose only,
**never CSS selectors, class names, data-testids, or data-tab values**.
The loader (`backend/app/site_hints.py`) rejects selector-like text at
load time, so a playbook cannot sneak back in as data.

Format (one entry per domain):

```json
{
  "web.whatsapp.com": {
    "name": "WhatsApp Web",
    "hints": [
      "Chat list: a filter button labelled 'Groups' narrows the list to group chats.",
      "Search: a search box labelled 'Search or start new chat' filters the chat list; typing there does not send a message.",
      "Composer: the message box is a textbox labelled 'Type a message' at the bottom of the open chat."
    ]
  }
}
```

The planner inserts a `<site-hints trusted="true" advisory="true">`
block for the current host (after `<saved-credentials>`). The block says
the live accessibility tree is the truth: **a plan that quotes an
element the tree does not have is invalid whether a hint named it or
not** — `_validate_plan` rejects unknown refs, and the loop re-observes
rather than acting on a hint that contradicts the page.

Hints are off entirely with `PRIVAGENT_SITE_HINTS=false`
(`backend/.env.example`). The agent then runs in pure-general mode.
A plan that needs a hint to work is a bug: the deterministic plan for a
synthetic generic page is identical with hints on and off
(`tests/test_generalisation.py::test_deterministic_plan_is_hint_independent`).

### How to add a hint for a new site (no code change)

1. Add one entry to `backend/app/site_hints.json` with the domain as key,
   a human-readable `name`, and 1–4 short `hints` in plain prose.
2. Never put a selector, class, id, `data-*` attribute, or URL path
   pattern in a hint — the loader raises `ValueError` at import if you do.
3. Run `pytest tests/test_generalisation.py` — `test_hints_file_is_prose_only_and_small`
   enforces the prose-only rule and the ≤7-domain cap (hints are a
   handful, not a site list).
4. Restart the backend. No code change, no new test file needed — but add
   one if the hint encodes behaviour a test should pin.

## What was deleted

- All 7 SYSTEM_FRAME playbook sections (rewritten as one general section).
- `_DIRECT_SITE_TARGETS`, `_MESSAGING_HOSTS`, `_SEARCH_HOSTS` (planner).
- Per-retailer search URL templates in the resolver (shopping + travel).
- The `KNOWN_RETAILERS` code literal and the `samsung` special case
  (research extractor).
- All site-name enumerations in intent routing.
- All site-specific selectors/blocks in the extension's generic element
  collection path, element resolution, click/type handling, page-state
  probe, and readable-text extraction.

## What remains site-specific — and why

| Remains | Why it stays |
|---|---|
| `backend/app/known_domains.json` (19 entries) | **Data, not code.** The registry for verification + direct navigation. Adding a site is a data edit. |
| `backend/app/site_hints.json` (7 domains) | **Advisory prose only**, loader-enforced. The live AX tree wins. |
| Lookalike brand list in `website_resolver.py` (`LOOKALIKE_PATTERNS`, `is_lookalike_or_suspicious`) | **Security, not behaviour** — phishing defence (e.g. `amaz0n.com`). Kept deliberately; covered by `test_lookalike_brand_list_survives_as_security`. |
| `_LOGIN_HOSTS` (`accounts.google.com`, `login.microsoftonline.com`, `login.live.com`) | Identity-provider hosts for the login-surface check, not site playbooks. |
| `mail.google.com` in `planner.py` | Mail service endpoint / connector route — Task 14, not a playbook. |
| `EMAIL_TRIGGER` / `CALENDAR_DIRECTIVE_PATTERN` / `target_tab_hint="mail.google.com"` in `intent_router.py` | Connector routes — Task 14. |
| Site-styled fixtures in extension tests (`contentDiscovery.test.ts`, `targeting.test.ts`) | Detection is evidence-based, not id-based; the fixtures prove legacy pages still work. The new `genericDiscovery.test.ts` covers the general path. |

## Tests changed

- **New** `backend/tests/test_generalisation.py` (19 tests): hints
  prose-only + advisory + disableable; prompt carries hints for known
  hosts only; deterministic plan identical with hints on/off; grep-style
  "no site-specific strings" over `planner.py`, `intent_router.py`,
  `extractors/research.py`, and the extension sources; resolver builds
  no per-site search URLs; lookalike list survives; plan quoting a
  missing ref is rejected, a live ref passes; generic search box /
  banner close / chat row by role+name; URL normalisation; generic
  comparison-site resolution; domain-agnostic intent routing.
- **New** `extension/tests/genericDiscovery.test.ts` (8 tests): generic
  search box by role/name, unlabelled search by type, ad-skip by
  aria-label, dialog close by aria-label, cookie-banner dismiss by
  name, chat rows by role + title, Groups filter by text, product
  price by currency text.
- **Updated** `backend/tests/test_website_discovery.py::test_scenario_b_shopping_discovery_no_website_specified`:
  asserts the GENERAL behaviour (selected sources are independent
  generic search hubs — Google/DuckDuckGo/Wikipedia — never
  per-retailer search URLs) instead of asserting amazon/flipkart in
  the selected domains. It still fails offline (sandbox has no DNS, so
  `is_safe_public_url` rejects every hub and zero candidates are
  returned) — the same pre-existing environment failure as before
  Task 8, not a new failure.

## Limits

- No live Chrome run on real sites: generalisation is unit-tested on
  synthetic pages only. The real test is driving the agent on a site it
  has never seen and watching it ground by role + name.
- Hints are advisory: a hint can be wrong, stale, or missing, and the
  agent must still work — that is the point of the design, and the
  reason hints are capped at a handful of domains.
- The AX tree is DOM-derived (Task 7), not CDP `getFullAXTree`;
  cross-origin iframes are still opaque, so a control inside one
  cannot be grounded by ref.
