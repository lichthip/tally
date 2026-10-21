"""Which exchange a ticker trades on, its local open time, and its benchmark.

Needed for point-in-time correctness: an idea received at 21:14 Singapore time on a
Wednesday is AFTER the SGX open (09:00 SGT) but BEFORE the NYSE open that day (09:30 New
York = 21:30 SGT). So a US idea gets that night's open; an SGX idea gets tomorrow's.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Exchange:
    code: str
    tz: ZoneInfo
    open_time: dt.time
    benchmark: str


EXCHANGES = {
    "US": Exchange("US", ZoneInfo("America/New_York"), dt.time(9, 30), "SPY"),
    "SI": Exchange("SI", ZoneInfo("Asia/Singapore"), dt.time(9, 0), "ES3.SI"),
    "HK": Exchange("HK", ZoneInfo("Asia/Hong_Kong"), dt.time(9, 30), "2800.HK"),
    "L": Exchange("L", ZoneInfo("Europe/London"), dt.time(8, 0), "ISF.L"),
}


def exchange_for(ticker: str) -> Exchange:
    if "." in ticker:
        suffix = ticker.rsplit(".", 1)[1]
        if suffix in EXCHANGES:
            return EXCHANGES[suffix]
    return EXCHANGES["US"]


def exchange_code(ticker: str) -> str:
    return exchange_for(ticker).code
