import "./styles/agent.css";
import { escapeHtml, linkify, linkLabel, renderMarkdown, safeHttpUrl, safeImageSrc } from "./sanitize";
import { taskRouteLabel } from "./routeLabel";
import { apiFetch, artifactUrl, wsUrl } from "./auth";
import { formatPlanStep, planPreviewTitle, type PlanPreviewStep } from "./planPreview";
import { buildTimelineRows, formatDuration, timelineSummary } from "./timeline";

const API = "http://localhost:8000";

// Workspace & Sidebar Elements
let agentWorkspace: HTMLElement | null = null;
const btnToggleSidebarHeader = document.getElementById("btnToggleSidebarHeader") as HTMLButtonElement | null;
let btnCollapseSidebar: HTMLButtonElement | null = null;
let btnNewMissionSidebar: HTMLButtonElement | null = null;
let chatHistoryList: HTMLUListElement | null = null;

// Directive & Timeline Elements
let missionInput: HTMLTextAreaElement | null = null;
let missionAttachment: HTMLInputElement | null = null;
let btnCancelMission: HTMLButtonElement | null = null;
let btnSendMission: HTMLButtonElement | null = null;
let btnAttachMission: HTMLButtonElement | null = null;
let composerChips: HTMLElement | null = null;
// The mission submitter, wired in initWorkspaceElements(). Enter in the
// input and the olive Send pill both call this; while a mission runs
// the Send pill morphs into the crimson Stop pill (see
// setButtonWorking). Assigned once, when the workspace mounts.
let startMission: (() => Promise<void>) | null = null;
const btnNewSession = document.getElementById("btnNewSession") as HTMLButtonElement | null;
let missionSubtext: HTMLElement | null = null;
let stateBadge: HTMLElement | null = null;
let stepsList: HTMLOListElement | null = null;
let stepsCount: HTMLElement | null = null;
let resultCard: HTMLElement | null = null;
let resultContent: HTMLElement | null = null;
let resultBadge: HTMLElement | null = null;
let resultTimestamp: HTMLElement | null = null;
let artifactsCard: HTMLElement | null = null;
let artifactsList: HTMLElement | null = null;
let artifactsCount: HTMLElement | null = null;

