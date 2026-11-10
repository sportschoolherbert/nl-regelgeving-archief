"""Gaten-dichter: EHRM (HUDOC) en het Hof van Justitie van de EU (Cellar).

EU-wetgeving en Staatsblad/Staatscourant/Tractatenblad zitten al in archief.yml;
die worden hier bewust niet gedownload. Losstaand van archief.py, zodat beide
workflows naast elkaar kunnen draaien.

  python gaten.py plan                     -> shardlijst
  python gaten.py prep                     -> wat er nog ontbreekt in Drive
  python gaten.py run <shard> [--max N] [--droog]
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN
"""
import os, re, sys, csv, gzip, io, json, time, zipfile, concurrent.futures as cf
import requests

UA = {"User-Agent": "Mozilla/5.0 (regelgeving-archief; open data)"}
CELLAR = "https://publications.europa.eu/resource/celex/"
SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
OEP = "https://repository.overheid.nl/sru"
HUDOC = "https://hudoc.echr.coe.int/app/query/results"
HUDOC_DOC = "https://hudoc.echr.coe.int/app/conversion/docx/?library=ECHR&id="
HOOFDMAP = "Wet- en regelgeving Nederland"
MAPPEN = {
    "00": "00 Index en zoeken",
    "01": "01 Internationaal en Europees recht",
    "02": "02 Nationale wetgeving",
    "03": "03 Decentrale regels (gemeenten, provincies, waterschappen)",
    "04": "04 Jurisprudentie (rechtspraak)",
    "05": "05 Ongeschreven recht",
    "06": "06 Cao's en afspraken met wetskracht",
    "07": "07 Wetsgeschiedenis en literatuur",
}
VANDAAG = time.strftime("%Y-%m-%d")
S = requests.Session(); S.headers.update(UA)


def haal(url, accept=None, timeout=120, pogingen=4, params=None):
    h = {"Accept": accept} if accept else None
    for i in range(pogingen):
        try:
            r = S.get(url, headers=h, params=params, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1) ** 2)
    return None


def veilig(t, n=70):
    return re.sub(r"[^\w\-.]+", "_", t or "")[:n].strip("_")


# ---------- EU ----------
def celex_wetgeving(decennium, alleen_geldig=True):
    """CELEX-nummers (sector 3) van een decennium, bijv. '2020' = 2020 t/m 2029."""
    jaar = int(decennium)
    geldig = " ?w cdm:resource_legal_in-force true ." if alleen_geldig else ""
    pat = "|".join(f"3{y}" for y in range(jaar, jaar + 10))
    q = ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> "
         "SELECT DISTINCT ?c WHERE { ?w cdm:resource_legal_id_celex ?c . "
         f'FILTER(regex(str(?c), "^({pat})")) .{geldig} }}')
    for i in range(4):
        try:
            r = S.post(SPARQL, data={"query": q}, timeout=600,
                       headers={"Accept": "application/sparql-results+json"})
            if r.status_code == 200:
                return sorted({b["c"]["value"] for b in r.json()["results"]["bindings"]})
        except (requests.RequestException, KeyError, ValueError):
            pass
        time.sleep(10 * (i + 1))
    raise RuntimeError("SPARQL faalt " + decennium)


def celex_verdragen():
    q = ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> "
         "SELECT DISTINCT ?c WHERE { ?w cdm:resource_legal_id_celex ?c . "
         'FILTER(regex(str(?c), "^1")) }')
    r = S.post(SPARQL, data={"query": q}, timeout=600,
               headers={"Accept": "application/sparql-results+json"})
    r.raise_for_status()
    return sorted({b["c"]["value"] for b in r.json()["results"]["bindings"]})


def eu_document(celex):
    pad = requests.utils.quote(celex, safe="")   # haakjes in CELEX moeten %28 %29 worden
    for acc in ("application/xml;notice=object", "application/xml"):
        r = haal(CELLAR + pad, accept=acc, timeout=180, pogingen=3)
        if r is not None and len(r.content) > 400:
            return r.content
    return None


