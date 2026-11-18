import requests
import time
import math
import json
import sqlite3
import logging
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://api.toobit.com"

TIMEFRAMES = ["15m", "1h"]

CANDLE_LIMIT = 150
MAX_WORKERS = 3

MIN_SCORE = 7
MIN_VOLUME_RATIO = 1.2

ATR_PERIOD = 14
STOP_ATR_MULTIPLIER = 1.5

CONTRACT_REFRESH_SECONDS = 900
SCAN_INTERVAL_SECONDS = 60

DATABASE_FILE = "signals_history.db"

REQUEST_TIMEOUT = 15
MAX_RETRIES = 4

# دارایی‌های فلزات گران‌بها که نباید صرفاً
# به دلیل ماهیت فلزی از فهرست حذف شوند.
PRECIOUS_METAL_BASES = {
    "PAXG",
    "XAUT",
    "KAU",
    "KAG",
    "GOLD",
    "SILVER"
}

# نمادهای شناخته‌شده بازارهای سنتی و شاخص‌ها
# این فهرست محافظه‌کارانه است و باید با metadata
# واقعی قراردادهای Toobit تکمیل شود.
TRADFI_BASES = {
    "SPX",
    "SP500",
    "NDX",
    "NASDAQ",
    "DJI",
    "US30",
    "US500",
    "US100",
    "DAX",
    "FTSE",
    "NIKKEI",
    "WTI",
    "BRENT",
    "OIL",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD"
}

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 ToobitScanner/1.0",
    "Accept": "application/json"
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

db_lock = threading.Lock()


# =========================================================
# DATABASE
# =========================================================

