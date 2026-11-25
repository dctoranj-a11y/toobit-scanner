import csv
from collections import Counter
from statistics import mean, median

with open("toobit_backtest_results.csv", newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

rows = [r for r in rows if r["method"] == "candidate"]

groups = {
    "TP3": [r for r in rows if r["outcome"] == "TP3"],
    "SL": [r for r in rows if r["outcome"] == "SL"],
    "OPEN": [r for r in rows if r["outcome"] == "OPEN"],
}

numeric = [
    "score",
    "breakout_score",
    "breakout_touches",
    "breakout_body_ratio",
    "breakout_volume_ratio",
    "breakout_distance",
    "retest_distance",
    "confirmation_score",
    "confirmation_volume_ratio",
    "indicator_score",
    "rsi",
    "macd_histogram",
    "fib_match_count",
]

categorical = [
    "direction",
    "retest_present",
    "confirmation_pattern",
    "higher_trend",
]

print("\n========== CANDIDATE FEATURE COMPARISON ==========")
print("Total candidate:", len(rows))

for label, subset in groups.items():
    print(f"\n{label}: {len(subset)} signals")

print("\n========== NUMERIC FEATURES ==========")
print(f"{'FEATURE':30s} {'TP3 AVG':>12s} {'SL AVG':>12s} {'TP3 MED':>12s} {'SL MED':>12s}")

for field in numeric:
    values = {}
    for label in ("TP3", "SL"):
        nums = []
        for r in groups[label]:
            try:
                value = float(r[field])
                nums.append(value)
            except (ValueError, TypeError, KeyError):
                pass
        values[label] = nums

    if values["TP3"] and values["SL"]:
        print(
            f"{field:30s} "
            f"{mean(values['TP3']):12.3f} "
            f"{mean(values['SL']):12.3f} "
            f"{median(values['TP3']):12.3f} "
            f"{median(values['SL']):12.3f}"
        )

print("\n========== CATEGORICAL FEATURES ==========")

for field in categorical:
    print(f"\n{field}:")
    for label in ("TP3", "SL"):
        counts = Counter(r.get(field, "") for r in groups[label])
        print(f"  {label}: {dict(counts)}")

print("\n========== INDIVIDUAL CANDIDATE SIGNALS ==========")
for r in rows:
    print(
        r["symbol"],
        r["direction"],
        "score=" + r["score"],
        "outcome=" + r["outcome"],
        "RSI=" + r["rsi"],
        "trend4h=" + r["higher_trend"],
        "pattern=" + str(r["confirmation_pattern"]),
        "retest=" + str(r["retest_present"]),
        "fib=" + r["fib_match_count"],
    )

print("\nWARNING: Very small sample. These are exploratory comparisons, not proven filters.")
