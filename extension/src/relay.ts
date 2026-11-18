export type RelayMessage = {
  type: "OBSERVATION" | "RESULT" | "PROGRESS";
  taskId: string;
  payload: unknown;
};

export type InboundMessage =
  | {
      type: "EXECUTE_ACTION";
      taskId: string;
      payload: unknown;
      // Task 3: the task's domain allow-list, sent by the backend with the
      // action so the extension can refuse a disallowed navigation itself.
      allowedDomains?: string[];
    }
  | {
      // Task 4: takeover notifications. No payload, no action — the
      // extension just stops observing/screenshotting until TAKEOVER_END.
      type: "TAKEOVER_START" | "TAKEOVER_END";
      taskId: string;
      payload: unknown;
    };

const INBOUND_TYPES = new Set(["EXECUTE_ACTION", "TAKEOVER_START", "TAKEOVER_END"]);

/** Frame around a WebSocket: outbound envelopes + inbound dispatch.
 * Routing logic is pure and testable without Chrome. */
export class Relay {
  private socket: WebSocket | null = null;
  private handlers: ((m: InboundMessage) => void)[] = [];

  attach(socket: WebSocket): void {
    this.socket = socket;
    socket.addEventListener("message", ev => {
      let msg: unknown;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (typeof msg !== "object" || msg === null) return;
      const m = msg as { type?: string; taskId?: string; payload?: unknown; allowedDomains?: unknown };
      if (!m.type || !INBOUND_TYPES.has(m.type) || typeof m.taskId !== "string" || !m.taskId) return;
      for (const h of this.handlers) {
        try {
          h({
            type: m.type as InboundMessage["type"],
            taskId: m.taskId,
            payload: m.payload ?? {},
            allowedDomains: Array.isArray(m.allowedDomains)
              ? m.allowedDomains.filter((d): d is string => typeof d === "string")
              : undefined,
          });
        } catch {
          // A handler bug must not tear down the socket listener.
        }
      }
    });
  }

  /** Detach a socket that is closing so later sends cannot target it. */
  detach(socket: WebSocket): void {
    if (this.socket === socket) this.socket = null;
  }

  onMessage(cb: (m: InboundMessage) => void): void {
    this.handlers.push(cb);
  }

  /** Returns false (instead of throwing) when the relay is offline. */
  send(msg: RelayMessage): boolean {
    const ws = this.socket as (WebSocket & { readyState?: number }) | null;
    if (!ws) return false;
    const open = typeof WebSocket !== "undefined" ? WebSocket.OPEN : 1;
    // Sockets without readyState (test doubles) are treated as open.
    if (ws.readyState !== undefined && ws.readyState !== open) return false;
    try {
      ws.send(JSON.stringify(msg));
      return true;
    } catch {
      return false;
    }
  }
}
