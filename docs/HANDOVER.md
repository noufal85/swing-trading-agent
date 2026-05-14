# Handover — swing-trading-agent (local + AWS dual mode)

Fork of [kevmyung/swing-trading-agent](https://github.com/kevmyung/swing-trading-agent). The original project is AWS-only (Bedrock for LLM, DynamoDB+S3 for storage, AgentCore for runtime). This fork adds a **provider toggle** so the agent can run locally without AWS — only the LLM call needs swapping; everything else was already local-first.

For a visual walkthrough of the architecture, open [`how-it-works.excalidraw`](how-it-works.excalidraw) (or [`how-it-works.png`](how-it-works.png)) in this folder.

---

## What changed in this fork

| File | Change |
|---|---|
| `config/settings.py` | New: `MODEL_PROVIDER` (`bedrock` \| `anthropic`), `ANTHROPIC_API_KEY`, `ANTHROPIC_MAX_TOKENS`. Default is `bedrock` — existing AWS users see no behavior change. |
| `agents/base_agent.py` | `_get_boto_model()` now branches on provider. Helpers `_build_bedrock_model()` / `_build_anthropic_model()`. Module-level `_bedrock_to_anthropic_model_id()` maps Bedrock inference-profile IDs (`us.anthropic.claude-sonnet-4-5-20251001-v1:0`) to Anthropic API IDs (`claude-sonnet-4-5-20251001`). |
| `agents/research_analyst_agent.py` | Same branch on `MODEL_PROVIDER`. Research path keeps its Bedrock-specific config (no cache, no thinking) when running on Bedrock; uses the base helper when running on Anthropic. |
| `requirements.txt` | Added `anthropic>=0.21.0` (Strands' `AnthropicModel` imports it at runtime). |
| `.env.example` | New model-provider section at the top. Bedrock-only vars now marked "required only when `MODEL_PROVIDER=bedrock`". |
| `docs/how-it-works.excalidraw` + `.png` | Architecture overview diagram (three cycles, internal convergence, execution + persistence). |
| `docs/HANDOVER.md` | This file. |

---

## Two ways to run

### Local mode (no AWS)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# In .env set:
#   MODEL_PROVIDER=anthropic
#   ANTHROPIC_API_KEY=sk-ant-...          (from console.anthropic.com)
#   ALPACA_API_KEY=...  ALPACA_SECRET_KEY=...
#   ALPACA_BASE_URL=https://paper-api.alpaca.markets

python main.py --cycle EOD_SIGNAL     # one-shot
python main.py                         # scheduler (paper trading)
```

Dashboard:
```bash
cd frontend && npm install && npm run dev
# opens Vite at :5173, FastAPI at :8000
```

**Skip `./deploy.sh` entirely.** It only provisions the cloud-hosted AgentCore runtime; `python main.py` runs the same code locally.

What's already local-first (no changes needed):
- **Store**: `STORE_MODE=local` default → JSON files under `backtest/sessions/`
- **Agent session memory**: `AGENT_SESSION_STORAGE=file` default → local files
- **Broker**: Alpaca paper API (just needs your keys)
- **Market data**: yfinance (no key) or Polygon (optional, for news-informed backtests)

### Cloud mode (your existing AWS path)

Flip one line in `.env`:
```
MODEL_PROVIDER=bedrock
```
…and the original cloud flow works as documented in the upstream README — `./deploy.sh`, AgentCore, DynamoDB, S3 all intact.

---

## Model ID mapping

The `BEDROCK_MODEL_ID` setting is the source of truth for which Claude is used. When `MODEL_PROVIDER=anthropic`, it's auto-mapped to the Anthropic API ID:

| BEDROCK_MODEL_ID | Used directly when `bedrock` | Mapped to (Anthropic) |
|---|---|---|
| `us.anthropic.claude-sonnet-4-5-20251001-v1:0` | (as-is) | `claude-sonnet-4-5-20251001` |
| `us.anthropic.claude-haiku-4-5-20251001-v1:0`  | (as-is) | `claude-haiku-4-5-20251001` |
| `eu.anthropic.claude-opus-4-1-20250805-v1:0`   | (as-is) | `claude-opus-4-1-20250805` |

The research agent (`ResearchAnalystAgent`) is hardcoded to Haiku 4.5 (`_RESEARCH_MODEL_ID` in `agents/research_analyst_agent.py:66`) regardless of the PM model.

---

## Verified

- Mapping helper covers `us.` / `eu.` / `apac.` / `global.` prefixes, with or without the `anthropic.` middle, with or without `-v1:0` suffix.
- Settings parse: default = bedrock, env override → anthropic works, invalid values rejected by `Literal["bedrock", "anthropic"]`.
- End-to-end LLM call against the Anthropic API was **not** executed (needs your API key). The branching is ~3 lines of `if` and the `AnthropicModel` signature was confirmed against `strands_agents-1.39.0`.

---

## Open items / things I noticed during review

These came out of the initial codebase analysis — not introduced by this fork, but worth tracking:

1. **No pinned dependencies / lockfile.** `requirements.txt` uses `>=` bounds only. For a system that places real-money trades, pin (`pip-compile` → `requirements.lock`) before any live run.
2. **`PersistingOrchestrator` lives in `main.py:107`.** It's a persistence decorator hard-coded in the entry point. Could move to `store/` for testability.
3. **`_record_daily_stats` (`main.py:120`) does a synchronous yfinance call** for SPY benchmark on every cycle. Cache or move off the hot path.
4. **`--paper` flag is a warning-only override** (`main.py:294`). If `ALPACA_PAPER=false` in env, `--paper` warns but doesn't actually flip the setting. Subtle live-trading footgun.
5. **Backtest news vs. live news divergence.** Backtests use Polygon, live trading uses yfinance — different data sources is a classic backtest/live skew. Worth normalizing.
6. **Two `main.py` files** (root + `cloud/main.py`). Easy to drift. The cloud one wraps the same orchestrator but it'd be safer if both went through a shared bootstrap.
7. **`LocalStore` JSON writes** have no file locking — concurrent writes from a cycle + the dashboard API could race.

---

## Useful entry points

| Want to… | Read |
|---|---|
| Understand the daily flow | `docs/how-it-works.excalidraw` |
| See how cycles are orchestrated | `agents/portfolio_agent.py` + `agents/_eod_cycle.py` / `_morning_cycle.py` / `_intraday_cycle.py` |
| Tweak entry/exit rules | `playbook/entry/*.md`, `playbook/position/*.md`, `playbook/intraday/*.md` |
| Tune quant signals | `agents/quant_engine.py` + `config/settings.py` (`quant_*` fields) |
| Add/swap a broker | `providers/broker.py` (interface), `providers/live_broker.py` (Alpaca impl), `providers/mock_broker.py` (backtest impl) |
| Swap storage backend | `store/factory.py` (toggles via `STORE_MODE` env) |
| Change LLM provider | `agents/base_agent.py` — `_build_bedrock_model` / `_build_anthropic_model` |

---

## Syncing with upstream

```bash
git remote -v
# origin    = noufal85/swing-trading-agent (this fork)
# upstream  = kevmyung/swing-trading-agent (original)

git fetch upstream
git merge upstream/main      # or: git rebase upstream/main
```