# ---------- OEP ----------
def oep_blok(pub, jaar, start, aantal):
    q = f'c.content-area=="officielepublicaties/{pub}/{jaar}"'
    r = haal(OEP, params={"operation": "searchRetrieve", "version": "1.2",
                          "maximumRecords": min(1000, aantal),
                          "startRecord": start, "query": q}, timeout=120)
    if r is None:
        return []
    out = []
    for rec in re.findall(r"<sru:record>.*?</sru:record>", r.text, re.S):
        mid = re.search(r"<dcterms:identifier>([^<]*)<", rec)
        tit = re.search(r"<dcterms:title>([^<]*)<", rec)
        url = re.search(r'itemUrl manifestation="xml">([^<]*)<', rec)
        if mid and url:
            out.append((mid.group(1), tit.group(1) if tit else "", url.group(1)))
    return out


# ---------- EHRM ----------
def hudoc_ids(jaar, start, lengte):
    r = haal(HUDOC, params={
        "query": f"contentsitename:ECHR AND kpdate:[{jaar}-01-01T00:00:00Z TO {jaar}-12-31T23:59:59Z]",
        "select": "itemid,docname", "sort": "kpdate Ascending",
        "start": start, "length": lengte}, timeout=120)
    if r is None:
        raise RuntimeError("HUDOC-API niet bereikbaar (Cloudflare/blokkade); shard niet als klaar opslaan")
    j = r.json()
    return j.get("results", []), j.get("resultcount", 0)


def hudoc_document(itemid):
    r = haal(HUDOC_DOC + itemid + "&filename=" + veilig(itemid) + ".docx", timeout=120, pogingen=3)
    return r.content if r is not None and len(r.content) > 2000 else None


