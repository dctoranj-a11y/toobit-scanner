import requests

BASE_URL = "https://api.toobit.com"
MIN_VOLUME_USDT = 1_000_000
MAX_SYMBOLS = 100

# دارایی‌های با پشتوانه فلزات گران‌بها را وارد بک‌تست رمزارزها نکن.
EXCLUDED_BASES = {
    "PAXG", "XAUT", "KAU", "KAG", "GOLD", "SILVER"
}

session = requests.Session()
session.headers.update({"User-Agent": "Toobit-Backtest-Symbol-Selector/1.0"})

def get_json(url, params=None):
    response = session.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()

def unwrap(data):
    if isinstance(data, dict):
        return data.get("data", data.get("result", data))
    return data

def main():
    print("Downloading 24h ticker data...")
    ticker = unwrap(get_json(
        BASE_URL + "/quote/v1/contract/ticker/24hr",
        {"realtimeInterval": "24h"}
    ))

    print("Downloading contract information...")
    info = unwrap(get_json(BASE_URL + "/api/v1/exchangeInfo"))

    if not isinstance(ticker, list):
        raise RuntimeError("Unexpected ticker API response")

    if not isinstance(info, dict) or not isinstance(info.get("contracts"), list):
        raise RuntimeError("Unexpected exchangeInfo API response")

    active = {
        c.get("symbol"): c
        for c in info["contracts"]
        if c.get("status") == "TRADING"
        and isinstance(c.get("symbol"), str)
    }

    selected = []
    excluded_category = 0
    excluded_volume = 0
    excluded_other = 0

    for item in ticker:
        symbol = item.get("s")
        if not isinstance(symbol, str) or not symbol.endswith("-SWAP-USDT"):
            continue

        contract = active.get(symbol)
        if not contract:
            excluded_other += 1
            continue

        base = symbol[:-len("-SWAP-USDT")]
        categories = contract.get("categories") or []

        if not isinstance(categories, list) or not categories:
            excluded_category += 1
            continue

        if "TradFi" in categories or "Forex" in categories:
            excluded_category += 1
            continue

        if base in EXCLUDED_BASES:
            excluded_category += 1
            continue

        try:
            quote_volume = float(item.get("qv", 0))
        except (TypeError, ValueError):
            excluded_other += 1
            continue

        if quote_volume < MIN_VOLUME_USDT:
            excluded_volume += 1
            continue

        selected.append({
            "symbol": symbol,
            "volume": quote_volume,
            "categories": categories
        })

    selected.sort(key=lambda x: x["volume"], reverse=True)
    selected = selected[:MAX_SYMBOLS]

    if not selected:
        raise RuntimeError("No symbols passed the filters; no file written.")

    symbols = [x["symbol"] for x in selected]

    with open("backtest_symbols.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(symbols) + "\n")

    print("\n========== SELECTION SUMMARY ==========")
    print("Active contracts:", len(active))
    print("Selected symbols:", len(symbols))
    print("Minimum 24h volume:", f"{MIN_VOLUME_USDT:,.0f} USDT")
    print("Maximum symbols:", MAX_SYMBOLS)
    print("Excluded by category:", excluded_category)
    print("Excluded by volume:", excluded_volume)
    print("Other exclusions:", excluded_other)

    print("\n========== SELECTED SYMBOLS ==========")
    for i, item in enumerate(selected, 1):
        print(
            f"{i:3}. {item['symbol']:25} "
            f"{item['volume']:>15,.0f} USDT | {item['categories']}"
        )

    print("\nSaved: backtest_symbols.txt")
    print("\nBACKTEST COMMAND:")
    print(
        "python toobit_backtest.py --days 30 --symbols "
        + ",".join(symbols)
    )

if __name__ == "__main__":
    main()
