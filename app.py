"""
Ichimoku Cloud Alerts -> Phone notification (ntfy)  |  single-file Streamlit app

Run locally :  pip install -U streamlit yfinance pandas requests
               python -m streamlit run app.py
Secrets     :  NTFY_TOPIC = "your-topic-name"   (optional - the key can also be set / changed from the
               dashboard: top-right "ntfy setup")
               ADMIN_PASSWORD = "..."              (optional - locks the ntfy setup panel)

How it works
  * Every selected combination (index x timeframe x cloud zone [x target %]) becomes its own alert.
  * A background engine starts with the app and keeps notifying every 5 minutes per active alert
    (default alert on a fresh start: Nifty 50, 4H, Above Cloud + Below Cloud). No need to keep the page open
    as long as the server itself stays awake.
  * Each alert has its own Pause / Resume and Delete button. There are also Pause all / Resume all and
    multi-select Pause / Resume / Delete.
  * Tap "Action taken" on a notification (or send a message containing `action taken` to the ntfy topic)
    -> all alerts are paused and your message is shown in the log's "Action taken" column.
    ANY message you type in the ntfy chat box also stops the alerts and is shown there; the column can be edited by hand.
  * Every notification (time, status, action taken + time) is saved in notification_log.json.

Keep-awake (optional, free): `python app.py --keep-awake` opens the app in a headless browser so the free
Streamlit Cloud app does not sleep. It is used by .github/workflows/keep_awake.yml (that small config file
must stay a separate file because GitHub only reads workflows from that folder).
"""
import sys

if __name__ == "__main__" and "--keep-awake" in sys.argv:
    # ---- GitHub Actions mode: visit the app like a real visitor, then exit (Streamlit is not started) ----
    import os as _os
    import time as _time
    from playwright.sync_api import sync_playwright

    _url = _os.environ.get("APP_URL", "https://cloud-alert.streamlit.app")
    _stay = int(_os.environ.get("STAY_SECONDS", "90"))
    with sync_playwright() as _p:
        _browser = _p.chromium.launch()
        _page = _browser.new_page()
        _page.goto(_url, wait_until="domcontentloaded", timeout=120_000)
        try:  # a sleeping app shows this button
            _page.get_by_role("button", name="Yes, get this app back up!").click(timeout=15_000)
            print("App was sleeping - wake-up button clicked")
        except Exception:
            print("No wake-up button (app already awake)")
        _time.sleep(_stay)  # stay connected so the script runs and the engine thread starts
        print("Page title:", _page.title())
        _browser.close()
    sys.exit(0)

import os, re, json, time, secrets, datetime, threading, hashlib
import requests
import pandas as pd
import yfinance as yf
import streamlit as st

st.set_page_config(page_title="Ichimoku Cloud Alerts", page_icon="☁️", layout="wide")

# ============ CONFIG ============
CHECK_EVERY_SECONDS = 5 * 60     # notification gap per alert
RETRY_SECONDS = 60               # retry gap when data could not be fetched
STOP_POLL_SECONDS = 10           # how often to look for "action taken" on ntfy
CONFIG_FILE = "dashboard_config.json"   # dropdown selections
ALERTS_FILE = "alerts.json"             # the running / paused alerts
LOG_FILE = "notification_log.json"
SETTINGS_FILE = "settings.json"          # ntfy key edited from the dashboard
APP_TITLE = "Ichimoku Cloud Alert"       # title of every notification this app sends
LOG_MAX = 2000
# Buttons shown under every alert notification (max 3, no commas / semicolons in the text).
# Tapping one stops all alerts and its text is shown in the log's "Action taken" column.
ACTION_BUTTONS = ["Action taken", "1% hedged", "Portfolio 50% booked"]

TENKAN_PERIOD, KIJUN_PERIOD, SENKOU_B_PERIOD, DISPLACEMENT = 9, 26, 52, 26
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SEGMENT_HOURS = {            # IST, Mon-Fri (exchange holidays are not covered)
    "NSE": (datetime.time(9, 15), datetime.time(15, 30)),
    "BSE": (datetime.time(9, 15), datetime.time(15, 30)),
    "MCX": (datetime.time(9, 0), datetime.time(23, 30)),
    "Stocks": (datetime.time(9, 15), datetime.time(15, 30)),
    # Crypto = 24x7, Forex = Mon ~03:00 IST to Sat ~03:00 IST (handled in in_market_hours)
}

# Yahoo Finance tickers. BSE (except Sensex) are best guesses - verify once on Yahoo.
# Stocks: NSE stocks (Yahoo ticker = SYMBOL.NS). Any other NSE stock can be typed in the dashboard.
NSE_STOCKS = ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "BHARTIARTL", "ITC", "SBIN", "LT",
              "HINDUNILVR", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "M&M", "MARUTI", "SUNPHARMA",
              "HCLTECH", "NTPC", "TITAN", "ULTRACEMCO", "ONGC", "ADANIPORTS", "POWERGRID", "TATASTEEL",
              "COALINDIA", "ASIANPAINT", "BAJAJFINSV", "NESTLEIND", "JSWSTEEL", "WIPRO", "GRASIM",
              "HINDALCO", "TECHM", "ADANIENT", "CIPLA", "DRREDDY", "EICHERMOT", "HEROMOTOCO", "BPCL",
              "APOLLOHOSP", "TATACONSUM", "BRITANNIA", "INDUSINDBK", "SBILIFE", "HDFCLIFE",
              "SHRIRAMFIN", "TRENT", "BEL", "BAJAJ-AUTO", "ETERNAL", "JIOFIN"]
