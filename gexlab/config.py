"""Every knob Edge GEX Lab uses, each with the reason it holds that value.

Change behavior here, not in the modules. Values that come from the frozen
model v1.0 spec are marked SPEC.
"""
from __future__ import annotations

import os
from datetime import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- identity
APP_VERSION = "1.3.2"            # The public release this build is. Bump it before tagging a release: the update check
                                 # (gexlab/update_check.py) compares it with GitHub's latest release tag.
UPDATE_REPO = "simonro/Edge-GEX-Lab"  # Where the update check looks for releases (the public repo).
UPDATE_EVERY_H = 24              # At most one update check a day; the answer is cached in CACHE_DIR.
UPDATE_TIMEOUT_S = 3             # The check gives up after 3 s, silently.
MODEL_VERSION = "1.1"           # Model/input version, stored on every capture. 1.1: same gamma math as 1.0, but the
                                 # open interest inputs changed vendor, so the version moved; the capture gate stays 1.2.
GATE_VERSION = "1.3"             # Capture eligibility gate. 1.3 (2026-10-19 evening, day-1 audit): 1.2 plus
                                 # (a) open interest must be from the previous completed session (OI_STALE_MAX_FRAC),
                                 # (b) a band contract with OI whose |GEX| cannot be estimated (no donor IV) fails the
                                 # capture instead of being dropped from the denominator (no safe upper bound exists for
                                 # a missing near-expiry contract), (c) one quote clock: a quote is "future" only when it
                                 # is stamped after it was received (the fetch: the time it arrived; the gate: the capture's
                                 # completion), and at the close only within CLOSED_QUOTE_AFTER_CLOSE_S of the close.
                                 # History: 1.2 (2026-10-19 11:15 ET).
                                 # 1.0 gated on the CONTRACT count within +-10%; 1.1 (2026-10-19, 10:15 snapshot on)
                                 # on OPEN INTEREST within +-5% (GATE_OI_*); 1.2 on the estimated ABSOLUTE GEX within
                                 # +-5% (GATE_GEX_MIN_PCT). Stored on every new capture (captures.gate_version); rows
                                 # with no value were judged by 1.0, and every row keeps the status its own gate gave
                                 # it. The GEX math is unchanged, so MODEL_VERSION stays 1.0, and the study protocol
                                 # (touches, placebo seeds) is unchanged, so the study protocol version stays 1.0.
PINNED_TICKERS = ["BTC", "ETH", "SOL", "TRX", "SUI", "EGLD"]  # The coins the app serves.

# ---------------------------------------------------------------- modes
def is_demo() -> bool:
    """SPEC: GEXLAB_DEMO=1 serves fixtures, no network. Read live so tests can toggle it."""
    return os.getenv("GEXLAB_DEMO", "0") == "1"


FIXTURES_DIR = ROOT / "fixtures"             # Synthetic inputs for DEMO mode and tests.
DEMO_DB_DEFAULT = ROOT / "data" / "gexlab_demo.db"  # DEMO writes never touch the research DB (GEXLAB_DEMO_DB overrides).
CACHE_DIR = ROOT / "data" / "cache"          # Gitignored (data/); holds cached daily bars (ADV and ATR).
API_PORT = int(os.getenv("GEXLAB_PORT") or 8240)  # API port, 8240 by default. launch.bat reads the same GEXLAB_PORT.

# ---------------------------------------------------------------- feed labels and HTTP
FEED_LABELS = {
    "demo": "Synthetic fixture",
    "demo_crypto": "Recorded Deribit snapshot (demo)",
    "deribit": "Deribit public API, refreshed 60s",
    "okx": "OKX public API, refreshed 60s",
    "deribit+okx": "Deribit + OKX public APIs, refreshed 60s",
}                                # Labels exactly as written.
HTTP_MAX_RETRIES = 4             # 429/5xx backoff attempts before giving up on a request.
HTTP_BACKOFF_BASE_S = 1.5        # Exponential backoff base (1.5, 3, 6, 12 s) unless Retry-After says otherwise.


