import { useEffect, useMemo, useRef, useState } from "react";
import {
  ColorType,
  CrosshairMode,
  createChart,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { api } from "../api/client";
import type { GexMap } from "../api/types";
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
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("gex");
  const [pair, setPair] = useState(ticker === "BTC" ? "ETH" : "BTC");
  const pairCoin = pair === ticker ? CRYPTO_TICKERS.find((t) => t !== ticker) ?? "ETH" : pair;

  const map = useAsync(() => api.map(ticker, [], true), [ticker]);
  const mapB = useAsync(() => api.map(pairCoin, [], true), [pairCoin], tab === "gex");
  const candles = useAsync(() => api.candles(ticker, bar), [ticker, bar]);
  useEvery(map.reload, 60_000);
  useEvery(mapB.reload, 60_000);
  useEvery(candles.reload, REFRESH_MS);

  const last = candles.data?.candles[candles.data.candles.length - 1];
  const first = candles.data?.candles[0];
  const change = last && first ? ((last.c - first.o) / first.o) * 100 : null;
  const m = map.data && map.data.ticker === ticker ? map.data : null; // useAsync keeps the previous coin's data while loading

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
          {m?.venues?.map((v) => (
            <span key={v.name} className={`vbadge ${v.status}`} title={v.error ?? undefined}>
              {v.label}
              {v.status === "ok" && v.gex_share_pct != null ? ` ${v.gex_share_pct.toFixed(0)}%` : v.status === "no_options" ? " no options" : v.status === "unavailable" ? " down" : ""}
            </span>
          ))}
        </div>
      </div>

      <div className="term-main">
        <section className="term-chart" aria-label={`${ticker} candles with GEX levels`}>
          <CandleChart ticker={ticker} bar={bar} data={candles.data} map={m} />
          {candles.error && <div className="term-msg">{candles.error.message}</div>}
          {map.error && !m && <div className="term-msg term-msg-2">No GEX levels: {map.error.message}</div>}
        </section>

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
        GEX levels come from the same map as the Live map screen (Deribit + OKX + Bybit where available). Net GEX is dollars of delta
        exposure per 1% move under the calls +, puts - assumption. Research tool, not advice.
      </p>
    </div>
  );
}

function CandleChart({ ticker, bar, data, map }: { ticker: string; bar: string; data: import("../api/types").Candles | null; map: GexMap | null }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candle = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vol = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const fitted = useRef("");
  const spotRef = useRef<number | null>(null);
  const levelPx = useRef<number[]>([]);

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
    chart.current = c;
    return () => {
      c.remove();
      chart.current = candle.current = vol.current = null;
      lines.current = [];
    };
  }, []);

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