INDICES_BY_SEGMENT = {
    "NSE": {
        "Nifty 50": "^NSEI",
        "Nifty 200": "^CNX200",
        "Nifty 100": "^CNX100",
        "Nifty Midcap 150": "NIFTYMIDCAP150.NS",
        "Nifty Smallcap 250": "NIFTYSMLCAP250.NS",
        "Nifty Next 50": "^NSMIDCP",
    },
    "BSE": {
        "Sensex": "^BSESN",
        "BSE 100": "BSE-100.BO",
        "BSE 200": "BSE-200.BO",
        "BSE 500": "BSE-500.BO",
        "BSE Midcap": "BSE-MIDCAP.BO",
        "BSE Smallcap": "BSE-SMLCAP.BO",
    },
    # MCX: Yahoo has no MCX feed -> international futures (USD) x USDINR, converted to MCX units (see INR_FACTOR)
    "MCX": {
        "Gold": "GC=F",
        "Silver": "SI=F",
        "Crude Oil": "CL=F",
        "Natural Gas": "NG=F",
        "Copper": "HG=F",
    },
    "Stocks": {sym: f"{sym}.NS" for sym in NSE_STOCKS},
    # Crypto: USD price x USDINR = rupee value
    "Crypto": {
        "Bitcoin": "BTC-USD",
        "Ethereum": "ETH-USD",
        "BNB": "BNB-USD",
        "XRP": "XRP-USD",
        "Solana": "SOL-USD",
        "Dogecoin": "DOGE-USD",
        "Cardano": "ADA-USD",
    },
    "Forex": {
        "USD/INR": "USDINR=X",
        "EUR/INR": "EURINR=X",
        "GBP/INR": "GBPINR=X",
        "JPY/INR": "JPYINR=X",
        "EUR/USD": "EURUSD=X",
        "GBP/USD": "GBPUSD=X",
        "USD/JPY": "USDJPY=X",
        "AUD/USD": "AUDUSD=X",
        "USD/CAD": "USDCAD=X",
        "USD/CHF": "USDCHF=X",
        "EUR/JPY": "EURJPY=X",
        "GBP/JPY": "GBPJPY=X",
    },
}
SEGMENTS = list(INDICES_BY_SEGMENT.keys())
INDICES = {n: t for seg in INDICES_BY_SEGMENT.values() for n, t in seg.items()}
SEGMENT_OF = {n: s for s, d in INDICES_BY_SEGMENT.items() for n in d}

# Rupee conversion: price(INR) = USD price x USDINR x factor. Factor converts to the Indian trading unit.
USDINR = "USDINR=X"
MCX_ADJ = {}   # optional fine-tune to match the real MCX price, e.g. {"Gold": 1.05} (MCX has duty/premium)
INR_FACTOR = {
    "Gold": 10 / 31.1035 * MCX_ADJ.get("Gold", 1.0),             # USD/oz  -> INR per 10 g
    "Silver": 1000 / 31.1035 * MCX_ADJ.get("Silver", 1.0),       # USD/oz  -> INR per kg
    "Crude Oil": 1.0 * MCX_ADJ.get("Crude Oil", 1.0),            # USD/bbl -> INR per barrel
    "Natural Gas": 1.0 * MCX_ADJ.get("Natural Gas", 1.0),        # USD/mmBtu -> INR per mmBtu
    "Copper": 1 / 0.45359237 * MCX_ADJ.get("Copper", 1.0),       # USD/lb  -> INR per kg
    **{n: 1.0 for n in INDICES_BY_SEGMENT["Crypto"]},            # USD -> INR
}

TFS = {"30m": "30 Minutes", "1h": "1 Hour", "4h": "4 Hours", "1d": "1 Day"}
ZONES = {
    "above": "Above Cloud",
    "near_top": "Near Cloud Top",
    "inside": "Inside Cloud",
    "near_bottom": "Near Cloud Bottom",
    "below": "Below Cloud",
}
NEAR_ZONES = ("near_top", "near_bottom")
NEAR_OPTIONS = [("0.5", "0.5%"), ("1", "1%"), ("2", "2%"), ("3", "3%")]
DEFAULT_CFG = {"segments": ["NSE"], "indices": ["Nifty 50"], "timeframes": ["4h"],
               "zones": ["above", "below"], "near_pcts": [1], "custom_stocks": []}


def ticker_of(name):
    """Yahoo ticker of an index / commodity / stock. Unknown names = NSE stocks typed by the user."""
    return INDICES.get(name) or f"{name}.NS"


def segment_of(name):
    return SEGMENT_OF.get(name, "Stocks")


def _dec(x):
    x = abs(x)
    return 2 if x >= 100 else (3 if x >= 10 else 5)


def fmt_p(x):
    """Price text: 2 decimals for big prices, more decimals for forex / cheap prices."""
    return f"{x:,.{_dec(x)}f}"


def rnd_p(x):
    return round(x, _dec(x))


# ================================


# Shared state must survive Streamlit reruns -> cache_resource
@st.cache_resource(show_spinner=False)
def get_shared():
    return {"results": {}, "last_scan": None, "since": 0, "last_action": 0,
            "engine_started": False, "lock": threading.RLock()}


bot = get_shared()


def _secret(name, default=""):
    v = os.environ.get(name)
    if v:
        return v
    try:
        return str(st.secrets[name])
    except Exception:
        return default


SECRET_TOPIC = _secret("NTFY_TOPIC")
ADMIN_PW = _secret("ADMIN_PASSWORD")   # optional: locks the ntfy setup panel


def get_topics():
    """ntfy key(s): the one saved from the dashboard, else the NTFY_TOPIC secret."""
    raw = load_json(SETTINGS_FILE, {}).get("ntfy_topic") or SECRET_TOPIC
    return [x.strip() for x in raw.split(",") if x.strip()]


def now_ist():
    return datetime.datetime.now(IST).replace(tzinfo=None)


def fmt_ist(ts):
    return datetime.datetime.fromtimestamp(ts, IST).strftime("%d %b %H:%M:%S")


def in_market_hours(segment="NSE"):
    t = now_ist()
    wd, tm = t.weekday(), t.time()
    if segment == "Crypto":
        return True
    if segment == "Forex":     # Mon ~03:00 IST -> Sat ~03:00 IST
        return (wd in (1, 2, 3, 4) or (wd == 0 and tm >= datetime.time(3, 0))
                or (wd == 5 and tm < datetime.time(3, 0)))
    lo, hi = SEGMENT_HOURS.get(segment, SEGMENT_HOURS["NSE"])
    return wd < 5 and lo <= tm <= hi


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# ---------- alerts store (file is the source of truth; engine and UI both use it) ----------
def alert_id(name, tf, zone, pct):
    return f"{name}|{tf}|{zone}|{'-' if pct is None else format(float(pct), 'g')}"


def alert_label(zone, pct):
    return ZONES[zone] + (f" ({format(float(pct), 'g')}%)" if pct is not None else "")


def make_alert(name, tf, zone, pct):
    return {"id": alert_id(name, tf, zone, pct), "index": name, "tf": tf, "zone": zone,
            "pct": pct, "state": "active", "next_due": 0, "last_sent": None}


def load_alerts():
    return load_json(ALERTS_FILE, [])


def mutate_alerts(fn):
    """Load alerts, let fn change the list in place, save it back (thread safe)."""
    with bot["lock"]:
        alerts = load_alerts()
        fn(alerts)
        save_json(ALERTS_FILE, alerts)


