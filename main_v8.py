"""
Nifty Jarvis Signal Engine v9.0
================================
Infrastructure: Manus
Strategy: Institutional Intent Detection
Based on: 31 chapters Stock Burner curriculum

Core Question: "Are institutions moving this market right now?"

3-Layer System:
Layer 1: DIRECTION  — 1-hour market structure + VWAP
Layer 2: CONFIRM    — 9 EMA + candle quality + volume
Layer 3: PROTECT    — fake signal filter + location check

Candle Patterns: Single/Double/Triple
Risk: ATR-based SL, max 3 trades, 2-loss stop
"""

import os, time, datetime, threading, requests, json
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
def now_ist(): return datetime.datetime.now(IST)
import pyotp
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from SmartApi import SmartConnect

load_dotenv()
API_KEY     = os.getenv("ANGEL_API_KEY", "")
CLIENT_ID   = os.getenv("ANGEL_CLIENT_ID", "")
PASSWORD    = os.getenv("ANGEL_PASSWORD", "")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")
BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "8738811972:AAFNIu_5r-DpHcC7DdYcnF2_Z6UwsLMYoe8")
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "8217586252")

SYMBOLS = {
    "NIFTY":     {"token": "99926000", "exchange": "NSE", "step": 50,  "lot": 75},
    "BANKNIFTY": {"token": "99926009", "exchange": "NSE", "step": 100, "lot": 30},
}

MAX_TRADES      = 3      # Max signals per instrument per day
MAX_LOSS_STREAK = 2      # Stop after 2 consecutive losses
COOLDOWN_SECS   = 300    # 5 min between signals
MARKET_START    = (9, 20)
MARKET_END      = (15, 10)
CAPITAL         = 35000  # Starting capital
MAX_RISK_PCT    = 0.10   # 10% max risk per trade

def _inst_state():
    return {
        "trades_today":     0,
        "loss_streak":      0,
        "last_signal_time": None,
        "last_signal_dir":  None,
        "last_st_dir":      None,
        "day_direction":    None,   # Set once per day from 1-hour
    }

state = {
    "auth_token":    None,
    "last_login":    None,
    "current_signal": None,
    "signal_history": [],
    "market_open":   False,
    "last_update":   None,
    "ping_count":    0,
    "error":         None,
    "last_trade_date": None,
    "market_data": {
        "NIFTY":     {"ltp": 0.0, "change_pct": 0.0},
        "BANKNIFTY": {"ltp": 0.0, "change_pct": 0.0},
    },
    "strategy_status": {
        "NIFTY":     {"layer1": {}, "layer2": {}, "layer3": {}, "signal": None},
        "BANKNIFTY": {"layer1": {}, "layer2": {}, "layer3": {}, "signal": None},
    },
    "inst_state": {
        "NIFTY":     _inst_state(),
        "BANKNIFTY": _inst_state(),
    },
}

smart_api = SmartConnect(api_key=API_KEY)

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    if not BOT_TOKEN or not CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        print(f"[TELEGRAM] {e}")

# ── Indicators ────────────────────────────────────────────────────────────────
def ema(prices, n):
    if not prices: return 0.0
    if len(prices) < n: return round(sum(prices)/len(prices), 2)
    k = 2.0/(n+1); v = sum(prices[:n])/n
    for x in prices[n:]: v = x*k + v*(1-k)
    return round(v, 2)

def rsi(prices, n=14):
    if len(prices) < n+1: return 50.0
    gains  = [max(prices[i]-prices[i-1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i-1]-prices[i], 0) for i in range(1, len(prices))]
    ag = sum(gains[-n:])/n; al = sum(losses[-n:])/n
    return 100.0 if al == 0 else round(100-(100/(1+ag/al)), 2)

def atr(highs, lows, closes, period=14):
    if len(closes) < 2: return 50.0
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i]-lows[i],
            abs(highs[i]-closes[i-1]),
            abs(lows[i]-closes[i-1])
        ))
    if len(trs) < period: return round(sum(trs)/len(trs), 2)
    return round(sum(trs[-period:])/period, 2)

