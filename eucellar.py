"""EU-aanvulling via Cellar (EU Publications Office): wat in map 01 nog ontbreekt.

Map 01 heeft de geldende EU-wetgeving (sector 3) en de uitspraken van het Hof
(sector 6). Hier komt bij, in het Nederlands en ongewijzigd zoals Cellar het geeft:

  wet  - EU-wetgeving die niet meer geldt (ingetrokken, vervallen, verlopen; sector 3
         zonder cdm:resource_legal_in-force true)
  com  - voorbereidende handelingen (sector 5: COM-voorstellen, mededelingen, SEC, JOIN ...)
  ag   - conclusies van de advocaten-generaal (sector 6, type CC) die nog niet in de
         Hof-zips van map 01 zitten

Per CELEX-nummer eerst de Nederlandse xhtml; bestaat die niet, dan de Nederlandse pdf.
Bestaat ook die niet, dan is er geen Nederlandse versie: dat is geen fout en komt als
'geen_nl' in de index. Alleen echte netwerkfouten gaan naar .mislukt.csv.

  python eucellar.py prep                 -> eucellar_plan/ + shards die nog open zijn
  python eucellar.py run <shard> [--max N] [--droog]
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN
"""
import concurrent.futures as cf
import csv
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile

import requests

from gaten import Drive, CELLAR, SPARQL, veilig, UA
from kst import meld_mislukt

PLAN = "eucellar_plan"
PER_SHARD = 12000
THREADS = 6
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "200")) * 60   # job-limiet GitHub is 6 uur
START = time.time()
P = "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> "
S = requests.Session()
S.headers.update(UA)
SOORTEN = {"wet": "EU_vervallen_wetgeving", "com": "EU_voorbereidend", "ag": "EU_conclusies_AG"}
EIGEN = ("EU_vervallen", "EU_voorbereidend", "EU_conclusies")


def sparql(q):
    for i in range(5):
        try:
            r = S.post(SPARQL, data={"query": q}, timeout=900,
                       headers={"Accept": "application/sparql-results+json"})
            if r.status_code == 200:
                return {b["c"]["value"] for b in r.json()["results"]["bindings"]}
        except (requests.RequestException, KeyError, ValueError):
            pass
        time.sleep(20 * (i + 1))
    raise RuntimeError("SPARQL faalt")


def lijst(soort):
    basis = "?w cdm:resource_legal_id_celex ?c . "
    if soort == "wet":
        alle = sparql(P + "SELECT DISTINCT ?c WHERE { " + basis + 'FILTER(regex(str(?c), "^3[0-9]{4}")) }')
        geldig = sparql(P + "SELECT DISTINCT ?c WHERE { " + basis + "?w cdm:resource_legal_in-force true . "
                        'FILTER(regex(str(?c), "^3[0-9]{4}")) }')
        return sorted(alle - geldig)
    if soort == "com":
        return sorted(sparql(P + "SELECT DISTINCT ?c WHERE { " + basis + 'FILTER(regex(str(?c), "^5[0-9]{4}")) }'))
    return sorted(sparql(P + "SELECT DISTINCT ?c WHERE { " + basis + 'FILTER(regex(str(?c), "^6[0-9]{4}CC")) }'))


def bestaande_ids(d, mappen):
    """Veilige bestandsnamen (zonder extensie) die al in een EU_-zip van map 01 staan."""
    r = requests.get("https://www.googleapis.com/drive/v3/files", headers=d.h, timeout=60,
                     params={"q": f"'{mappen['00']}' in parents and trashed = false and name contains 'EU_' "
                                  "and name contains '.index.csv.gz'",
                             "fields": "files(id,name)", "pageSize": 1000})
    r.raise_for_status()
    ids = set()
    for f in r.json()["files"]:
        if not f["name"].startswith("EU_") or f["name"].startswith(EIGEN):
            continue
        d.download(f["id"], "_idx.gz")
        with gzip.open("_idx.gz", "rt", encoding="utf8", newline="") as fh:
            for rij in csv.DictReader(fh):
                ids.add(re.sub(r"\.(xhtml|xml|pdf)$", "", rij.get("bestand", "")))
        os.remove("_idx.gz")
    return ids


def shardnaam(soort, i):
    return f"{soort}-{i:03d}"


def zipnaam(shard):
    soort, nr = shard.split("-")
    return f"{SOORTEN[soort]}_deel{nr}.zip"


