#!/usr/bin/env python3
"""Coin Compass forecast engine.

Runs hourly in GitHub Actions. For every base coin it estimates, for 1h, 3h, 6h,
12h, 24h and 7d ahead:
  * the probability that price will be higher than now, and
  * a likely range (50% band) and a wide range (90% band).

How it works, in short:
  1. Pull hourly (and daily) candles from Binance public data.
  2. Turn each moment in history into ~12 numbers: momentum over several windows,
     recent volatility vs normal, RSI, distance from the weekly average, what Bitcoin
     is doing, and Fear & Greed. Everything is scaled by the coin's own volatility
     so one model can learn from all coins at once.
  3. Fit a regularised logistic regression per horizon: "given these numbers, how
     often was the price higher afterwards?" It is tested on the most recent 25% of
     history that it never saw, and those honest results are published.
  4. Range width comes from volatility, calibrated on history, and is checked the
     same way (how often did reality land inside the band?).
  5. Every forecast is written to a ledger and scored when its time arrives, so
     you get a live, out-of-sample track record.

Information only. Not financial advice.
"""
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

import numpy as np

CG = "https://api.coingecko.com/api/v3"
BN = "https://data-api.binance.vision/api/v3"
HERE = os.path.dirname(os.path.abspath(__file__))

HOURLY_N = 3000
DAILY_N = 1000
HORIZONS = [("1h", 1, "h"), ("3h", 3, "h"), ("6h", 6, "h"), ("12h", 12, "h"), ("24h", 24, "h"), ("7d", 7, "d")]
QS = [0.05, 0.25, 0.5, 0.75, 0.95]
CONF_EDGE = 0.03  # a "call" is a probability at least 3 points away from 50%

HOURLY_CFG = dict(steps=(1, 3, 6, 12, 24, 72, 168), lam=0.97, long_n=168, short_n=24, rsi_n=14, dist_n=168, mkt_step=24)
DAILY_CFG = dict(steps=(1, 3, 7, 14, 30, 60), lam=0.94, long_n=30, short_n=7, rsi_n=14, dist_n=50, mkt_step=7)


# ---------------------------------------------------------------- network
def get(url, tries=3):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "coin-compass/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2 + 4 * i)
    raise RuntimeError(f"GET failed: {url} ({err})")


def klines(sym, interval, total):
    out, end = [], None
    while len(out) < total:
        lim = min(1000, total - len(out))
        url = f"{BN}/klines?symbol={sym}USDT&interval={interval}&limit={lim}" + (f"&endTime={end}" if end else "")
        j = get(url, tries=2)
        if not j:
            break
        out = j + out
        end = j[0][0] - 1
        if len(j) < lim:
            break
        time.sleep(0.15)
    return out


