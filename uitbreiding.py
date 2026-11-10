"""Extra bronnen voor het archief, naast BWB / CVDR / rechtspraak.

  EU        -> Cellar (publications.europa.eu), content negotiation per CELEX-nummer
  OEP       -> repository.overheid.nl/sru: Staatsblad, Tractatenblad, Staatscourant
  EHRM      -> HUDOC-API, volledige tekst als .docx

Daarnaast: index_van_zip() maakt per zip een indexbestand (.csv.gz) met
id, titel en datum, zodat het archief doorzoekbaar wordt.
"""
import re, csv, gzip, io, json, time, zipfile
import requests

UA = {"User-Agent": "Mozilla/5.0 (regelgeving-archief; open data)"}
CELLAR = "https://publications.europa.eu/resource/celex/"
SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
OEP = "https://repository.overheid.nl/sru"
HUDOC = "https://hudoc.echr.coe.int/app/query/results"
HUDOC_DOC = "https://hudoc.echr.coe.int/app/conversion/docx/?library=ECHR&id="
ACCEPT_EU = ("application/xml;notice=object", "application/xml;notice=indent", "application/xml")


def sessie():
    s = requests.Session()
    s.headers.update(UA)
    return s


def haal(s, url, accept=None, timeout=120, pogingen=5):
    h = {"Accept": accept} if accept else None
    for i in range(pogingen):
        try:
            r = s.get(url, headers=h, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1) ** 2)
    return None


def veilig(t, n=80):
    return re.sub(r"[^\w\-.]+", "_", t or "")[:n].strip("_")


# ---------------- EU (Cellar) ----------------
def celex_lijst(s, decennium, alleen_geldig=True):
    """CELEX-nummers van wetgeving (sector 3) uit een decennium, bijv. '2010'."""
    geldig = " ?w cdm:resource_legal_in-force true ." if alleen_geldig else ""
    q = ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> "
         "SELECT DISTINCT ?c WHERE { ?w cdm:resource_legal_id_celex ?c . "
         f'FILTER(regex(str(?c), "^3{decennium[2:4]}")) .{geldig} }}')
    for i in range(4):
        try:
            r = s.post(SPARQL, data={"query": q}, timeout=600,
                       headers={"Accept": "application/sparql-results+json"})
            if r.status_code == 200:
                return sorted({b["c"]["value"] for b in r.json()["results"]["bindings"]})
        except (requests.RequestException, KeyError, ValueError):
            pass
        time.sleep(10 * (i + 1))
    raise RuntimeError("SPARQL faalt voor " + decennium)


def celex_verdragen(s):
    """Verdragen en oprichtingsverdragen (sector 1)."""
    q = ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> "
         "SELECT DISTINCT ?c WHERE { ?w cdm:resource_legal_id_celex ?c . "
         'FILTER(regex(str(?c), "^1")) }')
    r = s.post(SPARQL, data={"query": q}, timeout=600,
               headers={"Accept": "application/sparql-results+json"})
    r.raise_for_status()
    return sorted({b["c"]["value"] for b in r.json()["results"]["bindings"]})


def eu_document(s, celex):
    """Volledige tekst van een CELEX-nummer; None als het niet lukt."""
    for acc in ACCEPT_EU:
        try:
            r = s.get(CELLAR + celex, headers={"Accept": acc}, timeout=180, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 400:
                return r.content, r.headers.get("Content-Type", "")
        except requests.RequestException:
            pass
    return None, ""


# ---------------- Officiele Publicaties (OEP) ----------------
def oep_telling(s, pub, jaar):
    q = f'c.content-area=="officielepublicaties/{pub}/{jaar}"'
    r = haal(s, OEP, timeout=60)
    r = s.get(OEP, params={"operation": "searchRetrieve", "version": "1.2",
                           "maximumRecords": 1, "query": q}, timeout=90)
    m = re.search(r"numberOfRecords>(\d+)", r.text) if r.status_code == 200 else None
    return int(m.group(1)) if m else 0


def oep_records(s, pub, jaar, start, aantal):
    """Geeft (id, titel, xmlurl) voor een blok records."""
    q = f'c.content-area=="officielepublicaties/{pub}/{jaar}"'
    r = s.get(OEP, params={"operation": "searchRetrieve", "version": "1.2",
                           "maximumRecords": min(1000, aantal),
                           "startRecord": start, "query": q}, timeout=120)
    if r.status_code != 200:
        time.sleep(3)
        return []
    out = []
    for rec in re.findall(r"<sru:record>.*?</sru:record>", r.text, re.S):
        mid = re.search(r"<dcterms:identifier>([^<]*)<", rec)
        tit = re.search(r"<dcterms:title>([^<]*)<", rec)
        url = re.search(r'itemUrl manifestation="xml">([^<]*)<', rec)
        if mid and url:
            out.append((mid.group(1), tit.group(1) if tit else "", url.group(1)))
    return out


# ---------------- EHRM (HUDOC) ----------------
def hudoc_ids(s, van, tot, start, lengte):
    params = {"query": f"contentsitename:ECHR AND kpdate:[{van}T00:00:00Z TO {tot}T23:59:59Z]",
              "select": "itemid,docname,kpdate", "sort": "kpdate Ascending",
              "start": start, "length": lengte}
    r = s.get(HUDOC, params=params, timeout=120)
    if r.status_code != 200:
        return [], 0
    j = r.json()
    return j.get("results", []), j.get("resultcount", 0)


def hudoc_document(s, itemid):
    r = haal(s, HUDOC_DOC + itemid + "&filename=" + veilig(itemid) + ".docx", timeout=120)
    return r.content if r is not None else None


# ---------------- index per zip ----------------
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


def index_van_zip(zip_pad, shard):
    """Leest de zip en schrijft <zip>.index.csv.gz met id, titel, datum."""
    uit = io.StringIO()
    w = csv.writer(uit)
    w.writerow(["shard", "bestand", "id", "titel", "datum"])
    n = 0
    with zipfile.ZipFile(zip_pad) as z:
        for naam in z.namelist():
            if naam.startswith("_") or not naam.endswith((".xml", ".docx")):
                continue
            try:
                kop = z.read(naam)[:20000].decode("utf8", "replace")
            except Exception:
                continue
            w.writerow([shard, naam,
                        _pak(kop, TAGS["id"]) or naam.rsplit(".", 1)[0],
                        _pak(kop, TAGS["titel"]),
                        _pak(kop, TAGS["datum"])])
            n += 1
    pad = zip_pad + ".index.csv.gz"
    with gzip.open(pad, "wt", encoding="utf8", newline="") as f:
        f.write(uit.getvalue())
    return pad, n
