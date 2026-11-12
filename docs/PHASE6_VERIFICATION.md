# Phase 6 verification — Memory and privacy

Working observations have a bounded in-process buffer. Existing chat messages and checkpoints provide episodic history; monitoring missions provide mission memory; research packages and artifacts provide artifact memory. Semantic facts now have a dedicated SQLite table, three allowed categories, explicit creation/deletion, and 1–365 day retention. The agent loads allowed facts into its initial context.

Known credentials and PII are rejected from semantic facts and preferences; messages, titles, checkpoints, research packages and artifact metadata pass through storage redaction. Page, PDF, search and MCP text are cleansed and treated as untrusted; hidden, zero-size, zero-opacity and strongly negative z-index nodes are excluded at the DOM boundary. The backend also withholds opted-in screenshots if visible page text or controls contain detected sensitive values.

Verification: `python -m pytest tests -q`, `npm test && npx tsc --noEmit && npm run build` in `extension`, `npm test && npm run build` at root.

Limits: Pattern matching cannot prove that arbitrary pixels, uncommon PII formats or novel prompt-injection phrasing are safe. Screenshots remain opt-in, and the current policy withholds them when detected sensitive content is present; a full OCR or image redaction guarantee is not implemented. No live external privacy audit was performed.