def prep():
    d = Drive()
    mappen, _ = d.mappen()
    bestaand = bestaande_ids(d, mappen)
    print(f"al in map 01/00: {len(bestaand)} documenten", flush=True)
    if os.path.isdir(PLAN):
        for f in os.listdir(PLAN):
            os.remove(os.path.join(PLAN, f))
    os.makedirs(PLAN, exist_ok=True)
    alle_shards = []
    for soort in SOORTEN:
        totaal = lijst(soort)
        nieuw = [c for c in totaal if veilig(c) not in bestaand]
        print(f"{soort}: {len(totaal)} in Cellar, {len(totaal) - len(nieuw)} al aanwezig, {len(nieuw)} te doen",
              flush=True)
        for i in range((len(nieuw) + PER_SHARD - 1) // PER_SHARD):
            naam = shardnaam(soort, i)
            with open(os.path.join(PLAN, naam + ".json"), "w") as f:
                json.dump(nieuw[i * PER_SHARD:(i + 1) * PER_SHARD], f)
            alle_shards.append(naam)
    keuze = [s.strip() for s in os.environ.get("SHARDS", "").split(",") if s.strip()]
    open_ = keuze or [s for s in alle_shards if not d.zoek(zipnaam(s), mappen["01"])]
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"{len(alle_shards)} shards, nog te doen {len(open_)}")


def een(celex):
    """(soort, bytes, fout): soort xhtml|pdf|geen_nl|fout."""
    pad = requests.utils.quote(celex, safe="")
    if time.time() - START > GRENS_SEC:
        return "fout", None, "tijd: niet meer aan begonnen binnen de tijdgrens van de job"
    for acc, soort in (("application/xhtml+xml", "xhtml"), ("application/pdf", "pdf")):
        vorm = "application/xhtml+xml" if soort == "xhtml" else "application/pdf"
        for i in range(4):
            try:
                r = S.get(CELLAR + pad, timeout=120, headers={"Accept": acc, "Accept-Language": "nld"})
            except requests.RequestException as e:
                laatste = type(e).__name__
                time.sleep(5 * (i + 1) ** 2)
                continue
            if r.status_code == 200 and len(r.content) > 800 and r.headers.get("Content-Type", "").startswith(vorm):
                if soort == "pdf" and not r.content.startswith(b"%PDF-"):
                    return "fout", None, "pdf zonder kop"
                return soort, r.content, ""
            if r.status_code in (200, 404, 406, 300):
                break           # er is geen versie in deze vorm; volgende vorm proberen
            laatste = f"HTTP {r.status_code}"
            time.sleep(5 * (i + 1) ** 2)
        else:
            return "fout", None, f"{soort}: {laatste}"
    return "geen_nl", None, ""


def run(shard, max_n, droog):
    with open(os.path.join(PLAN, shard + ".json")) as f:
        celex = json.load(f)
    if max_n:
        celex = celex[:max_n]
    zn = zipnaam(shard)
    print(f"{shard}: {len(celex)} CELEX-nummers", flush=True)
    rijen, mislukt = [], []
    n = {"xhtml": 0, "pdf": 0, "geen_nl": 0}
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            for k, (c, (soort, data, fout)) in enumerate(zip(celex, ex.map(een, celex)), 1):
                if soort == "fout":
                    mislukt.append(f"{c}: {fout}")
                    continue
                n[soort] += 1
                naam = ""
                if data:
                    naam = veilig(c) + "." + soort
                    z.writestr(naam, data)
                rijen.append([shard, naam, c, soort, len(data or b""),
                              hashlib.sha256(data).hexdigest() if data else ""])
                if k % 2000 == 0:
                    print(f"  {k}/{len(celex)}", flush=True)
        z.writestr("_OVERZICHT.txt",
                   f"EU-aanvulling via Cellar ({SOORTEN[shard.split('-')[0]]})\nShard: {shard}\n"
                   f"Verzameld: {time.strftime('%Y-%m-%d')}\nCELEX-nummers: {len(celex)}\n"
                   f"Nederlandse xhtml: {n['xhtml']}\nNederlandse pdf: {n['pdf']}\n"
                   f"Geen Nederlandse versie in Cellar: {n['geen_nl']}\nMislukt: {len(mislukt)}\n"
                   "Bron: https://publications.europa.eu/resource/celex/<CELEX> met Accept-Language nld.\n"
                   "Bestanden ongewijzigd; sha256 in de index.\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))
    print(f"{zn}: {n} mislukt {len(mislukt)}", flush=True)
    rij = io.StringIO()
    w = csv.writer(rij)
    w.writerow(["shard", "bestand", "celex", "soort", "bytes", "sha256"])
    w.writerows(rijen)
    idx = zn + ".index.csv.gz"
    with gzip.open(idx, "wt", encoding="utf8", newline="") as f:
        f.write(rij.getvalue())
    if droog:
        print(f"DROOG: {zn} {os.path.getsize(zn)/1024/1024:.1f} MB -> map 01")
        return
    d = Drive()
    mappen, _ = d.mappen()
    d.upload(zn, mappen["01"])
    os.remove(zn)
    d.upload(idx, mappen["00"])
    os.remove(idx)
    pad = meld_mislukt(zn, mislukt)
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
