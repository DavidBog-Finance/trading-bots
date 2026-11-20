# Notes for Claude Code

This repo holds three daily trading bots for Alpaca (stocks, crypto, SPY put credit spreads) and two Claude
trading agents (stock, crypto) run by an always-on Mac runner (`daemon.py` under launchd), paper first.
The owner works on a MacBook Pro and monitors from an iPhone (ntfy alerts, GitHub app, Alpaca app).

- `strategies.py` is the single source of truth for signals. The live bots and `backtest.py` both
  import it. Never put signal logic in the bot files.
- Any strategy change must be backtested with `python backtest.py <stocks|crypto> --download --split <date>`
  and judged on the OUT-OF-SAMPLE half only. Report costs, max drawdown and trades/yr, not just return.
- Options are limited to defined-risk spreads. No naked short options, no leverage beyond what a spread needs,
  no intraday trading unless the owner asks and it passes an out-of-sample test after fees.
- New symbols go in only after `python validate.py` passes them.
- Keep the safety rails in `broker.py` and `config.py` (paper default, I_UNDERSTAND_LIVE_RISK, caps,
  deposit-aware MAX_LOSS_PCT, kill switch, stale-data checks).
- Run `python -m pytest -q tests` before every commit. Never commit `.env`.
- Agents: Claude only acts through the tools in `agent.py`; every order passes `RiskGate`. Never give the
  agent a tool that bypasses the gate, places options/short/leveraged trades, or edits config.
- Claude API calls go through `claude_client.py` (budget, cost log, fallbacks, refusal handling).
- Agents and rules bots must never trade the same symbols (`market.core_symbols()`).
- Local AI (`local_llm.py`) only screens and scores news. It must never place orders or wake an agent
  with a buy idea on its own; buy-idea wakes need a Claude score. Claude calls are paced by `claude_client.allow()`.
- Never commit `linux/services.env` or `data/`.
- Phases: cloud (`agents.yml` runs `daemon.py --once`) -> home server + cloud -> server only. Keep every job
  runnable both as one `--once` pass (state in `logs/state.db`) and in the long-running loop.
- Portfolio 1k (`portfolio1k/`) is the active project: options only, own DB (logs/portfolio1k.db), own limits.
  All rules are pure functions in `portfolio1k/rules.py` with tests in tests/test_portfolio1k.py; keep it that way.
  It must run both as `python -m portfolio1k run --max-minutes N` (GitHub) and `run` forever (always-on computer).
  Claude only grades trades and writes the game plan; it never places orders or changes limits.