# ---------------------------------------------------------------- window and refresh
WINDOW_DAYS = 7                  # SPEC: expiries 0 to 7 calendar days.
REFRESH_MIN_S = 60               # SPEC: live map refresh at most every 60 s; chain cache TTL.
STALE_AFTER_S = 180              # SPEC: no successful refresh in 3 min means stale.
FETCH_STRIKE_BAND = 0.20         # Fetch strikes within +-20% so the +-10% zero-gamma sweep sees edge gamma.
DISPLAY_STRIKE_BAND = 0.10       # SPEC: strikes in /api/map limited to spot +-10%.
WALL_STRIKE_BAND = 0.10          # Walls chosen inside the displayed band so a wall is always on the chart.
SWEEP_BAND = 0.10                # SPEC: zero-gamma sweep within +-10% of spot.
PROFILE_POINTS = 201             # SPEC: profile_curve about 200 points (201 includes spot at the center).
SWEEP_STEP = {"SPY": 0.01, "QQQ": 0.01, "SPX": 0.5, "NDX": 1.0}  # SPEC grid steps.
STOCK_STEP_FRAC = 0.01           # SPEC: "0.01 * price rounded" for stocks (literal reading, about 20 grid points).
SWEEP_CHUNK = 256                # Grid points per numpy chunk; bounds memory near 256 x contracts floats.

# ---------------------------------------------------------------- IV and quality
WIDE_SPREAD_FRAC = 0.50          # Mid unusable when (ask - bid) / mid exceeds 50%: the mid is not a price there.
IV_MIN = 0.01                    # An IV below 1% is a bad print, not a market; treated as invalid.
IV_MAX = 5.0                     # Matches the solver ceiling (500%); above it the solve is meaningless.
NEUTRAL_FRAC = 0.02              # Regime "neutral" when |net GEX| is under 2% of gross GEX at spot.
COVERAGE_STRIKE_BAND = 0.10     # Coverage counts OI > 0 contracts within +-10%, the band the map and sweep describe.
OI_MISSING_MAX_FRAC = 0.50       # More than half the chain with no OI record means the OI feed failed: invalid.
QUALITY_MIN_PCT = 95.0           # Gate 1.0 (diagnostic since 1.1): share of CONTRACTS within +-10% with usable OI and IV.
                                 # Day 1 showed why it cannot gate: SPY 10:00 failed at 91.7% while 99.9% of its
                                 # estimated |GEX| was valid; the misses were 0DTE tail puts quoted 0.01 x 0.02
                                 # (a one-tick spread is 67% of mid) with negligible gamma. Still reported.
GATE_OI_BAND = 0.05              # Gate band (1.1 and 1.2): +-5% of spot. The walls, the flip and the reactions the studies
                                 # measure sit here (an ATR is about 1%); beyond it 0-7 DTE gamma is tiny.
GATE_OI_MIN_PCT = 95.0           # Gate 1.1 (diagnostic reference since 1.2): valid contracts held >= 95% of the open
                                 # interest inside GATE_OI_BAND. Still stored on every capture (oi_coverage_pct).
                                 # Day 1 showed why it cannot gate: SPY 10:30 OI 93.38% with |GEX| 99.53%, QQQ 10:45
                                 # OI 94.40% with |GEX| 98.25%. The OI misses were 0DTE tail puts quoted 0.01 x 0.02:
                                 # real open interest, almost no gamma.
GATE_OI_UNKNOWN_WEIGHT = "median"  # A contract inside the band with NO OI record is invalid, and its unknown OI is
                                 # weighted as the median OI of the band's known contracts (it cannot be ignored,
                                 # and treating it as zero would hide a broken OI feed).
GATE_GEX_MIN_PCT = 98.0          # Gate 1.2: valid contracts must hold >= 98% of the estimated ABSOLUTE GEX inside
                                 # GATE_OI_BAND, on the unrounded ratio. |GEX| weights each miss by the gamma
                                 # exposure it hides, which is what the walls and the flip are built from. An invalid
                                 # contract's gamma uses the IV of the nearest valid strike of the same expiry and
                                 # type, else of the same expiry, else it is left out and counted. Day 1 (2026-10-19)
                                 # measured 98.25% to 99.97% on every capture, those failing gate 1.1 included, so 98
                                 # passes healthy chains and still fails one missing a real slice of gamma. No
                                 # computable share (null) = invalid.
OI_STALE_MAX_FRAC = 0.01         # Gate 1.3: at most 1% of the open interest inside GATE_OI_BAND may sit on contracts whose
                                 # OI is not dated the previous completed session (older, undated or mixed sources). Day 1
                                 # showed why: QQQ's 10:00 capture used OI dated Oct 15 on Oct 19 (two sessions old)
                                 # and still passed. A vendor that does not publish an OI date has it INFERRED from the
                                 # payload time (research policy, declared here).