interface ArtifactItem {
  artifact_id: string;
  task_id: string;
  conversation_id: string;
  filename: string;
  file_format: string;
  file_size_bytes: number;
  title: string;
  summary: string;
  created_at: number;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function loadArtifacts(taskId?: string | null, chatId?: string | null, isCurrent?: () => boolean): Promise<void> {
  if (!artifactsCard || !artifactsList) return;
  const targetUrl = taskId ? `${API}/api/tasks/${taskId}/artifacts` : (chatId ? `${API}/api/chats/${chatId}/artifacts` : null);
  if (!targetUrl) {
    artifactsCard.hidden = true;
    return;
  }

  try {
    const res = await apiFetch(targetUrl);
    if (!res.ok) {
      artifactsCard.hidden = true;
      return;
    }
    const list = (await res.json()) as ArtifactItem[];
    // A newer chat selection superseded this one while the fetch was
    // in flight: never render the stale chat's artifacts.
    if (isCurrent && !isCurrent()) return;
    if (!Array.isArray(list) || list.length === 0) {
      artifactsCard.hidden = true;
      return;
    }

    artifactsCard.hidden = false;
    if (artifactsCount) artifactsCount.textContent = `${list.length} artifact${list.length === 1 ? "" : "s"}`;
    artifactsList.innerHTML = "";

    list.forEach(art => {
      const itemEl = document.createElement("div");
      itemEl.className = "artifact-item";
      // Task 12 XSS audit: file_format is backend-supplied metadata, so
      // it must never be interpolated raw into a class attribute. Only
      // a known format token is used; anything else falls back to "file".
      const KNOWN_FORMATS = new Set(["pdf", "docx", "pptx", "xlsx", "csv", "html", "md", "txt", "png", "jpg", "jpeg"]);
      const fmt = KNOWN_FORMATS.has((art.file_format || "").toLowerCase()) ? (art.file_format || "file").toLowerCase() : "file";
      const badgeCls = `artifact-format-badge artifact-format-badge--${fmt}`;
      const sizeStr = formatBytes(art.file_size_bytes || 0);
      // artifact_id is backend-generated hex, but encode it anyway so a
      // compromised/compromising id can never break out of the href.
      const artId = encodeURIComponent(art.artifact_id || "");

      itemEl.innerHTML = `
        <div class="artifact-info">
          <span class="${badgeCls}">${escapeHtml(fmt.toUpperCase())}</span>
          <div class="artifact-details">
            <span class="artifact-filename" title="${escapeHtml(art.title || art.filename)}">${escapeHtml(art.title || art.filename)}</span>
            <span class="artifact-meta">${escapeHtml(art.filename)} · ${sizeStr}</span>
          </div>
        </div>
        <div class="artifact-actions">
          <a href="${artifactUrl(`${API}/api/artifacts/${artId}/preview`)}" target="_blank" rel="noopener noreferrer" class="btn-artifact btn-artifact--preview">Preview</a>
          <a href="${artifactUrl(`${API}/api/artifacts/${artId}/download`)}" class="btn-artifact btn-artifact--download" download>Download</a>
        </div>
      `;
      artifactsList?.appendChild(itemEl);
    });
  } catch {
    artifactsCard.hidden = true;
  }
}

// Human Gate
let humanGate: HTMLElement | null = null;
let humanGateMsg: HTMLElement | null = null;
let btnGateApprove: HTMLButtonElement | null = null;
let btnGateApproveStep: HTMLButtonElement | null = null;
let btnGateReject: HTMLButtonElement | null = null;
let planPreview: HTMLElement | null = null;
let planPreviewTitleEl: HTMLElement | null = null;
let planPreviewList: HTMLOListElement | null = null;
let taskTimelineCard: HTMLElement | null = null;
let taskTimelineList: HTMLOListElement | null = null;
let timelineSummaryEl: HTMLElement | null = null;

// Takeover Banner (Task 4: the agent is paused and not watching)
let takeoverBanner: HTMLElement | null = null;
let takeoverMsg: HTMLElement | null = null;
let takeoverMeta: HTMLElement | null = null;
let btnTakeoverResume: HTMLButtonElement | null = null;
let btnTakeoverCancel: HTMLButtonElement | null = null;

// Inspector
let inspectorUrl: HTMLAnchorElement | null = null;
let browserFrameUrlText: HTMLElement | null = null;
let telDomNodes: HTMLElement | null = null;
let telPiiMasked: HTMLElement | null = null;
let inspectorTitle: HTMLElement | null = null;
let inspectorEvidence: HTMLElement | null = null;
let telStepsDone: HTMLElement | null = null;
let telRemoteVision: HTMLElement | null = null;
let telPlannerLatency: HTMLElement | null = null;
let obstacleBadge: HTMLElement | null = null;
let inspectorScreenshot: HTMLImageElement | null = null;
let inspectorScreenshotPlaceholder: HTMLElement | null = null;

// WS status
const wsDot = document.getElementById("wsDot") as HTMLElement | null;
const wsText = document.getElementById("wsText") as HTMLElement | null;
// ---------------------------------------------------------------------------
// Lazy workspace mount (UI flicker fix).
// The workspace markup lives inert inside <template id="workspaceTemplate">
// in agent.html, so it is never painted until the operator opens the Agent
// Workspace, Browser Inspector, or Activity view. No workspace node may be
// resolved at module load: every workspace ref above starts null and is
// resolved here, once, on first mount, before any listener wiring or data
// load that touches those nodes. The static header (sidebar toggle, New
// Mission, connection status) is wired at module load and stays live.
// ---------------------------------------------------------------------------
let workspaceMounted = false;

export function ensureWorkspaceMounted(): HTMLElement | null {
  if (workspaceMounted) return agentWorkspace;
  const tpl = document.getElementById("workspaceTemplate") as HTMLTemplateElement | null;
  if (!tpl) return null;
  const frag = tpl.content.cloneNode(true) as DocumentFragment;
  if (!frag.querySelector("#agentWorkspace")) return null;
  // Replace the inert template with its live clone, in place.
  tpl.replaceWith(frag);
  workspaceMounted = true;
  initWorkspaceElements();
  return agentWorkspace;
}

function initWorkspaceElements(): void {
  agentWorkspace = document.getElementById("agentWorkspace") as HTMLElement | null;
  btnCollapseSidebar = document.getElementById("btnCollapseSidebar") as HTMLButtonElement | null;
  btnNewMissionSidebar = document.getElementById("btnNewMissionSidebar") as HTMLButtonElement | null;
  chatHistoryList = document.getElementById("chatHistoryList") as HTMLUListElement | null;
  missionInput = document.getElementById("missionInput") as HTMLTextAreaElement | null;
  missionAttachment = document.getElementById("missionAttachment") as HTMLInputElement | null;
  btnCancelMission = document.getElementById("btnCancelMission") as HTMLButtonElement | null;
  btnSendMission = document.getElementById("btnSendMission") as HTMLButtonElement | null;
  btnAttachMission = document.getElementById("btnAttachMission") as HTMLButtonElement | null;
  composerChips = document.getElementById("composerChips") as HTMLElement | null;
  browserFrameUrlText = document.getElementById("browserFrameUrlText") as HTMLElement | null;
  telDomNodes = document.getElementById("telDomNodes") as HTMLElement | null;
  telPiiMasked = document.getElementById("telPiiMasked") as HTMLElement | null;
  missionSubtext = document.getElementById("missionSubtext") as HTMLElement | null;
  stateBadge = document.getElementById("missionStateBadge") as HTMLElement | null;
  stepsList = document.getElementById("agentStepsList") as HTMLOListElement | null;
  stepsCount = document.getElementById("feedStepsCount") as HTMLElement | null;
  resultCard = document.getElementById("agentResultCard") as HTMLElement | null;
  resultContent = document.getElementById("resultContent") as HTMLElement | null;
  resultBadge = document.getElementById("resultBadge") as HTMLElement | null;
  resultTimestamp = document.getElementById("resultTimestamp") as HTMLElement | null;
  artifactsCard = document.getElementById("agentArtifactsCard") as HTMLElement | null;
  artifactsList = document.getElementById("agentArtifactsList") as HTMLElement | null;
  artifactsCount = document.getElementById("artifactsCount") as HTMLElement | null;
  humanGate = document.getElementById("humanGate") as HTMLElement | null;
  humanGateMsg = document.getElementById("humanGateMsg") as HTMLElement | null;
  btnGateApprove = document.getElementById("btnGateApprove") as HTMLButtonElement | null;
  btnGateApproveStep = document.getElementById("btnGateApproveStep") as HTMLButtonElement | null;
  btnGateReject = document.getElementById("btnGateReject") as HTMLButtonElement | null;
  planPreview = document.getElementById("planPreview") as HTMLElement | null;
  planPreviewTitleEl = document.getElementById("planPreviewTitle") as HTMLElement | null;
  planPreviewList = document.getElementById("planPreviewList") as HTMLOListElement | null;
  taskTimelineCard = document.getElementById("taskTimelineCard") as HTMLElement | null;
  taskTimelineList = document.getElementById("taskTimelineList") as HTMLOListElement | null;
  timelineSummaryEl = document.getElementById("timelineSummary") as HTMLElement | null;
  takeoverBanner = document.getElementById("takeoverBanner") as HTMLElement | null;
  takeoverMsg = document.getElementById("takeoverMsg") as HTMLElement | null;
  takeoverMeta = document.getElementById("takeoverMeta") as HTMLElement | null;
  btnTakeoverResume = document.getElementById("btnTakeoverResume") as HTMLButtonElement | null;
  btnTakeoverCancel = document.getElementById("btnTakeoverCancel") as HTMLButtonElement | null;
  inspectorUrl = document.getElementById("inspectorUrl") as HTMLAnchorElement | null;
  inspectorTitle = document.getElementById("inspectorTitle") as HTMLElement | null;
  inspectorEvidence = document.getElementById("inspectorEvidence") as HTMLElement | null;
  telStepsDone = document.getElementById("telStepsDone") as HTMLElement | null;
  telRemoteVision = document.getElementById("telRemoteVision") as HTMLElement | null;
  telPlannerLatency = document.getElementById("telPlannerLatency") as HTMLElement | null;
  obstacleBadge = document.getElementById("obstacleBadge") as HTMLElement | null;
  inspectorScreenshot = document.getElementById("inspectorScreenshot") as HTMLImageElement | null;
  inspectorScreenshotPlaceholder = document.getElementById("inspectorScreenshotPlaceholder") as HTMLElement | null;

  // Workspace listeners — wired once, on first mount. The handlers below
  // are unchanged from the old module-top-level wiring; they only moved.
  btnCollapseSidebar?.addEventListener("click", () => {
    agentWorkspace?.classList.add("sidebar-collapsed");
  });

  btnNewMissionSidebar?.addEventListener("click", () => {
    createNewChat();
  });

  // Enter submits the mission (Shift+Enter inserts a newline). The
  // olive Send pill is the same submit path, for pointer users.
  missionInput?.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void startMission?.();
    }
  });

  // Auto-expanding textarea (capped at 160px, per the Editorial
  // composer spec) so a long directive never hides the Send pill.
  missionInput?.addEventListener("input", () => {
    if (!missionInput) return;
    missionInput.style.height = "auto";
    missionInput.style.height = `${Math.min(missionInput.scrollHeight, 160)}px`;
  });

  // The olive Send pill submits the mission.
  btnSendMission?.addEventListener("click", () => {
    void startMission?.();
  });

  // The Attach ghost pill opens the hidden native file input.
  btnAttachMission?.addEventListener("click", () => {
    missionAttachment?.click();
  });

  // Render the file chips when a file is chosen.
  missionAttachment?.addEventListener("change", () => {
    renderAttachmentChips();
  });

  startMission = async (): Promise<void> => {
    if (isWorking || !missionInput || !missionInput.value.trim()) return;
    const instruction = missionInput.value.trim();
    activeInstruction = instruction;

    setButtonWorking(true);
    if (resultCard) resultCard.hidden = true;
    hideHumanGate();
    currentTaskId = null;
    renderedTerminalTaskId = null;
    cancelRequested = false;

    // Append user instruction bubble immediately (with the attached
    // file name, if any, so the user sees the agent received it).
    const attachedFileName = missionAttachment?.files?.[0]?.name;
    addUserMessageItem(instruction, attachedFileName);
    missionInput.value = "";
    missionInput.style.height = "auto";

    if (telPlannerLatency) telPlannerLatency.textContent = "—";
    setTaskState("planning");

    try {
      let fileToken: string | undefined;
      const selectedFile = missionAttachment?.files?.[0];
      if (selectedFile) {
        if (selectedFile.size > 5 * 1024 * 1024 || !/\.(pdf|png|jpe?g|txt|csv|docx|xlsx)$/i.test(selectedFile.name)) {
          throw new Error("Attachment must be an allowed file under 5 MiB");
        }
        const encoded = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
          reader.onerror = () => reject(new Error("Could not read attachment"));
          reader.readAsDataURL(selectedFile);
        });
        const uploadResponse = await apiFetch(`${API}/api/attachments`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ fileName: selectedFile.name, base64: encoded }),
        });
        if (!uploadResponse.ok) throw new Error(`Attachment rejected (HTTP ${uploadResponse.status})`);
        fileToken = ((await uploadResponse.json()) as { fileToken: string }).fileToken;
      }
      let res: Response;
      if (activeChatId) {
        res = await apiFetch(`${API}/api/chat/${activeChatId}/message`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ instruction, fileToken }),
        });
      } else {
        res = await apiFetch(`${API}/api/task`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ instruction, fileToken }),
        });
      }

      if (!res.ok) throw new Error(`Backend response HTTP ${res.status}`);
      const data = (await res.json()) as { task_id?: string; taskId?: string; chat_id?: string; chatId?: string };
      currentTaskId = data.task_id || data.taskId || null;
      if (missionAttachment) missionAttachment.value = "";
      renderAttachmentChips();
      activeChatId = activeChatId || data.chat_id || data.chatId || null;

      // Refresh chat list to show active title and timestamp
      loadChatList();

      // Cancel clicked while POST was in flight
      if (cancelRequested && currentTaskId) {
        try {
          await apiFetch(`${API}/api/task/${currentTaskId}/cancel`, { method: "POST" });
        } catch { /* offline */ }
        setTaskState("cancelled");
        if (btnCancelMission) btnCancelMission.hidden = true;
        setButtonWorking(false);
        return;
      }

      // Start background sync / polling for active task
      if (currentTaskId) {
        startFallbackPolling(currentTaskId);
        const stRes = await apiFetch(`${API}/api/task/${currentTaskId}`).catch(() => null);
        if (stRes && stRes.ok) {
          const t = (await stRes.json()) as { taskState: string; message: string; stepsDone?: number; screenshot?: string | null };
          if (t.taskState && t.taskState !== "planning") {
            setTaskState(t.taskState);
          }
          // Unconditional: a screenshot-less task must clear the frame
          // rather than keep the previous chat's image.
          updateVisualFrame(t.screenshot ?? null, t.message || "", t.taskState);
          if (["completed", "failed", "cancelled"].includes(t.taskState)) {
            stopFallbackPolling();
            renderCompletion(t.taskState, t.message || "", true, t.stepsDone);
            loadChatList();
          }
        }
      }
    } catch (err) {
      stopFallbackPolling();
      clearStalledNotice();
      setTaskState("failed");
      addStepItem(`⚠️ Backend connection failed: ${err}`);
      setButtonWorking(false);
      cancelRequested = false;
      if (btnCancelMission) btnCancelMission.hidden = true;
    }
  };

  btnCancelMission?.addEventListener("click", async () => {
    const id = currentTaskId;
    cancelRequested = true;
    stopFallbackPolling();
    clearStalledNotice();
    if (btnCancelMission) btnCancelMission.hidden = true;
    hideHumanGate();
    if (id) {
      try {
        await apiFetch(`${API}/api/task/${id}/cancel`, { method: "POST" });
      } catch {
        // Offline
      }
    }
    setTaskState("cancelled");
    setButtonWorking(false);
  });

  btnGateApprove?.addEventListener("click", async () => {
    if (!currentTaskId) return;
    hideHumanGate();
    await apiFetch(`${API}/api/task/${currentTaskId}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: "approve" }),
    });
  });

  // Task 12: approve this step only — the backend runs the pending step
  // and then re-plans, so the next step gets its own preview instead of
  // running on as part of the pinned batch. This is a gate decision,
  // not an override: the loop still enforces every safety gate.
  btnGateApproveStep?.addEventListener("click", async () => {
    if (!currentTaskId) return;
    hideHumanGate();
    await apiFetch(`${API}/api/task/${currentTaskId}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: "approve_step" }),
    });
  });

  btnGateReject?.addEventListener("click", async () => {
    if (!currentTaskId) return;
    hideHumanGate();
    await apiFetch(`${API}/api/task/${currentTaskId}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: "reject" }),
    });
  });

  // Task 4: takeover resume / cancel. Resume tells the backend the
  // operator finished the manual step; the loop re-observes from scratch
  // (its first post-takeover observation carries no screenshot).
  btnTakeoverResume?.addEventListener("click", async () => {
    if (!currentTaskId) return;
    hideTakeover();
    await apiFetch(`${API}/api/task/${currentTaskId}/takeover/resume`, { method: "POST" });
  });

  btnTakeoverCancel?.addEventListener("click", async () => {
    if (!currentTaskId) return;
    hideTakeover();
    await apiFetch(`${API}/api/task/${currentTaskId}/takeover/cancel`, { method: "POST" });
  });

  // Now that the nodes exist, load the data that renders into them.
  // No auto-select on mount: opening (or refreshing) the workspace
  // must land on a blank New Mission, not re-open the latest completed
  // chat. The sidebar still lists every mission; clicking one opens it.
  void loadChatList({ autoSelect: false });
  void loadTelemetryConfig();
}

// Session & Task State
let activeChatId: string | null = null;

// Generation guard for selectChat: two rapid sidebar clicks resolve in
// arbitrary order, and the slower (wrong) chat's messages must never
// overwrite the newer selection's feed.
let selectChatGeneration = 0;
let currentTaskId: string | null = null;
let renderedTerminalTaskId: string | null = null;
let stepCounter = 0;
let cancelRequested = false;
let isWorking = false;
let lastProgressTime = 0;
let pollInterval: number | null = null;
// Instruction of the current (or last loaded) turn. Used to decide whether a
// price badge is meaningful, so explanatory "$500" examples are not treated as
// the mission's product price.
let activeInstruction = "";

interface ChatSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  status: string;
}

interface ChatStepItem {
  stepNum: number;
  message: string;
  action?: any;
  status?: string;
  result_ok?: boolean;
}

interface ChatMessage {
  id: string;
  chat_id: string;
  role: "user" | "agent" | "system";
  content: string;
  evidence?: string | null;
  task_id?: string | null;
  steps?: ChatStepItem[];
  created_at: string;
}

interface ActiveTaskInfo {
  taskId: string;
  taskState: string;
  stepsDone: number;
  message: string;
  screenshot?: string | null;
  intent?: string | null;
  isBrowserTask?: boolean;
  sessionId?: string | null;
  primaryTabId?: number | null;
  childTabIds?: number[];
  steps?: ChatStepItem[];
  // Task 4: the active takeover request, if any (kind/reason/url only).
  takeover?: { kind?: string; reason?: string; url?: string } | null;
}

interface ChatDetail {
  id?: string;
  title?: string;
  status?: string;
  chat?: ChatSummary;
  messages: ChatMessage[];
  memory: Record<string, string>;
  active_task?: ActiveTaskInfo | null;
  total_steps?: number;
}

function setConnectionStatus(online: boolean, text: string): void {
  if (wsDot) {
    wsDot.className = online ? "status-dot status-dot--online" : "status-dot status-dot--offline";
  }
  if (wsText) wsText.textContent = text;
}

function setTaskState(state: string): void {
  if (!stateBadge) return;
  stateBadge.textContent = state.toUpperCase();
  if (["running", "planning"].includes(state.toLowerCase())) {
    stateBadge.style.color = "var(--accent)";
    stateBadge.style.background = "var(--accent-soft)";
  } else if (state === "completed") {
    stateBadge.style.color = "var(--privacy)";
    stateBadge.style.background = "var(--privacy-soft)";
  } else if (state === "awaiting_takeover") {
    // Task 4: takeover is not an error — terracotta amber, distinct
    // from the confirmation gate's default styling.
    stateBadge.style.color = "var(--gate)";
    stateBadge.style.background = "var(--gate-soft)";
  } else if (["failed", "cancelled"].includes(state)) {
    stateBadge.style.color = "var(--danger)";
    stateBadge.style.background = "var(--danger-soft)";
  } else {
    stateBadge.style.color = "";
    stateBadge.style.background = "";
  }
}

// Telemetry pills reflect backend reality instead of hardcoded markup.
// Fetched once per page load from /api/health; failures keep sensible text.
let stepBudget: number | null = null;

function renderStepBudget(): void {
  const done = stepCounter;
  if (telStepsDone) {
    telStepsDone.textContent = stepBudget ? `${done} / ${stepBudget}` : `${done}`;
  }
}

async function loadTelemetryConfig(): Promise<void> {
  try {
    const res = await apiFetch(`${API}/api/health`);
    if (!res.ok) return;
    const h = (await res.json()) as { maxSteps?: number; allowScreenshots?: boolean };
    if (typeof h.maxSteps === "number" && h.maxSteps > 0) stepBudget = h.maxSteps;
    if (telRemoteVision) {
      telRemoteVision.textContent = h.allowScreenshots ? "On (opted in)" : "Off";
    }
    renderStepBudget();
  } catch {
    /* offline: keep existing markup values */
  }
}

function updateVisualFrame(screenshot: string | null | undefined, message: string, taskState: string): void {
  const m = message.toLowerCase();
  const hasObstacle = /\b(skip\s*ad|advertisement|cookie\s*banner|close\s*popup|modal\s*overlay|dismiss\s*dialog)\b/.test(m);

  if (obstacleBadge) {
    if (taskState === "awaiting_confirmation" || m.includes("authentication required") || m.includes("credentials required")) {
      obstacleBadge.className = "obstacle-badge obstacle-badge--shielded";
      obstacleBadge.textContent = "🔒 Shielded (Auth/Payment)";
    } else if (hasObstacle) {
      obstacleBadge.className = "obstacle-badge obstacle-badge--detected";
      obstacleBadge.textContent = "⚠️ Obstacle Detected (Ad/Modal)";
    } else {
      obstacleBadge.className = "obstacle-badge obstacle-badge--clear";
      obstacleBadge.textContent = "✓ Workflow Clear";
    }
  }

  const dataUrl = safeImageSrc(screenshot);
  if (dataUrl && inspectorScreenshot) {
    inspectorScreenshot.src = dataUrl;
    inspectorScreenshot.hidden = false;
    if (inspectorScreenshotPlaceholder) inspectorScreenshotPlaceholder.hidden = true;
  } else if (inspectorScreenshot) {
    // No screenshot for this step: clear the frame instead of leaving
    // the previous step's — or the previous chat's — image up as if it
    // were this step's visual.
    inspectorScreenshot.removeAttribute("src");
    inspectorScreenshot.hidden = true;
    if (inspectorScreenshotPlaceholder) inspectorScreenshotPlaceholder.hidden = false;
  }
}

function actionIcon(msg: string): string {
  const m = msg.toLowerCase();
  if (m.includes("navigate") || m.includes("opened") || m.includes("http")) return "🧭";
  if (m.includes("click")) return "🖱️";
  if (m.includes("type") || m.includes("searched") || m.includes("submit")) return "⌨️";
  if (m.includes("scroll")) return "📜";
  if (m.includes("zoom")) return "🔍";
  if (m.includes("wait")) return "⏳";
  if (m.includes("sign-in") || m.includes("login") || m.includes("sensitive") || m.includes("credential")) return "🔐";
  return "⚡";
}

function formatResultHtml(rawMessage: string, includeRawMessage = true): string {
  const urlMatch = rawMessage.match(/https?:\/\/[^\s"'<>)]+/);
  // Show a single price badge only for a shopping directive. Explanatory
  // answers often contain an example price ("price drops below $500"), and
  // surfacing that as the mission's price is misleading.
  const isCommerce = /\b(?:buy|purchase|price|cost|checkout|cart|order|deal|discount|shop|store|product|amazon|flipkart|croma|reliance|samsung|myntra|dress|laptop|phone|specs?)\b/i.test(activeInstruction);
  const allPrices = rawMessage.match(/(?:₹|Rs\.?|INR|\$)\s*\d[\d,]*(?:\.\d+)?/gi) ?? [];
  const normalized = new Set(allPrices.map(p => p.replace(/\s+/g, "")));
  const directUrl = urlMatch ? safeHttpUrl(urlMatch[0]) : null;
  const price = isCommerce && normalized.size === 1 && allPrices.length > 0 ? (allPrices[0] ?? "").trim() : "";

  let showcaseHtml = "";

  // 1. Multi-Store Comparison Card Extraction
  const hasMultipleStores = /amazon|flipkart|croma|reliance|samsung|myntra/i.test(rawMessage) && allPrices.length >= 2;
  if (isCommerce && hasMultipleStores) {
    const storeCards: string[] = [];
    const csvRows: string[][] = [["Store", "Details", "Price", "Link"]];
    const storePatterns = [
      { name: "Amazon India", pattern: /amazon(?:\.in)?[:\s\-]+([^;|\n]+)/i, color: "amber" },
      { name: "Flipkart", pattern: /flipkart(?:\.com)?[:\s\-]+([^;|\n]+)/i, color: "blue" },
      { name: "Croma Retail", pattern: /croma(?:\.com)?[:\s\-]+([^;|\n]+)/i, color: "teal" },
      { name: "Reliance Digital", pattern: /reliance(?:\s*digital)?[:\s\-]+([^;|\n]+)/i, color: "red" },
      { name: "Samsung Official", pattern: /samsung(?:\.com)?[:\s\-]+([^;|\n]+)/i, color: "indigo" },
    ];

    let lowestPriceVal = Infinity;
    let lowestStoreName = "";

    for (const sp of storePatterns) {
      const match = rawMessage.match(sp.pattern);
      if (match) {
        const snippet = match[1];
        const pMatch = snippet.match(/(?:₹|Rs\.?|INR|\$)\s*(\d[\d,]*)/i);
        const pStr = pMatch ? pMatch[0].replace(/\s+/g, " ") : "";
        const pVal = pMatch ? parseInt(pMatch[1].replace(/,/g, ""), 10) : 0;
        const uMatch = snippet.match(/https?:\/\/[^\s"'<>)]+/);
        const uStr = uMatch ? safeHttpUrl(uMatch[0]) : "";

        if (pVal > 0 && pVal < lowestPriceVal) {
          lowestPriceVal = pVal;
          lowestStoreName = sp.name;
        }

        csvRows.push([sp.name, snippet.replace(/https?:\/\/\S+/g, "").trim(), pStr, uStr || ""]);

        storeCards.push(`
          <div class="comparison-store-card comparison-store-card--${sp.color}">
            <div class="comparison-store-name">${escapeHtml(sp.name)}</div>
            <div class="comparison-store-price">${pStr ? escapeHtml(pStr) : "Price on site"}</div>
            <div class="comparison-store-details">${escapeHtml(snippet.replace(/https?:\/\/\S+/g, "").trim().slice(0, 95))}</div>
            ${uStr ? `<a href="${escapeHtml(uStr)}" target="_blank" rel="noopener noreferrer" class="comparison-store-btn">View on ${escapeHtml(sp.name.split(" ")[0])} ↗</a>` : ""}
          </div>
        `);
      }
    }

    if (storeCards.length >= 2) {
      const encodedCsv = encodeURIComponent(csvRows.map(r => r.map(c => `"${c.replace(/"/g, '""')}"`).join(",")).join("\n"));
      showcaseHtml = `
        <div class="comparison-showcase">
          <div class="comparison-showcase__header">
            <div>
              <span class="deliverable-badge">📊 Cross-Platform Comparison</span>
              <h4 class="comparison-showcase__title">Product Price &amp; Store Analysis</h4>
            </div>
            <button class="btn-download-excel" data-csv="${encodedCsv}">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Export Excel (.csv)
            </button>
          </div>
          <div class="comparison-grid">
            ${storeCards.join("")}
          </div>
          ${lowestPriceVal < Infinity ? `<div class="comparison-best-deal">⭐ <strong>Best Deal:</strong> ${escapeHtml(lowestStoreName)} at ₹${lowestPriceVal.toLocaleString("en-IN")}</div>` : ""}
        </div>
      `;
    }
  }

  // 2. Single Price Showcase Fallback
  if (!showcaseHtml && (directUrl || (price && /\d/.test(price)))) {
    showcaseHtml = `
      <div class="product-showcase">
        <h4 class="product-showcase__title">Mission Summary</h4>
        <div class="product-showcase__meta">
          ${price && /\d/.test(price) ? `<span class="price-tag">${escapeHtml(price)}</span>` : ""}
          ${directUrl ? `<a href="${escapeHtml(directUrl)}" target="_blank" rel="noopener noreferrer" class="product-link-btn">Open ${escapeHtml(linkLabel(directUrl))} ↗</a>` : ""}
        </div>
      </div>
    `;
  }

  // 3. Suggested Next Moves Launchpad
  const nextMovesHtml = `
    <div class="suggested-moves">
      <span class="suggested-moves__label">Suggested Next Moves:</span>
      <div class="suggested-moves__chips">
        <button class="suggested-chip" data-prompt="Export this comparison into a formatted Excel spreadsheet">📊 Export to Excel</button>
        <!-- "Email this to me": no address is baked in here. The operator's
             address is resolved at runtime from the connected Gmail account
             (or asked for); a personal address must never ship in source. -->
        <button class="suggested-chip" data-prompt="Email this comparison summary to me">📧 Email this to me</button>
        <button class="suggested-chip" data-prompt="Continuously monitor this product price and notify me if it drops">🔔 Monitor price for drops</button>
        <button class="suggested-chip" data-prompt="What is on my calendar this week?">📅 Check Google Calendar</button>
      </div>
    </div>
  `;

  // The raw message is omitted when the caller already shows it in the
  // timeline (see renderCompletion): the result card then carries only
  // the structured extras (comparison showcase, price, links) and the
  // Suggested Next Moves — the reply itself appears once, in the
  // timeline, instead of twice. The watch marker is stripped here too
  // so it never renders as literal text in the card; the interactive
  // watch card lives in the timeline item, not the result card.
  const rawHtml = includeRawMessage
    ? `<div class="result-raw-text md-body">${renderMarkdown(stripWatchMarker(rawMessage).text)}</div>`
    : "";
  return `
    ${showcaseHtml}
    ${rawHtml}
    ${nextMovesHtml}
  `;
}

