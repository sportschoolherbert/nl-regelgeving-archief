"""Parlementaire geschiedenis na 1994: Handelingen, Aanhangsel van de Handelingen
(Kamervragen met antwoord), Kamervragen zonder antwoord en de overige
Staten-Generaal-publicaties die niet onder 'Kamerstuk' vallen (Niet-dossierstuk,
Agenda).

Bron: de SRU van repository.overheid.nl, dezelfde index als officielebekendmakingen.nl.
Van elk stuk bewaren we de xml (gestructureerde tekst) en de pdf, ongewijzigd, met
sha256 in de index. Alles met publicatienaam 'Kamerstuk' zit al in kst.py en wordt
hier bewust niet opnieuw gehaald (andere publicatienaam, dus per definitie
disjunct; prep controleert dat geen id met 'kst-' begint).

  python handelingen.py prep               -> parlement_plan/ + shards die nog open zijn
  python handelingen.py run <shard> [--max N] [--droog]     (shard = h-000, aa-003, ...)
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN, TIJDGRENS_MIN (standaard 200)
"""
import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import sys
import time
import zipfile

from kst import haal, tel, pagina, verdeel, controleer_xml, controleer_pdf, meld_mislukt
from gaten import Drive

MAP = "07"
PER_SHARD = 5000
PLAN = "parlement_plan"
THREADS = 6
SETS = {
    "h": ("Handelingen", ['w.publicatienaam=="Handelingen"']),
    "aa": ("Aanhangsel", ['w.publicatienaam=="Kamervragen (Aanhangsel)"']),
    "kz": ("KamervragenZA", ['w.publicatienaam=="Kamervragen zonder antwoord"']),
    "ov": ("OverigParlement", ['w.organisatietype=="staten generaal" and w.publicatienaam=="Niet-dossierstuk"',
                                'w.organisatietype=="staten generaal" and w.publicatienaam=="Agenda"']),
}
START = time.time()
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "200")) * 60


def een(pat, tekst):
    m = re.search(pat, tekst, re.S)
    return m.group(1).strip() if m else ""


def parse(blok):
    ident = een(r"<dcterms:identifier>([^<]+)<", blok)
    urls = dict(re.findall(r'<gzd:itemUrl manifestation="([^"]+)">([^<]+)</gzd:itemUrl>', blok))
    maker = een(r"<dcterms:creator[^>]*>([^<]*)<", blok)
    kamer = "TK" if "Tweede" in maker else "EK" if "Eerste" in maker else "VV" if "Verenigde" in maker else maker
    datum = (een(r"<overheidwetgeving:datumVergadering>([^<]*)<", blok) or
             een(r"<dcterms:date>([^<]*)<", blok) or een(r"<dcterms:available>([^<]*)<", blok))
    versie = ""
    m = re.search(r"/" + re.escape(ident) + r"/(\d+)/", urls.get("xml", "") or urls.get("pdf", ""))
    if m:
        versie = m.group(1)
    return {"id": ident, "kamer": kamer, "vergaderjaar": een(r"<overheidwetgeving:vergaderjaar>([^<]*)<", blok),
            "dossier": een(r"<overheidwetgeving:dossiernummer>([^<]*)<", blok), "datum": datum,
            "titel": een(r"<dcterms:title>([^<]*)<", blok), "versie": versie,
            "xml": urls.get("xml", ""), "pdf": urls.get("pdf", "")}


def records(query, verwacht):
    start, uit = 1, []
    while True:
        tekst = pagina(query, start)
        blokken = re.findall(r"<sru:record>.*?</sru:record>", tekst, re.S)
        if not blokken:
            break
        uit += [parse(b) for b in blokken]
        start += len(blokken)
        if len(uit) > verwacht + 2000:
            break
    if len(uit) < verwacht:
        raise RuntimeError(f"{len(uit)} records opgehaald, {verwacht} verwacht: {query}")
    return uit


def zipnaam(shard):
    s, n = shard.split("-")
    return f"Parlement_{SETS[s][0]}_deel{n}.zip"


