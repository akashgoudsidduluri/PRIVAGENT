# Phase 3: in-page companion

- A metallic floating companion is mounted once inside a closed Shadow DOM. Its eleven states have distinct eye/body animations, with reduced-motion support.
- The drawer shows the active directive, a bounded live timeline, and approval, abort, pause, resume, cancel, and dashboard controls. Every in-page control rejects synthetic clicks.
- The extension worker owns task state and subscribes to backend progress. It stores a session snapshot, re-injects on authorized tab navigation, and hydrates via `COMPANION_SYNC`. Approval calls require the active task, pending gate, and authorized tab.
- Pause and resume endpoints stop the loop at the next action boundary and preserve its time budget. Cancellation wakes a paused loop.

Verification: backend suite, extension tests/type check/build, and root tests/build. `companionWorker.test.ts` exercises session restoration, authorized child tab re-injection, and an unauthorized approval attempt. `test_companion_controls.py` checks pause/resume/cancel and a paused loop. Visual animation on a real third-party site remains outside the deterministic test harness.
