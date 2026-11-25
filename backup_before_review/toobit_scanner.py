import os
import time
import math
import sqlite3
import logging
import threading
import requests
import base64
import io

from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# =========================================================
# TOOBIT VIP FUTURES SCANNER
# CONFIGURATION
# =========================================================

BASE_URL = "https://api.toobit.com"

TIMEFRAMES = ["15m", "1h"]
CONFIRM_TIMEFRAME = "5m"
HIGHER_TIMEFRAME = "4h"

CANDLE_LIMIT = 200
MAX_WORKERS = 3

SCAN_INTERVAL_SECONDS = 60
SIGNAL_SELECTION_INTERVAL_SECONDS = 1800
MAX_SIGNALS_PER_2H = 4
MAX_DAILY_SIGNALS = 60
DELIVERY_START_HOUR = 7
DELIVERY_END_HOUR = 22
TEHRAN_TZ = ZoneInfo("Asia/Tehran")

CONTRACT_REFRESH_SECONDS = 900

REQUEST_TIMEOUT = 15
MAX_RETRIES = 4

DATABASE_FILE = "signals_history.db"

MIN_SIGNAL_SCORE = 90
MIN_REVIEW_SCORE = 85
MIN_HISTORY_SCORE = 75
MIN_TRENDLINE_TOUCHES = 3
MIN_RR_TP1 = 2.0

ATR_PERIOD = 14
MIN_VOLUME_RATIO = 1.2

STOP_ATR_MULTIPLIER = 1.5

MIN_LEVERAGE = 1
MAX_LEVERAGE = 30

# خروج از حجم اولیه پوزیشن
TP1_CLOSE_FRACTION = 0.50
TP2_CLOSE_FRACTION = 0.25
TP3_CLOSE_FRACTION = 0.25

# حداکثر عمر سیگنال بر حسب ساعت
SIGNAL_MAX_AGE_HOURS = 24
RETEST_MAX_BARS = 8
CONTRACT_CACHE_SECONDS = CONTRACT_REFRESH_SECONDS
CANDLE_CACHE_SECONDS = 10



# =========================================================
# TELEGRAM BRIDGE
# =========================================================

BRIDGE_URL = os.getenv(
    "TOOBIT_BRIDGE_URL",
    "https://toobit-telegram-bridge.dctoranj.workers.dev/send"
)

BRIDGE_SECRET = os.getenv(
    "TOOBIT_BRIDGE_SECRET",
    ""
)

PHOTO_URL = (
    BRIDGE_URL.rsplit("/send", 1)[0] + "/send-photo"
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =========================================================
# HTTP SESSION
# =========================================================

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "Toobit-VIP-Scanner/3.0",
    "Accept": "application/json"
})

DB_LOCK = threading.Lock()
CANDIDATE_LOCK = threading.Lock()
CANDLE_CACHE_LOCK = threading.Lock()
CANDLE_CACHE = {}


# =========================================================
# GLOBAL STATE
# =========================================================

contracts = []

last_contract_refresh = 0

signal_candidates = []

daily_signal_count = 0

daily_date = date.today()


# =========================================================
# SYMBOL FILTERS
# =========================================================

PRECIOUS_METALS = {
    "PAXG",
    "XAUT",
    "KAU",
    "KAG",
    "GOLD",
    "SILVER"
}

TRADFI_BASES = {
    "SPX", "SP500", "NDX", "NASDAQ",
    "DJI", "US30", "US500", "US100",
    "DAX", "FTSE", "NIKKEI",
    "WTI", "BRENT", "OIL",
    "EURUSD", "GBPUSD", "USDJPY",
    "AUDUSD", "USDCAD"
}


# =========================================================
# HTTP REQUEST
# =========================================================



def get_json(url, params=None):
    for attempt in range(MAX_RETRIES):
        try:
            response = SESSION.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = float(retry_after)
                except (TypeError, ValueError):
                    wait_seconds = min(60, 2 ** (attempt + 1))

                if attempt < MAX_RETRIES - 1:
                    wait_seconds = max(1, min(wait_seconds, 120))
                    logging.warning(
                        "Toobit rate limit (429). Waiting %.1f seconds.",
                        wait_seconds
                    )
                    time.sleep(wait_seconds)
                    continue

                logging.error("Toobit rate limit persists after retries.")
                return None

            response.raise_for_status()
            return response.json()

        except requests.exceptions.HTTPError as error:
            status = (
                error.response.status_code
                if error.response is not None
                else None
            )

            if status is not None and 400 <= status < 500:
                logging.error(
                    "Non-retryable HTTP error %s: %s",
                    status,
                    error
                )
                return None

            logging.warning(
                "Request attempt %s/%s failed: %s",
                attempt + 1,
                MAX_RETRIES,
                error
            )

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError
        ) as error:
            logging.warning(
                "Request attempt %s/%s failed: %s",
                attempt + 1,
                MAX_RETRIES,
                error
            )

        # Must precede RequestException because Requests JSONDecodeError
        # can inherit from both ValueError and RequestException.
        except ValueError as error:
            logging.warning(
                "Invalid JSON response on attempt %s/%s: %s",
                attempt + 1,
                MAX_RETRIES,
                error
            )

        except requests.exceptions.RequestException as error:
            logging.error("Request failed: %s", error)
            return None

        if attempt < MAX_RETRIES - 1:
            time.sleep(min(2 ** attempt, 8))

    logging.error("Request failed after %s attempts.", MAX_RETRIES)
    return None



