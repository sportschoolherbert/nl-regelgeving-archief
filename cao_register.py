"""CAO-register van het ministerie van SZW (https://cao.minszw.nl): alle aangemelde cao's
en sociale plannen met hun documenten (cao-tekst, wijzigingen, aanmeldingen).

Route: het register (Mozard) staat achter Anubis (proof-of-work), dus een echte browser
(Playwright). De zoeklijst van het register kapt af op een willekeurig aantal (999 of minder,
wisselt per aanvraag) en is dus niet betrouwbaar. De detailpagina `scherm1007?mObj=<n>` is
dat wel, voor elk objectnummer. Daarom lopen we alle objectnummers af in vaste blokken
(shards van BLOK nummers) en bewaren elk object dat bij een cao-registratie hoort of
documenten heeft. Zo is volledigheid controleerbaar: elk nummer is geprobeerd.

  python cao_register.py prep                 -> shards (GITHUB_OUTPUT shards=...), slaat shards over die in Drive staan
  python cao_register.py run <shard> [--droog] [--max N]
Een shard heet cao-NNN (blok NNN = objectnummers NNN*BLOK .. (NNN+1)*BLOK-1).
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN
"""
import csv
import gzip
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import zipfile

BASIS = "https://cao.minszw.nl/mozard/"
START = BASIS + "!suite92.scherm1007?mObj=6939"
MAP = "06"
BLOK = 1000
MAX_ID = 70000          # ruim boven het hoogste cao-object (57407 op 14-11-2026)
PAUZE = 0.15            # beleefd: korte pauze tussen twee verzoeken, een verzoek tegelijk
REGISTRATIES = ("aangemelde cao", "aan- of afmelden", "cao")


# Blokken met veel documenten (de job haalde de tijdslimiet niet) lopen als vier sub-shards
# van 250 nummers: cao-003-1 .. cao-003-4, zip CAO_register_deel003_1.zip .. _4.zip.
# Een blok geldt als klaar met een gewone zip OF met alle vier de sub-zips.
SPLITSEN = {3, 15, 16, 19, 20, 23, 24, 26}
DEEL = 4
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "300")) * 60   # GitHub breekt na 350 min af
START_TIJD = time.time()


def splits(n):
    return [f"cao-{n:03d}-{k}" for k in range(1, DEEL + 1)]


