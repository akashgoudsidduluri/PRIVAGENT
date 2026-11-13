"""Mock provider — deterministic plans for tests and no-key demos.

Every plan it emits is tagged in the planner output path so it can never be
confused with real AI execution (NAVIGATOR_PROVIDER=mock is set explicitly).
"""
from __future__ import annotations

from typing import Any, Optional

from ..privacy import PrivacyFilteredPayload


class MockProvider:
    """_ = payload arg unused but kept for interface parity."""
    force_invalid: bool = False

    def name(self) -> str:
        return "mock"

    def plan_call(self, instruction: str, payload: PrivacyFilteredPayload, history: list[dict[str, Any]], working_memory: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if self.force_invalid:
            return {"goal": "x", "steps": [{"action": {"type": "run_js", "risk": "read", "verify": "y", "timeoutMs": 1000}, "why": "bad"}]}
        steps: list[dict[str, Any]] = []
        # Find a text field to type into, else a button to click.
        text_el = next((e for e in payload.interactive if e.tag == "input" and e.enabled and e.input_type not in ("password", "hidden")), None)
        btn = next((e for e in payload.interactive if e.role in ("button", "link") and e.enabled), None)
        if text_el and "type" not in {s["action"]["type"] for s in history}:
            steps.append({
                "action": {"type": "type", "risk": "write", "verify": "field filled", "timeoutMs": 5000,
                           "target": {"targetId": text_el.target_id, "name": text_el.name},
                           "params": {"text": instruction, "clear": True, "submit": True}},
                "why": "type the instruction into the main text field",
            })
        elif btn:
            steps.append({
                "action": {"type": "click", "risk": "write", "verify": "page reacts", "timeoutMs": 5000,
                           "target": {"targetId": btn.target_id, "name": btn.name}},
                "why": "click the primary control",
            })
        else:
            steps.append({
                "action": {"type": "navigate", "risk": "write", "verify": "url changed", "timeoutMs": 15000,
                           "params": {"url": "https://www.google.com"}},
                "why": "nothing useful on page; go to google",
            })
        return {"goal": instruction, "steps": steps}

    def chat_call(self, prompt: str, history: Optional[list[dict[str, Any]]] = None,
                  system_prompt: Optional[str] = None) -> str:
        lower = (prompt or "").strip().lower()
        if any(w in lower for w in ("hi", "hello", "hey", "greetings", "howdy")):
            return "Hello! I am PrivAgent, your privacy-preserving AI assistant. How can I help you today?"
        if "transformer" in lower:
            return "Transformers are deep learning architectures based on self-attention mechanisms that process input sequences in parallel rather than sequentially."
        if "bfs" in lower or "c++" in lower:
            return "Here is a C++ BFS implementation:\n\n```cpp\n#include <iostream>\n#include <vector>\n#include <queue>\n\nvoid bfs(int start, const std::vector<std::vector<int>>& adj) {\n    std::vector<bool> visited(adj.size(), false);\n    std::queue<int> q;\n    q.push(start);\n    visited[start] = true;\n    while (!q.empty()) {\n        int u = q.front(); q.pop();\n        std::cout << u << \" \";\n        for (int v : adj[u]) {\n            if (!visited[v]) {\n                visited[v] = true;\n                q.push(v);\n            }\n        }\n    }\n}\n```"
        if "browser automation" in lower:
            return "Browser automation refers to programmatically controlling a web browser to automate web tasks, form fills, testing, and navigation without manual human interaction."
        if "google search" in lower:
            return "Google Search works by crawling web pages via automated spiders (Googlebot), indexing text and metadata in a massive database, and ranking pages against user queries using algorithms like PageRank and AI relevance models."
        if "calculate" in lower or "compute" in lower or "%" in lower or "*" in lower or "solve" in lower:
            if "25%" in lower and "800" in lower:
                return "25% of 800 is 200."
            if "125" in lower and "8" in lower:
                return "125 * 8 is 1000."
            return f"Calculated answer for: {prompt}"
        return f"Here is the requested information regarding: {prompt}"


class InjectionProbeProvider(MockProvider):
    """Test double: emits a plan containing a non-whitelisted action type to
    prove the planner's schema gate rejects it."""


provider_instance: MockProvider | InjectionProbeProvider = MockProvider()
_current: str = "mock"


def get_provider():
    global provider_instance, _current
    return provider_instance


def set_provider(p: Any) -> None:
    global provider_instance
    provider_instance = p