# ---------- zip + index ----------
def maak_zip(zipnaam, taken, fn, threads=4):
    mislukt, ok = [], 0
    with zipfile.ZipFile(zipnaam, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(threads) as ex:
            futs = {ex.submit(fn, arg): (naam, arg) for naam, arg in taken}
            for i, fut in enumerate(cf.as_completed(futs), 1):
                naam, arg = futs[fut]
                try:
                    data = fut.result()
                except Exception:
                    data = None
                if data:
                    z.writestr(naam, data); ok += 1
                else:
                    mislukt.append(str(arg))
                if i % 2000 == 0:
                    print(f"  {i}/{len(taken)}", flush=True)
        z.writestr("_OVERZICHT.txt", f"Gedownload op {VANDAAG}\nGelukt: {ok}\nMislukt: {len(mislukt)}\n"
                   + "".join(u + "\n" for u in mislukt))
    print(f"{zipnaam}: {ok} gelukt, {len(mislukt)} mislukt", flush=True)
    return ok, mislukt


TAGS = {
    "id": ("dcterms:identifier", "dc:identifier", "identifier"),
    "titel": ("dcterms:title", "dc:title", "title", "intitule"),
    "datum": ("dcterms:date", "dcterms:modified", "dcterms:issued", "uitspraakdatum", "datum"),
}


def _pak(tekst, namen):
    for n in namen:
        m = re.search(r"<" + n + r"[^>]*>([^<]{2,400})<", tekst)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _meta(kop, shard):
    """Titel en datum; EU-XML heeft een eigen structuur (EXPRESSION_TITLE / START_OF_VALIDITY)."""
    if shard.startswith("eu"):
        t = re.search(r"<EXPRESSION_TITLE[^>]*>\s*<VALUE>(.{2,600}?)</VALUE>", kop, re.S)
        d = re.search(r"<START_OF_VALIDITY>(\d{4}-\d{2}-\d{2})", kop)
        titel = re.sub(r"\s+", " ", t.group(1)).strip() if t else ""
        return titel, (d.group(1) if d else "")
    return _pak(kop, TAGS["titel"]), _pak(kop, TAGS["datum"])


def index_van_zip(zip_pad, shard):
    uit = io.StringIO(); w = csv.writer(uit)
    w.writerow(["shard", "bestand", "id", "titel", "datum"])
    n = 0
    with zipfile.ZipFile(zip_pad) as z:
        for naam in z.namelist():
            if naam.startswith("_") or not naam.endswith((".xml", ".docx", ".xhtml")):
                continue
            kop = z.read(naam)[:60000].decode("utf8", "replace")
            titel, datum = _meta(kop, shard)
            w.writerow([shard, naam, _pak(kop, TAGS["id"]) or naam.rsplit(".", 1)[0], titel, datum])
            n += 1
    pad = zip_pad + ".index.csv.gz"
    with gzip.open(pad, "wt", encoding="utf8", newline="") as f:
        f.write(uit.getvalue())
    return pad, n


# ---------- Drive ----------
class Drive:
    def __init__(self):
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": os.environ["GD_CLIENT_ID"], "client_secret": os.environ["GD_CLIENT_SECRET"],
            "refresh_token": os.environ["GD_REFRESH_TOKEN"], "grant_type": "refresh_token"}, timeout=60)
        r.raise_for_status()
        self.h = {"Authorization": "Bearer " + r.json()["access_token"]}

    def zoek(self, naam, ouder=None, map_=False, begint=False):
        op = "contains" if begint else "="
        veilig_naam = naam.replace("'", "\\'")
        q = f"name {op} '{veilig_naam}' and trashed = false"
        if ouder: q += f" and '{ouder}' in parents"
        if map_: q += " and mimeType = 'application/vnd.google-apps.folder'"
        r = requests.get("https://www.googleapis.com/drive/v3/files", headers=self.h,
                         params={"q": q, "fields": "files(id,name)"}, timeout=60)
        r.raise_for_status()
        f = r.json()["files"]
        return f[0]["id"] if f else None

    def mappen(self):
        hoofd = self.zoek(HOOFDMAP, map_=True)
        if not hoofd:
            raise SystemExit("hoofdmap niet gevonden in Drive")
        return {k: (self.zoek(v, hoofd, True) or hoofd) for k, v in MAPPEN.items()}, hoofd

    def upload(self, pad, ouder, naam=None):
        naam = naam or os.path.basename(pad)
        size = os.path.getsize(pad)
        oud = self.zoek(naam, ouder)
        if oud:
            r = requests.patch(f"https://www.googleapis.com/upload/drive/v3/files/{oud}?uploadType=resumable",
                               headers=self.h, json={}, timeout=60)
        else:
            r = requests.post("https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable",
                              headers=self.h, json={"name": naam, "parents": [ouder]}, timeout=60)
        r.raise_for_status()
        sess = r.headers["Location"]
        blok = 64 * 1024 * 1024
        with open(pad, "rb") as f:
            pos = 0
            while pos < size:
                data = f.read(blok); eind = pos + len(data) - 1
                for i in range(6):
                    try:
                        rr = requests.put(sess, data=data, timeout=600,
                                          headers={"Content-Range": f"bytes {pos}-{eind}/{size}"})
                        if rr.status_code in (200, 201, 308):
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(10 * (i + 1))
                else:
                    raise RuntimeError("upload mislukt " + naam)
                pos = eind + 1
        print(f"geupload: {naam} ({size / 1024 / 1024:.1f} MB)", flush=True)


# ---------- shards ----------
def plan():
    """Alleen wat archief.yml niet doet: EHRM en het Hof van Justitie van de EU.

    EU-wetgeving (Cellar), Staatsblad/Staatscourant/Tractatenblad zitten al in
    archief.yml; die hier niet nog eens downloaden.
    """
    s = [f"hudoc-{y}" for y in range(1960, 2027)]
    s += [f"cjeu-{d}" for d in range(1950, 2030, 10)]
    return s