def init_database():
    with sqlite3.connect(DATABASE_FILE) as conn:

        conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            timeframe TEXT,
            direction TEXT,
            entry REAL,
            stop REAL,
            tp1 REAL,
            tp2 REAL,
            tp3 REAL,
            score INTEGER,
            status TEXT,
            created_at TEXT,
            closed_at TEXT,
            result REAL
        )
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_symbol
        ON signals(symbol, timeframe, status)
        """)

        conn.commit()


# =========================================================
# HTTP REQUEST
# =========================================================

def get_json(url, params=None):

    for attempt in range(MAX_RETRIES):

        try:
            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code == 429:
                wait_time = min(10 * (attempt + 1), 40)
                logging.warning(
                    "Rate limit. Waiting %s seconds",
                    wait_time
                )
                time.sleep(wait_time)
                continue

            response.raise_for_status()

            return response.json()

        except Exception as error:

            logging.warning(
                "Request error: %s | attempt %s",
                error,
                attempt + 1
            )

            time.sleep(2 * (attempt + 1))

    return None


# =========================================================
# CONTRACT DISCOVERY
# =========================================================

def get_contracts():

    url = BASE_URL + "/api/v1/exchangeInfo"

    data = get_json(url)

    if not data:
        logging.error("Could not load exchangeInfo")
        return []

    contracts = data.get("contracts", [])

    if not isinstance(contracts, list):
        logging.error("Unexpected exchangeInfo structure")
        return []

    selected = []

    for contract in contracts:

        symbol = str(contract.get("symbol", "")).upper()
        status = str(contract.get("status", "")).upper()

        if not symbol.endswith("-SWAP-USDT"):
            continue

        if status and status not in {
            "TRADING",
            "ONLINE",
            "ENABLED"
        }:
            continue

        base = symbol.replace("-SWAP-USDT", "")

        # فلزات گران‌بها حفظ شوند.
        if base in PRECIOUS_METAL_BASES:
            selected.append(symbol)
            continue

        # حذف نمادهای سنتی شناخته‌شده.
        if base in TRADFI_BASES:
            continue

        selected.append(symbol)

    selected = sorted(set(selected))

    logging.info(
        "Active USDT perpetual contracts: %s",
        len(selected)
    )

    return selected


# =========================================================
# CANDLE DATA
# =========================================================

def get_candles(symbol, interval):

    url = BASE_URL + "/quote/v1/klines"

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": CANDLE_LIMIT
    }

    data = get_json(url, params)

    if not data or not isinstance(data, list):
        return None

    candles = []

    for row in data:

        try:

            candles.append({
                "time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5])
            })

        except (ValueError, TypeError, IndexError):
            continue

    # حذف کندل در حال تشکیل
    if len(candles) > 1:
        candles = candles[:-1]

    if len(candles) < 70:
        return None

    return candles


# =========================================================
# EMA
# =========================================================

def ema(values, period):

    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    result = [values[0]]

    for value in values[1:]:

        result.append(
            value * multiplier
            + result[-1] * (1 - multiplier)
        )

    return result


# =========================================================
# RSI
# =========================================================

def rsi(values, period=14):

    if len(values) <= period:
        return 50

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):

        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


# =========================================================
# MACD
# =========================================================

def calculate_macd(values):

    fast = ema(values, 12)
    slow = ema(values, 26)

    if not fast or not slow:
        return 0, 0, 0

    macd_line = [
        fast[i] - slow[i]
        for i in range(len(values))
    ]

    signal_line = ema(macd_line, 9)

    if not signal_line:
        return 0, 0, 0

    macd_value = macd_line[-1]
    signal_value = signal_line[-1]

    histogram = macd_value - signal_value

    return macd_value, signal_value, histogram


# =========================================================
# ATR
# =========================================================

def calculate_atr(candles, period=14):

    if len(candles) <= period:
        return 0

    true_ranges = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        true_ranges.append(tr)

    return sum(true_ranges[-period:]) / period


# =========================================================
# VOLUME RATIO
# =========================================================

def volume_ratio(candles, period=20):

    if len(candles) < period + 1:
        return 0

    previous_volumes = [
        c["volume"]
        for c in candles[-period-1:-1]
    ]

    average_volume = sum(previous_volumes) / len(
        previous_volumes
    )

    if average_volume <= 0:
        return 0

    return candles[-1]["volume"] / average_volume


# =========================================================
# SWING DETECTION
# =========================================================

def confirmed_swings(values, left=3, right=3):

    swing_highs = []
    swing_lows = []

    for i in range(left, len(values) - right):

        current = values[i]

        left_values = values[i-left:i]
        right_values = values[i+1:i+right+1]

        if all(current > x for x in left_values) and \
           all(current >= x for x in right_values):

            swing_highs.append((i, current))

        if all(current < x for x in left_values) and \
           all(current <= x for x in right_values):

            swing_lows.append((i, current))

    return swing_highs, swing_lows


# =========================================================
# MARKET STRUCTURE
# =========================================================

def analyze_structure(candles):

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]

    swing_highs, swing_lows = confirmed_swings(
        closes,
        left=3,
        right=3
    )

    result = {
        "bos": None,
        "choch": None,
        "sweep": None,
        "fvg": None,
        "support": None,
        "resistance": None
    }

    if len(swing_highs) >= 2:

        last_high = swing_highs[-1][1]

        previous_high = swing_highs[-2][1]

        result["resistance"] = last_high

        if closes[-1] > last_high:
            result["bos"] = "BULLISH"

        elif closes[-1] < previous_high:
            result["choch"] = "BEARISH"

    if len(swing_lows) >= 2:

        last_low = swing_lows[-1][1]

        previous_low = swing_lows[-2][1]

        result["support"] = last_low

        if closes[-1] < last_low:
            result["bos"] = "BEARISH"

        elif closes[-1] > previous_low:
            result["choch"] = "BULLISH"

    # Liquidity sweep
    if len(swing_highs) >= 1:

        last_high = swing_highs[-1][1]

        if highs[-1] > last_high and closes[-1] < last_high:
            result["sweep"] = "BUY_SIDE"

    if len(swing_lows) >= 1:

        last_low = swing_lows[-1][1]

        if lows[-1] < last_low and closes[-1] > last_low:
            result["sweep"] = "SELL_SIDE"

    # Fair Value Gap
    if len(candles) >= 3:

        a = candles[-3]
        c = candles[-1]

        if c["low"] > a["high"]:
            result["fvg"] = "BULLISH"

        elif c["high"] < a["low"]:
            result["fvg"] = "BEARISH"

    return result
    # =========================================================
# TRENDLINE ANALYSIS
# =========================================================

def analyze_trendline(candles, direction):

    if len(candles) < 30:
        return None

    atr = calculate_atr(candles)

    if atr <= 0:
        return None

    if direction == "LONG":

        values = [c["low"] for c in candles]

        # کف‌ها برای خط حمایت
        _, pivots = confirmed_swings(values)

        line_type = "SUPPORT"

    else:

        values = [c["high"] for c in candles]

        # سقف‌ها برای خط مقاومت
        pivots, _ = confirmed_swings(values)

        line_type = "RESISTANCE"

    if len(pivots) < 2:
        return None

    best_line = None

    # بررسی جفت‌های مختلف نقاط پیوت
    for i in range(max(0, len(pivots) - 8), len(pivots) - 1):

        for j in range(i + 1, len(pivots)):

            x1, y1 = pivots[i]
            x2, y2 = pivots[j]

            if x2 == x1:
                continue

            slope = (y2 - y1) / (x2 - x1)

            # خطوطی که شیب بسیار شدید دارند حذف شوند.
            if abs(slope) > atr:
                continue

            def line_at(index):
                return y1 + slope * (index - x1)

            touches = 0

            for index, value in pivots:

                expected = line_at(index)

                if abs(value - expected) <= atr * 0.25:
                    touches += 1

            current_line = line_at(len(values) - 1)

            price = candles[-1]["close"]

            # خط شکسته‌شده معتبر نیست.
            if direction == "LONG" and price < current_line - atr * 0.35:
                continue

            if direction == "SHORT" and price > current_line + atr * 0.35:
                continue

            if best_line is None or touches > best_line["touches"]:

                best_line = {
                    "type": line_type,
                    "price": current_line,
                    "touches": touches,
                    "strength": (
                        "STRONG" if touches >= 3
                        else "MODERATE" if touches >= 2
                        else "WEAK"
                    ),
                    "distance_atr": abs(
                        price - current_line
                    ) / atr
                }

    if best_line and best_line["touches"] >= 2:
        return best_line

    return None


# =========================================================
# TIMEFRAME ANALYSIS
# =========================================================

def analyze_timeframe(candles):

    closes = [c["close"] for c in candles]

    ema20_values = ema(closes, 20)
    ema50_values = ema(closes, 50)

    if not ema20_values or not ema50_values:
        return None

    price = closes[-1]

    ema20_value = ema20_values[-1]
    ema50_value = ema50_values[-1]

    rsi_value = rsi(closes)

    macd_value, signal_value, histogram = calculate_macd(
        closes
    )

    atr = calculate_atr(candles)

    vol_ratio = volume_ratio(candles)

    structure = analyze_structure(candles)

    direction = None
    score = 0
    reasons = []

    # روند اصلی
    if price > ema20_value > ema50_value:

        direction = "LONG"
        score += 2
        reasons.append("EMA bullish alignment")

    elif price < ema20_value < ema50_value:

        direction = "SHORT"
        score += 2
        reasons.append("EMA bearish alignment")

    # RSI
    if direction == "LONG":

        if 50 <= rsi_value <= 70:
            score += 1
            reasons.append("RSI bullish")

        elif rsi_value > 75:
            score -= 1
            reasons.append("RSI overbought")

    elif direction == "SHORT":

        if 30 <= rsi_value <= 50:
            score += 1
            reasons.append("RSI bearish")

        elif rsi_value < 25:
            score -= 1
            reasons.append("RSI oversold")

    # MACD
    if direction == "LONG" and histogram > 0:

        score += 1
        reasons.append("MACD bullish")

    elif direction == "SHORT" and histogram < 0:

        score += 1
        reasons.append("MACD bearish")

    # حجم
    if vol_ratio >= MIN_VOLUME_RATIO:

        score += 1
        reasons.append("Volume confirmation")

    # ساختار بازار
    if direction == "LONG":

        if structure["bos"] == "BULLISH":
            score += 2
            reasons.append("Bullish BOS")

        if structure["choch"] == "BULLISH":
            score += 2
            reasons.append("Bullish CHoCH")

        if structure["sweep"] == "SELL_SIDE":
            score += 1
            reasons.append("Sell-side liquidity sweep")

        if structure["fvg"] == "BULLISH":
            score += 1
            reasons.append("Bullish FVG")

    elif direction == "SHORT":

        if structure["bos"] == "BEARISH":
            score += 2
            reasons.append("Bearish BOS")

        if structure["choch"] == "BEARISH":
            score += 2
            reasons.append("Bearish CHoCH")

        if structure["sweep"] == "BUY_SIDE":
            score += 1
            reasons.append("Buy-side liquidity sweep")

        if structure["fvg"] == "BEARISH":
            score += 1
            reasons.append("Bearish FVG")

    if direction:

        trendline = analyze_trendline(
            candles,
            direction
        )

        if trendline:

            if trendline["touches"] >= 3:
                score += 2

            else:
                score += 1

            reasons.append(
                "Trendline " + trendline["strength"]
                + " (" + str(trendline["touches"])
                + " touches)"
            )

        else:
            trendline = None

    else:
        trendline = None

    return {
        "direction": direction,
        "score": score,
        "price": price,
        "ema20": ema20_value,
        "ema50": ema50_value,
        "rsi": rsi_value,
        "macd": macd_value,
        "macd_signal": signal_value,
        "macd_histogram": histogram,
        "atr": atr,
        "volume_ratio": vol_ratio,
        "structure": structure,
        "trendline": trendline,
        "reasons": reasons
    }


# =========================================================
# TRADE LEVELS
# =========================================================

def make_trade_levels(candles, analysis, direction):

    price = analysis["price"]
    atr = analysis["atr"]

    structure = analysis["structure"]

    if atr <= 0:
        return None

    support = structure.get("support")
    resistance = structure.get("resistance")

    trendline = analysis.get("trendline")

    entry_low = None
    entry_high = None
    entry = None
    stop = None

    if direction == "LONG":

        candidates = []

        if support and support < price:
            candidates.append(support)

        if trendline and trendline["type"] == "SUPPORT":
            if trendline["price"] < price:
                candidates.append(trendline["price"])

        if candidates:

            zone_center = max(candidates)

        else:

            zone_center = price - atr * 0.5

        entry_low = zone_center - atr * 0.15
        entry_high = zone_center + atr * 0.15

        entry = (entry_low + entry_high) / 2

        # اگر حمایت خیلی دور باشد، ورود فوری صادر نشود.
        distance = abs(price - entry) / atr

        status = (
            "READY" if distance <= 0.35
            else "WAIT"
        )

        stop_reference = min(
            [x for x in [support, entry] if x is not None]
        )

        stop = stop_reference - atr * STOP_ATR_MULTIPLIER

        risk = entry - stop

        if risk <= 0:
            return None

        tp1 = entry + risk * 1
        tp2 = entry + risk * 2
        tp3 = entry + risk * 3

        if resistance:

            if resistance > entry and resistance < tp1:
                tp1 = resistance

            elif resistance > tp1 and resistance < tp2:
                tp2 = resistance

            elif resistance > tp2 and resistance < tp3:
                tp3 = resistance

    else:

        candidates = []

        if resistance and resistance > price:
            candidates.append(resistance)

        if trendline and trendline["type"] == "RESISTANCE":
            if trendline["price"] > price:
                candidates.append(trendline["price"])

        if candidates:

            zone_center = min(candidates)

        else:

            zone_center = price + atr * 0.5

        entry_low = zone_center - atr * 0.15
        entry_high = zone_center + atr * 0.15

        entry = (entry_low + entry_high) / 2

        distance = abs(price - entry) / atr

        status = (
            "READY" if distance <= 0.35
            else "WAIT"
        )

        stop_reference = max(
            [x for x in [resistance, entry] if x is not None]
        )

        stop = stop_reference + atr * STOP_ATR_MULTIPLIER

        risk = stop - entry

        if risk <= 0:
            return None

        tp1 = entry - risk * 1
        tp2 = entry - risk * 2
        tp3 = entry - risk * 3

        if support:

            if support < entry and support > tp1:
                tp1 = support

            elif support < tp1 and support > tp2:
                tp2 = support

            elif support < tp2 and support > tp3:
                tp3 = support

    if direction == "LONG":

        rr1 = (tp1 - entry) / risk
        rr2 = (tp2 - entry) / risk
        rr3 = (tp3 - entry) / risk

    else:

        rr1 = (entry - tp1) / risk
        rr2 = (entry - tp2) / risk
        rr3 = (entry - tp3) / risk

    return {
        "entry": entry,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk": risk,
        "status": status,
        "rr1": rr1,
        "rr2": rr2,
        "rr3": rr3,
        "stop_distance_percent": abs(
            entry - stop
        ) / entry * 100
    }


# =========================================================
# SIGNAL CREATION
# =========================================================

def build_signal(symbol, timeframe, analysis, candles):

    if not analysis:
        return None

    direction = analysis["direction"]

    if not direction:
        return None

    if analysis["score"] < MIN_SCORE:
        return None

    levels = make_trade_levels(
        candles,
        analysis,
        direction
    )

    if not levels:
        return None

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "score": analysis["score"],
        "price": analysis["price"],
        "entry": levels["entry"],
        "entry_low": levels["entry_low"],
        "entry_high": levels["entry_high"],
        "stop": levels["stop"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "tp3": levels["tp3"],
        "status": levels["status"],
        "rr1": levels["rr1"],
        "rr2": levels["rr2"],
        "rr3": levels["rr3"],
        "stop_distance_percent": levels[
            "stop_distance_percent"
        ],
        "rsi": analysis["rsi"],
        "volume_ratio": analysis["volume_ratio"],
        "reasons": analysis["reasons"],
        "created_at": datetime.now(
            timezone.utc
        ).isoformat()
    }
    # =========================================================
# SCAN ONE SYMBOL
# =========================================================

def scan_symbol(symbol):

    results = []

    for timeframe in TIMEFRAMES:

        try:

            candles = get_candles(
                symbol,
                timeframe
            )

            if not candles:
                continue

            analysis = analyze_timeframe(candles)

            if not analysis:
                continue

            signal = build_signal(
                symbol,
                timeframe,
                analysis,
                candles
            )

            if signal:
                results.append(signal)

        except Exception as error:

            logging.error(
                "Error scanning %s %s: %s",
                symbol,
                timeframe,
                error
            )

    return results


# =========================================================
# DUPLICATE CHECK
# =========================================================

def signal_exists(signal):

    with db_lock:

        with sqlite3.connect(DATABASE_FILE) as conn:

            row = conn.execute("""
                SELECT id
                FROM signals
                WHERE symbol = ?
                AND timeframe = ?
                AND direction = ?
                AND status IN ('READY', 'WAIT', 'TP1 HIT', 'TP2 HIT')
                LIMIT 1
            """, (
                signal["symbol"],
                signal["timeframe"],
                signal["direction"]
            )).fetchone()

    return row is not None


# =========================================================
# SAVE SIGNAL
# =========================================================

def save_signal(signal):

    if signal_exists(signal):
        return False

    with db_lock:

        with sqlite3.connect(DATABASE_FILE) as conn:

            conn.execute("""
                INSERT INTO signals (
                    symbol,
                    timeframe,
                    direction,
                    entry,
                    stop,
                    tp1,
                    tp2,
                    tp3,
                    score,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal["symbol"],
                signal["timeframe"],
                signal["direction"],
                signal["entry"],
                signal["stop"],
                signal["tp1"],
                signal["tp2"],
                signal["tp3"],
                signal["score"],
                signal["status"],
                signal["created_at"]
            ))

            conn.commit()

    return True


