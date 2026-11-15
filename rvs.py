"""Raad van State: uitspraken die nog niet op rechtspraak.nl (en dus niet in Drive map 04) staan,
en alle gepubliceerde adviezen (wetgevingsadviezen en voorlichtingen).

Route: de eigen zoekpagina's van raadvanstate.nl (/uitspraken/ en /adviezen/), met
pager_rows=1000 (128 + 13 lijstpagina's) en daarna per stuk de gewone detailpagina met de volledige
tekst. Er is geen open-data-export en geen JSON-API. robots.txt: alleen /aspx/ en export=pdf zijn verboden; die komen hier niet voor (we halen de html,
niet de pdf-export). robots.txt vraagt crawl-delay 30 en dat wordt gerespecteerd: een verbinding,
30 s tussen twee verzoeken (ook bij de lijstpagina's), een job tegelijk voor de hele site, en direct
stoppen bij 15 weigeringen (403/429) op rij. Shards van 500 pagina's; de workflow loopt via een
schedule tot alles binnen is. Volgorde: eerst de adviezen, dan de ontbrekende uitspraken.

Ontdubbelen: uitspraken met een ECLI die data.rechtspraak.nl (creator Raad van State) al kent zijn
al in Drive map 04 en worden niet opnieuw opgehaald. Adviezen staan in zijn geheel hier; de
Kamerstukken (kst.py) bevatten een deel van dezelfde adviezen als bijlage bij wetsvoorstellen, maar
onder een ander kenmerk, dus die zijn niet betrouwbaar te ontdubbelen.

  python rvs.py prep                  -> rvs_plan/ + shards die nog open zijn
  python rvs.py run <shard>  (rvs-uit000, rvs-adv000) [--max N] [--droog]
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN, optioneel TIJDGRENS_MIN (300), RVS_PAUZE (30)
"""
import csv
import gzip
import hashlib
import html as H
import io
import json
import os
import re
import sys
import time
import zipfile

import requests

import kst
from gaten import Drive

BASIS = "https://www.raadvanstate.nl"
PLAN = "rvs_plan"
PER_SHARD = 500          # bij 30 s pauze ongeveer 4,3 uur per shard
PAUZE = float(os.environ.get("RVS_PAUZE", "30"))
START = time.time()
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "300")) * 60
MAANDEN = {m: i for i, m in enumerate(
    "januari februari maart april mei juni juli augustus september oktober november december".split(), 1)}
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (regelgeving-archief; open data)"})


def get(url, params=None, timeout=90, pogingen=5):
    for i in range(pogingen):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code in (200, 404):
                return r
            if r.status_code in (403, 429):
                time.sleep(30 * (i + 1))
                continue
        except requests.RequestException:
            pass
        time.sleep(5 * (i + 1))
    return None


def iso(tekst):
    m = re.search(r"(\d{1,2}) (" + "|".join(MAANDEN) + r") (\d{4})", tekst or "")
    return f"{m.group(3)}-{MAANDEN[m.group(2)]:02d}-{int(m.group(1)):02d}" if m else ""


def plat(b):
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", "|", b)))


def lijst(soort):
    """Alle items van /uitspraken/ of /adviezen/ (id, slug, ecli, datum, kenmerk, titel)."""
    uit, p = [], 0   # pager_page is 0-gebaseerd: de eerste pagina is 0
    while True:
        r = get(f"{BASIS}/{soort}/", {"pager_rows": 1000, "pager_page": p})
        if r is None or r.status_code != 200:
            raise RuntimeError(f"lijstpagina {soort} {p} niet op te halen")
        blokken = re.split(r'(?=<div class="grid-blok[^"]*rol-entry)', r.text)[1:]
        if not blokken:
            break
        for b in blokken:
            m = re.search(r"/" + soort + r"/(@\d+)/([^/\"]+)/", b)
            if not m:
                continue
            t = plat(b)
            e = re.search(r"ECLI[ |]+(ECLI:[A-Za-z]+:[A-Za-z0-9]+:\d{4}:[A-Za-z0-9.]+)", t)
            k = re.search(r"Kenmerk[ |]+([A-Z0-9./-]+)", t)
            d = re.search(r"Datum (?:uitspraak|advies)[ |]+(\d{1,2} \w+ \d{4})", t)
            uit.append({"id": m.group(1), "slug": m.group(2), "soort": soort,
                        "url": f"{BASIS}/{soort}/{m.group(1)}/{m.group(2)}/",
                        "ecli": e.group(1) if e else "", "kenmerk": k.group(1) if k else m.group(2),
                        "datum": iso(d.group(1)) if d else "", "heeft_datum": bool(d)})
        print(f"  {soort} pagina {p}: {len(uit)}", flush=True)
        p += 1
        time.sleep(PAUZE)
        if len(blokken) < 1000:
            break
    return uit


