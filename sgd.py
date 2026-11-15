"""Staten-Generaal Digitaal 1814-1995: Kamerstukken, Handelingen en Aanhangsels van voor 1995.

Bron: dezelfde SRU als officielebekendmakingen.nl (repository.overheid.nl), met
c.product-area==sgd. Geen inlog of robots-beperking; de site is open data van
Overheid.nl. Per stuk zijn er (alleen als dat er is) de volgende manifestaties:
  ocr          de tekst per pagina (xml)            ~60 KB per stuk
  metadata     metadata.xml + didl.xml               klein
  coordinaten  woordcoordinaten per pagina (xml)     ~200 KB per stuk
  pdf          het hele stuk als pdf                 ~3,7 MB per stuk
  jpg          paginascan                            ~1,8 MB per stuk

De keuze van soorten staat in SGD_SOORTEN (komma-lijst, standaard 'ocr', wat ook de
metadata meeneemt). Elke soort krijgt eigen zips (SGD_<soort>_deelNNN.zip), zodat een
volgende soort later als aparte ronde kan zonder iets opnieuw te doen.
Bestanden zijn ongewijzigd opgeslagen; sha256 in de index.

  python sgd.py prep               -> sgd_plan/ + shards die nog open zijn
  python sgd.py run <shard> [--max N] [--droog]     (shard = 0000, 0001, ...)
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN, SGD_SOORTEN, TIJDGRENS_MIN
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
import xml.etree.ElementTree as ET

from kst import haal, tel, pagina, controleer_pdf, meld_mislukt
from gaten import Drive

BASIS = "c.product-area==sgd"
MAP = "07"
PER_SHARD = 2500
LIMIET = 9500
PLAN = "sgd_plan"
THREADS = 6
SOORTEN = [s.strip() for s in os.environ.get("SGD_SOORTEN", "ocr").split(",") if s.strip()]
LABEL = "_".join(SOORTEN)
START = time.time()
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "200")) * 60
ROOT = "https://repository.overheid.nl/frbr/sgd/"


def een(pat, tekst):
    m = re.search(pat, tekst, re.S)
    return m.group(1).strip() if m else ""


def parse(blok):
    ident = een(r"<dcterms:identifier>([^<]+)<", blok)
    man = re.findall(r'<gzd:itemUrl manifestation="([^"]+)">([^<]+)</gzd:itemUrl>', blok)
    uit = {"ocr": [], "coordinaten": [], "jpg": [], "pdf": [], "metadata": []}
    for soort, u in man:
        if soort in uit:
            uit[soort].append(u)
    maker = een(r"<dcterms:creator[^>]*>([^<]*)<", blok)
    kamer = "TK" if "Tweede" in maker else "EK" if "Eerste" in maker else "VV" if "Verenigde" in maker else maker
    vj = een(r"<overheidwetgeving:vergaderjaar>([^<]*)<", blok)
    basis = None
    for u in uit["pdf"] + uit["ocr"] + uit["metadata"]:
        m = re.match(r"(" + re.escape(ROOT) + r"[^/]+/" + re.escape(ident) + r"/\d+)/", u)
        if m:
            basis = m.group(1)
            break
    rec = {"id": ident, "kamer": kamer, "vergaderjaar": vj, "dossier": een(r"<overheidwetgeving:dossiernummer>([^<]*)<", blok),
           "datum": een(r"<dcterms:temporal>([^<]*)<", blok) or een(r"<dcterms:available>([^<]*)<", blok),
           "titel": een(r"<dcterms:title>([^<]*)<", blok), "type": een(r"<dcterms:type[^>]*>([^<]*)<", blok),
           "basis": basis}
    # compact: de bestandsnamen van pagina's volgen een vast patroon; afwijking = volledige url's bewaren
    if basis:
        for soort in ("ocr", "coordinaten", "jpg"):
            rec[soort] = [u[len(basis) + 1:] for u in uit[soort]]
        rec["pdf"] = [u[len(basis) + 1:] for u in uit["pdf"]]
        rec["metadata"] = [u[len(basis) + 1:] for u in uit["metadata"]]
    return rec


def url_van(r, soort, naam):
    return r["basis"] + "/" + naam


def records(query, verwacht):
    start, uit = 1, []
    while True:
        blokken = re.findall(r"<sru:record>.*?</sru:record>", pagina(query, start, 500), re.S)
        if not blokken:
            break
        for b in blokken:
            r = parse(b)
            for soort in ("ocr", "metadata", "coordinaten", "pdf", "jpg"):
                if soort not in SOORTEN and not (soort == "metadata" and "ocr" in SOORTEN):
                    r.pop(soort, None)
            uit.append(r)
        start += len(blokken)
        if len(uit) > verwacht + 2000:
            break
    if len(uit) < verwacht:
        raise RuntimeError(f"{len(uit)} records opgehaald, {verwacht} verwacht: {query}")
    return uit


def stukken_plan():
    """Knipt c.product-area==sgd per beginjaar van de content-area (sgd/1868, sgd/18681869,
    sgd/18631873 horen bij het beginjaar) en waar nodig per type. De som moet het totaal zijn."""
    totaal = tel(BASIS)
    stukken, som = [], 0
    for j in range(1800, 2000):
        q = f'{BASIS} and c.content-area>="sgd/{j}" and c.content-area<"sgd/{j + 1}"'
        n = tel(q)
        if not n:
            continue
        if n <= LIMIET:
            stukken.append((q, n))
            som += n
            continue
        sub = 0
        for ty in ("Kamerstuk", "Handelingen", "Kamervragen (Aanhangsel)", "Naamlijst", "Register"):
            q2 = f'{q} and dt.type=="{ty}"'
            k = tel(q2)
            if k > LIMIET:
                raise RuntimeError(f"stuk blijft te groot ({k}): {q2}")
            if k:
                stukken.append((q2, k))
                sub += k
        if sub != n:
            raise RuntimeError(f"{q}: types dekken {sub} van {n}")
        som += n
    print(f"{totaal} totaal in de index, {som} gedekt door {len(stukken)} stukken", flush=True)
    if som != totaal:
        raise RuntimeError(f"plan dekt {som} van {totaal}")
    return stukken, totaal


def shardbestand(s):
    return os.path.join(PLAN, f"sgd-{s}.json")


def zipnaam(s):
    return f"SGD_{LABEL}_deel{s}.zip"


def prep():
    stukken, totaal = stukken_plan()
    gezien, ruw = {}, 0
    for i, (q, n) in enumerate(stukken, 1):
        for r in records(q, n):
            ruw += 1
            gezien[r["id"]] = r
        if i % 10 == 0:
            print(f"  {i}/{len(stukken)} stukken, {ruw} records", flush=True)
    print(f"{ruw} records, {len(gezien)} unieke ids; index-totaal {totaal}", flush=True)
    uniek = sorted(gezien.values(), key=lambda r: (r["vergaderjaar"], r["id"]))
    zonder = sum(1 for r in uniek if not r["basis"])
    print(f"  zonder bestanden: {zonder}; ocr-paginas {sum(len(r.get('ocr', [])) for r in uniek)}, "
          f"met pdf {sum(1 for r in uniek if r.get('pdf'))}", flush=True)
    os.makedirs(PLAN, exist_ok=True)
    for f in os.listdir(PLAN):
        os.remove(os.path.join(PLAN, f))
    n = (len(uniek) + PER_SHARD - 1) // PER_SHARD
    for i in range(n):
        with open(shardbestand(f"{i:04d}"), "w", encoding="utf8") as f:
            json.dump(uniek[i * PER_SHARD:(i + 1) * PER_SHARD], f)
    alles = [f"{i:04d}" for i in range(n)]
    keuze = [x.strip() for x in os.environ.get("SHARDS", "").split(",") if x.strip()]
    if keuze:
        open_ = keuze
    elif os.environ.get("GD_REFRESH_TOKEN"):
        d = Drive()
        mappen, _ = d.mappen()
        open_ = [s for s in alles if d.zoek(zipnaam(s), mappen[MAP]) is None]
    else:
        open_ = alles
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"{n} shards, nog te doen: {len(open_)}")


def taken_van(r):
    """(soort, bestandsnaam in zip, url) per te halen bestand van dit stuk."""
    t = []
    if "ocr" in SOORTEN:
        for nm in r.get("ocr", []):
            t.append(("ocr", nm))
        for nm in r.get("metadata", []):
            t.append(("metadata", nm))
    for soort in ("coordinaten", "pdf", "jpg"):
        if soort in SOORTEN:
            for nm in r.get(soort, []):
                t.append((soort, nm))
    return t


def haal_een(args):
    r, soort, nm = args
    if time.time() - START > GRENS_SEC:
        return args, None, "tijd: niet meer aan begonnen binnen de tijdgrens"
    url = r["basis"] + "/" + nm
    try:
        a = haal(url, min_bytes=40 if soort != "jpg" and soort != "pdf" else 500)
    except Exception as e:
        return args, None, f"{type(e).__name__}: {str(e)[:150]}"
    if a is None:
        return args, None, "404"
    data = a.content
    if soort in ("ocr", "coordinaten", "metadata"):
        try:
            ET.fromstring(data)
        except ET.ParseError as e:
            return args, None, f"xml niet goed gevormd ({e})"
    elif soort == "pdf":
        f = controleer_pdf(data)
        if f:
            return args, None, f
    elif soort == "jpg" and not data.startswith(b"\xff\xd8"):
        return args, None, "geen jpg-kop"
    return args, data, ""


def run(s, max_n, droog):
    with open(shardbestand(s), encoding="utf8") as f:
        stukken = json.load(f)
    if max_n:
        stukken = stukken[:max_n]
    zn = zipnaam(s)
    taken = [(r, soort, nm) for r in stukken if r["basis"] for soort, nm in taken_van(r)]
    zonder = [r["id"] for r in stukken if not r["basis"]]
    print(f"{s}: {len(stukken)} stukken, {len(taken)} bestanden -> {zn}", flush=True)
    rijen, mislukt, tel_ = [], [f"{i}: geen bestanden in de index" for i in zonder], {}
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            for k, ((r, soort, nm), data, fout) in enumerate(ex.map(haal_een, taken), 1):
                if fout:
                    mislukt.append(f"{r['id']}: {soort}/{nm}: {fout}")
                else:
                    z.writestr(f"{r['id']}/{nm}", data)
                    tel_[soort] = tel_.get(soort, 0) + 1
                    rijen.append([r["id"], r["kamer"], r["vergaderjaar"], r["dossier"], r["datum"], r["titel"],
                                  soort, r["basis"] + "/" + nm, len(data), hashlib.sha256(data).hexdigest()])
                if k % 5000 == 0:
                    print(f"  {k}/{len(taken)}", flush=True)
        z.writestr("_OVERZICHT.txt",
                   f"Staten-Generaal Digitaal 1814-1995, soorten: {', '.join(SOORTEN)}\n"
                   f"Shard: {s}\nVerzameld: {time.strftime('%Y-%m-%d')}\nStukken: {len(stukken)}\n"
                   f"Bestanden per soort: {tel_}\nMislukt: {len(mislukt)}\n\n"
                   "Bron: repository.overheid.nl (c.product-area==sgd), de open data achter statengeneraaldigitaal.nl.\n"
                   "Bestanden ongewijzigd; xml gecontroleerd op goed gevormd, pdf op kop en einde. sha256 in de index.\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))
    print(f"{zn}: {tel_}, mislukt {len(mislukt)}", flush=True)
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