SPOT_MAX_AGE_S = 60              # SPEC: spot age <= 60 s, measured at completion AND against the valuation time.
CLOCK_SKEW_S = 2                 # A vendor timestamp later than our completion clock by more than this is "future": rejected.
CLOSED_QUOTE_AFTER_CLOSE_S = 960  # Market closed: the spot is the session close, and an OPRA closing quote may be stamped up to
                                 # 16 min after it (ETF and index options trade until 16:15 ET; verified 2026-10-17: SPY
                                 # quotes at 16:14:59). Such a quote is solved at the close; one older than 60 s before it is not.

# ---------------------------------------------------------------- option settlement (America/New_York)
DEFAULT_SETTLE_PM = time(16, 0)  # SPEC: PM settlement 16:00 ET (early-close aware via the calendar).
DEFAULT_SETTLE_AM = time(9, 30)  # SPEC: AM settlement 09:30 ET.

# ---------------------------------------------------------------- AI-free copy guard
BANNED_WORDS = ("buy", "sell", "bullish", "bearish", "signal", "entry", "should")  # Brand rules 2 and 4.

# ---------------------------------------------------------------- net GEX magnitude reference
ADV_SESSIONS = 20                # Average daily dollar volume over the last 20 completed regular sessions: one trading
                                 # month, long enough to smooth one heavy day, short enough to track a regime change.
ADV_LOOKBACK_DAYS = 45           # Calendar days of daily bars requested to be sure 20 sessions are inside (holidays).
ADV_CACHE_FILE = ROOT / "data" / "cache" / "adv_daily.json"  # One entry per symbol, valid for one completed session.
ADV_RETRY_S = 300                # After a failed ADV fetch, retry in 5 min, never on every 60 s map refresh.
_NO_REF = "No free volume reference: index hedging trades mostly in ES and NQ futures"
ADV_NO_REFERENCE = {"SPX": _NO_REF, "NDX": _NO_REF}  # No share volume, and the hedge trades mostly in futures we have
                                 # no free data for. Scaling SPY/QQQ dollar volume by the price ratio is a units
                                 # error, so no share of ADV is shown (decision 2026-10-17, PROPOSED, supersedes the proxy).
MAGNITUDE_MOVES = (0.5, 1.0, 2.0)  # Hedge-flow rows in the method drawer: linear in the move, labeled as such.
MAGNITUDE_TICKS = (1, 2, 5, 10)  # Neutral reference ticks on the scale, percent of ADV. Descriptive, no colors.
PERCENTILE_MIN_HISTORY = 20      # No percentile until 20 eligible frozen captures exist; fewer is not a distribution.
ATR_SESSIONS = 14                # Typical day = simple mean true range of the last 14 completed regular sessions (the
                                 # common ATR length). Needs 15 daily bars: the first one only supplies a prior close.
ATR_CACHE_FILE = ROOT / "data" / "cache" / "atr_daily.json"  # Same daily cache rule as ADV: one entry per completed session.
ATR_FROM_ETF = {"SPX": "SPY", "NDX": "QQQ"}  # There are no index bars. ATR% is a percentage, so the ETF's ATR% is a
                                 # fair stand in for the index's (no units problem, unlike dollar volume). Labeled.

# ---------------------------------------------------------------- context metrics (display only)
# Vol Drift and DEX sit beside the levels on the Live Map. Neither touches the GEX math, the levels, the gate, the
# recorder captures or the studies.
VOL_DRIFT_MIN_FULL_SESSIONS = 3  # The expiry measured is the nearest one that settles at or after the close of the
                                 # third session after the current one: at least three full trading days left
                                 # (decided 2026-10-28). 0DTE IV is distorted late in the day, and on a 1-day expiry the clock
                                 # itself (calendar T, quiet overnight hours) moves IV through the session; three
                                 # sessions dilute that. Friday-only stocks have no such expiry inside the 0 to 7 day
                                 # window on Wednesdays and Thursdays: unavailable then.
VOL_DRIFT_BAND = 0.05            # Both strikes bracketing spot must lie within 5% of spot, and the quotes counted toward
                                 # VOL_DRIFT_MIN_USABLE are those inside the same band.
VOL_DRIFT_MIN_USABLE = 4         # Fewer usable IVs (calls plus puts) inside the band on that expiry: unavailable, never
                                 # a guess. Four is two strikes with both sides, the least that brackets spot.
