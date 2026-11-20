import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ColorType,
  CrosshairMode,
  createChart,
  LineStyle,
  LineType,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { api } from "../api/client";
import type { FlowRow, GexMap, HedgeFlow, Snapshot } from "../api/types";
import { CRYPTO_TICKERS, gex, priceDigits, px } from "../lib/format";
import { useAsync } from "../lib/hooks";
import "./Terminal.css";

const BARS = ["5m", "15m", "1H", "4H", "1D"] as const;
const TABS = [
  { id: "levels", label: "Levels" },
  { id: "venues", label: "Sources" },
  { id: "gex", label: "GEX" },
] as const;
const LADDER_BAND = 0.08; // strikes shown: +-8% of spot
const REFRESH_MS = 30_000;
const BAR_S: Record<string, number> = { "5m": 300, "15m": 900, "1H": 3600, "4H": 14400, "1D": 86400 };
const utc = (ts: number) => new Date(ts * 1000).toISOString().slice(5, 16).replace("T", " ") + " UTC";

/** Reload `fn` on a timer without blanking the screen (useAsync keeps the previous data). */
function useEvery(reload: () => void, ms: number) {
  useEffect(() => {
    const id = window.setInterval(reload, ms);
    return () => window.clearInterval(id);
  }, [reload, ms]);
}

interface Props {
  ticker: string;
  onTicker: (t: string) => void;
}