def celex_cjeu(decennium):
    """CELEX-nummers van rechtspraak (sector 6) uit een decennium."""
    jaar = int(decennium)
    pat = "|".join(f"6{y}" for y in range(jaar, jaar + 10))
    q = ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> "
         "SELECT DISTINCT ?c WHERE { ?w cdm:resource_legal_id_celex ?c . "
         f'FILTER(regex(str(?c), "^({pat})")) }}')
    for i in range(4):
        try:
            r = S.post(SPARQL, data={"query": q}, timeout=900,
                       headers={"Accept": "application/sparql-results+json"})
            if r.status_code == 200:
                return sorted({b["c"]["value"] for b in r.json()["results"]["bindings"]})
        except (requests.RequestException, KeyError, ValueError):
            pass
        time.sleep(15 * (i + 1))
    raise RuntimeError("SPARQL faalt " + decennium)


def cjeu_document(celex):
    """Arrest van het Hof; Nederlands waar beschikbaar, anders niets."""
    pad = requests.utils.quote(celex, safe="")
    for acc in ("application/xhtml+xml", "application/xml;notice=object"):
        for i in range(3):
            try:
                r = S.get(CELLAR + pad, timeout=180, allow_redirects=True,
                          headers={"Accept": acc, "Accept-Language": "nld"})
                if r.status_code == 200 and len(r.content) > 800:
                    return r.content
                if r.status_code in (404, 406):
                    break
            except requests.RequestException:
                pass
            time.sleep(5 * (i + 1))
    return None


def zipnaam(shard):
    """Naam van de zip, zonder datum, zodat herstarten hem herkent."""
    if shard.startswith("hudoc-"):
        return f"EHRM_{shard.split('-')[1]}.zip"
    if shard.startswith("cjeu-"):
        return f"EU_hof_{shard.split('-')[1]}.zip"
    raise SystemExit("onbekende shard " + shard)


def prep():
    """Kijkt in Drive welke zips er al staan en print de resterende shards."""
    d = Drive(); d.mappen()
    open_ = [sh for sh in plan() if not d.zoek(zipnaam(sh))]
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"nog te doen: {len(open_)} van {len(plan())} shards")
    print(json.dumps(open_))


def bouw(shard, max_n):
    """Geeft (zipnaam, mapcode) en maakt de zip."""
    if shard.startswith("cjeu-"):
        dec = shard.split("-")[-1]
        celex = [c for c in celex_cjeu(dec) if "/" not in c]
        if max_n: celex = celex[:max_n]
        zn = f"EU_hof_{dec}.zip"
        print(f"{shard}: {len(celex)} uitspraken", flush=True)
        maak_zip(zn, [(veilig(c) + ".xhtml", c) for c in celex], cjeu_document)
        return zn, "01"
    if shard.startswith("hudoc-"):
        jaar = shard.split("-")[-1]
        ids, start = [], 0
        while True:
            res, _ = hudoc_ids(jaar, start, 500)
            if not res: break
            ids += [(r["columns"]["itemid"], r["columns"].get("docname", "")) for r in res]
            start += len(res)
            if len(res) < 500 or (max_n and len(ids) >= max_n): break
        if max_n: ids = ids[:max_n]
        zn = f"EHRM_{jaar}.zip"
        print(f"{shard}: {len(ids)} uitspraken", flush=True)
        maak_zip(zn, [(f"{i}_{veilig(n, 60)}.docx", i) for i, n in ids], hudoc_document)
        return zn, "04"
    raise SystemExit("onbekende shard " + shard)


def run(shard, max_n, droog):
    zn, m = bouw(shard, max_n)
    idx, n = index_van_zip(zn, shard)
    print(f"index: {n} regels", flush=True)
    if droog:
        print("DROOG:", zn, f"{os.path.getsize(zn)/1024:.0f} KB ->", MAPPEN[m])
        print("DROOG:", idx, f"{os.path.getsize(idx)/1024:.0f} KB ->", MAPPEN["00"])
        return
    d = Drive(); mappen, _ = d.mappen()
    d.upload(zn, mappen[m]); os.remove(zn)
    d.upload(idx, mappen["00"]); os.remove(idx)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__); raise SystemExit
    if a[0] == "plan":
        print(json.dumps(plan()))
    elif a[0] == "prep":
        prep()
    elif a[0] == "run":
        mx = int(a[a.index("--max") + 1]) if "--max" in a else 0
        run(a[1], mx, "--droog" in a)
    else:
        print(__doc__)
