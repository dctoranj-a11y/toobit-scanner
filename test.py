import logging
import toobit_scanner as scanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

def main():
    print("\n======================================")
    print("       TOOBIT DIAGNOSTIC MODE")
    print("       TELEGRAM: DISABLED")
    print("       VIP QUEUE: DISABLED")
    print("       AUTO TRADING: DISABLED")
    print("======================================\n")

    scanner.reset_diagnostics()

    contracts = scanner.get_contracts()
    print(f"Contracts loaded: {len(contracts or [])}")

    if not contracts:
        print("Diagnostic stopped: no contracts available.")
        return

    scan_ok = scanner.scan_market()

    print("\n========== DIAGNOSTIC RESULT ==========")
    print(f"Scan completed successfully: {scan_ok}")
    print(f"Candidates found: {len(scanner.signal_candidates)}")
    scanner.report_diagnostics()
    print("======================================")
    print("No Telegram messages sent.")
    print("No VIP queue operations performed.")

if __name__ == "__main__":
    main()
