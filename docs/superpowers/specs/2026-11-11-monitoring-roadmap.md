# PrivAgent Master Roadmap v3 — Memory + Monitoring Integration

Date: 2026-11-11 · Status: PROPOSED (awaiting approval) · Companion spec: `2026-11-11-memory-architecture.md`

Supersedes nothing: P0/P1 remain first. This document integrates the memory architecture (refined) and the monitoring roadmap (preserved) into one staged progression: **reliable basic agent → improved agent → advanced generalized agent.**

## 0. Sequencing after approval (single source of truth)

| Order | Stage | Scope | Depends on |
|---|---|---|---|
| 1 | **P0 remainder** | 3 UI defects (taskconsole state suffix, hardcoded telemetry, price badge) — small, in working tree already partially | — |
| 2 | **P1 demos** | Live-run the 3 demo workflows; fix findings; honest README | P0 green |
| 3 | **M0 memory seams** | recall/commit extraction + migration runner + redact-on-write. Zero behavior change | P0 |
| 4 | **P2a monitor readiness** | MONITOR intent honesty guard (classify→honest "not yet supported" reply); ObservationSpec contract; adapter contract doc | P0/P1 |
| 5 | **M1 memory reliability** | episodes + semantic_facts + MemoryStore + scopes + evidence taxonomy + acceptance tests 1–7 | M0 |
| 6 | **P2b connectors (Gmail first)** | connector base + token store + gmail adapter | P1 |
| 7 | **Stage B basic monitoring** | missions table, scheduler, browser-observe source, change detection, notifications, pause/stop | P1 + M1 (baselines as source-scoped facts) |
| 8 | **P2c MCP foundation** | client transport, discovery, permission mapping | P1 |
| 9 | **M2 retrieval intelligence** | FTS5 + ranking + eval harness | M1 |
| 10 | **Stage C improved monitoring** | Gmail connector source, normalized observations, dedup, LLM relevance on diff, recovery/auto-pause | Stage B + M1 + P2b |
| 11 | **M3 summarization + memory UI** | chat rollups, /api/memory, minimal UI | M1 |
| 12 | **Stage D advanced monitoring** | webhooks, adaptive cadence, cross-source, NL conditions, permissioned auto-actions | Stage C |
| 13 | **Stage E generalized ecosystem** | MCP adapter, dynamic capability registry, desktop, presence | P2c + Stage C/D |

Rationale for interleaving: memory M1 must land *before* Stage B monitoring because monitoring baselines, dedup, and audit re-use `semantic_facts`/`episodes` and the shared ranker — building monitoring first would orphan its persistence. Everything else keeps the original P0→P6 spirit. The immediate 2-day demo/reliability plan is untouched.

### Multi-tab / complex browser capability work requested (fits as P1.5, after P1)
Ordered build requested by the user, between demos and monitoring:
1. **Multi-tab mastery:** verify/extend select_tab/close_tab/new-tab detection + child-tab registry against real multi-tab sessions (live test).
2. **Complex search flows:** pagination, filters, form-driven search across real sites (live test scripts).
3. **Research-to-document task:** "research topic X across multiple pages and produce a structured .docx document" — planner strategy + browser research + a document generator capability (local python-docx for .docx: headings/sections), image handling honest-first: embed only images with captured source URLs (screenshots of pages, downloaded images, hotlink-safe — with offline fallback to image placeholders + links if downloads fail/copyright-unclear). NEW capability `document_generation` + capability declaration; all through the single loop; doc built from `collected_research` + sources, verified against fresh page evidence before delivery. Acceptance: topic task → multi-page evidence (≥4 sources, each observed) → docx with H1/H2 structure, per-source citations, images where legitimately obtainable → mission summary lists every source.
4. **Email triage (demo 1 evolution):** still browser-based first: scan inbox → classify → propose archive/delete batches → user approves batches via gate → actions execute with verification. No API until connectors (P2b) land, then this gains API speed.

## 1. Monitoring roadmap (PRESERVED — as agreed in v2)

Summary of v2 commitments (unchanged): mission = stored intent, scheduler turns missions into ordinary tasks through the ONLY agent loop; stages B/C/D/E with the same acceptance criteria; the do-not list (no per-app monitors, no second planner, no auto-actions without approval in B/C, no claim that monitoring exists until Stage B criteria pass).

**Memory-driven refinements now incorporated (formerly conflicts, now resolved):**
- Baselines are `semantic_facts(scope=source, source=domain)` — the monitor's dedup/change-detection reuses §1 conflict resolution; "alert once" = fact upsert who changed + notification-id dedupe via episodes.
- Monitoring relevance evaluation (Stage C) reuses the M2 ranker + the same redaction-before-LLM rule; condition matching gets memory context as untrusted labeled block.
- Mission definitions may carry NL conditions compiled once (Stage D) → stored as structured conditions, NOT model-inferred facts.
- Monitoring audit rows are episodes with `outcome="observation"` — one audit story for the product.

## 2. Capability matrix update

Adds (from memory spec §10): M-phase acceptance gates; and in the matrix rows: "source memory (per-site knowledge)" PARTIAL→M1/M2; "procedural strategies with staleness" M2; "monitoring" Stage B–E as in v2. Demo and monitoring rows unchanged.

## 3. Conflicts & resolutions register

Memory vs monitoring: baselines/audit overlap → resolved by shared MemoryStore ownership (memory spec is the store of record; monitoring consumes it). No other conflicts; P0/P1 untouched in both documents.

## 4. Files changed by this planning cycle (documents only)

- ADDED `docs/superpowers/specs/2026-11-11-monitoring-roadmap.md` (v3, this file)
- ADDED `docs/superpowers/specs/2026-11-11-memory-architecture.md` (memory design, refined per user's 6 refinements)
- Existing app/test code: UNCHANGED by this cycle (earlier P0 fixes remain uncommitted in the working tree, preserved).