VOL_DRIFT_FLAT_MIN_PTS = 0.5     # Flat band, relative to the IV level (decided 2026-10-28): flat while the change, as
VOL_DRIFT_FLAT_REL = 0.05        # displayed (0.1 vol point), is below max(0.5 vol point, 5% of the reference IV);
                                 # rising or falling at or beyond it. SPY at 19% IV: 0.95 point; NVDA at 42%: 2.1
                                 # points. Round starting values, not tuned on any outcome.
VOL_DRIFT_DEMO_REF = FIXTURES_DIR / "vol_ref.json"   # Synthetic 09:45 references for demo mode.

# ---------------------------------------------------------------- trade guidance
GUIDANCE_MAX_DATA_AGE_S = 300   # Market open: no setups when the vendor's data time (spot trade, or the payload) is
                                 # more than 5 min old, even if the last fetch "succeeded" (day-1 verification).
GUIDANCE_MIN_RR = 1.0            # A scenario is shown only when the reward from the entry zone's worst edge to target 1
                                 # is at least the risk from that edge to the stop. Below 1 to 1 the
                                 # idea needs a hit rate above 50% to break even, which nothing here has measured.

# ---------------------------------------------------------------- crypto (Deribit, model profile "crypto")
# Crypto runs through the same engine as equities with a different market profile: 24/7 trading, one fixed
# settlement instant (08:00 UTC), per-expiry forwards instead of spot, live open interest, and wider bands (BTC and
# ETH move several times more than SPY in a day). Nothing here touches the equity path.
# One entry per coin: where its options live, which instrument carries its volume, and how its price is shown.
#   deribit.currency  the get_book_summary_by_currency argument: the coin itself for the inverse BTC and ETH books, "USDC"
#                     for the linear books (SOL, TRX, ...). deribit.prefix is what its instrument names start with.
#   okx.family        OKX option family ("BTC-USD"); None = OKX lists no options for the coin (checked 2026-11-20).
#   okx.swap          the perpetual swap used as a volume and ATR reference.
#   px_dp             decimals of a price on screen. name is for the search box.
# A coin with no options anywhere (SUI, EGLD today) stays registered: each venue is asked, and the map says plainly that
# none lists options. The day one does, the coin works with no code change.
CRYPTO_COINS = {
    "BTC": {"name": "Bitcoin", "px_dp": 0,
            "deribit": {"currency": "BTC", "prefix": "BTC", "index": "btc_usd", "perp": "BTC-PERPETUAL"},
            "okx": {"family": "BTC-USD", "swap": "BTC-USDT-SWAP"}},
    "ETH": {"name": "Ether", "px_dp": 1,
            "deribit": {"currency": "ETH", "prefix": "ETH", "index": "eth_usd", "perp": "ETH-PERPETUAL"},
            "okx": {"family": "ETH-USD", "swap": "ETH-USDT-SWAP"}},
    "SOL": {"name": "Solana", "px_dp": 2,
            "deribit": {"currency": "USDC", "prefix": "SOL_USDC", "index": "sol_usdc", "perp": "SOL_USDC-PERPETUAL"},
            "okx": {"family": None, "swap": "SOL-USDT-SWAP"}},
    "TRX": {"name": "TRON", "px_dp": 5,
            "deribit": {"currency": "USDC", "prefix": "TRX_USDC", "index": "trx_usdc", "perp": "TRX_USDC-PERPETUAL"},
            "okx": {"family": None, "swap": "TRX-USDT-SWAP"}},
    "SUI": {"name": "Sui", "px_dp": 4,
            "deribit": {"currency": "USDC", "prefix": "SUI_USDC", "index": "sui_usdc", "perp": "SUI_USDC-PERPETUAL"},
            "okx": {"family": None, "swap": "SUI-USDT-SWAP"}},
    "EGLD": {"name": "MultiversX (EGLD)", "px_dp": 2,
             "deribit": {"currency": "USDC", "prefix": "EGLD_USDC", "index": "egld_usdc", "perp": "EGLD_USDC-PERPETUAL"},
             "okx": {"family": None, "swap": "EGLD-USDT-SWAP"}},
}
CRYPTO_TICKERS = set(CRYPTO_COINS)
NO_OPTIONS_COINS = {"SUI", "EGLD"}   # Checked live: no option listed on Deribit or OKX. Shown in search, 404 with a clear message.
VENUES = ("deribit", "okx", "bybit", "binance")      # Venues merged into one map, in spot-preference order (Deribit's index is the map's spot).
VENUE_LABELS = {"deribit": "Deribit", "okx": "OKX", "bybit": "Bybit", "binance": "Binance"}
VENUES_NOT_INCLUDED = ("Binance", "CME")   # Named in the warning so nobody reads the map as the whole market.
DERIBIT_BASE = "https://www.deribit.com/api/v2/public"  # Public REST only: no keys, no websocket, no trading endpoint.
OKX_BASE = "https://www.okx.com/api/v5"   # Public REST only: no keys, no websocket, no trading endpoint.
BYBIT_BASE = "https://api.bybit.com/v5"   # Public REST only. Blocks some cloud IPs (HTTP 403): then the venue is reported unavailable.
BYBIT_TIMEOUT_S = 20
BINANCE_BASE = "https://eapi.binance.com/eapi/v1"  # Public REST only. Blocks some IPs (HTTP 451/403): then the venue is reported unavailable.
BINANCE_TIMEOUT_S = 20
OKX_TIMEOUT_S = 20
OKX_SETTLE_UTC = time(8, 0)      # OKX options also settle at 08:00 UTC on the expiry date.
OKX_SPECS_TTL_S = 3600           # Contract size per family is static: fetched at most hourly.
DERIBIT_SUMMARY_TTL_S = 15       # The linear (USDC) book is shared between coins for this many seconds.
DERIBIT_TIMEOUT_S = 20           # A full BTC option summary is about 0.5 MB; 20 s is generous for a healthy API.
DERIBIT_SETTLE_UTC = time(8, 0)  # Every Deribit option settles at 08:00 UTC on its expiry date (daily, weekly, monthly, quarterly).
CRYPTO_SETTLEMENT = "UTC08"      # Contract.settlement value for them (equities use AM / PM).
CRYPTO_WINDOW_DAYS = 180         # Expiries 0 to 180 days: monthlies and quarterlies hold most of the open interest.
CRYPTO_MIN_T_HOURS = 1.0         # A contract with less than this left to 08:00 UTC is dropped, not marked invalid: ATM gamma grows as
                                 # 1 / sqrt(T), so the last hour of a daily expiry produces numbers that say nothing about hedging flows.