def vwap_calc(highs, lows, closes, vols):
    tp_vol = sum(((highs[i]+lows[i]+closes[i])/3)*vols[i] for i in range(len(closes)))
    total  = sum(vols)
    return round(tp_vol/total, 2) if total > 0 else closes[-1]

def bollinger_bandwidth(closes, period=20):
    if len(closes) < period: return 2.0
    sma = sum(closes[-period:])/period
    var = sum((c-sma)**2 for c in closes[-period:])/period
    std = var**0.5
    return round(((sma + 2*std) - (sma - 2*std))/sma*100, 3)

def market_structure_1h(highs, lows, closes):
    """Detect 1-hour market structure: BULLISH/BEARISH/SIDEWAYS"""
    if len(closes) < 6: return "SIDEWAYS"
    # Find recent swing highs and lows (simplified)
    mid = len(closes)//2
    first_half_high = max(highs[:mid])
    first_half_low  = min(lows[:mid])
    second_half_high = max(highs[mid:])
    second_half_low  = min(lows[mid:])
    hh = second_half_high > first_half_high
    hl = second_half_low  > first_half_low
    lh = second_half_high < first_half_high
    ll = second_half_low  < first_half_low
    if hh and hl: return "BULLISH"
    if lh and ll: return "BEARISH"
    return "SIDEWAYS"

# ── Candle Pattern Detection ──────────────────────────────────────────────────
def detect_candle_pattern(opens, highs, lows, closes):
    """Detect single/double/triple candle patterns on closed candles"""
    if len(closes) < 5: return "NO_PATTERN", None, 0

    # Last 3 CLOSED candles (not live)
    o1,h1,l1,c1 = opens[-4],highs[-4],lows[-4],closes[-4]
    o2,h2,l2,c2 = opens[-3],highs[-3],lows[-3],closes[-3]
    o3,h3,l3,c3 = opens[-2],highs[-2],lows[-2],closes[-2]

    body1 = abs(c1-o1); body2 = abs(c2-o2); body3 = abs(c3-o3)
    range1 = max(h1-l1, 0.01)
    range2 = max(h2-l2, 0.01)
    range3 = max(h3-l3, 0.01)

    # ── SINGLE CANDLE ─────────────────────────────────────────
    # Bullish Marubozu
    if c3>o3 and (h3-c3)<body3*0.05 and (o3-l3)<body3*0.05 and body3/range3>0.9:
        return "BULLISH_MARUBOZU", "CE", 5
    # Bearish Marubozu
    if c3<o3 and (h3-o3)<body3*0.05 and (c3-l3)<body3*0.05 and body3/range3>0.9:
        return "BEARISH_MARUBOZU", "PE", 5
    # Hammer
    low_wick3  = min(o3,c3) - l3
    high_wick3 = h3 - max(o3,c3)
    if low_wick3 > body3*2 and high_wick3 < body3*0.5 and body3 > 0:
        return "HAMMER", "CE", 3
    # Shooting Star
    if high_wick3 > body3*2 and low_wick3 < body3*0.5 and body3 > 0:
        return "SHOOTING_STAR", "PE", 3
    # Doji
    if body3 < range3*0.1:
        return "DOJI", "WAIT", 0

    # ── DOUBLE CANDLE ─────────────────────────────────────────
    # Bullish Engulfing
    if (c2<o2 and c3>o3 and o3<=c2 and c3>=o2 and body3>body2):
        return "BULLISH_ENGULFING", "CE", 4
    # Bearish Engulfing
    if (c2>o2 and c3<o3 and o3>=c2 and c3<=o2 and body3>body2):
        return "BEARISH_ENGULFING", "PE", 4
    # Tweezer Bottom
    if abs(l2-l3) < range2*0.02 and c3>o3:
        return "TWEEZER_BOTTOM", "CE", 3
    # Tweezer Top
    if abs(h2-h3) < range2*0.02 and c3<o3:
        return "TWEEZER_TOP", "PE", 3

    # ── TRIPLE CANDLE ─────────────────────────────────────────
    # Morning Star
    if (c1<o1 and body1>range1*0.5 and
        body2<body1*0.35 and
        c3>o3 and body3>range3*0.5 and
        c3>(o1+c1)/2):
        return "MORNING_STAR", "CE", 5
    # Evening Star
    if (c1>o1 and body1>range1*0.5 and
        body2<body1*0.35 and
        c3<o3 and body3>range3*0.5 and
        c3<(o1+c1)/2):
        return "EVENING_STAR", "PE", 5
    # Three White Soldiers
    if (c1>o1 and c2>o2 and c3>o3 and c2>c1 and c3>c2 and
        body1/range1>0.6 and body2/range2>0.6 and body3/range3>0.6):
        return "THREE_SOLDIERS", "CE", 5
    # Three Black Crows
    if (c1<o1 and c2<o2 and c3<o3 and c2<c1 and c3<c2 and
        body1/range1>0.6 and body2/range2>0.6 and body3/range3>0.6):
        return "THREE_CROWS", "PE", 5

    return "NO_PATTERN", None, 0