function addUserMessageItem(message: string, attachmentName?: string): void {
  if (!stepsList) return;
  const placeholder = stepsList.querySelector(".agent-step-placeholder");
  if (placeholder) placeholder.remove();

  const li = document.createElement("li");
  li.className = "agent-step-item agent-step-item--user";
  // Show the attached file name so the user can see the agent received
  // it (the planner is told the same name in the backend prompt).
  // Inlined (not a pre-built string) so the innerHTML guard can see
  // the escapeHtml() call at the assignment site.
  li.innerHTML = `
    <span class="agent-step-item__icon">💬</span>
    <span class="agent-step-item__text">${linkify(message)}${attachmentName ? `<span class="agent-step-item__attachment">📎 ${escapeHtml(attachmentName)}</span>` : ""}</span>
  `;
  stepsList.appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/** Render the composer's file chips from the hidden input's files.
    Each chip shows "📎 name" with an × that clears the input. The
    chip's name is escaped via escapeHtml() at the assignment site
    (inlined, so the innerHTML guard can see it). */
function renderAttachmentChips(): void {
  if (!composerChips) return;
  const files = missionAttachment?.files;
  if (!files || files.length === 0) {
    composerChips.innerHTML = "";
    composerChips.hidden = true;
    return;
  }
  const name = files[0].name;
  composerChips.innerHTML = `<span class="composer-chip">📎 ${escapeHtml(name)} <button type="button" class="composer-chip__remove" aria-label="Remove attachment">×</button></span>`;
  composerChips.hidden = false;
  composerChips.querySelector(".composer-chip__remove")?.addEventListener("click", () => {
    if (missionAttachment) missionAttachment.value = "";
    renderAttachmentChips();
  });
}

function updateStepsCount(count: number): void {
  const timelineSteps = stepsList ? stepsList.querySelectorAll("li.agent-step-item:not(.agent-step-item--user):not(.agent-step-item--result):not(.agent-step-item--failed)").length : 0;
  stepCounter = Math.max(0, count, timelineSteps);
  if (stepsCount) stepsCount.textContent = `${stepCounter} step${stepCounter === 1 ? "" : "s"} executed`;
  renderStepBudget();
}

function addStepItem(message: string, stepNum?: number): void {
  if (!stepsList) return;
  const placeholder = stepsList.querySelector(".agent-step-placeholder");
  if (placeholder) placeholder.remove();

  if (typeof stepNum === "number" && stepNum > 0) {
    updateStepsCount(stepNum);
  } else {
    renderStepBudget();
  }

  // Normalize display: avoid double prefix like "[1] Step 1:"
  let displayMessage = (message || "").trim();
  // An empty progress update must not render a blank row: it shows up
  // as a lone ⚡ icon with no text (the default actionIcon), which
  // looks like a broken step. Progress events with no message are
  // legitimate (state changes), they just carry nothing to show.
  if (!displayMessage) return;
  const stepPrefixRegex = /^\[\d+\]\s*/;
  if (stepPrefixRegex.test(displayMessage)) {
    displayMessage = displayMessage.replace(stepPrefixRegex, "");
  }

  const eventKey = `${stepNum ?? "status"}:${displayMessage}`;
  if (Array.from(stepsList.children).some(node => (node as HTMLElement).dataset.eventKey === eventKey)) return;

  // "Thinking..." is a live status, not a completed step. It is removed when
  // the turn reaches a terminal state so it never looks stuck in the timeline.
  const isTransient = /^thinking\.{0,3}$/i.test(displayMessage);
  if (isTransient) {
    stepsList.querySelectorAll('li[data-transient="true"]').forEach(el => el.remove());
  }

  // Deduplicate against the immediate previous step
  const existingSteps = stepsList.querySelectorAll("li.agent-step-item");
  if (existingSteps.length > 0) {
    const lastEl = existingSteps[existingSteps.length - 1];
    const lastText = (lastEl.querySelector(".agent-step-item__text")?.textContent || "").trim();
    if (lastText === displayMessage) {
      return;
    }
  }

  const li = document.createElement("li");
  li.className = isTransient
    ? "agent-step-item agent-step-item--thinking"
    : "agent-step-item agent-step-item--completed";
  li.dataset.eventKey = eventKey;
  if (isTransient) li.dataset.transient = "true";
  if (typeof stepNum === "number" && stepNum > 0) li.dataset.stepNum = String(stepNum);
  // The live "Thinking" row gets a subtle warm-stone pulse bar +
  // "Sanitizing DOM locally…" label (no bouncing dots) so the user
  // can see at a glance the agent is still working. The row's
  // textContent is still "Thinking..." and the dedup above keeps
  // working. Completed steps carry --completed so the stepper node
  // renders a forest-sage checkmark. Inlined (not a pre-built
  // string) so the innerHTML guard can see the escaping at the
  // assignment site.
  //
  // Completed steps are expandable: the row is a button (chevron +
  // text) and clicking it opens a detail panel with the step number,
  // the full message, and a Copy button — the step stream is no
  // longer a static log the user can only read. The Thinking row
  // stays a plain status line (it is transient, not a step).
  li.innerHTML = isTransient
    ? `
    <span class="agent-step-item__icon">◌</span>
    <span class="agent-step-item__text"><span class="thinking-label">Sanitizing DOM locally…</span><span class="thinking-pulse" aria-hidden="true"></span></span>
  `
    : `
    <span class="agent-step-item__icon">✓</span>
    <div class="agent-step-item__main">
      <button type="button" class="agent-step-item__toggle" aria-expanded="false">
        <span class="agent-step-item__chevron" aria-hidden="true">›</span>
        <span class="agent-step-item__text">${linkify(displayMessage)}</span>
      </button>
      <div class="agent-step-item__detail" hidden>
        <div class="agent-step-item__detail-meta">${typeof stepNum === "number" && stepNum > 0 ? `Step ${escapeHtml(String(stepNum))} · ` : ""}Completed</div>
        <div class="agent-step-item__detail-text">${escapeHtml(displayMessage)}</div>
        <button type="button" class="agent-step-item__copy" data-copy-step>Copy step</button>
      </div>
    </div>
  `;
  stepsList.appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });

  const urlMatch = displayMessage.match(/https?:\/\/[^\s"'<>)]+/);
  const safeUrl = urlMatch ? safeHttpUrl(urlMatch[0]) : null;
  if (safeUrl && inspectorUrl) {
    inspectorUrl.href = safeUrl;
    inspectorUrl.textContent = safeUrl;
  }
  if (safeUrl && browserFrameUrlText) {
    browserFrameUrlText.textContent = safeUrl;
  }
  if (inspectorEvidence) {
    inspectorEvidence.textContent = displayMessage;
  }
}

function showStalledNotice(): void {
  if (missionSubtext && isWorking) {
    missionSubtext.innerHTML = `
      <span style="color: var(--gate); font-weight: 500;">
        ⏳ Progress is taking longer than usual (planner waiting for model response). You may wait or click Stop.
      </span>
    `;
  }
}

function clearStalledNotice(): void {
  if (missionSubtext) {
    // The subtext is a live status line only — no decorative default.
    // .agent-subtext:empty hides it until a real status is written.
    missionSubtext.textContent = "";
  }
}

// Working state is a flag, not a button label: there is no Start button
// any more, so there is nothing to re-label. The Cancel button's
// visibility is managed by the call sites, not here.
function setButtonWorking(working: boolean): void {
  isWorking = working;
  if (working) {
    lastProgressTime = Date.now();
  } else {
    clearStalledNotice();
  }
  // The olive Send pill morphs into the crimson Stop pill while a
  // mission runs (same spot); Stop returns to Send when it ends.
  if (btnSendMission) btnSendMission.hidden = working;
  if (btnCancelMission) btnCancelMission.hidden = !working;
}

function normalizeForDedup(text: string): string {
  // Compare messages ignoring markdown markers and whitespace: a
  // progress step renders with linkify (keeps "**bold**" literal) while
  // the result renders with renderMarkdown (strips it), so the same
  // message would otherwise never compare equal. The watch marker is
  // stripped too: the timeline renders the message without it, so a
  // raw confirmation (marker included) must compare equal to the
  // rendered one or the result would be added twice.
  return stripWatchMarker(text).text.replace(/[*_`#]/g, "").replace(/\s+/g, " ").trim();
}

// Chat-only monitoring (2026-11-18): when the agent creates a watch,
// the backend appends `<!-- privagent-watch: <mission-id> -->` to the
// confirmation. The marker is machine-readable only — it is stripped
// before rendering and turned into an interactive watch card (Pause /
// Resume / Cancel / View changes) in the conversation. The id is a
// uuid4 hex string; anything else is not a marker.
const WATCH_MARKER_RE = /<!--\s*privagent-watch:\s*([0-9a-fA-F-]{36})\s*-->/;
function stripWatchMarker(message: string): { text: string; watchId: string | null } {
  const match = (message || "").match(WATCH_MARKER_RE);
  if (!match) return { text: message || "", watchId: null };
  return { text: (message || "").replace(WATCH_MARKER_RE, "").replace(/\n{3,}/g, "\n\n").trim(), watchId: match[1] };
}

async function fetchWatch(missionId: string): Promise<{ id: string; title: string; status: string; condition_type?: string; target_url?: string } | null> {
  try {
    const res = await apiFetch(`${API}/api/control/missions`);
    if (!res.ok) return null;
    const missions = (await res.json()) as Array<{ id: string; title: string; status: string; condition_type?: string; target_url?: string }>;
    return missions.find(m => m.id === missionId) || null;
  } catch {
    return null;
  }
}

async function hydrateWatchCard(card: HTMLElement): Promise<void> {
  const missionId = card.dataset.watchId;
  if (!missionId) return;
  const mission = await fetchWatch(missionId);
  if (!mission) {
    card.querySelector<HTMLElement>(".watch-card__title")!.textContent = "Watch not found";
    card.querySelector<HTMLElement>(".watch-card__note")!.textContent = "This watch may have been cancelled already.";
    card.querySelectorAll<HTMLButtonElement>(".watch-card__btn").forEach(b => { b.disabled = true; });
    return;
  }
  card.querySelector<HTMLElement>(".watch-card__title")!.textContent = mission.title || "Watch active";
  const statusEl = card.querySelector<HTMLElement>("[data-watch-status]")!;
  const pauseBtn = card.querySelector<HTMLButtonElement>("[data-watch-pause]")!;
  const applyStatus = (status: string) => {
    statusEl.textContent = status === "active" ? "Active" : "Paused";
    statusEl.dataset.status = status;
    pauseBtn.textContent = status === "active" ? "Pause" : "Resume";
  };
  applyStatus(mission.status);
}

function isMessageInTimeline(message: string): boolean {
  // Is this exact message already visible in the timeline? A progress
  // step carries it in its whole text; a result/failed item carries it
  // in its .md-body (the "Mission Result:" label is not part of it).
  const norm = normalizeForDedup(message);
  if (!norm || !stepsList) return false;
  const items = stepsList.querySelectorAll("li.agent-step-item");
  for (const item of Array.from(items)) {
    if (item.classList.contains("agent-step-item--user")) continue;
    const body = item.querySelector(".md-body");
    const text = body
      ? body.textContent || ""
      : item.querySelector(".agent-step-item__text")?.textContent || "";
    if (normalizeForDedup(text) === norm) return true;
  }
  return false;
}

function addCompletionStepItem(taskState: string, message: string): void {
  if (!stepsList) return;
  // Strip the watch marker before dedup/render: the timeline shows
  // the confirmation without it, and the card below carries the id.
  const { text: cleanMessage, watchId } = stripWatchMarker(message);
  // No duplicate: if this exact message is already the last thing the
  // timeline shows (e.g. a conversational reply that arrived as a
  // progress step), adding a "Mission Result" item would show it twice.
  // The reply stays once, in the step that already carries it.
  if (isMessageInTimeline(cleanMessage)) return;
  const isOk = taskState === "completed";

  // Each turn owns its own result item. Overwriting a previous turn's result
  // made a follow-up failure erase the answer the user was still reading.
  const li = document.createElement("li");
  li.className = isOk ? "agent-step-item agent-step-item--result" : "agent-step-item agent-step-item--failed";
  // The watch card is inlined here (not a helper call) so the
  // innerHTML guard can see escapeHtml(watchId) at the assignment
  // site. It renders only when the backend appended the watch marker
  // to the confirmation; hydrateWatchCard fills in the real title and
  // status from GET /api/control/missions.
  li.innerHTML = `
    <span class="agent-step-item__icon">${isOk ? "✓" : "✗"}</span>
    <div class="agent-step-item__text">
      <div class="agent-step-item__label"><strong>${isOk ? "Mission Result" : "Task Incomplete"}</strong>:</div>
      <div class="md-body">${renderMarkdown(cleanMessage || (isOk ? "Mission completed." : "Task stopped."))}</div>
      ${watchId ? `
      <div class="watch-card" data-watch-id="${escapeHtml(watchId)}">
        <div class="watch-card__header">
          <span class="watch-card__icon" aria-hidden="true">🔔</span>
          <span class="watch-card__title">Watch active</span>
          <span class="watch-card__status" data-watch-status>Active</span>
        </div>
        <div class="watch-card__body">
          <p class="watch-card__note">This watch lives in this conversation. I'll alert you here the moment anything changes.</p>
        </div>
        <div class="watch-card__actions">
          <button type="button" class="watch-card__btn watch-card__btn--pause" data-watch-pause>Pause</button>
          <button type="button" class="watch-card__btn watch-card__btn--ghost" data-watch-events>View changes</button>
          <button type="button" class="watch-card__btn watch-card__btn--danger" data-watch-cancel>Cancel watch</button>
        </div>
        <div class="watch-card__events" hidden></div>
      </div>
      ` : ""}
    </div>
  `;
  stepsList.appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });
  if (watchId) {
    const card = li.querySelector<HTMLElement>(".watch-card");
    if (card) void hydrateWatchCard(card);
  }
}

async function syncActiveTask(taskId: string): Promise<boolean> {
  try {
    const res = await apiFetch(`${API}/api/task/${taskId}`);
    if (!res.ok) {
      if (res.status === 404 && currentTaskId === taskId) {
        stopFallbackPolling();
        renderCompletion("failed", "Task session not found on server.");
        return true;
      }
      return false;
    }
    const t = (await res.json()) as {
      taskState: string;
      message: string;
      stepsDone?: number;
      screenshot?: string | null;
      intent?: string;
      isBrowserTask?: boolean;
      sessionId?: string | null;
      primaryTabId?: number | null;
      childTabIds?: number[] | null;
      steps?: ChatStepItem[];
      timings?: {
        steps?: Array<{ step: number; action: string; batched: boolean; cache: string; checker_skipped: boolean; total_ms: number; overhead_ms: number }>;
        overhead_ms?: number;
      } | null;
    };
    if (currentTaskId !== taskId) return false;
    lastProgressTime = Date.now();
    clearStalledNotice();

    if (missionSubtext && !missionSubtext.innerHTML.includes("Progress is taking longer")) {
      if (t.isBrowserTask === false || t.intent === "NON_BROWSER") {
        missionSubtext.innerHTML = `<span style="color: var(--accent); font-weight: 500;">💬 Mode: Conversational (Zero Browser Actions)</span>`;
      } else if (t.sessionId) {
        const tabInfo = t.primaryTabId ? ` · Tab #${t.primaryTabId}` : "";
        const childInfo = t.childTabIds && t.childTabIds.length > 0 ? ` (+${t.childTabIds.length} child tabs)` : "";
        missionSubtext.innerHTML = `<span style="color: var(--text-secondary); font-weight: 500;">🌐 Session: ${escapeHtml(t.sessionId.slice(0, 8))}${tabInfo}${childInfo}</span>`;
      }
      // Task 14: routing decision ("via Gmail connector" / "via
      // WebMCP tool X" / "via UCP checkout"). The label is built from
      // names only and escaped; the set above resets innerHTML on
      // every poll, so appending here cannot accumulate.
      const routeText = taskRouteLabel((t as any).route);
      if (routeText) {
        missionSubtext.innerHTML += ` <span style="color: var(--privacy); font-weight: 500;">${escapeHtml(routeText)}</span>`;
      }
    }

    if (t.taskState && t.taskState !== "planning") {
      setTaskState(t.taskState);
    }
    // Unconditional: a screenshot-less poll must clear the frame.
    updateVisualFrame(t.screenshot ?? null, t.message || "", t.taskState);
    // Rehydrate intermediate execution steps if missing from the timeline
    if (Array.isArray(t.steps) && t.steps.length > 0) {
      t.steps.forEach(st => {
        if (st && st.message) {
          addStepItem(st.message, st.stepNum);
        }
      });
    }
    // Task 12: task timeline from the loop timings + history steps.
    renderTaskTimeline(t);
    if (["completed", "failed", "cancelled"].includes(t.taskState)) {
      renderCompletion(t.taskState, t.message || "", true, t.stepsDone);
      loadChatList();
      return true;
    }
    return false;
  } catch {
    return false;
  }
}

