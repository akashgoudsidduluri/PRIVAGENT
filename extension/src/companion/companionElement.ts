import { createDrawer, type CompanionSnapshot, trustedCompanionEvent } from "./companionDrawer";

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
:host{all:initial;position:fixed;right:24px;bottom:24px;z-index:2147483647;font-family:system-ui,sans-serif;color:#edf6ff;contain:layout style}
*{box-sizing:border-box}button{font:inherit;cursor:pointer}
.robot{position:relative;width:76px;height:72px;border:1px solid #7cb2d3;border-radius:27px 27px 24px 24px;background:linear-gradient(150deg,#a7b9c9,#34495d 26%,#101d2e 56%,#627a90);box-shadow:inset 0 2px 3px #e2f7ff88,inset 0 -7px 14px #030b1c,0 14px 36px #0009;animation:bob 3.6s ease-in-out infinite;color:#55eaff}
.robot:before{content:"";position:absolute;left:10px;right:10px;top:17px;height:34px;border-radius:13px;background:radial-gradient(ellipse at 50% 30%,#193650,#071327);box-shadow:inset 0 1px 9px #010710,0 0 0 1px #83a5ba77}
.antenna{position:absolute;top:-10px;left:35px;width:6px;height:11px;border-radius:5px;background:#789bb1}.antenna:before{content:"";position:absolute;top:-5px;left:-2px;width:10px;height:10px;border-radius:50%;background:#67edff;box-shadow:0 0 12px #65e7ff}
.eyes{position:absolute;top:26px;left:20px;display:flex;gap:10px}.eyes i{display:block;width:13px;height:14px;border-radius:6px;background:currentColor;box-shadow:0 0 13px currentColor;animation:blink 5s infinite}
.mouth{position:absolute;bottom:13px;left:31px;width:15px;height:3px;border-radius:3px;background:#78c6de}
.bubble{max-width:290px;padding:10px 14px;margin:0 0 10px auto;border:1px solid #36759a;border-radius:14px;background:#0c2036;box-shadow:0 12px 30px #0008;font-size:12px;line-height:1.5;overflow-wrap:anywhere}
[hidden]{display:none!important}.drawer{width:min(350px,calc(100vw - 32px));max-height:min(70vh,570px);overflow:auto;padding:18px;margin:0 0 12px auto;border:1px solid #3e7191;border-radius:19px;background:linear-gradient(160deg,#152b45,#091524);box-shadow:0 22px 60px #000a}
.drawer header{display:flex;flex-wrap:wrap;align-items:center;gap:8px;border-bottom:1px solid #37506a;padding-bottom:12px}.drawer header small{width:100%;letter-spacing:.2em;font-size:10px;color:#59e9ff}.state{margin-left:auto;color:#9fe4ff;text-transform:uppercase}.directive{color:#c6dcf2;font-size:13px;line-height:1.45}.timeline{padding-left:21px;color:#b9d1e7;font-size:12px;line-height:1.5;max-height:190px;overflow:auto}.timeline li{margin:7px 0}.gate{padding:12px;border:1px solid #eeb962;border-radius:10px;background:#45321b;color:#ffdea5}.gate p{margin:0 0 9px}.controls{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}.drawer button{border:1px solid #3e7795;border-radius:8px;background:#193b54;color:#edf6ff;padding:8px 10px;font-size:11px;margin:3px}.drawer button:focus-visible,.robot:focus-visible{outline:2px solid #59e9ff}.gate [data-action=approve]{background:#375e51;border-color:#59bd99}.gate [data-action=abort]{background:#5c3036;border-color:#e7878a}
[data-state=listening] .eyes i{height:20px}[data-state=thinking] .eyes{animation:scan .7s alternate infinite}[data-state=browsing] .eyes{animation:scan 1.8s alternate infinite}[data-state=reading] .eyes i{animation:sweep 1.1s alternate infinite}[data-state=executing] .robot{animation:pulse .8s alternate infinite}[data-state=waiting_approval] .robot{color:#ffcb65;animation:pulse 1s alternate infinite}[data-state=success] .robot{color:#8cf2ac}[data-state=success] .mouth{border-bottom:3px solid #8cf2ac;background:none;border-radius:50%;height:7px}[data-state=error] .robot{color:#ff8888;transform:rotate(-7deg)}[data-state=blocked] .robot{color:#ff8585;animation:shake .4s alternate infinite}[data-state=paused] .eyes i{height:3px;animation:none}
@keyframes bob{50%{transform:translateY(-6px)}}@keyframes blink{2%{scale:1 .15}4%{scale:1 1}}@keyframes scan{to{transform:translateX(8px)}}@keyframes sweep{to{opacity:.4;transform:translateX(4px)}}@keyframes pulse{to{filter:brightness(1.6);scale:1.04}}@keyframes shake{to{transform:rotate(6deg)}}@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
`;

export function mountCompanion(): HTMLElement | null {
  if (typeof document === "undefined" || typeof chrome === "undefined" || !chrome.runtime?.sendMessage) return null;
  if (document.querySelector("privagent-companion")) return document.querySelector("privagent-companion");
  if (!customElements.get("privagent-companion")) {
    customElements.define("privagent-companion", class extends HTMLElement {
      private button: HTMLButtonElement;
      private bubble: HTMLElement;
      private updateDrawer: (snapshot: CompanionSnapshot) => void;
      constructor() {
        super();
        const root = this.attachShadow({ mode: "closed" });
        const style = document.createElement("style"); style.textContent = CSS; root.append(style);
        const bubble = document.createElement("div"); bubble.className = "bubble"; bubble.hidden = true; bubble.setAttribute("aria-live", "polite"); root.append(bubble);
        this.bubble = bubble;
        const { drawer, update } = createDrawer(root, message => chrome.runtime.sendMessage(message));
        this.updateDrawer = update;
        const button = document.createElement("button"); button.className = "robot"; button.dataset.state = "idle";
        button.setAttribute("aria-label", "Toggle PrivAgent companion"); button.setAttribute("aria-expanded", "false");
        button.innerHTML = '<span class="antenna"></span><span class="eyes"><i></i><i></i></span><span class="mouth"></span>';
        button.addEventListener("click", event => {
          if (!trustedCompanionEvent(event)) return;
          drawer.hidden = !drawer.hidden; button.setAttribute("aria-expanded", String(!drawer.hidden));
        });
        root.append(button); this.button = button;
      }
      update(snapshot: CompanionSnapshot) {
        this.button.dataset.state = eyeStateFor(snapshot);
        this.bubble.textContent = snapshot.currentThought.slice(0, 220); this.bubble.hidden = !snapshot.currentThought;
        this.updateDrawer(snapshot);
      }
    });
  }
  const element = document.createElement("privagent-companion") as HTMLElement & { update: (state: CompanionSnapshot) => void };
  document.documentElement.append(element);
  chrome.runtime.onMessage.addListener((message: { type?: string; state?: CompanionSnapshot }, _sender, reply) => {
    if (message.type === "COMPANION_STATE" && message.state) { element.update(message.state); reply({ ok: true }); }
  });
  chrome.runtime.sendMessage({ type: "COMPANION_SYNC" }).then((state: CompanionSnapshot | null) => {
    if (state?.taskId) element.update(state);
  }).catch(() => {});
  return element;
}
