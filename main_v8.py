"""
Nifty Jarvis Signal Engine v8.0
Strategy: Supertrend + EMA Cross + Extended ORB
Simple, proven, fires 2-5 signals per day.
"""

import os, time, math, datetime, threading, requests, pyotp, logging
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

load_dotenv()

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger("engine")

# ─── Constants ────────────────────────────────────────────────────────────────
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_START = datetime.time(9, 15)
MARKET_END   = datetime.time(15, 30)
ORB_START    = datetime.time(9, 15)
ORB_END      = datetime.time(9, 30)   # ORB range built from first 15 min
ORB_SIGNAL_UNTIL = datetime.time(13, 0)  # ORB breakout valid until 1 PM

SUPERTREND_PERIOD  = 7
SUPERTREND_MULT    = 3.0
EMA_FAST           = 9
EMA_SLOW           = 21
RSI_PERIOD         = 14
RSI_MIN            = 30   # don't trade trend when RSI too oversold
RSI_MAX            = 70   # don't trade trend when RSI too overbought
COOLDOWN_SECS      = 300  # 5 min between signals per instrument
MAX_SIGNALS_DAY    = 5
TICK_INTERVAL      = 60   # seconds between engine ticks

# Angel One tokens (correct tokens verified)
INSTRUMENTS = {
    "NIFTY":    {"exchange": "NSE", "token": "99926000", "sl_pts": 40,  "tgt_pts": 80},
    "BANKNIFTY":{"exchange": "NSE", "token": "99926009", "sl_pts": 100, "tgt_pts": 200},
}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8738811972:AAFfyNLXYJYnBMbS5LKPbsqGjGDCOiKBUmg")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID",  "7974525984")

# ─── State ────────────────────────────────────────────────────────────────────
state = {
    "version": "8.0.0",
    "market_open": False,
    "logged_in": False,
    "last_update": None,
    "trades_today": 0,
    "today_date": None,
    "current_signal": None,
    "signal_history": [],
    "market_data": {
        inst: {"ltp": 0.0, "change_pct": 0.0, "open": 0.0}
        for inst in INSTRUMENTS
    },
    "strategy_status": {
        inst: {
            "orb":        {"active": False, "direction": None, "orb_high": 0, "orb_low": 0, "orb_ready": False},
            "supertrend": {"active": False, "direction": None, "supertrend": 0, "trend_up": None},
            "ema_cross":  {"active": False, "direction": None, "ema9": 0, "ema21": 0, "rsi": 0},
        }
        for inst in INSTRUMENTS
    },
    "last_signal_time": {inst: None for inst in INSTRUMENTS},
    "consecutive_losses": 0,
}

smart_api = None

# ─── Angel One Login ──────────────────────────────────────────────────────────
def login():
    global smart_api
    try:
        from SmartApi import SmartConnect
        api_key  = os.getenv("ANGEL_API_KEY")
        client   = os.getenv("ANGEL_CLIENT_ID")
        password = os.getenv("ANGEL_PASSWORD")
        totp_key = os.getenv("ANGEL_TOTP_SECRET")
        totp     = pyotp.TOTP(totp_key).now()
        smart_api = SmartConnect(api_key=api_key)
        resp = smart_api.generateSession(client, password, totp)
        if resp.get("status"):
            state["logged_in"] = True
            log.info(f"✅ Logged in to Angel One")
            send_telegram("✅ *Nifty Jarvis v8.0 Started*\nEngine running — Supertrend + EMA Cross + ORB")
            return True
        else:
            log.error(f"Login failed: {resp}")
            return False
    except Exception as e:
        log.error(f"Login error: {e}")
        return False

# ─── Helpers ──────────────────────────────────────────────────────────────────
def now_ist():
    return datetime.datetime.now(IST)

def is_market_open():
    t = now_ist().time()
    return MARKET_START <= t <= MARKET_END

def fetch_candles(token, exchange):
    """Fetch all 1-min candles from 9:15 AM today to now."""
    try:
        ist_now = now_ist()
        frm = ist_now.replace(hour=9, minute=15, second=0, microsecond=0)
        params = {
            "exchange":    exchange,
            "symboltoken": token,
            "interval":    "ONE_MINUTE",
            "fromdate":    frm.strftime("%Y-%m-%d %H:%M"),
            "todate":      ist_now.strftime("%Y-%m-%d %H:%M"),
        }
        resp = smart_api.getCandleData(params)
        data = resp.get("data") or []
        if not data:
            return None
        # Each candle: [timestamp, open, high, low, close, volume]
        return data
    except Exception as e:
        log.error(f"fetch_candles error: {e}")
        return None

