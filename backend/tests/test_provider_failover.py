"""Rate-limit handling: fail fast instead of sleeping through quota windows.

A live free-tier Groq key hit its 200k daily token cap. The old provider slept
8s on every 429 and then tried the other Groq models (which share the same
account-wide cap) before falling back to Gemini — roughly 40s of latency per
planning step. These tests lock in the fast path.
"""
import pytest

from app.providers import gemini as gemini_mod
from app.providers import groq as groq_mod
from app.providers.errors import ProviderUnavailable
from app.providers.gemini import GeminiProvider, extract_json_from_text
from app.providers.groq import GroqProvider, _parse_duration, _quota_scope
from app.planner import _validate_plan


@pytest.fixture(autouse=True)
def _reset_provider_blocks():
    """Quota blocks are process-wide by design; tests must not leak them."""
    yield
    groq_mod._GROQ_BLOCKED_UNTIL = 0.0
    gemini_mod._MODEL_BLOCKED_UNTIL.clear()


def _payload():
    from app.contracts import BrowserObservation
    from app.privacy import classify_and_redact

    obs = BrowserObservation(
        tab_id=1, url="https://example.com", title="Example",
        interactive=[], scroll={}, dpr=1.0,
    )
    payload, _ = classify_and_redact(obs, instruction="test")
    return payload


class _Response:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self):
        return {}


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return self.response


def test_parse_duration_handles_minutes():
    assert _parse_duration("try again in 23m13.2s") == pytest.approx(1393.2)
    assert _parse_duration("try again in 899ms") == pytest.approx(0.899)
    assert _parse_duration("no hint here") == 0.0


def test_quota_scope_distinguishes_day_and_minute():
    assert _quota_scope("on tokens per day (TPD): Limit 200000") == "day"
    assert _quota_scope("on tokens per minute (TPM): Limit 8000") == "minute"
    assert _quota_scope("something else") == ""


