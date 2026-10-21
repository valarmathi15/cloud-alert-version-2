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

import os, re, json, time, secrets, datetime, threading
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

TENKAN_PERIOD, KIJUN_PERIOD, SENKOU_B_PERIOD, DISPLACEMENT = 9, 26, 52, 26
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SEGMENT_HOURS = {            # IST, Mon-Fri (exchange holidays are not covered)
    "NSE": (datetime.time(9, 15), datetime.time(15, 30)),
    "BSE": (datetime.time(9, 15), datetime.time(15, 30)),
    "MCX": (datetime.time(9, 0), datetime.time(23, 30)),
}

# Yahoo Finance tickers. BSE (except Sensex) are best guesses - verify once on Yahoo.
# MCX: Yahoo has no MCX feed, so these are the international futures (USD) used as a proxy.
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
    "MCX": {
        "Gold": "GC=F",
        "Silver": "SI=F",
        "Crude Oil": "CL=F",
        "Natural Gas": "NG=F",
        "Copper": "HG=F",
    },
}
SEGMENTS = list(INDICES_BY_SEGMENT.keys())
INDICES = {n: t for seg in INDICES_BY_SEGMENT.values() for n, t in seg.items()}
SEGMENT_OF = {n: s for s, d in INDICES_BY_SEGMENT.items() for n in d}

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
               "zones": ["above", "below"], "near_pcts": [1]}
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
    lo, hi = SEGMENT_HOURS.get(segment, SEGMENT_HOURS["NSE"])
    return t.weekday() < 5 and lo <= t.time() <= hi


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
        return round(float(yf.Ticker(ticker).fast_info["last_price"]), 2)
    except Exception:
        return None


# ---------- ntfy ----------
def active_channels():
    return ["Phone notification (ntfy)"] if get_topics() else []


def send_alert(message, with_button=True):
    """Push notification through ntfy. The 'Action taken' button posts 'action taken' to the topic."""
    topics = get_topics()

    def worker():
        for topic in topics:
            headers = {"Title": APP_TITLE, "Priority": "high"}
            if with_button:
                headers["Actions"] = (f"http, Action taken, https://ntfy.sh/{topic}, "
                                      f"method=POST, body=action taken, clear=true")
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
    """(unix time, message text) of the latest 'action taken' message published to the topic
    after `since` (this app's own notifications are ignored), else None."""
    found = None
    for topic in get_topics():
        try:
            r = requests.get(f"https://ntfy.sh/{topic}/json",
                             params={"poll": "1", "since": str(int(since))}, timeout=15)
            for line in r.text.splitlines():
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                text = str(m.get("message", ""))
                if (m.get("event") == "message" and m.get("title") != APP_TITLE
                        and m.get("time", 0) >= int(since) and "action taken" in text.lower()):
                    if not found or int(m["time"]) >= found[0]:
                        found = (int(m["time"]), text.strip())
        except Exception as e:
            print("ntfy poll error:", e)
    return found


def handle_action_taken(ts, text="action taken"):
    mutate_alerts(lambda alerts: [a.update(state="paused") for a in alerts])
    mark_action_taken(ts, text)
    bot["last_action"], bot["since"] = ts, ts + 1
    send_alert("✅ Neenga action eduthuteenga - ellaa alerts-um PAUSE aagiduchu. "
               "Dashboard-la irundhu Resume pannalaam.", with_button=False)


