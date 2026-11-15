"""Decentrale officiële bladen: Gemeenteblad, Provinciaal blad, Waterschapsblad en
Blad gemeenschappelijke regeling (bekendmakingen van lokale overheden, vanaf 2013).

Bron: de SRU van repository.overheid.nl (zelfde index als officielebekendmakingen.nl),
met per record de manifestaties xml/pdf. Wij halen ze ongewijzigd op:

  - xml voor alle vier de bladen (de volledige gestructureerde tekst);
  - pdf alleen voor Provinciaal blad, Waterschapsblad en Blad gemeenschappelijke regeling.
    De pdf's van het Gemeenteblad (4,7 miljoen stuks, gemeten gemiddeld 290 kB) zouden
    ± 1,4 TB zijn; die vallen buiten de ruimte en staan niet in de oogst (zet
    PDF_PUBS/'gmb' bij als daar ruimte voor komt).

Een shard is een vaste datumperiode (dt.date) van één blad: een maand of jaar, of een
helft daarvan als het er meer dan PER_SHARD records zijn. Binnen de shard wordt de
periode verder geknipt tot stukken onder de 9.500 records (de SRU geeft er maximaal
10.000). De som van de stukken moet exact het getelde aantal zijn.

  python decentraal.py plan                -> telling per blad/periode (geen bestanden)
  python decentraal.py prep                -> decentraal_plan/ + shards die nog open zijn
  python decentraal.py run <shard> [--max N] [--droog]
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN; TIJDGRENS_MIN (standaard 250)
"""
import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile

import kst
from gaten import Drive

PUBS = {"gmb": "Gemeenteblad", "prb": "Provinciaal blad", "wsb": "Waterschapsblad",
        "bgr": "Blad gemeenschappelijke regeling"}
PDF_PUBS = {"prb", "wsb", "bgr"}
STARTJAAR = 2013
PER_SHARD = 30000
LIMIET = kst.LIMIET
PLAN = "decentraal_plan"
THREADS = int(os.environ.get("THREADS", "20"))
START = time.time()
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "250")) * 60


def basis(pub):
    return f'w.publicatienaam=="{PUBS[pub]}"'


def periode_q(pub, a, b):
    return f"{basis(pub)} and dt.date>={a.isoformat()} and dt.date<{b.isoformat()}"


def maanden(pub):
    """Vaste periodes: per maand voor gmb, per jaar voor de rest, tot en met morgen."""
    eind = dt.date.today() + dt.timedelta(days=2)
    uit = []
    if pub == "gmb":
        y, m = STARTJAAR - 1, 1          # 2013 komt in een los blok mee (paar duizend stuks)
        a = dt.date(1990, 1, 1)
        b = dt.date(STARTJAAR, 1, 1)
        uit.append((a, b))
        y, m = STARTJAAR, 1
        while dt.date(y, m, 1) < eind:
            a = dt.date(y, m, 1)
            b = dt.date(y + (m == 12), 1 if m == 12 else m + 1, 1)
            uit.append((a, min(b, eind)))
            y, m = b.year, b.month
    else:
        uit.append((dt.date(1990, 1, 1), dt.date(STARTJAAR, 1, 1)))
        for y in range(STARTJAAR, eind.year + 1):
            uit.append((dt.date(y, 1, 1), min(dt.date(y + 1, 1, 1), eind)))
    return uit


def knip_periode(pub, a, b, per_shard=PER_SHARD):
    """[(a, b, n)] met n <= per_shard (of een enkele dag als dat niet verder kan)."""
    n = kst.tel(periode_q(pub, a, b))
    if n == 0:
        return []
    if n <= per_shard or (b - a).days <= 1:
        return [(a, b, n)]
    m = a + dt.timedelta(days=(b - a).days // 2)
    return knip_periode(pub, a, m, per_shard) + knip_periode(pub, m, b, per_shard)


def shardnaam(pub, a, b):
    return f"{pub}-{a.strftime('%Y%m%d')}-{b.strftime('%Y%m%d')}"


def parse(shard):
    pub, a, b = shard.split("-")
    f = lambda s: dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))
    return pub, f(a), f(b)


