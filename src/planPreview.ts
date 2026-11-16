/**
 * Plan preview formatting (Task 12) — DOM-free, unit-testable.
 *
 * The backend sends the pending plan as structured preview dicts
 * (see backend/app/plan_preview.py): one entry per validated step
 * with a type, a risk class, a target (name / role / elementRef) and
 * a one-line plain-language detail. Nothing here touches the DOM and
 * nothing here builds HTML: the workspace renders each line with
 * textContent, so a target name that echoes page text stays inert.
 */

export interface PlanPreviewTarget {
  name: string | null;
  role: string | null;
  elementRef: string | null;
}

export interface PlanPreviewStep {
  index: number;
  type: string;
  risk: string;
  target: PlanPreviewTarget;
  detail: string;
}

const TYPE_LABELS: Record<string, string> = {
  navigate: "Navigate",
  new_tab: "New tab",
  type: "Type",
  click: "Click",
  select_option: "Choose",
  set_date: "Set date",
  upload_file: "Upload",
  scroll: "Scroll",
  zoom: "Zoom",
  wait: "Wait",
  observe: "Observe",
  switch_tab: "Switch tab",
  select_tab: "Switch tab",
  close_tab: "Close tab",
};

/** One plain-language line for one preview step. No HTML is produced. */
export function formatPlanStep(step: PlanPreviewStep): string {
  const num = typeof step.index === "number" ? step.index + 1 : 1;
  const label = TYPE_LABELS[step.type] ?? step.type;
  const risk = step.risk && step.risk !== "read" ? ` [${step.risk}]` : "";
  const target = step.target?.name
    ? ` — ${step.target.name}${step.target.role ? ` (${step.target.role})` : ""}`
    : "";
  return `${num}. ${label}${risk}${target}: ${step.detail}`;
}

/** Heading for the preview block, e.g. "Plan preview — 3 steps". */
export function planPreviewTitle(plan: PlanPreviewStep[] | null | undefined): string {
  const n = Array.isArray(plan) ? plan.length : 0;
  if (n === 0) return "Plan preview";
  return `Plan preview — ${n} step${n === 1 ? "" : "s"}`;
}
