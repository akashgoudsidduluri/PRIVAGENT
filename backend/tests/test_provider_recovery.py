import asyncio
import threading
import time
import pytest
from app.contracts import BrowserObservation
from app.privacy import classify_and_redact
from app.providers import gemini as gemini_mod
from app.providers import groq as groq_mod
from app.providers.gemini import GeminiProvider, _block_model, _model_blocked, _MODEL_BLOCKED_UNTIL
from app.providers.groq import GroqProvider, _block_groq, _groq_blocked
from app.providers.limits import quota_scope, parse_retry_seconds
from app.providers.errors import ProviderUnavailable
from app import agentLoop, config
from app.hub import Hub, TaskRecord


@pytest.fixture(autouse=True)
def _reset_provider_blocks():
    """Ensure process-wide cooldowns and executor states do not leak across tests."""
    groq_mod._GROQ_BLOCKED_UNTIL = 0.0
    _MODEL_BLOCKED_UNTIL.clear()
    agentLoop.shutdown_planner_executor(wait=False, cancel_futures=True)
    yield
    groq_mod._GROQ_BLOCKED_UNTIL = 0.0
    _MODEL_BLOCKED_UNTIL.clear()
    agentLoop.shutdown_planner_executor(wait=False, cancel_futures=True)


def _payload():
    obs = BrowserObservation(
        tab_id=1,
        url="https://example.com",
        title="Example",
        interactive=[],
        scroll={},
        dpr=1.0,
    )
    p, _ = classify_and_redact(obs, instruction="test")
    return p


class _Response:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self):
        import json
        return json.loads(self.text)


class _Client:
    def __init__(self, resp: _Response) -> None:
        self.resp = resp
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        return self.resp


# ---------------------------------------------------------------------------
# Provider Timeouts & Client Configuration
# ---------------------------------------------------------------------------

def test_gemini_http_timeout_configured():
    """GeminiProvider passes http_options timeout in milliseconds to Client."""
    prov = GeminiProvider(api_key="test-key", timeout_s=35.0)
    client_http_options = getattr(prov._client, "_http_options", None)
    if client_http_options is None:
        assert prov._timeout_s == 35.0
    else:
        assert client_http_options.timeout == 35000


def test_gemini_cooldown_when_retry_after_is_missing(monkeypatch):
    """When Gemini returns a 429 without parseable retry delay, cooldown is 60s, NOT 3600s."""
    _MODEL_BLOCKED_UNTIL.clear()

    class _Models:
        def __init__(self) -> None:
            self.calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED Quota exceeded for metric")

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _Models()

    prov = GeminiProvider(api_key="dummy")
    monkeypatch.setattr(prov, "_client", _GeminiClient())
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)

    with pytest.raises(ProviderUnavailable):
        prov.plan_call("test", _payload(), [])

    block_time = _MODEL_BLOCKED_UNTIL.get(prov.DEFAULT_MODEL, 0.0)
    assert block_time > now
    assert block_time <= now + 65.0, f"Expected ~60s cooldown, got {block_time - now}s"


def test_gemini_daily_quota_exhaustion_identified(monkeypatch):
    """When Gemini returns a daily cap error, 3600s cooldown is preserved."""
    _MODEL_BLOCKED_UNTIL.clear()

    class _DailyModels:
        def generate_content(self, **kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED Quota exceeded for quota_group: GenerateRequestsPerDay")

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _DailyModels()

    prov = GeminiProvider(api_key="dummy")
    monkeypatch.setattr(prov, "_client", _GeminiClient())
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)

    with pytest.raises(ProviderUnavailable):
        prov.plan_call("test", _payload(), [])

    block_time = _MODEL_BLOCKED_UNTIL.get(prov.DEFAULT_MODEL, 0.0)
    assert block_time >= now + 3550.0, f"Expected ~3600s cooldown, got {block_time - now}s"


# ---------------------------------------------------------------------------
# Daily Quota String Normalization & Scenarios
# ---------------------------------------------------------------------------

