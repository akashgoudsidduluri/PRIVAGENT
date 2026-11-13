import { createDrawer, type CompanionSnapshot, trustedCompanionEvent, getRandomBanter } from "./companionDrawer";

export const EYE_STATES = ["idle", "listening", "thinking", "browsing", "reading", "executing",
  "waiting_approval", "success", "error", "blocked", "paused"] as const;
export type EyeState = typeof EYE_STATES[number];

export function eyeStateFor(snapshot: CompanionSnapshot): EyeState {
  if (snapshot.pendingGate) return "waiting_approval";
  if (snapshot.status === "completed") return "success";
  if (snapshot.status === "paused") return "paused";
  if (snapshot.status === "failed") return /privacy|blocked|unauthorized/i.test(snapshot.currentThought) ? "blocked" : "error";
  if (snapshot.status === "awaiting_input") return "listening";
  if (snapshot.status === "planning") return "thinking";
  if (snapshot.status === "running") {
    if (/navigate|open(?:ed)? (?:new )?tab|brows/i.test(snapshot.currentThought)) return "browsing";
    if (/observ|read|extract/i.test(snapshot.currentThought)) return "reading";
    return "executing";
  }
  return "idle";
}

const CSS = `
:host{all:initial;position:fixed;right:24px;bottom:24px;z-index:2147483647;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:#edf6ff;contain:layout style}
*{box-sizing:border-box}button,input{font:inherit}button{cursor:pointer}
.robot{position:relative;width:78px;height:74px;border:1px solid #7cb2d3;border-radius:28px 28px 25px 25px;background:linear-gradient(150deg,#b3c6d6,#3b536b 26%,#101e30 56%,#6a859e);box-shadow:inset 0 2px 4px #e2f7ff99,inset 0 -7px 14px #030b1c,0 14px 38px #000000a6;animation:bob 3.6s ease-in-out infinite;color:#55eaff;transition:transform .2s cubic-bezier(.34,1.56,.64,1),box-shadow .2s ease}
.robot:hover{transform:translateY(-4px) scale(1.05);box-shadow:inset 0 2px 5px #e2f7ffcc,inset 0 -7px 14px #030b1c,0 18px 46px #55eaff44;color:#7bf5ff}
.robot:active{transform:translateY(2px) scale(0.96) rotate(-2deg)}
.robot:before{content:"";position:absolute;left:10px;right:10px;top:17px;height:35px;border-radius:14px;background:radial-gradient(ellipse at 50% 30%,#193957,#07152b);box-shadow:inset 0 1px 10px #010710,0 0 0 1px #83a5ba88}
.antenna{position:absolute;top:-11px;left:36px;width:6px;height:12px;border-radius:5px;background:#789bb1;transition:transform .2s ease}.antenna:before{content:"";position:absolute;top:-6px;left:-2px;width:10px;height:10px;border-radius:50%;background:#67edff;box-shadow:0 0 14px #65e7ff;animation:pulse-glow 2s infinite alternate}
.robot:hover .antenna{transform:rotate(12deg)}
.eyes{position:absolute;top:27px;left:21px;display:flex;gap:10px}.eyes i{display:block;width:13px;height:14px;border-radius:6px;background:currentColor;box-shadow:0 0 14px currentColor;animation:blink 4.5s infinite;transition:all .2s ease}
.robot:hover .eyes i:first-child{transform:scaleY(0.2) translateY(2px)} /* Winking on hover! */
.mouth{position:absolute;bottom:13px;left:31px;width:16px;height:3px;border-radius:3px;background:#78c6de;transition:all .2s ease}
.robot:hover .mouth{height:6px;border-radius:0 0 10px 10px;border-bottom:2px solid #55eaff;background:transparent} /* Smiling on hover! */

.bubble{max-width:310px;padding:11px 15px;margin:0 0 12px auto;border:1px solid #36759a;border-radius:16px;background:linear-gradient(135deg,#0c233c,#081628);box-shadow:0 14px 34px #00000099,0 0 18px #55eaff22;font-size:12px;line-height:1.5;overflow-wrap:anywhere;position:relative;animation:pop-in .25s cubic-bezier(.34,1.56,.64,1)}
.bubble:after{content:"";position:absolute;bottom:-7px;right:32px;width:12px;height:12px;background:#0c233c;border-right:1px solid #36759a;border-bottom:1px solid #36759a;transform:rotate(45deg)}

[hidden]{display:none!important}
.drawer{width:min(365px,calc(100vw - 32px));max-height:min(74vh,600px);overflow-y:auto;padding:18px;margin:0 0 14px auto;border:1px solid #3e799c;border-radius:20px;background:linear-gradient(160deg,#142d4a,#081627);box-shadow:0 24px 64px #000000cc,0 0 24px #55eaff18;animation:slide-up .25s ease-out}
.drawer header{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #375373;padding-bottom:12px;margin-bottom:12px}
.companion-brand{display:flex;align-items:center;gap:10px}
.bot-badge{font-size:22px;filter:drop-shadow(0 0 6px #55eaff)}
.companion-brand small{display:block;letter-spacing:.15em;font-size:9px;color:#59e9ff;font-weight:700}
.companion-brand strong{display:block;font-size:13px;color:#fff}
.state{padding:3px 9px;border-radius:12px;font-size:10px;font-weight:700;letter-spacing:.08em;background:#183c5c;border:1px solid #38709e;color:#8ce8ff;text-transform:uppercase}

/* Bot Banter & Personality Section */
.bot-personality-card{padding:12px;border-radius:14px;background:rgba(10,28,49,0.7);border:1px solid #2b577d;margin-bottom:14px}
.bot-speech-bubble{font-size:12px;line-height:1.5;color:#e1f2ff;font-style:italic;margin-bottom:10px;background:#0c2035;padding:9px 12px;border-radius:10px;border-left:3px solid #55eaff}
.bot-quick-chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.chip-btn{border:1px solid #356b91;border-radius:8px;background:#123250;color:#cce7ff;padding:5px 9px;font-size:11px;transition:all .15s ease}
.chip-btn:hover{background:#1d4d75;border-color:#59e9ff;color:#fff;transform:translateY(-1px)}
.bot-mini-chat{display:flex;gap:6px}
.bot-chat-input{flex:1;background:#081524;border:1px solid #2a557a;border-radius:8px;padding:7px 10px;color:#edf6ff;font-size:11px;outline:none;transition:border-color .15s}
.bot-chat-input:focus{border-color:#55eaff;box-shadow:0 0 8px #55eaff44}
.bot-chat-send{border:1px solid #3d88b0;border-radius:8px;background:#1d537a;color:#fff;padding:7px 12px;font-size:11px;font-weight:600;transition:all .15s}
.bot-chat-send:hover{background:#2874aa;border-color:#55eaff}

.mission-status-section{border-top:1px solid #294766;padding-top:10px}
.directive{color:#b9d5ee;font-size:12px;line-height:1.45;margin:0 0 8px;font-weight:500}
.timeline{padding-left:18px;color:#9bc0e0;font-size:11px;line-height:1.5;max-height:140px;overflow-y:auto;margin:0 0 10px}
.timeline li{margin:5px 0}
.gate{padding:12px;border:1px solid #eeb962;border-radius:12px;background:#45321b;color:#ffdea5;margin-bottom:10px}.gate p{margin:0 0 9px}
.controls{display:flex;flex-wrap:wrap;gap:7px}
.drawer button[data-action]{border:1px solid #34688b;border-radius:8px;background:#153752;color:#edf6ff;padding:7px 11px;font-size:11px}
.drawer button[data-action]:hover{background:#205278;border-color:#55eaff}
.drawer button:focus-visible,.robot:focus-visible{outline:2px solid #59e9ff}
.gate [data-action=approve]{background:#2d6952!important;border-color:#59bd99!important;color:#fff}
.gate [data-action=abort]{background:#662d35!important;border-color:#e7878a!important;color:#fff}

[data-state=listening] .eyes i{height:20px}
[data-state=thinking] .eyes{animation:scan .7s alternate infinite}
[data-state=browsing] .eyes{animation:scan 1.8s alternate infinite}
[data-state=reading] .eyes i{animation:sweep 1.1s alternate infinite}
[data-state=executing] .robot{animation:pulse .8s alternate infinite}
[data-state=waiting_approval] .robot{color:#ffcb65;animation:pulse 1s alternate infinite}
[data-state=success] .robot{color:#8cf2ac}
[data-state=success] .mouth{border-bottom:3px solid #8cf2ac;background:none;border-radius:50%;height:7px}
[data-state=error] .robot{color:#ff8888;transform:rotate(-7deg)}
[data-state=blocked] .robot{color:#ff8585;animation:shake .4s alternate infinite}
[data-state=paused] .eyes i{height:3px;animation:none}

@keyframes bob{50%{transform:translateY(-6px)}}
@keyframes blink{2%{scale:1 .15}4%{scale:1 1}}
@keyframes scan{to{transform:translateX(8px)}}
@keyframes sweep{to{opacity:.4;transform:translateX(4px)}}
@keyframes pulse{to{filter:brightness(1.5);scale:1.03}}
@keyframes pulse-glow{to{box-shadow:0 0 20px #55eaff}}
@keyframes shake{to{transform:rotate(6deg)}}
@keyframes pop-in{from{opacity:0;transform:scale(0.85) translateY(8px)}to{opacity:1;transform:scale(1) translateY(0)}}
@keyframes slide-up{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
`;

