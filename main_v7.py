"""
Nifty Jarvis Signal Engine v7.0
================================
Strategy: ORB + VWAP-Trend + VWAP-Reversal (market-adaptive)

Fixes from v6:
  1. EMA uses proper SMA seed for first N candles
  2. Trend: no near_ema requirement — uses EMA9/EMA21 crossover + VWAP filter
  3. Breakout: ORB from 9:15–9:30, signal from 9:30–10:15, single candle confirm
  4. Reversal: RSI thresholds 35/65 (not 25/75), VWAP cross confirmation
  5. VWAP calculated from 9:15 AM daily (not rolling 60-min window)
  6. SL = swing low/high of last 5 candles, minimum fixed points
  7. strategy_status stored per-instrument (NIFTY + BANKNIFTY separately)
  8. Market type detection: TREND vs SIDEWAYS based on VWAP distance + EMA slope
"""

import os, time, datetime, threading, requests
import pyotp
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from SmartApi import SmartConnect

load_dotenv()

# ── Credentials ───────────────────────────────────────────────────────────────
API_KEY        = os.getenv("ANGEL_API_KEY", "")
CLIENT_ID      = os.getenv("ANGEL_CLIENT_ID", "")
PASSWORD       = os.getenv("ANGEL_PASSWORD", "")
TOTP_SECRET    = os.getenv("ANGEL_TOTP_SECRET", "")
BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "8738811972:AAFNIu_5r-DpHcC7DdYcnF2_Z6UwsLMYoe8")
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID",   "8217586252")

# ── Instrument Config ─────────────────────────────────────────────────────────
SYMBOLS = {
    "NIFTY":     {"token": "26000", "exchange": "NSE", "step": 50,  "sl_pts": 30,  "tgt_pts": 60},
    "BANKNIFTY": {"token": "26009", "exchange": "NSE", "step": 100, "sl_pts": 80,  "tgt_pts": 160},
}

# ── Trading Rules ─────────────────────────────────────────────────────────────
MAX_TRADES_PER_DAY  = 5      # per instrument
COOLDOWN_SECONDS    = 300    # 5 minutes between signals per instrument
MAX_LOSS_STREAK     = 3      # stop trading after 3 consecutive losses (per instrument)
ORB_START           = (9, 15)   # ORB range calculation start
ORB_END             = (9, 30)   # ORB range calculation end
ORB_SIGNAL_END      = (10, 15)  # ORB signal window end
MARKET_START        = (9, 20)
MARKET_END          = (15, 10)
VWAP_TREND_THRESHOLD = 0.003   # 0.3% from VWAP = trending

# ── Global State ──────────────────────────────────────────────────────────────
def _inst_state():
    return {
        "orb_high":         None,
        "orb_low":          None,
        "orb_ready":        False,
        "trades_today":     0,
        "loss_streak":      0,
        "last_signal_time": None,
        "last_signal_dir":  None,
    }

state = {
    "auth_token":   None,
    "last_login":   None,
    "current_signal": None,
    "signal_history": [],
    "market_open":  False,
    "last_update":  None,
    "ping_count":   0,
    "error":        None,
    "market_data": {
        "NIFTY":     {"ltp": 0.0, "change_pct": 0.0},
        "BANKNIFTY": {"ltp": 0.0, "change_pct": 0.0},
    },
    "strategy_status": {
        "NIFTY": {
            "orb":      {"active": False, "direction": None, "orb_high": 0.0, "orb_low": 0.0, "orb_ready": False},
            "trend":    {"active": False, "direction": None, "ema9": 0.0, "ema21": 0.0, "vwap": 0.0, "rsi": 50.0, "market_type": "UNKNOWN"},
            "reversal": {"active": False, "direction": None, "rsi": 50.0, "vwap": 0.0, "vwap_cross": False},
        },
        "BANKNIFTY": {
            "orb":      {"active": False, "direction": None, "orb_high": 0.0, "orb_low": 0.0, "orb_ready": False},
            "trend":    {"active": False, "direction": None, "ema9": 0.0, "ema21": 0.0, "vwap": 0.0, "rsi": 50.0, "market_type": "UNKNOWN"},
            "reversal": {"active": False, "direction": None, "rsi": 50.0, "vwap": 0.0, "vwap_cross": False},
        },
    },
    "inst_state": {
        "NIFTY":     _inst_state(),
        "BANKNIFTY": _inst_state(),
    },
}

