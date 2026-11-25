import json
import math
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import toobit_scanner as scanner

OUTPUT = "independent_signals.jsonl"
MIN_SCORE = 72
MAX_SIGNALS = 10
WORKERS = 3

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i-1]["close"]
        trs.append(max(c["high"] - c["low"],
                       abs(c["high"] - p),
                       abs(c["low"] - p)))
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def adx(candles, period=14):
    if len(candles) < period * 2 + 2:
        return None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i-1]
        up = cur["high"] - prev["high"]
        down = prev["low"] - cur["low"]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        trs.append(max(cur["high"] - cur["low"],
                       abs(cur["high"] - prev["close"]),
                       abs(cur["low"] - prev["close"])))
    dx = []
    for i in range(period, len(trs)):
        tr_sum = sum(trs[i-period+1:i+1])
        if tr_sum <= 0:
            continue
        p = 100 * sum(plus_dm[i-period+1:i+1]) / tr_sum
        m = 100 * sum(minus_dm[i-period+1:i+1]) / tr_sum
        dx.append(100 * abs(p-m) / (p+m) if p+m else 0)
    return sum(dx[-period:]) / min(period, len(dx)) if dx else None


def macd_hist(values):
    if len(values) < 40:
        return None

    def make_ema(data, period):
        result = [None] * len(data)
        if len(data) < period:
            return result

        alpha = 2 / (period + 1)
        current = sum(data[:period]) / period
        result[period - 1] = current

        for i in range(period, len(data)):
            current = data[i] * alpha + current * (1 - alpha)
            result[i] = current

        return result

    fast = make_ema(values, 12)
    slow = make_ema(values, 26)

    macd_line = [
        f - sl
        for f, sl in zip(fast, slow)
        if f is not None and sl is not None
    ]

    signal_line = make_ema(macd_line, 9)

    if not signal_line or signal_line[-1] is None:
        return None

    return macd_line[-1] - signal_line[-1]

def ema_series(values, period):
    if len(values) < period:
        return []
    alpha = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for value in values[period:]:
        out.append(alpha * value + (1-alpha) * out[-1])
    return [None] * (period-1) + out


def metrics(candles):
    closes = [x["close"] for x in candles]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    e20_series = ema_series(closes, 20)
    a = atr(candles)
    d = adx(candles)
    r = rsi(closes)
    h_now = macd_hist(closes)
    h_prev = macd_hist(closes[:-1]) if len(closes) > 41 else None
    return {
        "close": closes[-1], "ema20": e20, "ema50": e50,
        "ema20_prev": e20_series[-4] if len(e20_series) >= 4 else None,
        "atr": a, "adx": d, "rsi": r,
        "hist": h_now, "hist_prev": h_prev
    }


