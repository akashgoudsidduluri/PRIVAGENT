/** Task 14: operator-facing label for a task's routing decision.
 *
 * Connector routes carry their label in `via` ("via Gmail
 * connector"); WebMCP/UCP steps carry the tool / merchant name.
 * Names only, never content — and the caller still escapes the
 * result before putting it in the DOM. Returns null when the task
 * has no routing decision (plain browser task).
 */
export function taskRouteLabel(route: unknown): string | null {
  if (!route || typeof route !== "object") return null;
  const r = route as Record<string, unknown>;
  if (typeof r.via === "string" && r.via.startsWith("via ")) return r.via;
  if (r.via === "webmcp_tool" && typeof r.tool === "string" && r.tool) {
    return `via WebMCP tool ${r.tool}`;
  }
  if (r.via === "ucp_checkout" && typeof r.merchant === "string" && r.merchant) {
    return `via UCP checkout at ${r.merchant}`;
  }
  return null;
}