export function mountCompanion(): HTMLElement | null {
  try {
    if (typeof document === "undefined" || typeof chrome === "undefined" || !chrome.runtime?.sendMessage) return null;
    const existing = document.querySelector("privagent-companion") as (HTMLElement & { update: (state: CompanionSnapshot) => void }) | null;
    if (existing) return existing;

    const host = document.createElement("privagent-companion") as HTMLElement & { update: (state: CompanionSnapshot) => void };
    host.style.position = "fixed";
    host.style.right = "24px";
    host.style.bottom = "24px";
    host.style.zIndex = "2147483647";
    host.style.display = "block";

    const root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = CSS;
    root.append(style);

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.hidden = true;
    bubble.setAttribute("aria-live", "polite");
    root.append(bubble);

    const { drawer, update } = createDrawer(root, message => chrome.runtime.sendMessage(message));

    const button = document.createElement("button");
    button.className = "robot";
    button.dataset.state = "idle";
    button.setAttribute("aria-label", "Toggle PrivAgent companion");
    button.setAttribute("aria-expanded", "false");
    button.innerHTML = '<span class="antenna"></span><span class="eyes"><i></i><i></i></span><span class="mouth"></span>';

    // Return Greeting: Check if returning after being away
    let isReturningUser = false;
    try {
      const LAST_KEY = "privagent_last_seen_ts";
      const lastSeen = window.localStorage?.getItem(LAST_KEY);
      const now = Date.now();
      if (lastSeen && (now - parseInt(lastSeen, 10)) > 10 * 60 * 1000) {
        isReturningUser = true;
      }
      window.localStorage?.setItem(LAST_KEY, String(now));
    } catch {}

    if (isReturningUser) {
      bubble.textContent = "Hey buddy! Long time no see! Welcome back. What are we checking today? 👋";
      bubble.hidden = false;
      try {
        window.setTimeout(() => {
          if (button.dataset.state === "idle" && drawer.hidden) {
            bubble.hidden = true;
          }
        }, 7000);
      } catch {}
    }

    // Interactive Click: Thought bubble and toggle drawer
    button.addEventListener("click", event => {
      if (!trustedCompanionEvent(event)) return;
      drawer.hidden = !drawer.hidden;
      button.setAttribute("aria-expanded", String(!drawer.hidden));

      // If drawer is now open, show a fresh conversational thought
      if (!drawer.hidden && !button.dataset.state.includes("executing")) {
        const thought = isReturningUser ? "Welcome back buddy! Ready to search or monitor." : getRandomBanter();
        bubble.textContent = thought;
        bubble.hidden = false;
      }
    });
    root.append(button);

    // Ambient Thought Timer: Every 60s when idle, drop a natural thought bubble that auto-fades!
    let banterTimer: number | null = null;
    try {
      banterTimer = window.setInterval(() => {
        if (button.dataset.state === "idle" && drawer.hidden) {
          bubble.textContent = getRandomBanter();
          bubble.hidden = false;
          window.setTimeout(() => {
            if (button.dataset.state === "idle" && drawer.hidden) {
              bubble.hidden = true;
            }
          }, 6000);
        }
      }, 60000);
    } catch {
      /* jsdom or restricted context */
    }

    host.update = (snapshot: CompanionSnapshot) => {
      button.dataset.state = eyeStateFor(snapshot);
      bubble.textContent = snapshot.currentThought.slice(0, 220);
      bubble.hidden = !snapshot.currentThought;
      update(snapshot);
    };

    (document.body || document.documentElement).append(host);

    chrome.runtime.onMessage.addListener((message: { type?: string; state?: CompanionSnapshot }, _sender, reply) => {
      if (message.type === "COMPANION_STATE" && message.state) {
        host.update(message.state);
        reply?.({ ok: true });
      }
    });

    chrome.runtime.sendMessage({ type: "COMPANION_SYNC" }).then((state: CompanionSnapshot | null) => {
      if (state?.taskId) host.update(state);
    }).catch(() => {});

    return host;
  } catch {
    return null;
  }
}
