import { describe, expect, it } from "vitest";
import { hostMatchesAllowlist, hostOf, isAllowedNavigationUrl } from "../src/allowlist";

describe("extension allow-list (Task 3, mirrors backend/app/allowlist.py)", () => {
  it("allows everything when no allow-list is configured", () => {
    expect(isAllowedNavigationUrl("https://evil.example/", undefined)).toBe(true);
    expect(isAllowedNavigationUrl("https://evil.example/", [])).toBe(true);
  });

  it("matches a plain entry and its subdomains", () => {
    expect(isAllowedNavigationUrl("https://www.google.com/search", ["google.com"])).toBe(true);
    expect(isAllowedNavigationUrl("https://google.com/", ["google.com"])).toBe(true);
    expect(isAllowedNavigationUrl("https://notgoogle.com/", ["google.com"])).toBe(false);
    expect(isAllowedNavigationUrl("https://evilgoogle.com/", ["google.com"])).toBe(false);
  });

  it("matches a wildcard entry for subdomains only, never the bare domain", () => {
    expect(isAllowedNavigationUrl("https://en.wikipedia.org/wiki/X", ["*.wikipedia.org"])).toBe(true);
    expect(isAllowedNavigationUrl("https://wikipedia.org/", ["*.wikipedia.org"])).toBe(false);
  });

  it("rejects non-http(s) targets and credentialed URLs", () => {
    expect(isAllowedNavigationUrl("javascript:alert(1)", ["google.com"])).toBe(false);
    expect(isAllowedNavigationUrl("https://user:pass@google.com/", ["google.com"])).toBe(false);
    expect(isAllowedNavigationUrl("ftp://google.com/", ["google.com"])).toBe(false);
  });

  it("hostMatchesAllowlist is case-insensitive and ignores a trailing dot", () => {
    expect(hostMatchesAllowlist("WWW.Google.COM.", ["google.com"])).toBe(true);
    expect(hostMatchesAllowlist("a.b.example.org", ["*.example.org"])).toBe(true);
  });

  it("hostOf returns null for unparsable or non-http(s) URLs", () => {
    expect(hostOf("https://example.com/x")).toBe("example.com");
    expect(hostOf("not a url")).toBeNull();
    expect(hostOf("chrome://extensions/")).toBeNull();
  });
});