function startFallbackPolling(taskId: string): void {
  stopFallbackPolling();
  lastProgressTime = Date.now();
  pollInterval = window.setInterval(async () => {
    if (!isWorking || currentTaskId !== taskId) {
      stopFallbackPolling();
      return;
    }
    const terminal = await syncActiveTask(taskId);
    if (terminal) {
      stopFallbackPolling();
    } else if (Date.now() - lastProgressTime > 45000) {
      showStalledNotice();
    }
  }, 4000);
}

function stopFallbackPolling(): void {
  if (pollInterval !== null) {
    clearInterval(pollInterval);
    pollInterval = null;
  }
}

function renderCompletion(taskState: string, message: string, appendToTimeline = true, stepsDone?: number): void {
  stopFallbackPolling();
  clearStalledNotice();
  setTaskState(taskState);
  setButtonWorking(false);
  if (btnCancelMission) btnCancelMission.hidden = true;
  if (humanGate) humanGate.hidden = true;
  if (takeoverBanner) takeoverBanner.hidden = true;
  if (typeof stepsDone === "number" && stepsDone > 0) {
    updateStepsCount(stepsDone);
  }
  // A live "Thinking..." row must not survive the turn that produced it.
  stepsList?.querySelectorAll('li[data-transient="true"]').forEach(el => el.remove());

  if (currentTaskId && renderedTerminalTaskId === currentTaskId) {
    if (resultContent && !resultCard?.hidden) {
      if (taskState === "completed") {
        // The reply already lives in the timeline (Mission Result item
        // or the progress step that carried it): do not repeat it in
        // the result card. The card keeps the showcase + next moves.
        const showRaw = !isMessageInTimeline(message);
        resultContent.innerHTML = formatResultHtml(message || "The autonomous task completed successfully.", showRaw);
      } else {
        resultContent.innerHTML = `<div class="result-raw-text md-body">${renderMarkdown(stripWatchMarker(message).text || "Task stopped before completing all steps.")}</div>`;
      }
    }
    return;
  }
  if (currentTaskId) {
    renderedTerminalTaskId = currentTaskId;
  }

  if (appendToTimeline) {
    addCompletionStepItem(taskState, message);
  }

  if (resultCard && resultContent && resultBadge) {
    resultCard.hidden = false;
    resultTimestamp && (resultTimestamp.textContent = new Date().toLocaleTimeString());

    if (taskState === "completed") {
      resultCard.classList.remove("is-failed");
      resultBadge.textContent = "✓ Mission Completed";
      // The reply already lives in the timeline (the Mission Result
      // item addCompletionStepItem just added, or the progress step
      // that carried it): do not repeat it in the result card. The
      // card keeps the showcase + next moves; the reply appears once.
      const showRaw = !isMessageInTimeline(message);
      resultContent.innerHTML = formatResultHtml(message || "The autonomous task completed successfully.", showRaw);
    } else {
      resultCard.classList.add("is-failed");
      resultBadge.textContent = "✗ Task Blocked / Incomplete";
      resultContent.innerHTML = `<div class="result-raw-text md-body">${renderMarkdown(stripWatchMarker(message).text || "Task stopped before completing all steps.")}</div>`;
    }
    resultCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
    loadArtifacts(currentTaskId, activeChatId);
  }
}