def rechtspraak_rvs():
    """ECLI's van de Raad van State op data.rechtspraak.nl (= al in Drive map 04)."""
    ids = set()
    for jaar in range(1900, int(time.strftime("%Y")) + 1):
        start = 0
        while True:
            r = None
            for i in range(5):
                try:
                    r = S.get("https://data.rechtspraak.nl/uitspraken/zoeken", timeout=90, params=[
                        ("creator", "http://standaarden.overheid.nl/owms/terms/Raad_van_State"),
                        ("date", f"{jaar}-01-01"), ("date", f"{jaar}-12-31"),
                        ("max", 1000), ("from", start), ("return", "DOC")])
                    if r.status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(5 * (i + 1))
            else:
                raise RuntimeError(f"data.rechtspraak.nl faalt voor {jaar}")
            e = re.findall(r"<id>(ECLI:[^<]+)</id>", r.text)
            ids.update(e)
            if len(e) < 1000:
                break
            start += 1000
    return ids


def zipnaam(shard):
    return f"RvS_{shard.replace('rvs-', '').replace('uit', 'uitspraken_').replace('adv', 'adviezen_')}.zip"


def mapsleutel(shard):
    return "07" if shard.startswith("rvs-adv") else "04"


PLANNAAM = "_rvs_plan.json.gz"


def plan_uit_drive(d, mappen):
    """Hergebruik het plan van een eerdere prep (max 14 dagen oud): scheelt ~1,5 uur lijstpagina's."""
    fid = d.zoek(PLANNAAM, mappen["00"])
    if not fid:
        return False
    d.download(fid, PLANNAAM)
    with gzip.open(PLANNAAM, "rt", encoding="utf8") as f:
        p = json.load(f)
    os.remove(PLANNAAM)
    if time.time() - p["gemaakt"] > 14 * 86400:
        return False
    os.makedirs(PLAN, exist_ok=True)
    for f in os.listdir(PLAN):
        os.remove(os.path.join(PLAN, f))
    for naam, items in p["shards"].items():
        with open(f"{PLAN}/{naam}.json", "w", encoding="utf8") as f:
            json.dump(items, f)
    with open(f"{PLAN}/_tellingen.json", "w") as f:
        json.dump(p["tellingen"], f)
    print(f"plan hergebruikt uit Drive: {len(p['shards'])} shards, {p['tellingen']}", flush=True)
    return True


def prep():
    keuze = [s.strip() for s in os.environ.get("SHARDS", "").split(",") if s.strip()]
    d = mappen = None
    if not keuze:
        d = Drive()
        mappen, _ = d.mappen()
        if plan_uit_drive(d, mappen):
            return kies_open(d, mappen, keuze)
    bekend = rechtspraak_rvs()
    print(f"{len(bekend)} RvS-ECLI's op data.rechtspraak.nl", flush=True)
    uits = lijst("uitspraken")
    adv = lijst("adviezen")
    dubbel = [x for x in uits if x["ecli"] and x["ecli"] in bekend]
    nieuw_u = [x for x in uits if not x["ecli"] or x["ecli"] not in bekend]
    adv_tekst = [x for x in adv if x["heeft_datum"]]
    print(f"uitspraken op site {len(uits)}: al op rechtspraak.nl {len(dubbel)}, ontbrekend {len(nieuw_u)}")
    print(f"adviezen op site {len(adv)}: met gepubliceerde tekst {len(adv_tekst)}, nog aanhangig {len(adv) - len(adv_tekst)}")
    uniek = {}
    for x in nieuw_u + adv_tekst:
        uniek[x["soort"] + x["id"]] = x
    alle = sorted(uniek.values(), key=lambda x: (x["soort"], x["datum"], x["id"]))
    os.makedirs(PLAN, exist_ok=True)
    for f in os.listdir(PLAN):
        os.remove(os.path.join(PLAN, f))
    n = 0
    for soort, pre in (("adviezen", "adv"), ("uitspraken", "uit")):
        deel = [x for x in alle if x["soort"] == soort]
        for i in range((len(deel) + PER_SHARD - 1) // PER_SHARD):
            with open(f"{PLAN}/rvs-{pre}{i:03d}.json", "w", encoding="utf8") as f:
                json.dump(deel[i * PER_SHARD:(i + 1) * PER_SHARD], f)
            n += 1
    with open(f"{PLAN}/_tellingen.json", "w") as f:
        json.dump({"uitspraken_site": len(uits), "al_op_rechtspraak": len(dubbel),
                   "uitspraken_ontbrekend": len(nieuw_u), "adviezen_site": len(adv),
                   "adviezen_met_tekst": len(adv_tekst)}, f)
    print(f"{len(alle)} stukken te halen, {n} shards", flush=True)
    if d is not None:
        shards = {f[:-5]: json.load(open(f"{PLAN}/{f}", encoding="utf8"))
                  for f in os.listdir(PLAN) if f.startswith("rvs-")}
        with gzip.open(PLANNAAM, "wt", encoding="utf8") as f:
            json.dump({"gemaakt": time.time(), "shards": shards,
                       "tellingen": json.load(open(f"{PLAN}/_tellingen.json"))}, f)
        d.upload(PLANNAAM, mappen["00"])
        os.remove(PLANNAAM)
    kies_open(d, mappen, keuze)


def kies_open(d, mappen, keuze):
    if keuze:
        open_ = keuze
    else:
        open_ = []
        for s in sorted((f[:-5] for f in os.listdir(PLAN) if f.startswith("rvs-")),
                        key=lambda n: (n.startswith("rvs-uit"), n)):
            if not d.zoek(zipnaam(s), mappen[mapsleutel(s)]):
                open_.append(s)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"nog te doen: {len(open_)}", flush=True)


