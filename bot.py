#!/usr/bin/env python3
"""
Claude Bot – Trend+RSI Paper-Trading-Bot (Phase 1: eigenes Spielgeld-Konto)

Strategie (wie „SMC Lab v3“ / TrendRSI in TradingView):
  Long : Close > EMA(200) und RSI(14) kreuzt über 35
  Short: Close < EMA(200) und RSI(14) kreuzt unter 65
  Stop : 1,5 × ATR(14) vom Einstieg, Ziel: 2 × Stop-Abstand (2R)
  Risiko: 2 % der Equity pro Trade, max. 60 % der Equity pro Position, Gebühr 0,1 % je Seite

Befehle:
  python3 bot.py backtest            # Backtest ab 2024-01-01, Lern- vs. Prüf-Zeitraum
  python3 bot.py run                 # ein Live-Durchlauf (alle 4 h starten)
  python3 bot.py status              # Konto, Positionen, letzte Trades anzeigen

Nur Standardbibliothek – läuft auf dem Mac, Raspberry Pi oder GitHub Actions.
Kein echtes Geld, keine API-Schlüssel: Phase 1 handelt in einem simulierten Konto (state/).
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ───── Einstellungen ─────
UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK"]
DEFAULT_COINS = ["BTC", "ETH", "SOL"]
QUOTE = "USDT"
INTERVAL = "4h"
BAR_MS = 4 * 60 * 60 * 1000
EMA_LEN, RSI_LEN, ATR_LEN = 200, 14, 14
RSI_LO, RSI_HI = 35, 65
SL_ATR, RR = 1.5, 2.0
RISK, MAX_POS_FRAC, FEE = 0.02, 0.6, 0.001
START_EQUITY = 100_000.0
MAX_POSITIONS = 4
VERSION = "TrendRSI v3 (Python)"

DATA_URL = "https://data-api.binance.vision/api/v3/klines"
KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD", "BNB": "BNBUSD",
                "DOGE": "XDGUSD", "AVAX": "AVAXUSD", "LINK": "LINKUSD"}
DEFAULT_PARAMS = {"rsi_lo": 35, "rsi_hi": 65, "sl_atr": 1.5, "rr": 2.0, "adx_min": 0, "coins": DEFAULT_COINS,
                  "version": "TrendRSI v3", "since": None}
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")


# ───── Daten ─────
def fetch_klines(symbol, start_ms=None, limit=1000):
    """Geschlossene 4h-Kerzen von Binance (öffentlich, ohne Schlüssel)."""
    out, cursor = [], start_ms
    while True:
        url = f"{DATA_URL}?symbol={symbol}&interval={INTERVAL}&limit={limit}"
        if cursor is not None:
            url += f"&startTime={cursor}"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "claude-bot"}), timeout=20) as r:
            rows = json.load(r)
        if not rows:
            break
        for k in rows:
            out.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])})
        if cursor is None or len(rows) < limit:
            break
        cursor = rows[-1][0] + BAR_MS
        time.sleep(0.2)
    now_ms = int(time.time() * 1000)
    return [b for b in out if b["t"] + BAR_MS <= now_ms]  # laufende Kerze weglassen


def fetch_kraken(coin):
    """Ersatzquelle (z. B. wenn Binance auf GitHub-Servern gesperrt ist): letzte 720 4h-Kerzen in USD."""
    url = f"{KRAKEN_URL}?pair={KRAKEN_PAIRS[coin]}&interval=240"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "claude-bot"}), timeout=20) as r:
        js = json.load(r)
    if js.get("error"):
        raise RuntimeError("Kraken: " + ", ".join(js["error"]))
    rows = next(v for k, v in js["result"].items() if k != "last")
    now_ms = int(time.time() * 1000)
    bars = [{"t": int(k[0]) * 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])} for k in rows]
    return [b for b in bars if b["t"] + BAR_MS <= now_ms]


def fetch_live(coin):
    """Live-Kerzen: zuerst Binance, bei Fehler Kraken. Gibt (Kerzen, Quelle) zurück."""
    try:
        return fetch_klines(coin + QUOTE), "Binance"
    except Exception as e:  # noqa: BLE001
        print(f"{coin}: Binance nicht erreichbar ({e}), nutze Kraken")
        return fetch_kraken(coin), "Kraken"


# ───── Indikatoren (wie Pine: ta.ema, ta.rsi, ta.atr) ─────
def ema(values, n):
    out, a, e = [], 2 / (n + 1), None
    for i, v in enumerate(values):
        if i < n - 1:
            out.append(None)
            continue
        e = sum(values[:n]) / n if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


def rma(values, n):
    out, r = [], None
    for i, v in enumerate(values):
        if v is None or i < n:
            out.append(None)
            continue
        r = sum(values[i - n + 1:i + 1]) / n if r is None else (r * (n - 1) + v) / n
        out.append(r)
    return out


def rsi(closes, n):
    up = [None] + [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    dn = [None] + [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ru, rd = rma(up, n), rma(dn, n)
    return [None if u is None or d is None else (100.0 if d == 0 else 100 - 100 / (1 + u / d)) for u, d in zip(ru, rd)]


def atr(bars, n):
    tr = [bars[0]["h"] - bars[0]["l"]] + [
        max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])
    ]
    out, r = [], None
    for i, v in enumerate(tr):
        if i < n - 1:
            out.append(None)
            continue
        r = sum(tr[:n]) / n if r is None else (r * (n - 1) + v) / n
        out.append(r)
    return out


def adx(bars, n=14):
    """ADX nach Wilder – misst, wie stark ein Trend ist (über ~20 = Trend, darunter = Seitwärts)."""
    pdm, ndm, tr = [0.0], [0.0], [bars[0]["h"] - bars[0]["l"]]
    for p, b in zip(bars, bars[1:]):
        up, dn = b["h"] - p["h"], p["l"] - b["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])))
    def wild(v):
        out, r = [], None
        for i, x in enumerate(v):
            if i < n:
                out.append(None)
                continue
            r = sum(v[1:n + 1]) if r is None else r - r / n + x
            out.append(r)
        return out
    st, sp, sn = wild(tr), wild(pdm), wild(ndm)
    dx = [None if a is None or a == 0 else 100 * abs(p - m) / (p + m) if (p + m) else 0 for a, p, m in zip(st, sp, sn)]
    return rma([0.0 if v is None else v for v in dx], n)


def chart_rows(coin, bars, n=90, p=None):
    """Letzte n Kerzen mit EMA, RSI und Signal – für die Charts auf der Übersichtsseite."""
    c = [b["c"] for b in bars]
    e, r = ema(c, EMA_LEN), rsi(c, RSI_LEN)
    sig, _ = signals(bars, p)
    out = []
    for i in range(max(0, len(bars) - n), len(bars)):
        b = bars[i]
        out.append({"coin": coin, "t": iso(b["t"]), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                    "ema": round(e[i], 6) if e[i] is not None else None,
                    "rsi": round(r[i], 2) if r[i] is not None else None, "sig": sig[i]})
    return out


def signals(bars, p=None, cache=None):
    """Signal je Kerze: 1 Long, -1 Short, 0 nichts – plus ATR für den Stop."""
    p = p or DEFAULT_PARAMS
    if cache is None:
        c = [b["c"] for b in bars]
        cache = {"c": c, "e": ema(c, EMA_LEN), "r": rsi(c, RSI_LEN), "a": atr(bars, ATR_LEN),
                 "x": adx(bars) if p.get("adx_min") else None}
    c, e, r, a = cache["c"], cache["e"], cache["r"], cache["a"]
    if p.get("adx_min") and cache.get("x") is None:
        cache["x"] = adx(bars)
    x = cache.get("x")
    lo, hi, amin = p["rsi_lo"], p["rsi_hi"], p.get("adx_min", 0)
    sig = [0] * len(bars)
    for i in range(1, len(bars)):
        if None in (e[i], r[i], r[i - 1], a[i]):
            continue
        if amin and (x[i] is None or x[i] < amin):
            continue
        if c[i] > e[i] and r[i] > lo and r[i - 1] < lo:
            sig[i] = 1
        elif c[i] < e[i] and r[i] < hi and r[i - 1] > hi:
            sig[i] = -1
    return sig, a


# ───── Konto-Logik (gemeinsam für Backtest und Live) ─────
def open_position(acct, coin, side, price, atr_v, t, p=None):
    p = p or DEFAULT_PARAMS
    dist = p["sl_atr"] * atr_v
    if dist <= 0 or coin in acct["positions"] or len(acct["positions"]) >= MAX_POSITIONS:
        return None
    eq = acct["equity"]
    qty = min(RISK * eq / dist, MAX_POS_FRAC * eq / price)
    pos = {
        "coin": coin, "side": side, "qty": qty, "entry": price,
        "stop": price - side * dist, "target": price + side * p["rr"] * dist,
        "risk_usd": qty * dist, "opened_at": t, "version": p.get("version", VERSION),
    }
    acct["cash"] -= qty * price * FEE
    acct["positions"][coin] = pos
    return pos


def check_exit(acct, coin, bar):
    """Stop zuerst (vorsichtig), dann Ziel. Gibt den geschlossenen Trade zurück."""
    p = acct["positions"].get(coin)
    if not p:
        return None
    s = p["side"]
    hit_stop = bar["l"] <= p["stop"] if s == 1 else bar["h"] >= p["stop"]
    hit_tp = bar["h"] >= p["target"] if s == 1 else bar["l"] <= p["target"]
    if not (hit_stop or hit_tp):
        return None
    exit_px = p["stop"] if hit_stop else p["target"]
    gross = s * (exit_px - p["entry"]) * p["qty"]
    fee = exit_px * p["qty"] * FEE
    acct["cash"] += gross - fee
    acct["trade_no"] = acct.get("trade_no", 0) + 1
    trade = {
        "id": str(acct["trade_no"]), "kind": "Trade", "coin": coin,
        "side": "Long" if s == 1 else "Short", "entry": p["entry"], "exit": exit_px, "qty": p["qty"],
        "opened_at": p["opened_at"], "closed_at": bar["t"] + BAR_MS,
        "pnl": gross - fee - p["entry"] * p["qty"] * FEE,
        "r": (gross - fee - p["entry"] * p["qty"] * FEE) / p["risk_usd"], "strategy": p.get("version", VERSION),
    }
    del acct["positions"][coin]
    return trade


def mark_equity(acct, prices):
    unreal = sum(p["side"] * (prices[c] - p["entry"]) * p["qty"] for c, p in acct["positions"].items() if c in prices)
    acct["equity"] = acct["cash"] + unreal
    return unreal


def new_account():
    return {"cash": START_EQUITY, "equity": START_EQUITY, "positions": {}, "trade_no": 0, "last_bar": {}}


# ───── Backtest ─────
def stats(trades, curve):
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    wins = [t for t in trades if t["pnl"] > 0]
    gw, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    return {
        "net_pct": (curve[-1] / curve[0] - 1) * 100 if curve else 0, "max_dd_pct": mdd * 100,
        "trades": len(trades), "win_pct": 100 * len(wins) / len(trades) if trades else 0,
        "pf": gw / gl if gl else float("inf") if gw else 0,
    }


def backtest(start="2024-01-01", split="2025-07-01"):
    start_ms = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    split_ms = int(datetime.fromisoformat(split).replace(tzinfo=timezone.utc).timestamp() * 1000)
    warm = start_ms - (EMA_LEN + 50) * BAR_MS
    data = {}
    for c in DEFAULT_COINS:
        bars = fetch_klines(c + QUOTE, warm)
        sig, a = signals(bars)
        data[c] = {"bars": bars, "sig": sig, "atr": a, "idx": {b["t"]: i for i, b in enumerate(bars)}}
        print(f"{c}: {len(bars)} Kerzen geladen")
    COINS = DEFAULT_COINS
    times = sorted({b["t"] for c in COINS for b in data[c]["bars"] if b["t"] >= start_ms})
    for label, lo, hi in (("Lern-Zeitraum", start_ms, split_ms), ("Prüf-Zeitraum", split_ms, 10**15)):
        acct, trades, curve = new_account(), [], [START_EQUITY]
        per_coin = {c: [] for c in COINS}
        for t in (x for x in times if lo <= x < hi):
            prices = {}
            for c in COINS:
                i = data[c]["idx"].get(t)
                if i is None:
                    continue
                bar = data[c]["bars"][i]
                prices[c] = bar["c"]
                tr = check_exit(acct, c, bar)
                if tr:
                    trades.append(tr)
                    per_coin[c].append(tr)
            mark_equity(acct, prices)
            for c in COINS:
                i = data[c]["idx"].get(t)
                if i is not None and data[c]["sig"][i]:
                    bar = data[c]["bars"][i]
                    open_position(acct, c, data[c]["sig"][i], bar["c"], data[c]["atr"][i], t + BAR_MS)
            curve.append(acct["equity"])
        s = stats(trades, curve)
        print(f"\n{label}: Netto {s['net_pct']:+.1f} % | Max-Drawdown {s['max_dd_pct']:.1f} % | "
              f"{s['trades']} Trades | Treffer {s['win_pct']:.0f} % | PF {s['pf']:.2f}")
        for c in COINS:
            ts = per_coin[c]
            pnl = sum(t["pnl"] for t in ts)
            print(f"  {c}: {len(ts):3d} Trades, Ergebnis {'+' if pnl >= 0 else ''}{de(pnl)} $")


# ───── Selbst-Lernen (wöchentlich, Walk-Forward) ─────
GRID = [{"rsi_lo": lo, "rsi_hi": 100 - lo, "sl_atr": sl, "rr": rr, "adx_min": ax}
        for lo in (30, 35, 40) for sl in (1.0, 1.5, 2.0) for rr in (1.5, 2.0, 3.0) for ax in (0, 20, 25)]


def sim_r(bars, sig, a, p, lo, hi):
    """Trades als R-Vielfache (inkl. Gebühren) zwischen Kerzen-Index lo und hi."""
    out, i = [], max(lo, 1)
    while i < hi:
        if sig[i]:
            s, entry, dist = sig[i], bars[i]["c"], p["sl_atr"] * a[i]
            stop, tgt, fee_r = entry - s * dist, entry + s * p["rr"] * dist, 0.002 * entry / dist
            j = i + 1
            while j < hi:
                b = bars[j]
                if (b["l"] <= stop) if s == 1 else (b["h"] >= stop):
                    out.append(-1 - fee_r)
                    break
                if (b["h"] >= tgt) if s == 1 else (b["l"] <= tgt):
                    out.append(p["rr"] - fee_r)
                    break
                j += 1
            i = j + 1
        else:
            i += 1
    return out


def r_stats(rs):
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    gw, gl = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    return {"r": eq, "dd": dd, "n": len(rs), "pf": gw / gl if gl else (9.9 if gw else 0.0),
            "score": eq / max(dd, 3.0)}


def learn(days=420, test_frac=0.3):
    """Testet alle Varianten: wählt auf dem Lern-Teil die Top 5, entscheidet auf dem Prüf-Teil.
    Neue Einstellungen ersetzen die alten nur, wenn sie im Prüf-Teil klar besser sind."""
    start = int(time.time() * 1000) - (days + 40) * 86400 * 1000
    data = {}
    for c in UNIVERSE:
        try:
            bars = fetch_klines(c + QUOTE, start)
        except Exception as e:  # noqa: BLE001
            print(f"Lernen: {c} übersprungen ({e})")
            continue
        if len(bars) < 600:
            continue
        cut = int(len(bars) * (1 - test_frac))
        cache = {"c": [b["c"] for b in bars]}
        cache.update({"e": ema(cache["c"], EMA_LEN), "r": rsi(cache["c"], RSI_LEN), "a": atr(bars, ATR_LEN), "x": adx(bars)})
        data[c] = (bars, cut, cache)
    if not data:
        return None

    def evaluate(p, part):
        per = {}
        for c, (bars, cut, cache) in data.items():
            sig, a = signals(bars, p, cache)
            lo, hi = (EMA_LEN + 20, cut) if part == "train" else (cut, len(bars))
            per[c] = r_stats(sim_r(bars, sig, a, p, lo, hi))
        return per

    def total(per, coins):
        rs = [per[c] for c in coins if c in per]
        r = sum(x["r"] for x in rs)
        n = sum(x["n"] for x in rs)
        dd = max([x["dd"] for x in rs] or [0]) + 0.5 * sum(x["dd"] for x in rs)
        return {"r": r, "n": n, "score": r / max(dd, 3.0)}

    ranked = []
    for g in GRID:
        per = evaluate(g, "train")
        good = [c for c, x in per.items() if x["n"] >= 6 and x["pf"] >= 1.1]
        t = total(per, good)
        if len(good) >= 2 and t["n"] >= 25:
            ranked.append((t["score"], g, good))
    ranked.sort(key=lambda x: -x[0])
    best, best_t = None, None
    for _, g, good in ranked[:5]:
        tp = evaluate(g, "test")
        coins = [c for c in good if tp[c]["n"] >= 2 and tp[c]["r"] > 0] or good
        t = total(tp, coins)
        if t["n"] >= 10 and (best_t is None or t["score"] > best_t["score"]):
            best, best_t = dict(g, coins=coins[:6]), t
    cur = load("params.json", None) or DEFAULT_PARAMS
    cur_t = total(evaluate(cur, "test"), cur["coins"])
    log = load("learn_log.json", [])
    entry = {"at": iso(int(time.time() * 1000)), "current": {k: cur[k] for k in ("rsi_lo", "sl_atr", "rr", "adx_min", "coins")},
             "current_test": cur_t, "candidate": best, "candidate_test": best_t, "adopted": False}
    if best and best_t["r"] > 0 and best_t["score"] > cur_t["score"] * 1.15 + 0.1:
        v = 4 + sum(1 for x in log if x.get("adopted"))
        best.update({"version": f"TrendRSI v{v} (auto)", "since": entry["at"]})
        save("params.json", best)
        entry["adopted"] = True
        print(f"Gelernt: neue Version {best['version']} – Prüf-Teil {best_t['r']:+.1f} R statt {cur_t['r']:+.1f} R")
    else:
        if not load("params.json", None):
            save("params.json", dict(DEFAULT_PARAMS, since=entry["at"]))
        print(f"Gelernt: aktuelle Version bleibt (Prüf-Teil {cur_t['r']:+.1f} R)")
    log.append(entry)
    save("learn_log.json", log[-52:])
    return entry


# ───── Live-Durchlauf ─────
def load(name, default):
    try:
        with open(os.path.join(STATE_DIR, name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(name, obj):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = os.path.join(STATE_DIR, name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, os.path.join(STATE_DIR, name))


def de(x, d=0):
    """Zahl im deutschen Format, z. B. 100.000,00"""
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run():
    now_dt = datetime.now(timezone.utc)
    params = load("params.json", None)
    if params is None or (now_dt.weekday() == 6 and now_dt.hour < 4):   # sonntags beim ersten Lauf lernen
        last = (load("learn_log.json", [{}]) or [{}])[-1].get("at", "")
        if not last.startswith(now_dt.strftime("%Y-%m-%d")):
            try:
                learn()
            except Exception as e:  # noqa: BLE001
                print(f"Lernen fehlgeschlagen: {e}")
        params = load("params.json", None) or DEFAULT_PARAMS
    acct = load("account.json", None) or new_account()
    trades = load("trades.json", [{"id": "0", "kind": "Konto", "at": iso(int(time.time() * 1000)),
                                   "note": "Simuliertes Konto Claude Bot (Python) mit 100.000 $ eröffnet"}])
    equity = load("equity.json", [])
    markets, prices, log, candles = [], {}, [], []
    coins = list(dict.fromkeys(params.get("coins", DEFAULT_COINS) + [c for c in acct["positions"]]))
    for c in coins:
        try:
            bars, source = fetch_live(c)          # letzte geschlossene 4h-Kerzen
        except Exception as e:  # noqa: BLE001
            print(f"{c}: keine Kurse ({e})")
            continue
        sig, a = signals(bars, params)
        candles += chart_rows(c, bars, p=params)
        last_t = acct["last_bar"].get(c)
        new = [i for i, b in enumerate(bars) if last_t is None or b["t"] > last_t]
        if last_t is None:
            new = new[-1:]                        # erster Lauf: nur die letzte Kerze
        for i in new:
            b = bars[i]
            tr = check_exit(acct, c, b)
            if tr:
                tr["closed_at"] = iso(tr["closed_at"])
                trades.append(tr)
                log.append(f"{c}: {tr['side']} geschlossen, {tr['pnl']:+.0f} $ ({tr['r']:+.2f} R)")
            if sig[i] and i == len(bars) - 1:     # nur frische Signale handeln
                p = open_position(acct, c, sig[i], b["c"], a[i], iso(b["t"] + BAR_MS), params)
                if p:
                    log.append(f"{c}: {'Long' if sig[i] == 1 else 'Short'} eröffnet @ {p['entry']:.2f}, "
                               f"Stop {p['stop']:.2f}, Ziel {p['target']:.2f}")
        acct["last_bar"][c] = bars[-1]["t"]
        prices[c] = bars[-1]["c"]
        s = sig[-1]
        markets.append({"coin": c, "symbol": f"BINANCE:{c}{QUOTE}" if source == "Binance" else f"KRAKEN:{c}USD", "price": prices[c],
                        "signal": "Long-Signal" if s == 1 else "Short-Signal" if s == -1 else "Kein Signal"})
    unreal = mark_equity(acct, prices)
    now = iso(int(time.time() * 1000))
    for m in markets:
        p = acct["positions"].get(m["coin"])
        m.update({"position": ("Long" if p["side"] == 1 else "Short") if p else "Flat",
                  "qty": p["qty"] if p else None, "entry": p["entry"] if p else None,
                  "stop": p["stop"] if p else None, "target": p["target"] if p else None,
                  "unrealized": p["side"] * (prices[m["coin"]] - p["entry"]) * p["qty"] if p else 0,
                  "checked_at": now})
    realized = sum(t.get("pnl", 0) for t in trades if t.get("kind") == "Trade")
    equity.append({"at": now, "equity": round(acct["equity"], 2), "realized": round(realized, 2),
                   "unrealized": round(unreal, 2), "open_positions": len(acct["positions"]),
                   "note": "; ".join(log) or "kein neues Signal"})
    save("account.json", acct)
    save("trades.json", trades)
    save("equity.json", equity)
    save("markets.json", markets)
    save("candles.json", candles)
    print(f"{now}  Equity {de(acct['equity'])} $  " + (" | ".join(log) or "kein neues Signal"))


def status():
    acct = load("account.json", None)
    if not acct:
        print("Noch kein Lauf. Starte mit: python3 bot.py run")
        return
    print(f"Equity: {de(acct['equity'], 2)} $")
    for c, p in acct["positions"].items():
        print(f"  {c} {'Long' if p['side'] == 1 else 'Short'} {p['qty']:.4f} @ {p['entry']:.2f}  "
              f"Stop {p['stop']:.2f}  Ziel {p['target']:.2f}")
    for t in [t for t in load("trades.json", []) if t.get("kind") == "Trade"][-5:]:
        print(f"  #{t['id']} {t['coin']} {t['side']}: {t['pnl']:+.0f} $ ({t['r']:+.2f} R)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"backtest": backtest, "run": run, "status": status, "learn": learn}.get(cmd, status)()
