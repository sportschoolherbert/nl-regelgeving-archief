"""EHRM-uitspraken van HUDOC als DOCX plus tekstextract.

HUDOC staat achter Cloudflare: een kale requests-client krijgt 403, een echte
browser lost de challenge op. Daarom draait alles hier door Playwright, en
praten we via de pagina zelf met de API (zelfde origin, zelfde cookies,
zelfde TLS-vingerafdruk).

Per jaar één zip in map 01. Van elke uitspraak bewaren we zowel de DOCX
(leesbaar, met opmaak) als een .txt-extract (doorzoekbaar, klein).

Eerst waren de shards decennia; die bleken te groot (2000-2009 alleen al 11.183
uitspraken à ~3,5 s per conversie = 10 uur, ver boven de 350 minuten die een
GitHub-job mag draaien). Daarom nu per jaar, met een paar rondes voor de
uitspraken die de eerste keer niet doorkomen. De decennium-zips van 1960-1999
staan er al; die jaren worden niet opnieuw gedaan en `gaten` vult aan wat daar
ontbreekt.
"""

import csv
import gzip
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


def jaren():
    # HUDOC heeft geen uitspraken van voor 1960 (Lawless, 14-11-1960 is de eerste)
    return [f"ehr-{j}" for j in range(1960, time.localtime().tm_year + 1)]


def decennium_van(shard):
    """De decennium-zip die dit jaar al dekt (1960-1999 is zo binnengehaald)."""
    return f"{int(shard.split('-')[1]) // 10 * 10}"


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


def haal_docx(ctx, itemid, pogingen=2):
    for poging in range(pogingen):
        try:
            r = ctx.request.get(DOC, params={"library": "ECHR", "id": itemid,
                                             "filename": itemid + ".docx"}, timeout=120000)
            if r.status == 200 and r.body()[:2] == b"PK":
                return r.body()
        except Exception:
            pass
        time.sleep(2 * (poging + 1))
    return None


def haal_bulk(ctx, pg, records, rondes=3):
    """DOCX van alle records, in rondes voor wat de eerste keer niet doorkomt."""
    data, open_ = {}, list(records)
    for ronde in range(1, rondes + 1):
        rest = []
        for i, c in enumerate(open_, 1):
            d = haal_docx(ctx, c["itemid"])
            if d:
                data[c["itemid"]] = d
            else:
                rest.append(c)
            if i % 100 == 0:
                print(f"  ronde {ronde}: {i}/{len(open_)} gehad, "
                      f"{len(data)} binnen", flush=True)
        print(f"  ronde {ronde}: {len(data)} binnen, {len(rest)} nog niet", flush=True)
        if not rest or ronde == rondes:
            return data, rest
        open_ = rest
        time.sleep(30)
        try:
            pg.goto(HOME, timeout=120000, wait_until="load")
            pg.wait_for_timeout(8000)
        except Exception:
            pass
    return data, open_


def lijst(ctx, pg, query, max_n=0):
    """Alle uitspraken (itemid + titel + datum) van een HUDOC-query."""
    totaal = zoek(ctx, pg, query, 0, 1).get("resultcount", 0)
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
    return totaal, records


def jaarquery(van, tot):
    return (f'contentsitename:ECHR AND doctype="HEJUD" '
            f'AND kpdate:[{van}-01-01 TO {tot}-12-31]')


def schrijf_zip(zn, mapje, records, data, mislukt, totaal, kop):
    """Zip met per uitspraak .docx + .txt, plus een overzicht en de mislukten."""
    rijen = []
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for c in records:
            itemid, docx = c["itemid"], data.get(c["itemid"])
            if not docx:
                continue
            z.writestr(f"{mapje}/{itemid}.docx", docx, zipfile.ZIP_STORED)
            try:
                z.writestr(f"{mapje}/{itemid}.txt", naar_tekst(docx), zipfile.ZIP_DEFLATED)
            except Exception:
                pass
            rijen.append((f"{itemid}.docx", {
                "id": itemid,
                "titel": " ".join(str(c.get("docname", "")).split()),
                "datum": str(c.get("kpdate", ""))[:10],
                "bron": c.get("appno", ""),
                "url": f"https://hudoc.echr.coe.int/eng?i={itemid}"}))
        z.writestr("_OVERZICHT.txt",
                   f"{kop}\n"
                   f"Verzameld op {time.strftime('%Y-%m-%d')}\n"
                   f"In HUDOC: {totaal}\nUitspraken in deze zip: {len(rijen)}\n"
                   f"Niet op te halen: {len(mislukt)}\n"
                   f"Per uitspraak: .docx (opmaak) en .txt (doorzoekbaar)\n")
        if mislukt:
            z.writestr("_MISLUKT.txt", "\n".join(
                f"{c['itemid']}\t{str(c.get('kpdate', ''))[:10]}\t"
                f"{' '.join(str(c.get('docname', '')).split())}" for c in mislukt))
    return rijen


