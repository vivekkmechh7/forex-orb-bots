#!/usr/bin/env python3
"""
NY-open Opening Range Breakout (ORB) bot for Capital.com (DEMO) - USDJPY and GOLD.

Backtest basis (2012-2022 M15 data, see research_results.md): range = first 30 minutes after 08:00 New York time
(all year, DST-aware), stop-entry orders at the range high/low, stop-loss at the opposite side of the range,
take-profit = TP_MULTIPLE x range width, unfilled orders expire after ORDER_EXPIRY_MINUTES, everything is closed
CLOSE_AFTER_HOURS after the open. One trade cycle per pair per day, weekends skipped.

Differences from forex_orb_bot.py (which is left untouched):
  * trade size comes from a risk budget (RISK_PERCENT of balance, or RISK_USD) and the stop distance
  * range must be between ATR_MIN_RATIO and ATR_MAX_RATIO x the M15 ATR(14) (same filter as the backtest)
  * pending orders expire ORDER_EXPIRY_MINUTES after the range ends (not at session close)
  * once one leg is gone (filled), the other order is cancelled even if the position already closed
    (no stop-and-reverse second trade)
  * a one-line daily summary is appended to LOG_FILE

Run it from cron-job.org -> workflow_dispatch with:  python ny_orb_bot.py --once --place-orders
Without --place-orders it is a dry run (prints levels and size, places nothing).
"""

import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

import requests

BASE_URL = os.getenv("CAPITAL_BASE_URL", "https://demo-api-capital.backend-capital.com/api/v1")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config():
    def e(name, default=None):
        v = os.getenv(name)
        return v if v not in (None, "") else default

    cfg = {
        "epic": e("PAIR_EPIC"),
        "name": e("PAIR_NAME", e("PAIR_EPIC", "")),
        "state_file": e("STATE_FILE", "ny_orb_state.json"),
        "log_file": e("LOG_FILE", "ny_orb_log.csv"),
        "tz": e("SESSION_TZ", "America/New_York"),
        "open": e("SESSION_OPEN", "08:00"),
        "range_min": int(e("RANGE_MINUTES", "30")),
        "tp_multiple": float(e("TP_MULTIPLE", "3.0")),
        "order_window_min": int(e("ORDER_WINDOW_MINUTES", "30")),     # how long after the range ends we may still place
        "order_expiry_min": int(e("ORDER_EXPIRY_MINUTES", "60")),     # pending orders live this long after the range ends
        "close_hours": float(e("CLOSE_AFTER_HOURS", "6")),
        "risk_percent": float(e("RISK_PERCENT", "0.5")),
        "risk_usd": float(e("RISK_USD")) if e("RISK_USD") else None,
        "max_risk_overshoot": float(e("MAX_RISK_OVERSHOOT", "1.5")),
        "buffer_spreads": float(e("ENTRY_BUFFER_SPREADS", "0.5")),    # entry = level +/- this x current spread
        "atr_min": float(e("ATR_MIN_RATIO", "0.5")),
        "atr_max": float(e("ATR_MAX_RATIO", "4.0")),
    }
    if not cfg["epic"]:
        sys.exit("PAIR_EPIC is not set")
    return cfg


def usd_per_unit_per_price(name, price):
    """USD P&L of 1 unit for a price move of 1.0 (account currency assumed USD)."""
    return 1.0 / price if "JPY" in (name or "").upper() else 1.0