def analyze(symbol):
    try:
        data = {}
        for tf in ("4h", "1h", "15m"):
            candles = scanner.get_candles(symbol, tf, 160)
            candles = scanner.contiguous_candle_suffix(candles, tf)
            if len(candles) < 65:
                return None
            data[tf] = (candles, metrics(candles))

        c4, m4 = data["4h"]
        c1, m1 = data["1h"]
        c15, m15 = data["15m"]

        required = ("ema20", "ema50", "ema20_prev", "atr", "adx", "rsi", "hist", "hist_prev")
        if any(m.get(k) is None for m in (m4, m1, m15) for k in required):
            return None

        direction = None
        if (m4["ema20"] > m4["ema50"] and m4["close"] > m4["ema20"]
            and m4["ema20"] > m4["ema20_prev"]
            and m1["ema20"] > m1["ema50"] and m1["close"] > m1["ema20"]
            and m1["ema20"] > m1["ema20_prev"]):
            direction = "LONG"
        elif (m4["ema20"] < m4["ema50"] and m4["close"] < m4["ema20"]
              and m4["ema20"] < m4["ema20_prev"]
              and m1["ema20"] < m1["ema50"] and m1["close"] < m1["ema20"]
              and m1["ema20"] < m1["ema20_prev"]):
            direction = "SHORT"
        else:
            return None

        if m4["adx"] < 17 or m1["adx"] < 18:
            return None

        price = c15[-1]["close"]
        a = m15["atr"]
        if a <= 0 or a / price > 0.045:
            return None

        previous = c15[-2]
        last = c15[-1]
        recent = c15[-11:-1]
        volume_avg = sum(x["volume"] for x in c15[-21:-1]) / 20
        volume_ratio = last["volume"] / volume_avg if volume_avg else 0

        score = 50
        reasons = []

        if direction == "LONG":
            pullback = previous["low"] <= m15["ema20"] * 1.006
            momentum = (price > m15["ema20"] and last["close"] > last["open"]
                       and m15["rsi"] >= 50 and m15["rsi"] <= 70
                       and m15["hist"] > m15["hist_prev"])
            if not pullback or not momentum:
                return None
            if m15["hist"] > 0:
                score += 6
                reasons.append("MACD momentum positive")
            if m15["rsi"] > 52:
                score += 5
                reasons.append("RSI confirms bullish momentum")
            if last["close"] > previous["high"]:
                score += 7
                reasons.append("15m candle confirms continuation")
            stop = min(x["low"] for x in recent) - 0.25 * a
            entry2 = m15["ema20"]
        else:
            pullback = previous["high"] >= m15["ema20"] * 0.994
            momentum = (price < m15["ema20"] and last["close"] < last["open"]
                       and m15["rsi"] >= 30 and m15["rsi"] <= 50
                       and m15["hist"] < m15["hist_prev"])
            if not pullback or not momentum:
                return None
            if m15["hist"] < 0:
                score += 6
                reasons.append("MACD momentum negative")
            if m15["rsi"] < 48:
                score += 5
                reasons.append("RSI confirms bearish momentum")
            if last["close"] < previous["low"]:
                score += 7
                reasons.append("15m candle confirms continuation")
            stop = max(x["high"] for x in recent) + 0.25 * a
            entry2 = m15["ema20"]

        score += min(12, max(0, (m4["adx"] - 17) * 0.6))
        score += min(10, max(0, (m1["adx"] - 18) * 0.5))
        reasons.append("4h and 1h trend aligned")
        reasons.append("Trend strength confirmed by ADX")

        if volume_ratio >= 1.2:
            score += 6
            reasons.append("Above-average volume")
        elif volume_ratio < 0.65:
            return None

        entry1 = price
        risk = abs(entry1 - stop)
        risk_pct = risk / entry1 * 100
        if risk <= 0 or risk_pct < 0.25 or risk_pct > 3.0:
            return None

        if direction == "LONG" and not stop < entry1:
            return None
        if direction == "SHORT" and not stop > entry1:
            return None

        # Entry 2 is a pullback idea, not a guaranteed fill.
        if direction == "LONG":
            if not stop < entry2 < entry1 or entry1-entry2 > 0.8*a:
                entry2 = None
        else:
            if not entry1 < entry2 < stop or entry2-entry1 > 0.8*a:
                entry2 = None

        if score < MIN_SCORE:
            return None

        # Targets are risk multiples; future obstacle levels are not assumed.
        unit = risk
        sign = 1 if direction == "LONG" else -1
        targets = [entry1 + sign * unit * x for x in (1.2, 2.0, 3.0)]

        # Illustrative leverage ceiling: limits stop-loss impact on margin to ~3%.
        leverage = max(1, min(5, int(3.0 / risk_pct)))

        return {
            "symbol": symbol,
            "direction": direction,
            "time": datetime.now(timezone.utc).isoformat(),
            "entry1": round(entry1, 10),
            "entry2": round(entry2, 10) if entry2 else None,
            "stop_loss": round(stop, 10),
            "tp1": round(targets[0], 10),
            "tp2": round(targets[1], 10),
            "tp3": round(targets[2], 10),
            "score": round(score, 1),
            "leverage_suggestion": leverage,
            "stop_distance_pct": round(risk_pct, 3),
            "volume_ratio": round(volume_ratio, 2),
            "adx_4h": round(m4["adx"], 1),
            "adx_1h": round(m1["adx"], 1),
            "rsi_15m": round(m15["rsi"], 1),
            "reasons": reasons,
            "method": "independent_trend_pullback_v1",
            "note": "Leverage is an experimental estimate, not a personal risk recommendation."
        }
    except Exception as exc:
        logging.debug("%s analysis error: %s", symbol, exc)
        return None


def main():
    symbols = scanner.get_contracts(force_refresh=True)
    logging.info("Active contracts: %s", len(symbols))
    results = []

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(analyze, symbol) for symbol in symbols]
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)

    results.sort(key=lambda x: x["score"], reverse=True)
    selected = results[:MAX_SIGNALS]

    with open(OUTPUT, "w", encoding="utf-8") as f:
        for item in selected:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print("\n========== INDEPENDENT SCAN ==========")
    print("Contracts scanned:", len(symbols))
    print("Valid candidates:", len(results))
    print("Selected signals:", len(selected))
    for x in selected:
        print(
            f'{x["symbol"]:24} {x["direction"]:5} '
            f'Score={x["score"]:5} Entry={x["entry1"]} '
            f'SL={x["stop_loss"]} Leverage~{x["leverage_suggestion"]}x'
        )
    print("Saved:", OUTPUT)
    print("AUTO TRADING: OFF | TELEGRAM: OFF")


if __name__ == "__main__":
    main()