def init_database():

    with DB_LOCK:

        with sqlite3.connect(DATABASE_FILE) as conn:

            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry REAL,
                    stop REAL,
                    tp1 REAL,
                    tp2 REAL,
                    tp3 REAL,
                    score INTEGER,
                    status TEXT,
                    created_at TEXT,
                    closed_at TEXT,
                    result REAL DEFAULT 0,
                    leverage INTEGER DEFAULT 1,
                    tp1_done INTEGER DEFAULT 0,
                    tp2_done INTEGER DEFAULT 0,
                    tp3_done INTEGER DEFAULT 0,
                    initial_risk REAL DEFAULT 0,
                    setup_key TEXT DEFAULT ''
                )
            """)

            conn.commit()

    ensure_database_columns()


def ensure_database_columns():

    required = {
        "leverage": "INTEGER DEFAULT 1",
        "tp1_done": "INTEGER DEFAULT 0",
        "tp2_done": "INTEGER DEFAULT 0",
        "tp3_done": "INTEGER DEFAULT 0",
        "initial_risk": "REAL DEFAULT 0",
        "result": "REAL DEFAULT 0",
        "setup_key": "TEXT DEFAULT ''"
    }

    with DB_LOCK:

        with sqlite3.connect(DATABASE_FILE) as conn:

            existing = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(signals)"
                ).fetchall()
            }

            for column, definition in required.items():

                if column not in existing:

                    conn.execute(
                        f"ALTER TABLE signals ADD COLUMN "
                        f"{column} {definition}"
                    )

            conn.commit()


# =========================================================
# CONTRACT LIST
# =========================================================


def get_contracts(force_refresh=False):
    global contracts, last_contract_refresh
    now = time.time()
    if contracts and not force_refresh and now - last_contract_refresh < CONTRACT_CACHE_SECONDS:
        return list(contracts)

    data = get_json(BASE_URL + "/api/v1/exchangeInfo")
    if not isinstance(data, dict):
        logging.warning("Invalid exchangeInfo response.")
        return list(contracts)

    raw_contracts = data.get("contracts", [])
    if not isinstance(raw_contracts, list):
        logging.warning("exchangeInfo contracts field is not a list.")
        return list(contracts)

    result = []
    for contract in raw_contracts:
        if not isinstance(contract, dict):
            continue
        symbol = str(contract.get("symbol", "")).upper()
        status = str(contract.get("status", "")).upper()
        # Toobit documents ONLINE as not tradable for futures contracts.
        if status != "TRADING" or not symbol.endswith("-SWAP-USDT"):
            continue
        base = symbol[:-len("-SWAP-USDT")]
        # Filter known index/FX/commodity tickers by their actual base symbol.
        # Do not filter by the broad API category: Toobit's example also tags BTC
        # with "TradFi", so that category is not a reliable exclusion signal.
        if base in TRADFI_BASES and base not in PRECIOUS_METALS:
            continue
        result.append(symbol)

    if result:
        contracts = sorted(set(result))
        last_contract_refresh = now
        logging.info("Loaded %s active Toobit USDT-M contracts.", len(contracts))
    else:
        logging.warning("No TRADING USDT-M swap contracts found.")
    return list(contracts)

def normalize_symbol(user_symbol):

    symbol = str(user_symbol).strip().upper()

    symbol = symbol.replace("/", "")
    symbol = symbol.replace(" ", "")

    if symbol.endswith("-SWAP-USDT"):
        return symbol

    if symbol.endswith("USDT"):
        symbol = symbol[:-4]

    if symbol.endswith("-SWAP"):
        symbol = symbol[:-5]

    symbol = symbol.replace("-", "")

    return symbol + "-SWAP-USDT"


# =========================================================
# CANDLE DATA
# =========================================================




def get_candles(symbol, interval, limit=CANDLE_LIMIT):
    interval_ms_map = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000,
        "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "2h": 7_200_000,
        "4h": 14_400_000, "6h": 21_600_000,
        "8h": 28_800_000, "12h": 43_200_000,
        "1d": 86_400_000, "1w": 604_800_000
    }

    interval_ms = interval_ms_map.get(interval)
    if interval_ms is None:
        logging.error("Unsupported candle interval: %s", interval)
        return []

    try:
        requested_limit = int(limit)
        if requested_limit < 1:
            return []
        requested_limit = min(1000, requested_limit)
    except (TypeError, ValueError, OverflowError):
        logging.error("Invalid candle limit: %s", limit)
        return []

    cache_key = (symbol, interval, requested_limit)
    now_mono = time.monotonic()
    with CANDLE_CACHE_LOCK:
        cached = CANDLE_CACHE.get(cache_key)
        if cached and now_mono - cached[0] <= CANDLE_CACHE_SECONDS:
            return [dict(candle) for candle in cached[1]]

    end_time = int(time.time() * 1000)
    start_time = end_time - interval_ms * requested_limit

    data = get_json(
        BASE_URL + "/quote/v1/klines",
        params={
            "symbol": symbol,
            "interval": interval,
            "startTime": start_time,
            "endTime": end_time,
            "limit": requested_limit
        }
    )

    if isinstance(data, dict):
        data = data.get("data", data.get("result"))

    if not isinstance(data, list):
        logging.warning(
            "Invalid candle response: %s %s",
            symbol,
            interval
        )
        return []

    candles = []
    invalid_count = 0

    for item in data:
        try:
            if isinstance(item, dict):
                timestamp = item.get("time", item.get("t"))
                op = item.get("open", item.get("o"))
                hi = item.get("high", item.get("h"))
                lo = item.get("low", item.get("l"))
                cl = item.get("close", item.get("c"))
                vol = item.get("volume", item.get("v", 0))

            elif isinstance(item, (list, tuple)) and len(item) >= 6:
                timestamp, op, hi, lo, cl, vol = item[:6]

            else:
                invalid_count += 1
                continue

            timestamp = int(timestamp)

            if timestamp < 1_000_000_000_000:
                timestamp *= 1000

            op = float(op)
            hi = float(hi)
            lo = float(lo)
            cl = float(cl)
            vol = float(vol)

            values = (op, hi, lo, cl, vol)

            if not all(math.isfinite(value) for value in values):
                invalid_count += 1
                continue

            if timestamp <= 0 or min(op, hi, lo, cl) <= 0 or vol < 0:
                invalid_count += 1
                continue

            if hi < lo or hi < max(op, cl) or lo > min(op, cl):
                invalid_count += 1
                continue

            candles.append({
                "time": timestamp,
                "open": op,
                "high": hi,
                "low": lo,
                "close": cl,
                "volume": vol
            })

        except (ValueError, TypeError, OverflowError, IndexError, KeyError):
            invalid_count += 1
            continue

    if invalid_count:
        logging.warning(
            "Skipped %s invalid candles for %s %s.",
            invalid_count,
            symbol,
            interval
        )

    candles = sorted(
        {c["time"]: c for c in candles}.values(),
        key=lambda c: c["time"]
    )

    # Keep only candles that have fully closed.
    now_ms = int(time.time() * 1000)
    candles = [
        candle for candle in candles
        if candle["time"] + interval_ms <= now_ms
    ]

    with CANDLE_CACHE_LOCK:
        if len(CANDLE_CACHE) > 5000:
            expired_keys = [
                key for key, value in CANDLE_CACHE.items()
                if now_mono - value[0] > CANDLE_CACHE_SECONDS
            ]
            for key in expired_keys:
                CANDLE_CACHE.pop(key, None)
        CANDLE_CACHE[cache_key] = (time.monotonic(), [dict(candle) for candle in candles])

    return candles


def candle_values(candles, key):
    return [float(c[key]) for c in candles]


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (value * multiplier) + (result * (1 - multiplier))

    return result


def ema_series(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period
    output = [None] * (period - 1) + [result]

    for value in values[period:]:
        result = (value * multiplier) + (result * (1 - multiplier))
        output.append(result)

    return output



def calculate_rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        change = float(values[i]) - float(values[i - 1])
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_atr(candles, period=ATR_PERIOD):
    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        previous_close = float(candles[i - 1]["close"])

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    atr_value = sum(true_ranges[:period]) / period

    for tr in true_ranges[period:]:
        atr_value = ((atr_value * (period - 1)) + tr) / period

    return atr_value


def calculate_macd(values):
    if len(values) < 35:
        return None

    fast_series = ema_series(values, 12)
    slow_series = ema_series(values, 26)

    macd_values = []

    for fast_value, slow_value in zip(fast_series, slow_series):
        if fast_value is not None and slow_value is not None:
            macd_values.append(fast_value - slow_value)

    if len(macd_values) < 9:
        return None

    signal_value = ema(macd_values, 9)
    macd_value = macd_values[-1]

    if signal_value is None:
        return None

    return {
        "macd": macd_value,
        "signal": signal_value,
        "histogram": macd_value - signal_value
    }


def calculate_volume_ratio(candles, period=20):
    if len(candles) < period + 1:
        return None

    volumes = candle_values(candles, "volume")
    average_volume = sum(volumes[-period-1:-1]) / period

    if average_volume <= 0:
        return None

    return volumes[-1] / average_volume


def calculate_indicators(candles):
    if len(candles) < 50:
        return None

    closes = candle_values(candles, "close")
    macd_data = calculate_macd(closes)

    return {
        "price": closes[-1],
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "rsi": calculate_rsi(closes, 14),
        "atr": calculate_atr(candles, ATR_PERIOD),
        "volume_ratio": calculate_volume_ratio(candles),
        "macd": macd_data
    }


# =========================================================
# END OF TECHNICAL INDICATORS
# =========================================================


# =========================================================
# SWING POINTS, TRENDLINES AND FIBONACCI
# =========================================================

def find_swings(candles, left=3, right=3):
    highs = []
    lows = []

    if len(candles) < left + right + 5:
        return highs, lows

    for i in range(left, len(candles) - right):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])

        previous_highs = [
            float(candles[j]["high"])
            for j in range(i - left, i)
        ]
        next_highs = [
            float(candles[j]["high"])
            for j in range(i + 1, i + right + 1)
        ]

        previous_lows = [
            float(candles[j]["low"])
            for j in range(i - left, i)
        ]
        next_lows = [
            float(candles[j]["low"])
            for j in range(i + 1, i + right + 1)
        ]

        if high > max(previous_highs) and high >= max(next_highs):
            highs.append({
                "index": i,
                "price": high
            })

        if low < min(previous_lows) and low <= min(next_lows):
            lows.append({
                "index": i,
                "price": low
            })

    return highs, lows


def calculate_line(points):
    if len(points) < 2:
        return None

    xs = [float(p["index"]) for p in points]
    ys = [float(p["price"]) for p in points]

    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)

    denominator = sum((x - mean_x) ** 2 for x in xs)

    if denominator == 0:
        return None

    slope = sum(
        (xs[i] - mean_x) * (ys[i] - mean_y)
        for i in range(len(xs))
    ) / denominator

    intercept = mean_y - slope * mean_x

    return {
        "slope": slope,
        "intercept": intercept,
        "start_index": int(min(xs)),
        "end_index": int(max(xs)),
        "touches": len(points),
        "points": points
    }


def line_price(line, index):
    return line["slope"] * index + line["intercept"]


def analyze_trendline(candles, direction, atr_value):
    if atr_value is None or atr_value <= 0:
        return None

    swing_highs, swing_lows = find_swings(candles)

    if direction == "LONG":
        # Descending resistance line: breakout upward.
        points = swing_highs[-8:]
        expected_slope = -1

    elif direction == "SHORT":
        # Ascending support line: breakout downward.
        points = swing_lows[-8:]
        expected_slope = 1

    else:
        return None

    if len(points) < MIN_TRENDLINE_TOUCHES:
        return None

    line = calculate_line(points)

    if line is None:
        return None

    slope = line["slope"]

    # Reject flat or incorrectly sloped trendlines.
    if direction == "LONG" and slope >= 0:
        return None

    if direction == "SHORT" and slope <= 0:
        return None

    # Reject excessively steep trendlines.
    if abs(slope) > atr_value * 1.5:
        return None

    # Count genuine touches near the fitted line.
    valid_touches = []

    for point in points:
        expected_price = line_price(line, point["index"])

        if abs(point["price"] - expected_price) <= atr_value * 0.45:
            valid_touches.append(point)

    if len(valid_touches) < MIN_TRENDLINE_TOUCHES:
        return None

    # Touches must be distributed across the chart.
    touch_indices = [p["index"] for p in valid_touches]

    if max(touch_indices) - min(touch_indices) < 12:
        return None

    line["points"] = valid_touches
    line["touches"] = len(valid_touches)
    line["current_price"] = line_price(line, len(candles) - 1)

    return line



def analyze_breakout(candles, line, direction, atr_value, index=None):
    if line is None or atr_value is None or atr_value <= 0:
        return None
    if index is None:
        index = len(candles) - 1
    if index < 1 or index >= len(candles):
        return None

    previous, current = candles[index - 1], candles[index]
    previous_close = float(previous["close"])
    current_close = float(current["close"])
    current_open = float(current["open"])
    previous_line = line_price(line, index - 1)
    current_line = line_price(line, index)
    body_ratio = abs(current_close - current_open) / atr_value

    if direction == "LONG":
        crossed = previous_close <= previous_line and current_close > current_line
        distance = current_close - current_line
    elif direction == "SHORT":
        crossed = previous_close >= previous_line and current_close < current_line
        distance = current_line - current_close
    else:
        return None

    if not crossed or distance < atr_value * 0.05:
        return None

    volume_ratio = calculate_volume_ratio(candles[:index + 1])
    score = 0
    if line.get("touches", 0) >= 3:
        score += 9
    if line.get("touches", 0) >= 4:
        score += 3
    if body_ratio >= 0.5:
        score += 5
    if body_ratio >= 0.8:
        score += 5
    if distance >= atr_value * 0.15:
        score += 4
    if volume_ratio is not None and volume_ratio >= MIN_VOLUME_RATIO:
        score += 5

    return {
        "direction": direction, "price": current_close,
        "line_price": current_line, "distance": distance,
        "body_ratio": body_ratio, "volume_ratio": volume_ratio,
        "score": score, "touches": line.get("touches", 0),
        "index": index, "candle": current
    }


def fibonacci_levels(candles, lookback=100, direction=None):
    if len(candles) < 20:
        return {}
    recent = candles[-lookback:]
    highs, lows = find_swings(recent)
    swing_high = swing_low = None

    if direction == "LONG":
        for high in reversed(highs):
            prior = [low for low in lows if low["index"] < high["index"]]
            if prior:
                swing_high, swing_low = high, prior[-1]
                break
    elif direction == "SHORT":
        for low in reversed(lows):
            prior = [high for high in highs if high["index"] < low["index"]]
            if prior:
                swing_low, swing_high = low, prior[-1]
                break

    if swing_high is None or swing_low is None:
        highest = max(float(c["high"]) for c in recent)
        lowest = min(float(c["low"]) for c in recent)
    else:
        highest, lowest = float(swing_high["price"]), float(swing_low["price"])

    price_range = highest - lowest
    if price_range <= 0:
        return {}

    if direction == "SHORT":
        return {
            "0": lowest, "0.236": lowest + price_range * 0.236,
            "0.382": lowest + price_range * 0.382, "0.5": lowest + price_range * 0.5,
            "0.618": lowest + price_range * 0.618, "0.786": lowest + price_range * 0.786,
            "1": highest
        }
    return {
        "0": highest, "0.236": highest - price_range * 0.236,
        "0.382": highest - price_range * 0.382, "0.5": highest - price_range * 0.5,
        "0.618": highest - price_range * 0.618, "0.786": highest - price_range * 0.786,
        "1": lowest
    }

def fibonacci_extensions(high, low, direction):
    price_range = high - low

    if price_range <= 0:
        return {}

    if direction == "LONG":
        return {
            "1.272": high + price_range * 0.272,
            "1.618": high + price_range * 0.618,
            "2.0": high + price_range
        }

    return {
        "1.272": low - price_range * 0.272,
        "1.618": low - price_range * 0.618,
        "2.0": low - price_range
    }


# =========================================================
# END OF TRENDLINES AND FIBONACCI
# =========================================================


# =========================================================
# CANDLE PATTERNS, RETEST AND CONFIRMATION
# =========================================================


def detect_candle_pattern(candles, direction):
    if len(candles) < 3:
        return None
    previous, current = candles[-2], candles[-1]
    po, pc = float(previous["open"]), float(previous["close"])
    co, cc = float(current["open"]), float(current["close"])
    ch, cl = float(current["high"]), float(current["low"])
    body = abs(cc - co)
    previous_body = abs(pc - po)
    candle_range = ch - cl
    if candle_range <= 0:
        return None

    min_body = candle_range * 0.04
    upper_wick = max(0.0, ch - max(co, cc))
    lower_wick = max(0.0, min(co, cc) - cl)
    bullish, bearish = cc > co, cc < co

    bullish_engulfing = (
        pc < po and bullish and co <= pc and cc >= po
        and body >= max(previous_body * 0.9, min_body)
    )
    bearish_engulfing = (
        pc > po and bearish and co >= pc and cc <= po
        and body >= max(previous_body * 0.9, min_body)
    )
    if direction == "LONG" and bullish_engulfing:
        return {"name": "Bullish Engulfing", "score": 15}
    if direction == "SHORT" and bearish_engulfing:
        return {"name": "Bearish Engulfing", "score": 15}

    if body >= min_body:
        if direction == "LONG":
            if lower_wick >= body * 2 and upper_wick <= body * 0.7 and cc >= cl + candle_range * 0.55:
                return {"name": "Hammer", "score": 10}
            if upper_wick >= body * 2 and lower_wick <= body * 0.7 and cc >= cl + candle_range * 0.55:
                return {"name": "Inverted Hammer", "score": 8}
        if direction == "SHORT":
            if upper_wick >= body * 2 and lower_wick <= body * 0.7 and cc <= cl + candle_range * 0.45:
                return {"name": "Shooting Star", "score": 10}
            if lower_wick >= body * 2 and upper_wick <= body * 0.7 and cc <= cl + candle_range * 0.45:
                return {"name": "Hanging Man", "score": 8}

    if direction == "LONG" and lower_wick >= candle_range * 0.60 and bullish:
        return {"name": "Bullish Pin Bar", "score": 9}
    if direction == "SHORT" and upper_wick >= candle_range * 0.60 and bearish:
        return {"name": "Bearish Pin Bar", "score": 9}
    return None


def analyze_retest(candles, line, direction, atr_value, index=None):
    if line is None or atr_value is None or atr_value <= 0:
        return None
    if index is None:
        index = len(candles) - 1
    if index < 0 or index >= len(candles):
        return None

    current = candles[index]
    high, low, close = float(current["high"]), float(current["low"]), float(current["close"])
    expected_line = line_price(line, index)
    tolerance = atr_value * 0.35

    if direction == "LONG":
        touched, held = low <= expected_line + tolerance, close > expected_line
    elif direction == "SHORT":
        touched, held = high >= expected_line - tolerance, close < expected_line
    else:
        return None
    if not touched or not held:
        return None

    return {
        "confirmed": True, "line_price": expected_line,
        "distance": abs(close - expected_line),
        "index": index, "candle": current
    }



def confirm_five_minute(
    symbol, direction, line_price_value, atr_value,
    not_before_time=None, not_after_time=None
):
    """Find the first valid 5m confirmation in the bounded post-setup window."""
    candles = get_candles(symbol, CONFIRM_TIMEFRAME, limit=200)
    if not candles or len(candles) < 30:
        return None

    now_ms = int(time.time() * 1000)
    lower = int(not_before_time) if not_before_time is not None else int(candles[-1]["time"])
    upper = int(not_after_time) if not_after_time is not None else now_ms

    for index, candle in enumerate(candles):
        close_time = int(candle["time"]) + 300_000
        if close_time < lower:
            continue
        if close_time > upper:
            break
        if now_ms - close_time > 600_000:
            continue
        if index < 29:
            continue

        recent = candles[index - 29:index + 1]
        if any(int(b["time"]) - int(a["time"]) != 300_000 for a, b in zip(recent, recent[1:])):
            continue

        prefix = candles[:index + 1]
        pattern = detect_candle_pattern(prefix, direction)
        if pattern is None:
            continue
        close = float(candle["close"])
        if atr_value is not None and atr_value > 0:
            if abs(close - float(line_price_value)) > float(atr_value):
                continue
        volume_ratio = calculate_volume_ratio(prefix)
        return {
            "confirmed": True, "pattern": pattern["name"],
            "score": int(pattern["score"]), "price": close,
            "high": float(candle["high"]), "low": float(candle["low"]),
            "time": int(candle["time"]), "close_time": close_time,
            "volume_ratio": volume_ratio,
        }
    return None




def analyze_higher_trend(candles):
    if not candles or len(candles) < 60:
        return "NEUTRAL"

    closes = candle_values(candles, "close")

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    if ema20 is None or ema50 is None:
        return "NEUTRAL"

    if closes[-1] > ema20 > ema50:
        return "LONG"

    if closes[-1] < ema20 < ema50:
        return "SHORT"

    return "NEUTRAL"



def find_support_resistance(candles, lookback=100):
    if not candles or len(candles) < 20:
        return {"support": None, "resistance": None}

    recent = candles[-lookback:]
    current_price = float(recent[-1]["close"])
    atr_value = calculate_atr(recent) or current_price * 0.001
    tolerance = max(atr_value * 0.35, current_price * 0.0002)
    swing_highs, swing_lows = find_swings(recent)

    def cluster_prices(points):
        values = sorted(float(point["price"]) for point in points)
        clusters = []
        for value in values:
            if not clusters or abs(value - sum(clusters[-1]) / len(clusters[-1])) > tolerance:
                clusters.append([value])
            else:
                clusters[-1].append(value)
        # A repeated pivot is stronger than a single isolated wick.
        return [
            sum(cluster) / len(cluster)
            for cluster in clusters if len(cluster) >= 2
        ]

    support_levels = cluster_prices(swing_lows)
    resistance_levels = cluster_prices(swing_highs)

    supports = [level for level in support_levels if level < current_price]
    resistances = [level for level in resistance_levels if level > current_price]

    # If no repeated cluster exists, fall back to nearest confirmed swing pivot.
    if not supports:
        supports = [
            float(point["price"]) for point in swing_lows
            if float(point["price"]) < current_price
        ]
    if not resistances:
        resistances = [
            float(point["price"]) for point in swing_highs
            if float(point["price"]) > current_price
        ]

    return {
        "support": max(supports) if supports else None,
        "resistance": min(resistances) if resistances else None,
        "support_levels": sorted(supports),
        "resistance_levels": sorted(resistances)
    }

def analyze_indicators(candles, direction):
    data = calculate_indicators(candles)

    if data is None:
        return None

    if any(
        data[key] is None
        for key in ("ema20", "ema50", "rsi", "atr", "volume_ratio")
    ):
        return None

    macd_data = data["macd"]

    if macd_data is None:
        return None

    score = 0

    if direction == "LONG":
        if data["price"] > data["ema20"]:
            score += 3
        if data["ema20"] > data["ema50"]:
            score += 3
        if 45 <= data["rsi"] <= 70:
            score += 3
        if macd_data["histogram"] > 0:
            score += 4

    elif direction == "SHORT":
        if data["price"] < data["ema20"]:
            score += 3
        if data["ema20"] < data["ema50"]:
            score += 3
        if 30 <= data["rsi"] <= 55:
            score += 3
        if macd_data["histogram"] < 0:
            score += 4

    data["score"] = score
    return data


# =========================================================
# END OF CANDLE PATTERNS AND CONFIRMATION
# =========================================================


# =========================================================
# TRADE LEVELS AND SIGNAL SCORING
# =========================================================


def make_trade_levels(candles, direction, atr_value, support_resistance, entry_price=None):
    if not candles or atr_value is None or atr_value <= 0:
        return None
    entry = float(entry_price if entry_price is not None else candles[-1]["close"])
    if entry <= 0:
        return None
    recent = candles[-30:]
    recent_low = min(float(c["low"]) for c in recent)
    recent_high = max(float(c["high"]) for c in recent)

    if direction == "LONG":
        structural_stop = recent_low - atr_value * 0.15
        stop = min(structural_stop, entry - atr_value * STOP_ATR_MULTIPLIER)
        risk = entry - stop
        if risk <= 0:
            return None
        resistance = support_resistance.get("resistance")
        if resistance is not None and resistance <= entry:
            resistance = None
        target1, target2, target3 = entry + risk * 2, entry + risk * 3, entry + risk * 4
        if resistance is not None and resistance < target1:
            return None
    elif direction == "SHORT":
        structural_stop = recent_high + atr_value * 0.15
        stop = max(structural_stop, entry + atr_value * STOP_ATR_MULTIPLIER)
        risk = stop - entry
        if risk <= 0:
            return None
        support = support_resistance.get("support")
        if support is not None and support >= entry:
            support = None
        target1, target2, target3 = entry - risk * 2, entry - risk * 3, entry - risk * 4
        if support is not None and support > target1:
            return None
    else:
        return None

    risk_percent = abs(entry - stop) / entry * 100.0
    if risk_percent <= 0:
        return None
    # Target roughly 2% gross loss on initial margin when the stop distance permits it.
    # With a minimum of 1x, a stop wider than 2% will exceed that target.
    # Not a liquidation guarantee; fees, funding, slippage and maintenance margin are excluded.
    suggested_leverage = max(
        MIN_LEVERAGE,
        min(MAX_LEVERAGE, int(math.floor(2.0 / risk_percent)))
    )
    return {
        "entry": entry, "stop_loss": stop,
        "tp1": target1, "tp2": target2, "tp3": target3,
        "risk": risk, "risk_percent": risk_percent,
        "rr_tp1": abs(target1 - entry) / risk,
        "rr_tp2": abs(target2 - entry) / risk,
        "rr_tp3": abs(target3 - entry) / risk,
        "leverage": suggested_leverage,
        "margin_stop_loss_percent": risk_percent * suggested_leverage
    }

def fibonacci_confluence(price, levels, atr_value):
    if not levels or atr_value is None or atr_value <= 0:
        return []

    matches = []

    for name, level in levels.items():
        if abs(price - level) <= atr_value * 0.25:
            matches.append({
                "level": name,
                "price": level
            })

    return matches


def calculate_signal_score(
    breakout, retest, confirmation, indicator_data,
    higher_trend, fib_matches, direction
):
    # Maximum possible score = 98 (indicator model currently contributes 13).
    score = 0

    if breakout:
        score += 15
        if breakout.get("touches", 0) >= 4:
            score += 5
        if breakout.get("body_ratio", 0) >= 0.8:
            score += 5

    if retest:
        score += 15
    elif breakout and breakout.get("score", 0) >= 25:
        score += 7

    if confirmation:
        pattern = confirmation.get("pattern", "")
        if pattern in ("Bullish Engulfing", "Bearish Engulfing"):
            score += 20
        elif pattern in ("Hammer", "Shooting Star"):
            score += 15
        elif pattern in ("Inverted Hammer", "Hanging Man", "Bullish Pin Bar", "Bearish Pin Bar"):
            score += 12

    if indicator_data:
        score += min(13, int(indicator_data.get("score", 0)))

    if higher_trend == direction:
        score += 10
    elif higher_trend == "NEUTRAL":
        score += 5

    # Breakout and 5m confirmation volumes are distinct observations.
    breakout_volume = breakout.get("volume_ratio") if breakout else None
    confirm_volume = confirmation.get("volume_ratio") if confirmation else None
    if breakout_volume is not None:
        if breakout_volume >= 2.0:
            score += 6
        elif breakout_volume >= MIN_VOLUME_RATIO:
            score += 4
    if confirm_volume is not None:
        if confirm_volume >= 2.0:
            score += 4
        elif confirm_volume >= MIN_VOLUME_RATIO:
            score += 2

    if fib_matches:
        score += 5

    return min(100, int(score))




def build_signal(symbol, timeframe, direction, candles):
    """Build a signal using only candles closed by the 5m confirmation time."""
    interval_ms = {"15m": 900_000, "1h": 3_600_000}.get(timeframe)
    if interval_ms is None or not candles or len(candles) < 100:
        return None

    setup = None
    five_minute = None
    first_index = max(100, len(candles) - RETEST_MAX_BARS - 1)

    # Use the first valid breakout in the recent window, matching the chronological backtest.
    for breakout_index in range(first_index, len(candles)):
        prefix = candles[:breakout_index]
        if len(prefix) < 70:
            continue
        prefix_atr = calculate_atr(prefix)
        if prefix_atr is None or prefix_atr <= 0:
            continue
        line = analyze_trendline(prefix, direction, prefix_atr)
        if line is None:
            continue
        breakout = analyze_breakout(candles, line, direction, prefix_atr, index=breakout_index)
        if breakout is None:
            continue

        retest = None
        for retest_index in range(breakout_index + 1, min(len(candles), breakout_index + RETEST_MAX_BARS + 1)):
            retest_history = candles[:retest_index + 1]
            retest_atr = calculate_atr(retest_history)
            if retest_atr is None or retest_atr <= 0:
                continue
            candidate = analyze_retest(candles, line, direction, retest_atr, index=retest_index)
            if candidate:
                retest = candidate
                break

        if retest is None:
            # Live immediate-breakout signals can only use the latest closed candle.
            if breakout_index != len(candles) - 1 or breakout.get("score", 0) < 25:
                continue
            reference_index = breakout_index
            reference_candle = breakout["candle"]
        else:
            reference_index = int(retest["index"])
            reference_candle = retest["candle"]

        reference_close_time = int(reference_candle["time"]) + interval_ms
        candidate_setup = {
            "line": line,
            "breakout": breakout,
            "retest": retest,
            "reference_candle": reference_candle,
            "reference_index": reference_index,
            "reference_close_time": reference_close_time,
            "prefix_atr": prefix_atr,
        }
        line_at_reference = line_price(line, reference_index)
        candidate_confirmation = confirm_five_minute(
            symbol, direction, line_at_reference, prefix_atr,
            not_before_time=reference_close_time,
            not_after_time=reference_close_time + 600_000,
        )
        if candidate_confirmation is None:
            continue
        setup = candidate_setup
        five_minute = candidate_confirmation
        break

    if setup is None or five_minute is None:
        return None

    confirmation_close_time = int(five_minute["close_time"])
    closed_main = [
        candle for candle in candles
        if int(candle["time"]) + interval_ms <= confirmation_close_time
    ]
    if len(closed_main) < 100:
        return None

    indicators_now = calculate_indicators(closed_main)
    if not indicators_now or not indicators_now.get("atr"):
        return None
    atr_value = float(indicators_now["atr"])
    indicator_data = analyze_indicators(closed_main, direction)
    if indicator_data is None:
        return None

    higher_all = get_candles(symbol, HIGHER_TIMEFRAME, limit=200)
    higher_closed = [
        candle for candle in (higher_all or [])
        if int(candle["time"]) + 14_400_000 <= confirmation_close_time
    ]
    higher_trend = analyze_higher_trend(higher_closed)
    support_resistance = find_support_resistance(closed_main)
    levels = make_trade_levels(
        closed_main, direction, atr_value, support_resistance,
        entry_price=five_minute["price"]
    )
    if levels is None or levels["rr_tp1"] < MIN_RR_TP1:
        return None

    fib_levels = fibonacci_levels(closed_main, direction=direction)
    fib_matches = fibonacci_confluence(levels["entry"], fib_levels, atr_value)
    fib_high = fib_levels.get("0") if direction == "LONG" else fib_levels.get("1")
    fib_low = fib_levels.get("1") if direction == "LONG" else fib_levels.get("0")
    fib_extensions = (
        fibonacci_extensions(fib_high, fib_low, direction)
        if fib_high is not None and fib_low is not None else {}
    )

    score = calculate_signal_score(
        setup["breakout"], setup["retest"], five_minute,
        indicator_data, higher_trend, fib_matches, direction
    )
    if score < MIN_HISTORY_SCORE:
        return None
    tier = "SEND" if score >= MIN_SIGNAL_SCORE else ("REVIEW" if score >= MIN_REVIEW_SCORE else "HISTORY")

    breakout = dict(setup["breakout"])
    breakout["candle_time"] = int(breakout["candle"]["time"])
    line = dict(setup["line"])
    line["points"] = [dict(point) for point in line.get("points", [])]
    for point in line["points"]:
        idx = int(point["index"])
        if 0 <= idx < len(closed_main):
            point["time"] = int(closed_main[idx]["time"])
    line["chart_points"] = [
        {"time": int(closed_main[idx]["time"]), "price": line_price(line, idx)}
        for idx in range(int(line.get("start_index", 0)), len(closed_main))
    ]
    retest = dict(setup["retest"]) if setup["retest"] else None
    if retest:
        retest["candle_time"] = int(retest["candle"]["time"])

    return {
        "symbol": symbol, "timeframe": timeframe, "direction": direction,
        "score": score, "signal_tier": tier,
        "entry": levels["entry"], "stop_loss": levels["stop_loss"],
        "tp1": levels["tp1"], "tp2": levels["tp2"], "tp3": levels["tp3"],
        "risk_percent": levels["risk_percent"],
        "rr_tp1": levels["rr_tp1"], "rr_tp2": levels["rr_tp2"], "rr_tp3": levels["rr_tp3"],
        "leverage": levels["leverage"],
        "margin_stop_loss_percent": levels["margin_stop_loss_percent"],
        "atr": atr_value, "rsi": indicator_data["rsi"],
        "volume_ratio": indicator_data["volume_ratio"],
        "trendline": line, "breakout": breakout, "retest": retest,
        "confirmation": five_minute, "higher_trend": higher_trend,
        "fibonacci": fib_levels, "fibonacci_extensions": fib_extensions,
        "fib_matches": fib_matches, "status": "READY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "setup_key": f"{symbol}|{timeframe}|{direction}|{breakout['candle_time']}",
        # last_checked stores the confirmation candle OPEN time; monitor uses >.
        "monitor_start_time": int(five_minute["time"]),
    }



def init_vip_tracking():
    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vip_signal_tracking (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    tp1 REAL NOT NULL,
                    tp2 REAL NOT NULL,
                    tp3 REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    remaining_fraction REAL DEFAULT 1.0,
                    realized_roi REAL DEFAULT 0.0,
                    tp1_done INTEGER DEFAULT 0,
                    tp2_done INTEGER DEFAULT 0,
                    tp3_done INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_checked_candle_time INTEGER DEFAULT 0,
                    setup_key TEXT DEFAULT ''
                )
            """)
            existing = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(vip_signal_tracking)"
                ).fetchall()
            }
            migrations = {
                "last_checked_candle_time": "INTEGER DEFAULT 0",
                "setup_key": "TEXT DEFAULT ''"
            }
            for column, definition in migrations.items():
                if column not in existing:
                    conn.execute(
                        f"ALTER TABLE vip_signal_tracking ADD COLUMN {column} {definition}"
                    )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS scanner_daily_stats (
                    utc_date TEXT PRIMARY KEY,
                    sent_count INTEGER NOT NULL DEFAULT 0
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    sent_at TEXT,
                    last_error TEXT,
                    payload_type TEXT NOT NULL DEFAULT 'TEXT',
                    image_base64 TEXT,
                    UNIQUE(signal_id, event_type)
                )
            """)
            columns = {
                row[1] for row in
                conn.execute("PRAGMA table_info(notification_outbox)")
            }
            if "payload_type" not in columns:
                conn.execute(
                    "ALTER TABLE notification_outbox "
                    "ADD COLUMN payload_type TEXT NOT NULL DEFAULT 'TEXT'"
                )
            if "image_base64" not in columns:
                conn.execute(
                    "ALTER TABLE notification_outbox ADD COLUMN image_base64 TEXT"
                )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS scanner_schedule_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL
                )
            """)
            conn.commit()


