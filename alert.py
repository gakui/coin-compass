#!/usr/bin/env python3
"""Coin Compass cloud alerts.

Runs hourly in GitHub Actions. Checks the base coins in config.json, decides what
is worth telling you, and sends it to your phone through ntfy. State is kept in
state.json so you are not alerted twice for the same thing.

Information only. Not financial advice.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

CG = "https://api.coingecko.com/api/v3"
CC = "https://min-api.cryptocompare.com/data/v2"
HERE = os.path.dirname(os.path.abspath(__file__))

ADVICE = {
    "ACCUMULATE": "Looks cheap and is starting to stabilise. If you buy, spread it over several small purchases.",
    "WATCH": "Getting cheap, but the trend is still weak. If you buy, start small and keep cash for lower prices.",
    "HOLD": "No clear edge either way. Keep what you have.",
    "REDUCE": "Stretched or overheated. Consider taking some profit or tightening your stop.",
}
NICE = {"ACCUMULATE": "Accumulate", "WATCH": "Watch", "HOLD": "Hold", "REDUCE": "Reduce"}


# ---------- signal logic (same rules as the app) ----------
def rsi(c, n=14):
    if len(c) < n + 1:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        if d >= 0:
            g += d
        else:
            l -= d
    g /= n
    l /= n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def sma(c, n):
    s = c[-n:]
    return sum(s) / len(s)


def compute_signal(c, fng):
    if not c or len(c) < 60:
        return None
    last = c[-1]
    r = rsi(c)
    m50 = sma(c, 50)
    m200 = sma(c, 200) if len(c) >= 200 else None
    dd = last / max(c[-180:]) - 1
    s = 0
    if r < 30:
        s += 2
    elif r < 40:
        s += 1
    elif r > 70:
        s -= 2
    elif r > 60:
        s -= 1
    s += 1 if last > m50 else -1
    if m200:
        x = last / m200 - 1
        if x > 0.4:
            s -= 2
        elif x >= 0:
            s += 1
        else:
            s -= 1
    if dd <= -0.4:
        s += 1
    if fng is not None:
        if fng <= 20:
            s += 1
        elif fng >= 75:
            s -= 1
    label = "ACCUMULATE" if s >= 3 else "WATCH" if s >= 1 else "HOLD" if s >= -1 else "REDUCE"
    return {"label": label, "score": s, "rsi": r}


# ---------- network ----------
def get(url, headers=None, tries=3):
    h = {"User-Agent": "coin-compass/1.0", "Accept": "application/json"}
    h.update(headers or {})
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2 + 4 * i)
    raise RuntimeError(f"GET failed: {url} ({err})")


def fmt_price(p):
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:.2f}"
    if p >= 0.01:
        return f"{p:.4f}"
    return f"{p:.6f}"


def send(topic, title, body, priority="default", tags="", click=""):
    print(f"[notify] {title} | {body}")
    if not topic:
        return
    h = {
        "Title": title.encode("ascii", "ignore").decode(),
        "Priority": priority,
        "Tags": tags,
    }
    if click:
        h["Click"] = click
    req = urllib.request.Request(
        "https://ntfy.sh/" + urllib.parse.quote(topic),
        data=body.encode("utf-8"),
        headers=h,
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        print("ntfy error:", e)


# ---------- main ----------
def load(name, default):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def run(fetch_markets=None, fetch_candles=None, fetch_fng=None, now=None, topic=None, click=None):
    cfg = load("config.json", {})
    state = load("state.json", {})
    base = cfg.get("base") or []
    move_pct = float(cfg.get("move_pct", 7))
    brief_hour = int(cfg.get("brief_hour_utc", 6))
    topic = os.environ.get("NTFY_TOPIC", "") if topic is None else topic
    click = os.environ.get("APP_URL", "") if click is None else click
    now = now or dt.datetime.now(dt.timezone.utc)
    key = os.environ.get("CRYPTOCOMPARE_KEY", "")
    cc_headers = {"authorization": f"Apikey {key}"} if key else {}

    if fetch_markets is None:
        def fetch_markets(ids):
            q = f"{CG}/coins/markets?vs_currency=usd&ids={','.join(ids)}&price_change_percentage=24h,7d"
            return get(q)
    if fetch_candles is None:
        def fetch_candles(sym):
            j = get(f"{CC}/histoday?fsym={sym}&tsym=USD&limit=210", cc_headers)
            if j.get("Response") != "Success":
                raise RuntimeError("no candles for " + sym)
            return [d["close"] for d in j["Data"]["Data"] if d["close"] > 0]
    if fetch_fng is None:
        def fetch_fng():
            return int(get("https://api.alternative.me/fng/?limit=1")["data"][0]["value"])

    markets = {m["id"]: m for m in fetch_markets(base)}
    try:
        fng = fetch_fng()
    except Exception as e:  # noqa: BLE001
        print("fng failed:", e)
        fng = None

    labels = state.get("labels", {})
    moves = state.get("moves", {})
    fired = state.get("fired", {})
    first_run = not labels
    msgs = []  # (title, body, priority, tags)
    lines = []
    now_ts = now.timestamp()

    for cid in base:
        m = markets.get(cid)
        if not m:
            continue
        sym = m["symbol"].upper()
        price = m["current_price"]
        ch24 = m.get("price_change_percentage_24h_in_currency", m.get("price_change_percentage_24h"))
        sig = None
        try:
            c = fetch_candles(sym)
            c[-1] = price
            sig = compute_signal(c, fng)
        except Exception as e:  # noqa: BLE001
            print(f"candles failed for {sym}: {e}")
        time.sleep(0.4)

        if sig:
            prev = labels.get(cid)
            labels[cid] = sig["label"]
            if prev and prev != sig["label"] and sig["label"] in ("ACCUMULATE", "REDUCE"):
                msgs.append((
                    f"{sym}: {NICE[sig['label']]}",
                    f"{sym} at ${fmt_price(price)} moved from {NICE.get(prev, prev)} to {NICE[sig['label']]}. {ADVICE[sig['label']]}",
                    "high" if sig["label"] == "ACCUMULATE" else "default",
                    "green_circle" if sig["label"] == "ACCUMULATE" else "orange_circle",
                ))
        if ch24 is not None and abs(ch24) >= move_pct and now_ts - moves.get(cid, 0) > 86400:
            moves[cid] = now_ts
            up = ch24 > 0
            msgs.append((
                f"{sym} {'up' if up else 'down'} {abs(ch24):.1f}% in 24h",
                f"{sym} is ${fmt_price(price)} after a {abs(ch24):.1f}% {'rise' if up else 'drop'} today.",
                "default",
                "chart_with_upwards_trend" if up else "chart_with_downwards_trend",
            ))
        for a in cfg.get("alerts", []):
            if a.get("id") != cid:
                continue
            for d in ("above", "below"):
                if d not in a:
                    continue
                level = float(a[d])
                hit = price >= level if d == "above" else price <= level
                k = f"{cid}:{d}:{level}"
                if hit and not fired.get(k):
                    fired[k] = True
                    msgs.append((
                        f"{sym} {'rose above' if d == 'above' else 'fell below'} ${fmt_price(level)}",
                        f"{sym} is now ${fmt_price(price)}.",
                        "high",
                        "bell",
                    ))
                elif not hit and fired.get(k):
                    fired[k] = False
        lines.append(
            f"{sym} ${fmt_price(price)} "
            f"{'' if ch24 is None else ('+' if ch24 >= 0 else '') + format(ch24, '.1f') + '%'} "
            f"{NICE[sig['label']] if sig else ''}".strip()
        )

    if fng is not None:
        zone = "fear" if fng <= 15 else "greed" if fng >= 80 else "mid"
        if zone != state.get("fng_zone", "mid") and zone != "mid":
            msgs.append((
                "Extreme fear" if zone == "fear" else "Extreme greed",
                f"Fear & Greed is {fng}. "
                + ("Panic selling often marks cheap prices, but prices can keep falling. Stage any buying."
                   if zone == "fear" else "Markets look overheated. Be careful chasing prices."),
                "default",
                "warning",
            ))
        state["fng_zone"] = zone

    today = now.strftime("%Y-%m-%d")
    if now.hour >= brief_hour and state.get("brief_date") != today and lines:
        state["brief_date"] = today
        head = f"Mood: Fear & Greed {fng}\n" if fng is not None else ""
        msgs.append(("Coin Compass daily brief", head + "\n".join(lines) + "\nInformation only, not advice.", "default", "compass"))

    # First run only records the starting point so you are not flooded.
    if first_run:
        msgs = [m for m in msgs if m[0] == "Coin Compass daily brief"]

    for title, body, prio, tags in msgs[:8]:
        send(topic, title, body, prio, tags, click)
    if len(msgs) > 8:
        send(topic, "More alerts waiting", f"{len(msgs) - 8} more. Open the app to see everything.", "low", "inbox_tray", click)

    state.update({"labels": labels, "moves": moves, "fired": fired, "last_run_date": today})
    with open(os.path.join(HERE, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    print(f"done: {len(msgs)} alert(s)")
    return msgs


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # noqa: BLE001
        print("run failed:", e)
        sys.exit(1)
