"""macd_eurusd_bot.py - MACD(12,26,9) + EMA200 on EURUSD M30, capital.com DEMO account.

Same pattern as the ORB bots: one run per trigger (cron-job.org -> workflow_dispatch), state kept in a
json file that the workflow commits back, secrets CAPITAL_API_KEY / CAPITAL_IDENTIFIER / CAPITAL_PASSWORD.

Rules (identical to the backtest macd_control.py, hypothesis v1.0):
  long   MACD crosses above its signal line, both below zero, close > EMA200
  short  mirror image (cross below, both above zero, close < EMA200)
  signal is read on the last COMPLETED M30 bar; entry is a market order right after that bar closes
  stop   lowest low (long) / highest high (short) of the last 10 M30 bars including the signal bar
  target 1.5 x the stop distance, no break-even, no time limit
  skip   stop distance < 5 pips; one open trade at a time (this bot only)
Safety added for live trading:
  - spread filter, tradeable-market check, late-run filter, per-bar dedup in the state file
  - size = RISK_USD / stop distance (EURUSD: 1 unit moves 1 USD per 1.00 of price), capped by MAX_UNITS;
    if the broker minimum size would push the real risk above 2 x RISK_USD the trade is skipped
Usage:
  python macd_eurusd_bot.py --once                 log only (no orders)
  python macd_eurusd_bot.py --once --place-orders  place real orders on the demo account
  python macd_eurusd_bot.py --find-epic            confirm the epic code
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

BASE = os.environ.get("CAPITAL_BASE_URL", "https://demo-api-capital.backend-capital.com/api/v1")
EPIC = os.environ.get("PAIR_EPIC", "EURUSD")
STATE_FILE = os.environ.get("STATE_FILE", "macd_eurusd_state.json")
RISK_USD = float(os.environ.get("RISK_USD", "2"))
MAX_UNITS = int(os.environ.get("MAX_UNITS", "20000"))
MAX_SPREAD_PIPS = float(os.environ.get("MAX_SPREAD_PIPS", "2.0"))
MAX_LATE_MIN = float(os.environ.get("MAX_LATE_MIN", "10"))
TARGET_R = float(os.environ.get("TARGET_R", "1.5"))
STOP_LOOKBACK = 10
MIN_STOP_PIPS = 5.0
PIP = 0.0001
BARS = 1000          # EMA200 needs a long history to match the backtest
BAR_MIN = 30


def log(*a):
    print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), *a, flush=True)


# ---------------------------------------------------------------- pure logic (tested against the backtest)
def compute_signal(df):
    """df: index = bar start (UTC), columns Open High Low Close, completed bars only.
    Returns None or dict(direction=+1/-1, ll, hh, close, bar_start)."""
    if len(df) < 250:
        return None
    c = df["Close"]
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    sig = macd.ewm(span=9, adjust=False).mean()
    ema200 = c.ewm(span=200, adjust=False).mean()
    m0, m1, s0, s1 = macd.iloc[-2], macd.iloc[-1], sig.iloc[-2], sig.iloc[-1]
    close, e = c.iloc[-1], ema200.iloc[-1]
    ll = df["Low"].iloc[-STOP_LOOKBACK:].min()
    hh = df["High"].iloc[-STOP_LOOKBACK:].max()
    up = (m0 <= s0) and (m1 > s1) and (m1 < 0) and (s1 < 0) and (close > e)
    dn = (m0 >= s0) and (m1 < s1) and (m1 > 0) and (s1 > 0) and (close < e)
    if not (up or dn):
        return None
    return dict(direction=1 if up else -1, ll=float(ll), hh=float(hh), close=float(close),
                bar_start=df.index[-1])


def plan_trade(direction, bid, ask, ll, hh, risk_usd, min_size, max_units):
    """Returns (plan_dict, None) or (None, reason)."""
    if direction > 0:
        entry, stop = ask, ll
        dist = entry - stop
    else:
        entry, stop = bid, hh
        dist = stop - entry
    if dist < MIN_STOP_PIPS * PIP:
        return None, f"stop distance {dist / PIP:.1f} pips is below {MIN_STOP_PIPS} pips"
    target = entry + direction * TARGET_R * dist
    units = int(math.floor(risk_usd / dist))
    units = min(units, max_units)
    if units < min_size:
        units = int(math.ceil(min_size))
    real_risk = units * dist
    if real_risk > 2 * risk_usd:
        return None, f"minimum size {units} would risk {real_risk:.2f} USD (limit {2 * risk_usd:.2f})"
    return dict(direction=direction, entry=entry, stop=round(stop, 5), target=round(target, 5),
                dist_pips=dist / PIP, units=units, risk_usd=real_risk), None


# ---------------------------------------------------------------- capital.com API
class Capital:
    def __init__(self):
        self.s = requests.Session()
        self.key = os.environ["CAPITAL_API_KEY"]

    def req(self, method, path, tries=3, **kw):
        for i in range(tries):
            try:
                r = self.s.request(method, BASE + path, timeout=30, **kw)
                if r.status_code in (429, 500, 502, 503, 504) and i < tries - 1:
                    time.sleep(2 * (i + 1))
                    continue
                return r
            except requests.RequestException as ex:
                if i == tries - 1:
                    raise
                log("network error, retrying:", ex)
                time.sleep(2 * (i + 1))

    def login(self):
        r = self.req("POST", "/session", headers={"X-CAP-API-KEY": self.key, "Content-Type": "application/json"},
                     json={"identifier": os.environ["CAPITAL_IDENTIFIER"],
                           "password": os.environ["CAPITAL_PASSWORD"], "encryptedPassword": False})
        if r.status_code != 200:
            sys.exit(f"login failed {r.status_code}: {r.text[:300]}")
        self.s.headers.update({"X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"], "CST": r.headers["CST"],
                               "X-CAP-API-KEY": self.key})
        log("login ok")

    def candles(self):
        r = self.req("GET", f"/prices/{EPIC}", params={"resolution": "MINUTE_30", "max": BARS})
        if r.status_code != 200:
            sys.exit(f"prices failed {r.status_code}: {r.text[:300]}")
        rows = []
        for p in r.json()["prices"]:
            t = pd.Timestamp(p["snapshotTimeUTC"])
            rows.append((t, p["openPrice"]["bid"], p["highPrice"]["bid"], p["lowPrice"]["bid"], p["closePrice"]["bid"]))
        df = pd.DataFrame(rows, columns=["T", "Open", "High", "Low", "Close"]).set_index("T").sort_index()
        return df[~df.index.duplicated(keep="last")]

    def market(self):
        r = self.req("GET", f"/markets/{EPIC}")
        if r.status_code != 200:
            sys.exit(f"market failed {r.status_code}: {r.text[:300]}")
        j = r.json()
        snap, rules = j["snapshot"], j["dealingRules"]
        return dict(bid=snap["bid"], ask=snap["offer"], status=snap["marketStatus"],
                    min_size=rules["minDealSize"]["value"])

    def position_open(self, deal_id):
        r = self.req("GET", f"/positions/{deal_id}")
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        log(f"position check unclear ({r.status_code}): {r.text[:200]} - assuming still open")
        return True

    def open_position(self, plan):
        body = {"epic": EPIC, "direction": "BUY" if plan["direction"] > 0 else "SELL", "size": plan["units"],
                "guaranteedStop": False, "stopLevel": plan["stop"], "profitLevel": plan["target"]}
        r = self.req("POST", "/positions", tries=1, json=body)
        if r.status_code != 200:
            log(f"order rejected {r.status_code}: {r.text[:400]}")
            return None
        ref = r.json()["dealReference"]
        time.sleep(1)
        c = self.req("GET", f"/confirms/{ref}")
        j = c.json() if c.status_code == 200 else {}
        log("confirm:", json.dumps({k: j.get(k) for k in ("dealStatus", "reason", "dealId", "level", "direction", "size")}))
        return j.get("dealId") if j.get("dealStatus") == "ACCEPTED" else None


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


# ---------------------------------------------------------------- run
def run_once(place):
    api = Capital()
    api.login()
    st = load_state()

    if st.get("open_deal_id"):
        if api.position_open(st["open_deal_id"]):
            log("a trade is still open, nothing to do:", st["open_deal_id"])
            return
        log("previous trade closed:", st["open_deal_id"])
        st["open_deal_id"] = None
        save_state(st)

    df = api.candles()
    now = datetime.now(timezone.utc)
    cutoff = pd.Timestamp(now).tz_localize(None) - pd.Timedelta(minutes=BAR_MIN)
    df = df[df.index <= cutoff]                     # completed bars only
    if len(df) < 250:
        log("not enough completed bars:", len(df))
        return
    last = df.index[-1]
    close_time = last + pd.Timedelta(minutes=BAR_MIN)
    late = (pd.Timestamp(now).tz_localize(None) - close_time).total_seconds() / 60
    log(f"last completed bar {last} (closed {late:.1f} min ago) close {df['Close'].iloc[-1]:.5f}")

    sig = compute_signal(df)
    if sig is None:
        log("no signal on the last bar")
        return
    log(f"SIGNAL {'LONG' if sig['direction'] > 0 else 'SHORT'} on bar {last}: lowest low {sig['ll']:.5f}, highest high {sig['hh']:.5f}")
    if st.get("last_signal_bar") == str(last):
        log("this bar was already handled, skipping")
        return
    st["last_signal_bar"] = str(last)
    save_state(st)
    if late > MAX_LATE_MIN:
        log(f"run is {late:.1f} min after the bar close (limit {MAX_LATE_MIN}), price may have moved - skipping")
        return

    m = api.market()
    spread = (m["ask"] - m["bid"]) / PIP
    log(f"market {m['status']} bid {m['bid']} ask {m['ask']} spread {spread:.1f} pips min size {m['min_size']}")
    if m["status"] != "TRADEABLE":
        log("market not tradeable - skipping")
        return
    if spread > MAX_SPREAD_PIPS:
        log(f"spread above {MAX_SPREAD_PIPS} pips - skipping")
        return
    plan, why = plan_trade(sig["direction"], m["bid"], m["ask"], sig["ll"], sig["hh"], RISK_USD, m["min_size"], MAX_UNITS)
    if plan is None:
        log("no trade:", why)
        return
    log(f"PLAN {'BUY' if plan['direction'] > 0 else 'SELL'} {plan['units']} units entry ~{plan['entry']:.5f} "
        f"stop {plan['stop']} target {plan['target']} (stop {plan['dist_pips']:.1f} pips, risk {plan['risk_usd']:.2f} USD)")
    if not place:
        log("log only mode (no --place-orders): no order sent")
        return
    deal_id = api.open_position(plan)
    if deal_id:
        st["open_deal_id"] = deal_id
        st["opened_at"] = now.isoformat()
        save_state(st)
        log("trade open, deal id", deal_id)


def find_epic():
    api = Capital()
    api.login()
    r = api.req("GET", "/markets", params={"searchTerm": "EURUSD"})
    for mk in r.json().get("markets", [])[:10]:
        log(mk.get("epic"), "|", mk.get("instrumentName"), "|", mk.get("instrumentType"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--place-orders", action="store_true")
    ap.add_argument("--find-epic", action="store_true")
    a = ap.parse_args()
    if a.find_epic:
        find_epic()
    else:
        run_once(a.place_orders)