def save_vip_signal(signal):
    now = datetime.now(timezone.utc).isoformat()
    last_checked = int(
        signal.get("monitor_start_time")
        or signal.get("breakout", {}).get("candle_time")
        or signal.get("breakout", {}).get("candle", {}).get("time")
        or 0
    )
    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            cursor = conn.execute("""
                INSERT INTO vip_signal_tracking (
                    symbol, timeframe, direction,
                    entry, stop_loss, tp1, tp2, tp3,
                    leverage, score, status,
                    remaining_fraction, realized_roi,
                    created_at, updated_at,
                    last_checked_candle_time, setup_key
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal["symbol"], signal["timeframe"], signal["direction"],
                signal["entry"], signal["stop_loss"], signal["tp1"],
                signal["tp2"], signal["tp3"], signal["leverage"],
                signal["score"], "ACTIVE", 1.0, 0.0, now, now,
                last_checked, signal.get("setup_key", "")
            ))
            conn.commit()
            return cursor.lastrowid


def save_pending_vip_signal_and_enqueue(signal):
    now = datetime.now(timezone.utc).isoformat()
    last_checked = int(
        signal.get("monitor_start_time")
        or signal.get("breakout", {}).get("candle_time")
        or signal.get("breakout", {}).get("candle", {}).get("time")
        or 0
    )
    caption = format_signal_message(signal)
    image_base64 = None
    payload_type = "TEXT"

    try:
        chart = create_signal_chart(signal)
        if chart:
            image_base64 = base64.b64encode(chart.getvalue()).decode("ascii")
            payload_type = "PHOTO"
    except Exception:
        logging.exception("Signal chart creation failed; queueing text")

    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            cursor = conn.execute("""
                INSERT INTO vip_signal_tracking (
                    symbol, timeframe, direction,
                    entry, stop_loss, tp1, tp2, tp3,
                    leverage, score, status,
                    remaining_fraction, realized_roi,
                    created_at, updated_at,
                    last_checked_candle_time, setup_key
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal["symbol"], signal["timeframe"], signal["direction"],
                signal["entry"], signal["stop_loss"], signal["tp1"],
                signal["tp2"], signal["tp3"], signal["leverage"],
                signal["score"], "PENDING", 1.0, 0.0, now, now,
                last_checked, signal.get("setup_key", "")
            ))
            signal_id = cursor.lastrowid
            conn.execute("""
                INSERT INTO notification_outbox (
                    signal_id, event_type, message, created_at,
                    next_attempt_at, payload_type, image_base64
                )
                VALUES (?, 'INITIAL', ?, ?, ?, ?, ?)
            """, (
                signal_id, caption, now, now, payload_type, image_base64
            ))
            conn.commit()
            return signal_id


