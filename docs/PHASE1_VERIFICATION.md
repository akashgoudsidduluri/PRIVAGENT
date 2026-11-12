# Phase 1 — semantic perception and visual grounding

The extension builds a semantic record for each interactive control: accessible name, computed role, control class, viewport bounds, visibility, opacity, pointer-events, occlusion, and a small nearby text window. It traverses open shadow roots and same-origin frames; browser security makes closed roots and cross-origin frames opaque. The observer and content script carry these records to the backend. Privacy redacts them before the planner sees them. The bounded action loop matches a unique semantic target and supplies viewport coordinates as fallback. Detached React controls can be re-resolved by their accessible name, with position used only to disambiguate near-identical results.

Checks: backend `python -m pytest tests -q` (372 passing); extension `npm test && npx tsc --noEmit && npm run build` (117 passing); root `npm test && npm run build` (passing); `git diff --check` (clean).

This verifies the contract and mocked DOM/browser flows. Live Chrome sites and the rendered UI have not been visually verified in this remote workspace. The landing page overhaul belongs to Phase 7, and Phase 2 action controls have not been implemented by this phase.
