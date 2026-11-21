#!/usr/bin/env python3
"""Coin Compass paper-trading league.

Pretend money only. Four simple rule sets trade the same coins with the same starting
cash, and a buy-and-hold benchmark sits beside them, so you can see over weeks whether
any rule actually beats just holding. Fees (0.1% per side) and a small slippage cost
(0.05%) are charged on every trade, as on a real exchange.

Called once an hour by forecast.py. State lives in paper.json (also read by the app).
Information only. Not financial advice.
"""
import datetime as dt
import json
import os

import alert  # reuses the exact same Signal rules as the app

FEE = 0.001
SLIP = 0.0005
CAPITAL = 100.0  # pretend US dollars, split equally across the coins
COOL = 24 * 3600  # no re-entry for 24h after a stop-loss
STRATS = [
    ("dip", "Dip buyer"),
    ("trend", "Trend follower"),
    ("signal", "Signal follower"),
    ("hodl", "Buy and hold"),
]


def _sma(c, n):
    s = c[-n:]
    return sum(s) / len(s)


def decide(k, c, price, pos, now, fng):
    """Return (action, reason). action is 'buy', 'sell' or None."""
    holding = pos["qty"] > 0
    cooling = now < pos.get("cool", 0)
    if k == "dip":
        r = alert.rsi(c)
        if holding:
            if price <= pos["entry"] * 0.85:
                return "sell", "Stop-loss (down 15%)"
            if r is not None and r > 55:
                return "sell", "RSI recovered above 55"
        elif r is not None and r < 30 and not cooling:
            return "buy", "RSI under 30 (oversold)"
    elif k == "trend":
        if len(c) < 200:
            return None, ""
        m = _sma(c, 200)
        if holding and price < m * 0.98:
            return "sell", "Fell below the 200-day average"
        if not holding and price > m * 1.02 and not cooling:
            return "buy", "Rose above the 200-day average"
    elif k == "signal":
        sig = alert.compute_signal(c, fng)
        if sig is None:
            return None, ""
        if holding:
            if price <= pos["entry"] * 0.88:
                return "sell", "Stop-loss (down 12%)"
            if sig["score"] <= 0:
                return "sell", "Signal weakened to Hold or worse"
        elif sig["score"] >= 3 and not cooling:
            return "buy", "Signal reached Accumulate"
    return None, ""


def _buy(pos, price):
    spend = pos["cash"]
    pos["qty"] = spend * (1 - FEE) / (price * (1 + SLIP))
    pos["cost"] = spend
    pos["cash"] = 0.0
    pos["entry"] = price


def _sell(pos, price):
    proceeds = pos["qty"] * price * (1 - SLIP) * (1 - FEE)
    pnl = proceeds / pos["cost"] - 1 if pos["cost"] else 0.0
    pos["cash"] = proceeds
    pos["qty"] = 0.0
    pos["cost"] = 0.0
    return pnl, proceeds


def _init(ids, data, now_ts):
    sleeve = CAPITAL / len(ids)
    st = {
        "v": 1,
        "capital": CAPITAL,
        "start_ts": int(now_ts),
        "start": dt.datetime.fromtimestamp(now_ts, dt.timezone.utc).strftime("%Y-%m-%d"),
        "coins": ids,
        "sym": {i: data[i]["sym"] for i in ids},
        "last": {i: data[i]["price"] for i in ids},
        "p": {k: {i: {"cash": sleeve, "qty": 0.0, "entry": 0.0, "cost": 0.0, "cool": 0} for i in ids} for k, _ in STRATS},
        "closed": {k: {"n": 0, "wins": 0} for k, _ in STRATS},
        "peak": {},
        "mdd": {},
        "hist": [],
        "recent": [],
    }
    for i in ids:  # the benchmark buys on day one and never sells
        _buy(st["p"]["hodl"][i], data[i]["price"])
    return st


def step(folder, now_ts, data, fng=None):
    """data: {coin_id: {"price": float, "daily": [closes..., live], "sym": "BTC"}}"""
    path = os.path.join(folder, "paper.json")
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:  # noqa: BLE001
        st = None
    if not st:
        if len(data) < 2:
            return None
        st = _init(list(data), data, now_ts)

    for i in st["coins"]:
        if i in data:
            st["last"][i] = data[i]["price"]

    for k, _ in STRATS:
        if k == "hodl":
            continue
        for i in st["coins"]:
            d = data.get(i)
            if not d or len(d["daily"]) < 60:
                continue
            pos = st["p"][k][i]
            price = d["price"]
            action, why = decide(k, d["daily"], price, pos, now_ts, fng)
            if action == "buy" and pos["cash"] > 1e-9:
                spend = pos["cash"]
                _buy(pos, price)
                st["recent"].insert(0, {"t": int(now_ts), "s": k, "c": st["sym"][i], "side": "BUY", "p": price, "usd": round(spend, 2), "why": why})
            elif action == "sell" and pos["qty"] > 0:
                pnl, proceeds = _sell(pos, price)
                if "Stop-loss" in why:
                    pos["cool"] = int(now_ts) + COOL
                st["closed"][k]["n"] += 1
                st["closed"][k]["wins"] += int(pnl > 0)
                st["recent"].insert(0, {"t": int(now_ts), "s": k, "c": st["sym"][i], "side": "SELL", "p": price, "usd": round(proceeds, 2), "why": why, "pnl": round(pnl, 4)})
    st["recent"] = st["recent"][:40]

    eq = {}
    for k, _ in STRATS:
        eq[k] = sum(pos["cash"] + pos["qty"] * st["last"][i] for i, pos in st["p"][k].items())
        st["peak"][k] = max(st["peak"].get(k, eq[k]), eq[k])
        st["mdd"][k] = min(st["mdd"].get(k, 0.0), eq[k] / st["peak"][k] - 1)
    st["hist"].append([int(now_ts), {k: round(v, 2) for k, v in eq.items()}])
    if len(st["hist"]) > 2000:  # keep recent detail, thin out the old
        h = st["hist"]
        st["hist"] = h[:1000:2] + h[1000:]

    st["updated"] = int(now_ts)
    st["s"] = [
        {
            "k": k,
            "name": name,
            "eq": round(eq[k], 2),
            "ret": round(eq[k] / st["capital"] - 1, 4),
            "closed": st["closed"][k]["n"],
            "wins": st["closed"][k]["wins"],
            "mdd": round(st["mdd"][k], 4),
            "open": [st["sym"][i] for i, pos in st["p"][k].items() if pos["qty"] > 0],
        }
        for k, name in STRATS
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, separators=(",", ":"))
    return st