# --------------------------------------------------------------------------- #
# Capital.com API client (same calls as the live forex_orb_bot.py, plus balance and M15 prices)
# --------------------------------------------------------------------------- #
class Capital:
    def __init__(self, api_key, identifier, password):
        self.api_key, self.identifier, self.password = api_key, identifier, password
        self.s = requests.Session()

    def login(self):
        r = self.s.post(
            f"{BASE_URL}/session",
            headers={"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"identifier": self.identifier, "password": self.password, "encryptedPassword": False},
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Login failed: {r.status_code} {r.text[:200]}")
        self.s.headers.update({"X-CAP-API-KEY": self.api_key, "CST": r.headers["CST"],
                               "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]})
        log("Logged in to Capital.com")

    def req(self, method, path, **kw):
        last = None
        for attempt in range(4):
            r = self.s.request(method, f"{BASE_URL}{path}", timeout=30, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"{r.status_code} {r.text[:200]}"
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.text else {}
        raise RuntimeError(f"{method} {path} failed after retries: {last}")

    def search(self, term):
        return self.req("GET", "/markets", params={"searchTerm": term}).get("markets", [])

    def market(self, epic):
        return self.req("GET", f"/markets/{epic}")

    def balance(self):
        accs = self.req("GET", "/accounts").get("accounts", [])
        pref = [a for a in accs if a.get("preferred")] or accs
        return float(pref[0]["balance"]["balance"])

    def _prices(self, epic, resolution, start_utc, end_utc, mx):
        fmt = "%Y-%m-%dT%H:%M:%S"
        data = self.req("GET", f"/prices/{epic}", params={
            "resolution": resolution, "from": start_utc.strftime(fmt), "to": end_utc.strftime(fmt), "max": mx})
        return data.get("prices", [])

    def minute_prices(self, epic, start_utc, end_utc):
        return self._prices(epic, "MINUTE", start_utc, end_utc, 200)

    def m15_prices(self, epic, start_utc, end_utc):
        return self._prices(epic, "MINUTE_15", start_utc, end_utc, 200)

    def positions(self, epic):
        return [p["position"] for p in self.req("GET", "/positions").get("positions", [])
                if p.get("market", {}).get("epic") == epic]

    def working_orders(self, epic):
        out = []
        for o in self.req("GET", "/workingorders").get("workingOrders", []):
            d = o.get("workingOrderData", {})
            if d.get("epic") == epic or o.get("marketData", {}).get("epic") == epic:
                out.append(d)
        return out

    def confirm(self, deal_ref):
        time.sleep(1)
        return self.req("GET", f"/confirms/{deal_ref}")

    def place_stop_order(self, epic, direction, size, level, stop_level, profit_level, gtd):
        body = {"epic": epic, "direction": direction, "size": size, "level": level, "type": "STOP",
                "stopLevel": stop_level, "profitLevel": profit_level, "goodTillDate": gtd}
        resp = self.req("POST", "/workingorders", json=body)
        conf = self.confirm(resp["dealReference"])
        if conf.get("dealStatus") != "ACCEPTED":
            raise RuntimeError(f"{direction} order rejected: {conf.get('reason')} | {conf}")
        log(f"{direction} STOP placed @ {level} size {size} SL {stop_level} TP {profit_level} (deal {conf.get('dealId')})")
        return conf.get("dealId")

    def delete_order(self, deal_id):
        self.req("DELETE", f"/workingorders/{deal_id}")
        log(f"Working order {deal_id} cancelled")

    def close_position(self, deal_id):
        self.req("DELETE", f"/positions/{deal_id}")
        log(f"Position {deal_id} closed")


# --------------------------------------------------------------------------- #
# State and daily log
# --------------------------------------------------------------------------- #
def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def append_log(cfg, state):
    """One line per finished day: date, pair, status, range width, ATR, size, risk."""
    row = [state.get("date"), cfg["name"], state.get("status"), state.get("reason", ""),
           state.get("range_width"), state.get("atr15"), state.get("size"), state.get("risk_usd")]
    new = not os.path.exists(cfg["log_file"])
    with open(cfg["log_file"], "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "pair", "status", "reason", "range_width", "atr15", "size", "risk_usd"])
        w.writerow(row)


# --------------------------------------------------------------------------- #
# Strategy helpers
# --------------------------------------------------------------------------- #
def session_times(cfg, now):
    tz = ZoneInfo(cfg["tz"])
    local_today = now.astimezone(tz).date()
    hh, mm = (int(x) for x in cfg["open"].split(":"))
    open_local = datetime.combine(local_today, dtime(hh, mm), tzinfo=tz)
    return local_today, open_local.astimezone(timezone.utc)


def parse_ts(c):
    return datetime.strptime(c["snapshotTimeUTC"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def mid(c, key):
    return (c[key]["bid"] + c[key]["ask"]) / 2


def compute_range(api, cfg, open_utc):
    end = open_utc + timedelta(minutes=cfg["range_min"])
    candles = api.minute_prices(cfg["epic"], open_utc, end + timedelta(minutes=1))
    highs, lows = [], []
    for c in candles:
        ts = parse_ts(c)
        if open_utc <= ts < end:
            highs.append(mid(c, "highPrice"))
            lows.append(mid(c, "lowPrice"))
    if len(highs) < max(3, cfg["range_min"] // 3):
        raise RuntimeError(f"Not enough candles for opening range ({len(highs)} found)")
    return max(highs), min(lows), len(highs)


def atr15(api, cfg, end_utc, n=14):
    """Wilder ATR(14) on closed M15 bars that finished at or before end_utc (same as the backtest)."""
    cs = api.m15_prices(cfg["epic"], end_utc - timedelta(hours=26), end_utc)
    bars = sorted(((parse_ts(c), mid(c, "highPrice"), mid(c, "lowPrice"), mid(c, "closePrice")) for c in cs))
    bars = [b for b in bars if b[0] + timedelta(minutes=15) <= end_utc]
    if len(bars) < n + 2:
        return None
    tr = [bars[0][1] - bars[0][2]]
    for i in range(1, len(bars)):
        h, l, pc = bars[i][1], bars[i][2], bars[i - 1][3]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(tr[:n]) / n
    for t in tr[n:]:
        a = (a * (n - 1) + t) / n
    return a


def compute_size(api, cfg, mkt, price, stop_dist):
    rules = mkt.get("dealingRules", {})
    mn = float(rules.get("minDealSize", {}).get("value", 0.01))
    step = float(rules.get("minSizeIncrement", {}).get("value") or mn)
    mx = float(rules.get("maxDealSize", {}).get("value", 1e12))
    risk = cfg["risk_usd"] if cfg["risk_usd"] else api.balance() * cfg["risk_percent"] / 100.0
    vpu = usd_per_unit_per_price(cfg["name"], price)
    raw = risk / (stop_dist * vpu)
    size = min(math.floor(raw / step + 1e-9) * step, mx)
    if size < mn:
        size = mn
    size = round(size, 6)
    actual = size * stop_dist * vpu
    if actual > risk * cfg["max_risk_overshoot"]:
        return 0.0, actual, risk
    return size, actual, risk


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
def place_orders(api, cfg, state, open_utc, range_end, place):
    mkt = api.market(cfg["epic"])
    snap = mkt.get("snapshot", {})
    status = snap.get("marketStatus")
    if status != "TRADEABLE":
        log(f"Market status is {status}, will retry next run")
        return
    dp = int(snap.get("decimalPlacesFactor", 3 if "JPY" in cfg["name"].upper() else 5))

    high, low, n = compute_range(api, cfg, open_utc)
    width = high - low
    log(f"Range: high {high:.{dp}f} low {low:.{dp}f} width {width:.{dp}f} ({n} candles)")
    state["range_width"] = round(width, dp)

    try:
        atr = atr15(api, cfg, range_end)
    except Exception as ex:                       # never block the day only because the ATR call failed
        log(f"ATR unavailable ({ex}), range filter skipped")
        atr = None
    if atr:
        state["atr15"] = round(atr, dp + 1)
        log(f"M15 ATR(14) = {atr:.{dp + 1}f}  -> range = {width / atr:.2f} ATR (allowed {cfg['atr_min']}-{cfg['atr_max']})")
        if width < cfg["atr_min"] * atr or width > cfg["atr_max"] * atr:
            log("Range outside the ATR band, skipping today")
            if place:
                state.update(status="skipped", reason="range vs ATR")
            return

    bid, offer = snap.get("bid"), snap.get("offer")
    spread = (offer - bid) if (bid is not None and offer is not None) else 0.0
    buf = cfg["buffer_spreads"] * spread
    buy_entry, sell_entry = round(high + buf, dp), round(low - buf, dp)
    if bid is not None and offer is not None:
        log(f"Current price: bid {bid} / offer {offer} (spread {spread:.{dp}f})")
        if offer >= buy_entry or bid <= sell_entry:
            log("Price already broke out of the range before orders were placed, skipping today")
            if place:
                state.update(status="skipped", reason="breakout already happened")
            return

    tp_dist = width * cfg["tp_multiple"]
    buy_sl, sell_sl = round(low, dp), round(high, dp)
    buy_tp, sell_tp = round(buy_entry + tp_dist, dp), round(sell_entry - tp_dist, dp)
    stop_dist = max(buy_entry - buy_sl, sell_sl - sell_entry)

    size, actual_risk, risk = compute_size(api, cfg, mkt, buy_entry, stop_dist)
    if size <= 0:
        log(f"Minimum size would risk ${actual_risk:.2f} vs budget ${risk:.2f}, skipping today")
        if place:
            state.update(status="skipped", reason="min size too risky")
        return
    state.update(size=size, risk_usd=round(actual_risk, 2))
    gtd = (range_end + timedelta(minutes=cfg["order_expiry_min"])).strftime("%Y-%m-%dT%H:%M:%S")

    log(f"Size {size} (risk about ${actual_risk:.2f}, budget ${risk:.2f}); orders expire {gtd} UTC")
    log(f"BUY  stop {buy_entry} SL {buy_sl} TP {buy_tp}")
    log(f"SELL stop {sell_entry} SL {sell_sl} TP {sell_tp}")

    if not place:
        log("Dry run (no --place-orders): nothing placed")
        return

    buy_id = api.place_stop_order(cfg["epic"], "BUY", size, buy_entry, buy_sl, buy_tp, gtd)
    try:
        sell_id = api.place_stop_order(cfg["epic"], "SELL", size, sell_entry, sell_sl, sell_tp, gtd)
    except Exception:
        log("SELL order failed, cancelling BUY order")
        api.delete_order(buy_id)
        raise
    state.update(status="orders_placed", range_high=high, range_low=low, buy_deal=buy_id, sell_deal=sell_id)


def manage(api, cfg, state, now, close_time):
    positions = api.positions(cfg["epic"])
    orders = api.working_orders(cfg["epic"])

    if now >= close_time:
        log("Session time limit reached, closing everything")
        for o in orders:
            api.delete_order(o["dealId"])
        for p in positions:
            api.close_position(p["dealId"])
        state["status"] = "done"
        return

    placed = {state.get("buy_deal"), state.get("sell_deal")} - {None}
    remaining = {o["dealId"] for o in orders}

    if positions or (placed and remaining != placed):
        # one leg has been filled (the position may even be closed already): kill what is left, no reversal trade
        if orders:
            log("One leg filled, cancelling the remaining working order (OCO)")
            for o in orders:
                api.delete_order(o["dealId"])
        if positions:
            state["status"] = "in_trade"
            log(f"In trade: {positions[0].get('direction')} @ {positions[0].get('level')}")
        else:
            state["status"] = "done"
            log("Trade already closed by its stop or target, day finished")
    elif not orders:
        log("No position and no working orders left (expired), day finished")
        state["status"] = "done"
    else:
        log("Waiting for a breakout")


def tick(cfg, api, state, place, now=None):
    now = now or datetime.now(timezone.utc)
    today, open_utc = session_times(cfg, now)

    if today.weekday() >= 5:
        log("Weekend, nothing to do")
        return state

    if state.get("date") != today.isoformat():
        state = {"date": today.isoformat(), "status": "new"}

    if state["status"] in ("done", "skipped", "missed"):
        log(f"Today already {state['status']}")
        return state

    range_end = open_utc + timedelta(minutes=cfg["range_min"])
    if now < range_end:
        log(f"Opening range not complete yet (ends {range_end:%H:%M} UTC)")
        return state

    close_time = open_utc + timedelta(hours=cfg["close_hours"])

    if state["status"] == "new":
        if now > range_end + timedelta(minutes=cfg["order_window_min"]):
            log("Order window passed without placing orders, marking day as missed")
            state["status"] = "missed"
            return state
        api.login()
        place_orders(api, cfg, state, open_utc, range_end, place)
    else:
        api.login()
        manage(api, cfg, state, now, close_time)
    return state


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def make_api():
    key, ident, pwd = (os.getenv(k) for k in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_PASSWORD"))
    if not (key and ident and pwd):
        sys.exit("CAPITAL_API_KEY / CAPITAL_IDENTIFIER / CAPITAL_PASSWORD must be set")
    return Capital(key, ident, pwd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--place-orders", action="store_true", help="really place orders (otherwise dry run)")
    ap.add_argument("--find-epic", nargs="?", const="", help="search Capital.com for an epic")
    args = ap.parse_args()

    if args.find_epic is not None:
        term = args.find_epic or os.getenv("PAIR_NAME") or os.getenv("PAIR_EPIC", "")
        api = make_api()
        api.login()
        for m in api.search(term):
            print(f"{m.get('epic'):<14} {m.get('instrumentName'):<28} {m.get('instrumentType'):<12} {m.get('marketStatus')}")
        return

    cfg = load_config()
    api = make_api()
    while True:
        state = load_state(cfg["state_file"])
        before = state.get("status")
        try:
            state = tick(cfg, api, state, args.place_orders)
        except Exception as ex:
            log(f"ERROR: {ex}")
            save_state(cfg["state_file"], state)
            if args.once:
                sys.exit(1)
        else:
            save_state(cfg["state_file"], state)
            if state.get("status") in ("done", "skipped", "missed") and before != state.get("status"):
                append_log(cfg, state)
        if args.once:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