function showHumanGate(msg: string, plan?: PlanPreviewStep[] | null): void {
  if (!humanGate) return;
  if (humanGateMsg) humanGateMsg.textContent = msg || "Sign-in or authentication required to continue.";
  // Task 12: plan preview — the whole validated plan, in plain
  // language, before the operator decides. Each line is rendered
  // with textContent (never innerHTML): a target name may echo page
  // text, and page text is untrusted.
  if (planPreview && planPreviewList && planPreviewTitleEl) {
    if (Array.isArray(plan) && plan.length > 0) {
      planPreviewTitleEl.textContent = planPreviewTitle(plan);
      planPreviewList.replaceChildren();
      for (const step of plan) {
        const li = document.createElement("li");
        li.textContent = formatPlanStep(step);
        planPreviewList.appendChild(li);
      }
      planPreview.hidden = false;
    } else {
      planPreview.hidden = true;
    }
  }
  humanGate.hidden = false;
  humanGate.scrollIntoView({ behavior: "smooth", block: "center" });
}

function hideHumanGate(): void {
  if (humanGate) humanGate.hidden = true;
  if (planPreview) planPreview.hidden = true;
}

// Task 12: task timeline — one row per executed step from the Task 11
// loop timings joined to the checkpoint history (status). Timings
// carry action types and durations only, so every cell is text.
function renderTaskTimeline(t: {
  timings?: { steps?: Array<{ step: number; action: string; batched: boolean; cache: string; checker_skipped: boolean; total_ms: number; overhead_ms: number }>; overhead_ms?: number } | null;
  steps?: Array<{ stepNum: number; message?: string; status?: string; result_ok?: boolean }> | null;
}): void {
  if (!taskTimelineCard || !taskTimelineList || !timelineSummaryEl) return;
  const rows = buildTimelineRows(t.timings?.steps, t.steps);
  if (rows.length === 0) {
    taskTimelineCard.hidden = true;
    return;
  }
  timelineSummaryEl.textContent = timelineSummary(rows, t.timings?.overhead_ms);
  taskTimelineList.replaceChildren();
  for (const row of rows) {
    const li = document.createElement("li");
    li.className = "task-timeline__row";
    const step = document.createElement("span");
    step.className = "task-timeline__step";
    step.textContent = `#${row.step}`;
    const action = document.createElement("span");
    action.className = "task-timeline__action";
    action.textContent = row.action;
    const status = document.createElement("span");
    status.className = `task-timeline__status task-timeline__status--${row.status}`;
    status.textContent = row.status;
    const dur = document.createElement("span");
    dur.className = "task-timeline__duration";
    dur.textContent = formatDuration(row.durationMs);
    const flags = document.createElement("span");
    flags.className = "task-timeline__flags";
    flags.textContent = [
      row.batched ? "batched" : null,
      row.cache !== "n/a" ? `cache ${row.cache}` : null,
      row.checkerSkipped ? "checker skipped" : null,
    ].filter(Boolean).join(" · ") || "—";
    li.append(step, action, status, dur, flags);
    taskTimelineList.appendChild(li);
  }
  taskTimelineCard.hidden = false;
}

// ----------------------------------------------------
// Takeover (Task 4)
// ----------------------------------------------------
// While takeover is active the agent is paused and observes nothing:
// no screenshots, no DOM snapshots, no page text to the planner. The
// banner tells the operator what to do in the tab and offers Resume /
// Cancel. The reason and URL shown here come from the backend's
// takeover record (kind/reason/url only — never a secret).
function showTakeover(takeover: { kind?: string; reason?: string; url?: string } | null | undefined): void {
  if (!takeoverBanner) return;
  const kind = takeover?.kind || "login";
  const reason = takeover?.reason || "A login / payment step needs you to complete it in the browser.";
  if (takeoverMsg) takeoverMsg.textContent = reason;
  if (takeoverMeta) {
    const where = takeover?.url ? ` · ${takeover.url}` : "";
    takeoverMeta.textContent = `Takeover: ${kind}${where} — the agent is not watching the page while you do this.`;
  }
  takeoverBanner.hidden = false;
  takeoverBanner.scrollIntoView({ behavior: "smooth", block: "center" });
}

function hideTakeover(): void {
  if (takeoverBanner) takeoverBanner.hidden = true;
}

// ----------------------------------------------------
// Multi-Turn Chat & Session Isolation Management
// ----------------------------------------------------

async function loadChatList(options?: { autoSelect?: boolean }): Promise<void> {
  if (!chatHistoryList) return;
  try {
    const res = await apiFetch(`${API}/api/chats`);
    if (!res.ok) return;
    const chats = (await res.json()) as ChatSummary[];

    if (!Array.isArray(chats) || chats.length === 0) {
      chatHistoryList.innerHTML = `<li class="chat-history-empty">No saved missions yet</li>`;
      return;
    }

    chatHistoryList.innerHTML = "";
    chats.forEach(chat => {
      const li = document.createElement("li");
      li.className = `chat-history-item${chat.id === activeChatId ? " is-active" : ""}`;
      li.setAttribute("data-chat-id", chat.id);

      const dateObj = new Date(chat.updated_at || chat.created_at);
      const dateLabel = isNaN(dateObj.getTime())
        ? "Recent"
        : dateObj.toLocaleDateString(undefined, { month: "short", day: "numeric" });

      // The × on the OPEN mission is a Close, not a Delete: it returns
      // the workspace to a blank New Mission and the mission stays in
      // this list, so it can be clicked open again. (Permanent delete
      // lives in the control center, behind a confirmation dialog.)
      // Only the active item shows it — a mission that is not open has
      // nothing to close. Inlined (not a pre-built string) so the
      // innerHTML guard can see there is no unescaped value here.
      li.innerHTML = `
        <div class="chat-history-item__main">
          <span class="chat-history-item__title" title="${escapeHtml(chat.title)}">${escapeHtml(chat.title || "Untitled Mission")}</span>
          <span class="chat-history-item__meta">${escapeHtml(dateLabel)}</span>
        </div>
        ${chat.id === activeChatId ? `<button class="chat-close-btn" title="Close mission" aria-label="Close mission">×</button>` : ""}
      `;

      li.addEventListener("click", () => {
        if (activeChatId !== chat.id) {
          selectChat(chat.id);
        }
      });

      const closeBtn = li.querySelector(".chat-close-btn") as HTMLButtonElement | null;
      closeBtn?.addEventListener("click", ev => {
        ev.stopPropagation();
        closeChat(chat.id);
      });

      chatHistoryList?.appendChild(li);
    });

    // Auto-select only when asked: opening the workspace (mount) and
    // closing a mission must stay on the blank New Mission state, not
    // instantly re-open a mission. Callers that need a selection pass
    // autoSelect explicitly; the default stays true only for legacy
    // refresh paths where an active chat is already set (the guard
    // above makes the block a no-op there anyway).
    if (!activeChatId && (options?.autoSelect ?? true)) {
      const savedChatId = localStorage.getItem("privagent_active_chat_id");
      if (savedChatId && chats.some(c => c.id === savedChatId)) {
        void selectChat(savedChatId);
      } else if (chats.length > 0) {
        void selectChat(chats[0].id);
      }
    }
  } catch {
    // Backend may still be spinning up
  }
}

