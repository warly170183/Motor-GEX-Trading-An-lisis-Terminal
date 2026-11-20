"""Run this on YOUR machine (Binance blocks some countries and cloud IPs): checks the Binance venue against the live API.

    python tools/check_binance.py          # BTC, ETH, SOL, SUI, TRX (the coins configured in config.CRYPTO_COINS)
Prints, per coin, the contract count, the index, a sample contract and the units, so you can eyeball that open interest
is in coins and IV is a fraction (0.55, not 55). If anything looks off, tell me the output and I fix the parser."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gexlab.sources import binance  # noqa: E402
from gexlab.sources.base import NoOptions, SourceError  # noqa: E402

for t in ("BTC", "ETH", "SOL", "SUI", "TRX"):
    try:
        p = binance.fetch_part(t, datetime.now(timezone.utc))
        c = max(p.contracts, key=lambda x: x.oi or 0)
        print(f"{t}: {len(p.contracts)} contracts, index {p.spot.price}; biggest OI {c.symbol} oi={c.oi} iv={c.iv}")
        for w in p.warnings:
            print("   warning:", w)
    except NoOptions as e:
        print(f"{t}: {e}")
    except SourceError as e:
        print(f"{t}: ERROR {e}")