# ── Instrument Master ─────────────────────────────────────────────────────────
_inst_cache = {}; _inst_expiry = {}; _inst_loaded = False

def load_instrument_master():
    global _inst_cache, _inst_expiry, _inst_loaded
    try:
        r = requests.get(
            "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
            timeout=30
        )
        data = r.json()
        today = datetime.datetime.now().date()
        for name in ("NIFTY", "BANKNIFTY"):
            opts = [x for x in data if x.get("name","").upper()==name
                    and x.get("instrumenttype")=="OPTIDX"]
            for e in sorted(set(x.get("expiry","") for x in opts)):
                try:
                    if datetime.datetime.strptime(e,"%d%b%Y").date() >= today:
                        _inst_expiry[name] = e; break
                except: continue
        cache = {}
        for item in data:
            if item.get("instrumenttype") != "OPTIDX": continue
            name = item.get("name","").upper()
            if name not in ("NIFTY","BANKNIFTY"): continue
            if item.get("expiry") != _inst_expiry.get(name): continue
            try: strike = int(float(item.get("strike",0)))//100
            except: continue
            sym = item.get("symbol","")
            otype = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else None)
            if not otype: continue
            cache[f"{name}|{strike}|{otype}"] = {
                "token": item.get("token",""), "symbol": sym,
                "exch":  item.get("exch_seg","NFO")
            }
        _inst_cache = cache; _inst_loaded = True
        print(f"[INST] NIFTY={_inst_expiry.get('NIFTY')} BN={_inst_expiry.get('BANKNIFTY')} n={len(cache)}")
    except Exception as e:
        print(f"[INST] Error: {e}")

def fetch_option_ltp(inst, strike, otype):
    global _inst_loaded
    if not _inst_loaded: load_instrument_master()
    info = _inst_cache.get(f"{inst}|{strike}|{otype}")
    if not info: return None, f"{inst}{strike}{otype}"
    try:
        r = smart_api.ltpData(info["exch"], info["symbol"], info["token"])
        if r and r.get("status") and r.get("data"):
            return float(r["data"]["ltp"]), info["symbol"]
        return None, info["symbol"]
    except Exception as e:
        print(f"[OPT] {e}"); return None, info.get("symbol","")


# ── Data Fetching ─────────────────────────────────────────────────────────────
def fetch_candles(token, exchange, interval="FIVE_MINUTE", days=1):
    try:
        now = now_ist()
        frm = now - datetime.timedelta(days=days)
        r = smart_api.getCandleData({
            "exchange":    exchange,
            "symboltoken": token,
            "interval":    interval,
            "fromdate":    frm.strftime("%Y-%m-%d %H:%M"),
            "todate":      now.strftime("%Y-%m-%d %H:%M"),
        })
        if r and r.get("status") and r.get("data"):
            return r["data"]
        return None
    except Exception as e:
        print(f"[FETCH] {e}"); return None