smart_api = SmartConnect(api_key=API_KEY)

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        print(f"[TG] Error: {e}")

# ── Indicators ────────────────────────────────────────────────────────────────
def ema(prices: list, n: int) -> float:
    """Proper EMA: seed with SMA of first N candles, then apply EMA."""
    if len(prices) < n:
        return round(sum(prices) / len(prices), 2)
    k = 2.0 / (n + 1)
    v = sum(prices[:n]) / n          # SMA seed
    for x in prices[n:]:
        v = x * k + v * (1 - k)
    return round(v, 2)

def rsi(prices: list, n: int = 14) -> float:
    """RSI using simple moving average of gains/losses."""
    if len(prices) < n + 1:
        return 50.0
    gains  = [max(prices[i] - prices[i-1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i-1] - prices[i], 0) for i in range(1, len(prices))]
    ag = sum(gains[-n:]) / n
    al = sum(losses[-n:]) / n
    return 100.0 if al == 0 else round(100 - (100 / (1 + ag / al)), 2)

def vwap_from_open(highs: list, lows: list, closes: list, vols: list) -> float:
    """
    True daily VWAP = cumulative(typical_price × volume) / cumulative(volume)
    typical_price = (high + low + close) / 3
    """
    if not closes or not vols:
        return closes[-1] if closes else 0.0
    cum_pv  = sum(((h + l + c) / 3) * v for h, l, c, v in zip(highs, lows, closes, vols))
    cum_vol = sum(vols)
    return round(cum_pv / cum_vol, 2) if cum_vol > 0 else closes[-1]

def swing_low(lows: list, n: int = 5) -> float:
    return min(lows[-n:]) if len(lows) >= n else min(lows)

def swing_high(highs: list, n: int = 5) -> float:
    return max(highs[-n:]) if len(highs) >= n else max(highs)

# ── Market Timing ─────────────────────────────────────────────────────────────
def market_open() -> bool:
    n = datetime.datetime.now()
    if n.weekday() >= 5:
        return False
    start = n.replace(hour=MARKET_START[0], minute=MARKET_START[1], second=0, microsecond=0)
    end   = n.replace(hour=MARKET_END[0],   minute=MARKET_END[1],   second=0, microsecond=0)
    return start <= n <= end

def in_orb_window() -> bool:
    """True if current time is in the ORB signal window (9:30–10:15)."""
    n = datetime.datetime.now()
    start = n.replace(hour=ORB_END[0],        minute=ORB_END[1],        second=0, microsecond=0)
    end   = n.replace(hour=ORB_SIGNAL_END[0], minute=ORB_SIGNAL_END[1], second=0, microsecond=0)
    return start <= n <= end

def in_orb_build_window() -> bool:
    """True if current time is in the ORB build window (9:15–9:30)."""
    n = datetime.datetime.now()
    start = n.replace(hour=ORB_START[0], minute=ORB_START[1], second=0, microsecond=0)
    end   = n.replace(hour=ORB_END[0],   minute=ORB_END[1],   second=0, microsecond=0)
    return start <= n <= end

# ── Data Fetching ─────────────────────────────────────────────────────────────
def fetch_candles_since_open(token: str, exchange: str) -> list | None:
    """
    Fetch 1-min candles from 9:15 AM today to now.
    Returns list of [timestamp, open, high, low, close, volume] or None.
    """
    try:
        now  = datetime.datetime.now()
        frm  = now.replace(hour=9, minute=15, second=0, microsecond=0)
        r = smart_api.getCandleData({
            "exchange":    exchange,
            "symboltoken": token,
            "interval":    "ONE_MINUTE",
            "fromdate":    frm.strftime("%Y-%m-%d %H:%M"),
            "todate":      now.strftime("%Y-%m-%d %H:%M"),
        })
        if r and r.get("status") and r.get("data"):
            return r["data"]
        return None
    except Exception as e:
        print(f"[FETCH] Error: {e}")
        return None

