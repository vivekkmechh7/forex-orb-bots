#!/usr/bin/env python3
"""
Forex Opening Range Breakout (ORB) bot for Capital.com (DEMO).

How it works (one pair per run, state kept in a JSON file):
  1. At SESSION_OPEN (in SESSION_TZ) the opening range starts and lasts RANGE_MINUTES.
  2. When the range is complete, the bot places two STOP working orders:
       BUY  stop above the range high  (stop-loss at range low)
       SELL stop below the range low   (stop-loss at range high)
     Take-profit = range width x TP_MULTIPLE, measured from the entry.
     If TP_PROFIT_USD / SL_LOSS_USD are set, fixed-dollar distances are used instead.
  3. When one side fills, the other working order is cancelled (OCO).
  4. At SESSION_OPEN + CLOSE_AFTER_HOURS everything left is closed / cancelled.
  5. One trade cycle per pair per day. Weekends are skipped.

Run it every few minutes (cron-job.org -> workflow_dispatch) with:
    python forex_orb_bot.py --once --place-orders

Without --place-orders it is a dry run (prints levels, places nothing).
Find the right epic:
    python forex_orb_bot.py --find-epic EURUSD
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

import requests

BASE_URL = os.getenv(
    "CAPITAL_BASE_URL", "https://demo-api-capital.backend-capital.com/api/v1"
)


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
        "state_file": e("STATE_FILE", "orb_state.json"),
        "range_min": int(e("RANGE_MINUTES", "15")),
        "tp_multiple": float(e("TP_MULTIPLE", "1.5")),
        "units": float(e("UNITS", "2000")),
        "tp_usd": float(e("TP_PROFIT_USD")) if e("TP_PROFIT_USD") else None,
        "sl_usd": float(e("SL_LOSS_USD")) if e("SL_LOSS_USD") else None,
        "tz": e("SESSION_TZ", "Europe/London"),
        "open": e("SESSION_OPEN", "08:00"),
        "order_window_min": int(e("ORDER_WINDOW_MINUTES", "60")),
        "close_hours": float(e("CLOSE_AFTER_HOURS", "8")),
        "buffer_pips": float(e("ENTRY_BUFFER_PIPS", "1")),
        "min_range_pips": float(e("MIN_RANGE_PIPS", "3")),
        "max_range_pips": float(e("MAX_RANGE_PIPS", "40")),
    }
    if not cfg["epic"]:
        sys.exit("PAIR_EPIC is not set")
    return cfg


def pip_size(pair_name, epic):
    label = (pair_name or epic or "").upper()
    return 0.01 if "JPY" in label else 0.0001


# --------------------------------------------------------------------------- #
# Capital.com API client
# --------------------------------------------------------------------------- #
class Capital:
    def __init__(self, api_key, identifier, password):
        self.api_key = api_key
        self.identifier = identifier
        self.password = password
        self.s = requests.Session()

    def login(self):
        r = self.s.post(
            f"{BASE_URL}/session",
            headers={"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={
                "identifier": self.identifier,
                "password": self.password,
                "encryptedPassword": False,
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Login failed: {r.status_code} {r.text[:200]}")
        self.s.headers.update(
            {
                "X-CAP-API-KEY": self.api_key,
                "CST": r.headers["CST"],
                "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"],
            }
        )
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

    # --- market data ---
    def search(self, term):
        return self.req("GET", "/markets", params={"searchTerm": term}).get("markets", [])

    def market(self, epic):
        return self.req("GET", f"/markets/{epic}")

    def minute_prices(self, epic, start_utc, end_utc):
        fmt = "%Y-%m-%dT%H:%M:%S"
        data = self.req(
            "GET",
            f"/prices/{epic}",
            params={
                "resolution": "MINUTE",
                "from": start_utc.strftime(fmt),
                "to": end_utc.strftime(fmt),
                "max": 200,
            },
        )
        return data.get("prices", [])

    # --- trading ---
    def positions(self, epic):
        out = []
        for p in self.req("GET", "/positions").get("positions", []):
            if p.get("market", {}).get("epic") == epic:
                out.append(p["position"])
        return out

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
        body = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "level": level,
            "type": "STOP",
            "stopLevel": stop_level,
            "profitLevel": profit_level,
            "goodTillDate": gtd,
        }
        resp = self.req("POST", "/workingorders", json=body)
        conf = self.confirm(resp["dealReference"])
        if conf.get("dealStatus") != "ACCEPTED":
            raise RuntimeError(f"{direction} order rejected: {conf.get('reason')} | {conf}")
        log(f"{direction} STOP placed @ {level} SL {stop_level} TP {profit_level} (deal {conf.get('dealId')})")
        return conf.get("dealId")

    def delete_order(self, deal_id):
        self.req("DELETE", f"/workingorders/{deal_id}")
        log(f"Working order {deal_id} cancelled")

    def close_position(self, deal_id):
        self.req("DELETE", f"/positions/{deal_id}")
        log(f"Position {deal_id} closed")


# --------------------------------------------------------------------------- #
# State
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


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
def session_times(cfg, now):
    tz = ZoneInfo(cfg["tz"])
    local_today = now.astimezone(tz).date()
    hh, mm = (int(x) for x in cfg["open"].split(":"))
    open_local = datetime.combine(local_today, dtime(hh, mm), tzinfo=tz)
    return local_today, open_local.astimezone(timezone.utc)


def usd_to_price_distance(usd, units, price, pair_name):
    """Convert a USD amount into a price distance for `units` of the pair."""
    quote = pair_name.upper().split("/")[-1]
    if quote == "USD":
        return usd / units
    if quote == "JPY":
        return usd * price / units
    raise RuntimeError(f"TP_PROFIT_USD/SL_LOSS_USD not supported for quote currency {quote}")


def compute_range(api, cfg, open_utc):
    end = open_utc + timedelta(minutes=cfg["range_min"])
    candles = api.minute_prices(cfg["epic"], open_utc, end + timedelta(minutes=1))
    highs, lows = [], []
    for c in candles:
        ts = datetime.strptime(c["snapshotTimeUTC"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        if open_utc <= ts < end:
            highs.append((c["highPrice"]["bid"] + c["highPrice"]["ask"]) / 2)
            lows.append((c["lowPrice"]["bid"] + c["lowPrice"]["ask"]) / 2)
    if len(highs) < max(3, cfg["range_min"] // 3):
        raise RuntimeError(f"Not enough candles for opening range ({len(highs)} found)")
    return max(highs), min(lows), len(highs)


def place_orders(api, cfg, state, open_utc, place):
    mkt = api.market(cfg["epic"])
    status = mkt.get("snapshot", {}).get("marketStatus")
    if status != "TRADEABLE":
        log(f"Market status is {status}, will retry next run")
        return
    dp = int(mkt.get("snapshot", {}).get("decimalPlacesFactor", 3 if "JPY" in cfg["name"].upper() else 5))

    high, low, n = compute_range(api, cfg, open_utc)
    pip = pip_size(cfg["name"], cfg["epic"])
    width = high - low
    width_pips = width / pip
    log(f"Range: high {high:.{dp}f} low {low:.{dp}f} ({width_pips:.1f} pips, {n} candles)")

    if width_pips < cfg["min_range_pips"] or width_pips > cfg["max_range_pips"]:
        log("Range outside MIN/MAX_RANGE_PIPS, skipping today")
        state.update(status="skipped", reason="range size")
        return

    buf = cfg["buffer_pips"] * pip
    buy_entry = round(high + buf, dp)
    sell_entry = round(low - buf, dp)

    if cfg["tp_usd"] and cfg["sl_usd"]:
        tp_dist = usd_to_price_distance(cfg["tp_usd"], cfg["units"], buy_entry, cfg["name"])
        sl_dist = usd_to_price_distance(cfg["sl_usd"], cfg["units"], buy_entry, cfg["name"])
        log(f"Fixed-dollar exits: TP {tp_dist / pip:.1f} pips, SL {sl_dist / pip:.1f} pips")
    else:
        tp_dist = width * cfg["tp_multiple"]
        sl_dist = None  # opposite side of the range

    buy_sl = round(buy_entry - sl_dist, dp) if sl_dist else round(low, dp)
    sell_sl = round(sell_entry + sl_dist, dp) if sl_dist else round(high, dp)
    buy_tp = round(buy_entry + tp_dist, dp)
    sell_tp = round(sell_entry - tp_dist, dp)
    gtd = (open_utc + timedelta(hours=cfg["close_hours"])).strftime("%Y-%m-%dT%H:%M:%S")

    log(f"BUY  stop {buy_entry} SL {buy_sl} TP {buy_tp}")
    log(f"SELL stop {sell_entry} SL {sell_sl} TP {sell_tp}")

    if not place:
        log("Dry run (no --place-orders): nothing placed")
        return

    buy_id = api.place_stop_order(cfg["epic"], "BUY", cfg["units"], buy_entry, buy_sl, buy_tp, gtd)
    try:
        sell_id = api.place_stop_order(cfg["epic"], "SELL", cfg["units"], sell_entry, sell_sl, sell_tp, gtd)
    except Exception:
        log("SELL order failed, cancelling BUY order")
        api.delete_order(buy_id)
        raise

    state.update(
        status="orders_placed",
        range_high=high,
        range_low=low,
        buy_deal=buy_id,
        sell_deal=sell_id,
    )


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

    if positions:
        if orders:
            log("Position open, cancelling the opposite working order (OCO)")
            for o in orders:
                api.delete_order(o["dealId"])
        state["status"] = "in_trade"
        log(f"In trade: {positions[0].get('direction')} @ {positions[0].get('level')}")
    elif not orders:
        log("No position and no working orders left, day finished")
        state["status"] = "done"
    else:
        log("Waiting for a breakout")


def tick(cfg, api, state, place):
    now = datetime.now(timezone.utc)
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
        place_orders(api, cfg, state, open_utc, place)
    else:
        api.login()
        manage(api, cfg, state, now, close_time)
    return state


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def make_api():
    key = os.getenv("CAPITAL_API_KEY")
    ident = os.getenv("CAPITAL_IDENTIFIER")
    pwd = os.getenv("CAPITAL_PASSWORD")
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
        try:
            state = tick(cfg, api, state, args.place_orders)
        except Exception as ex:  # keep state file consistent, surface the error
            log(f"ERROR: {ex}")
            save_state(cfg["state_file"], state)
            if args.once:
                sys.exit(1)
        else:
            save_state(cfg["state_file"], state)
        if args.once:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