export default function Terminal({ ticker, onTicker }: Props) {
  const [bar, setBar] = useState<(typeof BARS)[number]>("15m");
  const [mainChart, setMainChart] = useState<IChartApi | null>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("gex");
  const [pair, setPair] = useState(ticker === "BTC" ? "ETH" : "BTC");
  const pairCoin = pair === ticker ? CRYPTO_TICKERS.find((t) => t !== ticker) ?? "ETH" : pair;

  const map = useAsync(() => api.map(ticker, [], true), [ticker]);
  const mapB = useAsync(() => api.map(pairCoin, [], true), [pairCoin], tab === "gex");
  const candles = useAsync(() => api.candles(ticker, bar), [ticker, bar]);
  useEvery(map.reload, 60_000);
  useEvery(mapB.reload, 60_000);
  useEvery(candles.reload, REFRESH_MS);

  const flow = useAsync(() => api.historyFlow(ticker, bar), [ticker, bar]);
  const snaps = useAsync(() => api.historySnapshots(ticker, 24), [ticker]);
  useEvery(flow.reload, 60_000);
  const bf = useAsync(() => api.backfill(), []);
  useEvery(bf.reload, bf.data?.running ? 5_000 : 60_000);
  useEvery(snaps.reload, 60_000);
  const [replay, setReplay] = useState(false);
  const [idx, setIdx] = useState(0);
  const [playing, setPlaying] = useState(false);
  const stamps = snaps.data && snaps.data.ticker === ticker ? snaps.data.timestamps : [];
  const replayTs = replay && stamps.length ? stamps[Math.min(idx, stamps.length - 1)] : null;
  const snap = useAsync(() => api.historySnapshot(ticker, replayTs as number), [ticker, replayTs], replayTs !== null);
  useEffect(() => {
    setReplay(false);
    setPlaying(false);
  }, [ticker]);
  useEffect(() => {
    if (!playing) return;
    const id = window.setInterval(() => setIdx((i) => (i + 1 < stamps.length ? i + 1 : (setPlaying(false), i))), 700);
    return () => window.clearInterval(id);
  }, [playing, stamps.length]);
  const enterReplay = useCallback(() => {
    setIdx(Math.max(0, stamps.length - 1));
    setReplay(true);
  }, [stamps.length]);

  const last = candles.data?.candles[candles.data.candles.length - 1];
  const first = candles.data?.candles[0];
  const change = last && first ? ((last.c - first.o) / first.o) * 100 : null;
  const live = map.data && map.data.ticker === ticker ? map.data : null; // useAsync keeps the previous coin's data while loading
  const sd: Snapshot | null = replayTs !== null && snap.data && snap.data.ticker === ticker ? snap.data : null;
  // while replaying, every panel reads the stored snapshot instead of the live map
  const m: GexMap | null = sd
    ? ({
        ticker, spot: { price: sd.spot, ts: "", source: "snapshot" }, levels: sd.levels, totals: sd.totals, strikes: sd.strikes,
        venues: sd.venues, lead: `Replay: ${sd.lead ?? ""}`, warnings: [`Stored snapshot from ${utc(sd.ts)}, not live.`],
        hedge_flow: sd.hedge_flow, oi_source: null,
      } as unknown as GexMap)
    : live;

  return (
    <div className="term">
      <div className="term-bar">
        <div className="term-tickers" role="group" aria-label="Coin">
          {CRYPTO_TICKERS.map((t) => (
            <button key={t} type="button" className="chip ticker" aria-pressed={t === ticker} onClick={() => onTicker(t)}>
              {t}
            </button>
          ))}
        </div>
        <div className="term-price">
          <b>{last ? px(last.c, ticker) : "…"}</b>
          {change !== null && <span className={change >= 0 ? "up" : "dn"}>{`${change >= 0 ? "+" : ""}${change.toFixed(2)}%`}</span>}
          <span className="term-sub">{candles.data ? `${candles.data.source.replace("okx:", "OKX ").replace("okx_swap_recorded", "OKX swap, recorded")} · UTC` : ""}</span>
        </div>
        <div className="term-bars" role="group" aria-label="Timeframe">
          {BARS.map((b) => (
            <button key={b} type="button" className="chip" aria-pressed={b === bar} onClick={() => setBar(b)}>
              {b}
            </button>
          ))}
        </div>
        <div className="term-badges">
          {live?.venues?.map((v) => (
            <span key={v.name} className={`vbadge ${v.status}`} title={v.error ?? undefined}>
              {v.label}
              {v.status === "ok" && v.gex_share_pct != null ? ` ${v.gex_share_pct.toFixed(0)}%` : v.status === "no_options" ? " no options" : v.status === "unavailable" ? " down" : ""}
            </span>
          ))}
        </div>
      </div>

      {bf.data && (bf.data.running || bf.data.stored_bars > 0) && (
        <div className="term-sub" role="status">
          {bf.data.running
            ? `Downloading price history: ${bf.data.done}/${bf.data.total} series${bf.data.current ? ` (${bf.data.current})` : ""} · ${bf.data.stored_bars.toLocaleString()} bars stored locally`
            : `Local price history: ${bf.data.stored_bars.toLocaleString()} bars stored${bf.data.error ? ` · last error: ${bf.data.error}` : ""}`}
        </div>
      )}

      <div className="term-replay">
        {!replay ? (
          <>
            <button type="button" className="chip" onClick={enterReplay} disabled={!stamps.length}>Replay</button>
            <span className="term-sub">
              {stamps.length
                ? `${stamps.length} snapshots in the last 24 h · from ${utc(stamps[0])}`
                : snaps.data?.recording
                  ? "Recording: the first snapshot lands within 5 minutes. History starts when recording starts; nothing is back-filled."
                  : "No snapshots stored. The app records the GEX map every 5 min while it runs (GEXLAB_RECORD=0 turns that off)."}
            </span>
          </>
        ) : (
          <>
            <button type="button" className="chip on" onClick={() => { setReplay(false); setPlaying(false); }}>Live</button>
            <button type="button" className="chip" onClick={() => setIdx((i) => Math.max(0, i - 1))} aria-label="Previous snapshot">‹</button>
            <button type="button" className="chip" onClick={() => setPlaying((p) => !p)} aria-pressed={playing}>{playing ? "Pause" : "Play"}</button>
            <button type="button" className="chip" onClick={() => setIdx((i) => Math.min(stamps.length - 1, i + 1))} aria-label="Next snapshot">›</button>
            <input type="range" min={0} max={Math.max(0, stamps.length - 1)} value={Math.min(idx, stamps.length - 1)} onChange={(e) => { setPlaying(false); setIdx(Number(e.target.value)); }} aria-label="Snapshot time" />
            <b className="term-replay-at">{replayTs ? utc(replayTs) : ""}</b>
          </>
        )}
      </div>

      <div className="term-main">
        <div className="term-left">
          <section className="term-chart" aria-label={`${ticker} candles with GEX levels`}>
            <CandleChart ticker={ticker} bar={bar} data={candles.data} map={m} flows={flow.data?.rows ?? []} replayTs={replayTs} onChart={setMainChart} />
            {candles.error && <div className="term-msg">{candles.error.message}</div>}
            {map.error && !m && <div className="term-msg term-msg-2">No GEX levels: {map.error.message}</div>}
          </section>
          <FlowChart bar={bar} rows={flow.data?.rows ?? []} coverage={flow.data?.coverage} main={mainChart} err={flow.error?.message} />
          {m?.hedge_flow && <HedgeStrip ticker={ticker} hf={m.hedge_flow} spot={m.spot.price} callWall={m.levels.call_wall} putWall={m.levels.put_wall} />}
        </div>

        <aside className="term-side">
          <div className="term-tabs" role="tablist">
            {TABS.map((t) => (
              <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}>
                {t.label}
              </button>
            ))}
          </div>
          {tab === "levels" && <LevelsTab ticker={ticker} m={m} err={map.error?.message} />}
          {tab === "venues" && <VenuesTab m={m} err={map.error?.message} />}
          {tab === "gex" && (
            <div className="term-multi">
              <div className="term-multi-head">
                <span>MultiMap</span>
                <select aria-label="Second coin" value={pairCoin} onChange={(e) => setPair(e.target.value)}>
                  {CRYPTO_TICKERS.filter((t) => t !== ticker).map((t) => (
                    <option key={t}>{t}</option>
                  ))}
                </select>
              </div>
              <div className="term-ladders">
                <Ladder ticker={ticker} m={m} err={map.error?.message} />
                <Ladder ticker={pairCoin} m={mapB.data && mapB.data.ticker === pairCoin ? mapB.data : null} err={mapB.error?.message} />
              </div>
            </div>
          )}
        </aside>
      </div>
      <p className="term-note">
        GEX levels come from the same map as the Live map screen (Deribit + OKX + Bybit + Binance where available). Net GEX is dollars of delta
        exposure per 1% move under the calls +, puts - assumption. Research tool, not advice.
      </p>
    </div>
  );
}