def schrijf_index(rijen, shard, zn):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["shard", "bestand", "id", "titel", "datum", "auteurs", "bron", "url"])
    for naam, m in rijen:
        w.writerow([shard, naam, m["id"], m["titel"], m["datum"], "", m["bron"], m["url"]])
    idx = zn + ".index.csv.gz"
    with gzip.open(idx, "wt", encoding="utf8", newline="") as f:
        f.write(buf.getvalue())
    print(f"index: {len(rijen)} regels", flush=True)
    return idx


def browser():
    p = __import__("playwright.sync_api", fromlist=["sync_playwright"]).sync_playwright().start()
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(locale="nl-NL")
    pg = ctx.new_page()
    pg.goto(HOME, timeout=120000, wait_until="load")
    pg.wait_for_timeout(8000)
    return p, b, ctx, pg


def zipnaam(shard):
    return f"EHRM_uitspraken_{shard.split('-')[1]}.zip"


def klaar(d, mappen, shard):
    if d.zoek(zipnaam(shard), mappen[MAP]) is not None:
        return True
    # 1960-1999 is per decennium binnen; `gaten` vult aan wat daar ontbreekt.
    return int(shard.split("-")[1]) < 2000 and \
        d.zoek(f"EHRM_uitspraken_{decennium_van(shard)}.zip", mappen[MAP]) is not None


def run(shard, max_n, droog):
    jaar = int(shard.split("-")[1])
    query = jaarquery(jaar, jaar)
    p, b, ctx, pg = browser()
    try:
        totaal, records = lijst(ctx, pg, query, max_n)
        print(f"{shard}: {totaal} uitspraken", flush=True)
        data, mislukt = haal_bulk(ctx, pg, records)
    finally:
        b.close()
        p.stop()

    zn = zipnaam(shard)
    rijen = schrijf_zip(zn, str(jaar), records, data, mislukt, totaal,
                        f"EHRM-uitspraken {jaar}")
    print(f"{zn}: {len(rijen)} uitspraken, {len(mislukt)} mislukt van {totaal}", flush=True)
    idx = schrijf_index(rijen, shard, zn)

    if droog:
        print(f"DROOG: {zn} {os.path.getsize(zn)/1024/1024:.1f} MB -> map {MAP}", flush=True)
        return
    d = Drive()
    mappen, _ = d.mappen()
    d.upload(zn, mappen[MAP])
    os.remove(zn)
    d.upload(idx, mappen["00"])
    os.remove(idx)


def gaten(shard):
    """Vult een decennium-zip aan met wat de eerste keer niet doorkwam.

    De index van de bestaande zip zegt wat we hebben; de rest halen we uit
    HUDOC en zetten we in één aanvulzip per decennium.
    """
    dec = shard.split("-")[1]
    van, tot = int(dec), int(dec) + 9
    d = Drive()
    mappen, _ = d.mappen()
    idx = f"EHRM_uitspraken_{dec}.zip.index.csv.gz"
    fid = d.zoek(idx, mappen["00"])
    if not fid:
        raise SystemExit("geen index in Drive voor " + dec)
    d.download(fid, idx)
    hebben = set()
    with gzip.open(idx, "rt", encoding="utf8") as f:
        for r in csv.DictReader(f):
            hebben.add(r["id"])
    os.remove(idx)
    print(f"{dec}: {len(hebben)} in de index", flush=True)

    query = jaarquery(van, tot)
    p, b, ctx, pg = browser()
    try:
        totaal, records = lijst(ctx, pg, query)
        mis = [c for c in records if c["itemid"] not in hebben]
        print(f"{dec}: {totaal} in HUDOC, {len(mis)} ontbreken", flush=True)
        data, mislukt = haal_bulk(ctx, pg, mis)
    finally:
        b.close()
        p.stop()

    zn = f"EHRM_aanvulling_{dec}.zip"
    rijen = schrijf_zip(zn, f"{van}-{tot}", mis, data, mislukt, len(mis),
                        f"EHRM-uitspraken {van}-{tot}, aanvulling")
    if not rijen:
        print("niets aan te vullen", flush=True)
        return
    print(f"{zn}: {len(rijen)} uitspraken, {len(mislukt)} mislukt van {len(mis)}", flush=True)
    idx = schrijf_index(rijen, shard, zn)
    d.upload(zn, mappen[MAP])
    os.remove(zn)
    d.upload(idx, mappen["00"])
    os.remove(idx)


def prep():
    keuze = os.environ.get("SHARDS", "").strip()
    d = Drive()
    mappen, _ = d.mappen()
    if keuze:
        open_ = [s.strip() for s in keuze.split(",") if s.strip()]
    else:
        open_ = [s for s in jaren() if not klaar(d, mappen, s)]
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"nog te doen: {len(open_)} van {len(jaren())}")
    print(json.dumps(open_))


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "plan":
        print(json.dumps(jaren()))
    elif a[0] == "prep":
        prep()
    elif a[0] == "gaten":
        gaten(a[1])
    elif a[0] == "run":
        mx = int(a[a.index("--max") + 1]) if "--max" in a else 0
        run(a[1], mx, "--droog" in a)
