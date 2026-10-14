# GOLD RIPPER v2.1.0 "merged edition" — full package

## What's inside
- `gold-ripper/`       — YOUR bot (v2.1.0), hardened with the best patterns
                         mined from the 9 repos below. Run: `python ripper.py`
                         (paper by default; see gold-ripper/README.md).
- `audits/`            — the 5 deep-audit reports of the 9 repos (what was
                         real, what was junk, what was stolen and why).
- `source-repos/`      — the original 9 GitHub repos as downloaded ZIPs:
                         bitget-trading-bot, aster-bot, astra-quant-agent,
                         Astra-algorithmic-trader, Astra,
                         Expert-Advisor-trading-bot, astrbot, freqtrade,
                         passivbot.
- `WORKLOG.md`         — full engineering history (16+ rounds of work).

## Quick start (Linux)
    cd gold-ripper
    pip install -r requirements.txt
    cp .env.example .env      # add Bitget keys ONLY for live mode
    python ripper.py          # PAPER mode - safe, 24/7 hunting

## Verify the merge on your machine
    python scripts/test_guards.py     # 41 tests: guards/protections/tape
    python scripts/bench_fasttape.py  # ms-class proof (~4.5us/bar)
    python scripts/test_lookahead.py  # HTF resample leak check
    python backtest.py --help         # or retrain: python train.py

## Audit verdicts (details in audits/)
- freqtrade / passivbot / bitget-trading-bot / astra-quant-agent: real,
  mined for protections, wallet-exposure math, Bitget plan orders, EV gates.
- aster-bot, Expert-Advisor-trading-bot, Astra, Astra-algorithmic-trader,
  astrbot: junk or not trading bots at all - audited, NOT copied.
