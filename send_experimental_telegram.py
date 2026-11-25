import json
import time
import os
import toobit_scanner as s

SOURCE = "experimental_signals.jsonl"
STATE = "experimental_telegram_sent.json"

if not s.BRIDGE_SECRET:
    raise SystemExit("ERROR: TOOBIT_BRIDGE_SECRET is not configured.")

with open(SOURCE, encoding="utf-8") as f:
    signals = [json.loads(line) for line in f if line.strip()][:10]

sent = set()
if os.path.exists(STATE):
    with open(STATE, encoding="utf-8") as f:
        sent = set(json.load(f))

def price(value):
    return f"{float(value):.10g}"

for i, x in enumerate(signals, 1):
    sid = x.get("signal_id") or f"{x['symbol']}|{x['direction']}|{x.get('close_time', '')}"
    if sid in sent:
        print(f"{i}/10 SKIPPED (already sent): {x['symbol']}")
        continue

    message = (
        "🧪 سیگنال آزمایشی — تأییدنشده\n"
        "⚠️ صرفاً جهت بررسی و ارزیابی؛ توصیه معاملاتی نیست.\n\n"
        f"📌 {x['symbol']}\n"
        f"جهت: {'🟢 LONG' if x['direction'] == 'LONG' else '🔴 SHORT'}\n"
        f"امتیاز آزمایشی: {x['quality_score']}/100\n\n"
        f"قیمت هنگام تولید: {price(x['current_price'])}\n"
        f"ورود اول: {price(x['entry1'])}\n"
        f"ورود دوم: {price(x['entry2'])}\n"
        f"حد ضرر: {price(x['stop_loss'])}\n\n"
        f"🎯 TP1: {price(x['tp1'])}\n"
        f"🎯 TP2: {price(x['tp2'])}\n"
        f"🎯 TP3: {price(x['tp3'])}\n\n"
        "وضعیت: آزمایشی؛ قبل از هر تصمیم، نمودار و قیمت فعلی بررسی شود."
    )

    if s.send_bridge_text(message):
        sent.add(sid)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent), f)
        print(f"{i}/10 SENT: {x['symbol']}")
    else:
        print(f"{i}/10 FAILED: {x['symbol']}")
        break

    time.sleep(1.2)

print(f"Finished. Sent/recorded: {len(sent)}")
