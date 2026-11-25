import json
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import toobit_scanner as s

OUTPUT = "experimental_signals.jsonl"
MIN_SCORE = 65


def trend(candles):
    if len(candles) < 55:
        return None
    closes = s.candle_values(candles, "close")
    fast, slow = s.ema(closes, 20), s.ema(closes, 50)
    if fast is None or slow is None:
        return None
    return "LONG" if fast > slow else "SHORT" if fast < slow else "NEUTRAL"


def analyze(symbol):
    try:
        c15 = s.contiguous_candle_suffix(s.get_candles(symbol, "15m", 160), "15m")
        c1h = s.contiguous_candle_suffix(s.get_candles(symbol, "1h", 100), "1h")
        c4h = s.contiguous_candle_suffix(s.get_candles(symbol, "4h", 100), "4h")

        if min(len(c15), len(c1h), len(c4h)) < 55:
            return None

        direction = trend(c15)
        t1, t4 = trend(c1h), trend(c4h)
        if direction not in ("LONG", "SHORT") or t1 != direction:
            return None

        closes = s.candle_values(c15, "close")
        price = closes[-1]
        atr = s.calculate_atr(c15)
        rsi = s.calculate_rsi(closes)
        volume = s.calculate_volume_ratio(c15)
        if atr is None or rsi is None or atr <= 0:
            return None

        sr = s.find_support_resistance(c15, 100)

        if direction == "LONG":
            level = sr.get("resistance")
            if not level or level <= price:
                return None
            distance = (level - price) / atr
            if not 0.10 <= distance <= 1.50 or not 42 <= rsi <= 70:
                return None
            entry1 = level + 0.05 * atr
            entry2 = level - 0.15 * atr
            stop = min(min(x["low"] for x in c15[-20:]), level) - 0.5 * atr
            average = (entry1 + entry2) / 2
            risk = average - stop
            targets = [average + risk * r for r in (1.5, 2.5, 3.5)]
        else:
            level = sr.get("support")
            if not level or level >= price:
                return None
            distance = (price - level) / atr
            if not 0.10 <= distance <= 1.50 or not 30 <= rsi <= 58:
                return None
            entry1 = level - 0.05 * atr
            entry2 = level + 0.15 * atr
            stop = max(max(x["high"] for x in c15[-20:]), level) + 0.5 * atr
            average = (entry1 + entry2) / 2
            risk = stop - average
            targets = [average - risk * r for r in (1.5, 2.5, 3.5)]

        if risk <= 0:
            return None

        score = 55
        reasons = ["روند ۱۵ دقیقه‌ای و یک‌ساعته همسو"]
        if t4 == direction:
            score += 15
            reasons.append("روند چهار‌ساعته همسو")
        elif t4 not in (None, "NEUTRAL"):
            score -= 10
            reasons.append("هشدار: روند چهار‌ساعته مخالف")
        rsi_ok = (48 <= rsi <= 65) if direction == "LONG" else (35 <= rsi <= 52)
        if rsi_ok:
            score += 10
            reasons.append("RSI مناسب")
        if volume is not None and volume >= 1.15:
            score += 10
            reasons.append("حجم بالاتر از میانگین")
        if distance <= 0.75:
            score += 10
            reasons.append("نزدیکی به سطح شکست")
        if score < MIN_SCORE:
            return None

        close_time = int(c15[-1]["time"]) + 900000
        return {
            "signal_id": f"{symbol}|{direction}|{close_time}",
            "symbol": symbol,
            "direction": direction,
            "setup": "PRE_BREAKOUT",
            "status": "EXPERIMENTAL_UNCONFIRMED",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "valid_until": close_time + 3600000,
            "current_price": price,
            "structure_level": level,
            "entry1": entry1,
            "entry1_reason": "ورود پس از شکست سطح ساختاری",
            "entry2": entry2,
            "entry2_reason": "ورود روی پولبک و آزمون مجدد سطح",
            "average_entry_if_both_filled": average,
            "stop_loss": stop,
            "stop_reason": "پشت ساختار اخیر به‌علاوه حاشیه ATR",
            "tp1": targets[0],
            "tp2": targets[1],
            "tp3": targets[2],
            "target_basis": "1.5R / 2.5R / 3.5R از میانگین دو ورود",
            "risk_percent": abs(risk / average) * 100,
            "rsi": rsi,
            "volume_ratio": volume,
            "trend_1h": t1,
            "trend_4h": t4,
            "distance_to_level_atr": distance,
            "quality_score": score,
            "reasons": reasons,
            "invalidation": "عبور و بسته‌شدن قیمت آن‌سوی حد ضرر؛ پس از پایان اعتبار زمانی نیز ورود جدید ممنوع است."
        }
    except Exception:
        logging.exception("Analysis failed: %s", symbol)
        return None


def main():
    contracts = s.get_contracts() or []
    symbols = sorted({
        str(x.get("symbol") if isinstance(x, dict) else x)
        for x in contracts
    })
    print(f"Active contracts: {len(symbols)}")

    found = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(analyze, symbol) for symbol in symbols]
        for job in as_completed(jobs):
            result = job.result()
            if result:
                found.append(result)

    found.sort(key=lambda x: x["quality_score"], reverse=True)

    old_ids = set()
    if os.path.exists(OUTPUT):
        with open(OUTPUT, encoding="utf-8") as f:
            for line in f:
                try:
                    old_ids.add(json.loads(line)["signal_id"])
                except (ValueError, KeyError):
                    pass

    new = [x for x in found if x["signal_id"] not in old_ids]
    with open(OUTPUT, "a", encoding="utf-8") as f:
        for item in new:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"New signals: {len(new)}")
    print(f"Total unique signals recorded: {len(old_ids) + len(new)}")
    print("TELEGRAM: OFF | VIP: OFF | AUTO TRADING: OFF")

    for x in new[:20]:
        print("\n" + "=" * 48)
        print(f"{x['symbol']} | {x['direction']} | Score: {x['quality_score']}")
        print(f"Current: {x['current_price']:.10g} | Level: {x['structure_level']:.10g}")
        print(f"Entry 1: {x['entry1']:.10g} | Entry 2: {x['entry2']:.10g}")
        print(f"SL: {x['stop_loss']:.10g}")
        print(f"TP1: {x['tp1']:.10g} | TP2: {x['tp2']:.10g} | TP3: {x['tp3']:.10g}")
        print("Reasons:", "، ".join(x["reasons"]))

    print(f"\nSaved in {OUTPUT}")
    if len(old_ids) + len(new) < 10:
        print(f"برای شروع ارزیابی آماری، {10 - len(old_ids) - len(new)} سیگنال دیگر لازم است.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    main()
