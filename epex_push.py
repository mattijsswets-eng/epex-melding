"""Dagelijkse pushmelding (ntfy) voor een warmtepompboiler: welk blok van 8 uur je morgen
moet programmeren. De boiler stookt aan het begin van het blok (RUN uur), dus het blok
begint bij de goedkoopste RUN aaneengesloten uren. Omdat de boiler buitenlucht gebruikt,
wordt de prijs gecorrigeerd voor de COP op basis van de verwachte buitentemperatuur.
Bronnen: Frank Energie (EPEX day-ahead NL, met Energy-Charts als reserve), Open-Meteo (temperatuur)."""
import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import sys

sys.stdout.reconfigure(line_buffering=True)  # logregels direct tonen in GitHub
_opener = urllib.request.build_opener()
_opener.addheaders = [("User-Agent", "Mozilla/5.0 (epex-melding)")]  # sommige sites weigeren Python
urllib.request.install_opener(_opener)

TZ = ZoneInfo("Europe/Amsterdam")
HOURS = 8   # lengte van het blok dat de app eist
RUN = int(os.environ.get("RUN_HOURS", "4"))  # uren dat de boiler echt stookt
TOPIC = os.environ["NTFY_TOPIC"]
DAY_OFFSET = int(os.environ.get("DAY_OFFSET", "1"))  # 1 = morgen, 0 = vandaag (om te testen)
LAT, LON = 51.92, 4.48   # Rotterdam
COP_PER_GRAAD = float(os.environ.get("COP_PER_GRAAD", "0.025"))  # COP stijgt ~2,5% per graad
# Vaste opslag per kWh incl. btw (energiebelasting + inkoopvergoeding leverancier).
# Telt mee omdat je die ook betaalt per kWh, en een betere COP bespaart dus ook daarop.
OPSLAG = float(os.environ.get("OPSLAG_CT", "13"))
# Verwarmingsadvies op basis van de all-in daggemiddelde prijs (ct/kWh, incl. belasting)
GOEDKOOP = float(os.environ.get("GOEDKOOP_CT", "22"))
DUUR = float(os.environ.get("DUUR_CT", "35"))
DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]


def _to_hourly(points, s0, s1):
    """points: lijst (unix_ts, prijs in EUR/MWh). Middelt kwartieren naar uren; None als de dag niet compleet is."""
    buckets = defaultdict(list)
    for ts, p in points:
        if p is not None and s0 <= ts < s1:
            buckets[ts - ts % 3600].append(p)
    expected = (s1 - s0) // 3600  # 23, 24 of 25 bij zomer/wintertijd
    hours = sorted(buckets)
    if len(hours) < expected:
        print(f"Onvolledig: {len(hours)} van {expected} uur")
        return None
    return [(h, sum(buckets[h]) / len(buckets[h])) for h in hours]


FRANK_URLS = ["https://graphql.frankenergie.nl/", "https://www.frankenergie.nl/graphql",
              "https://frank-graphql-prod.graphcdn.app/"]


def fetch_frank(start, end):
    """Zelfde bron als de Frank-app (nieuwe API sinds aug 2026). marketPrice is EUR/kWh excl. btw."""
    query = """query MarketPrices($date: String!, $resolution: PriceResolution!) {
      marketPrices(date: $date, resolution: $resolution) {
        electricityPrices { from till marketPrice } } }"""
    payload = json.dumps({"query": query, "operationName": "MarketPrices", "variables": {
        "date": start.date().isoformat(), "resolution": "PT15M"}}).encode()
    fouten = []
    for url in FRANK_URLS:
        try:
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.load(r)
            data = (resp.get("data") or {}).get("marketPrices") or {}
            rows = data.get("electricityPrices") or []
            if not rows:
                fouten.append(f"{url}: {resp.get('errors') or 'geen prijzen'}")
                continue
            factor = 1 if max(abs(x["marketPrice"]) for x in rows) > 5 else 1000  # naar EUR/MWh
            return [(int(datetime.fromisoformat(x["from"].replace("Z", "+00:00")).timestamp()),
                     x["marketPrice"] * factor) for x in rows]
        except Exception as e:
            fouten.append(f"{url}: {e}")
    raise RuntimeError(" | ".join(fouten))


