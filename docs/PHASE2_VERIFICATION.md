# Phase 2: browser actions and verification

## Implemented

- Dynamic DOM quiescence and bounded page fetch/XHR idle probing, with unavailable evidence remaining unknown.
- Custom portal dropdown selection with visible option uniqueness and readback, text/native/custom calendar date handling, focused modal scroll discrimination, and 5 MiB allowlisted file inputs.
- Files enter through a user-selected workspace picker, stay in a 10-minute in-memory token store, and are inserted into extension actions only after the sensitive approval gate. The planner, task history, and SQLite never receive file bytes.
- Six structured action layers are recorded in history. External writes cannot finish as verified without a fresh service affirmation and successful transport evidence. Unknown signals remain unknown; clicks and hopeful evidence strings never imply success.

## Verification

`cd backend && python -m pytest tests -q`

`cd extension && npm test && npx tsc --noEmit && npm run build`

`npm test && npm run build`

Tests cover portal dropdowns, date formats and month navigation, modal scrolling, file selection and limits, attachment token isolation, verification refusal after dispatch-only or spoofed affirmation, and the positive six-layer receipt. These are deterministic local/browser DOM harnesses. A live third-party checkout or login cannot be certified by this repository's tests; those flows deliberately remain unverified if the service does not expose the required confirmation.
