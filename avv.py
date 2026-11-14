"""CAO's met wetskracht: alle algemeen verbindend verklaarde CAO-bepalingen.

De Staatscourant publiceert elke algemeen verbindendverklaring (AVV) van
CAO-bepalingen: het verzoek, het besluit van de minister en de tekst waar het om
gaat. Die publicaties staan als officieel stuk in de repository van de overheid,
met een xml (gestructureerde tekst) en een pdf (vastgestelde weergave).

De set wordt bepaald door het documenttype zelf: Staatscourantstukken waarvan het
type zowel "cao" als "bepalingen" noemt. Dat is de rubriek
"algemeenverbindendverklaring van cao-bepalingen" en levert 8.445 stukken
(1994-heden). Ze worden ongewijzigd opgeslagen en per bestand gecontroleerd:
de xml moet goed gevormd zijn en het eigen nummer noemen, de pdf moet met %PDF-
beginnen en met %%EOF eindigen. De sha256 van beide staat in de index.

  python avv.py plan                 -> laten zien hoe de set verdeeld wordt
  python avv.py prep                 -> avv_plan/ + de shards die nog open zijn
  python avv.py run <shard> [--max N] [--droog]
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN
"""

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
import concurrent.futures as cf

from gaten import Drive
from kst import SRU, S, haal, tel, pagina, verdeel, controleer_xml, controleer_pdf

BASIS = 'dt.type any "cao" and dt.type any "bepalingen"'
MAP = "06"
PER_SHARD = 2500
PLAN = "avv_plan"
THREADS = 8


def record_uit(blok):
    def één(pat):
        m = re.search(pat, blok, re.S)
        return m.group(1).strip() if m else ""
    ident = één(r"<dcterms:identifier>([^<]+)</dcterms:identifier>")
    urls = dict(re.findall(r'<gzd:itemUrl manifestation="([^"]+)">([^<]+)</gzd:itemUrl>', blok))
    versie = ""
    m = re.search(r"/" + re.escape(ident) + r"/(\d+)/", urls.get("xml", "") or urls.get("pdf", ""))
    if m:
        versie = m.group(1)
    return {
        "id": ident,
        "titel": één(r"<dcterms:title>([^<]*)</dcterms:title>"),
        "type": één(r"<dcterms:type[^>]*>([^<]*)</dcterms:type>"),
        "maker": één(r"<dcterms:creator[^>]*>([^<]*)</dcterms:creator>"),
        "datum": één(r"<dcterms:date>([^<]*)</dcterms:date>"),
        "gewijzigd": één(r"<dcterms:modified>([^<]*)</dcterms:modified>"),
        "jaargang": één(r"<overheidwetgeving:jaargang>([^<]*)<"),
        "nummer": één(r"<overheidwetgeving:publicatienummer>([^<]*)<"),
        "versie": versie,
        "xml": urls.get("xml", ""),
        "pdf": urls.get("pdf", ""),
    }


def records(query, verwacht=None):
    start, uit = 1, []
    while True:
        tekst = pagina(query, start)
        blokken = re.findall(r"<sru:record>.*?</sru:record>", tekst, re.S)
        if not blokken:
            break
        uit += [record_uit(b) for b in blokken]
        start += len(blokken)
        if verwacht is not None and len(uit) > verwacht + 2000:
            break
    if verwacht is not None and len(uit) < verwacht:
        raise RuntimeError(f"{len(uit)} records opgehaald, {verwacht} verwacht: {query}")
    return uit


def shardnaam(i):
    return f"avv-{i:03d}"


def shardbestand(shard):
    return os.path.join(PLAN, shard + ".json")


def zipnaam(shard):
    return f"CAO_AVV_{shard.replace('avv-', 'deel')}.zip"


def klaar(d, mappen, shard):
    return d.zoek(zipnaam(shard), mappen[MAP]) is not None


def shards():
    if not os.path.isdir(PLAN):
        raise SystemExit("avv_plan/ ontbreekt; draai eerst: python avv.py prep")
    return sorted(f[:-5] for f in os.listdir(PLAN) if f.endswith(".json"))


def plan():
    totaal = tel(BASIS)
    print(f"{totaal} AVV-publicaties", flush=True)
    stukken = verdeel(BASIS, dt.date(1990, 1, 1), dt.date.today() + dt.timedelta(days=2))
    som = sum(k for _, k in stukken)
    print(f"{len(stukken)} stukken, samen {som} records")
    if som != totaal:
        raise RuntimeError(f"plan dekt {som} van {totaal} records")
    return stukken, totaal


def prep():
    stukken, totaal = plan()
    alle, gezien = [], {}
    for i, (query, n) in enumerate(stukken, 1):
        hier = records(query, n)
        for r in hier:
            oud = gezien.get(r["id"])
            if oud is None or int(r["versie"] or 0) > int(oud["versie"] or 0):
                gezien[r["id"]] = r
        alle += hier
        print(f"  {i}/{len(stukken)} {n} records (totaal {len(alle)})", flush=True)
    if len(gezien) < totaal:
        raise RuntimeError(f"{len(gezien)} unieke stukken opgehaald van {totaal}")
    uniek = sorted(gezien.values(), key=lambda r: r["id"])
    print(f"{len(alle)} records -> {len(uniek)} unieke stukken "
          f"({len(alle) - len(uniek)} dubbele versies samengevoegd)")
    print(f"  met xml: {sum(1 for r in uniek if r['xml'])}, "
          f"met pdf: {sum(1 for r in uniek if r['pdf'])}")

    if os.path.isdir(PLAN):
        for f in os.listdir(PLAN):
            os.remove(os.path.join(PLAN, f))
    os.makedirs(PLAN, exist_ok=True)
    aantal = (len(uniek) + PER_SHARD - 1) // PER_SHARD
    for i in range(aantal):
        with open(shardbestand(shardnaam(i)), "w", encoding="utf8") as f:
            json.dump(uniek[i * PER_SHARD:(i + 1) * PER_SHARD], f)
    print(f"{aantal} shards van {PER_SHARD} stukken")

    keuze = [s.strip() for s in os.environ.get("SHARDS", "").split(",") if s.strip()]
    if keuze:
        open_ = keuze
    else:
        d = Drive()
        mappen, _ = d.mappen()
        open_ = [s for s in shards() if not klaar(d, mappen, s)]
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"nog te doen: {len(open_)}")
    print(json.dumps(open_))