def fetch_energycharts(start, end):
    q = urllib.parse.urlencode({"bzn": "NL", "start": start.isoformat(), "end": end.isoformat()})
    with urllib.request.urlopen(f"https://api.energy-charts.info/price?{q}", timeout=30) as r:
        data = json.load(r)
    return list(zip(data.get("unix_seconds", []), data.get("price", [])))


def fetch_hourly(day):
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    end = start + timedelta(days=1)
    s0, s1 = int(start.timestamp()), int(end.timestamp())
    for name, fn in (("Frank", fetch_frank), ("Energy-Charts", fetch_energycharts)):
        try:
            hourly = _to_hourly(fn(start, end), s0, s1)
            if hourly:
                print("Prijzen via", name)
                return hourly
        except Exception as e:
            print(f"{name} mislukt:", e)
    return None


def fetch_temps(day, hours_ts):
    """Verwachte buitentemperatuur per uur; None als het ophalen mislukt."""
    q = urllib.parse.urlencode({"latitude": LAT, "longitude": LON, "hourly": "temperature_2m",
                                "timeformat": "unixtime", "timezone": "Europe/Amsterdam",
                                "start_date": day.isoformat(), "end_date": day.isoformat()})
    try:
        with urllib.request.urlopen(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=30) as r:
            h = json.load(r)["hourly"]
        t = dict(zip(h["time"], h["temperature_2m"]))
        temps = [t.get(ts) for ts in hours_ts]
        return None if None in temps else temps
    except Exception as e:
        print("Temperatuur ophalen mislukt:", e)
        return None


def cop_factor(temp):
    """Relatieve COP t.o.v. 7 graden. Alleen de verhouding telt voor de keuze."""
    return max(0.5, 1 + COP_PER_GRAAD * (temp - 7))


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
        temps = fetch_temps(day, ts)
        if temps:
            eff = [(p * 1.21 + OPSLAG) / cop_factor(t) for p, t in zip(ct, temps)]  # all-in per eenheid warmte
        else:
            eff = ct
        i = best_start(eff)
        j = best_start(ct)  # keuze zonder COP-correctie, ter vergelijking
        run_avg = sum(ct[i:i + RUN]) / RUN
        lines = [
            f"Programmeer: {fmt(ts[i])}-{fmt(ts[i + HOURS - 1] + 3600)}",
            f"Boiler stookt ca. {fmt(ts[i])}-{fmt(ts[i + RUN - 1] + 3600)}, gem {run_avg:.1f} ct",
        ]
        if temps:
            lines.append(f"Buitentemp tijdens stoken: gem {sum(temps[i:i + RUN]) / RUN:.0f} °C")
            lines.append(f"Buitentemp etmaal: gem {sum(temps) / len(temps):.0f} °C")
            if j != i:
                lines.append(f"(Alleen op prijs was {fmt(ts[j])} gekozen; door COP nu {fmt(ts[i])})")
        else:
            lines.append("Geen temperatuurdata, gekozen op prijs alleen")
        lines.append(f"Daggemiddelde: {sum(ct) / len(ct):.1f} ct")
        lines.append("(kale EPEX-prijs, excl. belasting)")
        # Verwarmingsadvies
        allin = [p * 1.21 + OPSLAG for p in ct]
        dag = sum(allin) / len(allin)
        if dag < GOEDKOOP:
            advies = "goedkope dag, ruim stoken mag"
        elif dag > DUUR:
            advies = "dure dag, thermostaat 1 graad lager"
        else:
            advies = "normale dag"
        d = max(range(len(ct) - 2), key=lambda k: sum(ct[k:k + 3]))
        lines.append(f"Verwarming: {advies} (all-in gem {dag:.0f} ct)")
        lines.append(f"Duurste uren: {fmt(ts[d])}-{fmt(ts[d + 2] + 3600)}, airco dan lager")
        body = "\n".join(lines)
        title = f"Boiler {DAGEN[day.weekday()]} {day.day}-{day.month}"

    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}", data=body.encode(),
                                 headers={"Title": title, "Tags": "zap"})
    urllib.request.urlopen(req, timeout=30)
    print(title, "\n", body)


if __name__ == "__main__":
    main()