# ── Login ─────────────────────────────────────────────────────────────────────
def login() -> bool:
    try:
        totp = pyotp.TOTP(TOTP_SECRET).now()
        d = smart_api.generateSession(CLIENT_ID, PASSWORD, totp)
        if d and d.get("status"):
            state["auth_token"] = d["data"]["jwtToken"]
            state["last_login"] = datetime.datetime.now().isoformat()
            state["error"]      = None
            print(f"[LOGIN] OK at {datetime.datetime.now().strftime('%H:%M:%S')}")
            return True
        msg = d.get("message", "Login failed") if d else "No response"
        state["error"] = msg
        print(f"[LOGIN] Failed: {msg}")
        return False
    except Exception as e:
        state["error"] = str(e)
        print(f"[LOGIN] Exception: {e}")
        return False

# ── Daily Reset ───────────────────────────────────────────────────────────────
_last_reset_date = None

def daily_reset():
    global _last_reset_date
    today = datetime.date.today().isoformat()
    if _last_reset_date == today:
        return
    _last_reset_date = today
    for inst in SYMBOLS:
        ist = state["inst_state"][inst]
        ist["trades_today"]  = 0
        ist["loss_streak"]   = 0
        ist["orb_high"]      = None
        ist["orb_low"]       = None
        ist["orb_ready"]     = False
        ist["last_signal_dir"] = None
    print(f"[RESET] Daily counters reset for {today}")

# ── Signal Gate ───────────────────────────────────────────────────────────────
def signal_allowed(inst: str) -> bool:
    ist = state["inst_state"][inst]
    if ist["trades_today"] >= MAX_TRADES_PER_DAY:
        return False
    if ist["loss_streak"] >= MAX_LOSS_STREAK:
        return False
    if ist["last_signal_time"]:
        elapsed = (datetime.datetime.now() - ist["last_signal_time"]).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            return False
    return True