# ---------- scan / engine ----------
def build_block(name, tf, price, top, bottom, lines, any_match, market_open):
    out = [f"{'✅' if any_match else '❌'} {name} ({tf.upper()})",
           f"Live price: {price:,.2f}",
           f"Cloud: {bottom:,.2f} - {top:,.2f}"] + lines
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
            prices[name] = live_price(INDICES[name])
        price = prices[name]
        res = {"price": price, "top": None, "bottom": None, "error": None, "ts": start}
        if price is None:
            res["error"] = "no live price"
        else:
            try:
                top, bottom = cloud_levels(get_data(INDICES[name], tf))
                res.update(top=round(top, 2), bottom=round(bottom, 2))
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
            lines.append(f"{'✅' if m else '❌'} {label} - {'IRUKKU' if m else 'ILLA'}")
            log_entries.append({
                "sent_unix": start, "sent_at": fmt_ist(start), "index": name, "tf": tf,
                "alert": label, "zone": a["zone"], "matched": m, "price": res["price"],
                "kind": "auto", "action_unix": None, "action_at": None})
            sent_ids.append(a["id"])
        blocks.append(build_block(name, tf, res["price"], res["top"], res["bottom"], lines,
                                  any_match, in_market_hours(SEGMENT_OF[name])))
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


def engine_loop():
    """Runs for the whole life of the app: every active alert notifies every 5 minutes."""
    next_refresh, last_poll = 0, time.time()
    while True:
        try:
            time.sleep(1)
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
def ensure_engine():
    """Start the background engine once per server process (also creates the default alert)."""
    with bot["lock"]:
        if bot["engine_started"]:
            return
        bot["engine_started"] = True
        now = int(time.time())
        bot["since"], bot["last_action"] = now, now
        # fresh start (or empty / broken file): Nifty 50 default alerts
        if not load_json(ALERTS_FILE, []):
            save_json(ALERTS_FILE, [make_alert("Nifty 50", "4h", z, None) for z in ("above", "below")])
        threading.Thread(target=engine_loop, daemon=True).start()


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
        st.toast("Dashboard key neekkiyaachu - Secrets key use aagum")
        return
    items, ok = _valid_topics(raw)
    if not ok:
        st.toast("Key-la letters, numbers, - and _ mattum (max 64) use pannunga")
        return
    save_json(SETTINGS_FILE, {"ntfy_topic": ",".join(items)})
    bot["since"] = int(time.time())
    st.toast("Puthu key save aachu - phone-la ntfy app-la indha key-ku Subscribe pannunga")


def reset_topic_cb():
    save_json(SETTINGS_FILE, {"ntfy_topic": ""})
    st.session_state["ntfy_topic_input"] = SECRET_TOPIC
    bot["since"] = int(time.time())
    st.toast("Ippo Secrets-la irukkura key use aagudhu")


def random_topic_cb():
    st.session_state["ntfy_topic_input"] = "ichimoku-" + secrets.token_hex(6)
    st.toast("Random key ready - Save click pannunga")


def test_topic_cb():
    if get_topics():
        send_alert("🔔 Test notification - ntfy setup correct-ah work aagudhu!", with_button=False)
        st.toast("Test notification anuppiyaachu")
    else:
        st.toast("Munnadi key save pannunga")


def unlock_cb():
    if st.session_state.get("setup_pw") == ADMIN_PW:
        st.session_state["setup_ok"] = True
    else:
        st.toast("Password thappu")


