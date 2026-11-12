import "./styles/agent.css";
import { escapeHtml, linkify, linkLabel, safeHttpUrl, safeImageSrc } from "./sanitize";

const API = "http://localhost:8000";

// Workspace & Sidebar Elements
const agentWorkspace = document.getElementById("agentWorkspace") as HTMLElement | null;
const btnToggleSidebarHeader = document.getElementById("btnToggleSidebarHeader") as HTMLButtonElement | null;
const btnCollapseSidebar = document.getElementById("btnCollapseSidebar") as HTMLButtonElement | null;
const btnNewMissionSidebar = document.getElementById("btnNewMissionSidebar") as HTMLButtonElement | null;
const chatHistoryList = document.getElementById("chatHistoryList") as HTMLUListElement | null;

// Directive & Timeline Elements
const missionInput = document.getElementById("missionInput") as HTMLTextAreaElement | null;
const btnStartMission = document.getElementById("btnStartMission") as HTMLButtonElement | null;
const btnCancelMission = document.getElementById("btnCancelMission") as HTMLButtonElement | null;
const btnNewSession = document.getElementById("btnNewSession") as HTMLButtonElement | null;
const missionSubtext = document.getElementById("missionSubtext") as HTMLElement | null;
const stateBadge = document.getElementById("missionStateBadge") as HTMLElement | null;
const stepsList = document.getElementById("agentStepsList") as HTMLOListElement | null;
const stepsCount = document.getElementById("feedStepsCount") as HTMLElement | null;
const resultCard = document.getElementById("agentResultCard") as HTMLElement | null;
const resultContent = document.getElementById("resultContent") as HTMLElement | null;
const resultBadge = document.getElementById("resultBadge") as HTMLElement | null;
const resultTimestamp = document.getElementById("resultTimestamp") as HTMLElement | null;
const artifactsCard = document.getElementById("agentArtifactsCard") as HTMLElement | null;
const artifactsList = document.getElementById("agentArtifactsList") as HTMLElement | null;
const artifactsCount = document.getElementById("artifactsCount") as HTMLElement | null;

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

async function loadArtifacts(taskId?: string | null, chatId?: string | null): Promise<void> {
  if (!artifactsCard || !artifactsList) return;
  const targetUrl = taskId ? `${API}/api/tasks/${taskId}/artifacts` : (chatId ? `${API}/api/chats/${chatId}/artifacts` : null);
  if (!targetUrl) {
    artifactsCard.hidden = true;
    return;
  }

  try {
    const res = await fetch(targetUrl);
    if (!res.ok) {
      artifactsCard.hidden = true;
      return;
    }
    const list = (await res.json()) as ArtifactItem[];
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
      const fmt = (art.file_format || "file").toLowerCase();
      const badgeCls = `artifact-format-badge artifact-format-badge--${fmt}`;
      const sizeStr = formatBytes(art.file_size_bytes || 0);

      itemEl.innerHTML = `
        <div class="artifact-info">
          <span class="${badgeCls}">${escapeHtml(fmt.toUpperCase())}</span>
          <div class="artifact-details">
            <span class="artifact-filename" title="${escapeHtml(art.title || art.filename)}">${escapeHtml(art.title || art.filename)}</span>
            <span class="artifact-meta">${escapeHtml(art.filename)} · ${sizeStr}</span>
          </div>
        </div>
        <div class="artifact-actions">
          <a href="${API}/api/artifacts/${art.artifact_id}/preview" target="_blank" rel="noopener noreferrer" class="btn-artifact btn-artifact--preview">Preview</a>
          <a href="${API}/api/artifacts/${art.artifact_id}/download" class="btn-artifact btn-artifact--download" download>Download</a>
        </div>
      `;
      artifactsList.appendChild(itemEl);
    });
  } catch {
    artifactsCard.hidden = true;
  }
}

// Human Gate
const humanGate = document.getElementById("humanGate") as HTMLElement | null;
const humanGateMsg = document.getElementById("humanGateMsg") as HTMLElement | null;
const btnGateApprove = document.getElementById("btnGateApprove") as HTMLButtonElement | null;
const btnGateReject = document.getElementById("btnGateReject") as HTMLButtonElement | null;