def titel_van(tekst):
    m = re.search(r"<title>([^<]*)</title>", tekst)
    return re.sub(r"\s+", " ", H.unescape(m.group(1))).strip() if m else ""


def run(shard, max_n, droog):
    with open(f"{PLAN}/{shard}.json", encoding="utf8") as f:
        items = json.load(f)
    if max_n:
        items = items[:max_n]
    print(f"{shard}: {len(items)} stukken", flush=True)
    zn = zipnaam(shard)
    rijen, mislukt, weigeringen = [], [], 0
    ok_u = ok_a = 0
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for k, x in enumerate(items, 1):
            if time.time() - START > GRENS_SEC:
                mislukt.append(f"{x['url']}: tijd: niet meer aan begonnen binnen de tijdgrens van de job")
                continue
            if weigeringen >= 15:
                mislukt.append(f"{x['url']}: geweigerd: aanhoudende 403/429, gestopt om de site te ontzien")
                continue
            r = get(x["url"])
            time.sleep(PAUZE)
            if r is None:
                weigeringen += 1
                mislukt.append(f"{x['url']}: geweigerd of onbereikbaar")
                continue
            weigeringen = 0
            data = r.content
            if r.status_code == 404 or len(data) < 3000:
                mislukt.append(f"{x['url']}: HTTP {r.status_code}, {len(data)} bytes")
                continue
            tekst = data.decode("utf8", "replace")
            ident = x["ecli"] or x["kenmerk"]
            if ident not in tekst and x["slug"] not in tekst:
                mislukt.append(f"{x['url']}: pagina noemt het eigen kenmerk niet")
                continue
            naam = f"{x['soort']}/{re.sub(r'[^A-Za-z0-9._-]', '_', ident)}_{x['id'][1:]}.html"
            z.writestr(naam, data)
            if x["soort"] == "uitspraken":
                ok_u += 1
            else:
                ok_a += 1
            rijen.append([x["soort"], ident, "Raad van State", x["datum"], titel_van(tekst),
                          x["url"], len(data), hashlib.sha256(data).hexdigest()])
            if k % 200 == 0:
                print(f"  {k}/{len(items)}", flush=True)
        z.writestr("_OVERZICHT.txt",
                   f"Raad van State, {shard}\nVerzameld: {time.strftime('%Y-%m-%d')}\n"
                   f"Uitspraken (niet op rechtspraak.nl): {ok_u}\nAdviezen: {ok_a}\nMislukt: {len(mislukt)}\n"
                   "Elke pagina is de ongewijzigde html van raadvanstate.nl met de volledige tekst;\n"
                   "sha256 staat in de index. De pdf-export wordt door robots.txt uitgesloten.\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))
    print(f"{zn}: uitspraken {ok_u}, adviezen {ok_a}, mislukt {len(mislukt)}", flush=True)
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(["soort", "id", "instantie", "datum", "titel", "url", "bytes", "sha256"])
    w.writerows(rijen)
    idx = zn + ".index.csv.gz"
    with gzip.open(idx, "wt", encoding="utf8", newline="") as f:
        f.write(s.getvalue())
    if droog:
        print(f"DROOG: {zn} {os.path.getsize(zn)/1024/1024:.1f} MB -> map {mapsleutel(shard)}")
        return
    d = Drive()
    mappen, _ = d.mappen()
    d.upload(zn, mappen[mapsleutel(shard)])
    os.remove(zn)
    d.upload(idx, mappen["00"])
    os.remove(idx)
    pad = kst.meld_mislukt(zn, mislukt)
    if pad:
        d.upload(pad, mappen["00"])
        os.remove(pad)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "prep":
        prep()
    elif a[0] == "run":
        run(a[1], int(a[a.index("--max") + 1]) if "--max" in a else 0, "--droog" in a)