function CandleChart({ ticker, bar, data, map, flows, replayTs, onChart }: { ticker: string; bar: string; data: import("../api/types").Candles | null; map: GexMap | null; flows: FlowRow[]; replayTs: number | null; onChart: (c: IChartApi | null) => void }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candle = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vol = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const fitted = useRef("");
  const spotRef = useRef<number | null>(null);
  const levelPx = useRef<number[]>([]);
  const callHist = useRef<ISeriesApi<"Line"> | null>(null);
  const putHist = useRef<ISeriesApi<"Line"> | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const c = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#07050b" }, textColor: "#8a8f98", fontFamily: "JetBrains Mono, monospace", fontSize: 11 },
      grid: { vertLines: { color: "#14101a" }, horzLines: { color: "#14101a" } },
      rightPriceScale: { borderColor: "#2a2235", scaleMargins: { top: 0.06, bottom: 0.22 } },
      timeScale: { borderColor: "#2a2235", timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
    });
    candle.current = c.addCandlestickSeries({
      autoscaleInfoProvider: (orig: () => { priceRange: { minValue: number; maxValue: number } | null; margins?: { above: number; below: number } } | null) => {
        const r = orig();
        const sp = spotRef.current;
        if (!r || !r.priceRange || sp == null) return r;
        // pull walls and the flip into view only when they are within 12% of spot, so a far wall never flattens the candles
        let { minValue, maxValue } = r.priceRange;
        for (const v of levelPx.current) if (Math.abs(v - sp) / sp <= 0.12) { minValue = Math.min(minValue, v); maxValue = Math.max(maxValue, v); }
        return { ...r, priceRange: { minValue, maxValue } };
      },
      upColor: "#78d202", downColor: "#9b4be8", wickUpColor: "#78d202", wickDownColor: "#9b4be8", borderVisible: false,
    });
    vol.current = c.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", color: "#3a3046" });
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    callHist.current = c.addLineSeries({ color: "rgba(120,210,2,0.55)", lineWidth: 1, lineType: LineType.WithSteps, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    putHist.current = c.addLineSeries({ color: "rgba(181,123,240,0.65)", lineWidth: 1, lineType: LineType.WithSteps, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    chart.current = c;
    onChart(c);
    return () => {
      onChart(null);
      c.remove();
      chart.current = candle.current = vol.current = callHist.current = putHist.current = null;
      lines.current = [];
    };
  }, []);

  useEffect(() => {
    // wall history as step lines (what the walls were, candle by candle), plus the replay cursor
    const up = flows.filter((f) => f.call_wall != null).map((f) => ({ time: f.t as UTCTimestamp, value: f.call_wall as number }));
    const dn = flows.filter((f) => f.put_wall != null).map((f) => ({ time: f.t as UTCTimestamp, value: f.put_wall as number }));
    callHist.current?.setData(up);
    putHist.current?.setData(dn);
  }, [flows]);

  useEffect(() => {
    const s = candle.current;
    if (!s) return;
    const step = BAR_S[bar] ?? 900;
    s.setMarkers(replayTs == null ? [] : [{ time: (Math.floor(replayTs / step) * step) as UTCTimestamp, position: "aboveBar", color: "#ffffff", shape: "arrowDown", text: "replay" }]);
  }, [replayTs, bar, data]);

  useEffect(() => {
    const dp = priceDigits(ticker);
    candle.current?.applyOptions({ priceFormat: { type: "price", precision: dp, minMove: Math.pow(10, -dp) } });
  }, [ticker]);

  useEffect(() => {
    if (!data || !candle.current || !vol.current) return;
    candle.current.setData(data.candles.map((k) => ({ time: k.t as UTCTimestamp, open: k.o, high: k.h, low: k.l, close: k.c })));
    vol.current.setData(
      data.candles.map((k) => ({ time: k.t as UTCTimestamp, value: k.v, color: k.c >= k.o ? "rgba(120,210,2,0.35)" : "rgba(155,75,232,0.35)" })),
    );
    const key = `${ticker}|${bar}`;
    if (fitted.current !== key) {
      chart.current?.timeScale().fitContent();
      fitted.current = key;
    }
  }, [data, ticker, bar]);

  useEffect(() => {
    const s = candle.current;
    if (!s) return;
    lines.current.forEach((l) => s.removePriceLine(l));
    lines.current = [];
    const lv = map?.ticker === ticker ? map.levels : null;
    spotRef.current = map?.ticker === ticker ? map.spot.price : null;
    levelPx.current = lv ? [lv.call_wall, lv.put_wall, lv.nearest_zero_gamma].filter((v): v is number => v != null) : [];
    if (!lv) return;
    const add = (price: number | null | undefined, title: string, color: string, style: LineStyle) => {
      if (price == null) return;
      lines.current.push(s.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
    };
    add(lv.call_wall, "CALL WALL", "#78d202", LineStyle.Solid);
    add(lv.put_wall, "PUT WALL", "#b57bf0", LineStyle.Solid);
    add(lv.nearest_zero_gamma, "FLIP", "#c9a25e", LineStyle.Dashed);
  }, [map, ticker]);

  return <div ref={host} className="term-canvas" />;
}

/** Per-candle modeled hedge flow from the recorded snapshots, on its own chart synced to the candles' time axis. */
function FlowChart({ bar, rows, coverage, main, err }: { bar: string; rows: FlowRow[]; coverage?: { snaps: number; first_ts: number | null }; main: IChartApi | null; err?: string }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Histogram"> | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const c = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#07050b" }, textColor: "#8a8f98", fontFamily: "JetBrains Mono, monospace", fontSize: 10 },
      grid: { vertLines: { color: "#14101a" }, horzLines: { color: "#14101a" } },
      rightPriceScale: { borderColor: "#2a2235", minimumWidth: 70 },
      timeScale: { borderColor: "#2a2235", timeVisible: true, secondsVisible: false, visible: false },
      crosshair: { mode: CrosshairMode.Normal },
      handleScroll: false,
      handleScale: false,
    });
    series.current = c.addHistogramSeries({ priceFormat: { type: "custom", formatter: (v: number) => gex(v, { sign: true }) }, priceLineVisible: false });
    chart.current = c;
    return () => {
      c.remove();
      chart.current = series.current = null;
    };
  }, []);

  useEffect(() => {
    series.current?.setData(rows.map((r) => ({ time: r.t as UTCTimestamp, value: r.flow_usd, color: r.flow_usd >= 0 ? "rgba(120,210,2,0.8)" : "rgba(155,75,232,0.85)" })));
  }, [rows]);

  useEffect(() => {
    if (!main || !chart.current) return;
    const c = chart.current;
    const sync = (r: { from: unknown; to: unknown } | null) => {
      if (!r) return;
      try {
        c.timeScale().setVisibleRange(r as { from: UTCTimestamp; to: UTCTimestamp });
      } catch {
        /* the flow series covers less time than the candles: nothing to align yet */
      }
    };
    main.timeScale().subscribeVisibleTimeRangeChange(sync);
    sync(main.timeScale().getVisibleRange());
    return () => main.timeScale().unsubscribeVisibleTimeRangeChange(sync);
  }, [main, rows]);

  return (
    <section className="term-flow" aria-label="Modeled hedge flow per candle">
      <div className="term-hedge-head">
        <b>Hedge flow per candle ({bar}, model)</b>
        <span>
          {rows.length
            ? `${rows.length} candles from ${coverage?.snaps ?? 0} snapshots · green buy, purple sell · walls drawn as step lines`
            : err ?? "No history yet: the app saves the GEX map every 5 min while it runs; candles before that stay empty."}
        </span>
      </div>
      <div ref={host} className="term-flow-canvas" />
    </section>
  );
}