def test_daily_quota_skips_remaining_groq_models(monkeypatch):
    """A TPD cap is account-wide: trying 20b/qwen only adds latency."""
    client = _Client(_Response(429, (
        '{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` '
        'on tokens per day (TPD): Limit 200000, Used 198758, Requested 4467. '
        'Please try again in 23m13.2s."}}'
    )))
    provider = GroqProvider(api_key="dummy")
    monkeypatch.setattr(provider, "_client", client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "")
    sleeps: list[float] = []
    monkeypatch.setattr(groq_mod.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.plan_call("test", _payload(), [])

    assert client.calls == 1, "must not cascade through sharing-the-same-cap models"
    assert sleeps == [], "must not sleep through a multi-minute window"
    assert "23m" in str(excinfo.value)


def test_minute_quota_pauses_at_most_two_seconds(monkeypatch):
    client = _Client(_Response(429, (
        '{"error":{"message":"on tokens per minute (TPM): Limit 8000. '
        'Please try again in 9.5s."}}'
    )))
    provider = GroqProvider(api_key="dummy")
    monkeypatch.setattr(provider, "_client", client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "")
    sleeps: list[float] = []
    monkeypatch.setattr(groq_mod.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(ProviderUnavailable):
        provider.plan_call("test", _payload(), [])

    assert client.calls == 3  # one attempt per model, none repeated
    assert sleeps and max(sleeps) <= 2.0


def test_gemini_long_quota_does_not_sleep(monkeypatch):
    class _Models:
        def __init__(self) -> None:
            self.calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED retry in 42s")

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _Models()

    provider = GeminiProvider(api_key="dummy")
    fake = _GeminiClient()
    monkeypatch.setattr(provider, "_client", fake)
    sleeps: list[float] = []
    monkeypatch.setattr("app.providers.gemini.time.sleep", lambda s: sleeps.append(s))

    with pytest.raises(ProviderUnavailable):
        provider.plan_call("test", _payload(), [])

    assert fake.models.calls == 3
    assert sleeps == []


def test_extract_json_from_text_variants():
    # Pure JSON
    assert extract_json_from_text('{"goal": "find shoes", "steps": []}') == {
        "goal": "find shoes",
        "steps": [],
    }

    # Markdown fenced json with preamble and postamble
    fenced = (
        "Here is the plan you requested:\n"
        "```json\n"
        '{\n  "goal": "fenced json",\n  "steps": []\n}\n'
        "```\n"
        "Hope that helps!"
    )
    assert extract_json_from_text(fenced)["goal"] == "fenced json"

    # Markdown fence without language specifier
    fenced_no_lang = (
        "```\n"
        '{\n  "goal": "bare fence",\n  "steps": []\n}\n'
        "```"
    )
    assert extract_json_from_text(fenced_no_lang)["goal"] == "bare fence"

    # Embedded in conversational prose without code fences
    embedded = 'I have devised the following plan: {"goal": "inline", "steps": []} to complete the task.'
    assert extract_json_from_text(embedded)["goal"] == "inline"

    # Truly invalid text
    with pytest.raises(ValueError, match="PLAN_INVALID"):
        extract_json_from_text("Sorry, I am an AI and cannot generate plans.")

    # Non-dict JSON
    with pytest.raises(ValueError, match="PLAN_INVALID"):
        extract_json_from_text('["not", "a", "dict"]')


def test_groq_quota_exhaustion_falls_back_to_gemini_valid_markdown(monkeypatch):
    """Groq daily 429 fails over to Gemini, which returns valid JSON wrapped in Markdown fences."""
    groq_client = _Client(_Response(429, (
        '{"error":{"message":"Rate limit reached on tokens per day (TPD). Please try again in 15m."}}'
    )))
    groq_provider = GroqProvider(api_key="groq-dummy")
    monkeypatch.setattr(groq_provider, "_client", groq_client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "gemini-dummy")

    class _GeminiContentResp:
        def __init__(self, text: str) -> None:
            self.text = text

    class _GeminiModels:
        def __init__(self) -> None:
            self.calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            return _GeminiContentResp(
                "Sure! Here is the plan:\n```json\n"
                '{"goal": "search product", "steps": []}\n'
                "```\nGood luck!"
            )

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _GeminiModels()

    fake_gemini = _GeminiClient()
    monkeypatch.setattr("google.genai.Client", lambda api_key: fake_gemini)

    raw_plan = groq_provider.plan_call("search product", _payload(), [])
    assert raw_plan["goal"] == "search product"
    assert groq_provider.last_provider.startswith("gemini:")

    # Validate that _validate_plan accepts this fallback plan
    plan = _validate_plan(raw_plan, payload=_payload(), instruction="search product")
    assert plan.goal == "search product"


def test_groq_quota_exhaustion_gemini_returns_malformed_json_raises_value_error(monkeypatch):
    """When Gemini fallback returns non-JSON, it must raise ValueError (PLAN_INVALID), NOT ProviderUnavailable."""
    groq_client = _Client(_Response(429, (
        '{"error":{"message":"Rate limit reached on tokens per day (TPD). Please try again in 15m."}}'
    )))
    groq_provider = GroqProvider(api_key="groq-dummy")
    monkeypatch.setattr(groq_provider, "_client", groq_client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "gemini-dummy")

    class _GeminiContentResp:
        def __init__(self, text: str) -> None:
            self.text = text

    class _GeminiModels:
        def generate_content(self, **kwargs):
            return _GeminiContentResp("I apologize, but I cannot generate a valid plan for this page.")

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _GeminiModels()

    monkeypatch.setattr("google.genai.Client", lambda api_key: _GeminiClient())

    with pytest.raises(ValueError) as excinfo:
        groq_provider.plan_call("search product", _payload(), [])

    assert "PLAN_INVALID" in str(excinfo.value)
    assert not issubclass(excinfo.type, ProviderUnavailable)


def test_complete_provider_exhaustion_raises_provider_unavailable(monkeypatch):
    """When both Groq and Gemini fail (quota/outage), raise ProviderUnavailable stating both failed."""
    groq_client = _Client(_Response(429, (
        '{"error":{"message":"Rate limit reached on tokens per day (TPD). Please try again in 25m."}}'
    )))
    groq_provider = GroqProvider(api_key="groq-dummy")
    monkeypatch.setattr(groq_provider, "_client", groq_client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "gemini-dummy")

    class _GeminiModels:
        def generate_content(self, **kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded retry in 10m")

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _GeminiModels()

    monkeypatch.setattr("google.genai.Client", lambda api_key: _GeminiClient())

    with pytest.raises(ProviderUnavailable) as excinfo:
        groq_provider.plan_call("test", _payload(), [])

    err_msg = str(excinfo.value)
    assert "Groq quota exhausted" in err_msg
    assert "Gemini fallback also failed" in err_msg


def test_gemini_continues_to_next_fallback_model_on_malformed_json(monkeypatch):
    """If Gemini's primary model returns malformed JSON, it tries the next fallback model."""
    class _GeminiContentResp:
        def __init__(self, text: str) -> None:
            self.text = text

    models_called = []

    class _GeminiModels:
        def generate_content(self, model: str, **kwargs):
            models_called.append(model)
            if len(models_called) == 1:
                return _GeminiContentResp("Here is why I cannot output JSON...")
            return _GeminiContentResp('{"goal": "succeeded on fallback model", "steps": []}')

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _GeminiModels()

    provider = GeminiProvider(api_key="dummy")
    fake = _GeminiClient()
    monkeypatch.setattr(provider, "_client", fake)

    plan = provider.plan_call("test", _payload(), [])
    assert plan["goal"] == "succeeded on fallback model"
    assert len(models_called) == 2


def test_gemini_all_models_malformed_json_raises_value_error(monkeypatch):
    """If all Gemini models return malformed JSON, raise ValueError (PLAN_INVALID)."""
    class _GeminiContentResp:
        def __init__(self, text: str) -> None:
            self.text = text

    class _GeminiModels:
        def __init__(self) -> None:
            self.calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            return _GeminiContentResp("Sorry, no JSON here.")

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _GeminiModels()

    provider = GeminiProvider(api_key="dummy")
    fake = _GeminiClient()
    monkeypatch.setattr(provider, "_client", fake)

    with pytest.raises(ValueError) as excinfo:
        provider.plan_call("test", _payload(), [])

    assert "PLAN_INVALID" in str(excinfo.value)
    assert fake.models.calls == 3
