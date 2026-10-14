# Audit: astrbotdevs/astrbot (AstrBot-master)

**Date:** 2026-02-28 · **Agent:** Explore (Task 2-e) · **Verdict: NOT a trading bot — no merge value for gold-ripper.**

## What it is
- **AstrBot v4.29.0-beta.2** — "Easy-to-use multi-platform LLM chatbot and development framework", AGPL-3.0-or-later, Python 3.12+.
- An all-in-one **Agent chatbot platform**: LLM conversations (OpenAI/Anthropic/Google/dashscope), MCP, knowledge base (faiss), personas, agent sandbox (aiodocker), WebUI dashboard (separate `dashboard/` frontend).
- Multi-platform IM adapters: QQ (aiocqhttp/qq-botpy), Telegram (python-telegram-bot), Slack, DingTalk, Lark/Feishu, Discord (py-cord), WeChat.

## Verification (hypothesis check)
- README (EN/zh/ja/…): "open-source all-in-one Agent chatbot platform… integrates with mainstream instant messaging apps."
- `pyproject.toml` deps are all chat/LLM/IM: anthropic, openai, faiss, mcp, python-telegram-bot, slack-sdk, quart/fastapi… **zero trading deps**.
- `rg` over the whole `astrbot/` package for `ccxt|exchange|binance|bitget|bybit|okx|trading` → **0 hits**. There is no market data, no order handling, no exchange integration, no risk logic anywhere.

## Anything tangentially useful? (barely)
- **Plugin ("Star") architecture**: decorator-registered, hot-loadable plugins + 1000+ plugin marketplace — a decent pattern reference if gold-ripper ever wants a module/plugin registry, but gold-ripper's M1–M4 module system is simpler and sufficient.
- **APScheduler** for cron-like scheduled jobs — nothing gold-ripper doesn't already do.
- Mature **Telegram adapter** (python-telegram-bot) — gold-ripper already ships its own Telegram alerting; the round-10 roadmap even plans a Telegram poller, and a tiny dedicated poller beats adopting this framework's.
- **AGPL-3.0 license** — copying any of this into gold-ripper would impose AGPL obligations on the merged bot. One more reason not to.

## Bottom line
Repo was listed by mistake (name similarity, "Astra"/"astr"). 0% of it is trading-related; nothing is worth lifting into the XAUUSD/Bitget perp bot. **Recommend: skip merge entirely.**
