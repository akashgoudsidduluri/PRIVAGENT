// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import "../lib/content";
import { validateAction } from "../src/contracts";

const agent = () => window.__navigatorAgent as any;
const rect = { x: 10, y: 10, top: 10, left: 10, right: 110, bottom: 40, width: 100, height: 30, toJSON: () => ({}) } as DOMRect;
afterEach(() => { document.body.innerHTML = ""; });

describe("production browser controls", () => {
  it("selects an option from a portal and verifies the control updated", async () => {
    document.body.innerHTML = '<button id="filter" aria-label="Sort">Choose</button><div id="portal"></div>';
    const button = document.getElementById("filter")!;
    button.getBoundingClientRect = () => rect;
    button.addEventListener("click", () => {
      document.getElementById("portal")!.innerHTML = '<div role="option">Newest</div>';
      const option = document.querySelector('[role="option"]')!;
      option.getBoundingClientRect = () => rect;
      option.addEventListener("click", () => { button.textContent = "Newest"; });
    });
    agent().list();
    expect((await agent().selectOption("filter", "Newest", "Sort")).ok).toBe(true);
  });

  it("rejects an option when the site did not update its display", async () => {
    document.body.innerHTML = '<button id="filter">Choose</button><div role="option">Newest</div>';
    document.querySelector('[role="option"]')!.getBoundingClientRect = () => rect;
    agent().list();
    expect((await agent().selectOption("filter", "Newest")).ok).toBe(false);
  });

  it("formats text calendars and fires change events", async () => {
    document.body.innerHTML = '<input id="when" placeholder="DD/MM/YYYY">';
    const input = document.getElementById("when") as HTMLInputElement;
    let changed = false;
    input.addEventListener("change", () => { changed = true; });
    agent().list();
    expect((await agent().setDate("when", "2026-12-31")).value).toBe("31/12/2026");
    expect(changed).toBe(true);
  });

  it("navigates a custom calendar month to a unique labelled day", async () => {
    document.body.innerHTML = '<button id="date">Choose date</button><div id="calendar"></div>';
    const control = document.getElementById("date")!;
    const calendar = document.getElementById("calendar")!;
    control.addEventListener("click", () => {
      calendar.innerHTML = '<div data-calendar-month>November 2026</div><button aria-label="Next month">Next</button>';
      calendar.querySelector("button")!.addEventListener("click", () => {
        calendar.innerHTML = '<div data-calendar-month>December 2026</div><button aria-label="2026-12-31">31</button>';
        calendar.querySelector("button")!.getBoundingClientRect = () => rect;
        calendar.querySelector("button")!.addEventListener("click", () => { control.textContent = "31 Dec 2026"; });
      });
    });
    agent().list();
    expect((await agent().setDate("date", "2026-12-31")).ok).toBe(true);
    expect(control.textContent).toBe("31 Dec 2026");
  });

  it("scrolls a focused modal independently of the page", () => {
    document.body.innerHTML = '<div id="modal" role="dialog" style="overflow-y:auto" tabindex="0"><button>Item</button></div>';
    const modal = document.getElementById("modal")!;
    Object.defineProperties(modal, { clientHeight: { value: 100 }, scrollHeight: { value: 500 } });
    modal.focus();
    expect(agent().scroll("down").scrollY).toBe(80);
    expect(modal.scrollTop).toBe(80);
  });

  it("sets an approved file input and rejects oversized payloads", () => {
    document.body.innerHTML = '<input id="file" type="file">';
    const input = document.getElementById("file") as HTMLInputElement;
    let files: File[] = [];
    Object.defineProperty(input, "files", { configurable: true, get: () => files, set: (value: File[]) => { files = value; } });
    const oldTransfer = (globalThis as any).DataTransfer;
    (globalThis as any).DataTransfer = class {
      files: File[] = [];
      items = { add: (file: File) => this.files.push(file) };
    };
    agent().list();
    const result = agent().uploadFile("file", "report.txt", btoa("hello"));
    (globalThis as any).DataTransfer = oldTransfer;
    expect(result).toEqual({ ok: true, value: "report.txt" });
    expect(input.files?.[0].size).toBe(5);
    const action = { type: "upload_file", risk: "sensitive", verify: "file selected", timeoutMs: 1000,
      target: { targetId: "file" }, params: { fileName: "evil.exe", base64: btoa("hello") } };
    expect(validateAction(action).ok).toBe(false);
  });
});