def fetch_candles_1h(token, exchange):
    return fetch_candles(token, exchange, "ONE_HOUR", days=5)

def fetch_candles_5m(token, exchange):
    return fetch_candles(token, exchange, "FIVE_MINUTE", days=1)

# ── Market Timing ─────────────────────────────────────────────────────────────
def market_open():
    n = now_ist()
    if n.weekday() >= 5: return False
    start = n.replace(hour=MARKET_START[0], minute=MARKET_START[1], second=0, microsecond=0)
    end   = n.replace(hour=MARKET_END[0],   minute=MARKET_END[1],   second=0, microsecond=0)
    return start <= n <= end

def is_expiry_day():
    """Check if today is NIFTY weekly expiry (Thursday or shifted)"""
    n = now_ist()
    return n.weekday() == 3  # Thursday

def daily_reset():
    today = now_ist().date().isoformat()
    if state.get("last_trade_date") != today:
        state["last_trade_date"] = today
        for inst in SYMBOLS:
            ist = state["inst_state"][inst]
            ist["trades_today"]    = 0
            ist["loss_streak"]     = 0
            ist["last_signal_dir"] = None
            ist["last_st_dir"]     = None
            ist["day_direction"]   = None
        print("[ENGINE] Daily reset")

def signal_allowed(inst):
    ist = state["inst_state"][inst]
    if ist["trades_today"]  >= MAX_TRADES:      return False
    if ist["loss_streak"]   >= MAX_LOSS_STREAK: return False
    if ist["last_signal_time"]:
        elapsed = (now_ist() - ist["last_signal_time"]).total_seconds()
        if elapsed < COOLDOWN_SECS: return False
    return True

# ── Login ─────────────────────────────────────────────────────────────────────
def login():
    try:
        totp = pyotp.TOTP(TOTP_SECRET).now()
        d = smart_api.generateSession(CLIENT_ID, PASSWORD, totp)
        if d and d.get("status"):
            state["auth_token"] = d["data"]["jwtToken"]
            state["last_login"]  = now_ist().isoformat()
            state["error"]       = None
            print(f"[LOGIN] OK at {now_ist().strftime('%H:%M:%S')}")
            return True
        msg = d.get("message","Login failed") if d else "No response"
        state["error"] = msg; print(f"[LOGIN] Failed: {msg}"); return False
    except Exception as e:
        state["error"] = str(e); print(f"[LOGIN] Exception: {e}"); return False