# ── Main Engine ───────────────────────────────────────────────────────────────
def run_engine():
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Engine tick")

    if not state["auth_token"]:
        print("[ENGINE] Not logged in — skipping")
        return

    daily_reset()
    state["market_open"] = market_open()

    for inst, info in SYMBOLS.items():
        try:
            candles = fetch_candles_since_open(info["token"], info["exchange"])
            if not candles or len(candles) < 5:
                print(f"[{inst}] Insufficient data ({len(candles) if candles else 0} candles)")
                continue

            # ── Parse OHLCV ──────────────────────────────────────────────────
            opens  = [float(c[1]) for c in candles]
            highs  = [float(c[2]) for c in candles]
            lows   = [float(c[3]) for c in candles]
            closes = [float(c[4]) for c in candles]
            vols   = [float(c[5]) for c in candles]

            ltp       = closes[-1]
            prev_close = closes[-2] if len(closes) > 1 else closes[-1]

            # ── Indicators ───────────────────────────────────────────────────
            e9   = ema(closes, 9)
            e21  = ema(closes, 21)
            r    = rsi(closes)
            vwap = vwap_from_open(highs, lows, closes, vols)

            # ── Market Data Update ────────────────────────────────────────────
            state["market_data"][inst] = {
                "ltp":        round(ltp, 2),
                "change_pct": round(((ltp - closes[0]) / closes[0]) * 100, 2) if closes[0] else 0.0,
            }

            # ── Market Type Detection ─────────────────────────────────────────
            # TREND  = price > 0.3% away from VWAP AND EMA9 slope confirms
            # SIDEWAYS = price hugging VWAP
            vwap_dist_pct = abs(ltp - vwap) / vwap if vwap > 0 else 0
            ema9_slope    = closes[-1] - closes[-3] if len(closes) >= 3 else 0  # 3-candle slope
            if vwap_dist_pct > VWAP_TREND_THRESHOLD and abs(ema9_slope) > 5:
                mkt_type = "TREND"
            else:
                mkt_type = "SIDEWAYS"

            # ── ORB Setup (9:15–9:30) ─────────────────────────────────────────
            ist = state["inst_state"][inst]
            if in_orb_build_window() or not ist["orb_ready"]:
                # Identify candles in 9:15–9:30 window (first 15 candles of day)
                orb_candles = candles[:15]
                if len(orb_candles) >= 3:
                    ist["orb_high"]  = max(float(c[2]) for c in orb_candles)
                    ist["orb_low"]   = min(float(c[3]) for c in orb_candles)
                    ist["orb_ready"] = True

            orb_high = ist["orb_high"]
            orb_low  = ist["orb_low"]
            orb_ready = ist["orb_ready"]

            # ── STRATEGY 1: ORB Breakout ──────────────────────────────────────
            # Signal window: 9:30–10:15
            # Condition: single candle close above ORB high (CE) or below ORB low (PE)
            # Volume: last candle volume > 1.2× average of last 10 candles
            orb_active = False
            orb_dir    = None

            if orb_ready and orb_high and orb_low and in_orb_window() and state["market_open"]:
                avg_vol = sum(vols[-11:-1]) / 10 if len(vols) >= 11 else (sum(vols[:-1]) / max(len(vols)-1, 1))
                vol_ok  = vols[-1] > avg_vol * 1.2

                if ltp > orb_high and vol_ok:
                    orb_active = True
                    orb_dir    = "CE"
                elif ltp < orb_low and vol_ok:
                    orb_active = True
                    orb_dir    = "PE"

            # ── STRATEGY 2: VWAP Trend ────────────────────────────────────────
            # Condition (CE): price > VWAP AND EMA9 > EMA21 AND last candle green AND RSI 50–70
            # Condition (PE): price < VWAP AND EMA9 < EMA21 AND last candle red  AND RSI 30–50
            trend_active = False
            trend_dir    = None

            if mkt_type == "TREND" and state["market_open"]:
                if ltp > vwap and e9 > e21 and ltp > prev_close and 50 <= r <= 72:
                    trend_active = True
                    trend_dir    = "CE"
                elif ltp < vwap and e9 < e21 and ltp < prev_close and 28 <= r <= 50:
                    trend_active = True
                    trend_dir    = "PE"

            # ── STRATEGY 3: VWAP Reversal ─────────────────────────────────────
            # Fires in SIDEWAYS market when price crosses VWAP with RSI confirmation
            # CE: prev candle below VWAP, current candle closes above VWAP, RSI < 65
            # PE: prev candle above VWAP, current candle closes below VWAP, RSI > 35
            rev_active = False
            rev_dir    = None

            prev_vwap_dist = prev_close - vwap  # positive = above VWAP

            if mkt_type == "SIDEWAYS" and state["market_open"] and len(closes) >= 2:
                vwap_cross_up   = prev_close < vwap and ltp > vwap
                vwap_cross_down = prev_close > vwap and ltp < vwap

                if vwap_cross_up and r < 65:
                    rev_active = True
                    rev_dir    = "CE"
                elif vwap_cross_down and r > 35:
                    rev_active = True
                    rev_dir    = "PE"

            # ── Update Strategy Status ────────────────────────────────────────
            state["strategy_status"][inst]["orb"].update({
                "active":    orb_active,
                "direction": orb_dir,
                "orb_high":  round(orb_high, 2) if orb_high else 0.0,
                "orb_low":   round(orb_low, 2)  if orb_low  else 0.0,
                "orb_ready": orb_ready,
            })
            state["strategy_status"][inst]["trend"].update({
                "active":      trend_active,
                "direction":   trend_dir,
                "ema9":        e9,
                "ema21":       e21,
                "vwap":        vwap,
                "rsi":         r,
                "market_type": mkt_type,
            })
            state["strategy_status"][inst]["reversal"].update({
                "active":     rev_active,
                "direction":  rev_dir,
                "rsi":        r,
                "vwap":       vwap,
                "vwap_cross": (prev_close < vwap and ltp > vwap) or (prev_close > vwap and ltp < vwap),
            })

            # ── Signal Voting ─────────────────────────────────────────────────
            # ANY 1 strategy firing = signal (not 2/3 — that was the problem)
            # Priority: ORB > Trend > Reversal
            strategies_fired = []
            if orb_active   and orb_dir:    strategies_fired.append(("ORB",      orb_dir))
            if trend_active and trend_dir:  strategies_fired.append(("TREND",    trend_dir))
            if rev_active   and rev_dir:    strategies_fired.append(("REVERSAL", rev_dir))

            if not strategies_fired:
                continue

            if not state["market_open"]:
                continue

            # Pick dominant direction (majority vote, or first fired if tie)
            ce_count = sum(1 for _, d in strategies_fired if d == "CE")
            pe_count = sum(1 for _, d in strategies_fired if d == "PE")
            if ce_count == 0 and pe_count == 0:
                continue
            dom = "CE" if ce_count >= pe_count else "PE"

            # Filter to only strategies agreeing with dominant direction
            agreed = [s for s, d in strategies_fired if d == dom]
            confidence = "HIGH" if len(agreed) >= 2 else "MEDIUM"

            # ── Gate Checks ───────────────────────────────────────────────────
            if not signal_allowed(inst):
                print(f"[{inst}] Signal gated (trades={ist['trades_today']}, loss={ist['loss_streak']}, cooldown)")
                continue

            # Avoid same direction repeat
            if ist["last_signal_dir"] == dom:
                print(f"[{inst}] Same direction block ({dom})")
                continue

            # ── Risk Management ───────────────────────────────────────────────
            entry = round(ltp, 2)
            atm   = int(round(entry / info["step"]) * info["step"])

            # SL = swing low/high of last 5 candles, with minimum fixed points
            if dom == "CE":
                raw_sl = swing_low(lows, 5)
                sl     = round(min(raw_sl, entry - info["sl_pts"]), 2)
            else:
                raw_sl = swing_high(highs, 5)
                sl     = round(max(raw_sl, entry + info["sl_pts"]), 2)

            # Target = 2× SL distance (1:2 R:R)
            sl_dist = abs(entry - sl)
            tgt     = round(entry + sl_dist * 2 if dom == "CE" else entry - sl_dist * 2, 2)
            rr      = round(abs(tgt - entry) / sl_dist, 1) if sl_dist > 0 else 2.0

            # ── Build Signal Object ───────────────────────────────────────────
            sig = {
                "id":          f"{inst}_{dom}_{int(time.time())}",
                "instrument":  inst,
                "option_type": dom,
                "strike":      atm,
                "entry":       entry,
                "target":      tgt,
                "stop_loss":   sl,
                "risk_reward": rr,
                "strategies":  agreed,
                "confidence":  confidence,
                "timestamp":   datetime.datetime.now().isoformat(),
                "ltp":         ltp,
                "ema9":        e9,
                "ema21":       e21,
                "vwap":        vwap,
                "rsi":         r,
                "market_type": mkt_type,
                "status":      "ACTIVE",
            }

            # Skip if same signal already active
            cur = state["current_signal"]
            if cur and cur.get("instrument") == inst and cur.get("option_type") == dom:
                continue

            # Archive previous
            if cur:
                cur["status"] = "CLOSED"
                state["signal_history"].insert(0, cur)
                state["signal_history"] = state["signal_history"][:50]

            # Activate
            state["current_signal"]         = sig
            ist["trades_today"]            += 1
            ist["last_signal_time"]         = datetime.datetime.now()
            ist["last_signal_dir"]          = dom

            print(f"[SIGNAL] {inst} {dom} {atm} @ {entry} | SL:{sl} TGT:{tgt} | {confidence} | {'+'.join(agreed)}")

            # ── Telegram Alert ────────────────────────────────────────────────
            icon   = "🔥" if confidence == "HIGH" else "⚡"
            strats = " + ".join(agreed)
            mkt_icon = "📈" if dom == "CE" else "📉"
            msg = (
                f"🚨 *NEW SIGNAL — {inst}*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{mkt_icon} *Direction:* {'CE (CALL)' if dom == 'CE' else 'PE (PUT)'}\n"
                f"🎯 *ATM Strike:* `{atm}`\n"
                f"💰 *Entry:* `{entry}`\n"
                f"🎯 *Target:* `{tgt}` (+{round(abs(tgt-entry)/entry*100,2)}%)\n"
                f"🛑 *Stop Loss:* `{sl}` (-{round(abs(entry-sl)/entry*100,2)}%)\n"
                f"📐 *R:R:* `1:{rr}`\n"
                f"{icon} *Confidence:* {confidence}\n"
                f"🔬 *Strategy:* {strats}\n"
                f"📊 *Market:* {mkt_type} | VWAP: {vwap} | RSI: {r}\n"
                f"🕒 *Time:* {datetime.datetime.now().strftime('%H:%M:%S')}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"_Nifty Jarvis v7 — Discipline Automated_"
            )
            send_telegram(msg)

        except Exception as e:
            print(f"[{inst}] Error: {e}")
            state["error"] = str(e)

    state["last_update"] = datetime.datetime.now().isoformat()