def ntfy_setup_panel():
    st.markdown("**📲 Phone notification setup**")
    if ADMIN_PW and not st.session_state.get("setup_ok"):
        st.text_input("Password", type="password", key="setup_pw")
        st.button("Unlock", key="setup_unlock", on_click=unlock_cb)
        return
    st.markdown("**1. ntfy app install pannunga**")
    c1, c2 = st.columns(2)
    c1.link_button("Android", PLAY_URL, width="stretch")
    c2.link_button("iPhone", IOS_URL, width="stretch")
    st.markdown("**2. App-la `+` click panni, indha key-ku Subscribe pannunga**")
    topics = get_topics()
    override = load_json(SETTINGS_FILE, {}).get("ntfy_topic") or ""
    if topics:
        for t in topics:
            st.code(t, language=None)
        if override:
            st.caption("Ippo use aagurathu: **dashboard-la save pannina key** (Secrets key-a override pannudhu).")
            if SECRET_TOPIC and SECRET_TOPIC != override:
                st.caption(f"Secrets-la irukkura key `{SECRET_TOPIC}` maaraadhu. Streamlit Secrets-a app-lerundhu "
                           "maatha mudiyaadhu, adha maathanum na Streamlit Settings > Secrets-la neenga thaan maathanum.")
            st.button("↩ Secrets key use pannu", key="ntfy_reset", on_click=reset_topic_cb)
        else:
            st.caption("Ippo use aagurathu: Streamlit **Secrets**-la irukkura key.")
    else:
        st.warning("Innum key set aagala. Keela oru key kudutthu Save pannunga.")
    st.markdown("**3. Key maathanum na inga edit pannunga**")
    st.text_input("ntfy key", key="ntfy_topic_input", label_visibility="collapsed",
                  placeholder="example: ichimoku-sathya-48271")
    b = st.columns(3)
    b[0].button("💾 Save", key="ntfy_save", on_click=save_topic_cb, width="stretch")
    b[1].button("🎲 Random", key="ntfy_rand", on_click=random_topic_cb, width="stretch")
    b[2].button("🔔 Test", key="ntfy_test", on_click=test_topic_cb, width="stretch")
    st.caption("Key maathinaa phone-la puthu key-ku thirumba Subscribe pannanum (pazhaya key-a unsubscribe pannunga). "
               "Dashboard-la save pannina key app reboot aanaa azhinjidum, appo Secrets key use aagum. "
               "Permanent-ah venum na Secrets-layum same key maathunga. Comma vechu rendu key-um kudukkalaam.")


st.session_state.setdefault("ntfy_topic_input", ", ".join(get_topics()))
h_left, h_right = st.columns([5, 1.3], vertical_alignment="center")
h_left.markdown(
    "<div style='background:#131B3B;padding:18px 22px;border-radius:10px;margin-bottom:6px'>"
    "<h2 style='color:#fff;margin:0;font-size:22px'>Ichimoku Cloud Alerts &rarr; Phone Notification</h2></div>",
    unsafe_allow_html=True,
)
with h_right.popover("📲 ntfy setup", width="stretch"):
    ntfy_setup_panel()

# ---- session defaults come from the saved config; checkbox states live in session_state ----
saved = load_json(CONFIG_FILE, DEFAULT_CFG)
saved_segments = saved.get("segments") or sorted(
    {SEGMENT_OF[i] for i in saved.get("indices", []) if i in SEGMENT_OF}) or ["NSE"]
_np_saved = saved.get("near_pcts")
if not _np_saved:
    _np_saved = [saved["near_pct"]] if "near_pct" in saved else [1]
_np_saved = {float(x) for x in _np_saved}


def keep(key, default):
    st.session_state[key] = st.session_state.get(key, default)


for s_ in SEGMENTS:
    keep(f"seg_{s_}", s_ in saved_segments)
for n_ in INDICES:
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
        "indices": [n for n in INDICES if SEGMENT_OF[n] in segs and ss.get(f"idx_{n}")],
        "timeframes": [t for t in TFS if ss.get(f"tf_{t}")],
        "zones": [z for z in ZONES if ss.get(f"zn_{z}")],
        "near_pcts": [float(v) for v, _ in NEAR_OPTIONS if ss.get(f"np_{v}")],
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
    st.toast("Alerts start aagiduchu - first notification udane varum")


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
            st.toast("Munnadi list-la alerts-a select pannunga")
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
    for a in load_alerts():
        st.session_state[f"sel_{a['id']}"] = v


tab_dash, tab_log = st.tabs(["Dashboard", "Notification log"])