/** Modeled dealer hedge flow per price step around spot: green = dealers buy, purple = sell. A model, not a measurement. */
function HedgeStrip({ ticker, hf, spot, callWall, putWall }: { ticker: string; hf: HedgeFlow; spot: number; callWall: number | null; putWall: number | null }) {
  const W = 1000, H = 120, mid = H / 2;
  const lo = spot * (1 - hf.band_pct / 100), hi = spot * (1 + hf.band_pct / 100);
  const x = (v: number) => ((v - lo) / (hi - lo)) * W;
  const max = Math.max(1, ...hf.bins.map((b) => Math.abs(b.flow_usd)));
  const mark = (v: number | null, label: string, color: string) =>
    v != null && v >= lo && v <= hi ? (
      <g key={label}>
        <line x1={x(v)} x2={x(v)} y1={0} y2={H} stroke={color} strokeWidth={1} strokeDasharray="3 3" />
        <text x={x(v) + 4} y={11} fill={color} fontSize={10} fontFamily="JetBrains Mono, monospace">{label}</text>
      </g>
    ) : null;
  const verb = (v: number) => (v >= 0 ? "buy" : "sell");
  return (
    <section className="term-hedge" aria-label="Modeled dealer hedge flow">
      <div className="term-hedge-head">
        <b>Dealer hedge flow (model)</b>
        <span>
          to {px(hi, ticker)}: dealers {verb(hf.to_up_usd)} ≈ {gex(Math.abs(hf.to_up_usd))} · to {px(lo, ticker)}: {verb(hf.to_down_usd)} ≈ {gex(Math.abs(hf.to_down_usd))}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="Bars of modeled dealer buying (green) and selling (purple) at each price step around spot">
        <line x1={0} x2={W} y1={mid} y2={mid} stroke="#2a2235" />
        {hf.bins.map((b) => {
          const h = (Math.abs(b.flow_usd) / max) * (mid - 14);
          const buy = b.flow_usd >= 0;
          return (
            <rect key={b.lo} x={x(b.lo) + 0.5} width={Math.max(1, x(b.hi) - x(b.lo) - 1)} y={buy ? mid - h : mid} height={h} fill={buy ? "#78d202" : "#9b4be8"} opacity={0.85}>
              <title>{`${px(b.lo, ticker)} to ${px(b.hi, ticker)}: dealers ${verb(b.flow_usd)} ≈ ${gex(Math.abs(b.flow_usd))}`}</title>
            </rect>
          );
        })}
        {mark(spot, "SPOT", "#ffffff")}
        {mark(callWall, "CALL", "#78d202")}
        {mark(putWall, "PUT", "#b57bf0")}
      </svg>
      <p className="term-hedge-note">{hf.method}</p>
    </section>
  );
}