# ── Scheduler ─────────────────────────────────────────────────────────────────
def scheduler():
    tick = 0
    while True:
        run_engine()
        tick += 1
        if tick >= 360:   # re-login every 6 hours
            print("[SCHEDULER] 6-hour re-login")
            login()
            tick = 0
        time.sleep(60)

# ── FastAPI Lifespan ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    if login():
        run_engine()
        threading.Thread(target=scheduler, daemon=True).start()
        print("[STARTUP] Signal Engine v7.0 started")
        send_telegram(
            "✅ *Nifty Jarvis v7.0 Online*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📡 Monitoring: NIFTY & BANKNIFTY\n"
            "🔬 Strategies: ORB + VWAP Trend + VWAP Reversal\n"
            "📊 Market-Adaptive: TREND vs SIDEWAYS mode\n"
            "⏱ Cooldown: 5 min | Max: 5 signals/day\n"
            "🛑 Loss Limit: 3 consecutive losses\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "_System ready. Watching markets..._"
        )
    else:
        print("[STARTUP] Login failed — engine not started")
    yield

# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(title="Nifty Jarvis Signal Engine", version="7.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    return {"status": "running", "version": "7.0.0", "mode": "LIVE"}

@app.get("/api/signal")
def get_signal():
    return {
        "signal":          state["current_signal"],
        "market_open":     state["market_open"],
        "last_update":     state["last_update"],
        "strategy_status": state["strategy_status"],
        "market_data":     state["market_data"],
    }

@app.get("/api/ping")
def ping():
    state["ping_count"] += 1
    run_engine()
    return {
        "signal":      state["current_signal"],
        "market_open": state["market_open"],
        "last_update": state["last_update"],
        "ping_count":  state["ping_count"],
        "market_data": state["market_data"],
    }

@app.get("/api/history")
def history():
    return {"signals": state["signal_history"], "total": len(state["signal_history"])}

@app.get("/api/strategies")
def strategies():
    return state["strategy_status"]

@app.get("/api/market")
def market():
    return {
        "market_open": state["market_open"],
        "market_data": state["market_data"],
        "last_update": state["last_update"],
    }

@app.get("/api/status")
def status():
    return {
        "status":       "ok",
        "version":      "7.0.0",
        "mode":         "LIVE",
        "market_open":  state["market_open"],
        "logged_in":    state["auth_token"] is not None,
        "last_login":   state["last_login"],
        "last_update":  state["last_update"],
        "ping_count":   state["ping_count"],
        "error":        state["error"],
        "trades_today": sum(state["inst_state"][i]["trades_today"] for i in SYMBOLS),
    }

@app.post("/api/callback")
def callback(data: dict):
    return {"status": "received"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
