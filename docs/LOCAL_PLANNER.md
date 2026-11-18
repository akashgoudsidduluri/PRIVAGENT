# Local planner (Ollama) — Task 10

PrivAgent can plan with a model that runs on your own machine, so no
page data ever leaves it. This is the "BYOM" (bring your own model)
path: you install Ollama and choose the model; PrivAgent never
downloads a model itself.

## Setup

```bash
# 1. Install Ollama (https://ollama.com) and start it:
ollama serve

# 2. Pull a model. The default is qwen2.5:7b — a small, capable
#    instruction model that runs on a laptop CPU. Any Ollama model
#    that can return JSON works; larger models plan better and slower.
ollama pull qwen2.5:7b

# 3. Point PrivAgent at it (backend/.env):
PRIVAGENT_PLANNER=ollama
PRIVAGENT_OLLAMA_MODEL=qwen2.5:7b
PRIVAGENT_OLLAMA_BASE_URL=http://127.0.0.1:11434
```

Or switch at runtime without restarting, via
`POST /api/llm/config {"provider": "ollama", "model": "qwen2.5:7b"}`.

## Modes

| Setting | Behaviour |
|---|---|
| `PRIVAGENT_PLANNER=ollama` | Ollama is the planner. If Ollama is unreachable, the loop falls back to the normal chain (deepai → groq → gemini → mock) and logs the fallback — it does not fail silently. |
| `PRIVAGENT_PLANNER_LOCAL_FIRST=true` | Same as above, but without changing the configured provider: Ollama is tried first, then the normal chain. |
| `PRIVAGENT_PLANNER_LOCAL_ONLY=true` | The chain is **ollama → fail**. When Ollama is unreachable the task fails honestly with "Ollama … is unreachable". It **never** silently falls back to a cloud provider — that would send page data out after you asked for local-only. A non-local planner selection (runtime or env) in local-only mode is a configuration error, raised at selection time. |

The default chain is unchanged for existing users: Ollama joins it
only when explicitly selected or when a local-first/local-only flag
is set.

## Safety properties

- **Loopback only.** The Ollama base URL must be `127.0.0.1`,
  `localhost`, or `::1`. A non-loopback URL is refused at
  construction, so a misconfigured env can never send page data to a
  remote host.
- **Egress gate.** The Ollama endpoint is checked against the Task 3
  egress allow-list before any request; loopback already passes it.
- **No API key.** Ollama needs no key; none is sent, stored, or
  logged. The runtime provider panel reports Ollama as configured
  when a loopback base URL and model are set.
- **Same plan contract.** `OllamaProvider` implements the same
  `plan_call` / `chat_call` interface as the Groq/Gemini/mock
  providers, builds the prompt with the same `build_prompt` (same
  redaction, same provenance tags, same allow-list), and parses the
  model's JSON with the same `extract_json_from_text`. A non-JSON
  reply is rejected as `PLAN_INVALID`, never executed.
- **Timeouts.** A 20 s HTTP timeout per call (same default as Groq);
  a connect failure or timeout is `ProviderUnavailable`, which the
  loop treats as a planner outage (step preserved, honest failure),
  not as a successful empty plan.

## Honest limits

- **No live Ollama run exists in this repo's test suite.** The
  provider is tested against a mocked HTTP endpoint (see
  `backend/tests/test_ollama_provider.py`); no real Ollama server was
  available in the build VM. Expect a local model to plan worse than
  a frontier cloud model: small models are more likely to emit
  malformed plans (rejected by validation, retried) or to pick the
  wrong element. The planner-validation and confirmation gates still
  apply — a bad plan is rejected, not executed.
- **Ollama itself keeps logs.** Ollama's own server log and the
  model's context live on your machine; "no data leaves the machine"
  means no network egress, not no local state. Combine with
  `PRIVAGENT_ZERO_RETENTION=true` (see `docs/PRIVACY.md`) if you
  want PrivAgent to persist nothing either.
- **Model quality is the operator's choice.** PrivAgent does not
  verify that the pulled model is any good at browser planning.
  `qwen2.5:7b` is a starting point, not a recommendation backed by a
  benchmark — no local-model benchmark has been run (see
  `docs/EVALS.md`).
