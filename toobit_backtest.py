#!/usr/bin/env python3
"""
Read-only historical backtest for Toobit Scanner.
Does not modify scanner source, database, or Telegram.
Run beside toobit_scanner.py: python toobit_backtest.py
"""
import argparse
import csv
import importlib
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.toobit.com"
INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
DEFAULT_SYMBOLS = [
    "BTC-SWAP-USDT", "ETH-SWAP-USDT", "SOL-SWAP-USDT", "BNB-SWAP-USDT",
    "XRP-SWAP-USDT", "DOGE-SWAP-USDT", "ADA-SWAP-USDT", "LINK-SWAP-USDT",
    "AVAX-SWAP-USDT", "1000PEPE-SWAP-USDT"
]
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Toobit-Scanner-Backtest/1.0", "Accept": "application/json"})


def fetch_klines(symbol, interval, days, end_ms=None):
    """Fetch validated, closed candles with backward pagination and no page truncation."""
    step = INTERVAL_MS[interval]
    requested_end_ms = int(end_ms or time.time() * 1000)
    start_ms = requested_end_ms - int(days * 86_400_000)
    page_end_ms = requested_end_ms
    out = {}
    previous_oldest = None

    while page_end_ms > start_ms:
        params = {
            "symbol": symbol, "interval": interval,
            "startTime": start_ms, "endTime": page_end_ms, "limit": 1000,
        }
        response = SESSION.get(BASE_URL + "/quote/v1/klines", params=params, timeout=25)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            data = data.get("data", data.get("result"))
        if not isinstance(data, list):
            raise RuntimeError(f"Invalid kline response for {symbol} {interval}")
        if not data:
            break

        parsed = []
        for item in data:
            try:
                if isinstance(item, dict):
                    t = item.get("time", item.get("t"))
                    o = item.get("open", item.get("o"))
                    h = item.get("high", item.get("h"))
                    lo = item.get("low", item.get("l"))
                    c = item.get("close", item.get("c"))
                    v = item.get("volume", item.get("v", 0))
                else:
                    t, o, h, lo, c, v = item[:6]
                t = int(t)
                if t < 1_000_000_000_000:
                    t *= 1000
                o, h, lo, c, v = map(float, (o, h, lo, c, v))
                values = (o, h, lo, c, v)
                if not all(math.isfinite(x) for x in values):
                    continue
                if min(o, h, lo, c) <= 0 or v < 0 or h < max(o, c, lo) or lo > min(o, c, h):
                    continue
                parsed.append({"time": t, "open": o, "high": h, "low": lo, "close": c, "volume": v})
            except (TypeError, ValueError, IndexError, OverflowError):
                continue

        if not parsed:
            raise RuntimeError(f"No valid candles in API page for {symbol} {interval}")
        parsed.sort(key=lambda x: x["time"])
        oldest_time = parsed[0]["time"]
        if previous_oldest is not None and oldest_time >= previous_oldest:
            print(f"WARNING: Pagination stopped without progress: {symbol} {interval}")
            break

        for candle in parsed:
            t = candle["time"]
            if start_ms <= t and t + step <= requested_end_ms and t <= page_end_ms:
                out[t] = candle

        if oldest_time <= start_ms:
            break
        previous_oldest = oldest_time
        page_end_ms = oldest_time - 1
        time.sleep(0.15)

    result = sorted(out.values(), key=lambda x: x["time"])
    print(f"  {interval} pagination: {len(result)} candles")
    return result



def contiguous_suffix(candles, interval_ms):
    """Return the newest uninterrupted candle segment."""
    if not candles:
        return []
    ordered = sorted(candles, key=lambda item: int(item["time"]))
    start = len(ordered) - 1
    while start > 0:
        if int(ordered[start]["time"]) - int(ordered[start - 1]["time"]) != interval_ms:
            break
        start -= 1
    return ordered[start:]


