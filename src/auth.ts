/**
 * Local operator auth for the web UI (Task 1).
 *
 * The backend requires `Authorization: Bearer <token>` on every
 * /api/* route (except /api/health). The token is a single local
 * operator secret — see backend/app/auth.py and the README security
 * section. It is NOT a user account and must never be committed.
 *
 * First run (manual step, once):
 *   1. Start the backend, then read the token from the backend dir:
 *        venv\Scripts\python.exe scripts\print_local_token.py   (Windows)
 *        ./venv/bin/python scripts/print_local_token.py          (Linux/macOS)
 *      (or use the PRIVAGENT_LOCAL_TOKEN value in backend/.env)
 *   2. Open the workspace once; on the first 401 the UI prompts for
 *      the token and stores it in localStorage. Paste it there.
 * The token lives in localStorage under `privagentLocalToken` and is
 * sent only to http://localhost:8000.
 */

export const TOKEN_STORAGE_KEY = "privagentLocalToken";

/** User-session token (email + password account, app/auth_users.py). */
export const SESSION_STORAGE_KEY = "privagentSessionToken";

export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, token.trim());
  } catch {
    // Private mode etc: the caller still gets one in-memory attempt.
  }
}

export function getSessionToken(): string | null {
  try {
    return window.localStorage.getItem(SESSION_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setSessionToken(token: string): void {
  try {
    window.localStorage.setItem(SESSION_STORAGE_KEY, token.trim());
  } catch {
    // Private mode etc: the caller still gets one in-memory attempt.
  }
}

export function clearSessionToken(): void {
  try {
    window.localStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // Already gone.
  }
}

export function isSignedIn(): boolean {
  return !!getSessionToken();
}

export function authHeaders(): Record<string, string> {
  // A user session wins over the local operator token: /api/profile and
  // /api/auth/me accept a session only, and every other /api/* route
  // accepts either. The local token stays the fallback for operator mode.
  const session = getSessionToken();
  if (session) return { Authorization: `Bearer ${session}` };
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Prompt for the token on a 401 and store it. Returns null if the user cancels.
 *
 * A cancelled prompt does NOT latch the prompt off forever (the old
 * `promptShown` flag did — one dismiss left the workspace 401ing with
 * no way back short of reload). Instead, re-prompting is throttled:
 * at most one prompt per cooldown window, so a polling loop cannot
 * nag the operator every second. A successful (non-401) response
 * clears the cooldown, so a later token rotation prompts at once. */
let promptOpen = false;
let lastPromptAt = 0;
const PROMPT_COOLDOWN_MS = 60_000;

function promptForToken(): string | null {
  if (promptOpen) return null;
  if (Date.now() - lastPromptAt < PROMPT_COOLDOWN_MS) return null;
  promptOpen = true;
  lastPromptAt = Date.now();
  try {
    const entered = window.prompt(
      "This workspace now needs the local operator token.\n" +
        "Find it with (from backend/): venv\\Scripts\\python.exe scripts\\print_local_token.py\n" +
        "(or the PRIVAGENT_LOCAL_TOKEN value in backend/.env)",
    );
    if (entered && entered.trim()) {
      setToken(entered);
      return entered.trim();
    }
    return null;
  } finally {
    promptOpen = false;
  }
}

/**
 * fetch() wrapper for backend calls. Adds the Authorization header and,
 * on a 401, prompts for the token (throttled, see promptForToken) and
 * retries once.
 */
export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const withAuth = (extra?: RequestInit): RequestInit => ({
    ...init,
    ...extra,
    headers: { ...(init?.headers as Record<string, string> | undefined), ...authHeaders(), ...((extra?.headers as Record<string, string> | undefined) ?? {}) },
  });
  const sessionSent = !!getSessionToken();
  let res = await fetch(input, withAuth());
  if (res.status === 401) {
    if (sessionSent) {
      // The session is dead (expired or revoked). Drop it and hand the
      // 401 back so the caller can route to sign-in. Never prompt an
      // email/password user for the local operator token — wrong
      // credential, and it leaks the operator-token mechanism.
      clearSessionToken();
      return res;
    }
    const token = promptForToken();
    if (token) {
      res = await fetch(input, {
        ...init,
        headers: { ...(init?.headers as Record<string, string> | undefined), Authorization: `Bearer ${token}` },
      });
    }
  } else {
    // Healthy again: a later 401 (e.g. token rotation) prompts at once
    // instead of waiting out a cooldown from an old dismissal.
    lastPromptAt = 0;
  }
  return res;
}

/** Append ?token= to a backend WebSocket URL when a token is stored. */
export function wsUrl(base: string): string {
  const token = getToken();
  if (!token) return base;
  const sep = base.includes("?") ? "&" : "?";
  return `${base}${sep}token=${encodeURIComponent(token)}`;
}

/**
 * Append ?token= to an artifact preview/download URL for plain <a href>
 * navigations, which cannot send an Authorization header. Only used for
 * those anchor links; fetch() calls use apiFetch() instead.
 */
export function artifactUrl(url: string): string {
  const token = getToken();
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}
