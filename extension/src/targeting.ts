import type { ElementTarget } from "./contracts";

export interface Candidate {
  targetId: string;
  role: string;
  name: string;
  cssSelector: string;
  enabled: boolean;
  visible: boolean;
  bounds?: { x: number; y: number; width: number; height: number };
  href?: string;
}

const norm = (s: string | undefined) => (s ?? "").trim().toLowerCase();
const cleanId = (s: string | undefined) => (s ?? "").replace(/^#/, "").trim();

/** Resolve an action target against the current observation's element list.
 * Priority: Author targetId (clean or with #) → accessible name (exact → substring → reverse substring → token match) → synthetic targetId → coordinates.
 * `interaction` filters unusable candidates (disabled). */
export function findTarget(
  target: ElementTarget,
  candidates: Candidate[],
  interaction: "click" | "type" | "none" = "none"
): Candidate | null {
  const tid = cleanId(target.targetId || (target as any).id || (target as any).target_id);
  const wantedName = norm(target.name);
  const stem = (s: string) => norm(s).replace(/s$/, "");
  const stemWanted = stem(wantedName);

  // 1. If an author ID exists (not synthetic nav-el-*), match directly
  if (tid && !tid.startsWith("nav-el-")) {
    const byId = candidates.find(c => cleanId(c.targetId) === tid || c.targetId === target.targetId);
    if (byId) {
      return usable(byId, interaction) ? byId : null;
    }
  }

  // 2. Match by accessible name / label if provided
  if (wantedName) {
    // 2a. Exact match or stem match (group vs groups)
    const exact = candidates.find(c => (norm(c.name) === wantedName || (stemWanted.length >= 4 && stem(c.name) === stemWanted)) && usable(c, interaction));
    if (exact) return exact;

    // 2b. Candidate name contains wanted name (e.g. "Realme 15 Pro Emerald Green" contains "realme 15 pro")
    const substring = candidates.find(c => {
      if (!usable(c, interaction)) return false;
      const cn = norm(c.name);
      return cn.length > 2 && (cn.includes(wantedName) || (stemWanted.length >= 4 && stem(cn).includes(stemWanted)));
    });
    if (substring) return substring;

    // 2c. Wanted name contains candidate name (e.g. wanted "send message" contains candidate "send", "first group" contains "groups")
    const reverseSub = candidates.find(c => {
      if (!usable(c, interaction)) return false;
      const cn = norm(c.name);
      return cn.length >= 3 && (wantedName.includes(cn) || (cn.length >= 4 && stemWanted.includes(stem(cn))));
    });
    if (reverseSub) return reverseSub;

    // 2d. Multi-token match (all words in wanted are present in candidate)
    const wantedTokens = wantedName.split(/\s+/).filter(w => w.length > 1);
    if (wantedTokens.length > 1) {
      const tokenMatch = candidates.find(c => {
        if (!usable(c, interaction)) return false;
        const cn = norm(c.name);
        return wantedTokens.every(tok => cn.includes(tok));
      });
      if (tokenMatch) return tokenMatch;

      // 2e. Token overlap matching: candidate name contains majority (>= 60%) of wanted tokens
      // or candidate's tokens are all contained within wantedName
      let bestCandidate: Candidate | null = null;
      let highestScore = 0;
      for (const c of candidates) {
        if (!usable(c, interaction)) continue;
        const cn = norm(c.name);
        const cTokens = cn.split(/\s+/).filter(w => w.length > 1);
        if (cTokens.length === 0) continue;
        const matched = wantedTokens.filter(tok => cn.includes(tok)).length;
        const score = matched / wantedTokens.length;
        const candSubset = cTokens.length >= 2 && cTokens.every(tok => wantedTokens.some(wt => wt.includes(tok)));
        if ((score >= 0.6 && score > highestScore) || candSubset) {
          highestScore = score;
          bestCandidate = c;
        }
      }
      if (bestCandidate) return bestCandidate;
    }
  }

  // 3. Fallback to synthetic targetId (nav-el-*)
  if (tid) {
    const byId = candidates.find(c => cleanId(c.targetId) === tid || c.targetId === target.targetId);
    if (byId && usable(byId, interaction)) {
      if (!wantedName) {
        return byId;
      }
      const cn = norm(byId.name);
      const wantedTokens = wantedName.split(/\s+/).filter(w => w.length > 1);
      const sharesToken = wantedTokens.some(tok => cn.includes(tok));
      const sharesRole = target.role ? byId.role === target.role : true;
      if (sharesToken || (cn.length === 0 && sharesRole) || (!tid.startsWith("nav-el-"))) {
        return byId;
      }
    }
  }

  // 4. Fallback to bounding box coordinates
  if (target.coords) {
    const { x, y } = target.coords;
    const byCoords = candidates.find(c => {
      if (!c.bounds || !usable(c, interaction)) return false;
      const b = c.bounds;
      return x >= b.x && x <= b.x + b.width && y >= b.y && y <= b.y + b.height;
    });
    if (byCoords) return byCoords;
  }
  return null;
}

function usable(c: Candidate, interaction: "click" | "type" | "none"): boolean {
  if (interaction !== "none" && !c.enabled) return false;
  return true;
}