def haal_stuk(r):
    uit = {"xml": None, "pdf": None, "fout": [], "controle": {}}
    for soort in ("xml", "pdf"):
        url = r.get(soort) or ""
        if not url:
            continue
        antwoord = haal(url, min_bytes=500)
        if antwoord is None:
            uit["fout"].append(f"{soort}:404")
            continue
        data = antwoord.content
        fout = controleer_xml(data, r["id"]) if soort == "xml" else controleer_pdf(data)
        if fout:
            uit["fout"].append(f"{soort}:{fout}")
            continue
        uit[soort] = data
        uit["controle"][soort] = hashlib.sha256(data).hexdigest()
    return r, uit


def run(shard, max_n, droog):
    with open(shardbestand(shard), encoding="utf8") as f:
        stukken = json.load(f)
    if max_n:
        stukken = stukken[:max_n]
    print(f"{shard}: {len(stukken)} stukken", flush=True)

    zn = zipnaam(shard)
    rijen, mislukt = [], []
    met_xml = met_pdf = 0
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            for k, (r, uit) in enumerate(ex.map(haal_stuk, stukken), 1):
                if uit["fout"]:
                    mislukt.append(f"{r['id']}: " + ", ".join(uit["fout"]))
                if uit["xml"]:
                    z.writestr(f"{r['id']}/xml/{r['id']}.xml", uit["xml"], zipfile.ZIP_DEFLATED)
                    met_xml += 1
                if uit["pdf"]:
                    z.writestr(f"{r['id']}/pdf/{r['id']}.pdf", uit["pdf"], zipfile.ZIP_DEFLATED)
                    met_pdf += 1
                rijen.append([shard, r["id"], r["titel"], r["type"], r["maker"], r["datum"],
                              r["gewijzigd"], r["jaargang"], r["nummer"],
                              "xml" if uit["xml"] else "", len(uit["xml"] or b""),
                              uit["controle"].get("xml", ""),
                              "pdf" if uit["pdf"] else "", len(uit["pdf"] or b""),
                              uit["controle"].get("pdf", ""),
                              r["xml"], r["pdf"]])
                if k % 500 == 0:
                    print(f"  {k}/{len(stukken)}", flush=True)

        z.writestr("_OVERZICHT.txt",
                   "Algemeen verbindend verklaarde CAO-bepalingen (Staatscourant)\n"
                   f"Shard: {shard}\nVerzameld: {time.strftime('%Y-%m-%d')}\n"
                   f"Stukken: {len(stukken)}\nXml-bestanden: {met_xml}\nPdf-bestanden: {met_pdf}\n"
                   f"Mislukt: {len(mislukt)}\n"
                   "\nBron van elk bestand:\n"
                   "  de manifestatie-url's uit het SRU-record van repository.overheid.nl,\n"
                   "  bijvoorbeeld https://repository.overheid.nl/frbr/officielepublicaties/stcrt/\n"
                   "  2024/stcrt-2024-19581/1/xml/stcrt-2024-19581.xml\n"
                   "Elk bestand is ongewijzigd opgeslagen. De xml is gecontroleerd op goed\n"
                   "gevormdheid en op het eigen nummer; de pdf op %PDF- en %%EOF. De sha256\n"
                   "van beide staat in de index, dus het is na te rekenen.\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))

    print(f"{zn}: {len(rijen)} stukken, xml {met_xml}, pdf {met_pdf}, mislukt {len(mislukt)}",
          flush=True)
    if mislukt:
        raise RuntimeError(f"{len(mislukt)} downloads mislukt; zip niet geupload")

    rij = io.StringIO()
    w = csv.writer(rij)
    w.writerow(["shard", "id", "titel", "type", "maker", "datum", "gewijzigd", "jaargang",
                "nummer", "xml", "xml_bytes", "xml_sha256", "pdf", "pdf_bytes", "pdf_sha256",
                "url_xml", "url_pdf"])
    for r in rijen:
        w.writerow(r)
    idx = zn + ".index.csv.gz"
    with gzip.open(idx, "wt", encoding="utf8", newline="") as f:
        f.write(rij.getvalue())

    if droog:
        print(f"DROOG: {zn} {os.path.getsize(zn)/1024/1024:.1f} MB -> map {MAP}", flush=True)
        return
    d = Drive()
    mappen, _ = d.mappen()
    d.upload(zn, mappen[MAP])
    os.remove(zn)
    d.upload(idx, mappen["00"])
    os.remove(idx)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "plan":
        plan()
    elif a[0] == "prep":
        prep()
    elif a[0] == "run":
        mx = int(a[a.index("--max") + 1]) if "--max" in a else 0
        run(a[1], mx, "--droog" in a)
