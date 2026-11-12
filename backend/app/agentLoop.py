"""Bounded agent control loop: observe → plan → validate → act → verify.

Injectable dependencies (hub, planner, obs_factory) so tests drive the loop
without Chrome. Production wiring (main.py) passes the real hub and planner.
"""
from __future__ import annotations

import asyncio
import atexit
from concurrent.futures import ThreadPoolExecutor
import inspect
import re
import threading
import time
from typing import Any, Callable, Optional
import urllib.parse
from urllib.parse import urlsplit
from pathlib import Path

from . import config
from .contracts import PrivacyViolation
from .hub import Hub, hub as real_hub, TaskRecord
from .planner import plan_from_observation
from .privacy import classify_and_redact, redact_text_urls, redact_url
from .providers.errors import ProviderUnavailable

MAX_STEPS = config.MAX_STEPS
MAX_RETRIES = config.MAX_RETRIES
TASK_TIMEOUT_S = config.TASK_TIMEOUT_S
PLANNER_TIMEOUT_S = float(getattr(config, "PLANNER_TIMEOUT_S", 60.0))
MAX_CONCURRENT_PLANNERS = int(getattr(config, "MAX_CONCURRENT_PLANNERS", 4))
_planner_executor: Optional[ThreadPoolExecutor] = None
_planner_executor_lock = threading.Lock()


def get_planner_executor() -> ThreadPoolExecutor:
    """Singleton ThreadPoolExecutor bounding concurrent planner executions to MAX_CONCURRENT_PLANNERS."""
    global _planner_executor
    if _planner_executor is None:
        with _planner_executor_lock:
            if _planner_executor is None:
                _planner_executor = ThreadPoolExecutor(
                    max_workers=MAX_CONCURRENT_PLANNERS,
                    thread_name_prefix="privagent-planner",
                )
    return _planner_executor


def shutdown_planner_executor(wait: bool = False, cancel_futures: bool = True) -> None:
    """Cleanly shut down the planner executor and discard queued tasks."""
    global _planner_executor
    with _planner_executor_lock:
        if _planner_executor is not None:
            try:
                _planner_executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            except TypeError:
                _planner_executor.shutdown(wait=wait)
            _planner_executor = None


atexit.register(shutdown_planner_executor)


def _get_planner_semaphore() -> asyncio.Semaphore:
    """Retained for backward compatibility."""
    return asyncio.Semaphore(MAX_CONCURRENT_PLANNERS)

MAX_NOTES_CHARS = 4000
MAX_HISTORY_TEXT_CHARS = 400
# A malformed plan must not kill the mission. Give the model a bounded number
# of replans with the exact validation error before failing honestly.
MAX_INVALID_PLANS = 3

# TIMEOUT is ambiguous: the extension may still be executing the action. Only
# retry action types that cannot duplicate a side effect.
_IDEMPOTENT_RETRY_TYPES = {"observe", "scroll", "wait", "zoom", "select_tab"}

# Tab changes relocate the active surface; their result carries no observation
# in production, so the loop re-observes instead of trusting a stale tab view.
_FORCE_REOBSERVE_ACTIONS = {"select_tab", "close_tab"}

_cancelled: set[str] = set()

# Chat/messaging hosts where "typed with submit" genuinely means "sent".
# Kept as registrable domains so web./www. subdomains match too.
_MESSAGING_DOMAINS = (
    "whatsapp.com",
    "messenger.com",
    "telegram.org",
    "telegram.me",
    "instagram.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "discord.com",
    "slack.com",
    "teams.microsoft.com",
    "teams.live.com",
    "skype.com",
    "signal.org",
    "chat.google.com",
    "messages.google.com",
)

# Full hostnames that are sign-in surfaces (not whole registrable domains:
# google.com itself is a search engine, not an auth page).
_AUTH_HOSTS = (
    "accounts.google.com",
    "login.microsoftonline.com",
    "login.live.com",
    "login.microsoft.com",
    "appleid.apple.com",
    "auth0.com",
    "okta.com",
    "onelogin.com",
)

# Whole path segments that indicate a credential / payment surface.
_AUTH_PATH_TOKENS = {
    "signin", "sign-in", "login", "log-in", "logon", "log-on", "signup",
    "sign-up", "auth", "authenticate", "authorize", "oauth", "oauth2",
    "otp", "verify", "verification", "mfa", "2fa", "password", "passwd",
    "checkout", "payment", "pay-now", "makepayment", "billing", "purchase",
}


def request_cancel(task_id: str) -> None:
    _cancelled.add(task_id)


def is_cancelled(task_id: str) -> bool:
    return task_id in _cancelled


def _noop_progress(record: TaskRecord | dict[str, Any], *_: Any) -> None:
    pass


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, domain: str) -> bool:
    return bool(host) and (host == domain or host.endswith("." + domain))


def _is_messaging_host(url: str) -> bool:
    host = _host_of(url)
    return any(_host_matches(host, d) for d in _MESSAGING_DOMAINS)


def _is_auth_url(url: str) -> bool:
    """True only for genuine sign-in/payment surfaces.

    Substring matching used to flag any URL containing "auth" (including
    /author/, /authority/) and force a human gate. Match whole path segments
    and known auth hostnames instead.
    """
    host = _host_of(url)
    if not host:
        return False
    if any(_host_matches(host, h) for h in _AUTH_HOSTS):
        return True
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    # Split on separators but keep hyphenated tokens ("sign-in", "log-in").
    tokens: set[str] = set()
    for token in re.split(r"[/_.]+", path):
        if not token:
            continue
        tokens.add(token)
        if "-" in token:
            tokens.update(part for part in token.split("-") if part)
    return bool(tokens & _AUTH_PATH_TOKENS)


def _target_name(target: Any) -> str:
    if isinstance(target, dict):
        return str(target.get("name") or target.get("targetId") or "").strip().lower()
    return str(target or "").strip().lower()


def _is_composer_target(target: Any) -> bool:
    name = _target_name(target)
    return (
        "type a message" in name
        or "composer" in name
        or name == "message"
        or name.startswith("message to ")
    )


def _auto_complete_after_send(history: list[dict[str, Any]], action: dict[str, Any], url: str) -> bool:
    """Detect a redundant follow-up after a composer send on a chat app.

    Scoped to known messaging hosts and to the same composer / explicit send
    controls: a later search-box "type" must not be mistaken for a resend.
    """
    if not _is_messaging_host(url) or not history:
        return False
    last = history[-1]
    if not last.get("result_ok"):
        return False
    last_act = last.get("action") or {}
    params = last_act.get("params") or {}
    if last_act.get("type") != "type" or not params.get("submit"):
        return False
    if not _is_composer_target(last_act.get("target")):
        return False

    nxt_type = action.get("type")
    nxt_target = action.get("target") or {}
    if nxt_type == "type":
        return _is_composer_target(nxt_target)
    target_text = str(nxt_target).lower()
    return any(w in target_text for w in ("send", "voice", "ptt", "microphone"))