# =========================================================
# PRINT SIGNAL
# =========================================================

def print_signal(signal):

    print("\n" + "=" * 65)

    print(
        signal["direction"],
        "|",
        signal["symbol"],
        "|",
        signal["timeframe"]
    )

    print("Score:", signal["score"])

    print("Current price:", signal["price"])

    print(
        "Entry zone:",
        signal["entry_low"],
        "-",
        signal["entry_high"]
    )

    print("Suggested entry:", signal["entry"])

    print("Stop loss:", signal["stop"])

    print("TP1:", signal["tp1"])
    print("TP2:", signal["tp2"])
    print("TP3:", signal["tp3"])

    print("Status:", signal["status"])

    print(
        "Stop distance:",
        round(signal["stop_distance_percent"], 2),
        "%"
    )

    print(
        "Risk / Reward:",
        round(signal["rr1"], 2),
        "|",
        round(signal["rr2"], 2),
        "|",
        round(signal["rr3"], 2)
    )

    print("RSI:", round(signal["rsi"], 2))

    print(
        "Volume ratio:",
        round(signal["volume_ratio"], 2)
    )

    print("Reasons:")

    for reason in signal["reasons"]:
        print("-", reason)

    print("=" * 65)


# =========================================================
# ACTIVE SIGNALS
# =========================================================

