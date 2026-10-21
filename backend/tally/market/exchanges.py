# =============================================================================
# market/exchanges.py: WHICH STOCK MARKET A TICKER TRADES ON
# -----------------------------------------------------------------------------
# For each exchange: its timezone, its daily opening time, and its BENCHMARK (an
# index fund representing "the market" there). We judge ideas against the local
# benchmark: a Singapore stock vs the STI ETF, a US stock vs the S&P 500 ETF.
#
# The ticker suffix tells us the exchange (Yahoo convention):
#   D05.SI -> Singapore, 0700.HK -> Hong Kong, VOD.L -> London, no suffix -> US.
#
# Used by: scoring/marking.py (entry timing), scoring/job.py (benchmark choice),
#          extraction/symbols.py (exchange column for new symbols).
# =============================================================================
"""Which exchange a ticker trades on, its local open time, and its benchmark.

Needed for point-in-time correctness: an idea received at 21:14 Singapore time on a
Wednesday is AFTER the SGX open (09:00 SGT) but BEFORE the NYSE open that day (09:30 New
York = 21:30 SGT). So a US idea gets that night's open; an SGX idea gets tomorrow's.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo     # Python's built-in timezone database (handles daylight saving)


@dataclass(frozen=True)
class Exchange:
    code: str           # "US", "SI", "HK", "L"
    tz: ZoneInfo        # the exchange's local timezone
    open_time: dt.time  # local time the market opens
    benchmark: str      # ticker of the index ETF we compare against


# The lookup table. SPY = S&P 500 ETF; ES3.SI = STI ETF; 2800.HK = Tracker Fund (Hang
# Seng); ISF.L = FTSE 100 ETF.
EXCHANGES = {
    "US": Exchange("US", ZoneInfo("America/New_York"), dt.time(9, 30), "SPY"),
    "SI": Exchange("SI", ZoneInfo("Asia/Singapore"), dt.time(9, 0), "ES3.SI"),
    "HK": Exchange("HK", ZoneInfo("Asia/Hong_Kong"), dt.time(9, 30), "2800.HK"),
    "L": Exchange("L", ZoneInfo("Europe/London"), dt.time(8, 0), "ISF.L"),
}


# "D05.SI" -> the SI Exchange. Anything without a known suffix is treated as US.
def exchange_for(ticker: str) -> Exchange:
    if "." in ticker:
        suffix = ticker.rsplit(".", 1)[1]     # split at the LAST dot: "D05.SI" -> ["D05", "SI"]
        if suffix in EXCHANGES:
            return EXCHANGES[suffix]
    return EXCHANGES["US"]


# Just the code ("SI"), for the symbols table.
def exchange_code(ticker: str) -> str:
    return exchange_for(ticker).code
