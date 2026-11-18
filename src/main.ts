import "./styles/main.css";
import "./styles/landing-refresh.css";
import "./taskconsole";

// Workflow 3-step scrubber: click a tab to show its panel.
const flowTabs = [...document.querySelectorAll<HTMLButtonElement>(".flow-tab")];
const flowPanels = [...document.querySelectorAll<HTMLElement>(".flow-panel")];
flowTabs.forEach(tab => {
  tab.addEventListener("click", () => {
    const idx = Number(tab.dataset.flow || 0);
    flowTabs.forEach(t => {
      const active = t === tab;
      t.classList.toggle("is-active", active);
      t.setAttribute("aria-selected", String(active));
    });
    flowPanels.forEach((p, i) => {
      p.hidden = i !== idx;
    });
  });
});

// Nav mobile toggle
const toggle = document.querySelector<HTMLButtonElement>(".nav__toggle");
const links = document.getElementById("navLinks");
toggle?.addEventListener("click", () => {
  const open = links?.classList.toggle("open");
  toggle.setAttribute("aria-expanded", String(!!open));
});
links?.querySelectorAll("a").forEach(a =>
  a.addEventListener("click", () => {
    links.classList.remove("open");
    toggle?.setAttribute("aria-expanded", "false");
  })
);

// Nav scroll state + active link tracking
const nav = document.querySelector<HTMLElement>(".nav");
const onScroll = () => nav?.classList.toggle("is-scrolled", scrollY > 30);
onScroll();
addEventListener("scroll", onScroll, { passive: true });

const navAnchors = [...document.querySelectorAll<HTMLAnchorElement>(".nav__links a")];
const sectionObserver = new IntersectionObserver(
  entries => {
    for (const e of entries) {
      if (e.isIntersecting) {
        navAnchors.forEach(a => a.classList.toggle("is-active", a.hash === `#${e.target.id}`));
      }
    }
  },
  { rootMargin: "-40% 0px -55% 0px" }
);
document.querySelectorAll("main section[id]").forEach(s => sectionObserver.observe(s));

const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
if (reducedMotion) {
  document.querySelectorAll(".reveal").forEach(el => el.classList.add("is-visible"));
} else {
  const revealObserver = new IntersectionObserver(
    entries => {
      for (const e of entries) {
        if (e.isIntersecting) {
          e.target.classList.add("is-visible");
          revealObserver.unobserve(e.target);
        }
      }
    },
    { rootMargin: "0px 0px -10% 0px", threshold: 0.05 }
  );
  document.querySelectorAll(".reveal:not(.hero .reveal)").forEach(el => revealObserver.observe(el));
}
