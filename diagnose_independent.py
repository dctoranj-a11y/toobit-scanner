import toobit_independent as bot
import toobit_scanner as scanner
from collections import Counter

counts = Counter()
errors = Counter()

symbols = scanner.get_contracts(force_refresh=True)
print("Diagnostic sample:", len(symbols))

for symbol in symbols:
    try:
        data = {}
        valid = True

        for tf in ("4h", "1h", "15m"):
            candles = scanner.get_candles(symbol, tf, 160)
            candles = scanner.contiguous_candle_suffix(candles, tf)
            if len(candles) < 65:
                counts["insufficient_candles"] += 1
                valid = False
                break
            data[tf] = (candles, bot.metrics(candles))

        if not valid:
            continue

        c4, m4 = data["4h"]
        c1, m1 = data["1h"]
        c15, m15 = data["15m"]

        required = ("ema20", "ema50", "ema20_prev", "atr", "adx", "rsi", "hist", "hist_prev")
        if any(m.get(k) is None for m in (m4, m1, m15) for k in required):
            counts["missing_indicators"] += 1
            continue
        counts["data_and_indicators_ok"] += 1

        long_trend = (
            m4["ema20"] > m4["ema50"] and m4["close"] > m4["ema20"]
            and m4["ema20"] > m4["ema20_prev"]
            and m1["ema20"] > m1["ema50"] and m1["close"] > m1["ema20"]
            and m1["ema20"] > m1["ema20_prev"]
        )
        short_trend = (
            m4["ema20"] < m4["ema50"] and m4["close"] < m4["ema20"]
            and m4["ema20"] < m4["ema20_prev"]
            and m1["ema20"] < m1["ema50"] and m1["close"] < m1["ema20"]
            and m1["ema20"] < m1["ema20_prev"]
        )
        if not (long_trend or short_trend):
            counts["trend_alignment_failed"] += 1
            continue
        counts["trend_aligned"] += 1
        direction = "LONG" if long_trend else "SHORT"

        if m4["adx"] < 17 or m1["adx"] < 18:
            counts["adx_failed"] += 1
            continue
        counts["adx_passed"] += 1

        price, a = m15["close"], m15["atr"]
        if a <= 0 or a / price > 0.045:
            counts["atr_filter_failed"] += 1
            continue
        counts["atr_passed"] += 1

        previous, last = c15[-2], c15[-1]
        recent = c15[-11:-1]
        avg_volume = sum(x["volume"] for x in c15[-21:-1]) / 20
        vr = last["volume"] / avg_volume if avg_volume else 0

        if direction == "LONG":
            pullback = previous["low"] <= m15["ema20"] * 1.006
            momentum = (
                price > m15["ema20"] and last["close"] > last["open"]
                and 50 <= m15["rsi"] <= 70
                and m15["hist"] > m15["hist_prev"]
            )
            stop = min(x["low"] for x in recent) - 0.25 * a
        else:
            pullback = previous["high"] >= m15["ema20"] * 0.994
            momentum = (
                price < m15["ema20"] and last["close"] < last["open"]
                and 30 <= m15["rsi"] <= 50
                and m15["hist"] < m15["hist_prev"]
            )
            stop = max(x["high"] for x in recent) + 0.25 * a

        if not pullback:
            counts["pullback_failed"] += 1
            continue
        counts["pullback_passed"] += 1

        if not momentum:
            counts["momentum_failed"] += 1
            continue
        counts["momentum_passed"] += 1

        if vr < 0.65:
            counts["volume_failed"] += 1
            continue
        counts["volume_passed"] += 1

        risk_pct = abs(price - stop) / price * 100
        if risk_pct < 0.25 or risk_pct > 3.0:
            counts["stop_distance_failed"] += 1
            continue
        counts["stop_distance_passed"] += 1

        score = 50
        if direction == "LONG":
            if m15["hist"] > 0: score += 6
            if m15["rsi"] > 52: score += 5
            if last["close"] > previous["high"]: score += 7
        else:
            if m15["hist"] < 0: score += 6
            if m15["rsi"] < 48: score += 5
            if last["close"] < previous["low"]: score += 7

        score += min(12, max(0, (m4["adx"] - 17) * 0.6))
        score += min(10, max(0, (m1["adx"] - 18) * 0.5))
        if vr >= 1.2: score += 6

        if score < bot.MIN_SCORE:
            counts["score_failed"] += 1
        else:
            counts["final_candidates"] += 1
            print("CANDIDATE", symbol, direction, "score", round(score, 1))

    except Exception as e:
        errors[type(e).__name__ + ": " + str(e)[:100]] += 1

print("\n--- FILTER FUNNEL ---")
for key, value in counts.most_common():
    print(f"{key:28} {value}")
print("\n--- ERRORS ---")
for key, value in errors.most_common(10):
    print(key, value)