// Inspector
const inspectorUrl = document.getElementById("inspectorUrl") as HTMLAnchorElement | null;
const inspectorTitle = document.getElementById("inspectorTitle") as HTMLElement | null;
const inspectorEvidence = document.getElementById("inspectorEvidence") as HTMLElement | null;
const telStepsDone = document.getElementById("telStepsDone") as HTMLElement | null;
const telRemoteVision = document.getElementById("telRemoteVision") as HTMLElement | null;
const telPlannerLatency = document.getElementById("telPlannerLatency") as HTMLElement | null;
const obstacleBadge = document.getElementById("obstacleBadge") as HTMLElement | null;
const inspectorScreenshot = document.getElementById("inspectorScreenshot") as HTMLImageElement | null;
const inspectorScreenshotPlaceholder = document.getElementById("inspectorScreenshotPlaceholder") as HTMLElement | null;

// WS status
const wsDot = document.getElementById("wsDot") as HTMLElement | null;
const wsText = document.getElementById("wsText") as HTMLElement | null;

// Session & Task State
let activeChatId: string | null = null;
let currentTaskId: string | null = null;
let renderedTerminalTaskId: string | null = null;
let stepCounter = 0;
let cancelRequested = false;
let isWorking = false;
let lastProgressTime = 0;
let pollInterval: number | null = null;

interface ChatSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  status: string;
}

interface ChatMessage {
  id: string;
  chat_id: string;
  role: "user" | "agent" | "system";
  content: string;
  evidence?: string | null;
  created_at: string;
}