def zipnaam(shard):
    return f"Decentraal_{shard}.zip"


def stukken_van(pub, a, b, n):
    """Knipt een periode in stukken onder LIMIET; som moet gelijk zijn aan n."""
    uit = kst._knipper(periode_q(pub, a, b), "dt.date", a, b)
    if uit is None or sum(k for _, k in uit) != n:
        raise RuntimeError(f"periode {pub} {a}..{b} laat zich niet knippen (n={n})")
    return uit


def plan():
    alles = {}
    for pub in PUBS:
        totaal = kst.tel(basis(pub))
        shards = []
        for a, b in maanden(pub):
            shards += knip_periode(pub, a, b)
        som = sum(n for _, _, n in shards)
        print(f"{pub}: {totaal} records in Bron, {len(shards)} shards, samen {som}", flush=True)
        if som != totaal:
            # nieuwe records tijdens het tellen mogen; minder is een fout
            if som < totaal:
                raise RuntimeError(f"{pub}: shards dekken {som} van {totaal}")
        alles[pub] = shards
    return alles


def klaar(d, mappen, shard):
    return d.zoek(zipnaam(shard), mappen["08"]) is not None


def prep():
    alles = plan()
    os.makedirs(PLAN, exist_ok=True)
    namen = []
    for pub, shards in alles.items():
        for a, b, n in shards:
            s = shardnaam(pub, a, b)
            with open(os.path.join(PLAN, s + ".json"), "w") as f:
                json.dump({"pub": pub, "van": a.isoformat(), "tot": b.isoformat(), "n": n}, f)
            namen.append(s)
    keuze = [s.strip() for s in os.environ.get("SHARDS", "").split(",") if s.strip()]
    if keuze:
        te_doen = keuze
    else:
        d = Drive()
        mappen, _ = d.mappen()
        te_doen = [s for s in namen if not klaar(d, mappen, s)]
    # een matrix mag hooguit 256 jobs hebben; de rest volgt in een volgende run
    te_doen = te_doen[:250]
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(te_doen) + "\n")
    print(f"{len(namen)} shards, nog te doen: {len(te_doen)}")


def alle_records(pub, a, b, n):
    """Records van de periode; per id de hoogste versie. Geeft (lijst, aantal_records)."""
    gezien, totaal = {}, 0
    for q, k in stukken_van(pub, a, b, n):
        start = 1
        while True:
            tekst = kst.pagina(q, start)
            blokken = re.findall(r"<sru:record>.*?</sru:record>", tekst, re.S)
            if not blokken:
                break
            for blok in blokken:
                r = kst.record_uit(blok)
                totaal += 1
                oud = gezien.get(r["id"])
                if oud is None or int(r["versie"] or 0) > int(oud["versie"] or 0):
                    gezien[r["id"]] = r
            start += len(blokken)
    return sorted(gezien.values(), key=lambda r: r["id"]), totaal


def haal_stuk(taak):
    r, pub = taak
    uit = {"xml": None, "pdf": None, "fout": [], "controle": {}}
    if time.time() - START > GRENS_SEC:
        uit["fout"].append("tijd: niet meer aan begonnen binnen de tijdgrens van de job")
        return r, uit
    for soort in ("xml", "pdf"):
        if soort == "pdf" and pub not in PDF_PUBS:
            continue
        url = r.get(soort) or ""
        if not url:
            continue
        try:
            antwoord = kst.haal(url, min_bytes=200 if soort == "xml" else 500)
        except Exception as e:
            uit["fout"].append(f"{soort}:{type(e).__name__}: {str(e)[:200]}")
            continue
        if antwoord is None:
            uit["fout"].append(f"{soort}:404")
            continue
        data = antwoord.content
        fout = kst.controleer_xml(data, r["id"]) if soort == "xml" else kst.controleer_pdf(data)
        if fout:
            uit["fout"].append(f"{soort}:{fout}")
            continue
        uit[soort] = data
        uit["controle"][soort] = hashlib.sha256(data).hexdigest()
    return r, uit