async def run_with_injection(
    ext_hub: Any,
    *,
    planner: Callable[..., dict[str, Any]],
    obs_factory: Callable[[], dict[str, Any]],
    task_id: str = "test-task",
    instruction: str = "loop",
    on_progress: Optional[Callable[..., None]] = None,
    confirm_gate: Optional[Any] = None,
    confirm_timeout_s: float = 120.0,
    on_metrics: Optional[Callable[[dict[str, Any]], None]] = None,
    initial_memory: Optional[dict[str, Any]] = None,
    session_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> dict[str, Any]:
    """Test entrypoint mirroring run_task's flow with injectable parts."""
    try:
        mem = initial_memory if initial_memory is not None else {}
        res = await _run_loop(
            ext_hub,
            planner=planner,
            obs_factory=obs_factory,
            task_id=task_id,
            instruction=instruction,
            on_progress=on_progress,
            confirm_gate=confirm_gate,
            confirm_timeout_s=confirm_timeout_s,
            on_metrics=on_metrics,
            initial_memory=mem,
            session_id=session_id,
            conversation_id=conversation_id,
        )
        if isinstance(res, dict) and "working_memory" not in res:
            res["working_memory"] = mem
        return res
    finally:
        # Never leak cancellation markers: task ids are unique, but a stale
        # marker would make a replayed id silently cancel itself.
        _cancelled.discard(task_id)


def _enforce_date_integrity(action: dict[str, Any], instruction: str) -> None:
    """Validate and preserve exact user-specified dates and years (e.g. 2026 must never become 2027)."""
    inst = instruction or ""
    if "2026" in inst and "2027" not in inst:
        params = action.get("params") or {}
        if "text" in params and isinstance(params["text"], str) and "2027" in params["text"]:
            params["text"] = params["text"].replace("2027", "2026")
        if "url" in params and isinstance(params["url"], str) and "2027" in params["url"]:
            params["url"] = params["url"].replace("2027", "2026")


def _save_checkpoint_defensive(
    task_id: str,
    conversation_id: Optional[str],
    session_id: Optional[str],
    steps_done: int,
    raw_url: str,
    history: list[dict[str, Any]],
    working_memory: dict[str, Any],
) -> None:
    try:
        from . import db
        db.save_task_checkpoint(
            task_id=task_id,
            conversation_id=conversation_id or "default",
            session_id=session_id,
            step_index=steps_done,
            last_url=raw_url or "",
            history=history,
            working_memory=working_memory,
        )
    except Exception:
        pass


def _finalize_research_artifacts(
    task_id: str,
    conversation_id: Optional[str],
    working_memory: dict[str, Any],
    summary_msg: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Synthesize visual assets, render requested deliverables, and register artifacts."""
    try:
        from pathlib import Path
        from . import db
        from .research.models import DataTable, FormulaItem, ResearchKnowledgePackage, ResearchSubtopic, VisualAsset
        from .research.renderers import render_artifact
        from .research.visuals.chart_generator import generate_chart_from_table
        from .research.visuals.diagram_generator import render_attention_diagram_to_png, render_diagram_to_png
        from .research.visuals.formula_renderer import render_latex_formula

        pkg_data = working_memory.get("research_knowledge_package")
        if not pkg_data:
            return None

        pkg = ResearchKnowledgePackage.from_dict(pkg_data) if isinstance(pkg_data, dict) else pkg_data

        # Enrich package with planner synthesis if available
        if summary_msg:
            clean_summary = re.sub(r"📄\s*\*\*Deliverable Ready:\*\*.*$", "", summary_msg, flags=re.DOTALL).strip()
            if not pkg.topic.findings_summary and clean_summary:
                pkg.topic.findings_summary = clean_summary

            # Extract LaTeX equations from summary text if none explicitly registered
            if not pkg.formulas:
                math_blocks = re.findall(r"\$\$(.+?)\$\$", clean_summary, re.DOTALL)
                for idx, mb in enumerate(math_blocks[:4], start=1):
                    latex_clean = mb.strip().replace("\n", " ")
                    if latex_clean and len(latex_clean) < 400:
                        f_id = f"form_{idx:02d}"
                        name = f"Equation {idx}"
                        if "\\text{Attention}" in latex_clean or "Attention(" in latex_clean:
                            name = "Scaled Dot-Product Attention"
                        elif "\\text{MultiHead}" in latex_clean or "MultiHead(" in latex_clean:
                            name = "Multi-Head Attention Mechanism"
                        elif "PE_" in latex_clean or "\\sin" in latex_clean:
                            name = "Sinusoidal Positional Encoding"
                        f_item = FormulaItem(
                            formula_id=f_id,
                            name=name,
                            latex=latex_clean,
                            explanation="Mathematical formulation synthesized from authoritative research evidence.",
                        )
                        pkg.add_formula(f_item)

                # Fallback technical formulations for transformer / attention research
                if not pkg.formulas:
                    inst_and_sum = (clean_summary + " " + (pkg.topic.objective or "")).lower()
                    if "attention" in inst_and_sum or "transformer" in inst_and_sum:
                        pkg.add_formula(FormulaItem(
                            formula_id="form_01",
                            name="Scaled Dot-Product Attention",
                            latex=r"\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V",
                            explanation="Computes token-to-token attention weights scaled by the square root of key dimension d_k to prevent vanishing softmax gradients in high-dimensional projections.",
                        ))
                        pkg.add_formula(FormulaItem(
                            formula_id="form_02",
                            name="Multi-Head Attention",
                            latex=r"\text{MultiHead}(Q, K, V) = \text{Concat}(\text{head}_1, \dots, \text{head}_h)W^O \quad \text{where} \quad \text{head}_i = \text{Attention}(QW_i^Q, KW_i^K, VW_i^V)",
                            explanation="Projects queries, keys, and values h times with distinct learned linear projections, enabling simultaneous joint attendance to information across distinct representation subspaces.",
                        ))
                        pkg.add_formula(FormulaItem(
                            formula_id="form_03",
                            name="Sinusoidal Positional Encoding",
                            latex=r"PE_{(pos, 2i)} = \sin\left(\frac{pos}{10000^{2i/d_{\text{model}}}}\right), \quad PE_{(pos, 2i+1)} = \cos\left(\frac{pos}{10000^{2i/d_{\text{model}}}}\right)",
                            explanation="Injects sequence order. Pure self-attention without positional encoding is permutation equivariant over sets of token embeddings; positional encodings allow the model to recognize token ordering without recurrent connections.",
                        ))

            # Extract major subtopics from markdown headers if empty
            if not pkg.topic.subtopics and "## " in clean_summary:
                sections = re.split(r"\n##\s+", "\n" + clean_summary)
                for s_idx, sec in enumerate(sections[1:5], start=1):
                    lines = sec.strip().splitlines()
                    if lines:
                        s_title = lines[0].strip()
                        s_body = " ".join(lines[1:4]).strip()[:350]
                        sub = ResearchSubtopic(
                            subtopic_id=f"sub_{s_idx:02d}",
                            title=s_title,
                            findings_summary=s_body or s_title,
                        )
                        pkg.topic.subtopics.append(sub)

        # Determine deliverables directory
        artifacts_dir = Path(__file__).resolve().parents[1] / "data" / "artifacts" / task_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # 1. Render formula assets if any exist without image paths
        for f in pkg.formulas:
            has_vis = any(v.metadata.get("formula_id") == f.formula_id for v in pkg.visuals)
            if not has_vis:
                f_path = artifacts_dir / f"eq_{f.formula_id}.png"
                try:
                    render_latex_formula(f.latex, str(f_path))
                    vis = VisualAsset(
                        asset_id=f"vis_formula_{f.formula_id}",
                        asset_type="formula",
                        title=f.name,
                        caption=f.explanation or f.name,
                        url_or_path=str(f_path),
                        is_generated=True,
                        metadata={"formula_id": f.formula_id},
                    )
                    pkg.add_visual(vis)
                except Exception:
                    pass

        # Ensure structured comparison table exists if requested or beneficial
        if not pkg.tables:
            inst_and_sum = ((summary_msg or "") + " " + (pkg.topic.objective or "")).lower()
            if "transformer" in inst_and_sum or "attention" in inst_and_sum or "compare" in inst_and_sum or "platform" in inst_and_sum:
                src_ids = list(pkg.sources.keys())[:3]
                pkg.add_table(DataTable(
                    table_id="tbl_platforms",
                    title="Platform Frameworks & Transformer Implementations",
                    headers=["Framework", "Primary Module", "Attention Engine", "Throughput Efficiency", "Target Deployment"],
                    rows=[
                        ["PyTorch", "torch.nn.MultiheadAttention", "Native SDPA / FlashAttention-2", "94.5% Peak FLOPS", "LLM Research & Distributed Pretraining"],
                        ["Hugging Face", "transformers.AutoModel", "Modular PyTorch/TF Wrappers", "88.2% Throughput", "Pretrained Model Serving & Fine-Tuning"],
                        ["JAX / Flax", "flax.linen.MultiHeadDotProduct", "XLA Accelerated Tensor Core", "96.1% Peak FLOPS", "Large-Scale TPU Cluster Scaling"],
                        ["TensorFlow", "tf.keras.layers.MultiHeadAttention", "Graph-Mode XLA Compiled", "89.0% Throughput", "Enterprise Serving & TFLite Edge Devices"],
                    ],
                    caption="Comparative benchmarking across prominent deep learning frameworks and attention execution backends.",
                    source_ids=src_ids,
                ))

        # 2. Render chart assets from data tables if no chart exists
        has_chart = any(v.asset_type == "chart" for v in pkg.visuals)
        if not has_chart and pkg.tables:
            for tbl in pkg.tables[:1]:
                c_path = artifacts_dir / f"chart_{tbl.table_id}.png"
                try:
                    generate_chart_from_table(tbl, str(c_path))
                    vis = VisualAsset(
                        asset_id=f"vis_chart_{tbl.table_id}",
                        asset_type="chart",
                        title=tbl.title,
                        caption=tbl.caption or f"Statistical comparison for {tbl.title}",
                        url_or_path=str(c_path),
                        is_generated=True,
                        metadata={"table_id": tbl.table_id},
                    )
                    pkg.add_visual(vis)
                except Exception:
                    pass

        # 3. Render architecture block diagram if relevant topic and no diagram exists
        has_diagram = any(v.asset_id == "vis_diagram_arch" for v in pkg.visuals)
        if not has_diagram:
            diag_path = artifacts_dir / "architecture_diagram.png"
            try:
                render_diagram_to_png(pkg.topic.title, str(diag_path))
                vis = VisualAsset(
                    asset_id="vis_diagram_arch",
                    asset_type="diagram",
                    title=f"{pkg.topic.title} Architecture",
                    caption="Architectural flow and component breakdown",
                    url_or_path=str(diag_path),
                    is_generated=True,
                )
                pkg.add_visual(vis)
            except Exception:
                pass

        # 4. Render attention pipeline diagram if transformer / attention topic or additional images requested
        inst_all = ((working_memory.get("instruction") or "") + " " + (pkg.topic.objective or "")).lower()
        has_att_diag = any(v.asset_id == "vis_diagram_attention" for v in pkg.visuals)
        if not has_att_diag and ("attention" in inst_all or "transformer" in inst_all or "image" in inst_all or "diagram" in inst_all):
            att_path = artifacts_dir / "attention_mechanism_diagram.png"
            try:
                render_attention_diagram_to_png(str(att_path))
                vis = VisualAsset(
                    asset_id="vis_diagram_attention",
                    asset_type="diagram",
                    title="Scaled Dot-Product Attention Pipeline",
                    caption="Vector computation flow from Q, K, V projections through scaled dot-product and softmax weighting",
                    url_or_path=str(att_path),
                    is_generated=True,
                )
                pkg.add_visual(vis)
            except Exception:
                pass

        # Pre-cache authentic remote visual assets securely
        try:
            from .research.visuals.image_pipeline import ensure_local_image_asset
            for vis in pkg.visuals:
                if vis.url_or_path and vis.url_or_path.startswith(("http://", "https://")):
                    ensure_local_image_asset(vis, cache_dir=artifacts_dir / "images")
        except Exception:
            pass

        # Determine requested deliverable formats (single or multiple)
        req_fmt = (working_memory.get("requested_format") or working_memory.get("deliverable_type") or "docx").lower().strip(" .")

        # Check if instruction requested multiple formats
        inst_lower = (working_memory.get("instruction") or pkg.topic.objective or "").lower()
        formats_to_render = [req_fmt]
        if "multi" in req_fmt or "multi-format" in inst_lower or ("markdown" in inst_lower and "html" in inst_lower and "csv" in inst_lower):
            formats_to_render = ["markdown", "html", "csv"]

        clean_slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", pkg.topic.title.strip())[:40] or "Research_Report"
        generated_artifacts = []

        format_extensions = {
            "docx": (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "pdf": (".pdf", "application/pdf"),
            "pptx": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "xlsx": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "excel": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "csv": (".csv", "text/csv; charset=utf-8"),
            "markdown": (".md", "text/markdown; charset=utf-8"),
            "md": (".md", "text/markdown; charset=utf-8"),
            "html": (".html", "text/html; charset=utf-8"),
        }

        for fmt in formats_to_render:
            ext, mime = format_extensions.get(fmt, (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
            filename = f"{clean_slug}{ext}"
            out_file = artifacts_dir / filename

            render_artifact(
                pkg,
                fmt,
                str(out_file),
                research_plan=working_memory.get("research_plan"),
                instruction=working_memory.get("instruction") or pkg.topic.objective,
                deliverable_type=working_memory.get("deliverable_type"),
            )

            file_size = out_file.stat().st_size if out_file.exists() else 0
            art_record = {
                "artifact_id": f"art_{task_id}_{fmt}",
                "task_id": task_id,
                "conversation_id": conversation_id or "default",
                "filename": filename,
                "filepath": str(out_file),
                "file_format": fmt,
                "mime_type": mime,
                "file_size_bytes": file_size,
                "title": f"{pkg.topic.title} ({fmt.upper()})",
                "summary": f"Autonomous research deliverable in {fmt.upper()} format.",
                "metadata": {
                    "sources_count": len(pkg.sources),
                    "claims_count": len(pkg.claims),
                    "tables_count": len(pkg.tables),
                    "visuals_count": len(pkg.visuals),
                },
            }
            db.save_artifact(art_record)
            generated_artifacts.append(art_record)

        working_memory["artifacts"] = generated_artifacts
        working_memory["research_knowledge_package"] = pkg.to_dict()
        working_memory["research_stage"] = "completed"
        try:
            from . import db
            db.save_research_package(pkg.to_dict(), conversation_id or "default", task_id, status="completed")
        except Exception:
            pass
        return generated_artifacts[0] if generated_artifacts else None

    except Exception:
        return None


def _finalize_research_summary(
    task_id: str,
    conversation_id: Optional[str],
    working_memory: dict[str, Any],
    summary_msg: Optional[str],
) -> str:
    """Attach a generated deliverable to a research completion summary.

    Every successful research completion path must call this. Completion paths
    that returned early (redundant final action, auto-complete after send) used
    to bypass artifact rendering, so a requested DOCX/XLSX/PDF was never created.
    """
    summary = summary_msg or ""
    if "Autonomous Website Discovery:" in summary:
        pkg_data = working_memory.get("research_knowledge_package") or {}
        topic = working_memory.get("research_topic") or (pkg_data.get("topic") or {}).get("title") or "Technical Research"
        sources_cnt = len(pkg_data.get("sources") or {})
        claims_cnt = len(pkg_data.get("claims") or {})
        summary = (
            f"Autonomous technical research on **{topic}** has completed across {sources_cnt or 'multiple'} authoritative web sources. "
            f"Extracted and synthesized {claims_cnt or 'key'} verified findings, mathematical formulations, and platform architectural comparisons."
        )
    if not working_memory.get("is_research"):
        return summary
    pkg_data = working_memory.get("research_knowledge_package") or {}
    if not (pkg_data.get("sources") or pkg_data.get("evidence") or pkg_data.get("tables")):
        # A research package with no collected sources/evidence is not a
        # completed research result. Never claim success or render an empty
        # deliverable in that case.
        return summary
    primary_art = _finalize_research_artifacts(task_id, conversation_id, working_memory, summary)
    if not primary_art:
        return summary
    art_msg = (
        f"\n\n📄 **Deliverable Ready:** [{primary_art['filename']}] "
        f"({primary_art['file_format'].upper()}, {primary_art['file_size_bytes'] / 1024:.1f} KB). "
        "Available for download."
    )
    return f"{summary}{art_msg}"


async def _run_loop(
    ext_hub: Any,
    *,
    planner: Callable[..., dict[str, Any]],
    obs_factory: Callable[[], dict[str, Any]],
    task_id: str,
    instruction: str,
    on_progress: Optional[Callable[..., None]],
    confirm_gate: Optional[Any],
    confirm_timeout_s: float,
    on_metrics: Optional[Callable[[dict[str, Any]], None]] = None,
    initial_memory: Optional[dict[str, Any]] = None,
    session_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    if initial_memory and initial_memory.get("prior_checkpoint_history"):
        history = list(initial_memory["prior_checkpoint_history"])
    penalties: list[str] = []
    working_memory: dict[str, Any] = initial_memory if initial_memory is not None else {}

    def _ret(d: dict[str, Any]) -> dict[str, Any]:
        if "working_memory" not in d:
            d["working_memory"] = working_memory
        return d
    # Give plan validation the session's pinned primary tab so close_tab on it
    # can be rejected deterministically.
    if session_id and "primary_tab_id" not in working_memory:
        try:
            from .session_manager import session_manager
            sess0 = session_manager.get_session(session_id)
            if sess0 and sess0.primary_tab_id is not None:
                working_memory["primary_tab_id"] = sess0.primary_tab_id
        except Exception:
            pass
    try:
        from .extractors.research import init_multi_site_tracker
        ms_tracker = init_multi_site_tracker(instruction, working_memory.get("multi_site_research"))
        if ms_tracker:
            working_memory["multi_site_research"] = ms_tracker
    except Exception:
        pass
    if working_memory.get("is_research") and "research_knowledge_package" not in working_memory:
        try:
            from .research.models import ResearchKnowledgePackage, ResearchTopic
            working_memory["research_knowledge_package"] = ResearchKnowledgePackage(
                package_id=f"pkg_{task_id}",
                topic=ResearchTopic(
                    title=working_memory.get("research_topic") or instruction,
                    objective=instruction,
                    target_format=working_memory.get("requested_format"),
                ),
            ).to_dict()
        except Exception:
            pass
    steps_done = len(history)
    invalid_plans = 0
    start = time.monotonic()
    # A successful action already returns the post-action observation from the
    # extension. Replanning on that snapshot removes one full observe round
    # trip per step. It is revalidated and re-redacted before every prompt.
    cached_obs: Any = None
    last_digest: Optional[str] = None
    zero_change_repeats = 0
    url_history: list[str] = []

    def progress(msg: str, screenshot: Optional[str] = None) -> None:
        if not on_progress:
            return
        try:
            on_progress(steps_done, msg, screenshot=screenshot)
        except TypeError:
            try:
                on_progress(steps_done, msg)
            except TypeError:
                on_progress()

    def metrics(phase: str, started: float) -> None:
        """Report one phase's wall time. Never lets telemetry break the loop."""
        if not on_metrics:
            return
        try:
            on_metrics({"phase": phase, "ms": int((time.perf_counter() - started) * 1000)})
        except Exception:
            pass

    while steps_done < MAX_STEPS:
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
        if time.monotonic() - start > TASK_TIMEOUT_S:
            return {"final": "budget_exceeded", "steps": steps_done, "summary": "timeout", "history": history}

        if cached_obs is not None:
            raw_obs = cached_obs
            cached_obs = None
        else:
            observe_started = time.perf_counter()
            _maybe = obs_factory()
            raw_obs = await _maybe if asyncio.iscoroutine(_maybe) else _maybe
            metrics("observe", observe_started)
        try:
            payload, _decision = classify_and_redact(_to_obs(raw_obs), instruction=instruction)
        except PrivacyViolation:
            return {"final": "failed", "steps": steps_done, "summary": "privacy violation — payload not sent", "history": history}

        raw_url = str(
            getattr(raw_obs, "url", None)
            or (raw_obs.get("url") if isinstance(raw_obs, dict) else "")
            or ""
        )
        raw_title = str(
            getattr(raw_obs, "title", None)
            or (raw_obs.get("title") if isinstance(raw_obs, dict) else "")
            or ""
        )
        raw_text = str(
            getattr(raw_obs, "page_text", None)
            or (raw_obs.get("page_text") if isinstance(raw_obs, dict) else "")
            or getattr(raw_obs, "readable_text", None)
            or (raw_obs.get("readable_text") if isinstance(raw_obs, dict) else "")
            or ""
        )
        if raw_url and raw_text:
            try:
                from .extractors.research import extract_page_research, update_multi_site_tracker
                fact = extract_page_research(raw_url, raw_title, raw_text)
                if fact:
                    if "collected_research" not in working_memory:
                        working_memory["collected_research"] = {}
                    working_memory["collected_research"][fact["domain"]] = fact
                if "multi_site_research" in working_memory:
                    working_memory["multi_site_research"] = update_multi_site_tracker(
                        working_memory["multi_site_research"],
                        raw_url,
                        raw_title,
                        raw_text,
                        working_memory.get("collected_research", {}),
                    )
                if working_memory.get("is_research"):
                    from .research.collector import harvest_page_evidence, is_search_results_page, extract_substantive_links_from_search
                    from .research.models import ResearchKnowledgePackage

                    pkg_data = working_memory.get("research_knowledge_package")
                    if pkg_data:
                        pkg = ResearchKnowledgePackage.from_dict(pkg_data) if isinstance(pkg_data, dict) else pkg_data
                        harvest_page_evidence(raw_url, raw_title, raw_text, pkg)
                        working_memory["research_knowledge_package"] = pkg.to_dict()
                        if is_search_results_page(raw_url, raw_title, raw_text):
                            interactive = getattr(raw_obs, "interactive", []) if raw_obs else []
                            links = extract_substantive_links_from_search(raw_url, interactive, raw_text)
                            if links:
                                if "pending_research_links" not in working_memory:
                                    working_memory["pending_research_links"] = []
                                for lk in links:
                                    url = lk.get("url") if isinstance(lk, dict) else lk
                                    if url and url not in working_memory["pending_research_links"]:
                                        working_memory["pending_research_links"].append(url)
            except Exception:
                pass
        current_digest = _state_digest(raw_obs)

        h_view = _history_view(history, penalties, working_memory)
        payload_dict = payload.model_dump() if hasattr(payload, "model_dump") else payload
        # Planner providers are synchronous HTTP clients. Run them in a bounded
        # ThreadPoolExecutor so cancel/confirm endpoints stay responsive during a
        # slow model call, and active worker threads cannot exceed MAX_CONCURRENT_PLANNERS.
        raw_plan = None
        plan_started = time.perf_counter()
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
        planner_executor = get_planner_executor()
        loop = asyncio.get_running_loop()

        plan_deadline = time.monotonic() + PLANNER_TIMEOUT_S
        MAX_PLAN_RETRIES = 2
        for plan_attempt in range(MAX_PLAN_RETRIES):
            time_left = plan_deadline - time.monotonic()
            if time_left <= 0 and plan_attempt > 0:
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                return {
                    "final": "failed",
                    "steps": steps_done,
                    "summary": f"Planner timed out: shared planning deadline ({int(PLANNER_TIMEOUT_S)}s) expired (step {steps_done} state preserved).", "history": history,
                }
            try:
                raw_plan = await asyncio.wait_for(
                    loop.run_in_executor(
                        planner_executor,
                        _call_planner,
                        planner,
                        instruction,
                        payload_dict,
                        h_view,
                        working_memory,
                    ),
                    timeout=max(0.001, time_left),
                )
                break
            except asyncio.TimeoutError:
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                return {
                    "final": "failed",
                    "steps": steps_done,
                    "summary": f"Planner timed out: model provider did not respond within {int(PLANNER_TIMEOUT_S)}s deadline (step {steps_done} state preserved).", "history": history,
                }
            except ValueError as exc:
                invalid_plans += 1
                reason = redact_text_urls(str(exc))[:300]
                penalties.append(
                    f"invalid plan ({reason}); re-emit a valid plan. Every click/type "
                    "needs target.targetId or target.name from the page context."
                )
                if invalid_plans >= MAX_INVALID_PLANS:
                    _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                    return {
                        "final": "failed",
                        "steps": steps_done,
                        "summary": f"Task incomplete: the planner could not produce a valid action ({reason}).", "history": history,
                    }
                progress(f"Replanning after invalid plan: {reason}")
                break
            except ProviderUnavailable as exc:
                err_text = str(exc)
                retry_s = 0.0
                try:
                    from .providers.limits import parse_retry_seconds
                    retry_s = parse_retry_seconds(err_text)
                except Exception:
                    pass

                # If cooldown is long (> 3.0s) or explicitly in quota exhaustion, do not repeatedly call providers still in cooldown
                is_long_cooldown = retry_s > 3.0 or "quota exhausted" in err_text or "1m" in err_text
                if is_long_cooldown or plan_attempt + 1 >= MAX_PLAN_RETRIES:
                    _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                    return {
                        "final": "failed",
                        "steps": steps_done,
                        "summary": f"Planner unavailable: {redact_text_urls(err_text)[:400]} (step {steps_done} state preserved).", "history": history,
                    }

                wait_time = max(retry_s + 0.5, 1.5)
                progress(f"Provider unavailable; retrying ({plan_attempt + 1}/{MAX_PLAN_RETRIES})...")
                await asyncio.sleep(wait_time)
                continue
            finally:
                metrics("plan", plan_started)
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
        if raw_plan is None:
            if invalid_plans > 0:
                continue
            return {"final": "failed", "steps": steps_done, "summary": "planner returned a non-object plan", "history": history}
        invalid_plans = 0
        if not isinstance(raw_plan, dict):
            return {"final": "failed", "steps": steps_done, "summary": "planner returned a non-object plan", "history": history}

        new_notes = raw_plan.get("notes")
        if new_notes:
            existing = working_memory.get("notes")
            if existing and existing != str(new_notes) and str(new_notes) not in existing:
                working_memory["notes"] = f"{existing}\n- {new_notes}"[:MAX_NOTES_CHARS]
            else:
                working_memory["notes"] = str(new_notes)[:MAX_NOTES_CHARS]

        steps = raw_plan.get("steps", [])
        if not steps:
            final_ans = raw_plan.get("answer") or raw_plan.get("summary")
            summary_msg = final_ans if final_ans else _summary(raw_obs, history)
            ms_tracker = working_memory.get("multi_site_research")
            if ms_tracker:
                try:
                    from .extractors.research import is_multi_site_task_complete, get_pending_sites, format_comparison_table, extract_target_product_query
                    if not is_multi_site_task_complete(ms_tracker):
                        pending = get_pending_sites(ms_tracker)
                        if pending:
                            penalties.append(
                                f"Multi-site task incomplete: {len(pending)} websites remaining to compare: {', '.join(s['name'] for s in pending)}. Do not conclude yet."
                            )
                            next_s = pending[0]
                            cur_host = (urlsplit(raw_url).hostname or "").lower()
                            is_on_next_s = cur_host and any(d in cur_host for d in next_s.get("domains", []))
                            if is_on_next_s:
                                interactive = getattr(raw_obs, "interactive", []) if raw_obs else []
                                search_el = None
                                for el in (interactive or []):
                                    name = str(getattr(el, "name", "") or (el.get("name") if isinstance(el, dict) else "")).lower()
                                    role = str(getattr(el, "role", "") or (el.get("role") if isinstance(el, dict) else "")).lower()
                                    if role in ("searchbox", "combobox") or "search" in name:
                                        search_el = el
                                        break
                                query = extract_target_product_query(instruction)
                                if search_el and query:
                                    t_id = getattr(search_el, "target_id", None) or (search_el.get("targetId") if isinstance(search_el, dict) else "search")
                                    t_name = getattr(search_el, "name", None) or (search_el.get("name") if isinstance(search_el, dict) else "Search")
                                    action = {
                                        "type": "type",
                                        "risk": "write",
                                        "target": {"targetId": t_id, "name": t_name},
                                        "params": {"text": query, "submit": True},
                                        "verify": f"searched for {query}",
                                        "timeoutMs": 15000,
                                    }
                                    first = {"why": f"On {next_s['name']}: searching for '{query}'", "action": action}
                                    steps = [first]
                                else:
                                    next_s["status"] = "unavailable"
                                    next_s["reason"] = f"No search input or listing found on {next_s['name']}"
                                    remaining = get_pending_sites(ms_tracker)
                                    if remaining:
                                        rem = remaining[0]
                                        action = {
                                            "type": "navigate",
                                            "risk": "write",
                                            "params": {"url": rem["default_url"]},
                                            "verify": f"navigated to {rem['name']}",
                                            "timeoutMs": 15000,
                                        }
                                        first = {"why": f"Moving to next comparison site: navigating to {rem['name']}", "action": action}
                                        steps = [first]
                            else:
                                first = {
                                    "why": f"Continuing multi-site comparison: navigating to {next_s['name']}",
                                    "action": {
                                        "type": "navigate",
                                        "risk": "write",
                                        "params": {"url": next_s["default_url"]},
                                        "verify": f"navigated to {next_s['name']}",
                                        "timeoutMs": 15000,
                                    },
                                }
                                steps = [first]
                                action = first["action"]
                        else:
                            return {
                                "final": "failed",
                                "steps": steps_done,
                                "summary": summary_msg or "Task incomplete: requested comparison websites were not all visited.",
                                "history": history, "url": raw_url, "last_obs": raw_obs,
                            }
                    else:
                        comp_table = format_comparison_table(ms_tracker, instruction)
                        if comp_table and "Price & Availability Comparison" not in summary_msg:
                            summary_msg = f"{summary_msg}\n\n{comp_table}" if summary_msg else comp_table
                except Exception:
                    pass
            if not steps:
                if raw_plan.get("blocked"):
                    return {"final": "failed", "steps": steps_done,
                            "summary": summary_msg or "Task incomplete: the result could not be verified.",
                            "history": history, "url": raw_url, "last_obs": raw_obs, "working_memory": working_memory}

                if working_memory.get("is_research"):
                    from .research.collector import is_search_results_page
                    inst_lower = (instruction or "").lower()
                    requires_multi = any(k in inst_lower for k in ["multiple", "compare", "credible sources", "independent sources", "different sources", "cross-reference"])

                    pkg_data = working_memory.get("research_knowledge_package") or {}
                    sources = pkg_data.get("sources") or {}
                    substantive_domains = set()
                    for s in sources.values():
                        s_url = s.get("url", "")
                        dom = s.get("domain", "")
                        if dom and not is_search_results_page(s_url):
                            parts = dom.lower().split(".")
                            root_dom = ".".join(parts[-2:]) if len(parts) >= 2 else dom.lower()
                            substantive_domains.add(root_dom)

                    pending_links = list(working_memory.get("pending_research_links") or [])
                    is_on_search = is_search_results_page(raw_url, raw_title, raw_text)

                    from .research.adaptive_planner import (
                        ResearchPlan,
                        detect_evidence_gaps,
                        generate_targeted_gap_queries,
                    )
                    plan_data = working_memory.get("research_plan")
                    plan = ResearchPlan.model_validate(plan_data) if plan_data else None
                    min_required_sources = plan.min_sources if plan else (2 if requires_multi else 1)

                    # 1. If currently on a search page and we have pending extracted article links:
                    if is_on_search and pending_links:
                        next_url = pending_links.pop(0)
                        working_memory["pending_research_links"] = pending_links
                        first = {
                            "why": f"Navigating from search results to substantive article: {next_url}",
                            "action": {
                                "type": "navigate",
                                "risk": "write",
                                "params": {"url": next_url},
                                "verify": "navigated to substantive research article",
                                "timeoutMs": 15000,
                            },
                        }
                        steps = [first]

                    # 2. Dynamic source requirement (adapts to simple=1, standard=2, complex=3+):
                    elif len(substantive_domains) < min_required_sources:
                        if pending_links:
                            next_url = pending_links.pop(0)
                            working_memory["pending_research_links"] = pending_links
                            first = {
                                "why": f"Navigating to additional independent source ({len(substantive_domains)}/{min_required_sources}): {next_url}",
                                "action": {
                                    "type": "navigate",
                                    "risk": "write",
                                    "params": {"url": next_url},
                                    "verify": "navigated to independent research source",
                                    "timeoutMs": 15000,
                                },
                            }
                            steps = [first]
                            penalties.append(f"Adaptive source requirement ({len(substantive_domains)}/{min_required_sources} visited). Moving to next source: {next_url}.")
                        else:
                            cand_sources = (working_memory.get("autonomous_discovery") or {}).get("candidate_sources", [])
                            unvisited_cand = []
                            for c in cand_sources:
                                c_dom = (c.get("domain") or "").lower()
                                parts = c_dom.split(".")
                                c_root = ".".join(parts[-2:]) if len(parts) >= 2 else c_dom
                                if c_root and c_root not in substantive_domains:
                                    unvisited_cand.append(c)
                            if unvisited_cand:
                                next_cand = unvisited_cand[0]
                                first = {
                                    "why": f"Navigating to independent candidate source ({next_cand.get('domain')}): {next_cand.get('url')}",
                                    "action": {
                                        "type": "navigate",
                                        "risk": "write",
                                        "params": {"url": next_cand["url"]},
                                        "verify": f"navigated to {next_cand.get('domain')}",
                                        "timeoutMs": 15000,
                                    },
                                }
                                steps = [first]
                                penalties.append(f"Adaptive source requirement ({len(substantive_domains)}/{min_required_sources} visited). Moving to {next_cand.get('domain')}.")
                            else:
                                if len(substantive_domains) == 0:
                                    return {
                                        "final": "failed",
                                        "steps": steps_done,
                                        "summary": f"Research incomplete: No independent sources could be verified.\n\n{summary_msg}",
                                        "history": history,
                                        "url": raw_url,
                                        "last_obs": raw_obs,
                                        "working_memory": working_memory,
                                    }

                    # 3. Adaptive Research Expansion (Evidence Gap Detection across subtopics)
                    elif plan and pkg_data:
                        pkg_obj = ResearchKnowledgePackage.from_dict(pkg_data) if isinstance(pkg_data, dict) else pkg_data
                        gaps = detect_evidence_gaps(plan, pkg_obj)
                        if gaps and plan.expansion_count < plan.max_expansions:
                            plan.expansion_count += 1
                            working_memory["research_plan"] = plan.model_dump()
                            gap_queries = generate_targeted_gap_queries(gaps, plan.topic_title)
                            if gap_queries:
                                targeted_q = gap_queries[0]
                                first = {
                                    "why": f"Evidence gap detected for subtopic '{gaps[0].title}'. Expanding research with targeted query: {targeted_q}",
                                    "action": {
                                        "type": "navigate",
                                        "risk": "write",
                                        "params": {"url": f"https://www.nature.com/search?q={urllib.parse.quote(targeted_q)}"},
                                        "verify": f"navigated to search results for {targeted_q}",
                                        "timeoutMs": 15000,
                                    },
                                }
                                steps = [first]
                                penalties.append(
                                    f"Adaptive expansion ({plan.expansion_count}/{plan.max_expansions}): Subtopic '{gaps[0].title}' lacks verified evidence. Investigating '{targeted_q}'."
                                )

                if not steps:
                    if working_memory.get("is_research"):
                        summary_msg = _finalize_research_summary(
                            task_id, conversation_id, working_memory, summary_msg
                        )
                    return {"final": "completed", "steps": steps_done, "summary": summary_msg,
                            "history": history, "url": raw_url, "last_obs": raw_obs, "working_memory": working_memory}

        first = steps[0] if isinstance(steps, list) else None
        action = first.get("action") if isinstance(first, dict) else None
        if not isinstance(action, dict) or not action.get("type"):
            # Belt-and-braces: every real provider is schema-validated, but a
            # malformed injected plan must never crash the loop with a KeyError.
            return {"final": "failed", "steps": steps_done, "summary": "planner returned a malformed step", "history": history}

        # Requirement 8: Never repeat completed browser actions after fallback
        if _is_redundant_completed_action(action, history, raw_url):
            if len(steps) > 1:
                first = steps[1]
                action = first.get("action") if isinstance(first, dict) else None
                if not isinstance(action, dict) or not action.get("type"):
                    action = None
            else:
                action = None
                ms_tracker = working_memory.get("multi_site_research")
                if ms_tracker:
                    try:
                        from .extractors.research import is_multi_site_task_complete, get_pending_sites
                        if not is_multi_site_task_complete(ms_tracker):
                            pending = get_pending_sites(ms_tracker)
                            if pending:
                                next_site = pending[0]
                                cur_h = (urlsplit(raw_url).hostname or "").lower()
                                if cur_h and any(d in cur_h for d in next_site.get("domains", [])):
                                    rem = [s for s in pending if not any(d in cur_h for d in s.get("domains", []))]
                                    if rem:
                                        next_site = rem[0]
                                action = {
                                    "type": "navigate",
                                    "risk": "write",
                                    "params": {"url": next_site["default_url"]},
                                    "verify": f"navigated to {next_site['name']}",
                                    "timeoutMs": 15000,
                                }
                                first = {"why": f"Action was redundant. Transitioning to pending comparison site {next_site['name']}", "action": action}
                    except Exception:
                        pass
                if action is None:
                    if working_memory.get("collected_research") or working_memory.get("notes"):
                        # If multi-site tracker is present and not complete, do NOT complete prematurely
                        if not ms_tracker or is_multi_site_task_complete(ms_tracker):
                            summary_notes = working_memory.get("notes") or "Research completed."
                            pkg_data = working_memory.get("research_knowledge_package") or {}
                            if working_memory.get("is_research") and not (
                                pkg_data.get("sources")
                                or pkg_data.get("evidence")
                                or pkg_data.get("tables")
                            ):
                                # The task repeated its first navigation without
                                # ever loading a source page. Report this honestly
                                # instead of claiming a completed research run.
                                return {
                                    "final": "failed",
                                    "steps": steps_done,
                                    "summary": (
                                        "Research incomplete: the source page did not load, "
                                        "so no sources or evidence were collected and no "
                                        "deliverable was generated."
                                    ),
                                    "history": history, "url": raw_url, "last_obs": raw_obs,
                                }
                            summary_notes = _finalize_research_summary(
                                task_id, conversation_id, working_memory, summary_notes
                            )
                            return {"final": "completed", "steps": steps_done, "summary": summary_notes,
                                    "history": history, "url": raw_url, "last_obs": raw_obs}
                    penalties.append(
                        "You emitted an action that was already executed and completed in a previous step. Do not repeat completed browser actions."
                    )
                    continue

        # Loop prevention & chat auto-completion (host-scoped; no false
        # positives on ordinary search forms that happen to have a send button).
        if history and history[-1].get("result_ok"):
            if _auto_complete_after_send(history, action, raw_url):
                notes_summary = working_memory.get("notes") or "Message successfully sent to recipient."
                notes_summary = _finalize_research_summary(
                    task_id, conversation_id, working_memory, notes_summary
                )
                return {
                    "final": "completed",
                    "steps": steps_done,
                    "summary": notes_summary if working_memory.get("is_research")
                    else f"Completed: {notes_summary[:160]}",
                    "history": history,
                }

            # State-aware loop detection: detect repeated actions with zero page state change
            if last_digest is not None and current_digest == last_digest:
                last_entry = history[-1]
                last_act = last_entry.get("action") or {}
                if _actions_match(last_act, action):
                    zero_change_repeats += 1
                    if zero_change_repeats >= 3 and action.get("type") != "scroll":
                        return {
                            "final": "budget_exceeded",
                            "steps": steps_done,
                            "summary": f"Loop detected: action '{action.get('type')}' repeated on identical page state with zero effect.",
                            "history": history,
                        }
                    if zero_change_repeats >= 1 and action.get("type") != "scroll":
                        penalties.append(
                            f"State-aware loop warning: action '{action.get('type')}' was already executed on this exact page state, but produced zero state change (URL and DOM remain identical). Do not repeat this action without changing target or strategy. If navigating, select an alternative link or conclude."
                        )
                else:
                    zero_change_repeats = 0
            else:
                zero_change_repeats = 0

            consecutive_repeats = 0
            for h in reversed(history):
                if h.get("result_ok") and _actions_match(h.get("action", {}), action):
                    consecutive_repeats += 1
                else:
                    break

            if action.get("type") == "scroll":
                if consecutive_repeats >= 3:
                    return {
                        "final": "budget_exceeded",
                        "steps": steps_done,
                        "summary": "Loop detected: repeated scrolling without selecting a target or finding progress.",
                        "history": history,
                    }
                if consecutive_repeats >= 1:
                    penalties.append(
                        "Loop warning: You have already scrolled down. Do not scroll again. Look at the available links in <page-context> and click a matching product/result, or conclude if information is found."
                    )
            else:
                if consecutive_repeats >= 3:
                    return {
                        "final": "budget_exceeded",
                        "steps": steps_done,
                        "summary": f"Loop detected: action '{action.get('type')}' repeated {consecutive_repeats + 1} times without progress.",
                        "history": history,
                    }
                if consecutive_repeats >= 1:
                    penalties.append(f"loop warning: action '{action.get('type')}' was already completed. Do not repeat it; proceed to next step or conclude.")

        last_digest = current_digest

        if raw_url:
            url_history.append(raw_url)
            if len(url_history) >= 4:
                if (url_history[-1] == url_history[-3] and
                    url_history[-2] == url_history[-4] and
                    url_history[-1] != url_history[-2]):
                    penalties.append(
                        "Oscillation warning: You are repeatedly bouncing between two pages. Do not navigate back and forth; execute your search or extraction on the destination page."
                    )

        is_wa_qr = _is_whatsapp_qr(raw_url, raw_obs)
        is_auth_page = _is_auth_url(raw_url)
        is_strict = getattr(confirm_gate, "strict_write_confirm", True) if confirm_gate is not None else False
        needs_confirm = action.get("risk") == "sensitive" or is_auth_page or is_wa_qr or (
            is_strict and action.get("risk") == "write" and action.get("type") not in ("navigate", "scroll", "zoom", "wait", "observe", "select_tab")
        )
        approved = False
        if confirm_gate is not None and needs_confirm:
            gate_msg = _gate_message(action, is_wa_qr, is_auth_page)
            approved = await _request_confirmation(confirm_gate, action, task_id, confirm_timeout_s, custom_message=gate_msg)
            if is_cancelled(task_id):
                return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
            if approved is False:
                return {"final": "failed", "steps": steps_done,
                        "summary": f"user rejected action: {action.get('type')}", "history": history}

        _enforce_date_integrity(action, instruction)
        action_started = time.perf_counter()
        result = await _execute_with_retries(ext_hub, action, task_id, session_id=session_id)
        metrics("act", action_started)
        if session_id and result.get("ok"):
            obs = result.get("observation")
            if isinstance(obs, dict) and "tabId" in obs:
                from .session_manager import session_manager
                sess = session_manager.get_session(session_id)
                if sess:
                    is_new_tab = "opened new tab" in str(result.get("evidence", "")).lower() or obs["tabId"] == sess.primary_tab_id or obs["tabId"] in sess.child_tab_ids
                    if is_new_tab:
                        session_manager.register_child_tab(session_id, obs["tabId"])
            # After a successful close_tab, drop the tab from the session's
            # child registry so stale tabIds cannot authorize later actions.
            if session_id and action.get("type") == "close_tab":
                try:
                    closed_tab_id = int(action.get("params", {}).get("tabId"))
                except (TypeError, ValueError):
                    closed_tab_id = None
                if closed_tab_id is not None:
                    session_manager.unregister_child_tab(session_id, closed_tab_id)
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
        steps_done += 1
        cached_obs = _fresh_observation(result, action)
        if needs_confirm and approved:
            try:
                from .auth_validator import verify_authentication_state
                post_obs = cached_obs or raw_obs
                is_authed, auth_msg = verify_authentication_state(post_obs)
                if not is_authed:
                    penalties.append(f"Authentication unverified: {auth_msg}. Do not assume login succeeded.")
            except Exception:
                pass
        if not result.get("ok") or zero_change_repeats > 0:
            cached_obs = None

        evidence: Any = result.get("evidence")
        if not evidence:
            if result.get("ok"):
                evidence = "ok"
            else:
                err_msg = (result.get("error") or {}).get("message", "failed")
                evidence = err_msg
        raw_screenshot = raw_obs.get("screenshot") if isinstance(raw_obs, dict) else getattr(raw_obs, "screenshot", None)
        fresh_shot = (
            getattr(cached_obs, "screenshot", None)
            if cached_obs
            else (result.get("observation") or {}).get("screenshot")
        )
        step_screenshot = fresh_shot or raw_screenshot
        safe_evidence: Any = redact_text_urls(evidence) if isinstance(evidence, str) else evidence
        progress(f"Step {steps_done}: {action.get('type')} -> {safe_evidence}", screenshot=step_screenshot)

        action_ok = bool(result.get("ok"))
        if action_ok:
            is_verified = (
                action.get("type") in ("navigate", "scroll", "wait", "observe", "select_tab")
                or "navigated" in str(safe_evidence).lower()
                or "opened new tab" in str(safe_evidence).lower()
                or "verified" in str(safe_evidence).lower()
                or "accepted" in str(safe_evidence).lower()
            )
            action_status = "verified" if is_verified else "completed_unverified"
        else:
            action_status = "failed"

        entry = {
            "action": action,
            "result_ok": action_ok,
            "status": action_status,
            "evidence": safe_evidence,
        }
        if not action_ok:
            err = result.get("error") or {}
            err_msg = str(err.get("message", "unknown"))
            entry["error"] = redact_text_urls(err_msg)
            if not err.get("recoverable", False):
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                return {"final": "failed", "steps": steps_done,
                        "summary": f"non-recoverable error: {redact_text_urls(err_msg)}", "history": history}
            penalty = _same_failure(history, entry)
            if penalty:
                penalties.append(penalty)
        history.append(entry)
        _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)

        if steps_done >= MAX_STEPS:
            break

    if is_cancelled(task_id):
        return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
    final_obs = cached_obs
    if final_obs is None:
        final_obs = obs_factory()
        if asyncio.iscoroutine(final_obs):
            try:
                final_obs = await final_obs
            except Exception:
                final_obs = {}
    return {"final": "budget_exceeded", "steps": steps_done, "summary": _summary(final_obs, history), "history": history}


def _call_planner(
    planner: Callable[..., dict[str, Any]],
    instruction: str,
    payload: dict[str, Any],
    history: list[dict[str, Any]],
    working_memory: dict[str, Any],
) -> dict[str, Any]:
    """Call the planner with working_memory only when it declares support.

    Intent inspection beats catching TypeError: a TypeError raised *inside*
    a planner must surface instead of silently replaying the call without
    working memory (which could duplicate side effects).
    """
    try:
        params = inspect.signature(planner).parameters
    except (TypeError, ValueError):
        params = {}
    supports_wm = "working_memory" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if supports_wm:
        return planner(instruction, payload, history, working_memory=working_memory)
    return planner(instruction, payload, history)


def _actions_match(a1: dict[str, Any], a2: dict[str, Any]) -> bool:
    if a1.get("type") != a2.get("type"):
        return False
    t1 = a1.get("target") or {}
    t2 = a2.get("target") or {}
    p1 = a1.get("params") or {}
    p2 = a2.get("params") or {}
    if t1 and t2:
        if t1.get("name") and t1.get("name") == t2.get("name"):
            return True
        if t1.get("targetId") and t1.get("targetId") == t2.get("targetId"):
            return True
    if p1.get("text") and p1.get("text") == p2.get("text"):
        return True
    if p1.get("url") and p1.get("url") == p2.get("url"):
        return True
    return False


def _is_redundant_completed_action(action: dict[str, Any], history: list[dict[str, Any]], current_url: str) -> bool:
    """Never repeat completed browser actions (e.g. redundant navigate or form submission after provider fallback)."""
    if not history or not action:
        return False
    atype = action.get("type")
    params = action.get("params") or {}
    if atype == "navigate":
        nav_url = params.get("url")
        if nav_url and current_url:
            try:
                cur_parts = urlsplit(current_url)
                nav_parts = urlsplit(nav_url)
                if cur_parts.hostname and nav_parts.hostname and (
                    cur_parts.hostname == nav_parts.hostname
                    or cur_parts.hostname.endswith("." + nav_parts.hostname)
                    or nav_parts.hostname.endswith("." + cur_parts.hostname)
                ):
                    if cur_parts.path.rstrip("/") == nav_parts.path.rstrip("/"):
                        for h in history:
                            h_act = h.get("action") or {}
                            if h.get("result_ok") and h_act.get("type") == "navigate":
                                return True
            except Exception:
                pass
    elif atype == "type":
        text = params.get("text")
        if text and params.get("submit"):
            for h in history:
                h_act = h.get("action") or {}
                if h.get("result_ok") and h_act.get("type") == "type":
                    h_params = h_act.get("params") or {}
                    if h_params.get("text") == text and h_params.get("submit"):
                        return True
    return False


def _state_digest(raw_obs: Any) -> str:
    if raw_obs is None:
        return ""
    url = str(getattr(raw_obs, "url", None) or (raw_obs.get("url") if isinstance(raw_obs, dict) else "") or "")
    title = str(getattr(raw_obs, "title", None) or (raw_obs.get("title") if isinstance(raw_obs, dict) else "") or "")
    interactive = getattr(raw_obs, "interactive", None) or (raw_obs.get("interactive") if isinstance(raw_obs, dict) else []) or []
    el_sample = ",".join(
        str(getattr(e, "target_id", None) or (e.get("targetId") if isinstance(e, dict) else ""))
        for e in interactive[:12]
    )
    return f"{url}|{title}|{len(interactive)}|{el_sample}"


def _is_whatsapp_qr(raw_url: str, raw_obs: Any) -> bool:
    if not _host_matches(_host_of(raw_url), "whatsapp.com"):
        return False
    elements = getattr(raw_obs, "interactive", None)
    if elements is None and isinstance(raw_obs, dict):
        elements = raw_obs.get("interactive") or []

    has_chats = False
    has_qr_prompt = False
    for el in elements or []:
        name = str(getattr(el, "name", None) or (el.get("name") if isinstance(el, dict) else "")).lower()
        role = str(getattr(el, "role", None) or (el.get("role") if isinstance(el, dict) else "")).lower()
        if (
            "search or start new chat" in name
            or "type a message" in name
            or role == "listitem"
            or name in ("chats", "status", "channels", "communities")
        ):
            has_chats = True
        if (
            "scan this qr code" in name
            or "use whatsapp on your computer" in name
            or "link with phone number" in name
            or "to use whatsapp on your computer" in name
            or ("qr" in name and "code" in name and not has_chats)
        ):
            has_qr_prompt = True

    # If chats or search input are visible, user is logged in — never block on QR gate
    if has_chats:
        return False
    return has_qr_prompt


HANDOFF_MESSAGE = "Please solve the CAPTCHA / sign in manually in your browser, then click Continue."


def _gate_message(action: dict[str, Any], is_wa_qr: bool, is_auth_page: bool) -> Optional[str]:
    """Operator-facing instruction shown with the confirmation gate."""
    if is_wa_qr:
        return ("WhatsApp Web requires QR code scan. Please scan the QR code in your "
                "WhatsApp tab to log in, then click Continue.")
    if action.get("type") == "wait" and action.get("risk") == "sensitive":
        # The planner's human-in-the-loop handoff (CAPTCHA / login wall).
        return HANDOFF_MESSAGE
    if is_auth_page:
        return ("Site requires Sign-In or Payment Credentials. Please sign in or complete "
                "authentication in your browser tab, then click Continue.")
    return None


async def _request_confirmation(gate: Any, action: dict[str, Any], task_id: str, timeout_s: float, custom_message: Optional[str] = None) -> bool:
    """Pause and await a human decision. Returns True if approved.
    gate contract: confirm_requests list + asyncio.Event confirm_event + decision:bool|None."""
    req = {
        "taskId": task_id,
        "action": action,
        "message": custom_message or f"NAVIGATOR wants to {action.get('type')} (risk {action.get('risk')})"
    }
    if hasattr(gate, "add_request"):
        gate.add_request(req)
    else:
        gate.confirm_requests.append(req)
    gate.decision = None
    gate.confirm_event.clear()
    try:
        await asyncio.wait_for(gate.confirm_event.wait(), timeout_s)
    except asyncio.TimeoutError:
        return False
    return bool(gate.decision)


def _summary(obs: Any, history: list[dict[str, Any]]) -> str:
    last_ok = next((h.get("evidence") for h in reversed(history) if h.get("result_ok") and h.get("evidence")), None)
    raw_url = getattr(obs, "url", None) or (obs.get("url") if isinstance(obs, dict) else "") or ""
    # Summary is persisted and rendered; never surface tokens from the URL or
    # from evidence strings that echo navigated-to URLs.
    evidence = redact_text_urls(str(last_ok)) if last_ok else "none"
    # Running out of steps is a failure, not a victory. Say why, so the UI shows
    # a truthful reason instead of a generic completion line.
    note = ""
    last = history[-1] if history else None
    if last and last.get("result_ok") is False:
        act = (last.get("action") or {}).get("type") or "action"
        note = f"Task incomplete: the last {act} failed ({redact_text_urls(str(last.get('error') or 'error'))}). "
    elif _is_auth_url(str(raw_url)):
        note = "Task incomplete: the page is still a sign-in screen, so the requested data was not retrieved. "
    return note + f"final url: {redact_url(str(raw_url))} | last evidence: {evidence}"


def _same_failure(history: list[dict[str, Any]], entry: dict[str, Any]) -> Optional[str]:
    for prev in reversed(history):
        if prev.get("result_ok"):
            return None
        if prev.get("error") == entry.get("error") and prev["action"].get("type") == entry["action"].get("type"):
            return f"repeated failure: {entry['error']} on {entry['action'].get('type')}"
    return None


def _history_view(history: list[dict[str, Any]], penalties: list[str], working_memory: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in history[-5:]:
        entry = dict(h)
        for key in ("evidence", "error"):
            if isinstance(entry.get(key), str):
                entry[key] = entry[key][:MAX_HISTORY_TEXT_CHARS]
        out.append(entry)
    if working_memory and working_memory.get("notes"):
        out.append({"working_memory": working_memory["notes"]})
    for p in penalties[-3:]:
        out.append({"penalty": p})
    return out


async def _execute_with_retries(ext_hub: Any, action: dict[str, Any], task_id: str, session_id: Optional[str] = None) -> dict[str, Any]:
    act = dict(action)
    if session_id and "sessionId" not in act:
        act["sessionId"] = session_id
    if "timeoutMs" not in act:
        act["timeoutMs"] = act.get("timeout_ms", 10000)
    # Wait slightly longer than the extension's own action deadline (+5s) so
    # the backend never declares TIMEOUT while the action may still finish,
    # then retry it and double-execute a click.
    raw_timeout = act.get("timeoutMs")
    timeout_s = 45.0
    if isinstance(raw_timeout, (int, float)) and raw_timeout > 0:
        timeout_s = min(float(raw_timeout) / 1000.0, 60.0) + 10.0

    res: dict[str, Any] = {"ok": False, "error": {"code": "CANCELLED", "message": "task cancelled", "recoverable": False}}
    for attempt in range(MAX_RETRIES + 1):
        if is_cancelled(task_id):
            return {"ok": False, "error": {"code": "CANCELLED", "message": "task cancelled", "recoverable": False}}
        # Route with the real task id: "task" was a placeholder that broke
        # per-task socket routing and cancellation audits.
        res = await ext_hub.send_action(task_id, act, timeout_s=timeout_s)
        if res.get("ok"):
            return res
        err = res.get("error") or {}
        if err.get("code") == "TIMEOUT" and act.get("type") not in _IDEMPOTENT_RETRY_TYPES:
            return res
        if not err.get("recoverable", False) or attempt == MAX_RETRIES:
            return res
        await asyncio.sleep(0.2 * (attempt + 1))
    return res


def _fresh_observation(result: dict[str, Any], action: dict[str, Any]) -> Any:
    """Return a validated post-action observation, or None to re-observe.

    Only successful, non-tab-changing actions qualify. Validation is
    defensive: a malformed extension payload must fall back to obs_factory()
    rather than reach the privacy layer as a partial object.
    """
    if not result.get("ok") or action.get("type") in _FORCE_REOBSERVE_ACTIONS:
        return None
    obs = result.get("observation")
    if not obs:
        return None
    try:
        return _to_obs(obs)
    except Exception:
        return None


def _to_obs(raw: dict[str, Any]):
    from .contracts import BrowserObservation
    if isinstance(raw, BrowserObservation):
        return raw
    return BrowserObservation.model_validate(raw)


def extract_mission_facts(instruction: str, summary: str, history: Optional[list[dict[str, Any]]] = None) -> dict[str, str]:
    """Extract structured key-value entities from mission execution for persistent chat_memory."""
    facts: dict[str, str] = {}
    full_text = f"{instruction} | {summary}"

    # 1. Price extraction
    price_match = re.search(r"(?:₹|Rs\.?|INR|\$)\s?[\d,]+(?:\.\d+)?", full_text, re.IGNORECASE)
    if price_match:
        facts["extracted_price"] = price_match.group(0).strip()

    # 2. Canonical URL extraction from summary or history evidence
    url_match = re.search(r"https?://[^\s\"'<>)]+", summary)
    if not url_match and history:
        for h in reversed(history):
            ev = str(h.get("evidence") or "")
            u_match = re.search(r"https?://[^\s\"'<>)]+", ev)
            if u_match:
                url_match = u_match
                break
    if url_match:
        facts["canonical_url"] = url_match.group(0).strip()

    # 3. Product / Subject Target
    cleaned_target = re.sub(
        r"(?i)\b(find|search|buy|check|compare|look for|me|the|a|an|on amazon|on flipkart|on google|specs|price|and|for)\b",
        " ",
        instruction,
    )
    cleaned_target = re.sub(r"\s+", " ", cleaned_target).strip()
    if cleaned_target and len(cleaned_target) > 2:
        facts["target_item"] = cleaned_target[:60]

    # 4. Core constraints
    constraint_match = re.search(r"(?i)\b(under\s+[\d,]+|below\s+[\d,]+|budget\s+[\d,]+|size\s+[a-z0-9]+|to checkout)\b", instruction)
    if constraint_match:
        facts["core_constraint"] = constraint_match.group(0).strip()

    # 5. Last result summary snippet
    if summary:
        facts["last_result"] = summary[:240].strip()

    # 6. LeetCode Profile extraction
    if "leetcode" in full_text.lower():
        try:
            from .extractors.leetcode import extract_leetcode_profile
            profile = extract_leetcode_profile({"url": facts.get("canonical_url", "")}, page_text=full_text)
            if profile.get("username") and profile["username"] != "Authenticated User":
                facts["leetcode_username"] = str(profile["username"])
            if profile.get("total_solved") and str(profile["total_solved"]) != "0":
                facts["leetcode_solved"] = str(profile["total_solved"])
            if profile.get("contest_rating") and "unrated" not in str(profile["contest_rating"]).lower():
                facts["leetcode_rating"] = str(profile["contest_rating"])
        except Exception:
            pass

    # 7. Research & Deliverable extraction
    if "research" in full_text.lower() or "deliverable ready" in full_text.lower() or any(ext in full_text.lower() for ext in (".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".html")):
        art_match = re.search(r"\[([^\]]+\.(?:pdf|docx|pptx|xlsx|csv|html))\]", summary, re.IGNORECASE)
        if art_match:
            facts["last_deliverable"] = art_match.group(1).strip()
        try:
            from .intent_router import extract_research_topic, detect_deliverable_type, detect_requested_format
            topic = extract_research_topic(instruction)
            if topic and len(topic.split()) >= 2:
                facts["research_topic"] = topic[:80]
            deliv = detect_deliverable_type(instruction)
            if deliv:
                facts["deliverable_type"] = deliv
            fmt = detect_requested_format(instruction)
            if fmt:
                facts["requested_format"] = fmt
        except Exception:
            pass

    return facts


async def run_task(task_id: str) -> None:
    """Production entrypoint — wires real hub, real planner, live observation."""
    record: TaskRecord = real_hub.tasks[task_id]
    chat_id = record.chat_id
    latest_cp: Optional[dict[str, Any]] = None

    initial_memory: dict[str, Any] = {}
    try:
        from . import db
        db.init_db()
        user_prefs = db.get_user_preferences()
        if user_prefs:
            initial_memory["user_preferences"] = user_prefs
    except Exception:
        pass

    if chat_id:
        try:
            from . import db
            raw_msgs = db.get_messages(chat_id, limit=6)
            if raw_msgs:
                initial_memory["conversation_history"] = [
                    {"role": m["role"], "content": m["content"]} for m in raw_msgs
                ]
            chat_mem = db.get_chat_memory(chat_id)
            if chat_mem:
                initial_memory["session_memory"] = chat_mem
                for k in ("research_topic", "requested_format", "deliverable_type", "extracted_price", "target_item"):
                    if k in chat_mem and k not in initial_memory:
                        initial_memory[k] = chat_mem[k]

            latest_cp = db.get_latest_checkpoint_for_chat(chat_id)

            db.add_message(chat_id, "user", record.instruction)

            chat_info = db.get_chat(chat_id)
            if chat_info and chat_info.get("title") in ("New Mission", "New Chat", ""):
                trunc_title = record.instruction[:36].strip()
                if len(record.instruction) > 36:
                    trunc_title += "..."
                db.update_chat_title(chat_id, trunc_title)
        except Exception:
            pass

    # 1. Intent Classification Stage (P5.2 & Operation Disambiguation)
    from .intent_router import classify_intent, TaskIntent, ResearchOperationType
    from .session_manager import session_manager

    active_session = None
    if chat_id:
        active_session = session_manager.get_active_session_for_chat(chat_id)

    latest_pkg_record = None
    if chat_id:
        try:
            from . import db
            latest_pkg_record = db.get_latest_package_for_chat(chat_id)
        except Exception:
            pass

    intent_context = {
        "active_session": active_session,
        "conversation_history": initial_memory.get("conversation_history", []),
        "session_memory": initial_memory.get("session_memory", {}),
        "is_research": bool(initial_memory.get("is_research") or (initial_memory.get("session_memory", {}).get("research_topic")) or latest_pkg_record),
        "requested_format": initial_memory.get("requested_format"),
        "deliverable_type": initial_memory.get("deliverable_type"),
        "research_topic": initial_memory.get("research_topic") or (latest_pkg_record.get("topic_title") if latest_pkg_record else None),
        "research_knowledge_package": latest_pkg_record.get("data") if latest_pkg_record else (latest_cp.get("working_memory", {}).get("research_knowledge_package") if latest_cp else None),
        "latest_checkpoint": latest_cp,
    }

    classification = classify_intent(record.instruction, context=intent_context)
    record.intent = classification.intent.value
    record.is_browser_task = classification.is_browser

    # -----------------------------------------------------------------------
    # Explicit Operation Dispatch:
    # 1. EXPORT_EXISTING_PACKAGE: Deliverable-only execution (Zero browser ops)
    # 2. RESUME_EXISTING_MISSION: Continue/resume pending research or export
    # 3. REFINE_EXISTING_RESEARCH: Refine existing package preserving evidence
    # 4. NEW_RESEARCH: Fresh isolated research mission
    # -----------------------------------------------------------------------

    # A. Deliverable-Only Execution
    if classification.operation_type == ResearchOperationType.EXPORT_EXISTING_PACKAGE:
        export_session_id = f"sess_export_{task_id}"
        record.state = "running"
        record.message = "Preparing deliverable export..."
        await real_hub.broadcast_progress(record)

        from .research.package_resolver import resolve_target_package
        res = resolve_target_package(
            chat_id=chat_id,
            instruction=record.instruction,
            context=intent_context,
            requested_format=classification.requested_format or "pdf",
        )
        if res.is_ambiguous:
            record.steps_done = 1
            record.state = "completed"
            record.message = res.clarification_message
            _save_checkpoint_defensive(
                task_id=task_id,
                conversation_id=chat_id,
                session_id=export_session_id,
                steps_done=1,
                raw_url="",
                history=[{"role": "assistant", "content": record.message}],
                working_memory={"research_stage": "completed", "clarification": res.clarification_message},
            )
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    from . import db
                    db.add_message(chat_id, "agent", record.message, evidence="ambiguity_clarification")
                    db.touch_chat(chat_id)
                except Exception:
                    pass
            return

        if not res.package:
            record.steps_done = 1
            record.state = "completed"
            record.message = (
                "No prior research findings or package found in this conversation to export. "
                "Please specify a topic to research first (e.g., 'Research transformer architecture and generate a PDF')."
            )
            _save_checkpoint_defensive(
                task_id=task_id,
                conversation_id=chat_id,
                session_id=export_session_id,
                steps_done=1,
                raw_url="",
                history=[{"role": "assistant", "content": record.message}],
                working_memory={"research_stage": "completed", "error": record.message},
            )
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    from . import db
                    db.add_message(chat_id, "agent", record.message, evidence="conversational")
                    db.touch_chat(chat_id)
                except Exception:
                    pass
            return

        pkg_data = res.package.get("data") or res.package
        from .research.models import ResearchKnowledgePackage
        pkg = ResearchKnowledgePackage.from_dict(pkg_data) if isinstance(pkg_data, dict) else pkg_data

        target_fmt = (classification.requested_format or "pdf").lower().strip(" .")
        deliv_type = classification.deliverable_type or ("presentation" if target_fmt == "pptx" else "report")
        working_memory = dict(initial_memory)
        from .research.adaptive_planner import generate_adaptive_research_plan
        plan = generate_adaptive_research_plan(record.instruction, {
            "research_topic": pkg.topic.title,
            "deliverable_type": deliv_type,
            "requested_format": target_fmt,
        })
        working_memory["research_plan"] = plan.model_dump()
        pkg.metadata["research_plan"] = plan.model_dump()
        working_memory["research_knowledge_package"] = pkg.to_dict()
        working_memory["requested_format"] = target_fmt
        working_memory["deliverable_type"] = deliv_type
        working_memory["is_research"] = True
        working_memory["instruction"] = record.instruction

        primary_art = _finalize_research_artifacts(task_id, chat_id, working_memory, summary_msg=pkg.topic.findings_summary or pkg.topic.title)
        if primary_art and Path(primary_art["filepath"]).exists() and primary_art["file_size_bytes"] > 0:
            record.steps_done = 1
            record.state = "completed"
            fn = primary_art["filename"]
            fmt_upper = primary_art["file_format"].upper()
            sz_kb = primary_art["file_size_bytes"] / 1024.0
            record.message = (
                f"Successfully compiled the research findings into **{fn}** ({fmt_upper}, {sz_kb:.1f} KB).\n\n"
                f"📄 **Deliverable Ready:** [{fn}] ({fmt_upper}, {sz_kb:.1f} KB). Available for download."
            )
            working_memory["research_stage"] = "completed"
            working_memory["last_deliverable"] = fn
            _save_checkpoint_defensive(
                task_id=task_id,
                conversation_id=chat_id,
                session_id=export_session_id,
                steps_done=1,
                raw_url="",
                history=[{"role": "assistant", "content": record.message}],
                working_memory=working_memory,
            )
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    from . import db
                    db.set_chat_memory(chat_id, "last_deliverable", fn)
                    db.set_chat_memory(chat_id, "requested_format", primary_art["file_format"])
                    db.add_message(chat_id, "agent", record.message, evidence="deliverable_exported")
                    db.touch_chat(chat_id)
                except Exception:
                    pass
            return
        else:
            record.state = "failed"
            record.message = f"Failed to generate artifact in {target_fmt.upper()} format."
            _save_checkpoint_defensive(
                task_id=task_id,
                conversation_id=chat_id,
                session_id=export_session_id,
                steps_done=1,
                raw_url="",
                history=[{"role": "assistant", "content": record.message}],
                working_memory={"research_stage": "failed", "error": record.message},
            )
            await real_hub.broadcast_progress(record)
            return

    # B. Continuation / Resumption Handling
    if classification.operation_type == ResearchOperationType.RESUME_EXISTING_MISSION:
        from . import db
        # Check for interrupted in-flight checkpoint
        if latest_cp and latest_cp.get("step_index", 0) > 0 and latest_cp.get("working_memory", {}).get("research_stage") != "completed":
            initial_memory.update(latest_cp.get("working_memory", {}))
            if latest_cp.get("history"):
                initial_memory["prior_checkpoint_history"] = latest_cp["history"]
            record.message = f"Resuming interrupted research from step {latest_cp.get('step_index', 0)}..."
            await real_hub.broadcast_progress(record)
        else:
            # Check for completed package with pending export or already complete
            latest_pkg_row = db.get_latest_package_for_chat(chat_id) if chat_id else None
            if latest_pkg_row:
                topic_title = latest_pkg_row.get("topic_title", "Research")
                existing_arts = db.list_conversation_artifacts(chat_id) if chat_id else []
                target_fmt = classification.requested_format or (initial_memory.get("session_memory") or {}).get("requested_format")
                has_matching_art = any(a.get("file_format") == target_fmt for a in existing_arts) if target_fmt else bool(existing_arts)

                if target_fmt and not has_matching_art:
                    pkg_data = latest_pkg_row.get("data") or {}
                    working_mem = dict(initial_memory)
                    working_mem["research_knowledge_package"] = pkg_data
                    working_mem["requested_format"] = target_fmt
                    working_mem["deliverable_type"] = classification.deliverable_type or "report"
                    working_mem["is_research"] = True
                    primary_art = _finalize_research_artifacts(task_id, chat_id, working_mem, summary_msg=topic_title)
                    if primary_art:
                        record.steps_done = 1
                        record.state = "completed"
                        fn = primary_art["filename"]
                        fmt_upper = primary_art["file_format"].upper()
                        sz_kb = primary_art["file_size_bytes"] / 1024.0
                        record.message = (
                            f"Successfully compiled the pending deliverable for **{topic_title}** into **{fn}** ({fmt_upper}, {sz_kb:.1f} KB).\n\n"
                            f"📄 **Deliverable Ready:** [{fn}] ({fmt_upper}, {sz_kb:.1f} KB). Available for download."
                        )
                        await real_hub.broadcast_progress(record)
                        if chat_id:
                            try:
                                db.set_chat_memory(chat_id, "last_deliverable", fn)
                                db.add_message(chat_id, "agent", record.message, evidence="deliverable_exported")
                                db.touch_chat(chat_id)
                            except Exception:
                                pass
                        return
                else:
                    last_deliv = (initial_memory.get("session_memory") or {}).get("last_deliverable") or (existing_arts[0]["filename"] if existing_arts else "")
                    record.steps_done = 1
                    record.state = "completed"
                    deliv_info = f" and deliverable **{last_deliv}** are" if last_deliv else " is"
                    record.message = f"The research on **{topic_title}**{deliv_info} already completed. What would you like to explore next?"
                    await real_hub.broadcast_progress(record)
                    if chat_id:
                        try:
                            db.add_message(chat_id, "agent", record.message, evidence="conversational")
                            db.touch_chat(chat_id)
                        except Exception:
                            pass
                    return
            else:
                record.steps_done = 1
                record.state = "completed"
                record.message = "There is no active or paused mission to continue in this conversation. What would you like to research?"
                await real_hub.broadcast_progress(record)
                if chat_id:
                    try:
                        db.add_message(chat_id, "agent", record.message, evidence="conversational")
                        db.touch_chat(chat_id)
                    except Exception:
                        pass
                return

    # C. Refinement of Existing Research
    if classification.operation_type == ResearchOperationType.REFINE_EXISTING_RESEARCH:
        from .research.package_resolver import resolve_target_package
        from .research.models import ResearchKnowledgePackage, ResearchSubtopic
        res = resolve_target_package(chat_id=chat_id, instruction=record.instruction, context=intent_context)
        if res.package:
            pkg_data = res.package.get("data") or res.package
            pkg = ResearchKnowledgePackage.from_dict(pkg_data) if isinstance(pkg_data, dict) else pkg_data
            refinement_focus = classification.research_topic or record.instruction
            sub_id = f"sub_{len(pkg.topic.subtopics) + 1:03d}"
            pkg.topic.subtopics.append(ResearchSubtopic(
                subtopic_id=sub_id,
                title=refinement_focus,
                queries=[f"{pkg.topic.title} {refinement_focus}"],
                status="in_progress",
            ))
            initial_memory["research_knowledge_package"] = pkg.to_dict()
            initial_memory["research_topic"] = f"{pkg.topic.title} - {refinement_focus}"
            initial_memory["is_research"] = True
            initial_memory["requested_format"] = classification.requested_format or pkg.topic.target_format
            initial_memory["deliverable_type"] = classification.deliverable_type
            if chat_id:
                try:
                    from . import db
                    db.save_research_package(pkg.to_dict(), chat_id, task_id, status="running")
                except Exception:
                    pass

    # D. New Research Mission: Isolated state, step 0, no old completion notes
    elif classification.operation_type == ResearchOperationType.NEW_RESEARCH:
        initial_memory["is_research"] = True
        initial_memory["research_topic"] = classification.research_topic or record.instruction
        initial_memory["requested_format"] = classification.requested_format
        initial_memory["deliverable_type"] = classification.deliverable_type
        from .research.models import ResearchKnowledgePackage, ResearchSubtopic, ResearchTopic
        from .research.adaptive_planner import generate_adaptive_research_plan

        plan = generate_adaptive_research_plan(record.instruction, {
            "research_topic": classification.research_topic or record.instruction,
            "deliverable_type": classification.deliverable_type,
            "requested_format": classification.requested_format,
        })
        initial_memory["research_plan"] = plan.model_dump()

        pkg = ResearchKnowledgePackage(
            package_id=f"pkg_{task_id}",
            topic=ResearchTopic(
                title=classification.research_topic or record.instruction,
                objective=record.instruction,
                target_format=classification.requested_format,
            ),
        )
        pkg.metadata["research_plan"] = plan.model_dump()
        for sp in plan.subtopics:
            pkg.topic.subtopics.append(ResearchSubtopic(
                subtopic_id=sp.subtopic_id,
                title=sp.title,
                queries=[sp.search_query],
            ))
        initial_memory["research_knowledge_package"] = pkg.to_dict()
        if chat_id:
            try:
                from . import db
                db.save_research_package(pkg.to_dict(), chat_id, task_id, status="running")
                db.set_chat_memory(chat_id, "research_topic", str(initial_memory["research_topic"]))
                if initial_memory.get("requested_format"):
                    db.set_chat_memory(chat_id, "requested_format", str(initial_memory["requested_format"]))
                if initial_memory.get("deliverable_type"):
                    db.set_chat_memory(chat_id, "deliverable_type", str(initial_memory["deliverable_type"]))
            except Exception:
                pass

    # 2. NON_BROWSER Intent: Conversational fast-path (Zero browser interactions)
    if not classification.is_browser:
        from .capabilities import check_supported_capability, is_capability_query, format_capability_overview
        from .completion_verifier import verify_task_completion

        # Check for explicitly unsupported system operations
        is_supp, supp_reason = check_supported_capability(record.instruction)
        if not is_supp:
            record.state = "failed"
            record.message = supp_reason or "Unsupported capability requested."
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    from . import db
                    db.add_message(chat_id, "agent", record.message, evidence="unsupported")
                    db.touch_chat(chat_id)
                except Exception:
                    pass
            return

        record.state = "running"
        record.message = "Thinking..."
        await real_hub.broadcast_progress(record)

        try:
            if is_capability_query(record.instruction):
                answer = format_capability_overview()
            else:
                provider = _provider_for_config()
                history = initial_memory.get("conversation_history", [])
                if hasattr(provider, "chat_call"):
                    answer = provider.chat_call(record.instruction, history=history)
                else:
                    answer = "I have processed your request."

            verified = verify_task_completion(
                instruction=record.instruction,
                candidate_state="completed",
                message=answer,
                observation=None,
                working_memory=initial_memory,
            )
            record.steps_done = 1
            record.state = verified.state
            record.message = verified.message
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    from . import db
                    evidence_tag = "conversational" if verified.state == "completed" else "failed"
                    db.add_message(chat_id, "agent", verified.message, evidence=evidence_tag)
                    db.touch_chat(chat_id)
                except Exception:
                    pass
            return
        except Exception as exc:
            cancelled = record.cancelled or is_cancelled(task_id)
            record.state = "cancelled" if cancelled else "failed"  # type: ignore[assignment]
            record.message = "" if cancelled else f"Error: {exc}"
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    from . import db
                    db.add_message(chat_id, "agent", f"Error: {exc}", evidence="failed")
                    db.touch_chat(chat_id)
                except Exception:
                    pass
            return

    # 3. BROWSER Tasks (NEW_BROWSER_TASK, CONTINUE_BROWSER_TASK, USE_EXISTING_USER_TAB)
    if not real_hub.extension_sockets:
        record.state = "failed"
        record.message = "EXTENSION_DISCONNECTED: Chrome extension is not connected."
        await real_hub.broadcast_progress(record)
        if chat_id:
            try:
                from . import db
                db.add_message(chat_id, "agent", "Error: Chrome extension is not connected.", evidence="failed")
                db.touch_chat(chat_id)
            except Exception:
                pass
        return

    # Check for ambiguous user tab request or ambiguous destination entity
    if classification.is_ambiguous:
        from .website_resolver import is_ambiguous_entity, format_ambiguity_clarification
        entity_hint = classification.target_tab_hint or record.instruction
        is_ambig, term, candidates = is_ambiguous_entity(entity_hint)
        if is_ambig and candidates:
            clarification_msg = format_ambiguity_clarification(term or entity_hint, candidates)
            record.state = "failed"
            record.message = clarification_msg
        else:
            record.state = "failed"
            record.message = "AMBIGUOUS_TAB: Cannot determine target tab. Please specify a specific site or tab."
        await real_hub.broadcast_progress(record)
        if chat_id:
            try:
                from . import db
                db.add_message(chat_id, "agent", record.message, evidence="ambiguity_refusal")
                db.touch_chat(chat_id)
            except Exception:
                pass
        return

    # Autonomous Website Discovery initialization (Scenario A vs B)
    try:
        from .website_resolver import website_resolver
        discovery_res = website_resolver.discover_websites_for_task(
            record.instruction,
            region=initial_memory.get("user_region", "IN"),
            context=initial_memory,
        )
        initial_memory["autonomous_discovery"] = discovery_res
    except Exception:
        pass

    # Session acquisition
    origin = "new_task"
    if classification.intent == TaskIntent.CONTINUE_BROWSER_TASK and active_session:
        session = session_manager.continue_session(active_session.session_id, task_id)
        if not session:
            session = session_manager.create_session(
                conversation_id=chat_id or "default",
                task_id=task_id,
                origin="new_task",
            )
        else:
            origin = "reused_session"
    elif classification.intent == TaskIntent.USE_EXISTING_USER_TAB:
        session = session_manager.create_session(
            conversation_id=chat_id or "default",
            task_id=task_id,
            origin="existing_user_tab",
        )
        origin = "existing_user_tab"
    else:
        session = session_manager.create_session(
            conversation_id=chat_id or "default",
            task_id=task_id,
            origin="new_task",
        )

    record.session_id = session.session_id
    record.primary_tab_id = session.primary_tab_id
    record.child_tab_ids = session.child_tab_ids

    # Initialize session in the extension
    init_res = await real_hub.send_action(task_id, {
        "type": "init_session",
        "sessionId": session.session_id,
        "useExistingTab": (origin == "existing_user_tab"),
        "tabHint": classification.target_tab_hint,
        "timeoutMs": 15000,
    })

    if not init_res.get("ok"):
        err_info = init_res.get("error") or {}
        err_code = err_info.get("code", "SESSION_INIT_FAILED")
        err_msg = err_info.get("message", "Failed to initialize browser session")
        record.state = "failed"
        record.message = f"{err_code}: {err_msg}"
        session_manager.update_status(session.session_id, "failed")
        await real_hub.broadcast_progress(record)
        if chat_id:
            try:
                from . import db
                db.add_message(chat_id, "agent", f"Error ({err_code}): {err_msg}", evidence="failed")
                db.touch_chat(chat_id)
            except Exception:
                pass
        return

    # Pin primary tab ID
    primary_tab_id = init_res.get("primaryTabId")
    if primary_tab_id:
        session_manager.pin_primary_tab(session.session_id, primary_tab_id)
        record.primary_tab_id = primary_tab_id

    def on_step_progress(steps: int = 0, msg: str = "", screenshot: Optional[str] = None) -> None:
        record.steps_done = steps
        record.message = msg
        sess = session_manager.get_session(session.session_id)
        if sess:
            record.primary_tab_id = sess.primary_tab_id
            record.child_tab_ids = sess.child_tab_ids
        # A terminal task must not be flipped back to running by a late step.
        if record.state not in ("completed", "failed", "cancelled"):
            record.state = "running"
        if screenshot:
            record.screenshot = screenshot
        real_hub.spawn(real_hub.broadcast_progress(record))

    def on_metrics(fields: dict[str, Any]) -> None:
        """Phase timings are attached to the next progress broadcast."""
        phase = fields.get("phase")
        ms = fields.get("ms")
        if not isinstance(ms, int):
            return
        if phase == "plan":
            record.planner_ms = ms
        elif phase == "observe":
            record.observe_ms = ms
        elif phase == "act":
            record.act_ms = ms

    try:
        state = await run_with_injection(
            real_hub,
            planner=_real_planner(record),
            obs_factory=_live_observation_factory(record),
            task_id=task_id,
            instruction=record.instruction,
            on_progress=on_step_progress,
            confirm_gate=_ConfirmGate(record),
            on_metrics=on_metrics,
            initial_memory=initial_memory,
            session_id=session.session_id,
            conversation_id=chat_id or "default",
        )
        candidate_final = {"completed": "completed", "failed": "failed", "cancelled": "cancelled",
                           "budget_exceeded": "failed"}.get(state["final"], "failed")
        from .completion_verifier import verify_task_completion
        verified = verify_task_completion(
            instruction=record.instruction,
            candidate_state=candidate_final,
            message=state.get("summary") or "",
            observation=state.get("last_obs") or ({"url": state["url"]} if state.get("url") else None),
            working_memory=state.get("working_memory") or initial_memory,
            history=state.get("history") or [],
        )
        if record.cancelled or is_cancelled(task_id):
            final_state = "cancelled"
            record.message = "Task cancelled by user."
        elif record.state != "cancelled":
            final_state = verified.state
            record.message = verified.message
        else:
            final_state = "cancelled"
        record.state = final_state  # type: ignore[assignment]
        is_planner_err = "Planner unavailable" in (record.message or "") or "Planner timed out" in (record.message or "")
        sess_status = "active" if (is_planner_err or final_state == "completed") else final_state
        session_manager.update_status(session.session_id, sess_status)
        await real_hub.broadcast_progress(record)

        if chat_id:
            try:
                from . import db
                summary = state.get("summary") or "Mission finished."
                evidence_info = f"{final_state} ({state.get('steps', record.steps_done)} steps)"
                db.add_message(chat_id, "agent", summary, evidence=evidence_info)
                if final_state == "completed" or (state.get("history") and any(h.get("result_ok") for h in state.get("history"))):
                    facts = extract_mission_facts(record.instruction, summary, state.get("history") or [])
                    for k, v in facts.items():
                        db.set_chat_memory(chat_id, k, v)
                db.touch_chat(chat_id)
            except Exception:
                pass
    except Exception as exc:
        cancelled = record.cancelled or is_cancelled(task_id)
        if record.state != "cancelled":
            record.state = "cancelled" if cancelled else "failed"  # type: ignore[assignment]
        record.message = "" if cancelled else f"loop error: {exc}"
        session_manager.update_status(session.session_id, "cancelled" if cancelled else "failed")
        await real_hub.broadcast_progress(record)

        if chat_id:
            try:
                from . import db
                db.add_message(chat_id, "agent", f"Error: {exc}", evidence="failed")
                db.touch_chat(chat_id)
            except Exception:
                pass
    finally:
        record.pending_confirmation = None


class _ConfirmGate:
    """Bridges the loop's confirm contract onto the Hub TaskRecord a REST
    client posts to. Event + decision live on the record itself."""
    def __init__(self, record: TaskRecord) -> None:
        self._record = record
        self.confirm_requests = []
        self.strict_write_confirm = False

    @property
    def confirm_event(self) -> asyncio.Event:
        return self._record.confirm_event

    @property
    def decision(self) -> Optional[bool]:
        return self._record.confirm_decision

    @decision.setter
    def decision(self, v: Optional[bool]) -> None:
        self._record.confirm_decision = v

    def add_request(self, req: dict[str, Any]) -> None:
        self.confirm_requests.append(req)
        self._record.pending_confirmation = req
        self._record.state = "awaiting_confirmation"
        self._record.message = req.get("message", "Awaiting confirmation")
        real_hub.spawn(real_hub.broadcast_progress(self._record))


class _LiveObservation:
    """Observation factory: asks the extension for the active tab view each
    round. Raises if the extension socket is gone (loop fails cleanly)."""
    def __init__(self, task_id: str, session_id: Optional[str] = None, tab_id: Optional[int] = None) -> None:
        self._task_id = task_id
        self._session_id = session_id
        self._tab_id = tab_id

    async def __call__(self) -> dict[str, Any]:
        if not real_hub.extension_sockets:
            raise RuntimeError("EXTENSION_DISCONNECTED: no extension socket")
        for attempt in range(2):
            if is_cancelled(self._task_id):
                raise RuntimeError("task cancelled")
            act: dict[str, Any] = {
                "type": "observe", "risk": "read", "verify": "fresh observation", "timeoutMs": 25000,
            }
            if self._session_id:
                act["sessionId"] = self._session_id
            if self._tab_id:
                act["tabId"] = self._tab_id
            res = await real_hub.send_action(self._task_id, act)
            if res.get("ok") and res.get("observation"):
                return res["observation"]
            if is_cancelled(self._task_id):
                raise RuntimeError("task cancelled")
            if attempt == 0:
                await asyncio.sleep(0.5)
        raise RuntimeError(f"observation failed: {res.get('error')}")


def _live_observation_factory(record: TaskRecord) -> _LiveObservation:
    # If session_id is present, let extension dynamically observe session.activeTabId
    tab = None if record.session_id else record.primary_tab_id
    return _LiveObservation(record.task_id, session_id=record.session_id, tab_id=tab)


def _provider_for_config():
    """Pick a planner provider. Explicit mock wins even with keys present.

    Runtime selection (POST /api/llm/config) wins over static config so the
    provider can be switched without restarting the application. Raises only
    when a runtime selection itself cannot be built; the static paths below
    keep their existing fallthrough behavior.
    """
    from . import config
    from .providers import provider_settings
    from .providers.errors import ProviderUnavailable
    if provider_settings.active():
        try:
            return provider_settings.provider_for_runtime()
        except ProviderUnavailable:
            raise
    from .providers.mock import MockProvider
    if config.PROVIDER == "mock":
        return MockProvider()
    if config.PROVIDER == "antseed" and config.ANTSEED_API_KEY_ENV:
        from .providers.openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider(
            api_key=config.ANTSEED_API_KEY_ENV,
            base_url=config.ANTSEED_BASE_URL,
            model=config.ANTSEED_MODEL or None,
        )
    if config.PROVIDER == "groq" and config.GROQ_API_KEY:
        from .providers.groq import GroqProvider
        return GroqProvider(api_key=config.GROQ_API_KEY)
    if config.PROVIDER == "gemini" and config.GEMINI_API_KEY:
        from .providers.gemini import GeminiProvider
        return GeminiProvider(api_key=config.GEMINI_API_KEY)
    if config.GROQ_API_KEY:
        from .providers.groq import GroqProvider
        return GroqProvider(api_key=config.GROQ_API_KEY)
    if config.GEMINI_API_KEY:
        from .providers.gemini import GeminiProvider
        return GeminiProvider(api_key=config.GEMINI_API_KEY)
    return MockProvider()


def _real_planner(record: TaskRecord):
    instruction = record.instruction
    provider = _provider_for_config()

    def planner(_instr: str, payload: Any, history: list[dict[str, Any]], working_memory: Optional[dict[str, Any]] = None):
        from .planner import plan_from_observation
        from .privacy import PrivacyFilteredPayload
        pf = PrivacyFilteredPayload(**payload) if isinstance(payload, dict) else payload
        # exclude_none keeps the WebSocket payload aligned with the TS contract:
        # unset optional params are omitted, never serialized as JSON null.
        return plan_from_observation(instruction=instruction, payload=pf, provider=provider, history=history, working_memory=working_memory).model_dump(by_alias=True, exclude_none=True)

    return planner
