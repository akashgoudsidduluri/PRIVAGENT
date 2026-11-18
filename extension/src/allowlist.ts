/**
 * Domain allow-list check for the extension (Task 3: isolation).
 *
 * Defence in depth only: the backend loop is the authoritative gate and
 * rejects disallowed navigations before they are sent. This check exists
 * so a bad action that somehow reaches the extension is still refused
 * here, without executing. It mirrors backend/app/allowlist.py — keep
 * the matching rules identical on both sides:
 * - an empty/undefined list means "not configured": allow everything.
 * - a plain entry (example.com) matches the host and its subdomains.
 * - a wildcard entry (*.example.com) matches subdomains only.
 * - only http(s) URLs with a host and no embedded credentials pass.
 */

export function hostOf(url: string): string | null {
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    if (u.username || u.password) return null;
    return (u.hostname || "").toLowerCase() || null;
  } catch {
    return null;
  }
}

export function hostMatchesAllowlist(host: string, domains: string[]): boolean {
  const h = (host || "").trim().toLowerCase().replace(/\.$/, "");
  if (!h) return false;
  for (const raw of domains) {
    const entry = (raw || "").trim().toLowerCase().replace(/\.$/, "");
    if (!entry) continue;
    if (entry.startsWith("*.")) {
      const suffix = entry.slice(2);
      if (h !== suffix && h.endsWith(`.${suffix}`)) return true;
    } else if (h === entry || h.endsWith(`.${entry}`)) {
      return true;
    }
  }
  return false;
}

export function isAllowedNavigationUrl(url: string, allowedDomains?: string[]): boolean {
  if (!allowedDomains || allowedDomains.length === 0) return true;
  const host = hostOf(url);
  if (host === null) return false;
  return hostMatchesAllowlist(host, allowedDomains);
}