async function selectChat(chatId: string): Promise<void> {
  const gen = ++selectChatGeneration;
  activeChatId = chatId;
  try {
    localStorage.setItem("privagent_active_chat_id", chatId);
  } catch {
    // Storage might be restricted
  }
  currentTaskId = null;
  stepCounter = 0;
  hideHumanGate();
  if (btnCancelMission) btnCancelMission.hidden = true;
  setButtonWorking(false);

  // Update active state in sidebar list
  if (chatHistoryList) {
    chatHistoryList.querySelectorAll(".chat-history-item").forEach(el => {
      const elId = el.getAttribute("data-chat-id");
      if (elId === chatId) {
        el.classList.add("is-active");
      } else {
        el.classList.remove("is-active");
      }
    });
  }

  // Clear feed and load history
  if (stepsList) stepsList.innerHTML = "";
  if (resultCard) resultCard.hidden = true;
  if (artifactsCard) artifactsCard.hidden = true;
  // Switching chats must not leave the previous chat's screenshot in
  // the Visual Perception frame; the detail fetch below re-shows the
  // new chat's active task image if it has one.
  updateVisualFrame(null, "", "ready");
  loadArtifacts(null, chatId, () => gen === selectChatGeneration);
  if (stepsCount) stepsCount.textContent = "0 steps executed";
  renderStepBudget();
  setTaskState("ready");

  if (missionInput) {
    missionInput.value = "";
    missionInput.placeholder = "Ask a follow-up directive for this mission...";
  }

  try {
    const res = await apiFetch(`${API}/api/chat/${chatId}`);
    if (!res.ok) return;
    const detail = (await res.json()) as ChatDetail;
    // A newer selection superseded this one while the fetch was in
    // flight: render nothing from the stale detail.
    if (gen !== selectChatGeneration) return;
    // /api/chat/{id} returns a FLAT shape (chat fields + messages + memory),
    // not {chat: {...}, messages}. Read status defensively from either form.
    const chatStatus = (detail as unknown as { status?: string }).status
      ?? detail.chat?.status
      ?? "";

    if (!detail.messages || detail.messages.length === 0) {
      if (stepsList) {
        stepsList.innerHTML = '<li class="agent-step-placeholder">No messages in this mission thread yet.</li>';
      }
      return;
    }

    let lastAgentMsg: string | null = null;
    let lastUserMsg = "";
    let finalTaskState = chatStatus === "failed" || chatStatus === "cancelled" ? chatStatus : "completed";
    let calculatedSteps = 0;

    const lastMsg = detail.messages[detail.messages.length - 1];
    const isPendingUserTurn = lastMsg && lastMsg.role === "user";

    detail.messages.forEach(msg => {
      if (msg.role === "user") {
        lastUserMsg = msg.content;
        addUserMessageItem(msg.content);
      } else if (msg.role === "agent") {
        lastAgentMsg = msg.content;
        const ev = (msg.evidence || "").toLowerCase();
        const content = (msg.content || "").toLowerCase();
        let msgState = "completed";
        if (ev.includes("failed") || ev.includes("unsupported") || content.startsWith("error:") || content.includes("planner unavailable")) {
          msgState = "failed";
          finalTaskState = "failed";
        } else if (ev.includes("blocked") || content.includes("blocked")) {
          msgState = "blocked";
          finalTaskState = "failed";
        } else if (ev.includes("partially") || content.includes("partially completed")) {
          msgState = "failed";
          finalTaskState = "failed";
        }

        // Rehydrate intermediate execution steps for this turn if present
        if (Array.isArray(msg.steps) && msg.steps.length > 0) {
          msg.steps.forEach(st => {
            if (st && st.message) {
              addStepItem(st.message, st.stepNum);
              if (typeof st.stepNum === "number") {
                calculatedSteps = Math.max(calculatedSteps, st.stepNum);
              }
            }
          });
        }

        addCompletionStepItem(msgState, msg.content);
        if (msg.evidence) {
          const urlMatch = msg.evidence.match(/https?:\/\/[^\s"'<>)]+/);
          const safeUrl = urlMatch ? safeHttpUrl(urlMatch[0]) : null;
          if (safeUrl && inspectorUrl) {
            inspectorUrl.href = safeUrl;
            inspectorUrl.textContent = safeUrl;
          }
          if (safeUrl && browserFrameUrlText) {
            browserFrameUrlText.textContent = safeUrl;
          }
          if (inspectorEvidence) inspectorEvidence.textContent = msg.evidence;
        }
      }
    });

    const totalSteps = typeof detail.total_steps === "number" && detail.total_steps > 0
      ? detail.total_steps
      : calculatedSteps;
    if (totalSteps > 0) {
      updateStepsCount(totalSteps);
    }

    // Check if there is an active in-flight task for this chat to reconnect to
    if (detail.active_task && ["planning", "running", "awaiting_confirmation", "awaiting_takeover"].includes(detail.active_task.taskState)) {
      const act = detail.active_task;
      currentTaskId = act.taskId;
      setButtonWorking(true);
      if (btnCancelMission) btnCancelMission.hidden = false;
      setTaskState(act.taskState);
      startFallbackPolling(currentTaskId);
      // Unconditional: reconnecting to a screenshot-less task must not
      // keep another chat's image in the frame.
      updateVisualFrame(act.screenshot ?? null, act.message || "", act.taskState);
      // Rehydrate in-flight execution steps executed before tab was re-opened
      if (Array.isArray(act.steps) && act.steps.length > 0) {
        act.steps.forEach(st => {
          if (st && st.message) {
            addStepItem(st.message, st.stepNum);
          }
        });
      }
      if (act.message && !Array.from(stepsList?.children || []).some(n => (n as HTMLElement).dataset.eventKey?.endsWith(act.message))) {
        addStepItem(act.message, act.stepsDone);
      }
      if (typeof act.stepsDone === "number" && act.stepsDone > 0) {
        updateStepsCount(act.stepsDone);
      }
      if (resultCard) resultCard.hidden = true;
      if (act.taskState === "awaiting_confirmation") {
        apiFetch(`${API}/api/task/${act.taskId}`)
          .then(r => r.json())
          .then(t => {
            const m = t.pending_confirmation?.message ?? "Authentication / Sign-in required";
            showHumanGate(m, t.pending_confirmation?.plan ?? null);
          })
          .catch(() => showHumanGate("Authentication / Sign-in required"));
      }
      if (act.taskState === "awaiting_takeover") {
        if (act.takeover) {
          showTakeover(act.takeover);
        } else {
          apiFetch(`${API}/api/task/${act.taskId}/takeover`)
            .then(r => r.json())
            .then(t => showTakeover(t.takeover))
            .catch(() => showTakeover(null));
        }
      }
    } else if (!isPendingUserTurn && lastAgentMsg) {
      activeInstruction = lastUserMsg;
      renderCompletion(finalTaskState, lastAgentMsg, false);
      setTaskState(finalTaskState);
    } else if (isPendingUserTurn) {
      setTaskState("ready");
      if (resultCard) resultCard.hidden = true;
    }
  } catch (err) {
    if (gen !== selectChatGeneration) return;
    addStepItem(`⚠️ Failed to load chat history: ${err}`);
  }
}

async function closeChat(chatId: string): Promise<void> {
  // Close is a deselect, never a delete: the mission stays in the
  // backend and in this sidebar list, so it can be opened again by
  // clicking it. Only the open mission can be closed.
  if (activeChatId !== chatId) return;
  createNewChat();
  // No auto-select: closing the active chat lands on the blank New
  // Mission state instead of instantly re-opening another mission.
  // The re-render also drops the × from the just-closed item.
  await loadChatList({ autoSelect: false });
}

function createNewChat(): void {
  try {
    localStorage.removeItem("privagent_active_chat_id");
  } catch {
    // Storage restricted
  }
  stopFallbackPolling();
  clearStalledNotice();
  activeChatId = null;
  currentTaskId = null;
  renderedTerminalTaskId = null;
  cancelRequested = false;
  stepCounter = 0;
  activeInstruction = "";
  hideHumanGate();

  if (missionInput) {
    missionInput.value = "";
    missionInput.placeholder = "e.g. Buy me a yellow dress from amazon (proceed until checkout)";
  }
  setButtonWorking(false);
  if (btnCancelMission) btnCancelMission.hidden = true;

  if (stepsList) {
    stepsList.innerHTML = '<li class="agent-step-placeholder">Submit a mission directive to begin autonomous browser execution.</li>';
  }
  if (resultCard) resultCard.hidden = true;
  // A new chat must not keep the previous chat's deliverables: the
  // artifacts card is only re-populated by loadArtifacts() for the chat
  // that is actually selected, and a fresh chat has none yet.
  if (artifactsCard) artifactsCard.hidden = true;
  if (artifactsList) artifactsList.innerHTML = "";
  if (artifactsCount) artifactsCount.textContent = "0 artifacts";
  if (stepsCount) stepsCount.textContent = "0 steps executed";
  renderStepBudget();
  if (telPlannerLatency) telPlannerLatency.textContent = "—";

  if (inspectorScreenshot) {
    inspectorScreenshot.hidden = true;
    inspectorScreenshot.removeAttribute("src");
  }
  if (inspectorScreenshotPlaceholder) inspectorScreenshotPlaceholder.hidden = false;
  if (inspectorUrl) {
    inspectorUrl.removeAttribute("href");
    inspectorUrl.textContent = "Waiting for task...";
  }
  if (inspectorTitle) inspectorTitle.textContent = "—";
  if (inspectorEvidence) inspectorEvidence.textContent = "Ready";
  if (obstacleBadge) {
    obstacleBadge.className = "obstacle-badge obstacle-badge--clear";
    obstacleBadge.textContent = "✓ Workflow Clear";
  }

  if (chatHistoryList) {
    chatHistoryList.querySelectorAll(".chat-history-item").forEach(el => el.classList.remove("is-active"));
  }

  setTaskState("ready");
}

// ----------------------------------------------------
// UI Events — static header + document-level handlers.
// The header buttons and the delegated document handler are wired at
// module load (their targets exist from the first paint). Every listener
// that touches a workspace node lives in initWorkspaceElements() above
// and is wired once, when the workspace template is first mounted.
// ----------------------------------------------------

btnToggleSidebarHeader?.addEventListener("click", () => {
  agentWorkspace?.classList.toggle("sidebar-collapsed");
});

btnNewSession?.addEventListener("click", () => {
  // From any Control Center view the workspace template is unmounted,
  // so createNewChat() alone is a visible no-op: every node it touches
  // is null. Mount first (synchronous), navigate to the workspace
  // view, then start the new chat against the live nodes.
  ensureWorkspaceMounted();
  if (location.hash !== "#agent-workspace") location.hash = "agent-workspace";
  createNewChat();
  missionInput?.focus();
});

// Delegated click handler for Deliverable Export & Suggested Next Moves
document.addEventListener("click", event => {
  const target = event.target as Element;
  const excelBtn = target.closest<HTMLButtonElement>(".btn-download-excel");
  if (excelBtn && excelBtn.dataset.csv) {
    try {
      const csvData = decodeURIComponent(excelBtn.dataset.csv);
      const blob = new Blob([csvData], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.setAttribute("href", url);
      link.setAttribute("download", `PrivAgent_Price_Comparison_${new Date().toISOString().slice(0, 10)}.csv`);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch {
      /* ignore download errors */
    }
    return;
  }

  const chipBtn = target.closest<HTMLButtonElement>(".suggested-chip");
  if (chipBtn && chipBtn.dataset.prompt && missionInput) {
    missionInput.value = chipBtn.dataset.prompt;
    missionInput.focus();
    missionInput.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return;
  }

  // Expandable step rows: the toggle button opens/closes the step's
  // detail panel (step number, full message, Copy). The Copy button
  // copies the step text to the clipboard.
  const stepToggle = target.closest<HTMLButtonElement>(".agent-step-item__toggle");
  if (stepToggle) {
    const item = stepToggle.closest<HTMLElement>(".agent-step-item");
    const detail = item?.querySelector<HTMLElement>(".agent-step-item__detail");
    if (detail) {
      const expanded = stepToggle.getAttribute("aria-expanded") === "true";
      stepToggle.setAttribute("aria-expanded", String(!expanded));
      detail.hidden = expanded;
    }
    return;
  }
  const copyBtn = target.closest<HTMLButtonElement>("[data-copy-step]");
  if (copyBtn) {
    const item = copyBtn.closest<HTMLElement>(".agent-step-item");
    const text = item?.querySelector<HTMLElement>(".agent-step-item__detail-text")?.textContent || "";
    const done = () => { copyBtn.textContent = "Copied ✓"; setTimeout(() => { copyBtn.textContent = "Copy step"; }, 1500); };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => { copyBtn.textContent = "Copy failed"; });
    } else {
      copyBtn.textContent = "Copy unavailable";
    }
    return;
  }

  // Interactive watch card (chat-only monitoring): Pause/Resume,
  // Cancel, and View changes call the control missions API and
  // update the card in place. Cancel is a two-step inline confirm
  // (no window.confirm) so a stray click cannot delete a watch.
  const watchCard = target.closest<HTMLElement>(".watch-card");
  if (watchCard?.dataset.watchId) {
    const missionId = watchCard.dataset.watchId;
    const statusEl = watchCard.querySelector<HTMLElement>("[data-watch-status]");
    const pauseBtn = watchCard.querySelector<HTMLButtonElement>("[data-watch-pause]");
    const cancelBtn = watchCard.querySelector<HTMLButtonElement>("[data-watch-cancel]");
    const eventsEl = watchCard.querySelector<HTMLElement>(".watch-card__events");
    const setBusy = (busy: boolean) => {
      watchCard.querySelectorAll<HTMLButtonElement>(".watch-card__btn").forEach(b => { b.disabled = busy; });
    };
    if (target.closest<HTMLButtonElement>("[data-watch-pause]") && pauseBtn && statusEl) {
      const resume = pauseBtn.textContent === "Resume";
      setBusy(true);
      apiFetch(`${API}/api/control/missions/${encodeURIComponent(missionId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: resume ? "active" : "paused" }),
      }).then(async res => {
        if (!res.ok) throw new Error("Backend returned " + res.status);
        const next = resume ? "active" : "paused";
        statusEl.textContent = next === "active" ? "Active" : "Paused";
        statusEl.dataset.status = next;
        pauseBtn.textContent = next === "active" ? "Pause" : "Resume";
      }).catch(() => {
        statusEl.textContent = "Update failed — try again";
      }).finally(() => setBusy(false));
      return;
    }
    if (target.closest<HTMLButtonElement>("[data-watch-cancel]") && cancelBtn) {
      if (cancelBtn.dataset.confirm !== "true") {
        cancelBtn.dataset.confirm = "true";
        cancelBtn.textContent = "Confirm cancel?";
        setTimeout(() => {
          if (cancelBtn.dataset.confirm === "true") {
            cancelBtn.dataset.confirm = "";
            cancelBtn.textContent = "Cancel watch";
          }
        }, 4000);
        return;
      }
      setBusy(true);
      apiFetch(`${API}/api/control/missions/${encodeURIComponent(missionId)}`, { method: "DELETE" })
        .then(async res => {
          if (!res.ok) throw new Error("Backend returned " + res.status);
          watchCard.querySelector<HTMLElement>(".watch-card__title")!.textContent = "Watch cancelled";
          if (statusEl) statusEl.textContent = "Cancelled";
          watchCard.querySelector<HTMLElement>(".watch-card__note")!.textContent = "This watch is stopped and removed. Ask me in chat to watch something again.";
          watchCard.querySelectorAll<HTMLButtonElement>(".watch-card__btn").forEach(b => { b.disabled = true; });
          if (eventsEl) eventsEl.hidden = true;
        })
        .catch(() => {
          cancelBtn.dataset.confirm = "";
          cancelBtn.textContent = "Cancel failed — try again";
        })
        .finally(() => setBusy(false));
      return;
    }
    if (target.closest<HTMLButtonElement>("[data-watch-events]") && eventsEl) {
      if (!eventsEl.hidden) {
        eventsEl.hidden = true;
        return;
      }
      eventsEl.hidden = false;
      eventsEl.innerHTML = `<p class="watch-card__events-note">Loading changes…</p>`;
      apiFetch(`${API}/api/control/missions/${encodeURIComponent(missionId)}/events`)
        .then(async res => {
          if (!res.ok) throw new Error("Backend returned " + res.status);
          const events = (await res.json()) as Array<{ title?: string; summary?: string; detected_at?: number }>;
          eventsEl.innerHTML = events.length
            ? events.slice(0, 10).map(e => `<article class="watch-card__event"><h4>${escapeHtml(e.title || "Change detected")}</h4><p>${escapeHtml(e.summary || "")}</p><time>${e.detected_at ? new Date(e.detected_at * 1000).toLocaleString() : ""}</time></article>`).join("")
            : `<p class="watch-card__events-note">No changes recorded yet — the next observed difference will appear here.</p>`;
        })
        .catch(() => {
          eventsEl.innerHTML = `<p class="watch-card__events-note">Could not load changes.</p>`;
        });
      return;
    }
  }
});







// ----------------------------------------------------
// WebSocket Client
// ----------------------------------------------------

function connectWebSocket(): void {
  const ws = new WebSocket(wsUrl(`ws://localhost:8000/ws/client`));

  ws.onopen = () => {
    setConnectionStatus(true, "Online");
    if (currentTaskId && isWorking) {
      syncActiveTask(currentTaskId);
    }
  };

  ws.onmessage = ev => {
    try {
      const env = JSON.parse(ev.data) as {
        type: string;
        taskId: string;
        payload: {
          chatId?: string | null;
          taskState: string;
          stepsDone: number;
          message: string;
          screenshot?: string | null;
          plannerMs?: number | null;
          observeMs?: number | null;
          actMs?: number | null;
          intent?: string;
          isBrowserTask?: boolean;
          sessionId?: string | null;
          primaryTabId?: number | null;
          childTabIds?: number[] | null;
          // Task 4: the active takeover request, if any.
          takeover?: { kind?: string; reason?: string; url?: string } | null;
        };
      };
      if (env.type !== "PROGRESS") return;
      if (!currentTaskId || env.taskId !== currentTaskId) {
        if (activeChatId && env.payload.chatId === activeChatId) {
          currentTaskId = env.taskId;
          setButtonWorking(true);
          if (btnCancelMission) btnCancelMission.hidden = false;
          startFallbackPolling(currentTaskId);
        } else {
          return;
        }
      }
      const p = env.payload;

      lastProgressTime = Date.now();
      clearStalledNotice();

      if (missionSubtext && !missionSubtext.innerHTML.includes("Progress is taking longer")) {
        if (p.isBrowserTask === false || p.intent === "NON_BROWSER") {
          missionSubtext.innerHTML = `<span style="color: var(--accent); font-weight: 500;">💬 Mode: Conversational (Zero Browser Actions)</span>`;
        } else if (p.sessionId) {
          const tabInfo = p.primaryTabId ? ` · Tab #${p.primaryTabId}` : "";
          const childInfo = p.childTabIds && p.childTabIds.length > 0 ? ` (+${p.childTabIds.length} child tabs)` : "";
          missionSubtext.innerHTML = `<span style="color: var(--text-secondary); font-weight: 500;">🌐 Session: ${escapeHtml(p.sessionId.slice(0, 8))}${tabInfo}${childInfo}</span>`;
        }
        // Task 14: routing decision — see the polling path above.
        const routeText = taskRouteLabel((p as any).route);
        if (routeText) {
          missionSubtext.innerHTML += ` <span style="color: var(--privacy); font-weight: 500;">${escapeHtml(routeText)}</span>`;
        }
      }

      if (["completed", "failed", "cancelled"].includes(p.taskState)) {
        stopFallbackPolling();
        renderCompletion(p.taskState, p.message, true, p.stepsDone);
        loadChatList();
        return;
      }

      setTaskState(p.taskState);
      addStepItem(p.message, p.stepsDone);
      updateVisualFrame(p.screenshot, p.message, p.taskState);
      if (telPlannerLatency && typeof p.plannerMs === "number") {
        telPlannerLatency.textContent = `${(p.plannerMs / 1000).toFixed(1)}s`;
      }

      if (p.taskState === "awaiting_confirmation") {
        apiFetch(`${API}/api/task/${env.taskId}`)
          .then(r => r.json())
          .then(t => {
            const msg = t.pending_confirmation?.message ?? "Authentication / Sign-in required";
            showHumanGate(msg, t.pending_confirmation?.plan ?? null);
          });
      }

      if (p.taskState === "awaiting_takeover") {
        // The broadcast already carries the takeover record; fetch only
        // as a fallback (e.g. a reconnect that missed the first frame).
        if (p.takeover) {
          showTakeover(p.takeover);
        } else {
          apiFetch(`${API}/api/task/${env.taskId}/takeover`)
            .then(r => r.json())
            .then(t => showTakeover(t.takeover))
            .catch(() => showTakeover(null));
        }
      } else if (takeoverBanner && !takeoverBanner.hidden) {
        hideTakeover();
      }
    } catch {
      // non-JSON frame
    }
  };

  ws.onclose = () => {
    setConnectionStatus(false, "Reconnecting...");
    setTimeout(connectWebSocket, 2500);
  };

  ws.onerror = () => {
    setConnectionStatus(false, "Offline");
  };
}

// Initial Load
connectWebSocket();
// loadChatList() no-ops until the workspace mounts; initWorkspaceElements()
// re-runs it once the nodes exist.
loadChatList();
initLlmSettings();
initVaultSettings();

// ---------------------------------------------------------------------------
// Credential vault (Task 4) — /api/vault/credentials.
// Values are password-typed, sent once over the authenticated API, and
// never rendered back: the list endpoint returns metadata (domain /
// kind / label / placeholder) only, and the value field is cleared
// after every save. OTP / one-time-code kinds are rejected by the
// backend vault, not here.
// ---------------------------------------------------------------------------

interface VaultCredentialMeta {
  domain: string;
  kind: string;
  label: string;
  username?: string | null;
  placeholder: string;
}

async function refreshVaultList(): Promise<void> {
  const list = $("vaultList") as HTMLUListElement | null;
  if (!list) return;
  list.innerHTML = "";
  try {
    const res = await apiFetch(`${API}/api/vault/credentials`);
    if (!res.ok) return;
    const data = (await res.json()) as { credentials: VaultCredentialMeta[] };
    for (const cred of data.credentials || []) {
      const li = document.createElement("li");
      li.textContent = `${cred.domain} · ${cred.kind} · ${cred.label} — ${cred.placeholder}`;
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn--sm btn--ghost";
      del.textContent = "Delete";
      del.addEventListener("click", async () => {
        await apiFetch(
          `${API}/api/vault/credentials/${encodeURIComponent(cred.domain)}/${encodeURIComponent(cred.kind)}/${encodeURIComponent(cred.label)}`,
          { method: "DELETE" },
        );
        await refreshVaultList();
      });
      li.appendChild(del);
      list.appendChild(li);
    }
    if (list.children.length === 0) {
      const li = document.createElement("li");
      li.textContent = "No saved credentials yet.";
      list.appendChild(li);
    }
  } catch {
    // Offline: leave the list empty rather than showing stale data.
  }
}

async function saveVaultCredential(): Promise<void> {
  const status = $("vaultSaveStatus");
  const domain = ($("vaultDomain") as HTMLInputElement | null)?.value.trim() || "";
  const kind = ($("vaultKind") as HTMLSelectElement | null)?.value || "login";
  const label = ($("vaultLabel") as HTMLInputElement | null)?.value.trim() || "";
  const username = ($("vaultUsername") as HTMLInputElement | null)?.value.trim() || "";
  const valueInput = $("vaultValue") as HTMLInputElement | null;
  const value = valueInput?.value || "";
  if (!domain || !label || !value) {
    if (status) status.textContent = "Domain, label, and the secret are all required.";
    return;
  }
  if (status) status.textContent = "Saving…";
  try {
    const res = await apiFetch(`${API}/api/vault/credentials`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ domain, kind, label, username: username || null, value }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => null) as { detail?: string } | null;
      if (status) status.textContent = detail?.detail || `Could not save (backend ${res.status}).`;
      return;
    }
    if (status) status.textContent = "Saved — the value is encrypted and will never be shown again.";
    // The secret must not linger in the page: clear the field at once.
    if (valueInput) valueInput.value = "";
    await refreshVaultList();
  } catch {
    if (status) status.textContent = "Backend offline — could not save.";
  }
}

function initVaultSettings(): void {
  const overlay = $("vaultOverlay");
  const openBtn = $("btnVaultSettings");
  const closeBtn = $("btnVaultClose");
  if (!overlay || !openBtn || !closeBtn) return;

  openBtn.addEventListener("click", () => { overlay.hidden = false; void refreshVaultList(); });
  closeBtn.addEventListener("click", () => { overlay.hidden = true; });
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.hidden = true; });
  $("btnVaultSave")?.addEventListener("click", () => { void saveVaultCredential(); });
}

initMemorySettings();

// ---------------------------------------------------------------------------
// Long-term memory (Task 9) — /api/memory/long/*.
// Facts / skills / files, all operator-owned: the list endpoints return
// metadata and text the operator wrote (never secrets — the backend
// rejects credentials and PII at write time), and file listings never
// include file content. All rendering uses textContent, never innerHTML.
// Proposals from a finished task appear only after the operator opens
// this dialog; accepting one stores it as a confirmed fact/skill.
// ---------------------------------------------------------------------------

interface MemoryFact { id: string; text: string; category: string; source: string; }
interface MemorySkill { id: string; name: string; version: number; success_count: number; }
interface MemoryFileMeta { id: string; file_name: string; summary: string; size_bytes: number; }
interface MemoryProposal { kind: string; text?: string; name?: string; steps?: string; }

function memoryListItem(label: string, onDelete: () => void): HTMLLIElement {
  const li = document.createElement("li");
  li.textContent = label;
  const del = document.createElement("button");
  del.type = "button";
  del.className = "btn btn--sm btn--ghost";
  del.textContent = "Delete";
  del.addEventListener("click", () => { void onDelete(); });
  li.appendChild(del);
  return li;
}

async function refreshMemoryLists(): Promise<void> {
  const factsList = $("memoryFactsList") as HTMLUListElement | null;
  const skillsList = $("memorySkillsList") as HTMLUListElement | null;
  const filesList = $("memoryFilesList") as HTMLUListElement | null;
  const proposalsSection = $("memoryProposalsSection");
  const proposalsList = $("memoryProposalsList") as HTMLUListElement | null;
  try {
    const [factsRes, skillsRes, filesRes] = await Promise.all([
      apiFetch(`${API}/api/memory/long/facts`),
      apiFetch(`${API}/api/memory/long/skills`),
      apiFetch(`${API}/api/memory/long/files`),
    ]);
    if (factsList) {
      factsList.innerHTML = "";
      if (factsRes.ok) {
        const data = (await factsRes.json()) as { facts: MemoryFact[] };
        for (const fact of data.facts || []) {
          factsList.appendChild(memoryListItem(
            `${fact.text} — ${fact.category} · ${fact.source}`,
            async () => {
              await apiFetch(`${API}/api/memory/long/facts/${encodeURIComponent(fact.id)}`, { method: "DELETE" });
              await refreshMemoryLists();
            },
          ));
        }
        if (factsList.children.length === 0) {
          const li = document.createElement("li");
          li.textContent = "No saved facts yet.";
          factsList.appendChild(li);
        }
      }
    }
    if (skillsList) {
      skillsList.innerHTML = "";
      if (skillsRes.ok) {
        const data = (await skillsRes.json()) as { skills: MemorySkill[] };
        for (const skill of data.skills || []) {
          skillsList.appendChild(memoryListItem(
            `${skill.name} — v${skill.version} · ${skill.success_count} success${skill.success_count === 1 ? "" : "es"}`,
            async () => {
              await apiFetch(`${API}/api/memory/long/skills/${encodeURIComponent(skill.id)}`, { method: "DELETE" });
              await refreshMemoryLists();
            },
          ));
        }
        if (skillsList.children.length === 0) {
          const li = document.createElement("li");
          li.textContent = "No learned skills yet.";
          skillsList.appendChild(li);
        }
      }
    }
    if (filesList) {
      filesList.innerHTML = "";
      if (filesRes.ok) {
        const data = (await filesRes.json()) as { files: MemoryFileMeta[] };
        for (const file of data.files || []) {
          filesList.appendChild(memoryListItem(
            `${file.file_name} — ${file.summary} (${Math.round(file.size_bytes / 1024)} KB)`,
            async () => {
              await apiFetch(`${API}/api/memory/long/files/${encodeURIComponent(file.id)}`, { method: "DELETE" });
              await refreshMemoryLists();
            },
          ));
        }
        if (filesList.children.length === 0) {
          const li = document.createElement("li");
          li.textContent = "No reference files yet.";
          filesList.appendChild(li);
        }
      }
    }
    // Pending proposals from the current task's checkpoint, if any.
    if (proposalsSection && proposalsList) {
      proposalsList.innerHTML = "";
      proposalsSection.hidden = true;
      if (currentTaskId) {
        const taskRes = await apiFetch(`${API}/api/task/${encodeURIComponent(currentTaskId)}`);
        if (taskRes.ok) {
          const task = (await taskRes.json()) as { checkpoint?: { proposals?: MemoryProposal[] } | null };
          const proposals = task.checkpoint?.proposals || [];
          if (proposals.length > 0) {
            proposalsSection.hidden = false;
            proposals.forEach((proposal, index) => {
              const label = proposal.kind === "fact"
                ? `Fact: ${proposal.text || ""}`
                : `Skill: ${proposal.name || ""}`;
              const li = document.createElement("li");
              li.textContent = label;
              const accept = document.createElement("button");
              accept.type = "button";
              accept.className = "btn btn--sm btn--primary";
              accept.textContent = "Remember";
              accept.addEventListener("click", async () => {
                // REVIEW (Task 9 audit): this used to send
                // {accepted_indices: [index]} while the backend only
                // reads "accepted" — pydantic silently dropped the
                // unknown field, so "Remember" did nothing at all.
                await apiFetch(`${API}/api/memory/long/proposals/${encodeURIComponent(currentTaskId as string)}/confirm`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ accepted: [index] }),
                });
                await refreshMemoryLists();
              });
              li.appendChild(accept);
              proposalsList.appendChild(li);
            });
          }
        }
      }
    }
  } catch {
    // Offline: leave the lists empty rather than showing stale data.
  }
}

