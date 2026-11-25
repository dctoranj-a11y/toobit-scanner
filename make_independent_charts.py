import json
from pathlib import Path
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

import toobit_scanner as scanner

SIGNALS_FILE = Path("independent_signals.jsonl")
OUT_DIR = Path("independent_charts")
TIMEFRAMES = ("4h", "1h", "15m")
CANDLE_LIMIT = 100


def load_signals():
    if not SIGNALS_FILE.exists():
        raise FileNotFoundError(f"Missing file: {SIGNALS_FILE}")

    with SIGNALS_FILE.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def ema(values, period):
    if len(values) < period:
        return [None] * len(values)

    alpha = 2 / (period + 1)
    current = sum(values[:period]) / period
    result = [None] * (period - 1) + [current]

    for value in values[period:]:
        current = alpha * value + (1 - alpha) * current
        result.append(current)

    return result


def draw_candles(ax, candles):
    width = 0.62

    for i, candle in enumerate(candles):
        o = candle["open"]
        h = candle["high"]
        l = candle["low"]
        c = candle["close"]

        color = "green" if c >= o else "red"
        ax.plot([i, i], [l, h], color=color, linewidth=0.9)

        body_bottom = min(o, c)
        body_height = abs(c - o)

        if body_height == 0:
            body_height = max((h - l) * 0.002, abs(c) * 0.00001)

        rect = Rectangle(
            (i - width / 2, body_bottom),
            width,
            body_height,
            facecolor=color,
            edgecolor=color,
            linewidth=0.7
        )
        ax.add_patch(rect)

    closes = [x["close"] for x in candles]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    ax.plot(range(len(e20)), e20, label="EMA 20", linewidth=1.1)
    ax.plot(range(len(e50)), e50, label="EMA 50", linewidth=1.1)

    step = max(1, len(candles) // 8)
    ticks = list(range(0, len(candles), step))
    labels = []

    for i in ticks:
        timestamp = candles[i]["time"] / 1000
        labels.append(
            datetime.fromtimestamp(
                timestamp, timezone.utc
            ).strftime("%m-%d %H:%M")
        )

    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, rotation=25, ha="right", fontsize=8)
    ax.grid(True, alpha=0.2)
    ax.legend(loc="upper left", fontsize=8)


def create_chart(signal):
    symbol = signal["symbol"]
    direction = signal["direction"]

    fig, axes = plt.subplots(
        3, 1,
        figsize=(15, 13),
        gridspec_kw={"height_ratios": [1, 1, 1.25]}
    )

    fig.suptitle(
        f'{symbol} | {direction} | Score {signal["score"]}',
        fontsize=15,
        fontweight="bold"
    )

    for ax, tf in zip(axes, TIMEFRAMES):
        candles = scanner.get_candles(symbol, tf, CANDLE_LIMIT)
        candles = scanner.contiguous_candle_suffix(candles, tf)

        if len(candles) < 55:
            raise ValueError(
                f"{symbol} {tf}: only {len(candles)} valid candles"
            )

        candles = candles[-CANDLE_LIMIT:]
        draw_candles(ax, candles)

        ax.set_title(f"{tf} — last {len(candles)} closed candles")

        ax.axhline(
            signal["entry1"],
            linestyle="--",
            linewidth=1.2,
            label="Entry 1"
        )
        ax.axhline(
            signal["stop_loss"],
            linestyle=":",
            linewidth=1.3,
            label="Stop Loss"
        )

        if signal.get("entry2") is not None:
            ax.axhline(
                signal["entry2"],
                linestyle="-.",
                linewidth=1,
                label="Entry 2"
            )

        ax.legend(loc="upper left", fontsize=8)

    targets = (
        f'TP1: {signal["tp1"]} | '
        f'TP2: {signal["tp2"]} | '
        f'TP3: {signal["tp3"]}'
    )

    details = (
        f'Entry: {signal["entry1"]} | '
        f'SL: {signal["stop_loss"]} | '
        f'Stop distance: {signal["stop_distance_pct"]}%\n'
        f'{targets} | '
        f'Leverage estimate: {signal["leverage_suggestion"]}x'
    )

    fig.text(0.5, 0.015, details, ha="center", fontsize=9)
    fig.tight_layout(rect=[0, 0.055, 1, 0.95])

    OUT_DIR.mkdir(exist_ok=True)
    filename = OUT_DIR / f"{symbol.replace('/', '_')}.png"

    fig.savefig(filename, dpi=140, bbox_inches="tight")
    plt.close(fig)

    return filename


def main():
    signals = load_signals()

    if not signals:
        print("No signals found.")
        return

    OUT_DIR.mkdir(exist_ok=True)
    print(f"Signals loaded: {len(signals)}")

    success = 0

    for signal in signals:
        try:
            path = create_chart(signal)
            success += 1
            print(f"CREATED: {path}")
        except Exception as exc:
            print(f"FAILED: {signal.get('symbol')}: {exc}")

    print()
    print(f"Charts created: {success}/{len(signals)}")
    print(f"Output folder: {OUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
