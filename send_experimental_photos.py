import json
import os
import time
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import toobit_scanner as s

SOURCE = "experimental_signals.jsonl"
STATE = "experimental_telegram_photo_sent.json"

if not s.BRIDGE_SECRET:
    raise SystemExit("ERROR: TOOBIT_BRIDGE_SECRET is not configured.")

with open(SOURCE, encoding="utf-8") as f:
    signals = [json.loads(line) for line in f if line.strip()][:10]

sent = set()
if os.path.exists(STATE):
    with open(STATE, encoding="utf-8") as f:
        sent = set(json.load(f))

def fmt(v):
    return f"{float(v):.10g}"

def make_chart(x):
    candles = s.get_candles(x["symbol"], "15m", limit=80)
    if not candles:
        return None

    fig, ax = plt.subplots(figsize=(14, 8))

    for i, c in enumerate(candles):
        op, cl = float(c["open"]), float(c["close"])
        hi, lo = float(c["high"]), float(c["low"])
        color = "green" if cl >= op else "red"
        ax.vlines(i, lo, hi, color=color, linewidth=1)
        bottom = min(op, cl)
        height = max(abs(cl - op), abs(op) * 0.00001, 1e-10)
        ax.bar(i, height, bottom=bottom, width=0.65, color=color)

    levels = [
        ("ENTRY 1", x["entry1"], "blue"),
        ("ENTRY 2", x["entry2"], "deepskyblue"),
        ("STOP LOSS", x["stop_loss"], "red"),
        ("TP1", x["tp1"], "green"),
        ("TP2", x["tp2"], "orange"),
        ("TP3", x["tp3"], "purple"),
    ]

    for name, value, color in levels:
        ax.axhline(
            float(value), color=color, linestyle="--",
            linewidth=1.5, label=f"{name}: {fmt(value)}"
        )

    ax.axhline(
        float(x["current_price"]), color="gray",
        linestyle=":", linewidth=1.5,
        label=f"Signal price: {fmt(x['current_price'])}"
    )

    direction = x["direction"]
    ax.set_title(
        f"{x['symbol']} | {direction} | "
        f"EXPERIMENTAL SCORE {x['quality_score']}/100"
    )
    ax.set_xlabel("15-minute closed candles")
    ax.set_ylabel("Price")
    ax.grid(True, alpha=0.2)
    ax.legend(loc="best", fontsize=8, ncol=2)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf

for i, x in enumerate(signals, 1):
    sid = x.get("signal_id") or (
        f"{x['symbol']}|{x['direction']}|{x.get('close_time', '')}"
    )

    if sid in sent:
        print(f"{i}/10 ALREADY SENT: {x['symbol']}")
        continue

    caption = (
        "🧪 EXPERIMENTAL SIGNAL — UNCONFIRMED\n"
        "⚠️ For chart review only; not a trading recommendation.\n\n"
        f"{x['symbol']} | {x['direction']}\n"
        f"Experimental score: {x['quality_score']}/100\n"
        f"Signal price: {fmt(x['current_price'])}\n\n"
        f"Entry 1: {fmt(x['entry1'])}\n"
        f"Entry 2: {fmt(x['entry2'])}\n"
        f"Stop Loss: {fmt(x['stop_loss'])}\n\n"
        f"TP1: {fmt(x['tp1'])}\n"
        f"TP2: {fmt(x['tp2'])}\n"
        f"TP3: {fmt(x['tp3'])}\n\n"
        "15m chart | Experimental / Unconfirmed"
    )

    try:
        chart = make_chart(x)
        if chart is None:
            print(f"{i}/10 FAILED: no candles for {x['symbol']}")
            continue

        ok = s.send_bridge_photo(chart, caption)
        if ok:
            sent.add(sid)
            with open(STATE, "w", encoding="utf-8") as f:
                json.dump(sorted(sent), f)
            print(f"{i}/10 PHOTO SENT: {x['symbol']}")
        else:
            print(f"{i}/10 FAILED TO SEND: {x['symbol']}")

    except Exception as e:
        print(f"{i}/10 ERROR {x['symbol']}: {e}")

    time.sleep(1.5)

print(f"Photo messages sent/recorded: {len(sent)}")
print("AUTO DELIVERY: OFF | VIP: OFF | AUTO TRADING: OFF")
