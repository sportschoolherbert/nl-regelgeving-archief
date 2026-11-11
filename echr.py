"""EHRM-uitspraken van HUDOC als DOCX plus tekstextract.

HUDOC staat achter Cloudflare: een kale requests-client krijgt 403, een echte
browser lost de challenge op. Daarom draait alles hier door Playwright, en
praten we via de pagina zelf met de API (zelfde origin, zelfde cookies,
zelfde TLS-vingerafdruk).

Per decennium één zip in map 01. Van elke uitspraak bewaren we zowel de DOCX
(leesbaar, met opmaak) als een .txt-extract (doorzoekbaar, klein).
"""

import io
import json
import os
import re
import sys
import time
import zipfile

import requests

from gaten import Drive

HOME = "https://hudoc.echr.coe.int/"
API = "https://hudoc.echr.coe.int/app/query/results"
DOC = "https://hudoc.echr.coe.int/app/conversion/docx"
MAP = "01"
BLOK = 500          # maxRecords per HUDOC-verzoek


def decennia():
    # HUDOC heeft geen uitspraken van voor 1960 (Lawless, 14-11-1960 is de eerste)
    return [f"ehr-{d}" for d in range(1960, 2030, 10)]


def zoek(ctx, pg, query, start, lengte):
    """Eén HUDOC-verzoek, met terugval op een fetch vanuit de pagina zelf."""
    params = {"query": query, "select": "itemid,docname,kpdate,appno,conclusion",
              "sort": "kpdate Ascending", "start": start, "length": lengte}
    url = API + "?" + "&".join(
        f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    for poging in range(4):
        try:
            r = ctx.request.get(url, timeout=120000)
            if r.status == 200 and r.body()[:1] == b"{":
                return r.json()
            print(f"  API {r.status}, opnieuw", flush=True)
        except Exception as fout:
            print(f"  API {type(fout).__name__}, opnieuw", flush=True)
        try:
            tekst = pg.evaluate("async (u) => { const r = await fetch(u); return await r.text(); }", url)
            if tekst[:1] == "{":
                return json.loads(tekst)
        except Exception:
            pass
        pg.goto(HOME, timeout=120000, wait_until="load")
        pg.wait_for_timeout(8000)
    raise RuntimeError("HUDOC geeft geen JSON voor " + query)


def naar_tekst(docx):
    """Plain text uit een DOCX, zonder Word nodig te hebben."""
    with zipfile.ZipFile(io.BytesIO(docx)) as z:
        xml = z.read("word/document.xml").decode("utf8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    return re.sub(r"<[^>]+>", "", xml).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def haal_docx(ctx, itemid):
    for poging in range(4):
        try:
            r = ctx.request.get(DOC, params={"library": "ECHR", "id": itemid,
                                             "filename": itemid + ".docx"}, timeout=120000)
            if r.status == 200 and r.body()[:2] == b"PK":
                return r.body()
        except Exception:
            pass
        time.sleep(3 * (poging + 1))
    return None


def zipnaam(shard):
    return f"EHRM_uitspraken_{shard.split('-')[1]}.zip"


def klaar(d, mappen, shard):
    return d.zoek(zipnaam(shard), mappen[MAP]) is not None


def run(shard, max_n, droog):
    decennium = shard.split("-")[1]
    van, tot = int(decennium), int(decennium) + 9
    query = (f'contentsitename:ECHR AND doctype="HEJUD" '
             f'AND kpdate:[{van}-01-01 TO {tot}-12-31]')

    with __import__("playwright.sync_api", fromlist=["sync_playwright"]).sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(locale="nl-NL")
        pg = ctx.new_page()
        pg.goto(HOME, timeout=120000, wait_until="load")
        pg.wait_for_timeout(8000)

        eerste = zoek(ctx, pg, query, 0, 1)
        totaal = eerste.get("resultcount", 0)
        print(f"{shard}: {totaal} uitspraken", flush=True)
        records = []
        for start in range(0, totaal, BLOK):
            j = zoek(ctx, pg, query, start, BLOK)
            for rec in j.get("results", []):
                c = rec.get("columns", {})
                if c.get("itemid"):
                    records.append(c)
            print(f"  opgehaald {len(records)}/{totaal}", flush=True)
            if max_n and len(records) >= max_n:
                break
        if max_n:
            records = records[:max_n]

        zn = zipnaam(shard)
        rijen, mislukt = [], 0
        with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for i, c in enumerate(records, 1):
                itemid = c["itemid"]
                data = haal_docx(ctx, itemid)
                if not data:
                    mislukt += 1
                    continue
                z.writestr(f"{decennium}/{itemid}.docx", data, zipfile.ZIP_STORED)
                try:
                    z.writestr(f"{decennium}/{itemid}.txt", naar_tekst(data), zipfile.ZIP_DEFLATED)
                except Exception:
                    pass
                rijen.append((f"{itemid}.docx", {
                    "id": itemid,
                    "titel": " ".join(str(c.get("docname", "")).split()),
                    "datum": str(c.get("kpdate", ""))[:10],
                    "bron": c.get("appno", ""),
                    "url": f"https://hudoc.echr.coe.int/eng?i={itemid}"}))
                if i % 250 == 0:
                    print(f"  {i}/{len(records)}", flush=True)
            z.writestr("_OVERZICHT.txt",
                       f"EHRM-uitspraken {decennium}-{tot}\n"
                       f"Verzameld op {time.strftime('%Y-%m-%d')}\n"
                       f"Uitspraken: {len(rijen)}\nNiet op te halen: {mislukt}\n"
                       f"Per uitspraak: .docx (opmaak) en .txt (doorzoekbaar)\n")
        b.close()

    print(f"{zn}: {len(rijen)} uitspraken, {mislukt} mislukt", flush=True)

    rij = io.StringIO()
    import csv
    w = csv.writer(rij)
    w.writerow(["shard", "bestand", "id", "titel", "datum", "auteurs", "bron", "url"])
    for naam, m in rijen:
        w.writerow([shard, naam, m["id"], m["titel"], m["datum"], "", m["bron"], m["url"]])
    idx = zn + ".index.csv.gz"
    import gzip
    with gzip.open(idx, "wt", encoding="utf8", newline="") as f:
        f.write(rij.getvalue())
    print(f"index: {len(rijen)} regels", flush=True)

    if droog:
        print(f"DROOG: {zn} {os.path.getsize(zn)/1024/1024:.1f} MB -> map {MAP}", flush=True)
        return
    d = Drive()
    mappen, _ = d.mappen()
    d.upload(zn, mappen[MAP])
    os.remove(zn)
    d.upload(idx, mappen["00"])
    os.remove(idx)


def prep():
    keuze = os.environ.get("SHARDS", "").strip()
    if keuze:
        open_ = [s.strip() for s in keuze.split(",") if s.strip()]
    else:
        d = Drive()
        mappen, _ = d.mappen()
        open_ = [s for s in decennia() if not klaar(d, mappen, s)]
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"nog te doen: {len(open_)} van {len(decennia())}")
    print(json.dumps(open_))


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "plan":
        print(json.dumps(decennia()))
    elif a[0] == "prep":
        prep()
    elif a[0] == "run":
        mx = int(a[a.index("--max") + 1]) if "--max" in a else 0
        run(a[1], mx, "--droog" in a)
