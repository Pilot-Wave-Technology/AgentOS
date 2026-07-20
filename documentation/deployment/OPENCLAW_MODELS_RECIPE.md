# Adding LLM models to the openclaw gateway

When adding a new API-native model (Anthropic, Gemini, OpenAI, Bedrock,
…) so the control panel can offer it through the GO/Assistant picker, two
layers need updating.

---

## 1. Control panel — `multi_agent_control_panel/app/llm_models.py`

Append a `ModelEntry` to `SUPPORTED_MODELS`. The fork accepts the model
id verbatim — no aliasing on the control-panel side. Format:

```python
{"id": "anthropic/claude-<name>",
 "label": "Claude <Name>",
 "provider": "anthropic",
 "tier": "fast|balanced|premium"},
```

Then commit + deploy. The model immediately appears in:

- `/goals` new-goal modal → "Goal Orchestrator model" dropdown
- `/settings/my-agents` configure modal → "Model" dropdown
- `/goals/<pid>/settings` → "Goal Orchestrator Model" card
- `/settings/my-agents` per-Assistant "Change Model" form

`is_supported(...)` validates user-submitted form values; unknown
models get rejected with a 400 before they reach the gateway.

---

## 2. Openclaw VM — allowlist in `~/.openclaw/openclaw.json`

The fork enforces an **allowed models list** at
`agents.defaults.models`. If a model isn't listed there, calls fall
back silently to the default with this gateway log line:

```
[model-fallback/decision] candidate_failed
  requested=anthropic/<model> reason=unknown
  next=google/gemini-2.5-flash
[model-fallback/decision] candidate_succeeded
  requested=anthropic/<model> candidate=google/gemini-2.5-flash
```

The `reason=unknown` is misleading — it actually means "not in allowed
list". The user gets a Gemini reply when they asked for Claude.

### Why `node openclaw.mjs config set` doesn't work

- Schema namespace is `agents.defaults`, NOT `defaults`. Don't trust
  the shape `config get` shows you — the get path strips `agents.` from
  its output but the setter path requires the prefix.
- The dotted-path parser doesn't handle `/` inside leaf keys, so
  `config set agents.defaults.models.anthropic/claude-X '{}'`
  errors with a validation failure.

### Working method: edit JSON directly with `jq`

```bash
cd ~/openclaw_fork_03122026

cp ~/.openclaw/openclaw.json ~/.openclaw/openclaw.json.bak

jq '.agents.defaults.models["anthropic/claude-<new-model>"] = {}' \
  ~/.openclaw/openclaw.json > /tmp/openclaw.json.new \
  && mv /tmp/openclaw.json.new ~/.openclaw/openclaw.json

# Verify it landed
jq '.agents.defaults.models | keys' ~/.openclaw/openclaw.json
node openclaw.mjs models list

# Restart gateway so the change takes effect
pkill -f "openclaw.mjs gateway" ; ~/start-openclaw.sh
```

Multiple models in one shot:

```bash
jq '.agents.defaults.models["anthropic/claude-haiku-4-5"]  = {}
  | .agents.defaults.models["anthropic/claude-sonnet-4-5"] = {}
  | .agents.defaults.models["anthropic/claude-opus-4-7"]   = {}
  | .agents.defaults.models["google/gemini-2.5-pro"]       = {}' \
  ~/.openclaw/openclaw.json > /tmp/openclaw.json.new \
  && mv /tmp/openclaw.json.new ~/.openclaw/openclaw.json
```

If `jq` isn't installed: `sudo apt-get install -y jq`.

---

## CLI providers don't need allowlisting

`claude-cli/*` and `codex-cli/*` shell out to a local binary and
**bypass the allowed list entirely**. So IAs running on
`claude-cli/sonnet` work without ever appearing in the allowlist.
Only API-native providers (`anthropic/*`, `google/*`, `openai/*`,
`amazon-bedrock/*`, etc.) need the allowlist entry.

This is also why the GO/Assistant model picker doesn't expose `claude-cli/*`
options — those are reserved for IAs. See `app/llm_models.py` for the
rationale.

---

## Make it permanent: bake into `start-openclaw.sh`

Add the `jq` block to the start script right after `pnpm build`, before
`node openclaw.mjs gateway`. `jq` writes are idempotent — re-running
on each gateway start is safe.

```bash
# in start-openclaw.sh after pnpm build:
jq '.agents.defaults.models["anthropic/claude-haiku-4-5"]  = {}
  | .agents.defaults.models["anthropic/claude-sonnet-4-5"] = {}
  | .agents.defaults.models["anthropic/claude-opus-4-7"]   = {}
  | .agents.defaults.models["google/gemini-2.5-pro"]       = {}' \
  ~/.openclaw/openclaw.json > /tmp/openclaw.json.new \
  && mv /tmp/openclaw.json.new ~/.openclaw/openclaw.json
```

---

## Where the silent-fallback gotcha came from

Found while testing the GO model picker with
`anthropic/claude-opus-4-7`. Direct `curl` to Anthropic returned `ok`,
proving the model id and key were valid, but every chat through the
goal channel produced Gemini-flavoured replies. Diagnosis via
`node openclaw.mjs models status --json` showed
`"allowed": ["google/gemini-2.5-flash"]` — a single-entry allowlist.
Adding the Anthropic ids via `jq` resolved it.

The fix surfaces in the gateway logs as:

```
[model-fallback/decision] candidate_succeeded
  requested=anthropic/claude-opus-4-7
  candidate=anthropic/claude-opus-4-7
```

— same `requested` and `candidate`, no fallback hop.
