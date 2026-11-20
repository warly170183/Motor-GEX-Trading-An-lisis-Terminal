"""Plain data shapes shared by sources, engine, recorder and API."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

@dataclass
class Contract:
    symbol: str
    root: str
    expiry: date
    settlement: str                 # AM | PM
    strike: float
    type: str                       # call | put
    multiplier: int = 100
    oi: int | None = None
    oi_date: str | None = None      # the vendor's open_interest_date for THIS contract
    oi_basis: str | None = None     # reported | zero_never_traded | zero_traded_after_oi_date | zero_vendor_null
    oi_source: str | None = None    # which vendor's open interest this contract carries
    bid: float | None = None
    ask: float | None = None
    quote_ts: datetime | None = None
    iv: float | None = None
    iv_source: str | None = None    # solved_mid | indicative | vendor | demo (method of the IV in use)
    iv_ts: datetime | None = None   # market-data time of the quote the IV in use came from (None = unknown age)
    iv_shared: bool = False         # True when iv was copied from the other member of the strike pair
    iv_own: float | None = None     # this contract's own IV before sharing (None when it had none)
    iv_own_source: str | None = None
    iv_donor: str | None = None     # symbol whose IV is in use when iv_shared
    iv_donor_ts: datetime | None = None  # the donor's IV time (its iv_ts)
    last_trade_date: str | None = None   # vendor last trade date; decodes a null OI record
    gamma: float | None = None
    gex: float | None = None        # signed, dollars per 1% move
    T: float | None = None          # years to settlement at as_of
    venue: str | None = None        # crypto: which exchange this row came from (deribit | okx | bybit | binance); None for equities
    fwd: float | None = None        # forward of the underlying for THIS expiry (crypto: Deribit underlying_price); None = spot
    fwd_ratio: float = 1.0          # fwd / spot at the map's spot, so a hypothetical spot S carries the forward S * fwd_ratio
    valid: bool = False
    invalid_reason: str | None = None


@dataclass
class Spot:
    price: float
    ts: datetime
    source: str                     # deribit | okx | bybit | binance | demo
    basis: str = "last_trade"       # last_trade (in session) | regular_close (market closed: the session's
                                    # official close, never an extended-hours print) | payload (the feed's own price)
                                    # | prior_close (market open but the price predates today's open)


@dataclass
class Chain:
    """One fetched option chain for the 0 to 7 DTE window, ready for the engine."""
    ticker: str
    as_of: datetime                 # request (wall clock) time of the fetch
    spot: Spot
    feed: str                       # deribit | okx | deribit+okx | demo
    contracts: list[Contract]
    oi_date: str | None = None
    source_ts: datetime | None = None   # payload timestamp when the source provides one 
    complete: bool = True               # every page fetched
    raw_hash: str | None = None         # sha256 over raw response bodies, in fetch order
    warnings: list[str] = field(default_factory=list)
    requests: int = 0                   # vendor requests this fetch cost
    oi_source: str | None = None        # deribit | okx | bybit | binance (demo: None): the open interest vendor for the chain
    oi_info: dict = field(default_factory=dict)  # OI provenance: label, basis, matched counts, fallback reason
    valuation_ts: datetime | None = None  # the one clock T is measured from (see calendar.valuation_time);
                                          # None = as_of (demo fixtures, older callers)
    venues: list[dict] = field(default_factory=list)  # crypto: one row per venue asked: name, status (ok | no_options |
                                                      # unavailable), error, and, once the map is built, its share of |GEX|
    index_ts: datetime | None = None    # index chains: the index value's own market-data time;
                                        # None for other feeds

    @property
    def valuation(self) -> datetime:
        """Valuation time: T for every contract is settlement minus this. as_of stays the request time."""
        return self.valuation_ts or self.as_of
