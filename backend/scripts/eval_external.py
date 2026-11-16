"""Readiness checker + runbook for the external eval suites (Task 6).

WebArena-Verified, OSWorld 2.0, PrivacyLens and AgentDojo cannot run in
this workspace: there is no live Chrome, no hosted eval site, no VM, and
no LLM API key. This script does NOT fake a run. For each suite it:

* checks the prerequisites that can be checked locally (Python package
  importable, env keys present, Chrome binary present, backend reachable,
  local LLM server reachable),
* writes a readiness JSON (``evals/results/<suite>_readiness_<date>.json``)
  with ``status`` ``ready`` | ``blocked`` and the exact missing items,
* prints the exact commands to run the suite on a machine that has them.

A PrivAgent adapter for each external harness (mapping its task/agent
interface onto the PrivAgent loop) does not exist yet — that is stated
in the readiness output as ``adapter: not_implemented``. The mock
measurements that *can* run here live in ``eval_live.py``.

Usage (from the repo root)::

    python backend/scripts/eval_external.py webarena
    python backend/scripts/eval_external.py agentdojo --out-dir evals
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import sys
import urllib.request
from datetime import date
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_DIR / "backend"

SUITES: dict[str, dict] = {
    "webarena": {
        "full_name": "WebArena-Verified",
        "needs": [
            "web-arena repo cloned (https://github.com/web-arena-x/webarena)",
            "hosted or self-hosted WebArena sites (Shopping, Reddit, GitLab, Maps, Wikipedia)",
            "Chrome + PrivAgent extension connected to the backend",
            "LLM API key: GEMINI_API_KEY or GROQ_API_KEY or OPENAI_API_KEY",
            "PrivAgent adapter mapping WebArena's agent interface onto the PrivAgent loop",
        ],
        "package": None,
        "env_keys": ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"),
        "command": (
            "git clone https://github.com/web-arena-x/webarena && cd webarena && "
            "pip install -r requirements.txt && "
            "python run.py --instruction_path <webarena-verified-tasks.json> "
            "--agent privagent --model <model>  # adapter not implemented yet"
        ),
    },
    "osworld": {
        "full_name": "OSWorld 2.0",
        "needs": [
            "OSWorld repo cloned (https://github.com/xlang-ai/OSWorld)",
            "VM hypervisor (VMware / VirtualBox / QEMU) with the OSWorld Ubuntu VM image",
            "LLM API key: GEMINI_API_KEY or GROQ_API_KEY or OPENAI_API_KEY",
            "PrivAgent adapter mapping OSWorld's computer-use interface onto the PrivAgent loop",
        ],
        "package": None,
        "env_keys": ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"),
        "command": (
            "git clone https://github.com/xlang-ai/OSWorld && cd OSWorld && "
            "pip install -r requirements.txt && "
            "python run.py --headless --model <model> --agent privagent  # adapter not implemented yet"
        ),
    },
    "privacylens": {
        "full_name": "PrivacyLens",
        "needs": [
            "PrivacyLens repo + benchmark data (https://github.com/privacy-lens/PrivacyLens)",
            "LLM API key: GEMINI_API_KEY or GROQ_API_KEY or OPENAI_API_KEY",
            "PrivAgent adapter mapping PrivacyLens tasks onto the PrivAgent loop",
        ],
        "package": None,
        "env_keys": ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"),
        "command": (
            "git clone https://github.com/privacy-lens/PrivacyLens && cd PrivacyLens && "
            "pip install -r requirements.txt && "
            "python run_privacylens.py --agent privagent  # adapter not implemented yet"
        ),
    },
    "agentdojo": {
        "full_name": "AgentDojo",
        "needs": [
            "agentdojo installed (pip install agentdojo) — verified installable here, version 0.1.35",
            "LLM API key (OPENAI_API_KEY / ANTHROPIC_API_KEY / GOOGLE_API_KEY / COHERE_API_KEY / TOGETHER_API_KEY) "
            "OR a local OpenAI-compatible LLM server on localhost:$LOCAL_LLM_PORT (default 8000) with --model local",
            "PrivAgent adapter: AgentDojo's AgentPipeline expects an LLM producing tool calls; "
            "PrivAgent's planner/loop is a browser agent, not that interface",
            "Note: LOCAL_LLM_PORT defaults to 8000, which collides with the PrivAgent backend port — set LOCAL_LLM_PORT to a free port",
        ],
        "package": "agentdojo",
        "env_keys": ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "COHERE_API_KEY", "TOGETHER_API_KEY"),
        "command": (
            "pip install agentdojo && "
            "python -m agentdojo.scripts.benchmark --suite v1 --model gpt-4o-2024-05-13 "
            "--logdir evals/agentdojo  # with a PrivAgent agent adapter, not implemented yet; "
            "or --model local with a local LLM server on $LOCAL_LLM_PORT"
        ),
    },
}


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _backend_healthy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def check_suite(name: str) -> dict:
    spec = SUITES[name]
    missing: list[str] = []
    present: list[str] = []

    if spec["package"]:
        if importlib.util.find_spec(spec["package"]) is not None:
            present.append(f"python package '{spec['package']}' importable")
        else:
            missing.append(f"python package '{spec['package']}' not installed")
    for key in spec["env_keys"]:
        if os.environ.get(key):
            present.append(f"env {key} set")
        else:
            missing.append(f"env {key} not set")
    if name == "agentdojo":
        port = int(os.environ.get("LOCAL_LLM_PORT", "8000"))
        if _port_open("127.0.0.1", port):
            present.append(f"local LLM server reachable on 127.0.0.1:{port}")
        else:
            missing.append(f"no local LLM server on 127.0.0.1:{port} (LOCAL_LLM_PORT)")
    if name in ("webarena", "osworld"):
        if shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser"):
            present.append("Chrome/Chromium binary present")
        else:
            missing.append("no Chrome/Chromium binary on PATH")
    if _backend_healthy():
        present.append("PrivAgent backend reachable at http://127.0.0.1:8000/api/health")
    else:
        missing.append("PrivAgent backend not reachable at http://127.0.0.1:8000 (start it first)")

    # The adapter gap applies to every external suite: none of them can
    # drive the PrivAgent loop as-is.
    missing.append("PrivAgent adapter for this harness: not implemented")
    return {
        "suite": name,
        "full_name": spec["full_name"],
        "status": "blocked",
        "adapter": "not_implemented",
        "present": present,
        "missing": missing,
        "needs": list(spec["needs"]),
        "command": spec["command"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=sorted(SUITES))
    parser.add_argument("--out-dir", type=Path, default=REPO_DIR / "evals")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args(argv)

    report = check_suite(args.suite)
    results_dir = args.out_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{args.suite}_readiness_{args.date}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"{report['full_name']}: {report['status'].upper()}")
    for item in report["present"]:
        print(f"  [have]    {item}")
    for item in report["missing"]:
        print(f"  [missing] {item}")
    print(f"  command: {report['command']}")
    print(f"Wrote {path}")
    return 2  # blocked: a readiness check that is not ready exits non-zero


if __name__ == "__main__":
    raise SystemExit(main())
