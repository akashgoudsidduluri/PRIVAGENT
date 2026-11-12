import type { CompanionSnapshot } from "./companionDrawer";

export interface ProgressPacket {
  taskId: string;
  taskState: string;
  stepsDone: number;
  message: string;
  instruction?: string;
  primaryTabId?: number | null;
  childTabIds?: number[] | null;
}

export const emptyCompanionState = (): CompanionSnapshot => ({
  taskId: null, stepIndex: 0, status: "idle", currentThought: "", directive: "", pendingGate: null, timeline: [],
});

export function nextCompanionState(previous: CompanionSnapshot, packet: ProgressPacket): CompanionSnapshot {
  const sameTask = previous.taskId === packet.taskId;
  const message = String(packet.message || "").slice(0, 220);
  const timeline = sameTask ? [...previous.timeline] : [];
  if (message && timeline[timeline.length - 1] !== message) timeline.push(message);
  return {
    taskId: packet.taskId, stepIndex: Math.max(0, packet.stepsDone || 0), status: packet.taskState,
    currentThought: message, directive: String(packet.instruction || previous.directive || "").slice(0, 160),
    pendingGate: packet.taskState === "awaiting_confirmation" && sameTask ? previous.pendingGate : null,
    timeline: timeline.slice(-20),
  };
}

export function authorizedCompanionTab(tabId: number | undefined, primaryTabId: number | null, childTabIds: number[]): boolean {
  return typeof tabId === "number" && (tabId === primaryTabId || childTabIds.includes(tabId));
}
