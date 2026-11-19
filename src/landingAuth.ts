/**
 * Landing-page account forms (#account section in index.html).
 *
 * Email + password signup / login against the backend's /api/auth/*
 * routes. These routes are exempt from the local-token guard, so this
 * module uses plain fetch() — never apiFetch() — or a first visit would
 * trigger the operator-token prompt before the user can even sign up.
 */

import { clearSessionToken, getSessionToken, setSessionToken } from "./auth";

const API = "http://localhost:8000";

type AuthMode = "signup" | "login";

export function initLandingAuth(): void {
  const form = document.getElementById("authForm") as HTMLFormElement | null;
  const emailInput = document.getElementById("authEmail") as HTMLInputElement | null;
  const nameInput = document.getElementById("authDisplayName") as HTMLInputElement | null;
  const nameRow = document.getElementById("authNameRow") as HTMLElement | null;
  const passwordInput = document.getElementById("authPassword") as HTMLInputElement | null;
  const errorEl = document.getElementById("authError") as HTMLElement | null;
  const submitBtn = document.getElementById("authSubmit") as HTMLButtonElement | null;
  const signedInEl = document.getElementById("authSignedIn") as HTMLElement | null;
  const signedInAs = document.getElementById("authSignedInAs") as HTMLElement | null;
  const signOutBtn = document.getElementById("authSignOut") as HTMLButtonElement | null;
  const tabSignup = document.getElementById("authTabSignup") as HTMLButtonElement | null;
  const tabLogin = document.getElementById("authTabLogin") as HTMLButtonElement | null;
  const tabsEl = document.getElementById("authTabs") as HTMLElement | null;
  if (!form || !emailInput || !passwordInput || !submitBtn) return;

  let mode: AuthMode = "signup";

  const showError = (message: string) => {
    if (!errorEl) return;
    errorEl.textContent = message;
    errorEl.hidden = false;
  };
  const clearError = () => {
    if (errorEl) errorEl.hidden = true;
  };

  const setMode = (next: AuthMode) => {
    mode = next;
    const signup = next === "signup";
    tabSignup?.classList.toggle("is-active", signup);
    tabLogin?.classList.toggle("is-active", !signup);
    tabSignup?.setAttribute("aria-selected", String(signup));
    tabLogin?.setAttribute("aria-selected", String(!signup));
    if (nameRow) nameRow.hidden = !signup;
    submitBtn.textContent = signup ? "Create account" : "Sign in";
    clearError();
  };

  const showSignedIn = (displayName: string, email: string) => {
    form.hidden = true;
    if (tabsEl) tabsEl.hidden = true;
    if (signedInEl) signedInEl.hidden = false;
    if (signedInAs) signedInAs.textContent = `${displayName} (${email})`;
  };

  tabSignup?.addEventListener("click", () => setMode("signup"));
  tabLogin?.addEventListener("click", () => setMode("login"));

  signOutBtn?.addEventListener("click", async () => {
    const token = getSessionToken();
    if (token) {
      try {
        await fetch(API + "/api/auth/logout", {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        });
      } catch {
        // The session is local state too: clearing it signs out even
        // if the backend is unreachable right now.
      }
    }
    clearSessionToken();
    form.hidden = false;
    if (tabsEl) tabsEl.hidden = false;
    if (signedInEl) signedInEl.hidden = true;
    setMode("login");
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();
    const email = emailInput.value.trim();
    const password = passwordInput.value;
    if (!email || !password) {
      showError("Enter your email and password.");
      return;
    }
    if (mode === "signup" && password.length < 8) {
      showError("Password must be at least 8 characters.");
      return;
    }
    submitBtn.disabled = true;
    try {
      const path = mode === "signup" ? "/api/auth/signup" : "/api/auth/login";
      const body: Record<string, string> =
        mode === "signup"
          ? { email, password, displayName: nameInput?.value.trim() || "" }
          : { email, password };
      const res = await fetch(API + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showError(typeof data?.detail === "string" ? data.detail : "Something went wrong. Try again.");
        return;
      }
      setSessionToken(data.session_token);
      showSignedIn(data.user.display_name, data.user.email);
    } catch {
      showError("Could not reach the backend on localhost:8000. Start it and try again.");
    } finally {
      submitBtn.disabled = false;
    }
  });

  // Returning visit: a stored session means "signed in". Only a 401
  // clears the token — a 500 or a network blip must not log the user
  // out; the server-side session is still valid then.
  const existing = getSessionToken();
  if (existing) {
    fetch(API + "/api/auth/me", { headers: { Authorization: `Bearer ${existing}` } })
      .then(async (res) => {
        if (res.status === 401) {
          clearSessionToken();
          return;
        }
        if (!res.ok) return;
        const me = await res.json();
        showSignedIn(me.display_name, me.email);
      })
      .catch(() => {
        // Backend unreachable: keep the token and show the form.
      });
  }
}
