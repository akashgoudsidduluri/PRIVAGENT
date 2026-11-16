/**
 * Task timeline rows (Task 12) — DOM-free, unit-testable.
 *
 * Builds one row per executed step from the Task 11 loop timings
 * (GET /api/task/{id} `timings.steps`: action type, durations,
 * batched / cache flags) joined to the checkpoint history steps
 * (same endpoint `steps`: stepNum, status, result_ok). Timings carry
 * action types and durations only — never names, values, or page
 * text — so the rows are safe to render as text.
 */

export interface TimingStep {
  step: number;
  action: string;
  batched: boolean;
  cache: string;
  checker_skipped: boolean;
  total_ms: number;
  overhead_ms: number;
}

export interface HistoryStep {
  stepNum: number;
  message?: string;
  status?: string;
  result_ok?: boolean;
}

export interface TimelineRow {
  step: number;
  action: string;
  status: string;
  durationMs: number;
  overheadMs: number;
  batched: boolean;
  cache: string;
  checkerSkipped: boolean;
}

/** "340ms" / "1.2s" / "12.5s". */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** Join timings steps to history steps by step number. */
export function buildTimelineRows(
  timingsSteps: TimingStep[] | null | undefined,
  historySteps: HistoryStep[] | null | undefined,
): TimelineRow[] {
  if (!Array.isArray(timingsSteps)) return [];
  const byNum = new Map<number, HistoryStep>();
  for (const h of Array.isArray(historySteps) ? historySteps : []) {
    if (typeof h?.stepNum === "number") byNum.set(h.stepNum, h);
  }
  return timingsSteps.map(t => {
    const h = byNum.get(t.step);
    const status = h?.status
      || (h?.result_ok === false ? "failed" : h?.result_ok === true ? "verified" : "executed");
    return {
      step: t.step,
      action: t.action || "step",
      status,
      durationMs: t.total_ms ?? 0,
      overheadMs: t.overhead_ms ?? 0,
      batched: Boolean(t.batched),
      cache: t.cache || "n/a",
      checkerSkipped: Boolean(t.checker_skipped),
    };
  });
}

/** One-line summary, e.g. "5 steps · 2 batched · 1 cache hit · 823ms". */
export function timelineSummary(
  rows: TimelineRow[],
  overheadMs: number | null | undefined,
): string {
  if (rows.length === 0) return "No steps yet";
  const batched = rows.filter(r => r.batched).length;
  const hits = rows.filter(r => r.cache === "hit").length;
  const parts = [`${rows.length} step${rows.length === 1 ? "" : "s"}`];
  if (batched > 0) parts.push(`${batched} batched`);
  if (hits > 0) parts.push(`${hits} cache hit${hits === 1 ? "" : "s"}`);
  if (typeof overheadMs === "number") parts.push(`${formatDuration(overheadMs)} overhead`);
  return parts.join(" · ");
}
