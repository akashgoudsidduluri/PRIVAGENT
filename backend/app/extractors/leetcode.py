"""LeetCode profile extractor.

Extracts structured statistics from LeetCode user profile pages without
fabricating data. Distinguishes 'Unrated' or 'Not Participated' from zero.
"""
from __future__ import annotations

import re
from typing import Any, Optional


def extract_leetcode_profile(
    obs: Any,
    page_text: Optional[str] = None,
) -> dict[str, Any]:
    """Extract structured profile statistics from a LeetCode profile page observation.
    
    Returns a dictionary with:
        - username (str)
        - profile_url (str)
        - total_solved (int | str)
        - easy_solved (int | str)
        - medium_solved (int | str)
        - hard_solved (int | str)
        - contest_rating (float | str) -> 'Unrated' if not participated
        - global_ranking (int | str) -> 'Unavailable' or rank number
        - badges (list[str])
        - recent_submissions (list[dict[str, str]])
    """
    url = (
        obs.get("url") if isinstance(obs, dict)
        else getattr(obs, "url", "")
    ) or "https://leetcode.com/profile/"

    text = page_text or (
        obs.get("pageText") or obs.get("page_text") if isinstance(obs, dict)
        else getattr(obs, "page_text", "")
    ) or ""

    interactive = (
        obs.get("interactive") if isinstance(obs, dict)
        else getattr(obs, "interactive", [])
    ) or []

    # 1. Username extraction
    username: Optional[str] = None
    u_match = re.search(r"leetcode\.com/u/([a-zA-Z0-9_\-]+)", url)
    if u_match:
        username = u_match.group(1)
    else:
        # Check title or interactive elements
        title = (
            obs.get("title") if isinstance(obs, dict)
            else getattr(obs, "title", "")
        ) or ""
        t_match = re.search(r"([a-zA-Z0-9_\-]+)\s*-\s*LeetCode\s*Profile", title, re.IGNORECASE)
        if t_match:
            username = t_match.group(1)

    if not username:
        # Search page text for username heading
        un_match = re.search(r"###\s*([a-zA-Z0-9_\-]{3,30})\b", text)
        if un_match:
            username = un_match.group(1)
        else:
            username = "Authenticated User"

    # 2. Solved Problems Count
    total_solved: Optional[int] = None
    easy_solved: Optional[int] = None
    medium_solved: Optional[int] = None
    hard_solved: Optional[int] = None

    # Matches "Solved 452 / 3120" or "452 / 3120 Solved" or "Total Solved: 452"
    tot_m = re.search(r"(?:Solved\s*[:\n]?\s*(\d+)\s*/|\b(\d+)\s*/\s*\d+\s*Solved|Total Solved\s*[:\n]?\s*(\d+))", text, re.IGNORECASE)
    if tot_m:
        val = tot_m.group(1) or tot_m.group(2) or tot_m.group(3)
        total_solved = int(val)

    # Matches "Easy 180 / 800" or "Easy: 180" or "Easy 180"
    easy_m = re.search(r"Easy\s*[:\n]?\s*(\d+)(?:\s*/\s*\d+)?", text, re.IGNORECASE)
    if easy_m:
        easy_solved = int(easy_m.group(1))

    # Matches "Medium 220 / 1700" or "Medium: 220"
    med_m = re.search(r"Medium\s*[:\n]?\s*(\d+)(?:\s*/\s*\d+)?", text, re.IGNORECASE)
    if med_m:
        medium_solved = int(med_m.group(1))

    # Matches "Hard 52 / 700" or "Hard: 52"
    hard_m = re.search(r"Hard\s*[:\n]?\s*(\d+)(?:\s*/\s*\d+)?", text, re.IGNORECASE)
    if hard_m:
        hard_solved = int(hard_m.group(1))

    # Fallback to interactive element names if not found in text
    if total_solved is None:
        for el in interactive:
            name = (el.get("name") if isinstance(el, dict) else getattr(el, "name", "")) or ""
            if "solved" in name.lower():
                m = re.search(r"(\d+)", name)
                if m:
                    total_solved = int(m.group(1))
                    break

    # If components exist but total is missing, sum them
    if total_solved is None and (easy_solved is not None or medium_solved is not None or hard_solved is not None):
        total_solved = (easy_solved or 0) + (medium_solved or 0) + (hard_solved or 0)

    # 3. Contest Rating & Global Ranking
    # Distinguish Unrated from 0!
    contest_rating: str | float = "Unrated / Not participated"
    global_ranking: str | int = "Unavailable"

    cr_m = re.search(r"Contest Rating\s*[:\n]?\s*([0-9,]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    if cr_m:
        try:
            contest_rating = float(cr_m.group(1).replace(",", ""))
        except ValueError:
            contest_rating = cr_m.group(1)
    elif re.search(r"\b(unrated|not attended|no contests attended)\b", text, re.IGNORECASE):
        contest_rating = "Unrated / Not participated"

    gr_m = re.search(r"Global Ranking\s*[:\n]?\s*([0-9,]+)(?:\s*/\s*[0-9,]+)?", text, re.IGNORECASE)
    if gr_m:
        try:
            global_ranking = int(gr_m.group(1).replace(",", ""))
        except ValueError:
            global_ranking = gr_m.group(1)

    # 4. Badges
    badges: list[str] = []
    b_count_m = re.search(r"(\d+)\s*Badges?", text, re.IGNORECASE)
    badge_count_str = f"{b_count_m.group(1)} Badges" if b_count_m else None

    # Search for specific badge titles in text
    badge_names = re.findall(r"\b((?:\d{2,4}\s+)?(?:Days?|Annual|Guardian|Knight|Monthly)\s+Badge(?:\s+\d{4})?)\b", text, re.IGNORECASE)
    for b in badge_names:
        if b not in badges:
            badges.append(b)
    if not badges and badge_count_str:
        badges.append(badge_count_str)
    if not badges:
        badges = ["None"]

    # 5. Recent Submissions
    recent_submissions: list[dict[str, str]] = []
    # Search for lines indicating submission: e.g. "Two Sum - Accepted - 2 hours ago"
    sub_matches = re.finditer(r"([a-zA-Z0-9\s\-]+)\s*\|\s*(Accepted|Wrong Answer|Time Limit Exceeded)\s*\|\s*([a-zA-Z0-9\s]+ago)", text, re.IGNORECASE)
    for sm in sub_matches:
        recent_submissions.append({
            "title": sm.group(1).strip(),
            "status": sm.group(2).strip(),
            "time": sm.group(3).strip(),
        })

    return {
        "username": username,
        "profile_url": url,
        "total_solved": total_solved if total_solved is not None else "0",
        "easy_solved": easy_solved if easy_solved is not None else "0",
        "medium_solved": medium_solved if medium_solved is not None else "0",
        "hard_solved": hard_solved if hard_solved is not None else "0",
        "contest_rating": contest_rating,
        "global_ranking": global_ranking,
        "badges": badges,
        "recent_submissions": recent_submissions,
    }


def format_leetcode_report(profile: dict[str, Any]) -> str:
    """Format structured LeetCode profile statistics into a clean markdown report."""
    un = profile.get("username", "Authenticated User")
    url = profile.get("profile_url", "https://leetcode.com/profile/")
    tot = profile.get("total_solved", "0")
    easy = profile.get("easy_solved", "0")
    med = profile.get("medium_solved", "0")
    hard = profile.get("hard_solved", "0")
    cr = profile.get("contest_rating", "Unrated")
    gr = profile.get("global_ranking", "Unavailable")
    badges = ", ".join(profile.get("badges", ["None"]))

    report = (
        f"### LeetCode Profile Report for {un}\n\n"
        f"- **Source URL**: {url}\n"
        f"- **Total Problems Solved**: {tot}\n"
        f"  - **Easy**: {easy}\n"
        f"  - **Medium**: {med}\n"
        f"  - **Hard**: {hard}\n"
        f"- **Contest Rating**: {cr}\n"
        f"- **Global Ranking**: {gr}\n"
        f"- **Badges**: {badges}\n"
    )

    subs = profile.get("recent_submissions", [])
    if subs:
        report += "\n**Recent Submissions**:\n"
        for s in subs[:5]:
            report += f"- {s.get('title')} ({s.get('status')}, {s.get('time')})\n"

    return report.strip()