def calculate_position_roi(direction, entry, exit_price, leverage):
    if entry <= 0:
        return 0.0

    if direction == "LONG":
        price_change = (exit_price - entry) / entry
    else:
        price_change = (entry - exit_price) / entry

    return price_change * leverage * 100



def update_vip_signal(signal_id, **fields):
    allowed = {
        "status", "remaining_fraction", "realized_roi",
        "tp1_done", "tp2_done", "tp3_done",
        "last_checked_candle_time"
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [signal_id]
    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            conn.execute(
                f"UPDATE vip_signal_tracking SET {assignments} WHERE id = ?",
                values
            )
            conn.commit()


def get_active_vip_signals():
    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM vip_signal_tracking
                WHERE status = 'ACTIVE'
                ORDER BY created_at ASC
            """).fetchall()
            return [dict(row) for row in rows]


def process_vip_signal(signal, candle):
    """Process one closed candle. SL has priority if SL and TP overlap."""
    if signal.get("status") != "ACTIVE":
        return []

    high, low = float(candle["high"]), float(candle["low"])
    direction = signal["direction"]
    entry = float(signal["entry"])
    stop = float(signal["stop_loss"])
    leverage = int(signal["leverage"])
    remaining = float(signal["remaining_fraction"])
    realized = float(signal["realized_roi"])
    candle_time = int(candle.get("time", 0))

    if direction == "LONG":
        stop_hit = low <= stop
        hits = [
            high >= float(signal["tp1"]),
            high >= float(signal["tp2"]),
            high >= float(signal["tp3"])
        ]
    else:
        stop_hit = high >= stop
        hits = [
            low <= float(signal["tp1"]),
            low <= float(signal["tp2"]),
            low <= float(signal["tp3"])
        ]

    if stop_hit:
        loss_roi = calculate_position_roi(direction, entry, stop, leverage)
        realized += remaining * loss_roi
        event = {
            "type": "SL",
            "price": stop,
            "realized_roi": realized,
            "leverage": leverage
        }
        signal.update({
            "status": "SL",
            "remaining_fraction": 0.0,
            "realized_roi": realized
        })

        enqueue_notification(
            signal["id"], event["type"],
            format_vip_result_message(signal, event)
        )

        update_vip_signal(
            signal["id"],
            status="SL",
            remaining_fraction=0.0,
            realized_roi=realized,
            last_checked_candle_time=candle_time
        )
        return [event]

    targets = [
        ("TP1", "tp1", "tp1_done", TP1_CLOSE_FRACTION),
        ("TP2", "tp2", "tp2_done", TP2_CLOSE_FRACTION),
        ("TP3", "tp3", "tp3_done", None)
    ]

    events = []
    updates = {"last_checked_candle_time": candle_time}

    for index, (event_type, price_key, done_key, close_fraction) in enumerate(targets):
        if not hits[index] or signal.get(done_key):
            continue
        if remaining <= 0:
            break

        fraction = remaining if close_fraction is None else min(close_fraction, remaining)
        target_price = float(signal[price_key])
        profit_roi = calculate_position_roi(
            direction, entry, target_price, leverage
        )
        realized += fraction * profit_roi
        remaining = max(0.0, remaining - fraction)

        signal[done_key] = 1
        signal["remaining_fraction"] = remaining
        signal["realized_roi"] = realized
        updates[done_key] = 1

        event = {
            "type": event_type,
            "price": target_price,
            "closed_fraction": fraction,
            "realized_roi": realized,
            "leverage": leverage
        }
        events.append(event)

        if event_type == "TP3":
            signal["status"] = "TP3"
            updates["status"] = "TP3"

    if events:
        updates["remaining_fraction"] = remaining
        updates["realized_roi"] = realized

        # Queue every event before committing candle progress.
        for event in events:
            enqueue_notification(
                signal["id"], event["type"],
                format_vip_result_message(signal, event)
            )

    update_vip_signal(signal["id"], **updates)
    return events


def monitor_vip_signals():
    """Monitor only 5m candles strictly after the confirmation candle."""
    active_signals = get_active_vip_signals()
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    for signal in active_signals:
        try:
            created = datetime.fromisoformat(signal["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            expiry_ms = int((created + timedelta(hours=SIGNAL_MAX_AGE_HOURS)).timestamp() * 1000)

            candles = get_candles(signal["symbol"], CONFIRM_TIMEFRAME, limit=1000)
            if candles:
                stored_last_checked = int(signal.get("last_checked_candle_time") or signal.get("monitor_start_time") or 0)
                # For records created by the older monitor, the stored value may be
                # a 15m/1h candle open. Raise it to the 5m candle immediately before
                # creation so no pre-entry 5m range is used after this upgrade.
                created_ms = int(created.timestamp() * 1000)
                created_floor = (created_ms // 300_000) * 300_000 - 300_000
                last_checked = max(stored_last_checked, created_floor)
                eligible = [
                    candle for candle in candles
                    if int(candle["time"]) > last_checked
                    and int(candle["time"]) + 300_000 <= expiry_ms
                ]
                for candle in eligible:
                    process_vip_signal(signal, candle)
                    if signal.get("status") != "ACTIVE":
                        break

            # Expire only after all available, closed 5m candles up to expiry were processed.
            if signal.get("status") == "ACTIVE" and now_ms >= expiry_ms:
                # Close the remaining simulated fraction at the last closed 5m price
                # available at or before the 24h expiry boundary.
                expiry_candles = [
                    candle for candle in (candles or [])
                    if int(candle["time"]) + 300_000 <= expiry_ms
                ]
                mark_candle = expiry_candles[-1] if expiry_candles else None
                if mark_candle is None:
                    logging.warning("Cannot finalize expired signal without a closed 5m candle: %s", signal["id"])
                    continue

                exit_price = float(mark_candle["close"])
                remaining = float(signal["remaining_fraction"])
                realized = float(signal["realized_roi"])
                realized += remaining * calculate_position_roi(
                    signal["direction"], float(signal["entry"]),
                    exit_price, int(signal["leverage"])
                )
                event = {
                    "type": "EXPIRED", "price": exit_price,
                    "realized_roi": realized,
                    "leverage": int(signal["leverage"]),
                }
                enqueue_notification(
                    signal["id"], "EXPIRED",
                    format_vip_result_message(signal, event)
                )
                update_vip_signal(
                    signal["id"], status="EXPIRED", remaining_fraction=0.0,
                    realized_roi=realized,
                    last_checked_candle_time=int(mark_candle["time"]),
                )
                signal.update({"status": "EXPIRED", "remaining_fraction": 0.0, "realized_roi": realized})

        except Exception:
            logging.exception("Signal monitoring failed for %s", signal.get("symbol", "UNKNOWN"))



def format_signal_message(signal):
    direction_fa = "خرید LONG 🟢" if signal["direction"] == "LONG" else "فروش SHORT 🔴"
    return (
        f"🚨 VIP TOOBIT SIGNAL\n\n"
        f"نماد: {signal['symbol']}\n"
        f"تایم‌فریم: {signal['timeframe']}\n"
        f"جهت: {direction_fa}\n"
        f"امتیاز: {signal['score']}/100\n\n"
        f"🎯 ورود: {signal['entry']}\n"
        f"🛑 حد ضرر: {signal['stop_loss']}\n"
        f"فاصله حد ضرر: {signal['risk_percent']:.2f}%\n\n"
        f"TP1: {signal['tp1']} (بستن ۵۰٪ حجم اولیه)\n"
        f"TP2: {signal['tp2']} (بستن ۲۵٪ حجم اولیه)\n"
        f"TP3: {signal['tp3']} (بستن ۲۵٪ باقیمانده)\n\n"
        f"⚖️ اهرم پیشنهادی: {signal['leverage']}x\n"
        f"ریسک تخمینی مارجین تا حد ضرر: {signal['margin_stop_loss_percent']:.2f}%\n"
        f"📊 نسبت سود به ضرر TP1: 1:{signal['rr_tp1']:.2f}\n"
        f"RSI: {signal['rsi']:.2f}\n"
        f"قدرت حجم: {signal['volume_ratio']:.2f}\n\n"
        f"تأیید ۵ دقیقه: {signal['confirmation']['pattern']}\n"
        f"روند ۴ ساعته: {signal['higher_trend']}\n\n"
        f"⚠️ اهرم فقط تخمینی است؛ تضمین جلوگیری از لیکوییدشدن نیست. "
        f"سود/زیان رصدشده ناخالص است و کارمزد، فاندینگ و لغزش قیمت را شامل نمی‌شود.\n"
        f"این ربات معامله‌ای اجرا نمی‌کند."
    )

def create_signal_chart(signal):
    candles = get_candles(
        signal["symbol"], signal["timeframe"], limit=CANDLE_LIMIT
    )
    if not candles:
        return None

    fig, ax = plt.subplots(figsize=(16, 9))
    time_to_index = {int(c["time"]): i for i, c in enumerate(candles)}

    for i, candle in enumerate(candles):
        op, cl = float(candle["open"]), float(candle["close"])
        hi, lo = float(candle["high"]), float(candle["low"])
        color = "green" if cl >= op else "red"
        ax.vlines(i, lo, hi, color=color, linewidth=1)
        body_bottom = min(op, cl)
        body_height = abs(cl - op)
        if body_height == 0:
            body_height = max(abs(op) * 0.00001, 1e-10)
        ax.bar(i, body_height, bottom=body_bottom, width=0.65,
               color=color, edgecolor=color)

    trendline = signal.get("trendline") or {}
    chart_points = [
        point for point in trendline.get("chart_points", [])
        if int(point["time"]) in time_to_index
    ]
    if chart_points:
        xs = [time_to_index[int(p["time"])] for p in chart_points]
        ys = [float(p["price"]) for p in chart_points]
        ax.plot(xs, ys, color="blue", linewidth=2.0, label="Trendline")

    # Mark every confirmed trendline touch.
    touch_points = [
        point for point in trendline.get("points", [])
        if point.get("time") is not None and int(point["time"]) in time_to_index
    ]
    if touch_points:
        tx = [time_to_index[int(p["time"])] for p in touch_points]
        ty = [float(p["price"]) for p in touch_points]
        ax.scatter(tx, ty, marker="o", s=55, color="deepskyblue",
                   edgecolors="black", linewidths=0.5, label="Trendline touches", zorder=5)

    breakout = signal.get("breakout") or {}
    breakout_time = breakout.get("candle_time")
    if breakout_time is not None and int(breakout_time) in time_to_index:
        bx = time_to_index[int(breakout_time)]
        by = float(breakout.get("price", candles[bx]["close"]))
        ax.scatter([bx], [by], marker="^" if signal["direction"] == "LONG" else "v",
                   s=150, color="lime" if signal["direction"] == "LONG" else "crimson",
                   edgecolors="black", label="Breakout", zorder=7)

    retest = signal.get("retest")
    if retest and retest.get("candle_time") is not None:
        rt = int(retest["candle_time"])
        if rt in time_to_index:
            rx = time_to_index[rt]
            ry = float(retest["candle"]["close"])
            ax.scatter([rx], [ry], marker="D", s=100, color="gold",
                       edgecolors="black", label="Retest", zorder=7)

    entry = float(signal["entry"])
    atr_value = float(signal.get("atr") or 0)
    if atr_value > 0:
        ax.axhspan(entry - atr_value * 0.10, entry + atr_value * 0.10,
                   color="blue", alpha=0.10, label="Entry zone")

    levels = [
        ("ENTRY", entry, "blue"),
        ("STOP LOSS", signal["stop_loss"], "red"),
        ("TP1", signal["tp1"], "green"),
        ("TP2", signal["tp2"], "orange"),
        ("TP3", signal["tp3"], "purple")
    ]
    for label, price, color in levels:
        ax.axhline(float(price), color=color, linestyle="--",
                   linewidth=1.4, label=f"{label}: {float(price):.8g}")

    fib_colors = {
        "0.382": "cyan", "0.5": "magenta",
        "0.618": "yellow", "0.786": "orange"
    }
    for name, color in fib_colors.items():
        price = (signal.get("fibonacci") or {}).get(name)
        if price is not None:
            ax.axhline(float(price), color=color, linestyle=":",
                       linewidth=1.1, alpha=0.85,
                       label=f"Fib {name}: {float(price):.8g}")

    extension_colors = {"1.272": "brown", "1.618": "darkcyan", "2.0": "black"}
    for name, color in extension_colors.items():
        price = (signal.get("fibonacci_extensions") or {}).get(name)
        if price is not None:
            ax.axhline(float(price), color=color, linestyle="-.",
                       linewidth=1.0, alpha=0.65,
                       label=f"Fib Ext {name}: {float(price):.8g}")

    ax.set_title(
        f"{signal['symbol']} | {signal['direction']} | "
        f"{signal['timeframe']} | Score {signal['score']}/100"
    )
    ax.set_xlabel("Closed candles")
    ax.set_ylabel("Price")
    ax.grid(True, alpha=0.22)
    ax.legend(loc="best", fontsize=7, ncol=2)
    fig.tight_layout()

    image_buffer = io.BytesIO()
    fig.savefig(image_buffer, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    image_buffer.seek(0)
    return image_buffer


def send_bridge_text(message):
    if not BRIDGE_SECRET:
        logging.error("TOOBIT_BRIDGE_SECRET is not configured.")
        return False

    try:
        response = requests.post(
            BRIDGE_URL,
            headers={"Authorization": f"Bearer {BRIDGE_SECRET}"},
            json={"message": message},
            timeout=20
        )

        if response.status_code != 200:
            logging.error(
                "Telegram text error: %s %s",
                response.status_code, response.text[:300]
            )
            return False

        try:
            payload = response.json()
        except ValueError:
            logging.error("Telegram text response is not valid JSON.")
            return False

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            logging.error("Telegram text delivery was not confirmed.")
            return False

        logging.info("Telegram text delivery confirmed.")
        return True

    except Exception:
        logging.exception("Telegram text request failed.")
        return False


def send_bridge_photo(image_buffer, caption):
    if not BRIDGE_SECRET:
        logging.error("TOOBIT_BRIDGE_SECRET is not configured.")
        return False

    try:
        encoded_image = base64.b64encode(
            image_buffer.getvalue()
        ).decode("utf-8")

        response = requests.post(
            PHOTO_URL,
            headers={"Authorization": f"Bearer {BRIDGE_SECRET}"},
            json={
                "image_base64": encoded_image,
                "caption": caption
            },
            timeout=30
        )

        if response.status_code != 200:
            logging.error(
                "Telegram photo error: %s %s",
                response.status_code, response.text[:300]
            )
            return False

        try:
            payload = response.json()
        except ValueError:
            logging.error("Telegram photo response is not valid JSON.")
            return False

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            logging.error("Telegram photo delivery was not confirmed.")
            return False

        logging.info("Telegram photo delivery confirmed.")
        return True

    except Exception:
        logging.exception("Telegram photo request failed.")
        return False


def send_signal_to_telegram(signal):
    caption = format_signal_message(signal)
    chart = create_signal_chart(signal)

    if chart is None:
        return send_bridge_text(caption)

    if send_bridge_photo(chart, caption):
        return True

    logging.warning("Chart delivery failed; trying text-only signal.")
    return send_bridge_text(caption)


def format_vip_result_message(signal, event):
    event_type = event["type"]
    titles = {
        "SL": "🛑 حد ضرر فعال شد",
        "TP1": "🎯 تارگت اول ثبت شد",
        "TP2": "🎯 تارگت دوم ثبت شد",
        "TP3": "🏆 هر سه تارگت تکمیل شد",
        "EXPIRED": "⌛ اعتبار سیگنال به پایان رسید"
    }
    title = titles.get(event_type, "📊 به‌روزرسانی سیگنال")
    return (
        f"{title}\n\n"
        f"نماد: {signal['symbol']}\n"
        f"جهت: {signal['direction']}\n"
        f"قیمت ثبت‌شده: {event['price']}\n"
        f"اهرم پیشنهادی: {event['leverage']}x\n"
        f"سود/زیان تجمعی تخمینی روی مارجین اولیه: "
        f"{event['realized_roi']:.2f}%\n\n"
        f"مقدار ناخالص است و کارمزد، فاندینگ و لغزش قیمت را شامل نمی‌شود. "
        f"معامله واقعی اجرا نشده است."
    )


def send_vip_result(signal, event):
    return send_bridge_text(format_vip_result_message(signal, event))


def enqueue_notification(signal_id, event_type, message):
    now = datetime.now(timezone.utc).isoformat()
    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO notification_outbox (
                    signal_id, event_type, message,
                    created_at, attempts, next_attempt_at
                )
                VALUES (?, ?, ?, ?, 0, ?)
            """, (signal_id, event_type, message, now, now))
            conn.commit()