def evaluate(zone, pct, price, top, bottom):
    """True if the price is currently in this zone."""
    near = (float(pct) if pct is not None else 1.0) / 100
    if zone == "above":
        return price > top
    if zone == "below":
        return price < bottom
    if zone == "inside":
        return bottom <= price <= top
    if zone == "near_top":
        return top * (1 - near) <= price <= top
    if zone == "near_bottom":
        return bottom <= price <= bottom * (1 + near)
    return False


def reach_info(zone, pct, price, top, bottom):
    """How far the price is from triggering this alert: (matched, text)."""
    near = (float(pct) if pct is not None else 1.0) / 100
    lo, hi = {                       # price band that triggers the alert (None = open-ended)
        "above": (top, None),
        "below": (None, bottom),
        "inside": (bottom, top),
        "near_top": (top * (1 - near), top),
        "near_bottom": (bottom, bottom * (1 + near)),
    }[zone]
    matched = evaluate(zone, pct, price, top, bottom)

    def move(target):
        d = target - price
        return (f"{'↑' if d > 0 else '↓'} {abs(d) / price * 100:.2f}% "
                f"({'+' if d > 0 else '-'}{fmt_p(abs(d))}) → {fmt_p(target)}")

    if matched:
        if zone == "above":
            return True, f"✅ Reached · {(price - top) / top * 100:.2f}% above top"
        if zone == "below":
            return True, f"✅ Reached · {(bottom - price) / bottom * 100:.2f}% below bottom"
        return True, "✅ Reached"
    if lo is not None and price < lo:   # price is under the zone -> needs to go up
        return False, move(lo)
    return False, move(hi)              # price is over the zone -> needs to come down


# ---------- notification log (saved to file, survives setting changes) ----------
def log_notifications(entries):
    if not entries:
        return
    with bot["lock"]:
        log = load_json(LOG_FILE, [])
        log.extend(entries)
        save_json(LOG_FILE, log[-LOG_MAX:])


def mark_action_taken(action_ts, text="action taken"):
    """'Action taken' acknowledges every notification sent since the previous acknowledgement."""
    with bot["lock"]:
        log = load_json(LOG_FILE, [])
        for e in log:
            if (e.get("kind") == "auto" and not e.get("action_unix")
                    and bot["last_action"] <= e.get("sent_unix", 0) <= action_ts + 2):
                e["action_unix"] = int(action_ts)
                e["action_at"] = fmt_ist(action_ts)
                e["action_text"] = text
        save_json(LOG_FILE, log)