def test_quota_scope_daily_string_formats():
    """Verify spaced, underscored, camelCase, concatenated, and abbreviation formats."""
    daily_cases = [
        "Rate limit reached on requests per day (RPD): Limit 14400",
        "tokens per day limit exceeded",
        "generate_requests_per_day",
        "generate_content_tokens_per_day",
        "GenerateRequestsPerDay",
        "generateContentRequestsPerDay",
        "generaterequestsperday",
        "tokensperday",
        "daily quota exceeded for user",
        "per-day-limit reached",
        "quota exceeded: rpd limit reached",
        "quota exceeded: tpd limit reached",
    ]
    for s in daily_cases:
        assert quota_scope(s) == "day", f"Expected 'day' for {s!r}, got {quota_scope(s)!r}"

    minute_cases = [
        "Rate limit reached on requests per minute (RPM). Please try again in 12s.",
        "tokens per minute (TPM) limit reached",
        "generate_requests_per_minute",
        "GenerateRequestsPerMinute",
        "generaterequestsperminute",
        "tokensperminute",
        "per-minute-limit reached",
        "quota exceeded: rpm limit reached",
        "quota exceeded: tpm limit reached",
    ]
    for s in minute_cases:
        assert quota_scope(s) == "minute", f"Expected 'minute' for {s!r}, got {quota_scope(s)!r}"

    transient_cases = [
        "429 RESOURCE_EXHAUSTED Quota exceeded for metric",
        "503 Service Unavailable: Gemini overloaded",
        "429 Too Many Requests. Retry after 5s.",
        "connection reset by peer",
        "request timed out after 45s",
    ]
    for s in transient_cases:
        assert quota_scope(s) == "", f"Transient limit was incorrectly classified as {quota_scope(s)!r} for {s!r}"


