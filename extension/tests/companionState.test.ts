// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountCompanion, EYE_STATES, eyeStateFor } from "../src/companion/companionElement";
import { authorizedCompanionTab, emptyCompanionState, nextCompanionState } from "../src/companion/companionState";
import { trustedCompanionEvent } from "../src/companion/companionDrawer";

afterEach(() => { document.querySelector("privagent-companion")?.remove(); vi.unstubAllGlobals(); });

describe("in-page companion", () => {
  it("mounts once with a closed shadow tree and ignores synthetic clicks", () => {
    const sendMessage = vi.fn(async () => null);
    vi.stubGlobal("chrome", { runtime: { sendMessage, onMessage: { addListener: vi.fn() } } });
    const host = mountCompanion()!;
    expect(host.tagName.toLowerCase()).toBe("privagent-companion");
    expect(host.shadowRoot).toBeNull();
    expect(mountCompanion()).toBe(host);
    expect(trustedCompanionEvent(new MouseEvent("click"))).toBe(false);
    expect(trustedCompanionEvent({ isTrusted: true } as Event)).toBe(true);
    host.click();
    expect(sendMessage).toHaveBeenCalledTimes(1); // initial sync only
  });

  it("keeps a bounded, deduplicated task timeline across authorized tabs", () => {
    let state = emptyCompanionState();
    const packet = { taskId: "task-1", taskState: "running", stepsDone: 2,
      message: "Step 2: observed page", instruction: "Find the policy", primaryTabId: 9, childTabIds: [10] };
    state = nextCompanionState(state, packet);
    state = nextCompanionState(state, packet);
    expect(state.timeline).toEqual(["Step 2: observed page"]);
    expect(state.stepIndex).toBe(2);
    expect(authorizedCompanionTab(10, 9, [10])).toBe(true);
    expect(authorizedCompanionTab(11, 9, [10])).toBe(false);
    expect(nextCompanionState(state, { ...packet, taskId: "task-2", message: "New task" }).timeline).toEqual(["New task"]);
  });

  it("maps all eleven expressive states without trusting page text as a command", () => {
    expect(EYE_STATES).toHaveLength(11);
    const base = emptyCompanionState();
    expect(eyeStateFor(base)).toBe("idle");
    expect(eyeStateFor({ ...base, status: "awaiting_input" })).toBe("listening");
    expect(eyeStateFor({ ...base, status: "planning" })).toBe("thinking");
    expect(eyeStateFor({ ...base, status: "running", currentThought: "navigated to a page" })).toBe("browsing");
    expect(eyeStateFor({ ...base, status: "running", currentThought: "observed content" })).toBe("reading");
    expect(eyeStateFor({ ...base, status: "running" })).toBe("executing");
    expect(eyeStateFor({ ...base, pendingGate: { taskId: "t", message: "Confirm" } })).toBe("waiting_approval");
    expect(eyeStateFor({ ...base, status: "completed" })).toBe("success");
    expect(eyeStateFor({ ...base, status: "failed" })).toBe("error");
    expect(eyeStateFor({ ...base, status: "failed", currentThought: "privacy blocked" })).toBe("blocked");
    expect(eyeStateFor({ ...base, status: "paused" })).toBe("paused");
  });
});