# ---------- Ichimoku ----------
def compute_ichimoku(df):
    high, low = df["High"], df["Low"]
    tenkan = (high.rolling(TENKAN_PERIOD).max() + low.rolling(TENKAN_PERIOD).min()) / 2
    kijun = (high.rolling(KIJUN_PERIOD).max() + low.rolling(KIJUN_PERIOD).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(DISPLACEMENT)
    senkou_b = ((high.rolling(SENKOU_B_PERIOD).max() +
                 low.rolling(SENKOU_B_PERIOD).min()) / 2).shift(DISPLACEMENT)
    df = df.copy()
    df["senkou_a"], df["senkou_b"] = senkou_a, senkou_b
    df["cloud_top"] = df[["senkou_a", "senkou_b"]].max(axis=1)
    df["cloud_bottom"] = df[["senkou_a", "senkou_b"]].min(axis=1)
    return df


def get_data(ticker, tf):
    if tf == "1d":
        df = yf.download(ticker, period="1y", interval="1d", progress=False)
    elif tf == "30m":
        df = yf.download(ticker, period="60d", interval="30m", progress=False)
    else:  # "1h" and "4h" both start from 60m candles
        df = yf.download(ticker, period="60d", interval="60m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["High", "Low", "Close"])
    if tf == "4h":
        frames = []
        for _, day_df in df.groupby(df.index.date):
            frames.append(day_df.resample("4h", origin="start").agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna())
        df = pd.concat(frames).sort_index()
    return df


def cloud_levels(df):
    last = compute_ichimoku(df).iloc[-1]
    return float(last["cloud_top"]), float(last["cloud_bottom"])


def live_price(ticker):
    try:
        return rnd_p(float(yf.Ticker(ticker).fast_info["last_price"]))
    except Exception:
        return None


def price_of(name):
    """Live price of an alert instrument (MCX / Crypto converted to INR)."""
    p = live_price(ticker_of(name))
    if p is None or name not in INR_FACTOR:
        return p
    fx = live_price(USDINR)
    return rnd_p(p * fx * INR_FACTOR[name]) if fx else None


def data_for(name, tf):
    """Candles of an alert instrument (MCX / Crypto converted to INR with the USDINR candles)."""
    df = get_data(ticker_of(name), tf)
    if name not in INR_FACTOR:
        return df
    try:
        fx = get_data(USDINR, tf)["Close"]
        fx_tz, df_tz = getattr(fx.index, "tz", None), getattr(df.index, "tz", None)
        if fx_tz is not None and df_tz is not None:
            fx = fx.tz_convert(df_tz)
        elif fx_tz is not None:
            fx = fx.tz_localize(None)
        elif df_tz is not None:
            fx = fx.tz_localize(df_tz)
        rate = fx.reindex(df.index, method="ffill").bfill()
    except Exception:
        rate = pd.Series(live_price(USDINR) or float("nan"), index=df.index)
    f = rate * INR_FACTOR[name]
    df = df.copy()
    for c in ("Open", "High", "Low", "Close"):
        df[c] = df[c] * f
    return df.dropna(subset=["High", "Low", "Close"])


# ---------- ntfy ----------
def active_channels():
    return ["Phone notification (ntfy)"] if get_topics() else []


def send_alert(message, with_button=True):
    """Push notification through ntfy. Each ACTION_BUTTONS button posts its own text to the topic."""
    topics = get_topics()

    def worker():
        for topic in topics:
            headers = {"Title": APP_TITLE, "Priority": "high"}
            if with_button:   # ntfy allows max 3 buttons; tapping one posts its text to the topic
                headers["Actions"] = "; ".join(
                    f"http, {label}, https://ntfy.sh/{topic}, method=POST, body={label}, clear=true"
                    for label in ACTION_BUTTONS)
            try:
                r = requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                                  headers=headers, timeout=20)
                print("ntfy", topic, r.status_code)
            except Exception as e:
                print("ntfy error:", e)

    if topics:
        threading.Thread(target=worker, daemon=True).start()
    else:
        print("No ntfy key set - notification not sent")


def ntfy_stop_requested(since):
    """(unix time of the latest message, text of the message(s)) for ANY message you publish to the
    topic after `since` (typed in the ntfy app chat box or the 'Action taken' button). This app's own
    notifications (title = APP_TITLE) are ignored. Returns None when there is no such message."""
    msgs, err = {}, None
    for topic in get_topics():
        try:
            r = requests.get(f"https://ntfy.sh/{topic}/json",
                             params={"poll": "1", "since": str(int(since))}, timeout=15)
            for line in r.text.splitlines():
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                text = str(m.get("message", "")).strip()
                if (m.get("event") == "message" and m.get("title") != APP_TITLE
                        and m.get("time", 0) >= int(since) and text):
                    msgs[m.get("id") or f"{topic}-{m['time']}-{text}"] = (int(m["time"]), text)
        except Exception as e:
            err = str(e)[:80]
            print("ntfy poll error:", e)
    bot["poll_info"] = {"at": now_ist().strftime("%H:%M:%S"), "n": len(msgs), "err": err,
                        "last": (sorted(msgs.values())[-1][1][:40] if msgs else "")}
    if not msgs:
        return None
    ordered = sorted(msgs.values())
    return ordered[-1][0], " | ".join(t for _, t in ordered)


def handle_action_taken(ts, text="action taken"):
    mutate_alerts(lambda alerts: [a.update(state="paused") for a in alerts])
    mark_action_taken(ts, text)
    bot["last_action"], bot["since"] = ts, ts + 1
    send_alert("✅ Action recorded - all alerts are PAUSED. "
               "You can resume them from the dashboard.", with_button=False)


# ---------- scan / engine ----------
def build_block(name, tf, price, top, bottom, lines, any_match, market_open):
    out = [f"{'✅' if any_match else '❌'} {name} ({tf.upper()})",
           f"Live price: {fmt_p(price)}",
           f"Cloud: {fmt_p(bottom)} - {fmt_p(top)}"] + lines
    if not market_open:
        out.append("(market closed - last close price)")
    return "\n".join(out)


def run_scan(due):
    """Refresh live data for every alert's index x timeframe, and notify the `due` alerts."""
    alerts = load_alerts()
    start = int(time.time())
    prices, results, pairs = {}, {}, []
    for a in alerts:
        if (a["index"], a["tf"]) not in pairs:
            pairs.append((a["index"], a["tf"]))

    for name, tf in pairs:
        if name not in prices:
            prices[name] = price_of(name)
        price = prices[name]
        res = {"price": price, "top": None, "bottom": None, "error": None, "ts": start}
        if price is None:
            res["error"] = "no live price"
        else:
            try:
                top, bottom = cloud_levels(data_for(name, tf))
                res.update(top=rnd_p(top), bottom=rnd_p(bottom))
            except Exception as e:
                res["error"] = f"data error: {e}"
        results[f"{name}|{tf}"] = res
        print(f"[{name}|{tf}] {res}")
    bot["results"], bot["last_scan"] = results, now_ist().strftime("%d %b %H:%M:%S")

    groups = {}
    for a in due:
        groups.setdefault((a["index"], a["tf"]), []).append(a)
    blocks, log_entries, sent_ids, retry_ids = [], [], [], []
    for (name, tf), items in groups.items():
        res = results.get(f"{name}|{tf}")
        if not res or res["error"]:
            retry_ids += [a["id"] for a in items]
            continue
        lines, any_match = [], False
        for a in items:
            m = evaluate(a["zone"], a.get("pct"), res["price"], res["top"], res["bottom"])
            any_match = any_match or m
            label = alert_label(a["zone"], a.get("pct"))
            lines.append(f"{'✅' if m else '❌'} {label} - {'MET' if m else 'NOT MET'}")
            log_entries.append({
                "sent_unix": start, "sent_at": fmt_ist(start), "index": name, "tf": tf,
                "alert": label, "zone": a["zone"], "matched": m, "price": res["price"],
                "kind": "auto", "action_unix": None, "action_at": None})
            sent_ids.append(a["id"])
        blocks.append(build_block(name, tf, res["price"], res["top"], res["bottom"], lines,
                                  any_match, in_market_hours(segment_of(name))))
    if blocks and get_topics():  # one combined notification per scan
        send_alert("\n\n".join(blocks))
        log_notifications(log_entries)

    def update(al):
        for a in al:
            if a["id"] in sent_ids:
                a["next_due"], a["last_sent"] = start + CHECK_EVERY_SECONDS, start
            elif a["id"] in retry_ids:
                a["next_due"] = start + RETRY_SECONDS
    mutate_alerts(update)


def engine_loop(gen):
    """Runs for the whole life of the app: every active alert notifies every 5 minutes.
    Stops by itself when a newer code version has started a newer engine (gen changed)."""
    next_refresh, last_poll = 0, time.time()
    while True:
        try:
            time.sleep(1)
            if bot.get("engine_gen") != gen:
                return
            now = time.time()
            alerts = load_alerts()
            active = [a for a in alerts if a["state"] == "active"]
            if active and get_topics() and now - last_poll >= STOP_POLL_SECONDS:
                last_poll = now
                hit = ntfy_stop_requested(bot["since"])
                if hit:
                    handle_action_taken(*hit)
                    continue
            due = [a for a in active if a.get("next_due", 0) <= now]
            if due or (alerts and now >= next_refresh):
                run_scan(due)
                next_refresh = time.time() + CHECK_EVERY_SECONDS
        except Exception as e:
            print("engine error:", e)
            time.sleep(5)


# ====================== STREAMLIT UI ======================
try:
    with open(__file__, "rb") as _f:
        ENGINE_VERSION = hashlib.md5(_f.read()).hexdigest()
except Exception:
    ENGINE_VERSION = "dev"


def ensure_engine():
    """Start the background engine (also creates the default alert). A new code version replaces the
    engine thread of the old code version automatically."""
    with bot["lock"]:
        if bot.get("engine_version") == ENGINE_VERSION:
            return
        if bot["engine_started"] and "engine_version" not in bot:
            # an engine thread of a much older code version is running and cannot be stopped from here
            bot["stale_engine"] = True
            return
        first = not bot["engine_started"]
        bot["engine_started"] = True
        bot["engine_version"] = ENGINE_VERSION
        bot["engine_gen"] = bot.get("engine_gen", 0) + 1
        now = int(time.time())
        if first:
            bot["since"], bot["last_action"] = now, now
        # fresh start (or empty / broken file): Nifty 50 default alerts
        if not load_json(ALERTS_FILE, []):
            save_json(ALERTS_FILE, [make_alert("Nifty 50", "4h", z, None) for z in ("above", "below")])
        threading.Thread(target=engine_loop, args=(bot["engine_gen"],), daemon=True).start()


ensure_engine()

PLAY_URL = "https://play.google.com/store/apps/details?id=io.heckel.ntfy"
IOS_URL = "https://apps.apple.com/us/app/ntfy/id1625396347"


def _valid_topics(raw):
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return items, bool(items) and all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", x) for x in items)


def save_topic_cb():
    raw = st.session_state.get("ntfy_topic_input", "")
    if not raw.strip():
        save_json(SETTINGS_FILE, {"ntfy_topic": ""})       # fall back to the Secrets key
        st.toast("Dashboard key removed - the Secrets key will be used")
        return
    items, ok = _valid_topics(raw)
    if not ok:
        st.toast("Key can only contain letters, numbers, - and _ (max 64 characters)")
        return
    save_json(SETTINGS_FILE, {"ntfy_topic": ",".join(items)})
    bot["since"] = int(time.time())
    st.toast("New key saved - subscribe to it in the ntfy phone app")


def reset_topic_cb():
    save_json(SETTINGS_FILE, {"ntfy_topic": ""})
    st.session_state["ntfy_topic_input"] = SECRET_TOPIC
    bot["since"] = int(time.time())
    st.toast("Now using the key from Secrets")


def random_topic_cb():
    st.session_state["ntfy_topic_input"] = "ichimoku-" + secrets.token_hex(6)
    st.toast("Random key ready - click Save")


def test_topic_cb():
    if get_topics():
        send_alert("🔔 Test notification - your ntfy setup is working!", with_button=False)
        st.toast("Test notification sent")
    else:
        st.toast("Save a key first")


def unlock_cb():
    if st.session_state.get("setup_pw") == ADMIN_PW:
        st.session_state["setup_ok"] = True
    else:
        st.toast("Wrong password")


def ntfy_setup_panel():
    st.markdown("**📲 Phone notification setup**")
    if ADMIN_PW and not st.session_state.get("setup_ok"):
        st.text_input("Password", type="password", key="setup_pw")
        st.button("Unlock", key="setup_unlock", on_click=unlock_cb)
        return
    st.markdown("**1. Install the ntfy app**")
    c1, c2 = st.columns(2)
    c1.link_button("Android", PLAY_URL, width="stretch")
    c2.link_button("iPhone", IOS_URL, width="stretch")
    st.markdown("**2. In the app, tap `+` and subscribe to this key**")
    topics = get_topics()
    override = load_json(SETTINGS_FILE, {}).get("ntfy_topic") or ""
    if topics:
        for t in topics:
            st.code(t, language=None)
        if override:
            st.caption("Currently using: **the key saved in this dashboard** (it overrides the Secrets key).")
            if SECRET_TOPIC and SECRET_TOPIC != override:
                st.caption(f"The Secrets key `{SECRET_TOPIC}` is not changed by this. "
                           "To change it, follow step 4 below.")
            st.button("↩ Use the Secrets key", key="ntfy_reset", on_click=reset_topic_cb)
        else:
            st.caption("Currently using: the key from Streamlit **Secrets**.")
    else:
        st.warning("No key set yet. Enter a key below and click Save.")
    st.markdown("**3. Change the key here (optional)**")
    st.text_input("ntfy key", key="ntfy_topic_input", label_visibility="collapsed",
                  placeholder="example: ichimoku-sathya-48271")
    b = st.columns(3)
    b[0].button("💾 Save", key="ntfy_save", on_click=save_topic_cb, width="stretch")
    b[1].button("🎲 Random", key="ntfy_rand", on_click=random_topic_cb, width="stretch")
    b[2].button("🔔 Test", key="ntfy_test", on_click=test_topic_cb, width="stretch")
    st.caption("After changing the key, subscribe to the new key in the phone app (and unsubscribe from the old one). "
               "A key saved here is lost when the app reboots, and the Secrets key is used again. "
               "For a permanent change use step 4. You can enter two keys separated by a comma.")
    st.markdown("**4. Change the Secrets key (permanent)**")
    st.markdown("1. Open your app and click **Manage app** (bottom-right corner)\n"
                "2. Click **⋮** → **Settings** → **Secrets**\n"
                "3. Edit the line below with your new key and click **Save**")
    st.code('NTFY_TOPIC = "your-new-key"', language="toml")
    st.caption("The app restarts by itself after saving. Then subscribe to the new key in the ntfy phone app.")


st.session_state.setdefault("ntfy_topic_input", ", ".join(get_topics()))
h_left, h_right = st.columns([5, 1.3], vertical_alignment="center")
h_left.markdown(
    "<div style='background:#131B3B;padding:18px 22px;border-radius:10px;margin-bottom:6px'>"
    "<h2 style='color:#fff;margin:0;font-size:22px'>Ichimoku Cloud Alerts &rarr; Phone Notification</h2></div>",
    unsafe_allow_html=True,
)
with h_right.popover("📲 ntfy setup", width="stretch"):
    ntfy_setup_panel()
if bot.get("stale_engine"):
    st.error("An older version of the engine is still running, so new features (such as stopping alerts by "
             "typing in the chat) will not work. In Streamlit Cloud open **Manage app → ⋮ → Reboot app**.")

# ---- session defaults come from the saved config; checkbox states live in session_state ----
saved = load_json(CONFIG_FILE, DEFAULT_CFG)
saved_segments = saved.get("segments") or sorted(
    {segment_of(i) for i in saved.get("indices", [])}) or ["NSE"]
customs = [c for c in saved.get("custom_stocks", []) if c not in INDICES]   # NSE stocks typed by the user
ALL_NAMES = list(INDICES) + customs
_np_saved = saved.get("near_pcts")
if not _np_saved:
    _np_saved = [saved["near_pct"]] if "near_pct" in saved else [1]
_np_saved = {float(x) for x in _np_saved}


def keep(key, default):
    st.session_state[key] = st.session_state.get(key, default)


for s_ in SEGMENTS:
    keep(f"seg_{s_}", s_ in saved_segments)
for n_ in ALL_NAMES:
    keep(f"idx_{n_}", n_ in saved.get("indices", []))
for t_ in TFS:
    keep(f"tf_{t_}", t_ in saved.get("timeframes", []))
for z_ in ZONES:
    keep(f"zn_{z_}", z_ in saved.get("zones", []))
for v_, _ in NEAR_OPTIONS:
    keep(f"np_{v_}", float(v_) in _np_saved)


def current_selection():
    ss = st.session_state
    segs = [s for s in SEGMENTS if ss.get(f"seg_{s}")]
    return {
        "segments": segs,
        "indices": [n for n in ALL_NAMES if segment_of(n) in segs and ss.get(f"idx_{n}")],
        "timeframes": [t for t in TFS if ss.get(f"tf_{t}")],
        "zones": [z for z in ZONES if ss.get(f"zn_{z}")],
        "near_pcts": [float(v) for v, _ in NEAR_OPTIONS if ss.get(f"np_{v}")],
        "custom_stocks": customs,
    }


def _toggle_all(prefix, values):
    v = st.session_state[f"{prefix}__all"]
    for x in values:
        st.session_state[f"{prefix}_{x}"] = v


def check_dropdown(col, title, items, prefix):
    """A dropdown (popover) with a 'Select all' box and one checkbox per option."""
    values = [v for v, _ in items]
    chosen = [lbl for v, lbl in items if st.session_state[f"{prefix}_{v}"]]
    if not chosen:
        text = "Select..."
    elif len(chosen) == len(items):
        text = "All"
    elif len(chosen) <= 2:
        text = ", ".join(chosen)
    else:
        text = f"{len(chosen)} selected"
    col.caption(title)
    with col.popover(text, width="stretch"):
        st.session_state[f"{prefix}__all"] = len(chosen) == len(items)
        st.checkbox("Select all", key=f"{prefix}__all", on_change=_toggle_all, args=(prefix, values))
        st.divider()
        for v, lbl in items:
            st.checkbox(lbl, key=f"{prefix}_{v}")
    return [v for v in values if st.session_state[f"{prefix}_{v}"]]


def add_custom_cb():
    """Add any NSE stock typed by the user (e.g. IRCTC) to the Stocks list and select it."""
    ss = st.session_state
    sym = re.sub(r"\.NS$", "", str(ss.get("custom_in", "")).strip().upper())
    ss["custom_in"] = ""
    if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", sym):
        st.toast("Enter a valid NSE symbol (example: IRCTC)")
        return
    if sym not in INDICES:
        cfg = load_json(CONFIG_FILE, DEFAULT_CFG)
        cs = cfg.get("custom_stocks", [])
        if sym not in cs:
            cs.append(sym)
        cfg["custom_stocks"] = cs
        save_json(CONFIG_FILE, cfg)
    ss[f"idx_{sym}"] = True
    ss["seg_Stocks"] = True


# ---- button callbacks ----
def _add_alerts(sel, only_new=False):
    """Create alerts for every selected combination. only_new=True leaves existing alerts untouched."""
    created = []

    def add(alerts):
        by_id = {a["id"]: a for a in alerts}
        for name in sel["indices"]:
            for tf in sel["timeframes"]:
                for zone in sel["zones"]:
                    for p in (sel["near_pcts"] if zone in NEAR_ZONES else [None]):
                        aid = alert_id(name, tf, zone, p)
                        if aid in by_id:
                            if not only_new:
                                by_id[aid].update(state="active", next_due=0)
                        else:
                            alerts.append(make_alert(name, tf, zone, p))
                            by_id[aid] = alerts[-1]
                            created.append(aid)
    mutate_alerts(add)
    if created or not only_new:
        bot["since"] = int(time.time())
    return created


def start_alerts_cb():
    sel = current_selection()
    if not (sel["indices"] and sel["timeframes"] and sel["zones"]):
        return
    _add_alerts(sel)
    st.toast("Alerts started - the first notification is on its way")


def toggle_pause_cb(aid):
    resumed = []

    def f(alerts):
        for a in alerts:
            if a["id"] == aid:
                if a["state"] == "active":
                    a["state"] = "paused"
                else:
                    a.update(state="active", next_due=0)
                    resumed.append(aid)
    mutate_alerts(f)
    if resumed:
        bot["since"] = int(time.time())


def delete_cb(ids):
    mutate_alerts(lambda alerts: alerts.__setitem__(slice(None), [a for a in alerts if a["id"] not in ids]))
    for i in ids:
        st.session_state.pop(f"sel_{i}", None)


def bulk_cb(action, scope):
    alerts = load_alerts()
    if scope == "all":
        ids = [a["id"] for a in alerts]
    else:
        ids = [a["id"] for a in alerts if st.session_state.get(f"sel_{a['id']}")]
        if not ids:
            st.toast("Select alerts in the list first")
            return
    if action == "delete":
        delete_cb(set(ids))
        return

    def f(al):
        for a in al:
            if a["id"] in ids:
                if action == "pause":
                    a["state"] = "paused"
                else:
                    a.update(state="active", next_due=0)
    mutate_alerts(f)
    if action == "resume":
        bot["since"] = int(time.time())


def sel_all_cb():
    v = st.session_state["sel_all"]
    for i in st.session_state.get("_vis_ids", []):     # only the rows currently shown by the filters
        st.session_state[f"sel_{i}"] = v


FILTER_DEFAULTS = {"f_q": "", "f_seg": "all", "f_tf": "all", "f_zone": "all", "f_state": "all", "f_met": "all"}


def clear_filters_cb():
    st.session_state.update(FILTER_DEFAULTS)


tab_dash, tab_log = st.tabs(["Dashboard", "Notification log"])

with tab_dash:
    with st.container(border=True):
        c_seg, c_idx, c_tf, c_zn, c_np, c_btn = st.columns([1, 1.6, 1.3, 1.6, 1.0, 1.1])

        sel_segments = check_dropdown(c_seg, "Segment", [(s, s) for s in SEGMENTS], "seg")
        idx_items = [(n, n) for n in ALL_NAMES if segment_of(n) in sel_segments]
        if idx_items:
            sel_indices = check_dropdown(c_idx, "Index / Symbol", idx_items, "idx")
        else:
            sel_indices = []
            c_idx.caption("Index")
            c_idx.popover("Select segment first", width="stretch", disabled=True)
        sel_tfs = check_dropdown(c_tf, "Timeframe", list(TFS.items()), "tf")
        sel_zones = check_dropdown(c_zn, "Cloud (alert me when price is)", list(ZONES.items()), "zn")
        if any(z in NEAR_ZONES for z in sel_zones):   # Target % only for Near Cloud Top / Bottom
            check_dropdown(c_np, "Target % (Near Cloud)", NEAR_OPTIONS, "np")

        if "Stocks" in sel_segments:
            st.text_input("➕ Need another NSE stock? Type its symbol and press Enter (example: IRCTC, PAYTM)",
                          key="custom_in", on_change=add_custom_cb, placeholder="NSE symbol")

        sel = current_selection()
        cfg = {k: sel[k] for k in ("segments", "indices", "timeframes", "zones", "near_pcts", "custom_stocks")}
        needs_pct = any(z in NEAR_ZONES for z in cfg["zones"])
        ok = bool(cfg["indices"] and cfg["timeframes"] and cfg["zones"]
                  and (cfg["near_pcts"] or not needs_pct))
        if cfg != saved:
            save_json(CONFIG_FILE, cfg)  # every change is saved
            if ok:  # AUTO-START: alerts start as soon as you select (no need to click Start)
                if _add_alerts(cfg, only_new=True):
                    st.toast("Alert started - the first notification is on its way")

        c_btn.caption("\u00a0")
        c_btn.button("Start alerts", type="primary", width="stretch", disabled=not ok,
                     on_click=start_alerts_cb)

        if needs_pct and not cfg["near_pcts"]:
            st.warning("You selected Near Cloud Top / Bottom - please choose at least one Target % (0.5 / 1 / 2 / 3).")
        if any(x in cfg["segments"] for x in ("MCX", "Crypto")):
            st.caption("MCX / Crypto: Yahoo has no Indian feed, so the international price (USD) x USDINR = ₹ value is used "
                       "(Gold ₹/10g, Silver ₹/kg, Crude ₹/barrel, Natural Gas ₹/mmBtu, Copper ₹/kg). "
                       "Indian exchange premium and duty are not included, so prices can differ slightly from the real MCX price.")
        if active_channels():
            st.caption("Alert channel: " + ", ".join(active_channels()) +
                       "  ·  Stop: tap a button on the notification / type anything in the ntfy chat (your message is shown in the log) / or use Pause below")
        else:
            st.warning("ntfy key not set - open \"📲 ntfy setup\" (top-right), enter a key and click Save "
                       "(or set NTFY_TOPIC in Secrets). No notifications are sent without a key.")

    @st.fragment(run_every=10)
    def alerts_panel():
        alerts = load_alerts()
        results = bot["results"]
        now = time.time()
        n_act = sum(a["state"] == "active" for a in alerts)
        mk = "  ·  ".join(f"{s} {'open' if in_market_hours(s) else 'closed'}" for s in SEGMENTS)
        st.info(f"🟢 **Alerts engine running** · Active: {n_act} · Paused: {len(alerts) - n_act} · "
                f"Last scan: {bot['last_scan'] or '-'} · {mk}")
        pi = bot.get("poll_info")
        if pi and n_act:
            st.caption(f"💬 Chat check: {pi['at']} · messages you sent: {pi['n']}"
                       + (f" · last: \"{pi['last']}\"" if pi["last"] else "")
                       + (f" · ⚠ error: {pi['err']}" if pi["err"] else ""))

        if not alerts:
            st.markdown(
                "<div style='border:1px dashed #D1D5DB;border-radius:10px;padding:32px;"
                "text-align:center;color:#6B7280'>No alerts yet. Select above - alerts start automatically "
                "(no need to click <b>Start alerts</b>).</div>", unsafe_allow_html=True)
            return

        # ---- filters: find an alert quickly when many indices / stocks are selected ----
        for k_, v_ in FILTER_DEFAULTS.items():
            st.session_state.setdefault(k_, v_)
        fc = st.columns([2.4, 1.2, 1.3, 1.5, 1.1, 1.1, 0.9], vertical_alignment="bottom")
        fc[0].text_input("Search", key="f_q", placeholder="🔍 Search index / stock (e.g. reliance, gold)")
        fc[1].selectbox("Segment", ["all"] + SEGMENTS, key="f_seg",
                        format_func=lambda x: "All" if x == "all" else x)
        fc[2].selectbox("Timeframe", ["all"] + list(TFS), key="f_tf",
                        format_func=lambda x: "All" if x == "all" else TFS[x])
        fc[3].selectbox("Alert type", ["all"] + list(ZONES), key="f_zone",
                        format_func=lambda x: "All" if x == "all" else ZONES[x])
        fc[4].selectbox("State", ["all", "active", "paused"], key="f_state",
                        format_func=lambda x: "All" if x == "all" else x.capitalize())
        fc[5].selectbox("Status", ["all", "met", "not_met"], key="f_met",
                        format_func=lambda x: {"all": "All", "met": "MET", "not_met": "NOT MET"}[x])
        filt_on = any(st.session_state[k_] != v_ for k_, v_ in FILTER_DEFAULTS.items())
        fc[6].button("✖ Clear", key="f_clear", on_click=clear_filters_cb, disabled=not filt_on, width="stretch")

        def _met(a):
            r_ = results.get(f"{a['index']}|{a['tf']}")
            if not r_ or r_["error"]:
                return None
            return evaluate(a["zone"], a.get("pct"), r_["price"], r_["top"], r_["bottom"])

        ss_ = st.session_state
        q_ = ss_["f_q"].strip().lower()
        visible = [a for a in alerts
                   if (not q_ or q_ in a["index"].lower())
                   and (ss_["f_seg"] == "all" or segment_of(a["index"]) == ss_["f_seg"])
                   and (ss_["f_tf"] == "all" or a["tf"] == ss_["f_tf"])
                   and (ss_["f_zone"] == "all" or a["zone"] == ss_["f_zone"])
                   and (ss_["f_state"] == "all" or (a["state"] == "active") == (ss_["f_state"] == "active"))
                   and (ss_["f_met"] == "all" or _met(a) is (ss_["f_met"] == "met"))]

        ids = [a["id"] for a in visible]
        ss_["_vis_ids"] = ids
        n_sel = sum(bool(st.session_state.get(f"sel_{i}")) for i in ids)
        st.session_state["sel_all"] = bool(ids) and n_sel == len(ids)

        b = st.columns([1.1, 1.1, 1.3, 1.3, 1.3, 2.4])
        b[0].button("⏸ Pause all", key="b_pause_all", width="stretch", on_click=bulk_cb, args=("pause", "all"))
        b[1].button("▶ Resume all", key="b_resume_all", width="stretch", on_click=bulk_cb, args=("resume", "all"))
        b[2].button("⏸ Pause selected", key="b_pause_sel", width="stretch", on_click=bulk_cb, args=("pause", "sel"))
        b[3].button("▶ Resume selected", key="b_resume_sel", width="stretch", on_click=bulk_cb, args=("resume", "sel"))
        b[4].button("🗑 Delete selected", key="b_del_sel", width="stretch", on_click=bulk_cb, args=("delete", "sel"))
        b[5].caption(f"{n_sel} selected · showing {len(visible)} of {len(alerts)} alerts")
        if not visible:
            st.info("No alerts match these filters. Click **✖ Clear** to see all alerts.")
            return

        W = [0.4, 1.3, 0.8, 1.9, 1.0, 1.8, 1.3, 2.4, 1.0, 1.0, 0.5, 0.5]
        h = st.columns(W, vertical_alignment="center")
        h[0].checkbox("Select all", key="sel_all", on_change=sel_all_cb, label_visibility="collapsed")
        for c, t in zip(h[1:10], ["Index", "TF", "Alert", "Live price", "Cloud (bottom - top)",
                                  "Status", "To reach (move needed → price)", "State", "Next"]):
            c.caption(t)
        st.divider()

        order = lambda a: (ALL_NAMES.index(a["index"]) if a["index"] in ALL_NAMES else len(ALL_NAMES), list(TFS).index(a["tf"]),
                           list(ZONES).index(a["zone"]), a.get("pct") or 0)
        for a in sorted(visible, key=order):
            res = results.get(f"{a['index']}|{a['tf']}")
            c = st.columns(W, vertical_alignment="center")
            c[0].checkbox("select", key=f"sel_{a['id']}", label_visibility="collapsed")
            c[1].markdown(f"**{a['index']}**")
            c[2].write(a["tf"].upper())
            c[3].write(alert_label(a["zone"], a.get("pct")))
            if not res:
                c[4].write("-"); c[5].write("-"); c[6].write("...checking"); c[7].write("-")
            elif res["error"]:
                c[4].write("-"); c[5].write("-"); c[6].markdown(":orange[data error]"); c[7].write("-")
            else:
                c[4].write(fmt_p(res["price"]))
                c[5].write(f"{fmt_p(res['bottom'])} - {fmt_p(res['top'])}")
                m, reach = reach_info(a["zone"], a.get("pct"), res["price"], res["top"], res["bottom"])
                c[6].markdown(":green[✅ MET]" if m else ":red[❌ NOT MET]")
                c[7].markdown(f":green[{reach}]" if m else f":orange[{reach}]")
            paused = a["state"] != "active"
            c[8].markdown(":gray[⏸ Paused]" if paused else ":green[● Active]")
            if paused:
                c[9].write("-")
            else:
                left = int(a.get("next_due", 0) - now)
                c[9].write("now" if left <= 0 else f"{left // 60}m {left % 60:02d}s")
            c[10].button("▶" if paused else "⏸", key=f"tg_{a['id']}", on_click=toggle_pause_cb,
                        args=(a["id"],), help="Resume" if paused else "Pause")
            c[11].button("🗑", key=f"dl_{a['id']}", on_click=delete_cb, args=({a["id"]},), help="Delete")

    alerts_panel()

def _edit_log_cb():
    """Save manual edits of the 'Action taken' column back to notification_log.json."""
    ss = st.session_state
    ver = ss.get("log_ver", 0)
    edits = (ss.get(f"log_editor_{ver}") or {}).get("edited_rows", {})
    idx_map = ss.get("_log_map", [])
    if not edits:
        return
    with bot["lock"]:
        log = load_json(LOG_FILE, [])
        for pos, changes in edits.items():
            pos = int(pos)
            if "Action taken" not in changes or pos >= len(idx_map):
                continue
            k = idx_map[pos]
            if k >= len(log):
                continue
            txt = str(changes["Action taken"] or "").strip()
            if txt in ("", "-", "No", "Pending"):          # cleared -> back to not-acknowledged
                for f in ("action_unix", "action_at", "action_text"):
                    log[k].pop(f, None)
            else:
                ts = int(time.time())
                if not log[k].get("action_unix"):
                    log[k]["action_unix"], log[k]["action_at"] = ts, fmt_ist(ts)
                log[k]["action_text"] = txt
        save_json(LOG_FILE, log)
    ss["log_ver"] = ver + 1   # fresh editor so it shows the saved values


with tab_log:
    @st.fragment(run_every=10)
    def log_panel():
        log = load_json(LOG_FILE, [])
        any_active = any(a["state"] == "active" for a in load_alerts())
        data, idx_map = [], []
        for k in range(len(log) - 1, -1, -1):
            e = log[k]
            if e.get("kind") == "manual":
                continue
            if e.get("action_at"):
                action, at = (e.get("action_text") or "Yes"), e["action_at"]
            elif any_active and e.get("sent_unix", 0) >= bot["last_action"]:
                action, at = "Pending", "-"
            else:
                action, at = "No", "-"
            if "matched" in e:
                result = "✅ MET" if e["matched"] else "❌ NOT MET"
            else:
                result = "-"
            idx_map.append(k)
            data.append({
                "Sent at (IST)": e.get("sent_at", ""),
                "Index": e.get("index", ""),
                "Timeframe": str(e.get("tf", "")).upper(),
                "Alert": e.get("alert") or e.get("status") or ZONES.get(e.get("zone"), e.get("zone")),
                "Result": result,
                "Price": e.get("price"),
                "Action taken": action,
                "Action time (IST)": at,
            })
        if not data:
            st.info("No notifications sent yet. Time, result and action taken will be saved here as they go out.")
            return
        df = pd.DataFrame(data)
        st.session_state["_log_map"] = idx_map
        st.caption(f"{len(df)} notifications saved (newest first) · double-click an \"Action taken\" cell "
                   f"to edit it by hand (Enter = save)")
        st.data_editor(
            df, width="stretch", hide_index=True,
            key=f"log_editor_{st.session_state.get('log_ver', 0)}",
            disabled=[c for c in df.columns if c != "Action taken"],
            column_config={"Price": st.column_config.NumberColumn("Price", format="%.2f"),
                           "Action taken": st.column_config.TextColumn("Action taken", width="medium")},
            on_change=_edit_log_cb)
        st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8"),
                           file_name="notification_log.csv", mime="text/csv", key="dl_log")

    log_panel()