def dispatch_notification_outbox(limit=20):
    global daily_signal_count
    if not is_delivery_window():
        return
    now = datetime.now(timezone.utc).isoformat()

    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            rows = conn.execute("""
                SELECT id, signal_id, event_type, message,
                       attempts, payload_type, image_base64
                FROM notification_outbox
                WHERE sent_at IS NULL AND next_attempt_at <= ?
                ORDER BY id ASC LIMIT ?
            """, (now, limit)).fetchall()

    for nid, sid, event, message, attempts, kind, image_data in rows:
        delivered = False

        if event == "INITIAL":
            with DB_LOCK:
                with sqlite3.connect(DATABASE_FILE) as conn:
                    pending = conn.execute(
                        "SELECT created_at, status FROM vip_signal_tracking WHERE id=?",
                        (sid,),
                    ).fetchone()
            stale = True
            if pending and pending[1] == "PENDING":
                try:
                    queued_at = datetime.fromisoformat(pending[0])
                    if queued_at.tzinfo is None:
                        queued_at = queued_at.replace(tzinfo=timezone.utc)
                    stale = (datetime.now(timezone.utc) - queued_at).total_seconds() > 600
                except (TypeError, ValueError):
                    stale = True
            if stale:
                with DB_LOCK:
                    with sqlite3.connect(DATABASE_FILE) as conn:
                        conn.execute(
                            "UPDATE vip_signal_tracking SET status='EXPIRED', updated_at=? WHERE id=? AND status='PENDING'",
                            (datetime.now(timezone.utc).isoformat(), sid),
                        )
                        conn.execute(
                            "DELETE FROM notification_outbox WHERE id=? AND sent_at IS NULL",
                            (nid,),
                        )
                        conn.commit()
                logging.info("Stale pending signal cancelled before delivery: %s", sid)
                continue

        if kind == "PHOTO" and image_data:
            try:
                buffer = io.BytesIO(base64.b64decode(image_data))
                delivered = send_bridge_photo(buffer, message)
            except Exception:
                logging.exception("Outbox photo failed; trying text")

        if not delivered:
            delivered = send_bridge_text(message)

        if delivered:
            activated = False
            sent_count = None
            with DB_LOCK:
                with sqlite3.connect(DATABASE_FILE) as conn:
                    cur = conn.execute("""
                        UPDATE notification_outbox
                        SET sent_at=?, last_error=NULL
                        WHERE id=? AND sent_at IS NULL
                    """, (datetime.now(timezone.utc).isoformat(), nid))

                    if cur.rowcount == 1 and event == "INITIAL":
                        active = conn.execute("""
                            UPDATE vip_signal_tracking
                            SET status='ACTIVE', updated_at=?
                            WHERE id=? AND status='PENDING'
                        """, (datetime.now(timezone.utc).isoformat(), sid))
                        activated = active.rowcount == 1

                        if activated:
                            today = datetime.now(timezone.utc).date().isoformat()
                            conn.execute("""
                                INSERT INTO scanner_daily_stats (utc_date, sent_count)
                                VALUES (?, 1)
                                ON CONFLICT(utc_date) DO UPDATE
                                SET sent_count=sent_count+1
                            """, (today,))
                            row = conn.execute(
                                "SELECT sent_count FROM scanner_daily_stats WHERE utc_date=?",
                                (today,)
                            ).fetchone()
                            sent_count = int(row[0]) if row else 0
                    conn.commit()

            if sent_count is not None:
                daily_signal_count = sent_count
            if event == "INITIAL" and not activated:
                logging.warning("Initial notification sent but signal was not activated: %s", sid)
            logging.info("Notification delivered. Outbox ID: %s", nid)

        else:
            new_attempts = attempts + 1
            delay = min(3600, 30 * (2 ** min(attempts, 7)))
            next_time = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + delay,
                timezone.utc
            ).isoformat()

            with DB_LOCK:
                with sqlite3.connect(DATABASE_FILE) as conn:
                    conn.execute("""
                        UPDATE notification_outbox
                        SET attempts=?, next_attempt_at=?, last_error=?
                        WHERE id=? AND sent_at IS NULL
                    """, (
                        new_attempts, next_time,
                        "Telegram delivery failed; see application log", nid
                    ))
                    conn.commit()
            logging.warning("Notification failed; retry scheduled. Outbox ID: %s", nid)


