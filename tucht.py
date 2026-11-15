"""Tuchtrecht: alle gepubliceerde tuchtuitspraken (advocatuur, notariaat, gezondheidszorg,
accountants, enz.) van tuchtrecht.overheid.nl.

Bron: de SRU van repository.overheid.nl (product-area tuchtrecht, ~48.800 records), met per
uitspraak de xml en de pdf die de overheid zelf publiceert. Bestanden ongewijzigd; sha256 in
de index. Per jaar (content-area "tuchtrecht/JJJJ") blijft elk stuk onder de 10.000-grens van
de SRU. Geen inlog; tuchtrecht.overheid.nl heeft geen robots.txt en de SRU is de open-data
route die de overheid zelf aanbiedt.

  python tucht.py prep                 -> tucht_plan/ + shards die nog open zijn
  python tucht.py run <shard> [--max N] [--droog]
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN, optioneel TIJDGRENS_MIN (300)
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

import kst
from kst import haal, tel, pagina
from gaten import Drive

MAP = "04"
PER_SHARD = 2500
PLAN = "tucht_plan"
THREADS = 6
START = time.time()
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "300")) * 60
TOTAAL_Q = "c.product-area==tuchtrecht"


def rec(blok):
    def een(pat):
        m = re.search(pat, blok, re.S)
        return m.group(1).strip() if m else ""
    ident = een(r"<dcterms:identifier>([^<]+)</dcterms:identifier>")
    urls = dict(re.findall(r'<gzd:itemUrl manifestation="([^"]+)">([^<]+)</gzd:itemUrl>', blok))
    m = re.search(r"/(\d+)/(?:xml|pdf)/", urls.get("xml", "") or urls.get("pdf", ""))
    return {"id": ident, "titel": een(r"<dcterms:title>([^<]*)<"),
            "instantie": een(r"<dcterms:creator[^>]*>([^<]*)<"),
            "domein": een(r"<overheidwetgeving:instantieDomein>([^<]*)<"),
            "datum": een(r"<overheidwetgeving:uitspraakdatum>([^<]*)<") or een(r"<dcterms:modified>([^<]*)<"),
            "versie": m.group(1) if m else "1",
            "xml": urls.get("xml", ""), "pdf": urls.get("pdf", "")}


def records(query, verwacht):
    start, uit = 1, []
    while True:
        blokken = re.findall(r"<sru:record>.*?</sru:record>", pagina(query, start), re.S)
        if not blokken:
            break
        uit += [rec(b) for b in blokken]
        start += len(blokken)
    if len(uit) < verwacht:
        raise RuntimeError(f"{len(uit)} van {verwacht}: {query}")
    return uit


def zipnaam(shard):
    return f"Tuchtrecht_{shard.replace('tucht-', 'deel')}.zip"


def prep():
    totaal = tel(TOTAAL_Q)
    print(f"{totaal} tuchtuitspraken in de SRU", flush=True)
    alle, som = {}, 0
    for jaar in range(1900, int(time.strftime("%Y")) + 2):
        q = f'c.content-area=="tuchtrecht/{jaar}"'
        n = tel(q)
        if not n:
            continue
        if n > 9500:
            raise RuntimeError(f"jaar {jaar} heeft {n} records; verdelen nodig")
        som += n
        for r in records(q, n):
            oud = alle.get(r["id"])
            if oud is None or int(r["versie"]) > int(oud["versie"]):
                alle[r["id"]] = r
        print(f"  {jaar}: {n} (uniek tot nu {len(alle)})", flush=True)
    if som < totaal:
        raise RuntimeError(f"jaren dekken {som} van {totaal}")
    uniek = sorted(alle.values(), key=lambda r: (r["datum"], r["id"]))
    os.makedirs(PLAN, exist_ok=True)
    for f in os.listdir(PLAN):
        os.remove(os.path.join(PLAN, f))
    aantal = (len(uniek) + PER_SHARD - 1) // PER_SHARD
    for i in range(aantal):
        with open(f"{PLAN}/tucht-{i:03d}.json", "w", encoding="utf8") as f:
            json.dump(uniek[i * PER_SHARD:(i + 1) * PER_SHARD], f)
    print(f"{len(uniek)} unieke uitspraken, {aantal} shards", flush=True)
    keuze = [s.strip() for s in os.environ.get("SHARDS", "").split(",") if s.strip()]
    if keuze:
        open_ = keuze
    else:
        d = Drive()
        mappen, _ = d.mappen()
        open_ = [s for s in sorted(f[:-5] for f in os.listdir(PLAN))
                 if not d.zoek(zipnaam(s), mappen[MAP])]
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"nog te doen: {len(open_)}", flush=True)


def veilig(i):
    return re.sub(r"[^\w.\-]", "_", i)


def stuk(r):
    uit = {"xml": None, "pdf": None, "fout": []}
    if time.time() - START > GRENS_SEC:
        uit["fout"].append("tijd: niet meer aan begonnen binnen de tijdgrens van de job")
        return r, uit
    for soort in ("xml", "pdf"):
        url = r.get(soort)
        if not url:
            continue
        try:
            a = haal(url, min_bytes=300)
        except Exception as e:
            uit["fout"].append(f"{soort}:{type(e).__name__}")
            continue
        if a is None:
            uit["fout"].append(f"{soort}:404")
            continue
        d = a.content
        if soort == "pdf" and not d.startswith(b"%PDF-"):
            uit["fout"].append("pdf:geen pdf-kop")
            continue
        if soort == "xml" and r["id"] not in d[:20000].decode("utf8", "ignore"):
            uit["fout"].append("xml:noemt eigen nummer niet")
            continue
        uit[soort] = d
    return r, uit


def run(shard, max_n, droog):
    with open(f"{PLAN}/{shard}.json", encoding="utf8") as f:
        lijst = json.load(f)
    if max_n:
        lijst = lijst[:max_n]
    print(f"{shard}: {len(lijst)} uitspraken", flush=True)
    zn = zipnaam(shard)
    rijen, mislukt = [], []
    nx = npdf = 0
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            for k, (r, u) in enumerate(ex.map(stuk, lijst), 1):
                if u["fout"]:
                    mislukt.append(f"{r['id']}: " + ", ".join(u["fout"]))
                base = veilig(r["id"])
                sx = sp = ""
                if u["xml"]:
                    z.writestr(f"{base}/{base}.xml", u["xml"])
                    nx += 1
                    sx = hashlib.sha256(u["xml"]).hexdigest()
                if u["pdf"]:
                    z.writestr(f"{base}/{base}.pdf", u["pdf"])
                    npdf += 1
                    sp = hashlib.sha256(u["pdf"]).hexdigest()
                rijen.append([r["id"], r["instantie"], r["domein"], r["datum"], r["titel"],
                              r["xml"], len(u["xml"] or b""), sx, r["pdf"], len(u["pdf"] or b""), sp])
                if k % 500 == 0:
                    print(f"  {k}/{len(lijst)}", flush=True)
        z.writestr("_OVERZICHT.txt",
                   f"Tuchtrecht (tuchtrecht.overheid.nl, via SRU repository.overheid.nl)\nShard: {shard}\n"
                   f"Verzameld: {time.strftime('%Y-%m-%d')}\nUitspraken: {len(lijst)}\nXml: {nx}\nPdf: {npdf}\n"
                   f"Mislukt: {len(mislukt)}\nBestanden ongewijzigd; sha256 staat in de index.\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))
    print(f"{zn}: {len(rijen)} uitspraken, xml {nx}, pdf {npdf}, mislukt {len(mislukt)}", flush=True)
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(["ecli", "instantie", "domein", "datum", "titel", "url_xml", "xml_bytes", "xml_sha256",
                "url_pdf", "pdf_bytes", "pdf_sha256"])
    w.writerows(rijen)
    idx = zn + ".index.csv.gz"
    with gzip.open(idx, "wt", encoding="utf8", newline="") as f:
        f.write(s.getvalue())
    if droog:
        print(f"DROOG: {zn} {os.path.getsize(zn)/1024/1024:.1f} MB -> map {MAP}")
        return
    d = Drive()
    mappen, _ = d.mappen()
    d.upload(zn, mappen[MAP])
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