def make_pairwise_line(points, candles_len, current_close, direction, atr, mod):
    """Alternative candidate line: test every swing-point pair, score by touches and proximity."""
    if atr is None or atr <= 0 or len(points) < mod.MIN_TRENDLINE_TOUCHES:
        return None
    best = None
    # Limit to the most recent 16 confirmed pivots to keep the search bounded.
    pts = points[-16:]
    for i in range(len(pts) - 1):
        for j in range(i + 1, len(pts)):
            p1, p2 = pts[i], pts[j]
            dx = p2["index"] - p1["index"]
            if dx == 0:
                continue
            slope = (p2["price"] - p1["price"]) / dx
            if direction == "LONG" and slope >= 0:
                continue
            if direction == "SHORT" and slope <= 0:
                continue
            if abs(slope) > atr * 1.5:
                continue
            intercept = p1["price"] - slope * p1["index"]
            line = {"slope": slope, "intercept": intercept,
                    "start_index": p1["index"], "end_index": p2["index"]}
            touches = [
                q for q in pts
                if abs(q["price"] - mod.line_price(line, q["index"])) <= atr * 0.45
            ]
            if len(touches) < mod.MIN_TRENDLINE_TOUCHES:
                continue
            inds = [q["index"] for q in touches]
            if max(inds) - min(inds) < 12:
                continue
            line["points"] = touches
            line["touches"] = len(touches)
            line["current_price"] = mod.line_price(line, candles_len - 1)
            # Prefer more independent touches, then broad time coverage, then nearer current line.
            distance_atr = abs(line["current_price"] - current_close) / atr
            rank = (len(touches), max(inds) - min(inds), -distance_atr, -abs(slope) / atr)
            if best is None or rank > best[0]:
                best = (rank, line)
    return best[1] if best else None


def line_price_at(line, idx):
    return line["slope"] * idx + line["intercept"]


def get_line(candles_prefix, direction, atr, method, mod):
    highs, lows = mod.find_swings(candles_prefix)
    points = highs if direction == "LONG" else lows
    if method == "legacy":
        return mod.analyze_trendline(candles_prefix, direction, atr)
    return make_pairwise_line(points, len(candles_prefix), float(candles_prefix[-1]["close"]), direction, atr, mod)


def first_confirmation(five, start_ms, line_price, atr, direction, mod):
    """Return the first valid closed 5m confirmation within ten minutes."""
    for i, candle in enumerate(five):
        close_time = int(candle["time"]) + INTERVAL_MS["5m"]
        if close_time < int(start_ms):
            continue
        if close_time - int(start_ms) > 600_000:
            break
        if i < 29:
            continue
        recent = five[i - 29:i + 1]
        if any(int(b["time"]) - int(a["time"]) != INTERVAL_MS["5m"] for a, b in zip(recent, recent[1:])):
            continue
        prefix = five[:i + 1]
        pattern = mod.detect_candle_pattern(prefix, direction)
        if pattern is None:
            continue
        close = float(candle["close"])
        if atr and atr > 0 and abs(close - float(line_price)) > float(atr):
            continue
        vr = mod.calculate_volume_ratio(prefix)
        score = int(pattern["score"])
        return {
            "confirmed": True, "pattern": pattern["name"], "score": score,
            "price": close, "high": float(candle["high"]), "low": float(candle["low"]),
            "time": int(candle["time"]), "volume_ratio": vr,
            "close_time": close_time, "index": i,
        }
    return None



