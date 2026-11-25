import json
import time
from io import BytesIO
from pathlib import Path
import toobit_scanner as scanner

SIGNALS = Path("independent_signals.jsonl")
CHARTS = Path("independent_charts")
STATE = Path("independent_telegram_sent.json")

sent = set()
if STATE.exists():
    try:
        sent = set(json.loads(STATE.read_text(encoding="utf-8")))
    except Exception:
        print("WARNING: Invalid independent state file; starting fresh.")
        sent = set()

signals = [
    json.loads(line)
    for line in SIGNALS.read_text(encoding="utf-8").splitlines()
    if line.strip()
]

success = 0
failed = 0

for s in signals:
    symbol = s["symbol"]
    chart = CHARTS / f"{symbol}.png"

    if symbol in sent:
        print("SKIP (already confirmed):", symbol)
        continue

    if not chart.exists():
        print("MISSING CHART:", chart)
        failed += 1
        continue

    caption = (
        f"🧪 INDEPENDENT EXPERIMENTAL SIGNAL\n\n"
        f"Symbol: {symbol}\n"
        f"Direction: {s['direction']}\n"
        f"Score: {s['score']}/100\n\n"
        f"Entry 1: {s['entry1']}\n"
        f"Entry 2: {s.get('entry2')}\n"
        f"Stop Loss: {s['stop_loss']}\n"
        f"TP1: {s['tp1']}\n"
        f"TP2: {s['tp2']}\n"
        f"TP3: {s['tp3']}\n\n"
        f"Stop distance: {s['stop_distance_pct']}%\n"
        f"Leverage estimate: {s['leverage_suggestion']}x\n\n"
        f"⚠️ Experimental signal for manual review only.\n"
        f"Not financial advice. No automatic trading."
    )

    try:
        photo = BytesIO(chart.read_bytes())
        result = scanner.send_bridge_photo(photo, caption)

        if result is True:
            sent.add(symbol)
            STATE.write_text(
                json.dumps(sorted(sent), ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            success += 1
            print("CONFIRMED SENT:", symbol)
            time.sleep(2)
        else:
            failed += 1
            print("FAILED (not recorded):", symbol)

    except Exception as exc:
        failed += 1
        print("FAILED:", symbol, repr(exc))

print(f"\nConfirmed sent: {success}")
print(f"Failed: {failed}")
print(f"Already confirmed before this run: {len(sent) - success}")