# ── MAIN SIGNAL ENGINE ────────────────────────────────────────────────────────
def run_engine():
    print(f"[{now_ist().strftime('%H:%M:%S')}] Engine tick")
    if not state["auth_token"]:
        print("[ENGINE] Not logged in"); return

    daily_reset()
    state["market_open"] = market_open()

    # Fetch NIFTY + BANKNIFTY 5-min candles
    all_candles = {}
    all_1h      = {}

    for inst, info in SYMBOLS.items():
        c5 = fetch_candles_5m(info["token"], info["exchange"])
        c1h = fetch_candles_1h(info["token"], info["exchange"])
        if not c5 or len(c5) < 20:
            print(f"[{inst}] Insufficient 5m data"); continue
        all_candles[inst] = c5
        all_1h[inst]      = c1h if c1h and len(c1h) >= 6 else None

        # Update market data ticker
        closes_5m = [float(c[4]) for c in c5]
        state["market_data"][inst] = {
            "ltp":        round(closes_5m[-1], 2),
            "change_pct": round(((closes_5m[-1]-closes_5m[0])/closes_5m[0])*100, 2) if closes_5m[0] else 0.0,
        }

    if not state["market_open"]:
        print("[ENGINE] Market closed — data updated")
        state["last_update"] = now_ist().isoformat()
        return

    # Need both instruments for correlation check
    if "NIFTY" not in all_candles or "BANKNIFTY" not in all_candles:
        print("[ENGINE] Missing candle data"); return

    for inst, info in SYMBOLS.items():
        try:
            c5  = all_candles[inst]
            c1h = all_1h.get(inst)
            ist = state["inst_state"][inst]

            opens5  = [float(c[1]) for c in c5]
            highs5  = [float(c[2]) for c in c5]
            lows5   = [float(c[3]) for c in c5]
            closes5 = [float(c[4]) for c in c5]
            vols5   = [float(c[5]) for c in c5]

            ltp        = closes5[-1]
            # Use closes5[-2] = last CLOSED candle (not live)
            last_close = closes5[-2]
            last_open  = opens5[-2]
            last_high  = highs5[-2]
            last_low   = lows5[-2]
            last_vol   = vols5[-2]

            # ── LAYER 1: DIRECTION ────────────────────────────────────────────
            # 1A. 1-hour market structure
            if c1h:
                h1h = [float(c[2]) for c in c1h]
                l1h = [float(c[3]) for c in c1h]
                cl1h = [float(c[4]) for c in c1h]
                hour_structure = market_structure_1h(h1h, l1h, cl1h)
            else:
                hour_structure = "SIDEWAYS"

            # Cache day direction (set once, used all day)
            if not ist["day_direction"]:
                ist["day_direction"] = hour_structure
            day_dir = ist["day_direction"]

            # 1B. VWAP
            vwap = vwap_calc(highs5, lows5, closes5, vols5)
            vwap_bullish = ltp > vwap
            vwap_bearish = ltp < vwap

            # 1C. ATR and BB check
            atr_val = atr(highs5, lows5, closes5)
            bb_bw   = bollinger_bandwidth(closes5)

            # Layer 1 result
            if day_dir == "BULLISH" and vwap_bullish:
                l1_direction = "CE"
            elif day_dir == "BEARISH" and vwap_bearish:
                l1_direction = "PE"
            elif day_dir == "BULLISH" and vwap_bearish:
                l1_direction = "CE"   # Day bullish overrides VWAP
            elif day_dir == "BEARISH" and vwap_bullish:
                l1_direction = "PE"   # Day bearish overrides VWAP
            else:
                l1_direction = None   # Sideways — no bias

            state["strategy_status"][inst]["layer1"] = {
                "hour_structure": hour_structure,
                "day_direction":  day_dir,
                "vwap":           vwap,
                "vwap_side":      "ABOVE" if vwap_bullish else "BELOW",
                "atr":            atr_val,
                "bb_bandwidth":   bb_bw,
                "direction":      l1_direction,
            }

            # ── LAYER 2: CONFIRMATION ─────────────────────────────────────────
            # 2A. 9 EMA — using closed candles
            e9 = ema(closes5[:-1], 9)   # EMA on closed candles only

            # 9 EMA setup detection (last CLOSED candle)
            ema_ce = (last_close > e9 and           # Above EMA
                      last_low <= e9 * 1.002 and    # Touched EMA
                      last_close > last_open)        # Bullish close

            ema_pe = (last_close < e9 and            # Below EMA
                      last_high >= e9 * 0.998 and   # Touched EMA
                      last_close < last_open)        # Bearish close

            # 2B. Candle quality check
            candle_body = abs(last_close - last_open)
            candle_range = max(last_high - last_low, 0.01)
            body_pct = candle_body / candle_range
            big_candle = (body_pct > 0.60 and
                          candle_body > atr_val * 0.5)

            # 2C. Volume confirmation
            avg_vol = sum(vols5[-15:-2])/13 if len(vols5) >= 15 else sum(vols5[:-2])/max(len(vols5)-2,1)
            vol_ok  = last_vol > avg_vol * 1.3 if avg_vol > 0 else False

            # 2D. RSI
            r_val = rsi(closes5[:-1])  # RSI on closed candles
            rsi_bullish = r_val > 50
            rsi_bearish = r_val < 50

            # 2E. Candle pattern
            pattern_name, pattern_dir, pattern_score = detect_candle_pattern(
                opens5, highs5, lows5, closes5
            )

            # Layer 2 result
            l2_ce = ema_ce and big_candle and vol_ok and rsi_bullish
            l2_pe = ema_pe and big_candle and vol_ok and rsi_bearish

            state["strategy_status"][inst]["layer2"] = {
                "ema9":          e9,
                "ema_ce":        ema_ce,
                "ema_pe":        ema_pe,
                "body_pct":      round(body_pct, 2),
                "big_candle":    big_candle,
                "volume_ok":     vol_ok,
                "rsi":           r_val,
                "pattern":       pattern_name,
                "pattern_dir":   pattern_dir,
                "pattern_score": pattern_score,
            }

            # ── LAYER 3: PROTECTION ───────────────────────────────────────────
            # 3A. Fake candle check (wick test)
            # If candle wick crossed a level but body closed back = fake
            wick_fake = False
            if last_close > last_open:  # Bullish candle
                lower_wick = last_open - last_low
                if lower_wick > candle_body * 2:
                    wick_fake = True   # Long lower wick = shakeout not real move
            else:
                upper_wick = last_high - last_open
                if upper_wick > candle_body * 2:
                    wick_fake = True

            # 3B. Liquidity sweep check (recent)
            recent_swing_low  = min(lows5[-10:-2])
            recent_swing_high = max(highs5[-10:-2])
            liq_sweep_bull = lows5[-2] < recent_swing_low and closes5[-2] > recent_swing_low
            liq_sweep_bear = highs5[-2] > recent_swing_high and closes5[-2] < recent_swing_high

            # 3C. Market noise check
            sideways = bb_bw < 0.4 or atr_val < 30
            broadening = (max(highs5[-8:]) > max(highs5[-16:-8]) and
                          min(lows5[-8:])  < min(lows5[-16:-8]))

            # 3D. NIFTY + BANKNIFTY correlation
            other_inst = "BANKNIFTY" if inst == "NIFTY" else "NIFTY"
            other_data = state["market_data"].get(other_inst, {})
            other_chg  = other_data.get("change_pct", 0)
            my_chg     = state["market_data"][inst].get("change_pct", 0)
            # Same direction if both positive or both negative
            corr_ok = (my_chg >= 0 and other_chg >= 0) or (my_chg < 0 and other_chg < 0)

            state["strategy_status"][inst]["layer3"] = {
                "wick_fake":      wick_fake,
                "liq_sweep_bull": liq_sweep_bull,
                "liq_sweep_bear": liq_sweep_bear,
                "sideways":       sideways,
                "broadening":     broadening,
                "corr_ok":        corr_ok,
                "bb_bandwidth":   bb_bw,
                "atr":            atr_val,
            }

            # ── SIGNAL DECISION ───────────────────────────────────────────────
            # Skip if market is in noise mode
            if sideways:
                print(f"[{inst}] SIDEWAYS market (BB={bb_bw}, ATR={atr_val}) — skip")
                continue
            if broadening:
                print(f"[{inst}] BROADENING triangle — dangerous — skip")
                continue
            if not corr_ok:
                print(f"[{inst}] Correlation divergence NIFTY/BANKNIFTY — skip")
                continue

            # Determine signal direction
            dom = None
            confidence_score = 0
            reasons = []

            # Layer 1 sets direction
            if l1_direction == "CE":
                if l2_ce and not wick_fake:
                    dom = "CE"
                    confidence_score += 3
                    reasons.append("9EMA+Vol")
                    if liq_sweep_bull:
                        confidence_score += 2
                        reasons.append("LiqSweep")
                    if pattern_dir == "CE" and pattern_score >= 3:
                        confidence_score += pattern_score
                        reasons.append(pattern_name)

            elif l1_direction == "PE":
                if l2_pe and not wick_fake:
                    dom = "PE"
                    confidence_score += 3
                    reasons.append("9EMA+Vol")
                    if liq_sweep_bear:
                        confidence_score += 2
                        reasons.append("LiqSweep")
                    if pattern_dir == "PE" and pattern_score >= 3:
                        confidence_score += pattern_score
                        reasons.append(pattern_name)

            # Also check pure candle pattern at key level (even without EMA)
            if dom is None and pattern_score >= 4 and not wick_fake:
                if pattern_dir == l1_direction or l1_direction is None:
                    dom = pattern_dir
                    confidence_score = pattern_score
                    reasons.append(f"PATTERN:{pattern_name}")

            if not dom:
                state["strategy_status"][inst]["signal"] = None
                continue

            # Gate checks
            if not signal_allowed(inst):
                print(f"[{inst}] Gated (trades={ist['trades_today']}, streak={ist['loss_streak']})")
                continue
            if ist["last_signal_dir"] == dom:
                print(f"[{inst}] Same direction block ({dom})")
                continue

            # Confidence level
            if confidence_score >= 7:
                confidence = "HIGH"
            elif confidence_score >= 4:
                confidence = "MEDIUM"
            else:
                print(f"[{inst}] Confidence too low ({confidence_score}) — skip")
                continue

            # ── RISK MANAGEMENT ───────────────────────────────────────────────
            entry = round(ltp, 2)
            atm   = int(round(entry/info["step"])*info["step"])

            # ATR-based SL (not fixed points)
            sl_dist = round(atr_val * 1.5, 2)
            sl  = round(entry - sl_dist if dom=="CE" else entry + sl_dist, 2)
            tgt = round(entry + sl_dist*2 if dom=="CE" else entry - sl_dist*2, 2)
            rr  = 2.0

            # Option premium
            opt_ltp, opt_sym = fetch_option_ltp(inst, atm, dom)
            if opt_ltp and opt_ltp > 5:
                opt_tgt     = round(opt_ltp * 1.40, 1)  # 40% target
                opt_sl      = round(opt_ltp * 0.65, 1)  # 35% SL
                opt_tgt_pts = round(opt_tgt - opt_ltp, 1)
                opt_sl_pts  = round(opt_ltp - opt_sl, 1)
                # Capital check
                max_loss = CAPITAL * MAX_RISK_PCT
                lot_size = info["lot"]
                premium_sl_loss = opt_sl_pts * lot_size
                if premium_sl_loss > max_loss:
                    print(f"[{inst}] Risk too high ({premium_sl_loss:.0f} > {max_loss:.0f}) — skip")
                    continue
            else:
                opt_tgt = opt_sl = opt_tgt_pts = opt_sl_pts = None

            # Build signal
            strat_str = " + ".join(reasons)
            sig = {
                "id":          f"{inst}_{dom}_{int(time.time())}",
                "instrument":  inst,
                "option_type": dom,
                "strike":      atm,
                "entry":       entry,
                "target":      tgt,
                "stop_loss":   sl,
                "risk_reward": rr,
                "strategies":  reasons,
                "confidence":  confidence,
                "score":       confidence_score,
                "timestamp":   now_ist().isoformat(),
                "ltp":         ltp,
                "ema9":        e9,
                "vwap":        vwap,
                "rsi":         r_val,
                "atr":         atr_val,
                "pattern":     pattern_name,
                "hour_struct": hour_structure,
                "status":      "ACTIVE",
                "opt_symbol":  opt_sym,
                "opt_ltp":     opt_ltp,
                "opt_target":  opt_tgt,
                "opt_sl":      opt_sl,
            }

            cur = state["current_signal"]
            if cur and cur.get("instrument")==inst and cur.get("option_type")==dom:
                continue
            if cur:
                cur["status"] = "CLOSED"
                state["signal_history"].insert(0, cur)
                state["signal_history"] = state["signal_history"][:50]

            state["current_signal"]          = sig
            state["strategy_status"][inst]["signal"] = sig
            ist["trades_today"]             += 1
            ist["last_signal_time"]          = now_ist()
            ist["last_signal_dir"]           = dom

            print(f"[SIGNAL] {inst} {dom} {atm} @ {entry} | SL:{sl} TGT:{tgt} | {confidence} | {strat_str}")

            # Telegram message
            icon = "📈" if dom=="CE" else "📉"
            conf_icon = "🔥" if confidence=="HIGH" else "⚡"

            if opt_ltp and opt_tgt:
                opt_block = (
                    f"\n💎 *BUY {opt_sym}*\n"
                    f"   Entry  : `₹{opt_ltp}`\n"
                    f"   Target : `₹{opt_tgt}` (+{opt_tgt_pts} pts)\n"
                    f"   SL     : `₹{opt_sl}` (-{opt_sl_pts} pts)\n"
                )
            else:
                opt_block = f"\n💎 *{inst} {atm} {dom}* — Premium N/A\n"

            msg = (
                f"🚨 *SIGNAL — {inst} {dom}*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{icon} {'CALL (CE)' if dom=='CE' else 'PUT (PE)'} · Strike `{atm}`\n"
                f"{opt_block}"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📊 *Index Entry :* `{entry}`\n"
                f"🎯 *Index TGT   :* `{tgt}`\n"
                f"🛑 *Index SL    :* `{sl}` (ATR×1.5)\n"
                f"📐 *R:R* `1:{rr}` | {conf_icon} {confidence} (score:{confidence_score})\n"
                f"🔬 *Why:* {strat_str}\n"
                f"📉 *RSI:* `{r_val}` | *9EMA:* `{e9}` | *VWAP:* `{vwap}`\n"
                f"📊 *Pattern:* {pattern_name} | *1H:* {hour_structure}\n"
                f"🕒 {now_ist().strftime('%H:%M:%S')}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"_Nifty Jarvis v9 — Institutional Intent_"
            )
            send_telegram(msg)

        except Exception as e:
            print(f"[{inst}] Error: {e}")
            state["error"] = str(e)

    state["last_update"] = now_ist().isoformat()