def default_hourly(sym, cid, now_ts):
    """Return (timestamps of candle opens, closes) for completed hourly candles."""
    try:
        k = [x for x in klines(sym, "1h", HOURLY_N) if x[6] / 1000 <= now_ts]
        if len(k) >= 800:
            return np.array([x[0] // 1000 for x in k]), np.array([float(x[4]) for x in k])
    except Exception as e:  # noqa: BLE001
        print(f"binance hourly failed for {sym}: {e}")
    time.sleep(8)
    j = get(f"{CG}/coins/{cid}/market_chart?vs_currency=usd&days=90")
    bucket = {}
    for t, p in j.get("prices", []):
        bucket[int(t // 1000) // 3600 * 3600] = p
    cur = int(now_ts) // 3600 * 3600
    keys = sorted(k for k in bucket if k < cur)
    return np.array(keys), np.array([bucket[k] for k in keys])


def default_daily(sym, cid, now_ts):
    try:
        k = [x for x in klines(sym, "1d", DAILY_N) if x[6] / 1000 <= now_ts]
        if len(k) >= 200:
            return np.array([x[0] // 1000 for x in k]), np.array([float(x[4]) for x in k])
    except Exception as e:  # noqa: BLE001
        print(f"binance daily failed for {sym}: {e}")
    time.sleep(8)
    j = get(f"{CG}/coins/{cid}/market_chart?vs_currency=usd&days=365&interval=daily")
    cur = int(now_ts) // 86400 * 86400
    pts = {int(t // 1000) // 86400 * 86400: p for t, p in j.get("prices", [])}
    keys = sorted(k for k in pts if k < cur)
    return np.array(keys), np.array([pts[k] for k in keys])


def default_fng():
    j = get("https://api.alternative.me/fng/?limit=0")
    return {int(d["timestamp"]) // 86400 * 86400: float(d["value"]) for d in j["data"]}


def default_markets(ids):
    return get(f"{CG}/coins/markets?vs_currency=usd&ids={','.join(ids)}")


# ---------------------------------------------------------------- features
def roll_mean(a, n):
    out = np.full(len(a), np.nan)
    if len(a) < n:
        return out
    cs = np.cumsum(np.insert(a, 0, 0.0))
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def roll_std(a, n):
    m, m2 = roll_mean(a, n), roll_mean(a * a, n)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


def ewma_var(r, lam):
    v = np.zeros(len(r))
    v[0] = np.var(r[1:200]) if len(r) > 5 else 1e-6
    for i in range(1, len(r)):
        v[i] = lam * v[i - 1] + (1 - lam) * r[i] * r[i]
    return v


def vol_estimate(lp, cfg):
    r = np.diff(lp, prepend=lp[0])
    ew = np.sqrt(ewma_var(r, cfg["lam"]))
    sd_l = roll_std(r, cfg["long_n"])
    return r, 0.5 * ew + 0.5 * sd_l, sd_l


def z_col(lp, k, sig):
    rk = np.full(len(lp), np.nan)
    rk[k:] = lp[k:] - lp[:-k]
    return np.clip(rk / (sig * math.sqrt(k)), -4, 4)


def make_features(lp, mkt_z, fng, cfg):
    """One row of 12 numbers per time step. Row i only uses data up to i."""
    r, sig, sd_l = vol_estimate(lp, cfg)
    sd_s = roll_std(r, cfg["short_n"])
    cols = [z_col(lp, k, sig) for k in cfg["steps"]]
    vr = np.clip(np.log(np.maximum(sd_s, 1e-12) / np.maximum(sd_l, 1e-12)), -2, 2)
    ag, al = roll_mean(np.maximum(r, 0), cfg["rsi_n"]), roll_mean(np.maximum(-r, 0), cfg["rsi_n"])
    rsi = 100 - 100 / (1 + ag / np.where(al == 0, 1e-12, al))
    dist = np.clip((lp - roll_mean(lp, cfg["dist_n"])) / (sig * math.sqrt(cfg["dist_n"])), -4, 4)
    return np.column_stack(cols + [vr, (rsi - 50) / 50, dist, mkt_z, fng]), sig


def mkt_factor(lp_btc, cfg):
    _, sig, _ = vol_estimate(lp_btc, cfg)
    return z_col(lp_btc, cfg["mkt_step"], sig)


# ---------------------------------------------------------------- model
def sigmoid(z):
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def fit_logit(X, y, l2=100.0, iters=30):
    n, d = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])
    w = np.zeros(d + 1)
    reg = np.eye(d + 1) * l2
    reg[0, 0] = 0
    for _ in range(iters):
        p = sigmoid(Xb @ w)
        g = Xb.T @ (p - y) + reg @ w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + reg
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-7:
            break
    return w


def predict(w, X):
    return sigmoid(np.hstack([np.ones((len(X), 1)), X]) @ w)


def build_xyz(F_list, sig_list, lp_list, h, N, fng_col_ok=True):
    """Stack usable rows from all coins for horizon h (in steps of this timeframe)."""
    Xs, ys, zs, ts = [], [], [], []
    for F, sig, lp in zip(F_list, sig_list, lp_list):
        T = np.arange(N - h)
        fwd = lp[T + h] - lp[T]
        ok = ~np.isnan(F[T]).any(axis=1) & ~np.isnan(sig[T]) & (sig[T] > 0)
        Xs.append(F[T][ok])
        ys.append((fwd[ok] > 0).astype(float))
        zs.append(fwd[ok] / (sig[T][ok] * math.sqrt(h)))
        ts.append(T[ok])
    return np.vstack(Xs), np.concatenate(ys), np.concatenate(zs), np.concatenate(ts)


def train_horizon(F_list, sig_list, lp_list, h, N, hours=None):
    X, y, z, t = build_xyz(F_list, sig_list, lp_list, h, N)
    split = int(0.75 * N)
    tr = t + h < split
    te = t >= split
    if tr.sum() < 800 or te.sum() < 200:
        return None
    mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
    l2 = 100.0 * math.sqrt(hours or h)  # longer horizons overlap more, so shrink harder
    w = fit_logit((X[tr] - mu) / sd, y[tr], l2=l2)
    p = predict(w, (X[te] - mu) / sd)
    base = y[tr].mean()
    qz = np.quantile(z[tr], QS)
    conf = np.abs(p - 0.5) >= CONF_EDGE
    metrics = {
        "n_test": int(te.sum()),
        "acc": float(np.mean((p > 0.5) == (y[te] > 0.5))),
        "acc_base": float(np.mean((y[te] > 0.5) == (base >= 0.5))),
        "brier": float(np.mean((p - y[te]) ** 2)),
        "brier_base": float(np.mean((base - y[te]) ** 2)),
        "conf_n": int(conf.sum()),
        "conf_acc": float(np.mean((p[conf] > 0.5) == (y[te][conf] > 0.5))) if conf.any() else None,
        "cov50": float(np.mean((z[te] >= qz[1]) & (z[te] <= qz[3]))),
        "cov90": float(np.mean((z[te] >= qz[0]) & (z[te] <= qz[4]))),
    }
    # final model on everything usable
    mu, sd = X.mean(0), X.std(0) + 1e-9
    w = fit_logit((X - mu) / sd, y, l2=l2)
    return {"w": w, "mu": mu, "sd": sd, "qz": np.quantile(z, QS), "metrics": metrics}


# ---------------------------------------------------------------- helpers
def load(name, default):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def align(series, grid):
    """Put each (ts, closes) on a common grid, forward-filling small gaps."""
    out = {}
    for cid, (ts, c) in series.items():
        d = dict(zip(ts.tolist(), c.tolist()))
        a = np.array([d.get(t, np.nan) for t in grid.tolist()])
        if np.isnan(a).mean() > 0.02 or np.isnan(a[-50:]).any():
            print(f"skipping {cid}: too many gaps ({np.isnan(a).mean():.1%})")
            continue
        for i in range(1, len(a)):
            if np.isnan(a[i]):
                a[i] = a[i - 1]
        if np.isnan(a[0]):
            first = a[~np.isnan(a)][0]
            a[np.isnan(a)] = first
        out[cid] = a
    return out


def fng_array(ts, fng):
    if not fng:
        return np.zeros(len(ts))
    days = sorted(fng)
    vals = np.zeros(len(ts))
    last = fng[days[0]]
    j = 0
    for i, t in enumerate(ts.tolist()):
        d = t // 86400 * 86400
        if d in fng:
            last = fng[d]
        vals[i] = (last - 50) / 50
    return vals


# ---------------------------------------------------------------- noise, ranges, alerts
def noise_stats(lp, sig, k=6, thr=2.0):
    """How stretched is the last k-hour move, and what happened after similar stretches?"""
    n = len(lp)
    z = np.full(n, np.nan)
    z[k:] = (lp[k:] - lp[:-k]) / (sig[k:] * math.sqrt(k))
    ev = rev = 0
    tot = 0.0
    last = -10 ** 9
    for t in range(max(60, k), n - k):
        zt = z[t]
        if np.isnan(zt) or abs(zt) < thr or t - last < k:  # one event per stretch
            continue
        last = t
        ev += 1
        giveback = -np.sign(zt) * (lp[t + k] - lp[t])
        rev += int(giveback > 0)
        tot += giveback
    return {"z": float(z[-1]), "n": ev, "rate": (rev / ev) if ev else None, "avg": (tot / ev) if ev else None}


def vol_regime(lp):
    r, sig, sd_l = vol_estimate(lp, HOURLY_CFG)
    sd_s = roll_std(r, HOURLY_CFG["short_n"])
    return {
        "ratio": float(sd_s[-1] / max(sd_l[-1], 1e-12)),
        "move24": float(sig[-1] * math.sqrt(24)),
        "norm24": float(sd_l[-1] * math.sqrt(24)),
    }


def load_at(folder, name, default):
    try:
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def send(topic, title, body, priority="default", tags="", click=""):
    print(f"[notify] {title} | {body}")
    if not topic:
        return
    h = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority, "Tags": tags}
    if click:
        h["Click"] = click
    req = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(topic), data=body.encode("utf-8"), headers=h, method="POST")
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        print("ntfy error:", e)


LEAN_HORIZONS = ["3h", "6h", "12h", "24h", "7d"]


def alert_pass(result, cfg, folder, topic="", click="", now_ts=None, send_fn=send):
    """Phone alerts: strong lean, ranges widening sharply, stretched (noisy) moves."""
    now_ts = now_ts or time.time()
    state = load_at(folder, "fstate.json", {})
    first = not state.get("init")
    lean_state, wide_state, noise_state = state.get("lean", {}), state.get("wide", {}), state.get("noise", {})
    strong = float(cfg.get("strong_lean", 0.06))
    wide_ratio = float(cfg.get("wide_ratio", 1.6))
    need_edge = bool(cfg.get("lean_requires_edge", True))
    noise_on = bool(cfg.get("noise_alerts", True))
    model = result.get("model", {})
    msgs = []
    for cid, c in result["coins"].items():
        sym = c["symbol"]
        new = []
        for k in LEAN_HORIZONS:
            f = c["h"].get(k)
            if not f:
                continue
            M = model.get(k)
            ok = (not need_edge) or (M and M["acc"] - M["acc_base"] >= 0.01 and M["brier"] < M["brier_base"])
            d = f["p"] - 0.5
            direction = (1 if d > 0 else -1) if (abs(d) >= strong and ok) else 0
            key = f"{cid}:{k}"
            if direction != 0 and lean_state.get(key, 0) != direction:
                new.append((k, f, M))
            lean_state[key] = direction
        if new:
            up = new[0][1]["p"] > 0.5
            lines = ", ".join(f"{k} {round(f['p'] * 100)}% chance higher" for k, f, _ in new)
            k0, f0, M0 = new[0]
            q = f0["q"]
            body = (f"{sym}: {lines}. Likely {k0} range {q[1] * 100:+.1f}% to {q[3] * 100:+.1f}%. "
                    f"On unseen history this horizon beat a coin flip by {(M0['acc'] - M0['acc_base']) * 100:.1f} pts. "
                    "Information only, not advice.")
            msgs.append((f"{sym}: strong lean {'up' if up else 'down'}", body, "default",
                         "chart_with_upwards_trend" if up else "chart_with_downwards_trend"))
        v = c.get("vol")
        if v:
            if v["ratio"] >= wide_ratio and not wide_state.get(cid):
                wide_state[cid] = True
                msgs.append((f"{sym}: ranges widening sharply",
                             f"{sym} is swinging {v['ratio']:.1f}x harder than normal. Typical 24h move is now about "
                             f"{v['move24'] * 100:.1f}% (normally {v['norm24'] * 100:.1f}%). Expect bigger moves both ways.",
                             "high", "warning"))
            elif v["ratio"] < 1.3:
                wide_state[cid] = False
        nz = c.get("noise")
        if noise_on and nz and abs(nz["z"]) >= 2.5 and nz["n"] >= 8 and (nz["rate"] or 0) >= 0.55 and (nz["avg"] or 0) > 0 \
                and now_ts - noise_state.get(cid, 0) > 12 * 3600:
            noise_state[cid] = now_ts
            msgs.append((f"{sym}: stretched move ({nz['z']:+.1f} sigma in 6h)",
                         f"After similar stretches {sym} gave part of the move back {round(nz['rate'] * 100)}% of the time "
                         f"({nz['n']} cases, average giveback {nz['avg'] * 100:.2f}%). Round-trip fees are about 0.2%, "
                         "so the edge is thin. Information only.", "low", "microscope"))
    state.update({"init": True, "lean": lean_state, "wide": wide_state, "noise": noise_state})
    with open(os.path.join(folder, "fstate.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh, separators=(",", ":"), sort_keys=True)
    if first:
        print(f"first run: recorded starting state, suppressed {len(msgs)} alert(s)")
        return []
    for title, body, prio, tags in msgs[:6]:
        send_fn(topic, title, body, prio, tags, click)
    if len(msgs) > 6:
        send_fn(topic, "More forecast alerts waiting", f"{len(msgs) - 6} more. Open the Forecast tab.", "low", "inbox_tray", click)
    return msgs


def run(fetch_hourly=None, fetch_daily=None, fetch_fng=None, fetch_markets=None, now=None, out_dir=None, notify=True, send_fn=None):
    out_dir = out_dir or HERE
    cfg = load("config.json", {})
    base = list(cfg.get("base") or [])
    ids = base + (["bitcoin"] if "bitcoin" not in base else [])
    now = now or dt.datetime.now(dt.timezone.utc)
    now_ts = now.timestamp()
    fetch_hourly = fetch_hourly or default_hourly
    fetch_daily = fetch_daily or default_daily
    fetch_fng = fetch_fng or default_fng
    fetch_markets = fetch_markets or default_markets

    markets = {m["id"]: m for m in fetch_markets(ids)}
    try:
        fng = fetch_fng()
    except Exception as e:  # noqa: BLE001
        print("fng failed:", e)
        fng = {}

    hourly, daily = {}, {}
    for cid in ids:
        m = markets.get(cid)
        if not m:
            continue
        sym = m["symbol"].upper()
        try:
            hourly[cid] = fetch_hourly(sym, cid, now_ts)
            daily[cid] = fetch_daily(sym, cid, now_ts)
        except Exception as e:  # noqa: BLE001
            print(f"data failed for {sym}: {e}")
        time.sleep(0.2)
    if "bitcoin" not in hourly or "bitcoin" not in daily:
        raise RuntimeError("Bitcoin data is required as the market factor")

    # ----- hourly grid
    gh = hourly["bitcoin"][0]
    Hc = align(hourly, gh)
    Nh = len(gh)
    lp_btc_h = np.log(Hc["bitcoin"])
    mkt_h = mkt_factor(lp_btc_h, HOURLY_CFG)
    fng_h = fng_array(gh, fng)

    # ----- daily grid (+ today's live point)
    gd = daily["bitcoin"][0]
    today = int(now_ts) // 86400 * 86400
    gd_live = np.append(gd, today)
    Dc = {}
    for cid, (ts, c) in daily.items():
        m = markets.get(cid)
        live = m["current_price"] if m else c[-1]
        Dc[cid] = (np.append(ts, today), np.append(c, live))
    Dc = align(Dc, gd_live)
    Nd = len(gd_live)
    lp_btc_d = np.log(Dc["bitcoin"])
    mkt_d = mkt_factor(lp_btc_d, DAILY_CFG)
    fng_d = fng_array(gd_live, fng)

    # ----- features per coin
    coins = [c for c in base if c in Hc and c in Dc]
    pool = coins if "bitcoin" in coins else coins + ["bitcoin"]
    FH, SH, LH, FD, SD, LD = {}, {}, {}, {}, {}, {}
    for cid in pool:
        lp = np.log(Hc[cid])
        FH[cid], SH[cid] = make_features(lp, mkt_h, fng_h, HOURLY_CFG)
        LH[cid] = lp
        lpd = np.log(Dc[cid])
        FD[cid], SD[cid] = make_features(lpd, mkt_d, fng_d, DAILY_CFG)
        LD[cid] = lpd

    models = {}
    for key, h, unit in HORIZONS:
        if unit == "h":
            M = train_horizon([FH[c] for c in pool], [SH[c] for c in pool], [LH[c] for c in pool], h, Nh, hours=h)
        else:
            M = train_horizon([FD[c] for c in pool], [SD[c] for c in pool], [LD[c] for c in pool], h, Nd, hours=h * 24)
        models[key] = M
        if M:
            mt = M["metrics"]
            print(f"{key}: acc {mt['acc']:.3f} (base {mt['acc_base']:.3f}) brier {mt['brier']:.4f} (base {mt['brier_base']:.4f}) "
                  f"cov50 {mt['cov50']:.2f} cov90 {mt['cov90']:.2f} n={mt['n_test']}")

    # ----- live forecasts
    T0 = int(gh[-1]) + 3600  # boundary time of the last completed hourly candle
    out_coins = {}
    for cid in coins:
        m = markets.get(cid, {})
        price = float(Hc[cid][-1])
        entry = {"symbol": m.get("symbol", cid).upper(), "price": round(price, 8 if price < 1 else 4), "h": {}}
        for key, h, unit in HORIZONS:
            M = models.get(key)
            if not M:
                continue
            if unit == "h":
                x, sig = FH[cid][-1], SH[cid][-1]
            else:
                x, sig = FD[cid][-1], SD[cid][-1]
            if np.isnan(x).any() or np.isnan(sig):
                continue
            p = float(predict(M["w"], ((x - M["mu"]) / M["sd"])[None, :])[0])
            q = np.exp(M["qz"] * sig * math.sqrt(h)) - 1
            entry["h"][key] = {"p": round(p, 4), "q": [round(float(v), 5) for v in q]}
        try:
            entry["vol"] = {k: round(v, 5) for k, v in vol_regime(LH[cid]).items()}
            nz = noise_stats(LH[cid], SH[cid])
            entry["noise"] = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in nz.items()}
        except Exception as e:  # noqa: BLE001
            print(f"vol/noise failed for {cid}: {e}")
        out_coins[cid] = entry

    # ----- ledger: score matured forecasts, then record new ones
    ledger = load("ledger.json", {"open": [], "stats": {}, "since": now.strftime("%Y-%m-%d")})
    price_at = {cid: dict(zip((gh + 3600).tolist(), Hc[cid].tolist())) for cid in coins}
    stats = ledger.setdefault("stats", {})
    still_open = []
    for e in ledger.get("open", []):
        hours = e["hrs"]
        T = e["T0"] + hours * 3600
        if T > T0:
            if T0 - e["T0"] < 9 * 86400:
                still_open.append(e)
            continue
        actual = price_at.get(e["id"], {}).get(T)
        if actual is None:
            continue
        s = stats.setdefault(e["k"], {"n": 0, "hits": 0, "brier": 0.0, "conf_n": 0, "conf_hits": 0, "in50": 0, "in90": 0})
        up = 1.0 if actual > e["p0"] else 0.0
        s["n"] += 1
        s["hits"] += int((e["p"] > 0.5) == (up > 0.5))
        s["brier"] += (e["p"] - up) ** 2
        if abs(e["p"] - 0.5) >= CONF_EDGE:
            s["conf_n"] += 1
            s["conf_hits"] += int((e["p"] > 0.5) == (up > 0.5))
        r = actual / e["p0"] - 1
        s["in50"] += int(e["b"][1] <= r <= e["b"][3])
        s["in90"] += int(e["b"][0] <= r <= e["b"][4])
    have = {(e["id"], e["k"], e["T0"]) for e in still_open}
    for cid, entry in out_coins.items():
        for key, h, unit in HORIZONS:
            f = entry["h"].get(key)
            if not f:
                continue
            hrs = h if unit == "h" else h * 24
            if unit == "d" and (T0 // 3600) % 24 != 0:
                continue  # one 7-day forecast per day keeps the ledger small
            if (cid, key, T0) in have:
                continue
            still_open.append({"id": cid, "k": key, "hrs": hrs, "T0": T0, "p0": entry["price"], "p": f["p"], "b": f["q"]})
    ledger["open"] = still_open
    with open(os.path.join(out_dir, "ledger.json"), "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, separators=(",", ":"))

    result = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "T0": T0,
        "fng": None if not fng else fng[max(fng)],
        "coins": out_coins,
        "model": {k: ({kk: (round(v, 4) if isinstance(v, float) else v) for kk, v in M["metrics"].items()} if M else None) for k, M in models.items()},
        "live": {k: {kk: (round(v, 4) if isinstance(v, float) else v) for kk, v in s.items()} for k, s in stats.items()},
        "since": ledger.get("since"),
    }
    with open(os.path.join(out_dir, "forecasts.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, separators=(",", ":"))
    print(f"done: {len(out_coins)} coins, {len(still_open)} open forecasts")
    if notify:
        try:
            alert_pass(result, cfg, out_dir, os.environ.get("NTFY_TOPIC", ""), os.environ.get("APP_URL", ""), now_ts, send_fn or send)
        except Exception as e:  # noqa: BLE001
            print("alert pass failed:", e)
    return result


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # noqa: BLE001
        print("forecast failed:", e)
        sys.exit(1)
