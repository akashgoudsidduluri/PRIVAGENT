// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { dismissModalInPage } from "../src/actionExecutor";

afterEach(() => {
  document.body.innerHTML = "";
});

function bigRect(el: Element): void {
  (el as HTMLElement).getBoundingClientRect = () => ({
    left: 0, top: 0, width: 400, height: 300, x: 0, y: 0,
    right: 400, bottom: 300, toJSON: () => ({}),
  }) as DOMRect;
}

describe("dismissModalInPage protects a target inside the modal", () => {
  it("does not dismiss a modal that contains the protected target", () => {
    let closed = false;
    document.body.innerHTML = `
      <div role="dialog" id="login-modal">
        <button aria-label="Close">✕</button>
        <input id="email" data-navigator-id="email-field" type="text" aria-label="Email">
      </div>`;
    const modal = document.getElementById("login-modal")!;
    bigRect(modal);
    modal.querySelector('button')!.addEventListener("click", () => { closed = true; });
    expect(dismissModalInPage("email-field")).toBe(false);
    expect(closed).toBe(false);
    expect(document.getElementById("login-modal")).not.toBeNull();
  });

  it("dismisses a modal that does not contain the protected target", () => {
    let closed = false;
    document.body.innerHTML = `
      <input id="search" data-navigator-id="search-box" type="text" aria-label="Search">
      <div role="dialog" id="login-modal">
        <button aria-label="Close">✕</button>
      </div>`;
    const modal = document.getElementById("login-modal")!;
    bigRect(modal);
    modal.querySelector('button')!.addEventListener("click", () => { closed = true; });
    expect(dismissModalInPage("search-box")).toBe(true);
    expect(closed).toBe(true);
  });

  it("dismisses a modal when no target is protected", () => {
    let closed = false;
    document.body.innerHTML = `
      <div role="dialog" id="login-modal">
        <button aria-label="Close">✕</button>
      </div>`;
    const modal = document.getElementById("login-modal")!;
    bigRect(modal);
    modal.querySelector('button')!.addEventListener("click", () => { closed = true; });
    expect(dismissModalInPage("")).toBe(true);
    expect(closed).toBe(true);
  });

  it("returns false when there is no modal at all", () => {
    document.body.innerHTML = `<input id="x" type="text">`;
    expect(dismissModalInPage("x")).toBe(false);
  });
});