CRYPTO_STALE_AFTER_S = 180       # Same rule as equities: no successful refresh in 3 min means stale.
CRYPTO_DATA_MAX_AGE_S = 300      # Guidance gives no setups when the newest quote in the summary is older than this.
CRYPTO_PX_DECIMALS = {t: c["px_dp"] for t, c in CRYPTO_COINS.items()}  # Display precision of prices, in dollars.
CRYPTO_BANDS = {                 # Strike bands, fractions of spot (equity values in the lines above).
    "fetch": 0.40,               # Keep strikes within +-40% so the +-20% sweep sees edge gamma.
    "display": 0.20, "wall": 0.20, "sweep": 0.20, "coverage": 0.20,
    "gate": 0.10,                # Gate band for the |GEX| coverage share: +-10%, about two typical BTC daily ranges.
}
SWEEP_STEP.update({"BTC": 50.0, "ETH": 2.0})  # Zero-gamma grid steps in dollars: about 0.08% of spot, roughly 490 points over +-20%.
CRYPTO_STEP_FRAC = 0.0008        # Any other crypto: 0.08% of spot, the same density.
CRYPTO_VOLUME_INSTRUMENT = {t: c["deribit"]["perp"] for t, c in CRYPTO_COINS.items()}  # Deribit perpetual: volume and ATR bars.
CRYPTO_OKX_SWAP = {t: c["okx"]["swap"] for t, c in CRYPTO_COINS.items()}                # OKX swap: volume (and ATR when Deribit has none).


def is_crypto(ticker: str | None) -> bool:
    return (ticker or "").upper() in CRYPTO_TICKERS


def band(name: str, ticker: str | None = None) -> float:
    """Strike band for a ticker: 'fetch', 'display', 'wall', 'sweep', 'coverage' or 'gate' (fractions of spot)."""
    if is_crypto(ticker):
        return CRYPTO_BANDS[name]
    return {"fetch": FETCH_STRIKE_BAND, "display": DISPLAY_STRIKE_BAND, "wall": WALL_STRIKE_BAND,
            "sweep": SWEEP_BAND, "coverage": COVERAGE_STRIKE_BAND, "gate": GATE_OI_BAND}[name]