def has_recent_setup(signal):
    setup_key = signal.get("setup_key", "")
    if not setup_key:
        return False
    try:
        with DB_LOCK:
            with sqlite3.connect(DATABASE_FILE) as conn:
                vip = conn.execute(
                    "SELECT 1 FROM vip_signal_tracking WHERE setup_key=? LIMIT 1",
                    (setup_key,),
                ).fetchone()
                audit = conn.execute(
                    "SELECT 1 FROM signals WHERE setup_key=? LIMIT 1",
                    (setup_key,),
                ).fetchone()
        return bool(vip or audit)
    except Exception:
        logging.exception("Recent setup lookup failed; failing closed.")
        return True



def save_signal_audit(signal):
    tier = signal.get("signal_tier")
    if tier not in ("REVIEW", "HISTORY"):
        return
    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            conn.execute("""
                INSERT INTO signals (
                    symbol, timeframe, direction, entry, stop,
                    tp1, tp2, tp3, score, status, created_at,
                    leverage, initial_risk, setup_key
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal["symbol"], signal["timeframe"], signal["direction"],
                signal["entry"], signal["stop_loss"], signal["tp1"],
                signal["tp2"], signal["tp3"], signal["score"], tier,
                signal["created_at"], signal["leverage"],
                signal["risk_percent"], signal.get("setup_key", "")
            ))
            conn.commit()


def has_active_duplicate(signal):
    try:
        with DB_LOCK:
            with sqlite3.connect(DATABASE_FILE) as conn:
                row = conn.execute("""
                    SELECT 1 FROM vip_signal_tracking
                    WHERE symbol=? AND timeframe=? AND direction=?
                      AND status IN ('PENDING','ACTIVE')
                    LIMIT 1
                """, (signal["symbol"], signal["timeframe"], signal["direction"])).fetchone()
        return bool(row)
    except Exception:
        logging.exception("Duplicate check failed; failing closed.")
        return True


def scan_symbol(symbol):
    found = []

    for timeframe in TIMEFRAMES:
        try:
            candles = get_candles(
                symbol,
                timeframe,
                limit=CANDLE_LIMIT
            )

            if not candles or len(candles) < 100:
                continue

            for direction in ("LONG", "SHORT"):
                signal = build_signal(
                    symbol,
                    timeframe,
                    direction,
                    candles
                )

                if signal is None:
                    continue

                if has_active_duplicate(signal) or has_recent_setup(signal):
                    continue

                found.append(signal)

        except Exception:
            logging.exception(
                "Symbol scan failed: %s",
                symbol
            )

    return found



def add_signal_candidate(signal):
    key = (
        signal["symbol"], signal["timeframe"],
        signal["direction"], signal.get("setup_key", "")
    )
    with CANDIDATE_LOCK:
        for index, existing in enumerate(signal_candidates):
            existing_key = (
                existing["symbol"], existing["timeframe"],
                existing["direction"], existing.get("setup_key", "")
            )
            if existing_key == key:
                if signal["score"] > existing["score"]:
                    signal_candidates[index] = signal
                return
        signal_candidates.append(signal)

def scan_market():
    """Replace the candidate snapshot on every successful full-market scan."""
    global signal_candidates
    active_contracts = get_contracts()
    if not active_contracts:
        logging.warning("No active contracts available; keeping previous candidate snapshot.")
        return False

    symbols = []
    for contract in active_contracts:
        raw_symbol = contract if isinstance(contract, str) else contract.get("symbol", "") if isinstance(contract, dict) else ""
        symbol = normalize_symbol(raw_symbol) if raw_symbol else None
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    logging.info("Starting market scan: %s symbols", len(symbols))
    all_found = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(scan_symbol, symbol) for symbol in symbols]
        for future in as_completed(futures):
            try:
                all_found.extend(future.result() or [])
            except Exception:
                logging.exception("Scanner worker failed.")

    # Only the latest scan is eligible for selection; old candidates cannot linger.
    with CANDIDATE_LOCK:
        signal_candidates = all_found
    logging.info("Market scan complete. Current valid candidates: %s", len(all_found))
    return True



def reset_daily_signal_counter():
    global daily_signal_count, daily_date
    today = datetime.now(timezone.utc).date()
    today_text = today.isoformat()

    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO scanner_daily_stats (utc_date, sent_count)
                VALUES (?, 0)
            """, (today_text,))
            row = conn.execute(
                "SELECT sent_count FROM scanner_daily_stats WHERE utc_date = ?",
                (today_text,)
            ).fetchone()
            conn.commit()

    if daily_date != today or row is not None:
        daily_date = today
        daily_signal_count = int(row[0]) if row else 0