def get_active_signals():

    with db_lock:

        with sqlite3.connect(DATABASE_FILE) as conn:

            rows = conn.execute("""
                SELECT
                    id,
                    symbol,
                    timeframe,
                    direction,
                    entry,
                    stop,
                    tp1,
                    tp2,
                    tp3,
                    status
                FROM signals
                WHERE status IN (
                    'READY',
                    'WAIT',
                    'TP1 HIT',
                    'TP2 HIT'
                )
            """).fetchall()

    return rows


# =========================================================
# UPDATE SIGNAL STATUS
# =========================================================

def update_signal_status(signal_id, status):

    with db_lock:

        with sqlite3.connect(DATABASE_FILE) as conn:

            if status in (
                "STOP HIT",
                "TP3 HIT",
                "INVALID"
            ):

                conn.execute("""
                    UPDATE signals
                    SET status = ?,
                        closed_at = ?
                    WHERE id = ?
                """, (
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    signal_id
                ))

            else:

                conn.execute("""
                    UPDATE signals
                    SET status = ?
                    WHERE id = ?
                """, (
                    status,
                    signal_id
                ))

            conn.commit()


# =========================================================
# SIGNAL MONITOR
# =========================================================

def monitor_signals():

    active = get_active_signals()

    if not active:
        return

    print("\n========== ACTIVE SIGNAL MONITOR ==========")

    for row in active:

        (
            signal_id,
            symbol,
            timeframe,
            direction,
            entry,
            stop,
            tp1,
            tp2,
            tp3,
            old_status
        ) = row

        try:

            candles = get_candles(
                symbol,
                timeframe
            )

            if not candles:
                continue

            price = candles[-1]["close"]

            new_status = old_status

            if direction == "LONG":

                # بررسی برخورد به حد ضرر
                if price <= stop:

                    new_status = "STOP HIT"

                elif price >= tp3:

                    new_status = "TP3 HIT"

                elif price >= tp2:

                    new_status = "TP2 HIT"

                elif price >= tp1:

                    new_status = "TP1 HIT"

                elif old_status == "WAIT":

                    if (
                        price >= entry * 0.998
                        and price <= entry * 1.002
                    ):
                        new_status = "READY"

            else:

                if price >= stop:

                    new_status = "STOP HIT"

                elif price <= tp3:

                    new_status = "TP3 HIT"

                elif price <= tp2:

                    new_status = "TP2 HIT"

                elif price <= tp1:

                    new_status = "TP1 HIT"

                elif old_status == "WAIT":

                    if (
                        price >= entry * 0.998
                        and price <= entry * 1.002
                    ):
                        new_status = "READY"

            if new_status != old_status:

                update_signal_status(
                    signal_id,
                    new_status
                )

                print(
                    symbol,
                    timeframe,
                    old_status,
                    "->",
                    new_status,
                    "| Price:",
                    price
                )

            else:

                print(
                    symbol,
                    timeframe,
                    "|",
                    old_status,
                    "| Price:",
                    price
                )

        except Exception as error:

            logging.error(
                "Monitor error %s: %s",
                symbol,
                error
            )