async function saveMemoryFact(): Promise<void> {
  const status = $("memoryFactStatus");
  const text = ($("memoryFactText") as HTMLInputElement | null)?.value.trim() || "";
  const category = ($("memoryFactCategory") as HTMLSelectElement | null)?.value || "general";
  if (!text) {
    if (status) status.textContent = "Write the fact first.";
    return;
  }
  if (status) status.textContent = "Saving…";
  try {
    const res = await apiFetch(`${API}/api/memory/long/facts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, category }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => null) as { detail?: string } | null;
      if (status) status.textContent = detail?.detail || `Could not save (backend ${res.status}).`;
      return;
    }
    if (status) status.textContent = "Saved.";
    const input = $("memoryFactText") as HTMLInputElement | null;
    if (input) input.value = "";
    await refreshMemoryLists();
  } catch {
    if (status) status.textContent = "Backend offline — could not save.";
  }
}

async function uploadMemoryFile(): Promise<void> {
  const status = $("memoryFileStatus");
  const input = $("memoryFileInput") as HTMLInputElement | null;
  const file = input?.files?.[0];
  const summary = ($("memoryFileSummary") as HTMLInputElement | null)?.value.trim() || "";
  if (!file) {
    if (status) status.textContent = "Choose a file first.";
    return;
  }
  if (status) status.textContent = "Uploading…";
  try {
    const base64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || "");
        resolve(result.includes(",") ? result.split(",").pop() || "" : result);
      };
      reader.onerror = () => reject(new Error("could not read file"));
      reader.readAsDataURL(file);
    });
    const res = await apiFetch(`${API}/api/memory/long/files`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fileName: file.name, base64, summary, taskId: currentTaskId || "" }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => null) as { detail?: string } | null;
      if (status) status.textContent = detail?.detail || `Could not attach (backend ${res.status}).`;
      return;
    }
    if (status) status.textContent = "Attached — the planner will see the summary only.";
    if (input) input.value = "";
    const summaryInput = $("memoryFileSummary") as HTMLInputElement | null;
    if (summaryInput) summaryInput.value = "";
    await refreshMemoryLists();
  } catch {
    if (status) status.textContent = "Could not read or upload that file.";
  }
}

async function forgetAllMemory(): Promise<void> {
  const status = $("memoryForgetStatus");
  if (!window.confirm("Forget everything the agent remembers about you — all facts, skills, and reference files? This cannot be undone.")) return;
  try {
    const res = await apiFetch(`${API}/api/memory`, { method: "DELETE" });
    if (!res.ok) {
      if (status) status.textContent = `Could not forget (backend ${res.status}).`;
      return;
    }
    if (status) status.textContent = "Forgotten — all memory tiers are empty.";
    await refreshMemoryLists();
  } catch {
    if (status) status.textContent = "Backend offline — could not forget.";
  }
}

function initMemorySettings(): void {
  const overlay = $("memoryOverlay");
  const openBtn = $("btnMemorySettings");
  const closeBtn = $("btnMemoryClose");
  if (!overlay || !openBtn || !closeBtn) return;

  openBtn.addEventListener("click", () => { overlay.hidden = false; void refreshMemoryLists(); });
  closeBtn.addEventListener("click", () => { overlay.hidden = true; });
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.hidden = true; });
  $("btnMemorySaveFact")?.addEventListener("click", () => { void saveMemoryFact(); });
  $("btnMemoryUploadFile")?.addEventListener("click", () => { void uploadMemoryFile(); });
  $("btnMemoryForgetAll")?.addEventListener("click", () => { void forgetAllMemory(); });
}

// ---------------------------------------------------------------------------
// LLM provider settings — runtime provider/model selection via /api/llm/*.
// The API key is password-typed, sent only to the backend, and never echoed
// back: the config response carries keyConfigured (boolean) only.
// ---------------------------------------------------------------------------

const FREE_GATEWAY_MODELS: Record<string, string> = {
  "deepseek-v4.1-flash": "DeepSeek V4.1 Flash — Primary Reasoning & Coding (Default)",
  "glm-5.3-flash": "GLM 5.3 Flash — Coding & Agent Tasks",
  "qwen-3.8-flash": "Qwen 3.8 Flash — General & Information Extraction",
  "mimo-2.6-pro": "MiMo 2.6 Pro — Complex Reasoning & Analysis",
  "minimax-m3.1-flash": "MiniMax M3.1 Flash — Alternative Coding & Reasoning",
};

const DEFAULT_SEEDED_MODELS = Object.keys(FREE_GATEWAY_MODELS);

function formatModelLabel(id: string): string {
  return FREE_GATEWAY_MODELS[id] || id;
}

function $(id: string): HTMLElement | null {
  return document.getElementById(id);
}

interface LlmConfig {
  provider: string;
  baseUrl: string;
  keyConfigured: boolean;
  model: string | null;
}

async function initLlmSettings(): Promise<void> {
  const overlay = $("llmSettingsOverlay");
  const openBtn = $("btnLlmSettings");
  const closeBtn = $("btnLlmClose");
  if (!overlay || !openBtn || !closeBtn) return;

  openBtn.addEventListener("click", () => { overlay.hidden = false; void refreshLlmSettings(); });
  closeBtn.addEventListener("click", () => { overlay.hidden = true; });
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.hidden = true; });

  const providerSelect = $("llmProviderSelect") as HTMLSelectElement | null;
  providerSelect?.addEventListener("change", () => {
    const isGateway = providerSelect.value === "antseed" || providerSelect.value === "deepai";
    const fields = $("antseedFields");
    if (fields) fields.hidden = !isGateway;
    const base = $("llmBaseUrl") as HTMLInputElement | null;
    if (base && providerSelect.value === "deepai" && (!base.value || base.value.includes(":8377"))) {
      base.value = "http://127.0.0.1:8787/v1";
    }
  });
  wireModelModeToggle();
  $("btnLlmDiscover")?.addEventListener("click", () => { void discoverLlmModels(); });
  $("btnLlmSave")?.addEventListener("click", () => { void saveLlmSettings(); });

  await refreshLlmSettings();
}

async function refreshLlmSettings(): Promise<void> {
  try {
    const res = await apiFetch(`${API}/api/llm/config`);
    if (!res.ok) return;
    const cfg = (await res.json()) as LlmConfig;
    const providerSelect = $("llmProviderSelect") as HTMLSelectElement | null;
    if (providerSelect) {
      providerSelect.value = cfg.provider;
      const fields = $("antseedFields");
      if (fields) fields.hidden = cfg.provider !== "antseed" && cfg.provider !== "deepai";
    }
    const base = $("llmBaseUrl") as HTMLInputElement | null;
    if (base) base.value = cfg.baseUrl;
    const key = $("llmApiKey") as HTMLInputElement | null;
    if (key) {
      key.value = ""; // never render the credential
      key.placeholder = cfg.keyConfigured ? "A key is configured (enter to replace)" : "Enter your gateway API key";
    }
    const modelSel = $("llmModelSelect") as HTMLSelectElement | null;
    if (modelSel) {
      if (!modelSel.options.length) {
        DEFAULT_SEEDED_MODELS.forEach(m => modelSel.add(new Option(formatModelLabel(m), m)));
      }
      if (cfg.model) {
        let found = false;
        for (const opt of Array.from(modelSel.options)) {
          if (opt.value === cfg.model) { modelSel.value = cfg.model; found = true; break; }
        }
        if (!found) {
          modelSel.add(new Option(formatModelLabel(cfg.model), cfg.model));
          modelSel.value = cfg.model;
        }
        const custom = $("llmModelCustom") as HTMLInputElement | null;
        if (custom) custom.value = cfg.model;
      }
    }
    const label = $("llmSettingsLabel");
    if (label) {
      label.textContent = cfg.model ? `${cfg.provider} · ${cfg.model}` : cfg.provider;
    }
  } catch {
    /* backend offline: panel still opens with defaults */
  }
}

function wireModelModeToggle(): void {
  const btn = $("btnLlmModelMode");
  const sel = $("llmModelSelect") as HTMLSelectElement | null;
  const custom = $("llmModelCustom") as HTMLInputElement | null;
  if (!btn || !sel || !custom) return;
  let customMode = false;
  btn.addEventListener("click", () => {
    customMode = !customMode;
    sel.hidden = customMode;
    custom.hidden = !customMode;
    if (customMode) custom.value = sel.value;
  });
}

async function discoverLlmModels(): Promise<void> {
  const status = $("llmDiscoverStatus");
  const modelSel = $("llmModelSelect") as HTMLSelectElement | null;
  if (status) status.textContent = "Discovering…";
  const saved = await saveLlmSettings({ silentStatus: true });
  if (!saved) {
    if (status) status.textContent = "Could not save settings; fix the form and retry.";
    return;
  }
  try {
    const res = await apiFetch(`${API}/api/llm/models`);
    if (!res.ok) {
      if (status) status.textContent = `Discovery failed (HTTP ${res.status}). Manual entry still available.`;
      return;
    }
    const data = (await res.json()) as { discovered?: boolean; models?: string[]; reason?: string | null };
    const models = data.models ?? [];
    if (data.discovered && modelSel) {
      const keep = modelSel.value;
      modelSel.options.length = 0;
      models.forEach(m => modelSel.add(new Option(formatModelLabel(m), m)));
      if (keep && models.includes(keep)) modelSel.value = keep;
      if (status) status.textContent = `Discovered ${models.length} model${models.length === 1 ? "" : "s"}.`;
    } else if (status) {
      const reason = data.reason ? ` (${data.reason})` : "";
      status.textContent = `Discovery unavailable${reason}. Seeded list loaded; manual entry still available.`;
    }
  } catch {
    if (status) status.textContent = "Discovery request failed. Manual entry still available.";
  }
}

async function saveLlmSettings(opts: { silentStatus?: boolean } = {}): Promise<boolean> {
  const status = $("llmSaveStatus");
  const provider = ($("llmProviderSelect") as HTMLSelectElement | null)?.value;
  const baseEl = $("llmBaseUrl") as HTMLInputElement | null;
  const keyEl = $("llmApiKey") as HTMLInputElement | null;
  const sel = $("llmModelSelect") as HTMLSelectElement | null;
  const custom = $("llmModelCustom") as HTMLInputElement | null;
  if (!provider) return false;

  const body: Record<string, unknown> = { provider };
  if (provider === "antseed" || provider === "deepai") {
    const defaultUrl = provider === "deepai" ? "http://127.0.0.1:8787/v1" : "http://localhost:8377/v1";
    const baseUrl = baseEl?.value.trim() || defaultUrl;
    body.baseUrl = baseUrl;
    const modelId = (custom && !custom.hidden && custom.value.trim())
      ? custom.value.trim()
      : (sel?.value.trim() ?? "");
    if (modelId) body.model = modelId;
    // Only send the key when the operator typed one; the stored key persists.
    const apiKey = keyEl?.value ?? "";
    if (apiKey.trim()) body.apiKey = apiKey.trim();
  }
  try {
    const res = await apiFetch(`${API}/api/llm/config`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: "" }));
      const msg = typeof detail.detail === "string" ? `: ${detail.detail}` : "";
      if (status && !opts.silentStatus) status.textContent = `Apply failed${msg}`;
      return false;
    }
    await refreshLlmSettings();
    if (status && !opts.silentStatus) status.textContent = "Applied.";
    // Clear the password field post-save; backend holds it now.
    if (keyEl) keyEl.value = "";
    return true;
  } catch {
    if (status && !opts.silentStatus) status.textContent = "Could not reach the backend.";
    return false;
  }
}
