#!/usr/bin/env python3
"""
Pre-fetch all market data needed for the presentation and save to disk cache.

Run this at home (with internet access) before going to school:

    cd backend
    source .venv/bin/activate
    python scripts/seed_cache.py

Downloads 56 tickers (6 indices + 50 Euro Stoxx 50 constituents) from yfinance
and stores them as Parquet files in data/cache/ohlcv/. All API endpoints will
then work offline (no IBKR, no internet required).

Re-run any time you want to refresh the cached data.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from src.historical import yfinance_loader, disk_cache

# All tickers needed by API endpoints
# Format: (yfinance_ticker, description)
TICKERS: list[tuple[str, str]] = [
    # Reference indices — used by risk/backtest/correlation pages
    ("^STOXX50E", "SX5E  — main index (VaR, backtest, PnL)"),
    ("^GSPC",     "SPX   — reference"),
    ("^NDX",      "NDX   — reference"),
    ("^GDAXI",    "DAX   — reference + correlation"),
    ("^N225",     "NKY   — reference"),
    ("^FCHI",     "CAC40 — reference"),
    # Euro Stoxx 50 constituents — page 1 sidebar
    ("ASML.AS",   "ASML Holding"),
    ("MC.PA",     "LVMH"),
    ("SAP.DE",    "SAP SE"),
    ("SIE.DE",    "Siemens AG"),
    ("OR.PA",     "L'Oréal"),
    ("TTE.PA",    "TotalEnergies"),
    ("SU.PA",     "Schneider Electric"),
    ("AIR.PA",    "Airbus SE"),
    ("ALV.DE",    "Allianz SE"),
    ("SAN.MC",    "Banco Santander"),
    ("BNP.PA",    "BNP Paribas"),
    ("AI.PA",     "Air Liquide"),
    ("DTE.DE",    "Deutsche Telekom"),
    ("IBE.MC",    "Iberdrola"),
    ("SAN.PA",    "Sanofi"),
    ("ITX.MC",    "Inditex"),
    ("UCG.MI",    "UniCredit SpA"),
    ("INGA.AS",   "ING Groep"),
    ("BAS.DE",    "BASF SE"),
    ("BMW.DE",    "BMW AG"),
    ("BAYN.DE",   "Bayer AG"),
    ("BBVA.MC",   "BBVA"),
    ("EL.PA",     "EssilorLuxottica"),
    ("RMS.PA",    "Hermès International"),
    ("ISP.MI",    "Intesa Sanpaolo"),
    ("DHL.DE",    "DHL Group"),
    ("ENEL.MI",   "Enel SpA"),
    ("ENI.MI",    "Eni SpA"),
    ("ABI.BR",    "Anheuser-Busch InBev"),
    ("AD.AS",     "Ahold Delhaize"),
    ("ADYEN.AS",  "Adyen NV"),
    ("ADS.DE",    "Adidas AG"),
    ("DG.PA",     "Vinci SA"),
    ("SAF.PA",    "Safran SA"),
    ("RACE.MI",   "Ferrari NV"),
    ("MUV2.DE",   "Munich Re"),
    ("CRH.L",     "CRH Plc"),
    ("FLTR.L",    "Flutter Entertainment"),
    ("BN.PA",     "Danone"),
    ("DB1.DE",    "Deutsche Börse"),
    ("DBK.DE",    "Deutsche Bank"),
    ("IFX.DE",    "Infineon Technologies"),
    ("PRX.AS",    "Prosus NV"),
    ("CS.PA",     "AXA SA"),
    ("KER.PA",    "Kering"),
    ("STLAM.MI",  "Stellantis NV"),
    ("HEIA.AS",   "Heineken NV"),
    ("VOW3.DE",   "Volkswagen Pref"),
    ("ENGI.PA",   "Engie SA"),
    ("NOKIA.HE",  "Nokia Oyj"),
]

LONG_START = "2005-01-01"   # 20+ year history for full backtest window


def seed() -> None:
    print("Seeding disk cache for offline presentation.")
    print(f"Cache directory: {disk_cache._CACHE_DIR}")
    print(f"Tickers to fetch: {len(TICKERS)}\n")

    ok, failed = 0, []
    for ticker, label in TICKERS:
        print(f"  {ticker:<12} {label:<35}", end="", flush=True)
        df = yfinance_loader.fetch_index_history(ticker, start=LONG_START)
        if df.empty:
            print("FAILED — no data returned")
            failed.append(ticker)
            continue
        disk_cache.save(ticker, df)
        print(f"OK  ({len(df)} rows  {df.index[0].date()} → {df.index[-1].date()})")
        ok += 1

    print(f"\n{'=' * 60}")
    print(f"Done: {ok}/{len(TICKERS)} tickers cached.")

    if ok > 0:
        total_kb = sum(e["size_kb"] for e in disk_cache.cache_info())
        print(f"Total cache size: {total_kb:.0f} KB")

    if failed:
        print(f"\nFailed tickers: {', '.join(failed)}")
        print("Re-run to retry, or check your internet connection.")
    else:
        print("\nAll tickers cached. You can now present offline.")


if __name__ == "__main__":
    seed()