# =========================================================
# PERFORMANCE STATISTICS
# =========================================================

def show_statistics():

    with sqlite3.connect(DATABASE_FILE) as conn:

        rows = conn.execute("""
            SELECT
                direction,
                timeframe,
                status,
                COUNT(*)
            FROM signals
            GROUP BY direction, timeframe, status
            ORDER BY direction, timeframe, status
        """).fetchall()

    print("\n========== SIGNAL STATISTICS ==========")

    if not rows:
        print("No signal history yet.")
        return

    for row in rows:

        direction, timeframe, status, count = row

        print(
            direction,
            "|",
            timeframe,
            "|",
            status,
            "|",
            count
        )

    with sqlite3.connect(DATABASE_FILE) as conn:

        total = conn.execute("""
            SELECT COUNT(*) FROM signals
        """).fetchone()[0]

        completed = conn.execute("""
            SELECT COUNT(*)
            FROM signals
            WHERE status IN (
                'TP1 HIT',
                'TP2 HIT',
                'TP3 HIT',
                'STOP HIT'
            )
        """).fetchone()[0]

        successful = conn.execute("""
            SELECT COUNT(*)
            FROM signals
            WHERE status IN (
                'TP1 HIT',
                'TP2 HIT',
                'TP3 HIT'
            )
        """).fetchone()[0]

    print("---------------------------------------")
    print("Total signals:", total)
    print("Completed signals:", completed)

    if completed:

        win_rate = successful / completed * 100

        print(
            "Target hit rate:",
            round(win_rate, 2),
            "%"
        )

    print("=======================================\n")