def run(shard, max_n, droog):
    with open(os.path.join(PLAN, shard + ".json")) as f:
        pl = json.load(f)
    pub = pl["pub"]
    a, b = dt.date.fromisoformat(pl["van"]), dt.date.fromisoformat(pl["tot"])
    recs, totaal = alle_records(pub, a, b, pl["n"])
    if max_n:
        recs = recs[:max_n]
    print(f"{shard}: {totaal} records -> {len(recs)} unieke stukken", flush=True)

    zn = zipnaam(shard)
    rijen, mislukt = [], []
    met_xml = met_pdf = 0
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            for k, (r, uit) in enumerate(ex.map(haal_stuk, [(r, pub) for r in recs]), 1):
                if uit["fout"]:
                    mislukt.append(f"{r['id']}: " + ", ".join(uit["fout"]))
                if uit["xml"]:
                    z.writestr(f"{r['id']}/xml/{r['id']}.xml", uit["xml"])
                    met_xml += 1
                if uit["pdf"]:
                    z.writestr(f"{r['id']}/pdf/{r['id']}.pdf", uit["pdf"])
                    met_pdf += 1
                rijen.append([shard, r["id"], r["titel"], r["type"], r["maker"], r["datum"], r["versie"],
                              "xml" if uit["xml"] else "", len(uit["xml"] or b""), uit["controle"].get("xml", ""),
                              "pdf" if uit["pdf"] else "", len(uit["pdf"] or b""), uit["controle"].get("pdf", ""),
                              r["xml"], r["pdf"]])
                if k % 2000 == 0:
                    print(f"  {k}/{len(recs)}", flush=True)
        z.writestr("_OVERZICHT.txt",
                   f"{PUBS[pub]} (officiële bekendmaking, decentraal)\nShard: {shard}\n"
                   f"Periode (dt.date): {a} t/m {b - dt.timedelta(days=1)}\n"
                   f"Verzameld: {time.strftime('%Y-%m-%d')}\nRecords in SRU: {totaal}\n"
                   f"Unieke stukken: {len(recs)}\nXml-bestanden: {met_xml}\nPdf-bestanden: {met_pdf}\n"
                   f"Mislukt: {len(mislukt)}\n"
                   + ("De pdf-weergave van het Gemeenteblad is bewust niet opgenomen (omvang ca. 1,4 TB); per stuk op te halen via de kolom url_pdf in de index.\n" if pub not in PDF_PUBS else "")
                   + "\nBron van elk bestand: de manifestatie-url's uit het SRU-record van\n"
                     "repository.overheid.nl (frbr/officielepublicaties/<blad>/<jaar>/<id>/<versie>/...).\n"
                     "Bestanden ongewijzigd; de xml is gecontroleerd op goede vorming en het eigen nummer,\n"
                     "de pdf op %PDF- en %%EOF; sha256 staat in de index.\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))
    print(f"{zn}: {len(rijen)} stukken, xml {met_xml}, pdf {met_pdf}, mislukt {len(mislukt)}", flush=True)

    rij = io.StringIO()
    w = csv.writer(rij)
    w.writerow(["shard", "id", "titel", "type", "maker", "datum", "versie", "xml", "xml_bytes", "xml_sha256",
                "pdf", "pdf_bytes", "pdf_sha256", "url_xml", "url_pdf"])
    w.writerows(rijen)
    idx = zn + ".index.csv.gz"
    with gzip.open(idx, "wt", encoding="utf8", newline="") as f:
        f.write(rij.getvalue())
    if droog:
        print(f"DROOG: {zn} {os.path.getsize(zn)/1024/1024:.1f} MB -> map 08")
        return
    d = Drive()
    mappen, _ = d.mappen()
    d.upload(zn, mappen["08"])
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
    elif a[0] == "plan":
        plan()
    elif a[0] == "prep":
        prep()
    elif a[0] == "run":
        run(a[1], int(a[a.index("--max") + 1]) if "--max" in a else 0, "--droog" in a)