with tab_dash:
    with st.container(border=True):
        c_seg, c_idx, c_tf, c_zn, c_np, c_btn = st.columns([1, 1.6, 1.3, 1.6, 1.0, 1.1])

        sel_segments = check_dropdown(c_seg, "Segment", [(s, s) for s in SEGMENTS], "seg")
        idx_items = [(n, n) for n in INDICES if SEGMENT_OF[n] in sel_segments]
        if idx_items:
            sel_indices = check_dropdown(c_idx, "Index", idx_items, "idx")
        else:
            sel_indices = []
            c_idx.caption("Index")
            c_idx.popover("Select segment first", width="stretch", disabled=True)
        sel_tfs = check_dropdown(c_tf, "Timeframe", list(TFS.items()), "tf")
        sel_zones = check_dropdown(c_zn, "Cloud (alert me when price is)", list(ZONES.items()), "zn")
        check_dropdown(c_np, "Target % (Near Cloud)", NEAR_OPTIONS, "np")

        sel = current_selection()
        cfg = {k: sel[k] for k in ("segments", "indices", "timeframes", "zones", "near_pcts")}
        needs_pct = any(z in NEAR_ZONES for z in cfg["zones"])
        ok = bool(cfg["indices"] and cfg["timeframes"] and cfg["zones"]
                  and (cfg["near_pcts"] or not needs_pct))
        if cfg != saved:
            save_json(CONFIG_FILE, cfg)  # every change is saved
            if ok:  # AUTO-START: dropdown-la select pannina udane alert start (Start button click venaam)
                if _add_alerts(cfg, only_new=True):
                    st.toast("Alert start aagiduchu - first notification udane varum")

        c_btn.caption("\u00a0")
        c_btn.button("Start alerts", type="primary", width="stretch", disabled=not ok,
                     on_click=start_alerts_cb)

        if needs_pct and not cfg["near_pcts"]:
            st.warning("Near Cloud Top / Bottom select pannirukeenga - Target % (0.5 / 1 / 2 / 3) ethaavadhu onnu select pannunga.")
        if "MCX" in cfg["segments"]:
            st.caption("MCX: Yahoo-la MCX feed illa, so international futures price (USD) use aagudhu (Gold GC=F, Silver SI=F, Crude CL=F, Natural Gas NG=F, Copper HG=F).")
        if active_channels():
            st.caption("Alert channel: " + ", ".join(active_channels()) +
                       "  ·  Stop: notification-la \"Action taken\" button / ntfy-la \"action taken ...\" nu message anuppunga / keela Pause")
        else:
            st.warning("ntfy key set aagala - mela right-top \"📲 ntfy setup\"-la key kudutthu Save pannunga "
                       "(illana Secrets-la NTFY_TOPIC). Key illama notification pogaadhu.")

    @st.fragment(run_every=10)
    def alerts_panel():
        alerts = load_alerts()
        results = bot["results"]
        now = time.time()
        n_act = sum(a["state"] == "active" for a in alerts)
        mk = "  ·  ".join(f"{s} {'open' if in_market_hours(s) else 'closed'}" for s in SEGMENTS)
        st.info(f"🟢 **Alerts engine running** · Active: {n_act} · Paused: {len(alerts) - n_act} · "
                f"Last scan: {bot['last_scan'] or '-'} · {mk}")

        if not alerts:
            st.markdown(
                "<div style='border:1px dashed #D1D5DB;border-radius:10px;padding:32px;"
                "text-align:center;color:#6B7280'>Alerts onnum illa. Mela select pannunga - "
                "udane alert start aagum (<b>Start alerts</b> click panna thevai illa).</div>", unsafe_allow_html=True)
            return

        ids = [a["id"] for a in alerts]
        n_sel = sum(bool(st.session_state.get(f"sel_{i}")) for i in ids)
        st.session_state["sel_all"] = n_sel == len(ids)

        b = st.columns([1.1, 1.1, 1.3, 1.3, 1.3, 2.4])
        b[0].button("⏸ Pause all", key="b_pause_all", width="stretch", on_click=bulk_cb, args=("pause", "all"))
        b[1].button("▶ Resume all", key="b_resume_all", width="stretch", on_click=bulk_cb, args=("resume", "all"))
        b[2].button("⏸ Pause selected", key="b_pause_sel", width="stretch", on_click=bulk_cb, args=("pause", "sel"))
        b[3].button("▶ Resume selected", key="b_resume_sel", width="stretch", on_click=bulk_cb, args=("resume", "sel"))
        b[4].button("🗑 Delete selected", key="b_del_sel", width="stretch", on_click=bulk_cb, args=("delete", "sel"))
        b[5].caption(f"{n_sel} selected")

        W = [0.4, 1.3, 0.8, 1.9, 1.0, 1.8, 1.3, 1.0, 1.0, 0.5, 0.5]
        h = st.columns(W, vertical_alignment="center")
        h[0].checkbox("Select all", key="sel_all", on_change=sel_all_cb, label_visibility="collapsed")
        for c, t in zip(h[1:9], ["Index", "TF", "Alert", "Live price", "Cloud (bottom - top)",
                                 "Status", "State", "Next"]):
            c.caption(t)
        st.divider()

        order = lambda a: (list(INDICES).index(a["index"]), list(TFS).index(a["tf"]),
                           list(ZONES).index(a["zone"]), a.get("pct") or 0)
        for a in sorted(alerts, key=order):
            res = results.get(f"{a['index']}|{a['tf']}")
            c = st.columns(W, vertical_alignment="center")
            c[0].checkbox("select", key=f"sel_{a['id']}", label_visibility="collapsed")
            c[1].markdown(f"**{a['index']}**")
            c[2].write(a["tf"].upper())
            c[3].write(alert_label(a["zone"], a.get("pct")))
            if not res:
                c[4].write("-"); c[5].write("-"); c[6].write("...checking")
            elif res["error"]:
                c[4].write("-"); c[5].write("-"); c[6].markdown(":orange[data error]")
            else:
                c[4].write(f"{res['price']:,.2f}")
                c[5].write(f"{res['bottom']:,.2f} - {res['top']:,.2f}")
                m = evaluate(a["zone"], a.get("pct"), res["price"], res["top"], res["bottom"])
                c[6].markdown(":green[✅ IRUKKU]" if m else ":red[❌ ILLA]")
            paused = a["state"] != "active"
            c[7].markdown(":gray[⏸ Paused]" if paused else ":green[● Active]")
            if paused:
                c[8].write("-")
            else:
                left = int(a.get("next_due", 0) - now)
                c[8].write("now" if left <= 0 else f"{left // 60}m {left % 60:02d}s")
            c[9].button("▶" if paused else "⏸", key=f"tg_{a['id']}", on_click=toggle_pause_cb,
                        args=(a["id"],), help="Resume" if paused else "Pause")
            c[10].button("🗑", key=f"dl_{a['id']}", on_click=delete_cb, args=({a["id"]},), help="Delete")

    alerts_panel()

with tab_log:
    @st.fragment(run_every=10)
    def log_panel():
        log = load_json(LOG_FILE, [])
        any_active = any(a["state"] == "active" for a in load_alerts())
        data = []
        for e in reversed(log):
            if e.get("kind") == "manual":
                continue
            if e.get("action_at"):
                action, at = (e.get("action_text") or "Yes"), e["action_at"]
            elif any_active and e.get("sent_unix", 0) >= bot["last_action"]:
                action, at = "Pending", "-"
            else:
                action, at = "No", "-"
            if "matched" in e:
                result = "✅ IRUKKU" if e["matched"] else "❌ ILLA"
            else:
                result = "-"
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
            st.info("Innum notification poga-la. Pogum bodhu inga time, result, action taken ellam save aagi varum.")
            return
        df = pd.DataFrame(data)
        st.caption(f"{len(df)} notifications saved (newest first)")
        st.dataframe(df.style.format({"Price": lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) else v}),
                     width="stretch", hide_index=True)
        st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8"),
                           file_name="notification_log.csv", mime="text/csv", key="dl_log")

    log_panel()
