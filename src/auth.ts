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

export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

let promptShown = false;

/** Prompt once for the token (first 401) and store it. Returns null if the user cancels. */
function promptForToken(): string | null {
  if (promptShown) return null;
  promptShown = true;
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
}

/**
 * fetch() wrapper for backend calls. Adds the Authorization header and,
 * on a first 401, prompts once for the token and retries once.
 */
export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const withAuth = (extra?: RequestInit): RequestInit => ({
    ...init,
    ...extra,
    headers: { ...(init?.headers as Record<string, string> | undefined), ...authHeaders(), ...((extra?.headers as Record<string, string> | undefined) ?? {}) },
  });
  let res = await fetch(input, withAuth());
  if (res.status === 401) {
    const token = promptForToken();
    if (token) {
      res = await fetch(input, {
        ...init,
        headers: { ...(init?.headers as Record<string, string> | undefined), Authorization: `Bearer ${token}` },
      });
    }
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