def shards():
    uit = []
    for n in range((MAX_ID + BLOK - 1) // BLOK):
        uit += splits(n) if n in SPLITSEN else [f"cao-{n:03d}"]
    return uit


def zipnaam(shard):
    d = shard.split("-")
    return f"CAO_register_deel{int(d[1]):03d}" + (f"_{d[2]}" if len(d) > 2 else "") + ".zip"


def bereik(shard):
    d = shard.split("-")
    n = int(d[1])
    if len(d) > 2:
        w = BLOK // DEEL
        van = n * BLOK + (int(d[2]) - 1) * w
        return van, min(van + w, MAX_ID)
    return n * BLOK, min((n + 1) * BLOK, MAX_ID)


def schoon(t):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", t or "")).split())


def parse(h):
    """Detailpagina -> metadata + documentlijst, of None als het geen toegankelijk object is."""
    titel = re.search(r"<title>MinSZW \| Directie Cao - (.*?)</title>", h, re.S)
    if not titel:
        return None
    velden = {}
    for k, v in re.findall(r'<dt class="prompttekst">(.*?)</dt><dd[^>]*>(.*?)</dd>', h, re.S):
        velden.setdefault(schoon(k), schoon(v))
    docs = []
    for m in re.finditer(r'<a href="(document/docnr/[^"]+)"[^>]*>(.*?)</a>', h, re.S):
        blok = m.group(2)
        nm = re.search(r'mozdoc_docnm">(.*?)</span>', blok, re.S)
        tp = re.search(r'mozdoc_doctype_regel">(.*?)</span>', blok, re.S)
        ext = re.search(r'sr-only">(.*?)</span>', blok, re.S)
        docs.append({"href": html.unescape(m.group(1)),
                     "naam": schoon(nm.group(1)) if nm else "",
                     "soort": schoon(tp.group(1)) if tp else "",
                     "ext": schoon(ext.group(1)) if ext else ""})
    return {"titel": schoon(titel.group(1)), "velden": velden, "docs": docs}


def browser():
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(locale="nl-NL")
    pg = ctx.new_page()
    open_pagina(pg)
    return p, b, ctx, pg


def open_pagina(pg):
    """Laat de browser de proof-of-work van Anubis oplossen."""
    for poging in range(4):
        try:
            pg.goto(START, timeout=120000, wait_until="domcontentloaded")
            for _ in range(40):
                if pg.title().startswith("MinSZW"):
                    return
                pg.wait_for_timeout(1500)
        except Exception as e:
            print("  openen mislukt:", str(e)[:80], flush=True)
        time.sleep(10)
    raise RuntimeError("Anubis niet doorgekomen")


JS_DETAIL = """async (o) => { const r = await fetch('/mozard/!suite92.scherm1007?mObj=' + o);
  return [r.status, await r.text()]; }"""


def detail(pg, oid):
    for poging in range(6):
        try:
            st, h = pg.evaluate(JS_DETAIL, oid)
            if "anubis_challenge" in h or st in (429, 503):
                print(f"  challenge/{st} bij {oid}, opnieuw", flush=True)
                time.sleep(15 * (poging + 1))
                open_pagina(pg)
                continue
            return h
        except Exception as e:
            print(f"  fout bij {oid}: {str(e)[:80]}", flush=True)
            time.sleep(5 * (poging + 1))
            try:
                open_pagina(pg)
            except Exception:
                pass
    return None


def is_html(b):
    return b[:200].lstrip().lower().startswith((b"<!doctype html", b"<html"))


_DOCPAGE = {}


def haal_doc(ctx, pg, href):
    """Document ophalen via een echte navigatie: Anubis eist dat, en het document zelf staat op
    een andere host (storage.mozardsaas.nl, zonder CORS), dus fetch/APIRequest werken niet."""
    url = BASIS + href
    for poging in range(4):
        dp = _DOCPAGE.get("p")
        if dp is None or dp.is_closed():
            dp = _DOCPAGE["p"] = ctx.new_page()
        antw = []
        def onr(r, antw=antw):
            antw.append(r)
        dp.on("response", onr)
        try:
            try:
                with dp.expect_download(timeout=90000) as dl:
                    try:
                        dp.goto(url, timeout=90000)
                    except Exception:
                        pass
                tmp = f"_dl_{os.getpid()}.bin"
                dl.value.save_as(tmp)
                with open(tmp, "rb") as f:
                    data = f.read()
                os.remove(tmp)
                if data and not is_html(data):
                    return url, data
            except Exception:
                # inline getoonde bestanden (geen download): neem de laatste niet-html-respons
                for r in reversed(antw):
                    ct = r.headers.get("content-type", "")
                    if r.status == 200 and not ct.startswith(("text/html", "text/css", "text/javascript", "application/json")):
                        data = r.body()
                        if data and not is_html(data):
                            return url, data
        except Exception:
            pass
        finally:
            try:
                dp.remove_listener("response", onr)
            except Exception:
                pass
        time.sleep(3 * (poging + 1))
    return url, None


def bestandsnaam(naam):
    return re.sub(r"[^\w\-. ()]+", "_", naam).strip(" ._")[:80] or "document"


def run(shard, max_n, droog):
    van, tot = bereik(shard)
    p, b, ctx, pg = browser()
    rijen, mislukt, gezien, toegankelijk, objecten = [], [], 0, 0, 0
    zn = zipnaam(shard)
    t0 = time.time()
    try:
        with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for oid in range(van, tot):
                if max_n and gezien >= max_n:
                    break
                if time.time() - START_TIJD > GRENS_SEC:
                    # tijdgrens: niet meer aan nieuwe objecten beginnen; rest komt in .mislukt.csv
                    mislukt.append((oid, "", f"tijd: nummers {oid} t/m {tot - 1} niet meer aan begonnen"))
                    print(f"  tijdgrens bereikt bij {oid}", flush=True)
                    break
                gezien += 1
                h = detail(pg, oid)
                if h is None:
                    mislukt.append((oid, "", "detailpagina niet op te halen"))
                    continue
                d = parse(h)
                time.sleep(PAUZE)
                if not d:
                    continue
                toegankelijk += 1
                v = d["velden"]
                reg = v.get("Registratie", "")
                if not d["docs"] and not reg.lower().startswith(REGISTRATIES):
                    continue
                objecten += 1
                sector = " | ".join(x for x in (v.get("Soort cao", ""),
                                                ("SBI " + v["SBI-code"]) if v.get("SBI-code") else "") if x)
                basis = {"cao_id": oid, "naam": d["titel"], "registratie": reg,
                         "sector": sector, "type": v.get("Type cao", ""),
                         "looptijd": f"{v.get('Ingangsdatum', '')} t/m {v.get('Expiratiedatum', '')}",
                         "kvo": v.get("Datum formele Kennisgeving van Ontvangst", ""),
                         "pagina": f"{BASIS}!suite92.scherm1007?mObj={oid}"}
                if not d["docs"]:
                    rijen.append({**basis, "documentsoort": "(geen document)", "bestand": "",
                                  "url": "", "bytes": 0, "sha256": ""})
                for i, doc in enumerate(d["docs"], 1):
                    url, data = haal_doc(ctx, pg, doc["href"])
                    if data is None:
                        mislukt.append((oid, url, "document niet op te halen"))
                        continue
                    ext = os.path.splitext(urllib.parse.unquote(doc["href"].split("/")[-1]))[1] \
                        or ("." + doc["ext"] if doc["ext"] else "")
                    pad = f"{oid}/{i:02d}_{bestandsnaam(doc['naam'])}{ext}"
                    z.writestr(pad, data, zipfile.ZIP_STORED)
                    rijen.append({**basis, "documentsoort": doc["soort"] or doc["ext"] or "document",
                                  "bestand": pad, "url": url, "bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()})
                if objecten % 25 == 0:
                    print(f"  {shard}: nummer {oid} ({gezien}/{tot - van}), {objecten} objecten, "
                          f"{len(rijen)} regels, {len(mislukt)} mislukt, {round(time.time() - t0)} s", flush=True)
            z.writestr("_OVERZICHT.txt",
                       f"CAO-register SZW (cao.minszw.nl), objectnummers {van} t/m {tot - 1}\n"
                       f"Verzameld op {time.strftime('%Y-%m-%d')}\n"
                       f"Nummers geprobeerd: {gezien}\nToegankelijke objecten: {toegankelijk}\n"
                       f"Objecten bewaard (cao-registratie of met document): {objecten}\n"
                       f"Documenten in deze zip: {sum(1 for r in rijen if r['bestand'])}\n"
                       f"Niet op te halen: {len(mislukt)}\n"
                       f"Bestanden ongewijzigd opgeslagen; per object een map met het objectnummer.\n"
                       f"Index: {zn}.index.csv.gz in map 00.\n")
            if mislukt:
                z.writestr("_MISLUKT.txt", "\n".join("\t".join(map(str, m)) for m in mislukt))
    finally:
        b.close()
        p.stop()

    idx = zn + ".index.csv.gz"
    kol = ["cao_id", "naam", "registratie", "sector", "type", "looptijd", "kvo",
           "documentsoort", "bestand", "url", "bytes", "sha256", "pagina"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["shard"] + kol)
    for r in rijen:
        w.writerow([shard] + [r[k] for k in kol])
    with gzip.open(idx, "wt", encoding="utf8", newline="") as f:
        f.write(buf.getvalue())
    mis = None
    if mislukt:
        mis = zn + ".mislukt.csv"
        with open(mis, "w", encoding="utf8", newline="") as f:
            cw = csv.writer(f)
            cw.writerow(["cao_id", "url", "reden"])
            cw.writerows(mislukt)
    print(f"{zn}: {gezien} nummers, {toegankelijk} toegankelijk, {objecten} objecten, "
          f"{sum(1 for r in rijen if r['bestand'])} documenten, {len(mislukt)} mislukt, "
          f"{os.path.getsize(zn) / 1024 / 1024:.1f} MB", flush=True)
    if droog:
        print("DROOG: geen upload", flush=True)
        return
    from gaten import Drive
    d = Drive()
    mappen, _ = d.mappen()
    d.upload(zn, mappen[MAP])
    os.remove(zn)
    d.upload(idx, mappen["00"])
    os.remove(idx)
    if mis:
        d.upload(mis, mappen["00"])
        os.remove(mis)


def prep():
    keuze = os.environ.get("SHARDS", "").strip()
    if keuze:
        open_ = [s.strip() for s in keuze.split(",") if s.strip()]
    else:
        from gaten import Drive
        d = Drive()
        mappen, _ = d.mappen()
        open_ = [s for s in shards() if d.zoek(zipnaam(s), mappen[MAP]) is None]
        # een gewone zip van een gesplitst blok telt ook als klaar
        open_ = [s for s in open_ if len(s.split("-")) < 3 or
                 d.zoek(zipnaam("-".join(s.split("-")[:2])), mappen[MAP]) is None]
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"nog te doen: {len(open_)} van {len(shards())}")
    print(json.dumps(open_))


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "plan":
        print(json.dumps(shards()))
    elif a[0] == "prep":
        prep()
    elif a[0] == "run":
        mx = int(a[a.index("--max") + 1]) if "--max" in a else 0
        run(a[1], mx, "--droog" in a)
