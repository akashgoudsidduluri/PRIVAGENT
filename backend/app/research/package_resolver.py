"""Package resolution module for PrivAgent research artifacts.

Resolves anaphoric and contextual package references:
- "this content"
- "previous analysis"
- "the previous report"
- "this research"
- "it"
- "the above findings"
against persisted research packages in SQLite.

Handles single-package chats, multi-package disambiguation, topic-keyword matching,
and ambiguity clarification without guessing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import db

logger = logging.getLogger("privagent.package_resolver")


@dataclass
class PackageResolutionResult:
    package: Optional[dict[str, Any]] = None
    is_ambiguous: bool = False
    clarification_message: Optional[str] = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    resolution_reason: str = ""


ANAPHORA_PATTERNS = re.compile(
    r"\b(?:this\s+(?:content|research|analysis|report|findings?|data|document|paper)|"
    r"the\s+(?:previous|prior|last|recent|above)\s+(?:analysis|report|research|findings?|data|document|paper|one)|"
    r"previous\s+analysis|prior\s+research|recent\s+report|above\s+findings|it|this|that)\b",
    re.IGNORECASE,
)

RECENCY_ANCHOR_PATTERNS = re.compile(
    r"\b(?:the\s+)?(?:previous|prior|last|recent)\s+(?:analysis|report|research|findings?|one)\b",
    re.IGNORECASE,
)


def resolve_target_package(
    chat_id: Optional[str],
    instruction: str,
    context: Optional[dict[str, Any]] = None,
    requested_format: Optional[str] = "pdf",
    db_path: Optional[str] = None,
    completed_only: bool = False,
) -> PackageResolutionResult:
    """Resolve the target ResearchKnowledgePackage for deliverable export or refinement.

    ``completed_only`` keeps an explicit export from silently selecting a
    package that is still being collected in another task.
    """
    ctx = context or {}
    candidates: list[dict[str, Any]] = []
    seen_pkg_ids: set[str] = set()
    db_errors: list[str] = []

    # 1. Fetch persisted packages from db
    if chat_id:
        try:
            persisted = db.list_packages_for_chat(chat_id, db_path=db_path)
            for p in persisted:
                pid = p.get("package_id")
                if pid and pid not in seen_pkg_ids:
                    seen_pkg_ids.add(pid)
                    if completed_only and p.get("status") not in (None, "completed"):
                        continue
                    candidates.append(p)
        except Exception as exc:
            logger.error("Database error while listing packages for chat %s: %s", chat_id, exc, exc_info=True)
            db_errors.append(f"list_packages: {exc}")

    # 2. Check latest checkpoint in db if not in research_packages table
    if chat_id:
        try:
            cp = db.get_latest_checkpoint_for_chat(chat_id, db_path=db_path)
            if cp and cp.get("working_memory"):
                wm = cp["working_memory"]
                pkg_data = wm.get("research_knowledge_package")
                if pkg_data and isinstance(pkg_data, dict):
                    pid = pkg_data.get("package_id") or f"pkg_{cp.get('task_id', 'unknown')}"
                    if pid not in seen_pkg_ids:
                        seen_pkg_ids.add(pid)
                        if not completed_only or wm.get("research_stage") == "completed":
                            top = pkg_data.get("topic") or {}
                            top_title = top.get("title") if isinstance(top, dict) else getattr(top, "title", "Research Findings")
                            candidates.append({
                                "package_id": pid,
                                "conversation_id": chat_id,
                                "task_id": cp.get("task_id", ""),
                                "topic_title": top_title,
                                "status": "completed",
                                "data": pkg_data,
                                "updated_at": cp.get("updated_at", 0),
                            })
        except Exception as exc:
            logger.error("Database error while fetching latest checkpoint for chat %s: %s", chat_id, exc, exc_info=True)
            db_errors.append(f"get_checkpoint: {exc}")

    # 3. Check memory context package
    ctx_pkg = ctx.get("research_knowledge_package")
    if ctx_pkg and isinstance(ctx_pkg, dict):
        pid = ctx_pkg.get("package_id") or "pkg_context"
        if pid not in seen_pkg_ids:
            seen_pkg_ids.add(pid)
            if not completed_only or ctx.get("research_package_status") in (None, "completed"):
                top = ctx_pkg.get("topic") or {}
                top_title = top.get("title") if isinstance(top, dict) else getattr(top, "title", "Research Findings")
                candidates.append({
                    "package_id": pid,
                    "conversation_id": chat_id or "default",
                    "task_id": ctx.get("task_id", ""),
                    "topic_title": top_title,
                    "status": "completed",
                    "data": ctx_pkg,
                    "updated_at": 0,
                })

    # Case A: No packages found
    if not candidates:
        if db_errors:
            return PackageResolutionResult(
                package=None,
                resolution_reason=f"database_error: {'; '.join(db_errors)}",
            )
        # Check conversation history for a previous report text
        conv_hist = ctx.get("conversation_history") or []
        if not conv_hist and ctx.get("latest_assistant_response"):
            conv_hist = [{"role": "agent", "content": ctx["latest_assistant_response"]}]
        for msg in reversed(conv_hist):
            if msg.get("role") == "agent" and len(str(msg.get("content") or "")) > 60:
                text_content = str(msg["content"])
                title_match = re.search(r"^#\s+(.+)$", text_content, re.MULTILINE)
                title = title_match.group(1).strip() if title_match else (ctx.get("research_topic") or "Technical Analysis")
                synth_pkg = {
                    "package_id": f"pkg_synth_{chat_id or 'default'}",
                    "conversation_id": chat_id or "default",
                    "task_id": "synth",
                    "topic_title": title,
                    "status": "completed",
                    "data": {
                        "package_id": f"pkg_synth_{chat_id or 'default'}",
                        "topic": {
                            "title": title,
                            "objective": title,
                            "findings_summary": text_content,
                            "scope": "Synthesized from prior conversation messages without live external web citations.",
                        },
                        "sources": {},
                        "claims": {},
                        "tables": [],
                        "visuals": [],
                        "formulas": [],
                    },
                    "updated_at": 0,
                }
                return PackageResolutionResult(
                    package=synth_pkg,
                    resolution_reason="synthesized_from_conversation_report",
                )
        return PackageResolutionResult(
            package=None,
            resolution_reason="no_package_found",
        )

    # Case B: Exactly 1 package found
    if len(candidates) == 1:
        return PackageResolutionResult(
            package=candidates[0],
            resolution_reason="single_persisted_package",
        )

    # Case C: Multiple packages found
    # Check if instruction has specific keywords matching exactly one package's title
    inst_lower = (instruction or "").lower()
    keyword_matches: list[dict[str, Any]] = []
    stopwords = {"the", "a", "an", "and", "or", "in", "on", "of", "for", "with", "to", "as", "is", "it", "report", "analysis", "research", "pdf", "docx", "pptx", "xlsx"}
    for p in candidates:
        title_words = set(re.findall(r"[a-zA-Z0-9]+", p.get("topic_title", "").lower())) - stopwords
        matched = False
        for tw in title_words:
            if len(tw) <= 3:
                continue
            stem = tw[:-3] if tw.endswith("ies") else (tw[:-1] if tw.endswith("s") else tw)
            if tw in inst_lower or (len(stem) >= 4 and stem in inst_lower):
                matched = True
                break
        if matched:
            keyword_matches.append(p)

    if len(keyword_matches) == 1:
        return PackageResolutionResult(
            package=keyword_matches[0],
            resolution_reason="topic_keyword_matched_package",
        )

    # If user explicitly said "previous analysis", "the previous report", "the last one"
    if RECENCY_ANCHOR_PATTERNS.search(inst_lower):
        return PackageResolutionResult(
            package=candidates[0],
            resolution_reason="recency_anchor_previous",
        )

    # Genuinely ambiguous (e.g. user just said "generate a PDF" or "bro generate a PDF man" with multiple distinct packages)
    topic_options = [c.get("topic_title", f"Package {i+1}") for i, c in enumerate(candidates[:4])]
    lines = "\n".join(f"{i+1}. **{t}**" for i, t in enumerate(topic_options))
    clarification = (
        f"You have multiple research topics in this session:\n{lines}\n\n"
        f"Which topic would you like to export as a {(requested_format or 'PDF').upper()}?"
    )
    return PackageResolutionResult(
        is_ambiguous=True,
        clarification_message=clarification,
        candidates=candidates,
        resolution_reason="ambiguous_multi_package",
    )