def simulate_exit(five, start_idx, signal, mod, entry_time=None, max_age_hours=24):
    """Conservative 5m simulation matching live partial exits; SL wins same-candle conflicts."""
    direction = signal["direction"]
    entry = float(signal["entry"])
    stop = float(signal["stop_loss"])
    targets = [float(signal["tp1"]), float(signal["tp2"]), float(signal["tp3"])]
    fractions = [float(mod.TP1_CLOSE_FRACTION), float(mod.TP2_CLOSE_FRACTION)]
    target_done = [False, False]
    remaining = 1.0
    realized_r = 0.0
    max_favorable_r = 0.0
    max_adverse_r = 0.0
    risk = abs(entry - stop)
    if not math.isfinite(risk) or risk <= 0 or start_idx < 0 or start_idx > len(five):
        return {"outcome": "INVALID", "realized_R": 0.0, "exit_time": None,
                "mfe_R": 0.0, "mae_R": 0.0}
    if start_idx == len(five):
        return {"outcome": "OPEN", "realized_R": 0.0, "exit_time": None,
                "mfe_R": 0.0, "mae_R": 0.0}
    if any(not math.isfinite(target) for target in targets):
        return {"outcome": "INVALID", "realized_R": 0.0, "exit_time": None,
                "mfe_R": 0.0, "mae_R": 0.0}

    expected_start = int(entry_time) if entry_time is not None else int(five[start_idx]["time"])
    if int(five[start_idx]["time"]) != expected_start:
        return {"outcome": "DATA_GAP", "realized_R": 0.0, "exit_time": None,
                "mfe_R": 0.0, "mae_R": 0.0}
    expiry_ms = expected_start + int(max_age_hours * 3_600_000)
    previous_open = None
    for candle in five[start_idx:]:
        candle_open = int(candle["time"])
        candle_close = candle_open + INTERVAL_MS["5m"]
        if candle_close > expiry_ms:
            break
        if previous_open is not None and candle_open - previous_open != INTERVAL_MS["5m"]:
            return {"outcome": "DATA_GAP", "realized_R": realized_r, "exit_time": None,
                    "mfe_R": max_favorable_r, "mae_R": max_adverse_r}
        previous_open = candle_open

        hi, lo = float(candle["high"]), float(candle["low"])
        if direction == "LONG":
            adverse = max(0.0, entry - lo) / risk
            favorable = max(0.0, hi - entry) / risk
            stop_hit = lo <= stop
            target_hits = [hi >= target for target in targets]
        else:
            adverse = max(0.0, hi - entry) / risk
            favorable = max(0.0, entry - lo) / risk
            stop_hit = hi >= stop
            target_hits = [lo <= target for target in targets]
        max_favorable_r = max(max_favorable_r, favorable)
        max_adverse_r = max(max_adverse_r, adverse)

        if stop_hit:
            realized_r -= remaining
            return {"outcome": "SL", "realized_R": realized_r,
                    "exit_time": candle_open, "mfe_R": max_favorable_r, "mae_R": max_adverse_r}

        # Live logic closes TP1/TP2 fractions of the initial position. TP3 closes
        # whatever remains, even if fractions are changed in future configuration.
        for k in range(2):
            if target_hits[k] and not target_done[k] and remaining > 1e-9:
                fraction = min(fractions[k], remaining)
                target_r = abs(targets[k] - entry) / risk
                realized_r += fraction * target_r
                remaining = max(0.0, remaining - fraction)
                target_done[k] = True

        if target_hits[2] and remaining > 1e-9:
            target_r = abs(targets[2] - entry) / risk
            realized_r += remaining * target_r
            remaining = 0.0

        if remaining <= 1e-9:
            return {"outcome": "TP3", "realized_R": realized_r,
                    "exit_time": candle_open, "mfe_R": max_favorable_r, "mae_R": max_adverse_r}

    # Only expire when the downloaded data actually reaches the expiry boundary.
    if five and int(five[-1]["time"]) + INTERVAL_MS["5m"] >= expiry_ms:
        eligible = [
            candle for candle in five[start_idx:]
            if int(candle["time"]) + INTERVAL_MS["5m"] <= expiry_ms
        ]
        expected_last_open = ((expiry_ms - INTERVAL_MS["5m"]) // INTERVAL_MS["5m"]) * INTERVAL_MS["5m"]
        if not eligible or int(eligible[-1]["time"]) != expected_last_open:
            return {"outcome": "DATA_GAP", "realized_R": realized_r, "exit_time": None,
                    "mfe_R": max_favorable_r, "mae_R": max_adverse_r}
        mark = eligible[-1]
        mark_price = float(mark["close"])
        mark_r = ((mark_price - entry) if direction == "LONG" else (entry - mark_price)) / risk
        realized_r += remaining * mark_r
        return {"outcome": "EXPIRED", "realized_R": realized_r,
                "exit_time": int(mark["time"]), "mfe_R": max_favorable_r, "mae_R": max_adverse_r}

    return {"outcome": "OPEN", "realized_R": realized_r,
            "exit_time": None, "mfe_R": max_favorable_r, "mae_R": max_adverse_r}


def run_backtest(symbols, days, methods, mod):
    all_trades = []
    backtest_end_ms = int(time.time() * 1000)
    evaluation_start_ms = backtest_end_ms - int(days * 86_400_000)
    for symbol in symbols:
        print(f"\n[{symbol}] downloading candles...", flush=True)
        cache = {}
        for tf in ("5m", "15m", "1h", "4h"):
            try:
                # Warm-up: 200 main-timeframe bars and at least 60 higher-timeframe bars.
                extra_days = 1 if tf == "5m" else 11
                cache[tf] = fetch_klines(
                    symbol, tf, days + extra_days, end_ms=backtest_end_ms
                )
            except Exception as exc:
                print(f"  {tf} download failed: {exc}")
                cache[tf] = []

        five, higher = cache["5m"], cache["4h"]
        if len(five) < 100 or len(higher) < 60:
            print("  skipped: insufficient 5m/4h history")
            continue

        for timeframe in ("15m", "1h"):
            candles = cache[timeframe]
            if len(candles) < 100:
                print(f"  skipped {timeframe}: only {len(candles)} candles")
                continue
            step = INTERVAL_MS[timeframe]
            print(f"  {timeframe}: {len(candles)} candles | 5m: {len(five)} | 4h: {len(higher)}")

            first_eval_idx = next(
                (idx for idx, candle in enumerate(candles)
                 if int(candle["time"]) + step >= evaluation_start_ms),
                len(candles)
            )

            for method in methods:
                for direction in ("LONG", "SHORT"):
                    seen_setups = set()
                    busy_until = 0
                    blocked_by_open_trade = False

                    for scan_idx in range(max(99, first_eval_idx), len(candles)):
                        scan_close_time = int(candles[scan_idx]["time"]) + step
                        if scan_close_time < busy_until:
                            continue

                        window_start = max(0, scan_idx + 1 - int(getattr(mod, "CANDLE_LIMIT", 200)))
                        window = contiguous_suffix(candles[window_start:scan_idx + 1], step)
                        if len(window) < 100:
                            continue

                        first_index = max(100, len(window) - int(mod.RETEST_MAX_BARS) - 1)
                        signal_created = False

                        for breakout_index in range(first_index, len(window)):
                            prefix = window[:breakout_index]
                            if len(prefix) < 70:
                                continue
                            prefix_atr = mod.calculate_atr(prefix)
                            if prefix_atr is None or prefix_atr <= 0:
                                continue
                            line = get_line(prefix, direction, prefix_atr, method, mod)
                            if line is None:
                                continue

                            breakout = mod.analyze_breakout(
                                window, line, direction, prefix_atr, index=breakout_index
                            )
                            if breakout is None:
                                continue

                            breakout_time = int(window[breakout_index]["time"])
                            setup_key = f"{symbol}|{timeframe}|{direction}|{breakout_time}"
                            if setup_key in seen_setups:
                                continue

                            retest = None
                            retest_idx = None
                            for r_idx in range(
                                breakout_index + 1,
                                min(len(window), breakout_index + int(mod.RETEST_MAX_BARS) + 1)
                            ):
                                retest_atr = mod.calculate_atr(window[:r_idx + 1])
                                if retest_atr is None or retest_atr <= 0:
                                    continue
                                trial = mod.analyze_retest(
                                    window, line, direction, retest_atr, index=r_idx
                                )
                                if trial:
                                    retest, retest_idx = trial, r_idx
                                    break

                            if retest is None:
                                # No look-ahead: immediate breakout is allowed only
                                # on the latest closed main-timeframe candle.
                                if breakout_index != len(window) - 1 or breakout.get("score", 0) < 25:
                                    continue
                                reference_idx = breakout_index
                            else:
                                reference_idx = retest_idx

                            ref_close_time = int(window[reference_idx]["time"]) + step
                            line_at_ref = line_price_at(line, reference_idx)
                            confirm = first_confirmation(
                                five, ref_close_time, line_at_ref, prefix_atr, direction, mod
                            )
                            if confirm is None:
                                continue

                            # Match live: all indicators and levels use at most the
                            # latest CANDLE_LIMIT main candles closed by confirmation.
                            hist_all = [
                                candle for candle in candles
                                if int(candle["time"]) + step <= int(confirm["close_time"])
                            ]
                            hist = contiguous_suffix(
                                hist_all[-int(getattr(mod, "CANDLE_LIMIT", 200)):], step
                            )
                            if len(hist) < 100:
                                continue
                            signal_atr = mod.calculate_atr(hist)
                            if signal_atr is None or signal_atr <= 0:
                                continue
                            indicator_data = mod.analyze_indicators(hist, direction)
                            if indicator_data is None:
                                continue

                            htf_hist_all = [
                                candle for candle in higher
                                if int(candle["time"]) + INTERVAL_MS["4h"] <= int(confirm["close_time"])
                            ]
                            htf_hist = contiguous_suffix(htf_hist_all[-200:], INTERVAL_MS["4h"])
                            if len(htf_hist) < 60:
                                continue
                            higher_trend = mod.analyze_higher_trend(htf_hist)
                            sr = mod.find_support_resistance(hist)
                            levels, levels_rejection = mod.make_trade_levels(
                                hist, direction, signal_atr, sr, entry_price=confirm["price"]
                            )
                            if levels is None or levels["rr_tp1"] < mod.MIN_RR_TP1:
                                continue
                            fib = mod.fibonacci_levels(hist, direction=direction)
                            fib_matches = mod.fibonacci_confluence(
                                levels["entry"], fib, signal_atr
                            )
                            score = mod.calculate_signal_score(
                                breakout, retest, confirm, indicator_data,
                                higher_trend, fib_matches, direction
                            )
                            if score < mod.MIN_HISTORY_SCORE:
                                continue

                            entry_time = int(confirm["close_time"])
                            if entry_time < evaluation_start_ms:
                                continue
                            exit_start = next(
                                (k for k, candle in enumerate(five)
                                 if int(candle["time"]) >= entry_time), len(five)
                            )

                            signal = {
                                "symbol": symbol, "timeframe": timeframe,
                                "direction": direction, "score": score,
                                "entry": levels["entry"], "stop_loss": levels["stop_loss"],
                                "tp1": levels["tp1"], "tp2": levels["tp2"],
                                "tp3": levels["tp3"],
                                "risk_percent": levels["risk_percent"],
                                "leverage": levels["leverage"],
                            }
                            result = simulate_exit(
                                five, exit_start, signal, mod,
                                entry_time=entry_time,
                                max_age_hours=getattr(mod, "SIGNAL_MAX_AGE_HOURS", 24),
                            )

                            macd_data = indicator_data.get("macd") or {}
                            fib_match_levels = "|".join(
                                str(item.get("level", "")) for item in fib_matches
                            )
                            trade = {
                                **signal,
                                "method": method,
                                "setup_key": setup_key,
                                "breakout_time": breakout_time,
                                "reference_time": int(window[reference_idx]["time"]),
                                "entry_time": entry_time,
                                "breakout_score": breakout.get("score"),
                                "breakout_touches": breakout.get("touches"),
                                "breakout_body_ratio": breakout.get("body_ratio"),
                                "breakout_volume_ratio": breakout.get("volume_ratio"),
                                "breakout_distance": breakout.get("distance"),
                                "retest_present": retest is not None,
                                "retest_distance": retest.get("distance") if retest else None,
                                "confirmation_pattern": confirm.get("pattern"),
                                "confirmation_score": confirm.get("score"),
                                "confirmation_volume_ratio": confirm.get("volume_ratio"),
                                "indicator_score": indicator_data.get("score"),
                                "indicator_price": indicator_data.get("price"),
                                "rsi": indicator_data.get("rsi"),
                                "ema20": indicator_data.get("ema20"),
                                "ema50": indicator_data.get("ema50"),
                                "indicator_atr": indicator_data.get("atr"),
                                "indicator_volume_ratio": indicator_data.get("volume_ratio"),
                                "macd_histogram": macd_data.get("histogram"),
                                "higher_trend": higher_trend,
                                "higher_trend_aligned": higher_trend == direction,
                                "fib_match_count": len(fib_matches),
                                "fib_match_levels": fib_match_levels,
                                **result,
                            }
                            all_trades.append(trade)
                            seen_setups.add(setup_key)
                            signal_created = True

                            if result["exit_time"] is None:
                                # OPEN or DATA_GAP: do not invent a later entry while
                                # the previous position's final state is unknown.
                                blocked_by_open_trade = True
                            else:
                                busy_until = int(result["exit_time"]) + INTERVAL_MS["5m"]
                            break

                        if blocked_by_open_trade:
                            break
                        if signal_created:
                            continue

                    if blocked_by_open_trade:
                        print(f"  {timeframe} {method} {direction}: stopped after unresolved position")
    return all_trades


def summarize(trades):
    if not trades:
        print("\nNo qualifying historical setups found.")
        return

    print("\n================ BACKTEST SUMMARY ================")
    for method in sorted({trade["method"] for trade in trades}):
        for timeframe in ("15m", "1h"):
            subset = [t for t in trades if t["method"] == method and t["timeframe"] == timeframe]
            if not subset:
                continue
            unresolved = [t for t in subset if t["outcome"] in ("OPEN", "DATA_GAP", "INVALID")]
            closed = [t for t in subset if t["outcome"] not in ("OPEN", "DATA_GAP", "INVALID")]
            wins = [t for t in closed if t["realized_R"] > 0]
            losses = [t for t in closed if t["realized_R"] < 0]
            avg_closed_r = sum(t["realized_R"] for t in closed) / len(closed) if closed else 0.0
            avg_all_r = sum(t["realized_R"] for t in subset) / len(subset)
            win_rate = 100.0 * len(wins) / len(closed) if closed else 0.0
            print(
                f"{method:9s} {timeframe:3s} | trades={len(subset):4d} "
                f"closed={len(closed):4d} positive={len(wins):4d} "
                f"negative={len(losses):4d} win%={win_rate:5.1f}"
            )
            print(
                f"             mean closed R={avg_closed_r:.3f} | "
                f"mean all R (incl. unresolved)={avg_all_r:.3f} | "
                f"unresolved={len(unresolved)}"
            )

    print("\nBy scanner score tier:")
    tiers = [
        ("75-84", lambda score: 75 <= score < 85),
        ("85-89", lambda score: 85 <= score < 90),
        ("90-100", lambda score: score >= 90),
    ]
    for label, predicate in tiers:
        subset = [trade for trade in trades if predicate(trade["score"])]
        if not subset:
            continue
        closed = [t for t in subset if t["outcome"] not in ("OPEN", "DATA_GAP", "INVALID")]
        unresolved = len(subset) - len(closed)
        wins = [t for t in closed if t["realized_R"] > 0]
        avg_closed = sum(t["realized_R"] for t in closed) / len(closed) if closed else 0.0
        win_rate = 100.0 * len(wins) / len(closed) if closed else 0.0
        print(
            f"  {label}: {len(subset)} trades | closed={len(closed)} | "
            f"positive={len(wins)} | win%={win_rate:.1f} | "
            f"mean closed R={avg_closed:.3f} | unresolved={unresolved}"
        )

    print("\nNote: fees, funding, slippage and liquidation mechanics are not included.")
    print("This is a per-symbol setup-quality backtest, not a full delivery-queue simulation.")
    print("It does not apply the live 30-minute best-signal slot, rolling 2h cap, or final live price-drift recheck.")
    print("Same 5m candle touching SL and TP is conservatively counted as SL.")
    print("DATA_GAP/OPEN/INVALID trades are unresolved and excluded from closed-trade win rates.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--methods", default="legacy,candidate")
    args = parser.parse_args()
    if args.days < 7 or args.days > 90:
        parser.error("--days must be between 7 and 90")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    if not symbols or any(m not in ("legacy", "candidate") for m in methods):
        parser.error("invalid symbols or methods")
    try:
        mod = importlib.import_module("toobit_scanner")
    except Exception as exc:
        print(f"Could not import toobit_scanner.py: {exc}")
        sys.exit(1)
    print("Read-only Toobit historical backtest")
    print(f"Days={args.days} | Symbols={len(symbols)} | Methods={methods}")
    print("No database writes. No Telegram. No live trading.")
    print("Backtest evaluates per-symbol setups; live delivery-slot and rolling-cap rules are not simulated.")
    trades = run_backtest(symbols, args.days, methods, mod)
    summarize(trades)
    if trades:
        out = "toobit_backtest_results.csv"
        with open(out, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            writer.writeheader()
            writer.writerows(trades)
        print(f"\nDetailed results saved to {out}")


if __name__ == "__main__":
    main()