# =========================================================
# MAIN SCANNER
# =========================================================

def main():

    init_database()

    print("\n======================================")
    print("       TOOBIT FUTURES SCANNER")
    print("======================================")

    print("Timeframes:", TIMEFRAMES)
    print("Automatic trading: DISABLED")
    print("Contract refresh: 15 minutes")
    print("Database:", DATABASE_FILE)

    contracts = []

    last_contract_refresh = 0

    while True:

        try:

            current_time = time.time()

            # به‌روزرسانی فهرست قراردادها
            if (
                not contracts
                or current_time - last_contract_refresh
                >= CONTRACT_REFRESH_SECONDS
            ):

                print("\nLoading Toobit contracts...")

                new_contracts = get_contracts()

                if new_contracts:

                    contracts = new_contracts

                    last_contract_refresh = current_time

                    print(
                        "Contracts loaded:",
                        len(contracts)
                    )

                elif not contracts:

                    print(
                        "Could not load contracts. Retrying..."
                    )

                    time.sleep(30)
                    continue

            print("\n======================================")
            print("Scanning", len(contracts), "contracts")
            print("Time:", datetime.now().strftime("%H:%M:%S"))
            print("======================================")

            found_signals = 0

            with ThreadPoolExecutor(
                max_workers=MAX_WORKERS
            ) as executor:

                futures = {
                    executor.submit(scan_symbol, symbol): symbol
                    for symbol in contracts
                }

                for future in as_completed(futures):

                    symbol = futures[future]

                    try:

                        signals = future.result()

                        for signal in signals:

                            if save_signal(signal):

                                print_signal(signal)

                                found_signals += 1

                    except Exception as error:

                        logging.error(
                            "Scanner error %s: %s",
                            symbol,
                            error
                        )

            print("\n========== SCAN SUMMARY ==========")

            print("Contracts tested:", len(contracts))
            print("New signals:", found_signals)
            print("Request errors are logged above.")

            # پیگیری سیگنال‌های قبلی
            monitor_signals()

            # آمار
            show_statistics()

            print(
                "Next scan in",
                SCAN_INTERVAL_SECONDS,
                "seconds"
            )

            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:

            print("\nScanner stopped by user.")

            break

        except Exception as error:

            logging.exception(
                "Main loop error: %s",
                error
            )

            print("Retrying in 30 seconds...")
            time.sleep(30)


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()