# ── Scheduler ─────────────────────────────────────────────────────────────────
def scheduler():
    tick = 0
    while True:
        run_engine()
        tick += 1
        if tick >= 360:
            print("[SCHEDULER] Re-login")
            login(); tick = 0
        time.sleep(60)

# ── FastAPI ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    if login():
        load_instrument_master()
        run_engine()
        threading.Thread(target=scheduler, daemon=True).start()
        print("[STARTUP] Signal Engine v9.0 started")
        send_telegram(
            "✅ *Nifty Jarvis v9.0 Online*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🧠 *System: Institutional Intent Detection*\n"
            "📊 3-Layer: Direction + Confirm + Protect\n"
            "🕯 Candle Patterns: Single/Double/Triple\n"
            "📈 9 EMA + ATR SL + Volume Filter\n"
            "🔗 NIFTY + BANKNIFTY Correlation\n"
            "💎 Option Premium: LIVE\n"
            "⏰ Window: 9:20 AM — 3:10 PM IST\n"
            "📊 Max: 3 signals/day | Stop: 2 losses\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "_Waiting for institutional moves..._"
        )
    else:
        print("[STARTUP] Login failed")
    yield

app = FastAPI(title="Nifty Jarvis v9", version="9.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root(): return FileResponse("/home/ubuntu/signal-engine/index.html")

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
    state["ping_count"] += 1; run_engine()
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
    return {"market_open": state["market_open"], "market_data": state["market_data"]}

@app.get("/api/status")
def status():
    trades = sum(state["inst_state"][i]["trades_today"] for i in SYMBOLS)
    return {
        "status":       "ok",
        "version":      "9.0.0",
        "mode":         "LIVE",
        "market_open":  state["market_open"],
        "logged_in":    state["auth_token"] is not None,
        "last_login":   state["last_login"],
        "last_update":  state["last_update"],
        "ping_count":   state["ping_count"],
        "error":        state["error"],
        "trades_today": trades,
    }

@app.post("/api/callback")
def callback(data: dict): return {"status": "received"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