def calc_ema(prices, period):
    """Calculate EMA for a list of prices."""
    if len(prices) < period:
        return prices[-1] if prices else 0
    k = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 2)

def calc_rsi(closes, period=14):
    """Calculate RSI."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calc_atr(candles, period=7):
    """Calculate ATR from candles."""
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i][2]
        l = candles[i][3]
        pc = candles[i-1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr

def calc_supertrend(candles, period=7, multiplier=3.0):
    """
    Calculate Supertrend indicator.
    Returns: (supertrend_value, trend_is_up)
    trend_is_up = True means bullish (CE), False means bearish (PE)
    """
    if len(candles) < period + 2:
        return 0, None

    # Calculate ATR for each candle
    atrs = [0] * period
    for i in range(period, len(candles)):
        window = candles[i-period:i+1]
        atr = calc_atr(window, period)
        atrs.append(atr)

    # Calculate basic upper/lower bands
    upper_bands = []
    lower_bands = []
    for i, c in enumerate(candles):
        hl2 = (c[2] + c[3]) / 2
        atr = atrs[i] if i < len(atrs) else atrs[-1]
        upper_bands.append(hl2 + multiplier * atr)
        lower_bands.append(hl2 - multiplier * atr)

    # Calculate final supertrend
    final_upper = list(upper_bands)
    final_lower = list(lower_bands)
    trend_up = [True] * len(candles)

    for i in range(1, len(candles)):
        # Adjust upper band
        if upper_bands[i] < final_upper[i-1] or candles[i-1][4] > final_upper[i-1]:
            final_upper[i] = upper_bands[i]
        else:
            final_upper[i] = final_upper[i-1]

        # Adjust lower band
        if lower_bands[i] > final_lower[i-1] or candles[i-1][4] < final_lower[i-1]:
            final_lower[i] = lower_bands[i]
        else:
            final_lower[i] = final_lower[i-1]

        # Determine trend
        close = candles[i][4]
        prev_trend = trend_up[i-1]
        if prev_trend:
            trend_up[i] = close >= final_lower[i]
        else:
            trend_up[i] = close > final_upper[i]

    last_close = candles[-1][4]
    is_up = trend_up[-1]
    st_val = final_lower[-1] if is_up else final_upper[-1]

    # Detect flip (trend change on last candle)
    flipped = (trend_up[-1] != trend_up[-2]) if len(trend_up) >= 2 else False

    return round(st_val, 2), is_up, flipped

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log.error(f"Telegram error: {e}")

def daily_reset():
    today = now_ist().date()
    if state["today_date"] != today:
        state["today_date"] = today
        state["trades_today"] = 0
        state["consecutive_losses"] = 0
        state["current_signal"] = None
        # Reset ORB for all instruments
        for inst in INSTRUMENTS:
            state["strategy_status"][inst]["orb"].update({
                "active": False, "direction": None,
                "orb_high": 0, "orb_low": 0, "orb_ready": False
            })
        log.info(f"📅 Daily reset for {today}")

def can_trade(inst):
    """Check if we can take a new trade."""
    if state["trades_today"] >= MAX_SIGNALS_DAY:
        return False, "Max signals reached"
    if state["consecutive_losses"] >= 3:
        return False, "3 consecutive losses — stopped for today"
    last = state["last_signal_time"].get(inst)
    if last:
        elapsed = (now_ist() - last).total_seconds()
        if elapsed < COOLDOWN_SECS:
            return False, f"Cooldown ({int(COOLDOWN_SECS - elapsed)}s left)"
    return True, "OK"

def fire_signal(inst, direction, strategy, entry, sl, tgt, confidence, extra_info=""):
    """Fire a signal — update state, send Telegram, log."""
    ok, reason = can_trade(inst)
    if not ok:
        log.info(f"Signal blocked ({reason}): {inst} {direction}")
        return

    cfg = INSTRUMENTS[inst]
    signal = {
        "instrument": inst,
        "direction": direction,
        "strategy": strategy,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tgt": round(tgt, 2),
        "confidence": confidence,
        "time": now_ist().strftime("%H:%M:%S"),
        "timestamp": now_ist().isoformat(),
    }
    state["current_signal"] = signal
    state["signal_history"].append(signal)
    state["trades_today"] += 1
    state["last_signal_time"][inst] = now_ist()

    icon = "🟢" if direction == "CE" else "🔴"
    conf_icon = "⭐⭐⭐" if confidence == "HIGH" else "⭐⭐"
    msg = (
        f"{icon} *{inst} {direction} SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Strategy:* {strategy}\n"
        f"💰 *Entry:* ₹{entry:.2f}\n"
        f"🎯 *Target:* ₹{tgt:.2f} (+{cfg['tgt_pts']} pts)\n"
        f"🛑 *Stop Loss:* ₹{sl:.2f} (-{cfg['sl_pts']} pts)\n"
        f"📐 *R:R:* 1:2\n"
        f"{conf_icon} *Confidence:* {confidence}\n"
    )
    if extra_info:
        msg += f"📝 {extra_info}\n"
    msg += (
        f"🕒 *Time:* {signal['time']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"_Nifty Jarvis — Discipline Automated_"
    )
    send_telegram(msg)
    log.info(f"🚀 SIGNAL: {inst} {direction} @ {entry} | SL:{sl} TGT:{tgt} | {strategy} | {confidence}")

# ─── Strategy Logic ───────────────────────────────────────────────────────────
def run_strategies(inst, candles):
    cfg = INSTRUMENTS[inst]
    ss  = state["strategy_status"][inst]
    ist_now = now_ist()
    t   = ist_now.time()

    closes  = [c[4] for c in candles]
    highs   = [c[2] for c in candles]
    lows    = [c[3] for c in candles]
    ltp     = closes[-1]

    # ── Indicators ────────────────────────────────────────────────────────────
    ema9  = calc_ema(closes, EMA_FAST)
    ema21 = calc_ema(closes, EMA_SLOW)
    rsi   = calc_rsi(closes, RSI_PERIOD)

    st_result = calc_supertrend(candles, SUPERTREND_PERIOD, SUPERTREND_MULT)
    st_val, st_up, st_flipped = st_result if len(st_result) == 3 else (0, None, False)

    # ── ORB Setup (build range during 9:15–9:30) ──────────────────────────────
    orb = ss["orb"]
    if ORB_START <= t <= ORB_END:
        # Build ORB range from all candles in the first 15 min
        orb_candles = [c for c in candles if datetime.datetime.fromisoformat(c[0]).astimezone(IST).time() <= ORB_END]
        if orb_candles:
            orb["orb_high"] = round(max(c[2] for c in orb_candles), 2)
            orb["orb_low"]  = round(min(c[3] for c in orb_candles), 2)
            orb["orb_ready"] = False  # not ready until window closes
    elif t > ORB_END and not orb["orb_ready"] and orb["orb_high"] > 0:
        orb["orb_ready"] = True
        log.info(f"{inst} ORB ready: H={orb['orb_high']} L={orb['orb_low']}")

    # ── Strategy 1: ORB Breakout ───────────────────────────────────────────────
    orb["active"] = False
    orb["direction"] = None
    if orb["orb_ready"] and t <= ORB_SIGNAL_UNTIL:
        if ltp > orb["orb_high"] * 1.001:  # 0.1% above ORB high
            orb["active"] = True
            orb["direction"] = "CE"
        elif ltp < orb["orb_low"] * 0.999:  # 0.1% below ORB low
            orb["active"] = True
            orb["direction"] = "PE"

    # ── Strategy 2: Supertrend Flip ────────────────────────────────────────────
    st_status = ss["supertrend"]
    st_status["supertrend"] = st_val
    st_status["trend_up"] = st_up
    st_status["active"] = False
    st_status["direction"] = None

    if st_flipped and st_up is not None:
        # Supertrend just flipped — this is the signal
        if st_up and RSI_MIN <= rsi <= RSI_MAX:
            st_status["active"] = True
            st_status["direction"] = "CE"
        elif not st_up and RSI_MIN <= rsi <= RSI_MAX:
            st_status["active"] = True
            st_status["direction"] = "PE"

    # ── Strategy 3: EMA Cross ──────────────────────────────────────────────────
    ema_status = ss["ema_cross"]
    ema_status["ema9"]  = ema9
    ema_status["ema21"] = ema21
    ema_status["rsi"]   = rsi
    ema_status["active"] = False
    ema_status["direction"] = None

    if len(closes) >= EMA_SLOW + 2:
        # Check for crossover on last 2 candles
        prev_closes = closes[:-1]
        prev_ema9  = calc_ema(prev_closes, EMA_FAST)
        prev_ema21 = calc_ema(prev_closes, EMA_SLOW)

        crossed_up   = prev_ema9 <= prev_ema21 and ema9 > ema21
        crossed_down = prev_ema9 >= prev_ema21 and ema9 < ema21

        if crossed_up and RSI_MIN <= rsi <= RSI_MAX:
            ema_status["active"] = True
            ema_status["direction"] = "CE"
        elif crossed_down and RSI_MIN <= rsi <= RSI_MAX:
            ema_status["active"] = True
            ema_status["direction"] = "PE"

    # ── Signal Firing ─────────────────────────────────────────────────────────
    # Priority: ORB > Supertrend > EMA Cross
    # Any single strategy can fire a signal
    for strat_name, strat_data, confidence in [
        ("ORB Breakout",      orb,        "HIGH"),
        ("Supertrend Flip",   st_status,  "HIGH"),
        ("EMA Cross",         ema_status, "MEDIUM"),
    ]:
        if strat_data["active"] and strat_data["direction"]:
            direction = strat_data["direction"]
            sl_pts  = cfg["sl_pts"]
            tgt_pts = cfg["tgt_pts"]
            if direction == "CE":
                sl  = ltp - sl_pts
                tgt = ltp + tgt_pts
            else:
                sl  = ltp + sl_pts
                tgt = ltp - tgt_pts
            extra = f"EMA9={ema9} EMA21={ema21} RSI={rsi} ST={st_val}"
            fire_signal(inst, direction, strat_name, ltp, sl, tgt, confidence, extra)
            break  # Only fire one signal per tick per instrument

# ─── Engine Tick ──────────────────────────────────────────────────────────────
def engine_tick():
    daily_reset()
    state["market_open"] = is_market_open()
    state["last_update"] = now_ist().isoformat()

    for inst, cfg in INSTRUMENTS.items():
        candles = fetch_candles(cfg["token"], cfg["exchange"])
        if not candles or len(candles) < 2:
            log.warning(f"{inst}: No candle data ({len(candles) if candles else 0} candles)")
            continue

        ltp = candles[-1][4]
        open_price = candles[0][1]
        change_pct = round((ltp - open_price) / open_price * 100, 2)

        state["market_data"][inst].update({
            "ltp": ltp,
            "change_pct": change_pct,
            "open": open_price,
        })
        log.info(f"{inst}: LTP={ltp} ({change_pct:+.2f}%) | {len(candles)} candles")

        if state["market_open"]:
            run_strategies(inst, candles)
        else:
            log.info(f"Market closed — skipping strategies")

def engine_loop():
    log.info("🔄 Engine loop started")
    while True:
        try:
            engine_tick()
        except Exception as e:
            log.error(f"Engine tick error: {e}")
        time.sleep(TICK_INTERVAL)

# ─── FastAPI Server ────────────────────────────────────────────────────────────
app = FastAPI(title="Nifty Jarvis v8")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/api/signal")
def get_signal():
    return {
        "signal":          state["current_signal"],
        "market_open":     state["market_open"],
        "last_update":     state["last_update"],
        "strategy_status": state["strategy_status"],
        "market_data":     state["market_data"],
        "trades_today":    state["trades_today"],
    }

@app.get("/api/status")
def get_status():
    return {
        "version":      state["version"],
        "logged_in":    state["logged_in"],
        "market_open":  state["market_open"],
        "last_update":  state["last_update"],
        "trades_today": state["trades_today"],
        "consecutive_losses": state["consecutive_losses"],
    }

@app.get("/api/history")
def get_history():
    return {"signals": state["signal_history"]}

@app.get("/api/strategies")
def get_strategies():
    return state["strategy_status"]

@app.get("/health")
def health():
    return {"status": "ok", "version": state["version"]}

# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 Starting Nifty Jarvis Signal Engine v8.0")
    if not login():
        log.error("Login failed — retrying in 30s")
        time.sleep(30)
        if not login():
            log.error("Login failed twice — exiting")
            exit(1)

    # Run first tick immediately
    try:
        engine_tick()
    except Exception as e:
        log.error(f"First tick error: {e}")

    # Start background engine loop
    t = threading.Thread(target=engine_loop, daemon=True)
    t.start()

    # Start API server
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
