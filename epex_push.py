"""Dagelijkse pushmelding (ntfy) voor een warmtepompboiler: welk blok van 8 uur je morgen
moet programmeren. De boiler stookt aan het begin van het blok (RUN uur), dus het blok
begint bij de goedkoopste RUN aaneengesloten uren. Bron: Energy-Charts (EPEX day-ahead NL)."""
import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
HOURS = 8   # lengte van het blok dat de app eist
RUN = int(os.environ.get("RUN_HOURS", "4"))  # uren dat de boiler echt stookt
TOPIC = os.environ["NTFY_TOPIC"]
DAY_OFFSET = int(os.environ.get("DAY_OFFSET", "1"))  # 1 = morgen, 0 = vandaag (om te testen)
DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]


def fetch_hourly(day):
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    end = start + timedelta(days=1)
    q = urllib.parse.urlencode({"bzn": "NL", "start": start.isoformat(), "end": end.isoformat()})
    with urllib.request.urlopen(f"https://api.energy-charts.info/price?{q}", timeout=30) as r:
        data = json.load(r)
    s0, s1 = int(start.timestamp()), int(end.timestamp())
    buckets = defaultdict(list)
    for ts, p in zip(data.get("unix_seconds", []), data.get("price", [])):
        if p is not None and s0 <= ts < s1:
            buckets[ts - ts % 3600].append(p)
    expected = (s1 - s0) // 3600  # 23, 24 of 25 bij zomer/wintertijd
    hours = sorted(buckets)
    if len(hours) < expected:
        return None
    return [(h, sum(buckets[h]) / len(buckets[h])) for h in hours]


def best_start(prices):
    """Startuur waarbij de eerste RUN uren het goedkoopst zijn en het blok van 8 nog in de dag past."""
    n = len(prices)
    return min(range(n - HOURS + 1), key=lambda i: sum(prices[i:i + RUN]))


def fmt(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


def main():
    day = datetime.now(TZ).date() + timedelta(days=DAY_OFFSET)
    hourly = None
    for attempt in range(13):  # max ~1 uur wachten op publicatie
        try:
            hourly = fetch_hourly(day)
        except Exception as e:
            print("Ophalen mislukt:", e)
        if hourly:
            break
        time.sleep(300)

    if not hourly:
        body, title = "Prijzen voor morgen nog niet beschikbaar. Check later zelf.", "EPEX: geen data"
    else:
        ts = [h for h, _ in hourly]
        ct = [p / 10 for _, p in hourly]  # EUR/MWh -> ct/kWh
        i = best_start(ct)
        run_avg = sum(ct[i:i + RUN]) / RUN
        lines = [
            f"Programmeer: {fmt(ts[i])}-{fmt(ts[i + HOURS - 1] + 3600)}",
            f"Boiler stookt ca. {fmt(ts[i])}-{fmt(ts[i + RUN - 1] + 3600)}, gem {run_avg:.1f} ct",
            f"Daggemiddelde: {sum(ct) / len(ct):.1f} ct",
        ]
        lines.append("(kale EPEX-prijs, excl. belasting)")
        body = "\n".join(lines)
        title = f"Boiler {DAGEN[day.weekday()]} {day.day}-{day.month}"

    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}", data=body.encode(),
                                 headers={"Title": title, "Tags": "zap"})
    urllib.request.urlopen(req, timeout=30)
    print(title, "\n", body)


if __name__ == "__main__":
    main()