def increment_daily_signal_count():
    global daily_signal_count
    today_text = datetime.now(timezone.utc).date().isoformat()
    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            conn.execute("""
                INSERT INTO scanner_daily_stats (utc_date, sent_count)
                VALUES (?, 1)
                ON CONFLICT(utc_date) DO UPDATE
                SET sent_count = sent_count + 1
            """, (today_text,))
            row = conn.execute(
                "SELECT sent_count FROM scanner_daily_stats WHERE utc_date = ?",
                (today_text,)
            ).fetchone()
            conn.commit()
    daily_signal_count = int(row[0]) if row else daily_signal_count + 1



def is_delivery_window(moment=None):
    local = moment.astimezone(TEHRAN_TZ) if moment else datetime.now(TEHRAN_TZ)
    return DELIVERY_START_HOUR <= local.hour < DELIVERY_END_HOUR


def current_selection_slot(moment=None):
    local = moment.astimezone(TEHRAN_TZ) if moment else datetime.now(TEHRAN_TZ)
    minute = 0 if local.minute < 30 else 30
    return local.replace(minute=minute, second=0, microsecond=0)


def claim_selection_slot():
    """Claim each Tehran half-hour slot once, including across restarts."""
    local = datetime.now(TEHRAN_TZ)
    if not is_delivery_window(local) or local.minute % 30 > 10:
        return False
    slot_key = current_selection_slot(local).isoformat()
    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            row = conn.execute(
                "SELECT state_value FROM scanner_schedule_state WHERE state_key='last_selection_slot'"
            ).fetchone()
            if row and row[0] == slot_key:
                return False
            conn.execute(
                "INSERT INTO scanner_schedule_state(state_key,state_value) VALUES('last_selection_slot',?) "
                "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value",
                (slot_key,),
            )
            conn.commit()
    return True