function Ladder({ ticker, m, err }: { ticker: string; m: GexMap | null; err?: string }) {
  const rows = useMemo(() => {
    if (!m || m.ticker !== ticker) return [];
    const s = m.spot.price;
    const r = m.strikes.filter((k) => Math.abs(k.strike - s) / s <= LADDER_BAND).sort((a, b) => b.strike - a.strike);
    const max = Math.max(1, ...r.map((k) => Math.abs(k.net_gex)));
    const lo = r.length ? r[r.length - 1].strike : 0, hi = r.length ? r[0].strike : 0;
    const near = (v: number | null | undefined) =>
      v == null || !r.length || v < lo || v > hi ? null : r.reduce((a, b) => (Math.abs(b.strike - v) < Math.abs(a.strike - v) ? b : a)).strike;
    const spotRow = near(s), flipRow = near(m.levels.nearest_zero_gamma);
    return r.map((k) => ({
      strike: k.strike,
      net: k.net_gex,
      w: (Math.abs(k.net_gex) / max) * 100,
      cw: k.strike === m.levels.call_wall,
      pw: k.strike === m.levels.put_wall,
      flip: k.strike === flipRow,
      spot: k.strike === spotRow,
    }));
  }, [m, ticker]);

  return (
    <div className="ladder">
      <div className="ladder-head">
        <b>{ticker}</b>
        <span>{m && m.ticker === ticker ? px(m.spot.price, ticker) : ""}</span>
        <span className={m && m.ticker === ticker && m.levels.regime === "negative" ? "neg" : "pos"}>
          {m && m.ticker === ticker ? (m.levels.regime === "negative" ? "gamma −" : m.levels.regime === "positive" ? "gamma +" : "") : ""}
        </span>
      </div>
      {err && <div className="ladder-empty">{err}</div>}
      {!err && !rows.length && <div className="ladder-empty">Loading…</div>}
      <div className="ladder-rows">
        {rows.map((r) => (
          <div key={r.strike} className={`lrow ${r.spot ? "spot" : ""} ${r.cw || r.pw ? "wall" : ""}`}>
            <span className="lbar" style={{ width: `${r.w}%` }} data-neg={r.net < 0} />
            <span className="lk">
              {r.cw && <i className="tag cw">CW</i>}
              {r.pw && <i className="tag pw">PW</i>}
              {r.flip && <i className="tag fl">FLIP</i>}
              {px(r.strike, ticker)}
            </span>
            <span className="lv">{gex(r.net, { sign: true })}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function LevelsTab({ ticker, m, err }: { ticker: string; m: GexMap | null; err?: string }) {
  if (err) return <div className="term-pad">{err}</div>;
  if (!m) return <div className="term-pad">Loading…</div>;
  const s = m.spot.price;
  const d = (v: number | null | undefined) => (v == null ? "" : `${(((v - s) / s) * 100).toFixed(1)}% from spot`);
  const L = m.levels;
  const cells: [string, string, string, string][] = [
    ["Spot", px(s, ticker), "", ""],
    ["Call wall", px(L.call_wall, ticker), d(L.call_wall), "pos"],
    ["Put wall", px(L.put_wall, ticker), d(L.put_wall), "neg"],
    ["Gamma flip", L.nearest_zero_gamma == null ? "none within ±20%" : px(L.nearest_zero_gamma, ticker), d(L.nearest_zero_gamma), ""],
    ["Net GEX / 1%", gex(m.totals.net_gex, { sign: true }), `calls ${gex(m.totals.call_gex)} · puts ${gex(m.totals.put_gex)}`, ""],
  ];
  return (
    <div className="term-pad">
      <div className="lv-grid">
        {cells.map(([k, v, sub, cls]) => (
          <div key={k} className="lv-cell">
            <span>{k}</span>
            <b className={cls}>{v}</b>
            <small>{sub}</small>
          </div>
        ))}
      </div>
      <p className="lv-lead">{m.lead}</p>
      <ul className="lv-warn">
        {m.warnings.slice(0, 5).map((w) => (
          <li key={w}>{w}</li>
        ))}
      </ul>
    </div>
  );
}

function VenuesTab({ m, err }: { m: GexMap | null; err?: string }) {
  if (err) return <div className="term-pad">{err}</div>;
  if (!m) return <div className="term-pad">Loading…</div>;
  return (
    <div className="term-pad">
      <div className="lv-lead">{m.oi_source?.label ?? "Open interest"}</div>
      {(m.venues ?? []).map((v) => (
        <div key={v.name} className="vrow">
          <div>
            <b>{v.label}</b> <span className={`vbadge ${v.status}`}>{v.status.replace("_", " ")}</span>
          </div>
          <div className="vbar">
            <span style={{ width: `${v.status === "ok" ? v.gex_share_pct ?? 0 : 0}%` }} />
          </div>
          <small>
            {v.status === "ok" ? `${v.contracts} contracts · ${v.gex_share_pct?.toFixed(1) ?? "0"}% of |GEX|` : v.error ?? ""}
          </small>
        </div>
      ))}
    </div>
  );
}
