# Aria — Appetite & Risk Intelligence Agent

Aria is a multi-signal appetite scoring agent for small commercial insurance underwriting. It applies deterministic YAML-driven rules to evaluate submissions across class, geography, and TIV dimensions, then uses an LLM to generate a narrative rationale for each decision. A human-in-the-loop (HITL) layer flags edge cases for underwriter review before any decision is finalised.

## Activate the virtual environment

```bash
cd ~/projects/aria_agent
source .venv/bin/activate
```

## Configure environment variables

```bash
cp .env.example .env
# Edit .env and add your API key(s)
```

Only the key for the active provider needs to be set (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`).

## Switch LLM provider

Open `config/llm_config.yaml` and change the `provider` line:

```yaml
provider: anthropic   # was: openai
```

## Run the server

```bash
uvicorn aria.server:app --port 8001 --reload
```
