import csv
from collections import defaultdict

FILE = "toobit_backtest_results.csv"

with open(FILE, newline="", encoding="utf-8") as f:
    trades = list(csv.DictReader(f))

trades = [t for t in trades if t.get("method") == "candidate"]

if not trades:
    print("No candidate trades found.")
    raise SystemExit

def value(t, key):
    try:
        return float(t.get(key) or 0)
    except (ValueError, TypeError):
        return 0.0

def report(title, subset):
    if not subset:
        return

    closed = [t for t in subset if t["outcome"] != "OPEN"]
    wins = [t for t in closed if value(t, "realized_R") > 0]
    losses = [t for t in closed if value(t, "realized_R") < 0]
    opens = [t for t in subset if t["outcome"] == "OPEN"]

    total_r = sum(value(t, "realized_R") for t in closed)
    avg_r = total_r / len(closed) if closed else 0
    win_rate = 100 * len(wins) / len(closed) if closed else 0

    print(f"\n{title}")
    print("-" * 65)
    print(f"Signals: {len(subset)} | Closed: {len(closed)} | Open: {len(opens)}")
    print(f"Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Closed Total R: {total_r:.3f}")
    print(f"Average R per closed trade: {avg_r:.3f}")

    outcomes = defaultdict(int)
    for t in subset:
        outcomes[t["outcome"]] += 1

    print("Outcomes:", " | ".join(
        f"{name}: {count}" for name, count in sorted(outcomes.items())
    ))

print("\n========== CANDIDATE BACKTEST ANALYSIS ==========")
print("Source:", FILE)
print("OPEN trades are excluded from closed-trade performance calculations.")

report("ALL CANDIDATE SIGNALS", trades)

print("\n========== PERFORMANCE BY SYMBOL ==========")

symbols = defaultdict(list)
for t in trades:
    symbols[t["symbol"]].append(t)

ranked = []
for symbol, subset in symbols.items():
    closed = [t for t in subset if t["outcome"] != "OPEN"]
    wins = [t for t in closed if value(t, "realized_R") > 0]
    total_r = sum(value(t, "realized_R") for t in closed)
    avg_r = total_r / len(closed) if closed else 0
    win_rate = 100 * len(wins) / len(closed) if closed else 0
    ranked.append((symbol, len(subset), len(closed),
                   len(subset) - len(closed), win_rate, total_r, avg_r))

ranked.sort(key=lambda x: (x[6], x[5]), reverse=True)

print(f"{'SYMBOL':<23} {'SIGNALS':>7} {'CLOSED':>7} {'OPEN':>5} {'WIN%':>7} {'TOTAL R':>9} {'AVG R':>8}")
print("-" * 82)

for row in ranked:
    symbol, signals, closed, opens, win_rate, total_r, avg_r = row
    print(f"{symbol:<23} {signals:>7} {closed:>7} {opens:>5} "
          f"{win_rate:>6.1f}% {total_r:>9.3f} {avg_r:>8.3f}")

print("\n========== TIMEFRAME ANALYSIS ==========")
for tf in sorted(set(t["timeframe"] for t in trades)):
    report(f"TIMEFRAME: {tf}", [
        t for t in trades if t["timeframe"] == tf
    ])

print("\n========== DIRECTION ANALYSIS ==========")
for direction in ("LONG", "SHORT"):
    report(f"DIRECTION: {direction}", [
        t for t in trades if t["direction"] == direction
    ])

print("\n========== SCORE TIERS ==========")

tiers = [
    ("75-79", 75, 80),
    ("80-84", 80, 85),
    ("85-89", 85, 90),
    ("90-100", 90, 101),
]

for label, low, high in tiers:
    subset = [
        t for t in trades
        if low <= int(value(t, "score")) < high
    ]
    report(f"SCORE: {label}", subset)

print("\n========== TIMEFRAME + SCORE ==========")
for tf in sorted(set(t["timeframe"] for t in trades)):
    for label, low, high in tiers:
        subset = [
            t for t in trades
            if t["timeframe"] == tf
            and low <= int(value(t, "score")) < high
        ]
        if subset:
            report(f"{tf} | SCORE {label}", subset)

print("\nNote: Fees, funding, slippage and liquidation mechanics are not included.")
print("Small samples should not be treated as reliable performance evidence.")
print("\nAnalysis completed. No scanner or backtest source files were modified.")