def rolling_two_hour_signal_count(now=None):
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=2)
    cutoff_text = cutoff.astimezone(timezone.utc).isoformat()
    with DB_LOCK:
        with sqlite3.connect(DATABASE_FILE) as conn:
            row = conn.execute("""
                SELECT COUNT(*)
                FROM notification_outbox o
                JOIN vip_signal_tracking v ON v.id=o.signal_id
                WHERE o.event_type='INITIAL'
                  AND o.created_at >= ?
                  AND (o.sent_at IS NOT NULL OR v.status='PENDING')
            """, (cutoff_text,)).fetchone()
    return int(row[0]) if row else 0


def rank_and_send_candidates():
    """Queue at most one fresh, best-scoring signal for this Tehran slot."""
    if not is_delivery_window():
        logging.info("Outside Tehran delivery hours; no signal selection/queueing.")
        return

    with CANDIDATE_LOCK:
        candidates = list(signal_candidates)
    if not candidates:
        logging.info("No valid candidates in this selection slot.")
        return

    candidates.sort(key=lambda item: int(item.get("score", 0)), reverse=True)
    send_candidates = []
    for signal in candidates:
        tier = signal.get("signal_tier", "SEND" if int(signal.get("score", 0)) >= MIN_SIGNAL_SCORE else "HISTORY")
        if tier in ("REVIEW", "HISTORY"):
            try:
                if not has_recent_setup(signal):
                    save_signal_audit(signal)
            except Exception:
                logging.exception("Could not save non-send signal audit.")
            continue
        if tier == "SEND" and int(signal.get("score", 0)) >= MIN_SIGNAL_SCORE:
            send_candidates.append(signal)

    for signal in send_candidates:
        try:
            if has_active_duplicate(signal) or has_recent_setup(signal):
                continue

            confirmation = signal.get("confirmation") or {}
            confirmation_close = int(confirmation.get("close_time") or 0)
            now_ms = int(time.time() * 1000)
            if not confirmation_close or now_ms - confirmation_close > 600_000 or confirmation_close > now_ms:
                continue

            latest = get_candles(signal["symbol"], CONFIRM_TIMEFRAME, limit=3)
            if not latest:
                continue
            current_price = float(latest[-1]["close"])
            entry = float(signal["entry"])
            atr_value = float(signal.get("atr") or 0)
            drift_limit = max(atr_value * 0.5, entry * 0.001)
            if abs(current_price - entry) > drift_limit:
                logging.info("Candidate rejected due to entry drift: %s", signal["symbol"])
                continue
            if signal["direction"] == "LONG" and current_price <= float(signal["stop_loss"]):
                continue
            if signal["direction"] == "SHORT" and current_price >= float(signal["stop_loss"]):
                continue

            if rolling_two_hour_signal_count() >= MAX_SIGNALS_PER_2H:
                logging.info("Rolling 2-hour signal cap reached (%s).", MAX_SIGNALS_PER_2H)
                return
            reset_daily_signal_counter()
            if daily_signal_count >= MAX_DAILY_SIGNALS:
                logging.info("Daily signal limit reached.")
                return

            signal_id = save_pending_vip_signal_and_enqueue(signal)
            logging.info(
                "Best signal queued: %s %s %s score=%s id=%s",
                signal["symbol"], signal["timeframe"], signal["direction"], signal["score"], signal_id
            )
            return
        except Exception:
            logging.exception("Candidate selection failed; trying next candidate.")

    logging.info("No candidate passed final freshness and duplicate checks.")




def automatic_scan_cycle():
    scan_market()
    if claim_selection_slot():
        rank_and_send_candidates()



# =========================================================
# END OF AUTOMATIC SCANNER
# =========================================================


# =========================================================
# MAIN PROGRAM
# =========================================================

def main():
    print("\n==============================================")
    print("          TOOBIT VIP FUTURES SCANNER")
    print("==============================================")
    print("Timeframes:", TIMEFRAMES)
    print("Confirmation:", CONFIRM_TIMEFRAME)
    print("Higher timeframe:", HIGHER_TIMEFRAME)
    print("Scan interval:", SCAN_INTERVAL_SECONDS, "seconds")
    print("Selection interval:", SIGNAL_SELECTION_INTERVAL_SECONDS // 60, "minutes")
    print("Maximum signals per rolling 2h:", MAX_SIGNALS_PER_2H)
    print("Delivery hours (Tehran): 07:00-22:00")
    print("Daily signal limit:", MAX_DAILY_SIGNALS)
    print("Send score threshold:", MIN_SIGNAL_SCORE)
    print("Automatic trading: DISABLED")
    print("==============================================")

    parsed_bridge = urlsplit(BRIDGE_URL) if isinstance(BRIDGE_URL, str) else None
    if (
        parsed_bridge is None or parsed_bridge.scheme != "https"
        or parsed_bridge.hostname != "toobit-telegram-bridge.dctoranj.workers.dev"
        or parsed_bridge.path != "/send" or parsed_bridge.query
        or parsed_bridge.username or parsed_bridge.password or not BRIDGE_SECRET
    ):
        logging.critical("Telegram Bridge configuration invalid; scanner startup aborted.")
        print("Scanner stopped: Telegram Bridge configuration is invalid.")
        return

    try:
        init_database()
        init_vip_tracking()
        logging.info("Database initialized.")
        loaded = get_contracts()
        logging.info("Toobit contracts loaded: %s", len(loaded or []))
        logging.info("VIP scanner started.")

        while True:
            cycle_started = time.time()
            try:
                reset_daily_signal_counter()
                scan_ok = scan_market()
                if scan_ok and claim_selection_slot():
                    rank_and_send_candidates()
                monitor_vip_signals()
                dispatch_notification_outbox()
            except KeyboardInterrupt:
                raise
            except Exception:
                logging.exception("Main scanner cycle failed.")
            time.sleep(max(1, SCAN_INTERVAL_SECONDS - (time.time() - cycle_started)))

    except KeyboardInterrupt:
        print("\nScanner stopped by user.")
        logging.info("Scanner stopped by user.")
    except Exception:
        logging.exception("Fatal scanner error.")
        print("Scanner stopped because of a fatal error.")



# =========================================================
# PROGRAM ENTRY POINT
# =========================================================

if __name__ == "__main__":
    main()


# =========================================================
# END OF TOOBIT VIP SCANNER
# =========================================================