interface ChatDetail {
  chat: ChatSummary;
  messages: ChatMessage[];
  memory: Record<string, string>;
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
    stateBadge.style.color = "#38bdf8";
    stateBadge.style.background = "rgba(56, 189, 248, 0.2)";
  } else if (state === "completed") {
    stateBadge.style.color = "#34d399";
    stateBadge.style.background = "rgba(52, 211, 153, 0.2)";
  } else if (["failed", "cancelled"].includes(state)) {
    stateBadge.style.color = "#f87171";
    stateBadge.style.background = "rgba(248, 113, 113, 0.2)";
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
    const res = await fetch(`${API}/api/health`);
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

function formatResultHtml(rawMessage: string): string {
  const urlMatch = rawMessage.match(/https?:\/\/[^\s"'<>)]+/);
  // Show a single price badge only when the summary contains exactly one
  // distinct price. In multi-product research summaries the first price is
  // just one of several — surfacing it as "the price" would mislead.
  const allPrices = rawMessage.match(/(?:₹|Rs\.?|INR|\$)\s*\d[\d,]*(?:\.\d+)?/gi) ?? [];
  const normalized = new Set(allPrices.map(p => p.replace(/\s+/g, "")));
  const directUrl = urlMatch ? safeHttpUrl(urlMatch[0]) : null;
  const price = normalized.size === 1 && allPrices.length > 0 ? (allPrices[0] ?? "").trim() : "";

  let showcaseHtml = "";
  if (directUrl || (price && /\d/.test(price))) {
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

  return `
    ${showcaseHtml}
    <div class="result-raw-text">${linkify(rawMessage)}</div>
  `;
}

function addUserMessageItem(message: string): void {
  if (!stepsList) return;
  const placeholder = stepsList.querySelector(".agent-step-placeholder");
  if (placeholder) placeholder.remove();

  const li = document.createElement("li");
  li.className = "agent-step-item agent-step-item--user";
  li.innerHTML = `
    <span class="agent-step-item__icon">💬</span>
    <span class="agent-step-item__text">${linkify(message)}</span>
  `;
  stepsList.appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function updateStepsCount(count: number): void {
  stepCounter = Math.max(0, count);
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
    updateStepsCount(stepCounter + 1);
  }

  // Normalize display: avoid double prefix like "[1] Step 1:"
  let displayMessage = message.trim();
  const stepPrefixRegex = /^\[\d+\]\s*/;
  if (stepPrefixRegex.test(displayMessage)) {
    displayMessage = displayMessage.replace(stepPrefixRegex, "");
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
  li.className = "agent-step-item";
  li.innerHTML = `
    <span class="agent-step-item__icon">${actionIcon(displayMessage)}</span>
    <span class="agent-step-item__text">${linkify(displayMessage)}</span>
  `;
  stepsList.appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });

  const urlMatch = displayMessage.match(/https?:\/\/[^\s"'<>)]+/);
  const safeUrl = urlMatch ? safeHttpUrl(urlMatch[0]) : null;
  if (safeUrl && inspectorUrl) {
    inspectorUrl.href = safeUrl;
    inspectorUrl.textContent = safeUrl;
  }
  if (inspectorEvidence) {
    inspectorEvidence.textContent = displayMessage;
  }
}

const START_MISSION_HTML = `
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
  Start Mission
`.trim();

const WORKING_MISSION_HTML = `
  <span class="btn-working-content">
    <svg class="btn-working-spinner" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
    Working<span class="btn-working-dots">...</span>
  </span>
`.trim();

function showStalledNotice(): void {
  if (missionSubtext && isWorking) {
    missionSubtext.innerHTML = `
      <span style="color: #fbbf24; font-weight: 500;">
        ⏳ Progress is taking longer than usual (planner waiting for model response). You may wait or click Cancel.
      </span>
    `;
  }
}

function clearStalledNotice(): void {
  if (missionSubtext) {
    missionSubtext.textContent = "Automated browsing with fail-closed privacy.";
  }
}

function setButtonWorking(working: boolean): void {
  isWorking = working;
  if (!btnStartMission) return;
  if (working) {
    lastProgressTime = Date.now();
    btnStartMission.disabled = true;
    btnStartMission.classList.add("btn--working");
    btnStartMission.innerHTML = WORKING_MISSION_HTML;
  } else {
    btnStartMission.classList.remove("btn--working");
    btnStartMission.innerHTML = START_MISSION_HTML;
    btnStartMission.disabled = !missionInput?.value.trim();
    clearStalledNotice();
  }
}

function addCompletionStepItem(taskState: string, message: string): void {
  if (!stepsList) return;
  const isOk = taskState === "completed";

  // If a result card is already in the steps list, update it in place rather than creating duplicate
  const existingResult = stepsList.querySelector(".agent-step-item--result, .agent-step-item--failed");
  if (existingResult) {
    existingResult.className = isOk ? "agent-step-item agent-step-item--result" : "agent-step-item agent-step-item--failed";
    existingResult.innerHTML = `
      <span class="agent-step-item__icon">${isOk ? "✓" : "✗"}</span>
      <span class="agent-step-item__text">
        <strong>${isOk ? "Mission Result" : "Task Incomplete"}</strong>:
        ${linkify(message || (isOk ? "Mission completed." : "Task stopped."))}
      </span>
    `;
    return;
  }

  const li = document.createElement("li");
  li.className = isOk ? "agent-step-item agent-step-item--result" : "agent-step-item agent-step-item--failed";
  li.innerHTML = `
    <span class="agent-step-item__icon">${isOk ? "✓" : "✗"}</span>
    <span class="agent-step-item__text">
      <strong>${isOk ? "Mission Result" : "Task Incomplete"}</strong>:
      ${linkify(message || (isOk ? "Mission completed." : "Task stopped."))}
    </span>
  `;
  stepsList.appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function syncActiveTask(taskId: string): Promise<boolean> {
  try {
    const res = await fetch(`${API}/api/task/${taskId}`);
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
      screenshot?: string | null;
      intent?: string;
      isBrowserTask?: boolean;
      sessionId?: string | null;
      primaryTabId?: number | null;
      childTabIds?: number[] | null;
    };
    if (currentTaskId !== taskId) return false;
    lastProgressTime = Date.now();
    clearStalledNotice();

    if (missionSubtext && !missionSubtext.innerHTML.includes("Progress is taking longer")) {
      if (t.isBrowserTask === false || t.intent === "NON_BROWSER") {
        missionSubtext.innerHTML = `<span style="color: #a78bfa; font-weight: 500;">💬 Mode: Conversational (Zero Browser Actions)</span>`;
      } else if (t.sessionId) {
        const tabInfo = t.primaryTabId ? ` · Tab #${t.primaryTabId}` : "";
        const childInfo = t.childTabIds && t.childTabIds.length > 0 ? ` (+${t.childTabIds.length} child tabs)` : "";
        missionSubtext.innerHTML = `<span style="color: #38bdf8; font-weight: 500;">🌐 Session: ${t.sessionId.slice(0, 8)}${tabInfo}${childInfo}</span>`;
      }
    }

    if (t.taskState && t.taskState !== "planning") {
      setTaskState(t.taskState);
    }
    if (t.screenshot) {
      updateVisualFrame(t.screenshot, t.message || "", t.taskState);
    }
    if (["completed", "failed", "cancelled"].includes(t.taskState)) {
      renderCompletion(t.taskState, t.message || "");
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

function renderCompletion(taskState: string, message: string, appendToTimeline = true): void {
  stopFallbackPolling();
  clearStalledNotice();
  setTaskState(taskState);
  setButtonWorking(false);
  if (btnCancelMission) btnCancelMission.hidden = true;
  if (humanGate) humanGate.hidden = true;

  if (currentTaskId && renderedTerminalTaskId === currentTaskId) {
    if (resultContent && !resultCard?.hidden) {
      if (taskState === "completed") {
        resultContent.innerHTML = formatResultHtml(message || "The autonomous task completed successfully.");
      } else {
        resultContent.innerHTML = `<div class="result-raw-text">${linkify(message || "Task stopped before completing all steps.")}</div>`;
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
      resultContent.innerHTML = formatResultHtml(message || "The autonomous task completed successfully.");
    } else {
      resultCard.classList.add("is-failed");
      resultBadge.textContent = "✗ Task Blocked / Incomplete";
      resultContent.innerHTML = `<div class="result-raw-text">${linkify(message || "Task stopped before completing all steps.")}</div>`;
    }
    resultCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
    loadArtifacts(currentTaskId, activeChatId);
  }
}

function showHumanGate(msg: string): void {
  if (!humanGate) return;
  if (humanGateMsg) humanGateMsg.textContent = msg || "Sign-in or authentication required to continue.";
  humanGate.hidden = false;
  humanGate.scrollIntoView({ behavior: "smooth", block: "center" });
}

function hideHumanGate(): void {
  if (humanGate) humanGate.hidden = true;
}

// ----------------------------------------------------
// Multi-Turn Chat & Session Isolation Management
// ----------------------------------------------------

async function loadChatList(): Promise<void> {
  if (!chatHistoryList) return;
  try {
    const res = await fetch(`${API}/api/chats`);
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

      li.innerHTML = `
        <div class="chat-history-item__main">
          <span class="chat-history-item__title" title="${escapeHtml(chat.title)}">${escapeHtml(chat.title || "Untitled Mission")}</span>
          <span class="chat-history-item__meta">${escapeHtml(dateLabel)}</span>
        </div>
        <button class="chat-delete-btn" title="Delete mission" aria-label="Delete mission">×</button>
      `;

      li.addEventListener("click", () => {
        if (activeChatId !== chat.id) {
          selectChat(chat.id);
        }
      });

      const delBtn = li.querySelector(".chat-delete-btn") as HTMLButtonElement | null;
      delBtn?.addEventListener("click", ev => {
        ev.stopPropagation();
        deleteChat(chat.id);
      });

      chatHistoryList.appendChild(li);
    });
  } catch {
    // Backend may still be spinning up
  }
}

async function selectChat(chatId: string): Promise<void> {
  activeChatId = chatId;
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
  loadArtifacts(null, chatId);
  if (stepsCount) stepsCount.textContent = "0 steps executed";
  renderStepBudget();
  setTaskState("ready");

  if (missionInput) {
    missionInput.value = "";
    missionInput.placeholder = "Ask a follow-up directive for this mission...";
    if (btnStartMission) btnStartMission.disabled = true;
  }

  try {
    const res = await fetch(`${API}/api/chat/${chatId}`);
    if (!res.ok) return;
    const detail = (await res.json()) as ChatDetail;
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
    let finalTaskState = chatStatus === "failed" || chatStatus === "cancelled" ? chatStatus : "completed";

    detail.messages.forEach(msg => {
      if (msg.role === "user") {
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
        addCompletionStepItem(msgState, msg.content);
        if (msg.evidence) {
          const urlMatch = msg.evidence.match(/https?:\/\/[^\s"'<>)]+/);
          const safeUrl = urlMatch ? safeHttpUrl(urlMatch[0]) : null;
          if (safeUrl && inspectorUrl) {
            inspectorUrl.href = safeUrl;
            inspectorUrl.textContent = safeUrl;
          }
          if (inspectorEvidence) inspectorEvidence.textContent = msg.evidence;
        }
      }
    });

    if (lastAgentMsg) {
      renderCompletion(finalTaskState, lastAgentMsg, false);
      setTaskState(finalTaskState);
    }
  } catch (err) {
    addStepItem(`⚠️ Failed to load chat history: ${err}`);
  }
}

async function deleteChat(chatId: string): Promise<void> {
  try {
    await fetch(`${API}/api/chat/${chatId}`, { method: "DELETE" });
  } catch {
    // Offline
  }

  if (activeChatId === chatId) {
    createNewChat();
  }
  await loadChatList();
}

function createNewChat(): void {
  stopFallbackPolling();
  clearStalledNotice();
  activeChatId = null;
  currentTaskId = null;
  renderedTerminalTaskId = null;
  cancelRequested = false;
  stepCounter = 0;
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
// UI Events & Directive Execution
// ----------------------------------------------------

btnToggleSidebarHeader?.addEventListener("click", () => {
  agentWorkspace?.classList.toggle("sidebar-collapsed");
});

btnCollapseSidebar?.addEventListener("click", () => {
  agentWorkspace?.classList.add("sidebar-collapsed");
});

btnNewMissionSidebar?.addEventListener("click", () => {
  createNewChat();
});

btnNewSession?.addEventListener("click", () => {
  createNewChat();
});

missionInput?.addEventListener("input", () => {
  if (btnStartMission && !isWorking) btnStartMission.disabled = !missionInput.value.trim();
});

missionInput?.addEventListener("keydown", (e: KeyboardEvent) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (!isWorking && missionInput.value.trim() && btnStartMission && !btnStartMission.disabled) {
      btnStartMission.click();
    }
  }
});

// Preset Chips
document.querySelectorAll(".preset-chip").forEach(chip => {
  chip.addEventListener("click", () => {
    const prompt = chip.getAttribute("data-prompt");
    if (prompt && missionInput) {
      missionInput.value = prompt;
      if (btnStartMission && !isWorking) btnStartMission.disabled = false;
      missionInput.focus();
    }
  });
});

btnStartMission?.addEventListener("click", async () => {
  if (isWorking || !missionInput || !btnStartMission || !missionInput.value.trim()) return;
  const instruction = missionInput.value.trim();

  setButtonWorking(true);
  if (btnCancelMission) btnCancelMission.hidden = false;
  if (resultCard) resultCard.hidden = true;
  hideHumanGate();
  currentTaskId = null;
  renderedTerminalTaskId = null;
  cancelRequested = false;

  // Append user instruction bubble immediately
  addUserMessageItem(instruction);
  missionInput.value = "";
  if (btnStartMission) btnStartMission.disabled = true;

  if (telPlannerLatency) telPlannerLatency.textContent = "—";
  setTaskState("planning");

  try {
    let res: Response;
    if (activeChatId) {
      res = await fetch(`${API}/api/chat/${activeChatId}/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instruction }),
      });
    } else {
      res = await fetch(`${API}/api/task`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instruction }),
      });
    }

    if (!res.ok) throw new Error(`Backend response HTTP ${res.status}`);
    const data = (await res.json()) as { task_id?: string; taskId?: string; chat_id?: string; chatId?: string };
    currentTaskId = data.task_id || data.taskId || null;
    activeChatId = activeChatId || data.chat_id || data.chatId || null;

    // Refresh chat list to show active title and timestamp
    loadChatList();

    // Cancel clicked while POST was in flight
    if (cancelRequested && currentTaskId) {
      try {
        await fetch(`${API}/api/task/${currentTaskId}/cancel`, { method: "POST" });
      } catch { /* offline */ }
      setTaskState("cancelled");
      if (btnCancelMission) btnCancelMission.hidden = true;
      setButtonWorking(false);
      return;
    }

    // Start background sync / polling for active task
    if (currentTaskId) {
      startFallbackPolling(currentTaskId);
      const stRes = await fetch(`${API}/api/task/${currentTaskId}`).catch(() => null);
      if (stRes && stRes.ok) {
        const t = (await stRes.json()) as { taskState: string; message: string; screenshot?: string | null };
        if (t.taskState && t.taskState !== "planning") {
          setTaskState(t.taskState);
        }
        if (t.screenshot) {
          updateVisualFrame(t.screenshot, t.message || "", t.taskState);
        }
        if (["completed", "failed", "cancelled"].includes(t.taskState)) {
          stopFallbackPolling();
          renderCompletion(t.taskState, t.message || "");
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
});

btnCancelMission?.addEventListener("click", async () => {
  const id = currentTaskId;
  cancelRequested = true;
  stopFallbackPolling();
  clearStalledNotice();
  if (btnCancelMission) btnCancelMission.hidden = true;
  hideHumanGate();
  if (id) {
    try {
      await fetch(`${API}/api/task/${id}/cancel`, { method: "POST" });
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
  await fetch(`${API}/api/task/${currentTaskId}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision: "approve" }),
  });
});

btnGateReject?.addEventListener("click", async () => {
  if (!currentTaskId) return;
  hideHumanGate();
  await fetch(`${API}/api/task/${currentTaskId}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision: "reject" }),
  });
});

// ----------------------------------------------------
// WebSocket Client
// ----------------------------------------------------

function connectWebSocket(): void {
  const ws = new WebSocket(`ws://localhost:8000/ws/client`);

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
        };
      };
      if (env.type !== "PROGRESS") return;
      if (env.taskId !== currentTaskId && env.payload?.chatId !== activeChatId) return;
      const p = env.payload;

      lastProgressTime = Date.now();
      clearStalledNotice();

      if (missionSubtext && !missionSubtext.innerHTML.includes("Progress is taking longer")) {
        if (p.isBrowserTask === false || p.intent === "NON_BROWSER") {
          missionSubtext.innerHTML = `<span style="color: #a78bfa; font-weight: 500;">💬 Mode: Conversational (Zero Browser Actions)</span>`;
        } else if (p.sessionId) {
          const tabInfo = p.primaryTabId ? ` · Tab #${p.primaryTabId}` : "";
          const childInfo = p.childTabIds && p.childTabIds.length > 0 ? ` (+${p.childTabIds.length} child tabs)` : "";
          missionSubtext.innerHTML = `<span style="color: #38bdf8; font-weight: 500;">🌐 Session: ${p.sessionId.slice(0, 8)}${tabInfo}${childInfo}</span>`;
        }
      }

      if (["completed", "failed", "cancelled"].includes(p.taskState)) {
        stopFallbackPolling();
        renderCompletion(p.taskState, p.message);
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
        fetch(`${API}/api/task/${env.taskId}`)
          .then(r => r.json())
          .then(t => {
            const msg = t.pending_confirmation?.message ?? "Authentication / Sign-in required";
            showHumanGate(msg);
          });
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
loadChatList();
initLlmSettings();

// ---------------------------------------------------------------------------
// LLM provider settings — runtime provider/model selection via /api/llm/*.
// The API key is password-typed, sent only to the backend, and never echoed
// back: the config response carries keyConfigured (boolean) only.
// ---------------------------------------------------------------------------

const DEFAULT_SEEDED_MODELS = ["glm-5.3-flash", "deepseek-v4-flash"];

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
    const show = providerSelect.value === "antseed";
    const fields = $("antseedFields");
    if (fields) fields.hidden = !show;
  });
  wireModelModeToggle();
  $("btnLlmDiscover")?.addEventListener("click", () => { void discoverLlmModels(); });
  $("btnLlmSave")?.addEventListener("click", () => { void saveLlmSettings(); });

  await refreshLlmSettings();
}

async function refreshLlmSettings(): Promise<void> {
  try {
    const res = await fetch(`${API}/api/llm/config`);
    if (!res.ok) return;
    const cfg = (await res.json()) as LlmConfig;
    const providerSelect = $("llmProviderSelect") as HTMLSelectElement | null;
    if (providerSelect) {
      providerSelect.value = cfg.provider;
      const fields = $("antseedFields");
      if (fields) fields.hidden = cfg.provider !== "antseed";
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
        DEFAULT_SEEDED_MODELS.forEach(m => modelSel.add(new Option(m, m)));
      }
      if (cfg.model) {
        let found = false;
        for (const opt of Array.from(modelSel.options)) {
          if (opt.value === cfg.model) { modelSel.value = cfg.model; found = true; break; }
        }
        if (!found) {
          modelSel.add(new Option(cfg.model, cfg.model));
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
    const res = await fetch(`${API}/api/llm/models`);
    if (!res.ok) {
      if (status) status.textContent = `Discovery failed (HTTP ${res.status}). Manual entry still available.`;
      return;
    }
    const data = (await res.json()) as { discovered?: boolean; models?: string[]; reason?: string | null };
    const models = data.models ?? [];
    if (data.discovered && modelSel) {
      const keep = modelSel.value;
      modelSel.options.length = 0;
      models.forEach(m => modelSel.add(new Option(m, m)));
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
  if (provider === "antseed") {
    const baseUrl = baseEl?.value.trim() || "http://localhost:8377/v1";
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
    const res = await fetch(`${API}/api/llm/config`, {
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