def prep():
    a, b = dt.date(1990, 1, 1), dt.date.today() + dt.timedelta(days=2)
    os.makedirs(PLAN, exist_ok=True)
    for f in os.listdir(PLAN):
        os.remove(os.path.join(PLAN, f))
    alles = []
    for s, (naam, basis) in SETS.items():
        gezien, totaal, ruw = {}, 0, 0
        for q in basis:
            totaal += tel(q)
            for qq, k in verdeel(q, a, b):
                for r in records(qq, k):
                    ruw += 1
                    o = gezien.get(r["id"])
                    if o is None or int(r["versie"] or 0) > int(o["versie"] or 0):
                        gezien[r["id"]] = r
        if len(gezien) < totaal:
            raise RuntimeError(f"{naam}: {len(gezien)} unieke van {totaal}")
        kstbots = [i for i in gezien if i.startswith("kst-")]
        if kstbots:
            raise RuntimeError(f"{naam}: overlap met Kamerstukken: {kstbots[:3]}")
        uniek = sorted(gezien.values(), key=lambda r: r["id"])
        print(f"{naam}: totaal {totaal}, records {ruw}, uniek {len(uniek)}, "
              f"xml {sum(1 for r in uniek if r['xml'])}, pdf {sum(1 for r in uniek if r['pdf'])}", flush=True)
        for i in range((len(uniek) + PER_SHARD - 1) // PER_SHARD):
            sh = f"{s}-{i:03d}"
            with open(os.path.join(PLAN, sh + ".json"), "w", encoding="utf8") as f:
                json.dump(uniek[i * PER_SHARD:(i + 1) * PER_SHARD], f)
            alles.append(sh)
    keuze = [x.strip() for x in os.environ.get("SHARDS", "").split(",") if x.strip()]
    if keuze:
        open_ = keuze
    elif os.environ.get("GD_REFRESH_TOKEN"):
        d = Drive()
        mappen, _ = d.mappen()
        open_ = [sh for sh in alles if d.zoek(zipnaam(sh), mappen[MAP]) is None]
    else:
        open_ = alles
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"{len(alles)} shards, nog te doen: {len(open_)}")


def haal_stuk(r):
    uit = {"fout": [], "data": {}}
    for soort in ("xml", "pdf"):
        url = r.get(soort) or ""
        if not url:
            continue
        if time.time() - START > GRENS_SEC:
            uit["fout"].append(f"{soort}:tijd: niet meer aan begonnen binnen de tijdgrens")
            continue
        try:
            a = haal(url, min_bytes=300)
        except Exception as e:
            uit["fout"].append(f"{soort}:{type(e).__name__}: {str(e)[:150]}")
            continue
        if a is None:
            uit["fout"].append(f"{soort}:404")
            continue
        fout = controleer_xml(a.content, r["id"]) if soort == "xml" else controleer_pdf(a.content)
        if fout:
            uit["fout"].append(f"{soort}:{fout}")
            continue
        uit["data"][soort] = a.content
    return r, uit


def run(shard, max_n, droog):
    with open(os.path.join(PLAN, shard + ".json"), encoding="utf8") as f:
        stukken = json.load(f)
    if max_n:
        stukken = stukken[:max_n]
    zn = zipnaam(shard)
    print(f"{shard}: {len(stukken)} stukken -> {zn}", flush=True)
    rijen, mislukt, n = [], [], {"xml": 0, "pdf": 0}
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            for k, (r, uit) in enumerate(ex.map(haal_stuk, stukken), 1):
                if uit["fout"]:
                    mislukt.append(f"{r['id']}: " + ", ".join(uit["fout"]))
                for soort, data in uit["data"].items():
                    z.writestr(f"{r['id']}/{soort}/{r['id']}.{soort}", data)
                    n[soort] += 1
                    rijen.append([r["id"], r["kamer"], r["vergaderjaar"], r["dossier"], r["datum"], r["titel"],
                                  soort, r[soort], len(data), hashlib.sha256(data).hexdigest()])
                if k % 500 == 0:
                    print(f"  {k}/{len(stukken)}", flush=True)
        z.writestr("_OVERZICHT.txt",
                   f"Parlementaire geschiedenis na 1994 ({SETS[shard.split('-')[0]][0]})\n"
                   f"Shard: {shard}\nVerzameld: {time.strftime('%Y-%m-%d')}\nStukken: {len(stukken)}\n"
                   f"Xml-bestanden: {n['xml']}\nPdf-bestanden: {n['pdf']}\nMislukt: {len(mislukt)}\n\n"
                   "Bron: manifestatie-url's uit het SRU-record van repository.overheid.nl.\n"
                   "Elk bestand ongewijzigd; xml gecontroleerd op goed gevormd en eigen id, pdf op kop en einde.\n"
                   "De sha256 per bestand staat in de index.\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))
    print(f"{zn}: xml {n['xml']}, pdf {n['pdf']}, mislukt {len(mislukt)}", flush=True)
    idx = zn + ".index.csv.gz"
    with gzip.open(idx, "wt", encoding="utf8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "kamer", "vergaderjaar", "dossier", "datum", "titel", "soort", "url", "bytes", "sha256"])
        w.writerows(rijen)
    if droog:
        print(f"DROOG: {zn} {os.path.getsize(zn)/1024/1024:.1f} MB, index {os.path.getsize(idx)} bytes")
        return
    d = Drive()
    mappen, _ = d.mappen()
    d.upload(zn, mappen[MAP])
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