def test_groq_rpm_tpm_cooldown(monkeypatch):
    """Groq TPM/RPM limit sets cooldown so subsequent plan calls don't thrash exhausted buckets."""
    groq_mod._GROQ_BLOCKED_UNTIL = 0.0
    client = _Client(_Response(429, (
        '{"error":{"message":"Rate limit reached on tokens per minute (TPM). Please try again in 14s."}}'
    )))
    groq_provider = GroqProvider(api_key="dummy")
    monkeypatch.setattr(groq_provider, "_client", client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "")
    sleeps = []
    monkeypatch.setattr(groq_mod.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(ProviderUnavailable):
        groq_provider.plan_call("test", _payload(), [])

    assert _groq_blocked() is True
    assert client.calls == 3
    with pytest.raises(ProviderUnavailable) as excinfo:
        groq_provider.plan_call("test", _payload(), [])
    assert client.calls == 3
    assert "Groq quota exhausted" in str(excinfo.value) or "No Gemini key" in str(excinfo.value)


def test_groq_daily_quota_exhaustion_skips_cascading(monkeypatch):
    """Daily cap halts Groq immediately without burning remaining fallback models."""
    groq_mod._GROQ_BLOCKED_UNTIL = 0.0
    client = _Client(_Response(429, (
        '{"error":{"message":"Rate limit reached on requests per day (RPD): Limit 14400, Used 14400. Please try again in 6h."}}'
    )))
    groq_provider = GroqProvider(api_key="dummy")
    monkeypatch.setattr(groq_provider, "_client", client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "")

    with pytest.raises(ProviderUnavailable):
        groq_provider.plan_call("test", _payload(), [])

    # Exited after exactly 1 call (did not cascade through other 2 Groq models)
    assert client.calls == 1
    assert _groq_blocked() is True


# ---------------------------------------------------------------------------
# Planner Concurrency & Thread Pool Bounding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ten_simultaneous_planner_requests_bounded_to_max_workers(monkeypatch):
    """10 simultaneous tasks running concurrently cannot exceed MAX_CONCURRENT_PLANNERS active threads."""
    active_workers = 0
    max_workers_seen = 0
    lock = threading.Lock()

    def _sync_planner(*args, **kwargs):
        nonlocal active_workers, max_workers_seen
        with lock:
            active_workers += 1
            if active_workers > max_workers_seen:
                max_workers_seen = active_workers
        try:
            time.sleep(0.15)
            return {"goal": "bounded success", "steps": []}
        finally:
            with lock:
                active_workers -= 1

    monkeypatch.setattr(agentLoop, "MAX_CONCURRENT_PLANNERS", 4)
    # Restart executor with limit 4
    agentLoop.shutdown_planner_executor(wait=True)

    obs_dict = {
        "tabId": 1,
        "url": "https://example.com",
        "title": "Example",
        "interactive": [],
        "scroll": {},
    }

    async def _run_one(i: int):
        hub = Hub()
        return await agentLoop.run_with_injection(
            hub,
            planner=_sync_planner,
            obs_factory=lambda: obs_dict,
            task_id=f"concurrent-task-{i}",
            instruction=f"task {i}",
        )

    tasks = [asyncio.create_task(_run_one(i)) for i in range(10)]
    results = await asyncio.gather(*tasks)

    for r in results:
        assert r["final"] == "completed"

    assert max_workers_seen <= 4, f"Observed {max_workers_seen} concurrent workers, expected <= 4"
    assert active_workers == 0


@pytest.mark.asyncio
async def test_multiple_planner_timeouts_without_exceeding_four_active_workers(monkeypatch):
    """Multiple tasks timing out simultaneously cannot exceed MAX_CONCURRENT_PLANNERS active threads."""
    active_workers = 0
    max_workers_seen = 0
    lock = threading.Lock()

    def _slow_planner(*args, **kwargs):
        nonlocal active_workers, max_workers_seen
        with lock:
            active_workers += 1
            if active_workers > max_workers_seen:
                max_workers_seen = active_workers
        try:
            time.sleep(0.4)
            return {"goal": "slow", "steps": []}
        finally:
            with lock:
                active_workers -= 1

    monkeypatch.setattr(agentLoop, "MAX_CONCURRENT_PLANNERS", 4)
    monkeypatch.setattr(agentLoop, "PLANNER_TIMEOUT_S", 0.05)
    agentLoop.shutdown_planner_executor(wait=True)

    obs_dict = {
        "tabId": 1,
        "url": "https://example.com",
        "title": "Example",
        "interactive": [],
        "scroll": {},
    }

    async def _run_one(i: int):
        hub = Hub()
        return await agentLoop.run_with_injection(
            hub,
            planner=_slow_planner,
            obs_factory=lambda: obs_dict,
            task_id=f"timeout-task-{i}",
            instruction=f"timeout task {i}",
        )

    tasks = [asyncio.create_task(_run_one(i)) for i in range(10)]
    results = await asyncio.gather(*tasks)

    for r in results:
        assert r["final"] == "failed"
        assert "Planner timed out" in r["summary"]

    assert max_workers_seen <= 4, f"Observed {max_workers_seen} concurrent workers during timeout storm, expected <= 4"

    # Wait for active workers to wind down
    await asyncio.sleep(0.5)
    assert active_workers == 0


@pytest.mark.asyncio
async def test_queued_planner_jobs_timing_out_before_execution(monkeypatch):
    """Queued planner tasks that time out before thread assignment are cancelled in queue."""
    started_jobs = []
    lock = threading.Lock()

    def _blocking_planner(instr, *args, **kwargs):
        with lock:
            started_jobs.append(instr)
        time.sleep(0.3)
        return {"goal": "done", "steps": []}

    monkeypatch.setattr(agentLoop, "MAX_CONCURRENT_PLANNERS", 2)
    monkeypatch.setattr(agentLoop, "PLANNER_TIMEOUT_S", 0.05)
    agentLoop.shutdown_planner_executor(wait=True)

    obs_dict = {
        "tabId": 1,
        "url": "https://example.com",
        "title": "Example",
        "interactive": [],
        "scroll": {},
    }

    async def _run_one(i: int):
        hub = Hub()
        return await agentLoop.run_with_injection(
            hub,
            planner=_blocking_planner,
            obs_factory=lambda: obs_dict,
            task_id=f"queue-task-{i}",
            instruction=f"job-{i}",
        )

    tasks = [asyncio.create_task(_run_one(i)) for i in range(8)]
    results = await asyncio.gather(*tasks)

    for r in results:
        assert r["final"] == "failed"

    # Allow running workers to finish
    await asyncio.sleep(0.4)

    # With 2 workers and 0.05s timeout vs 0.3s runtime:
    # Only the first 2 workers could have started before the 0.05s timeout occurred.
    # The remaining 6 jobs were queued and should have been cancelled before running!
    with lock:
        assert len(started_jobs) == 2, f"Expected exactly 2 started jobs, but {len(started_jobs)} ran: {started_jobs}"


@pytest.mark.asyncio
async def test_running_workers_completing_after_callers_time_out(monkeypatch):
    """Running synchronous workers complete safely in background after callers time out."""
    worker_finished = threading.Event()

    def _slow_planner(*args, **kwargs):
        try:
            time.sleep(0.2)
            return {"goal": "late done", "steps": []}
        finally:
            worker_finished.set()

    monkeypatch.setattr(agentLoop, "PLANNER_TIMEOUT_S", 0.05)

    hub = Hub()
    obs_dict = {
        "tabId": 1,
        "url": "https://example.com",
        "title": "Example",
        "interactive": [],
        "scroll": {},
    }
    result = await agentLoop.run_with_injection(
        hub,
        planner=_slow_planner,
        obs_factory=lambda: obs_dict,
        task_id="caller-timeout-task",
        instruction="slow task",
    )

    assert result["final"] == "failed"
    assert "Planner timed out" in result["summary"]

    # Verify background thread finishes cleanly without error
    finished_ok = await asyncio.to_thread(worker_finished.wait, 1.0)
    assert finished_ok is True


@pytest.mark.asyncio
async def test_late_results_not_overwriting_failed_or_cancelled_tasks(monkeypatch):
    """Task cancelled or timed out cannot be overwritten by a late-arriving planner response."""
    hub = Hub()
    record = hub.new_task("cancel during slow plan")

    def _hang_planner(*args, **kwargs):
        time.sleep(0.2)
        return {"goal": "late overwrite attempt", "steps": []}

    monkeypatch.setattr(agentLoop, "PLANNER_TIMEOUT_S", 0.05)

    # Pre-cancel task
    agentLoop.request_cancel(record.task_id)

    obs_dict = {
        "tabId": 1,
        "url": "https://example.com",
        "title": "Example",
        "interactive": [],
        "scroll": {},
    }
    state = await agentLoop.run_with_injection(
        hub,
        planner=_hang_planner,
        obs_factory=lambda: obs_dict,
        task_id=record.task_id,
        instruction=record.instruction,
    )

    assert state["final"] == "cancelled"
    assert not agentLoop.is_cancelled(record.task_id)

    # Wait for the background worker to finish
    await asyncio.sleep(0.3)
    assert record.state != "running"


# ---------------------------------------------------------------------------
# Multi-Task Failover and Recovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_task_submission_after_groq_429(monkeypatch):
    """After Groq hits 429, a subsequent task cleanly succeeds via fallback."""
    groq_mod._GROQ_BLOCKED_UNTIL = 0.0
    groq_client = _Client(_Response(429, (
        '{"error":{"message":"Rate limit reached on requests per minute (RPM). Please try again in 10s."}}'
    )))
    groq_provider = GroqProvider(api_key="groq-dummy")
    monkeypatch.setattr(groq_provider, "_client", groq_client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "gemini-dummy")

    class _GeminiContentResp:
        def __init__(self, text: str) -> None:
            self.text = text

    class _GeminiModels:
        def generate_content(self, **kwargs):
            return _GeminiContentResp('{"goal": "success from fallback", "steps": []}')

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _GeminiModels()

    monkeypatch.setattr("google.genai.Client", lambda api_key, **kw: _GeminiClient())
    monkeypatch.setattr(groq_mod.time, "sleep", lambda s: None)

    plan1 = groq_provider.plan_call("task 1", _payload(), [])
    assert plan1["goal"] == "success from fallback"

    groq_client_calls_before = groq_client.calls
    plan2 = groq_provider.plan_call("task 2", _payload(), [])
    assert plan2["goal"] == "success from fallback"
    assert groq_client.calls == groq_client_calls_before


@pytest.mark.asyncio
async def test_fresh_task_submission_after_both_providers_fail(monkeypatch):
    """When both providers fail, Task 1 fails fast, but Task 2 recovers once unblocked."""
    groq_mod._GROQ_BLOCKED_UNTIL = 0.0
    _MODEL_BLOCKED_UNTIL.clear()

    groq_client = _Client(_Response(429, (
        '{"error":{"message":"Rate limit reached on tokens per day (TPD). Please try again in 5s."}}'
    )))
    groq_provider = GroqProvider(api_key="groq-dummy")
    monkeypatch.setattr(groq_provider, "_client", groq_client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "gemini-dummy")

    gemini_calls = [0]

    class _GeminiContentResp:
        def __init__(self, text: str) -> None:
            self.text = text

    class _GeminiModels:
        def generate_content(self, **kwargs):
            gemini_calls[0] += 1
            if gemini_calls[0] <= 3:
                raise RuntimeError("429 RESOURCE_EXHAUSTED retry after 5s")
            return _GeminiContentResp('{"goal": "recovered task 2", "steps": []}')

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _GeminiModels()

    monkeypatch.setattr("google.genai.Client", lambda api_key, **kw: _GeminiClient())
    monkeypatch.setattr(groq_mod.time, "sleep", lambda s: None)

    with pytest.raises(ProviderUnavailable):
        groq_provider.plan_call("task 1", _payload(), [])

    groq_mod._GROQ_BLOCKED_UNTIL = 0.0
    _MODEL_BLOCKED_UNTIL.clear()

    plan2 = groq_provider.plan_call("task 2", _payload(), [])
    assert plan2["goal"] == "recovered task 2"


def test_gemini_503_unavailable_cascades_to_fallback_model(monkeypatch):
    """503 UNAVAILABLE on primary model must block it and cascade to next model."""
    _MODEL_BLOCKED_UNTIL.clear()

    class _GeminiContentResp:
        def __init__(self, text: str) -> None:
            self.text = text

    calls = []

    class _GeminiModels:
        def generate_content(self, model, **kwargs):
            calls.append(model)
            if model == "gemini-3.8-flash":
                raise RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand.")
            return _GeminiContentResp('{"goal": "success from fallback model", "steps": []}')

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _GeminiModels()

    provider = GeminiProvider(api_key="gemini-dummy")
    monkeypatch.setattr(provider, "_client", _GeminiClient())
    monkeypatch.setattr(gemini_mod.time, "sleep", lambda s: None)

    plan = provider.plan_call("test 503 fallback", _payload(), [])
    assert plan["goal"] == "success from fallback model"
    assert calls[0] == "gemini-3.8-flash"
    assert calls[1] in ("gemini-3.5-flash-lite", "gemini-3.6-flash")
    # Primary model should now be blocked
    assert _model_blocked("gemini-3.8-flash")


@pytest.mark.asyncio
async def test_task_checkpoint_preservation_and_recovery():
    """Verify task checkpoints save and recover completed steps and memory."""
    from app import db
    db.init_db()

    chat_id = "test-chat-checkpoint-123"
    task_id = "test-task-cp-1"
    history = [
        {"action": {"type": "navigate", "params": {"url": "https://example.com"}}, "result_ok": True, "status": "verified"},
        {"action": {"type": "click", "target": {"name": "Products"}}, "result_ok": True, "status": "verified"},
    ]
    wm = {
        "notes": "Found iPhone 15 at $799",
        "collected_research": {
            "example.com": {"domain": "example.com", "price": "$799", "title": "iPhone 15"}
        }
    }

    db.save_task_checkpoint(
        task_id=task_id,
        conversation_id=chat_id,
        session_id="sess-cp-1",
        step_index=2,
        last_url="https://example.com/products",
        history=history,
        working_memory=wm,
    )

    cp = db.get_task_checkpoint(task_id)
    assert cp is not None
    assert cp["step_index"] == 2
    assert len(cp["history"]) == 2
    assert cp["working_memory"]["collected_research"]["example.com"]["price"] == "$799"

    latest = db.get_latest_checkpoint_for_chat(chat_id)
    assert latest is not None
    assert latest["task_id"] == task_id
    assert latest["step_index"] == 2


def test_retry_after_headers_respected():
    """Verify parse_retry_headers accurately parses standard HTTP rate limit headers."""
    from app.providers.limits import parse_retry_headers

    assert parse_retry_headers({"retry-after": "5"}) == 5.0
    assert parse_retry_headers({"Retry-After": "4.2"}) == 4.2
    assert parse_retry_headers({"x-ratelimit-reset-tokens": "3.84s"}) == 3.84
    assert parse_retry_headers({"x-ratelimit-reset-requests": "1m30s"}) == 90.0
    assert parse_retry_headers({}) == 0.0


@pytest.mark.asyncio
async def test_simultaneous_groq_and_gemini_quota_exhaustion(monkeypatch):
    """Simultaneous Groq 429 and Gemini quota exhaustion fails fast without futile retry loops."""
    groq_mod._GROQ_BLOCKED_UNTIL = 0.0
    _MODEL_BLOCKED_UNTIL.clear()

    # Groq returns 429 TPM with 45s cooldown
    groq_client = _Client(_Response(429, '{"error":{"message":"Rate limit exceeded on tokens per minute (TPM). Please try again in 45s."}}'))
    groq_provider = GroqProvider(api_key="groq-dummy")
    monkeypatch.setattr(groq_provider, "_client", groq_client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "gemini-dummy")

    gemini_calls = [0]

    class _GeminiModels:
        def generate_content(self, **kwargs):
            gemini_calls[0] += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED retry in 45s")

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _GeminiModels()

    monkeypatch.setattr("google.genai.Client", lambda api_key, **kw: _GeminiClient())
    monkeypatch.setattr(groq_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(gemini_mod.time, "sleep", lambda s: None)

    # Calling plan_call directly
    with pytest.raises(ProviderUnavailable) as exc_info:
        groq_provider.plan_call("compare phones", _payload(), [])

    err_str = str(exc_info.value)
    assert "Groq" in err_str
    assert "Gemini" in err_str
    assert "exhausted" in err_str or "exceeded" in err_str

    # In loop execution: verify loop does NOT hammer providers in long cooldown
    planner_calls = [0]
    def _failing_planner(*args, **kwargs):
        planner_calls[0] += 1
        raise ProviderUnavailable(err_str)

    hub = Hub()
    obs_dict = {"tabId": 1, "url": "about:blank", "title": "Blank", "interactive": [], "scroll": {}}
    res = await agentLoop.run_with_injection(
        hub,
        planner=_failing_planner,
        obs_factory=lambda: obs_dict,
        task_id="simul-fail-task",
        instruction="compare phones",
        conversation_id="conv-simul",
    )
    assert res["final"] == "failed"
    assert "Planner unavailable" in res["summary"]
    # Because cooldown is >3s, loop must fail fast without repeated calls to the blocked provider!
    assert planner_calls[0] == 1


@pytest.mark.asyncio
async def test_task_resumes_from_checkpoint_without_repeating_completed_steps():
    """Verify task resuming from a checkpoint loads prior history and starts from next step."""
    from app import db
    db.init_db()

    chat_id = "chat-resume-test"
    prior_history = [
        {"action": {"type": "navigate", "params": {"url": "https://site1.com"}}, "result_ok": True, "status": "verified"},
        {"action": {"type": "click", "target": {"name": "Product 1"}}, "result_ok": True, "status": "verified"},
    ]
    db.save_task_checkpoint(
        task_id="task-prev",
        conversation_id=chat_id,
        session_id="sess-prev",
        step_index=2,
        last_url="https://site1.com/product",
        history=prior_history,
        working_memory={"collected_research": {"site1.com": {"price": "$500"}}},
    )

    planned_instructions = []
    def _planner(instruction, payload, history, working_memory=None):
        planned_instructions.append({"history_len": len(history), "working_memory": working_memory})
        return {"goal": "conclude comparison", "steps": [], "answer": "Comparison finished using prior data"}

    hub = Hub()
    obs_dict = {"tabId": 1, "url": "https://site1.com/product", "title": "Product", "interactive": [], "scroll": {}}

    latest_cp = db.get_latest_checkpoint_for_chat(chat_id)
    initial_memory = {}
    if latest_cp:
        if latest_cp.get("working_memory"):
            initial_memory.update(latest_cp["working_memory"])
        if latest_cp.get("history"):
            initial_memory["prior_checkpoint_history"] = latest_cp["history"]

    res = await agentLoop.run_with_injection(
        hub,
        planner=_planner,
        obs_factory=lambda: obs_dict,
        task_id="task-resume-next",
        instruction="continue research",
        conversation_id=chat_id,
        initial_memory=initial_memory,
    )

    assert res["final"] == "completed"
    assert res["steps"] == 2  # Started with 2 verified steps from prior checkpoint
    assert planned_instructions[0]["history_len"] == 2
    assert "site1.com" in planned_instructions[0]["working_memory"]["collected_research"]


def test_groq_primary_with_gemini_flash_then_flash_lite_fallbacks(monkeypatch):
    """Groq (primary) falls back to Gemini Flash, then to Gemini Flash-Lite."""
    groq_mod._GROQ_BLOCKED_UNTIL = 0.0
    _MODEL_BLOCKED_UNTIL.clear()

    # Groq returns 429
    groq_client = _Client(_Response(429, '{"error":{"message":"Rate limit exceeded on tokens per minute (TPM). Please try again in 5s."}}'))
    groq_provider = GroqProvider(api_key="groq-dummy")
    monkeypatch.setattr(groq_provider, "_client", groq_client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "gemini-dummy")

    gemini_models_called = []

    class _GeminiModels:
        def generate_content(self, model, **kwargs):
            gemini_models_called.append(model)
            if "3.8" in model or "3.6" in model:
                # Gemini Flash experiences 503 spike
                raise RuntimeError("503 UNAVAILABLE: Model is experiencing high demand.")
            # Flash-Lite succeeds!
            class _Resp:
                text = '{"goal": "success from flash-lite", "steps": []}'
                usage_metadata = type("Usage", (), {
                    "prompt_token_count": 120,
                    "candidates_token_count": 25,
                    "total_token_count": 145,
                })()
            return _Resp()

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _GeminiModels()

    monkeypatch.setattr("google.genai.Client", lambda api_key, **kw: _GeminiClient())
    monkeypatch.setattr(groq_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(gemini_mod.time, "sleep", lambda s: None)

    plan = groq_provider.plan_call("find specs", _payload(), [])
    assert plan["goal"] == "success from flash-lite"

    # Confirm fallback sequence:
    # 1. Groq failed
    # 2. Gemini Flash (gemini-3.8-flash, then gemini-3.6-flash) failed with 503
    # 3. Gemini Flash-Lite (gemini-3.5-flash-lite) succeeded!
    assert "gemini-3.8-flash" in gemini_models_called
    assert "gemini-3.6-flash" in gemini_models_called
    assert "gemini-3.5-flash-lite" in gemini_models_called

    # Confirm telemetry
    assert groq_provider.last_provider.startswith("gemini")
    assert groq_provider.last_model == "gemini-3.5-flash-lite"
    assert groq_provider.last_status == 200
    assert groq_provider.last_usage["total_tokens"] == 145
    assert "Flash" in (groq_provider.fallback_reason or "")


def test_deterministic_plan_avoids_llm_calls():
    """Direct site navigation produces instant deterministic plan with zero LLM tokens."""
    from app.planner import plan_from_observation, try_deterministic_plan

    called = [0]
    class _FailingProvider:
        is_llm = True

        def plan_call(self, *args, **kwargs):
            called[0] += 1
            raise RuntimeError("LLM provider should not be called for deterministic operation")

    obs = BrowserObservation(
        tab_id=1,
        url="about:blank",
        title="New Tab",
        interactive=[],
        scroll={},
        dpr=1.0,
    )
    payload, _ = classify_and_redact(obs, instruction="Open Google")

    plan = plan_from_observation(
        instruction="Open Google",
        payload=payload,
        provider=_FailingProvider(),
        history=[],
    )

    assert called[0] == 0
    assert plan.goal == "Open Google"
    assert len(plan.steps) == 1
    assert plan.steps[0].action.type == "navigate"
    assert plan.steps[0].action.params.url == "https://www.google.com"

    # Subsequent check after navigation completes: deterministically concludes
    obs_loaded = BrowserObservation(
        tab_id=1,
        url="https://www.google.com/",
        title="Google",
        interactive=[],
        scroll={},
        dpr=1.0,
    )
    payload_loaded, _ = classify_and_redact(obs_loaded, instruction="Open Google")
    hist_step_0 = [
        {"action": {"type": "navigate", "params": {"url": "https://www.google.com"}}, "result_ok": True}
    ]
    conclude_plan = plan_from_observation(
        instruction="Open Google",
        payload=payload_loaded,
        provider=_FailingProvider(),
        history=hist_step_0,
    )
    assert called[0] == 0
    assert conclude_plan.steps == []
    assert "Successfully opened Google" in (conclude_plan.answer or "")


@pytest.mark.asyncio
async def test_never_repeat_completed_browser_actions_after_fallback():
    """If fallback provider re-emits an action that already succeeded in history, it is not repeated."""
    hub = Hub()
    obs_dict = {
        "tabId": 1,
        "url": "https://www.google.com/",
        "title": "Google",
        "interactive": [],
        "scroll": {},
    }

    # History already shows navigate to google.com succeeded
    prior_history = [
        {"action": {"type": "navigate", "params": {"url": "https://www.google.com"}}, "result_ok": True}
    ]

    actions_executed = []
    async def _send_action(task_id, act, **kwargs):
        actions_executed.append(act)
        return {"ok": True, "observation": obs_dict}

    hub.send_action = _send_action

    # Planner re-emits navigate to google.com (which already succeeded), followed by typing search
    def _planner(instruction, payload, history, working_memory=None):
        return {
            "goal": "search query",
            "steps": [
                {"why": "re-navigating", "action": {"type": "navigate", "risk": "write", "params": {"url": "https://www.google.com"}}},
                {"why": "typing search", "action": {"type": "type", "risk": "write", "target": {"name": "q"}, "params": {"text": "privagent", "submit": True}}},
            ],
            "answer": None,
        }

    res = await agentLoop.run_with_injection(
        hub,
        planner=_planner,
        obs_factory=lambda: obs_dict,
        task_id="task-no-repeat",
        instruction="search privagent on google",
        initial_memory={"prior_checkpoint_history": prior_history},
    )

    # First executed action must be 'type' (the redundant 'navigate' was skipped)!
    assert len(actions_executed) > 0
    assert actions_executed[0]["type"] == "type"
    assert actions_executed[0].get("params", {}).get("text") == "privagent"



