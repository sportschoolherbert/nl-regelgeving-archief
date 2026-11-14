"""Open-access vakliteratuur (recht) naar Drive, map "07 Wetsgeschiedenis en literatuur".

Alleen wat legaal vrij beschikbaar is: OpenAlex (Unpaywall-gegevens) wijst per werk
een open pdf aan. Geen schaduwbibliotheken, geen inlog, geen paywall-omzeiling.
robots.txt wordt gelezen en gevolgd, per server maximaal drie gelijktijdige
downloads, generieke User-Agent zonder persoonsgegevens.

Twee bronnen (elk met jaarblokken als shard):

  nlrecht-<van>-<tot>   alle Nederlandstalige werken in de rechtsgeleerdheid met een
                        open pdf (onderwerp Law of concept Law), over alle repositories
                        en tijdschriften heen (Leiden, UvA, Radboud, RUG, Maastricht,
                        Ars Aequi, Tijdschrift voor Mensenrechten, MvV, enz.)
  tijdschr-<van>-<tot>  open pdf's uit juridische tijdschriften en boekenseries uit NL en
                        BE (lijst in literatuur_oa_bronnen.json: DOAJ, OpenAlex-bronnen),
                        in elke taal (Utrecht Law Review, Erasmus Law Review, Law and
                        Method, NJLP, enz.), zonder wat al in nlrecht zit

Per zip: Literatuur_OA_<shard>_deelNNN.zip in map 07 met de pdf's ongewijzigd en een
_OVERZICHT.txt; per zip een index (.index.csv.gz) in map 00 met titel, auteurs, jaar,
tijdschrift, doi/handle, licentie, url, bytes, sha256. Mislukte downloads staan in
<zip>.mislukt.csv in map 00. Ontdubbeld op DOI, OpenAlex-id, handle en sha256, ook tegen
alle bestaande Literatuur_*.index.csv.gz in map 00.

  python literatuur_oa.py plan                  -> alle shards
  python literatuur_oa.py prep                  -> shards zonder klaar-markering in Drive
  python literatuur_oa.py run <shard> [--max N] [--droog]
  python literatuur_oa.py blokken               -> telt per jaar en stelt jaarblokken voor
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN
"""
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.robotparser
import zipfile
import concurrent.futures as cf
from urllib.parse import urlparse, urljoin

import requests

from gaten import MAPPEN, Drive, veilig

UA = {"User-Agent": "literatuur-oa/1.0"}
ROBOT_NAAM = "literatuur-oa"
OPENALEX = "https://api.openalex.org/works"
S = requests.Session()
S.headers.update(UA)

HIER = os.path.dirname(os.path.abspath(__file__))
BRONNEN = os.path.join(HIER, "literatuur_oa_bronnen.json")

MAX_BYTES = 200 * 1024 * 1024        # één pdf
ZIP_MAX = 1200 * 1024 * 1024         # daarna volgend deel
ZIP_MAX_N = 2500
THREADS = 8
PER_HOST = 3
POGINGEN = 2          # per url
HOST_PAUZE = 0.25     # seconden tussen twee verzoeken aan dezelfde server
RETRY_WACHT = 3       # seconden, vermenigvuldigd met pogingnummer

SUBFIELD_LAW = "3308"
CONCEPT_LAW = "C199539241"
VELDEN = ("id,doi,title,publication_year,language,type,authorships,primary_location,"
          "best_oa_location,locations,open_access,topics,concepts")

# Jaarblokken (zie `blokken`): elk blok blijft ruim onder ~1800 werken
NLRECHT_BLOKKEN = [(1000, 1990), (1991, 2001), (2002, 2006), (2007, 2009), (2010, 2011), (2012, 2013),
                   (2014, 2015), (2016, 2016), (2017, 2017), (2018, 2018), (2019, 2019), (2020, 2020),
                   (2021, 2022), (2023, 2024), (2025, 2100)]
TIJDSCHR_BLOKKEN = [(1000, 2001), (2002, 2009), (2010, 2013), (2014, 2016), (2017, 2018), (2019, 2019),
                    (2020, 2020), (2021, 2021), (2022, 2022), (2023, 2023), (2024, 2024), (2025, 2025),
                    (2026, 2100)]


def plan():
    return ([f"nlrecht-{a}-{b}" for a, b in NLRECHT_BLOKKEN] +
            [f"tijdschr-{a}-{b}" for a, b in TIJDSCHR_BLOKKEN])


# ---------- OpenAlex ----------

def openalex(params, pogingen=8):
    for i in range(pogingen):
        try:
            r = S.get(OPENALEX, params=params, timeout=120)
        except requests.RequestException as fout:
            print(f"  netwerkfout {type(fout).__name__}, opnieuw", flush=True)
            time.sleep(15 * (i + 1))
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (400, 401, 403):
            raise RuntimeError(f"OpenAlex {r.status_code}: {r.text[:200]}")
        wacht = int(r.headers.get("Retry-After") or 0) or min(180, 20 * (i + 1))
        print(f"  OpenAlex {r.status_code}: {r.text[:140].strip()} - {wacht}s wachten", flush=True)
        time.sleep(wacht)
    raise RuntimeError("OpenAlex blijft weigeren: " + str(params.get("filter")))


def openalex_alles(filter_, max_n=0):
    """Alle werken bij een filter, met gewone paginering (blokken blijven ruim onder 10.000)."""
    uit = []
    for pagina in range(1, 51):
        j = openalex({"filter": filter_, "per-page": 200, "page": pagina, "select": VELDEN})
        res = j.get("results", [])
        uit.extend(res)
        if len(res) < 200 or (max_n and len(uit) >= max_n):
            break
    else:
        print("  WAARSCHUWING: 10.000-grens bereikt voor " + filter_, flush=True)
    return uit[:max_n] if max_n else uit


def is_nlrecht(w):
    """Hoort dit werk bij de nlrecht-selectie? Dan hoeft tijdschr het niet te nemen."""
    if w.get("language") != "nl":
        return False
    if any((t.get("subfield") or {}).get("id", "").endswith("/" + SUBFIELD_LAW)
           for t in (w.get("topics") or [])):
        return True
    return any((c.get("id") or "").endswith(CONCEPT_LAW) for c in (w.get("concepts") or []))


def werken_voor(shard, max_n=0):
    soort, van, tot = shard.split("-")
    jaar = f"publication_year:{van}-{tot}"
    basis = f"open_access.is_oa:true,has_pdf_url:true,{jaar}"
    gezien, uit = set(), []

    def neem(lijst, filtreer=None):
        for w in lijst:
            if w["id"] in gezien or (filtreer and filtreer(w)):
                continue
            gezien.add(w["id"])
            uit.append(w)

    if soort == "nlrecht":
        for f in (f"topics.subfield.id:{SUBFIELD_LAW}", f"concepts.id:{CONCEPT_LAW}"):
            neem(openalex_alles(f"language:nl,{basis},{f}", max_n))
            print(f"  {f}: {len(uit)} werken tot nu toe", flush=True)
    elif soort == "tijdschr":
        with open(BRONNEN, encoding="utf8") as f:
            ids = sorted(json.load(f))
        for i in range(0, len(ids), 40):
            neem(openalex_alles(f"primary_location.source.id:{'|'.join(ids[i:i+40])},{basis}", max_n),
                 is_nlrecht)
            print(f"  bronnenbatch {i//40+1}: {len(uit)} werken tot nu toe", flush=True)
    else:
        raise SystemExit("onbekende shard " + shard)
    return uit[:max_n] if max_n else uit


# ---------- metadata ----------

def norm(t):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def doi_van(w):
    d = (w.get("doi") or "").lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", d)


def handle_van(w):
    for loc in w.get("locations") or []:
        for u in (loc.get("landing_page_url"), loc.get("pdf_url")):
            m = re.search(r"hdl\.handle\.net/([\w.]+/[\w.\-]+)", u or "")
            if m:
                return m.group(1)
    return ""


def auteurs_van(w):
    return "; ".join(a["author"]["display_name"] for a in (w.get("authorships") or [])
                     if a.get("author", {}).get("display_name"))


def kandidaten(w):
    """(pdf-url, licentie) uit best_oa_location en de rest, zonder dubbelen."""
    uit, gezien = [], set()
    locs = ([w["best_oa_location"]] if w.get("best_oa_location") else []) + (w.get("locations") or [])
    for loc in locs:
        u = loc.get("pdf_url")
        if u and u not in gezien and loc.get("is_oa", True):
            gezien.add(u)
            uit.append((u, loc.get("license") or ""))
    return uit[:4]


def sleutels(w, sha=None):
    k = set()
    if doi_van(w):
        k.add("doi:" + doi_van(w))
    k.add("oa:" + w["id"].rsplit("/", 1)[-1])
    if handle_van(w):
        k.add("hdl:" + handle_van(w))
    k.add(titelsleutel(w.get("title"), w.get("publication_year"), auteurs_van(w)))
    if sha:
        k.add("sha:" + sha)
    return {x for x in k if x}


def titelsleutel(titel, jaar, auteurs):
    t = norm(titel)
    if len(t) < 25 or not jaar:
        return ""
    return f"th:{t}|{jaar}|{norm((auteurs or '').split(';')[0])}"


# ---------- Drive: bestaande indexen ----------

def drive_lijst(d, ouder, begin):
    uit, token = [], None
    while True:
        d._ververs()
        p = {"q": f"'{ouder}' in parents and trashed = false and name contains '{begin}'",
             "fields": "nextPageToken,files(id,name,size)", "pageSize": 1000}
        if token:
            p["pageToken"] = token
        r = requests.get("https://www.googleapis.com/drive/v3/files", headers=d.h, params=p, timeout=60)
        r.raise_for_status()
        j = r.json()
        uit += j["files"]
        token = j.get("nextPageToken")
        if not token:
            return uit


def laad_bestaand(d, mappen, tmp):
    """Sleutels van alles wat al in Drive staat (oude Literatuur_*-indexen + eigen)."""
    sleutelset = set()
    for f in drive_lijst(d, mappen["00"], "Literatuur_"):
        if not f["name"].endswith(".index.csv.gz"):
            continue
        pad = os.path.join(tmp, "oud_" + f["id"])
        d.download(f["id"], pad)
        with gzip.open(pad, "rt", encoding="utf8", newline="") as fh:
            for r in csv.DictReader(fh):
                if "sha256" in r:                       # eigen formaat
                    if r.get("doi"):
                        sleutelset.add("doi:" + r["doi"].lower())
                    if r.get("openalex_id"):
                        sleutelset.add("oa:" + r["openalex_id"])
                    if r.get("handle"):
                        sleutelset.add("hdl:" + r["handle"])
                    if r.get("sha256"):
                        sleutelset.add("sha:" + r["sha256"])
                    sleutelset.add(titelsleutel(r.get("titel"), r.get("jaar"), r.get("auteurs")))
                else:                                   # oud formaat aanvulling.py
                    m = re.search(r"10\.\d{4,9}/\S+", r.get("url", ""))
                    if m:
                        sleutelset.add("doi:" + m.group(0).lower())
                    sleutelset.add(titelsleutel(r.get("titel"), r.get("datum"), r.get("auteurs")))
        os.remove(pad)
    sleutelset.discard("")
    return sleutelset


# ---------- downloaden ----------

_robots, _robots_lock = {}, threading.Lock()
_host_sem, _host_laatst, _host_lock = {}, {}, threading.Lock()


def robots_toegestaan(url):
    p = urlparse(url)
    sleutel = f"{p.scheme}://{p.netloc}"
    with _robots_lock:
        rp = _robots.get(sleutel)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            status = None
            for _ in range(2):
                try:
                    r = S.get(sleutel + "/robots.txt", timeout=20)
                    status = r.status_code
                    break
                except requests.RequestException:
                    time.sleep(3)
            if status == 200:
                rp.parse(r.text.splitlines())
            elif status is not None and status < 500:
                rp.parse([])                      # geen robots.txt: alles toegestaan
            else:
                rp.parse(["User-agent: *", "Disallow: /"])   # onbereikbaar of 5xx: voorzichtig
            _robots[sleutel] = rp
    return rp.can_fetch(ROBOT_NAAM, url)


def _host_wacht(host):
    with _host_lock:
        sem = _host_sem.setdefault(host, threading.Semaphore(PER_HOST))
    sem.acquire()
    with _host_lock:
        nu = time.time()
        wacht = max(0, _host_laatst.get(host, 0) + HOST_PAUZE - nu)
        _host_laatst[host] = nu + wacht
    if wacht:
        time.sleep(wacht)
    return sem


def haal_een(url, tmp):
    """Downloadt één url naar een tempbestand. -> (pad, bytes, sha256) of (None, reden, None)."""
    huidig = url
    for hop in range(6):
        if not robots_toegestaan(huidig):
            return None, "robots.txt", None
        host = urlparse(huidig).netloc
        sem = _host_wacht(host)
        try:
            r = S.get(huidig, timeout=(15, 120), stream=True, allow_redirects=False)
        except requests.RequestException as e:
            sem.release()
            return None, type(e).__name__, None
        try:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                huidig = urljoin(huidig, r.headers["Location"])
                continue
            if r.status_code != 200:
                return None, f"http {r.status_code}", None
            if int(r.headers.get("Content-Length") or 0) > MAX_BYTES:
                return None, "te groot", None
            fd, pad = tempfile.mkstemp(dir=tmp, suffix=".pdf")
            h, n, kop = hashlib.sha256(), 0, b""
            try:
                with os.fdopen(fd, "wb") as f:
                    for blok in r.iter_content(256 * 1024):
                        if n == 0:
                            kop = blok[:1024]
                        n += len(blok)
                        if n > MAX_BYTES:
                            raise ValueError("te groot")
                        h.update(blok)
                        f.write(blok)
                with open(pad, "rb") as f:
                    f.seek(max(0, n - 8192))
                    staart = f.read()
            except Exception as e:
                os.remove(pad)
                return None, type(e).__name__ + " " + str(e)[:40], None
            if b"%PDF" not in kop:
                os.remove(pad)
                return None, "geen pdf", None
            if b"%%EOF" not in staart:
                os.remove(pad)
                return None, "pdf afgekapt", None
            return pad, n, h.hexdigest()
        finally:
            r.close()
            sem.release()
    return None, "te veel redirects", None


def haal_werk(w, tmp):
    reden = "geen pdf-url"
    for url, lic in kandidaten(w):
        for poging in range(POGINGEN):
            pad, n, sha = haal_een(url, tmp)
            if pad:
                return {"pad": pad, "bytes": n, "sha": sha, "url": url, "licentie": lic}
            reden = f"{n} ({urlparse(url).netloc})"
            if n in ("robots.txt", "geen pdf", "pdf afgekapt", "te groot", "http 404", "http 403", "http 401"):
                break
            time.sleep(RETRY_WACHT * (poging + 1))
    return {"fout": reden}


# ---------- zips en indexen ----------

INDEX_KOP = ["shard", "zip", "bestand", "openalex_id", "titel", "auteurs", "jaar", "tijdschrift",
             "taal", "type", "doi", "handle", "licentie", "oa_status", "url", "bytes", "sha256"]


def schrijf_csv_gz(pad, kop, rijen):
    with gzip.open(pad, "wt", encoding="utf8", newline="") as f:
        w = csv.writer(f)
        w.writerow(kop)
        w.writerows(rijen)


def bestandsnaam(w, gebruikt):
    jaar = w.get("publication_year") or "0000"
    wid = w["id"].rsplit("/", 1)[-1]
    naam = f"{jaar}_{veilig(w.get('title') or wid, 80)}_{wid}.pdf"
    while naam in gebruikt:
        naam = naam[:-4] + "_b.pdf"
    gebruikt.add(naam)
    return naam


class DeelZip:
    """Eén zip in opbouw; sluit af met _OVERZICHT.txt, index en eventueel mislukt-lijst."""

    def __init__(self, shard, nummer, map_):
        self.shard, self.nummer = shard, nummer
        self.naam = f"Literatuur_OA_{shard}_deel{nummer:03d}.zip"
        self.pad = os.path.join(map_, self.naam)
        self.z = zipfile.ZipFile(self.pad, "w", zipfile.ZIP_STORED)
        self.rijen, self.mislukt, self.namen, self.grootte = [], [], set(), 0

    def voeg_toe(self, w, res):
        naam = bestandsnaam(w, self.namen)
        self.z.write(res["pad"], naam)
        self.grootte += res["bytes"]
        src = (w.get("primary_location") or {}).get("source") or {}
        self.rijen.append([self.shard, self.naam, naam, w["id"].rsplit("/", 1)[-1], w.get("title") or "",
                           auteurs_van(w), w.get("publication_year") or "", src.get("display_name") or "",
                           w.get("language") or "", w.get("type") or "", doi_van(w), handle_van(w),
                           res["licentie"], (w.get("open_access") or {}).get("oa_status") or "",
                           res["url"], res["bytes"], res["sha"]])
        os.remove(res["pad"])

    def sluit(self):
        regels = [f"Open-access literatuur ({self.shard}), deel {self.nummer}",
                  f"Samengesteld op {time.strftime('%Y-%m-%d')} uit OpenAlex/Unpaywall; alleen werken met een",
                  "openlijk gepubliceerde pdf. Bestanden zijn ongewijzigd. Rechten blijven bij de auteurs/uitgevers;",
                  "de licentie per werk staat in de index (kolom licentie).",
                  f"Gelukt: {len(self.rijen)}   Mislukt in deze run: {len(self.mislukt)}", ""]
        regels += [f"{r[2]} | {r[4]} | {r[5]} | {r[6]} | {r[7]} | {r[10] or r[11]} | {r[12]}" for r in self.rijen]
        self.z.writestr("_OVERZICHT.txt", "\n".join(regels) + "\n")
        self.z.close()


def run(shard, max_n, droog):
    werk = os.path.join(os.getcwd(), "literatuur_werk_" + shard)
    shutil.rmtree(werk, ignore_errors=True)
    os.makedirs(werk)
    tmp = os.path.join(werk, "tmp")
    os.makedirs(tmp)
    d, mappen, bestaand = None, None, set()
    if os.environ.get("GD_REFRESH_TOKEN"):
        d = Drive()
        mappen, _ = d.mappen()
        bestaand = laad_bestaand(d, mappen, tmp)
        print(f"bestaand in Drive: {len(bestaand)} sleutels", flush=True)
    else:
        print("geen Drive-sleutel: geen ontdubbeling tegen Drive, droog doorlopen", flush=True)
        droog = True

    if shard == "herhaal":
        global THREADS, PER_HOST, POGINGEN, RETRY_WACHT, HOST_PAUZE
        THREADS, PER_HOST, POGINGEN, RETRY_WACHT, HOST_PAUZE = 3, 1, 4, 30, 2.0
        werken = herhaal_werken(d, mappen, tmp)
        if max_n:
            werken = werken[:max_n]
    else:
        werken = werken_voor(shard, max_n)
    print(f"{shard}: {len(werken)} kandidaten", flush=True)
    nieuw = []
    for w in werken:
        if not (sleutels(w) & bestaand):
            nieuw.append(w)
    print(f"  {len(werken) - len(nieuw)} al in Drive, {len(nieuw)} te halen", flush=True)

    start = 1
    if d:
        bestaande = [f["name"] for f in drive_lijst(d, mappen["07"], f"Literatuur_OA_{shard}_deel")]
        nums = [int(m.group(1)) for n in bestaande if (m := re.search(r"_deel(\d{3})\.zip$", n))]
        start = max(nums, default=0) + 1

    def afronden(deel):
        deel.sluit()
        idx = deel.naam + ".index.csv.gz"
        ipad = os.path.join(werk, idx)
        schrijf_csv_gz(ipad, INDEX_KOP, deel.rijen)
        bestanden = [(ipad, "00")]
        if deel.mislukt:
            mpad = os.path.join(werk, deel.naam + ".mislukt.csv.gz")
            schrijf_csv_gz(mpad, ["openalex_id", "titel", "doi", "url", "reden"], deel.mislukt)
            bestanden.append((mpad, "00"))
        print(f"  {deel.naam}: {len(deel.rijen)} pdf's, {deel.grootte/1048576:.0f} MB, "
              f"{len(deel.mislukt)} mislukt", flush=True)
        if droog:
            print(f"DROOG: {deel.naam} -> {MAPPEN['07']}; {[os.path.basename(p) for p, _ in bestanden]} -> {MAPPEN['00']}")
            return
        d.upload(deel.pad, mappen["07"])
        os.remove(deel.pad)
        for p, m in bestanden:
            d.upload(p, mappen[m])
            os.remove(p)

    totaal_ok = totaal_mis = 0
    deel = DeelZip(shard, start, werk) if nieuw else None
    gezien_sha = set()
    for i in range(0, len(nieuw), 200):
        groep = nieuw[i:i + 200]
        with cf.ThreadPoolExecutor(THREADS) as ex:
            res = list(ex.map(lambda w: haal_werk(w, tmp), groep))
        for w, r in zip(groep, res):
            if "fout" in r:
                deel.mislukt.append([w["id"].rsplit("/", 1)[-1], w.get("title") or "", doi_van(w),
                                     (kandidaten(w) or [("", "")])[0][0], r["fout"]])
                totaal_mis += 1
                continue
            if r["sha"] in gezien_sha or ("sha:" + r["sha"]) in bestaand:
                os.remove(r["pad"])         # zelfde bestand, andere vindplaats
                continue
            gezien_sha.add(r["sha"])
            bestaand |= sleutels(w, r["sha"])
            if deel.grootte + r["bytes"] > ZIP_MAX or len(deel.rijen) >= ZIP_MAX_N:
                afronden(deel)
                deel = DeelZip(shard, deel.nummer + 1, werk)
            deel.voeg_toe(w, r)
            totaal_ok += 1
        print(f"  {min(i + 200, len(nieuw))}/{len(nieuw)} verwerkt ({totaal_ok} gelukt, {totaal_mis} mislukt)", flush=True)
    if deel and (deel.rijen or deel.mislukt):
        afronden(deel)
    elif deel:
        deel.z.close()
        os.remove(deel.pad)
    if d and not droog:
        marker = os.path.join(werk, f"Literatuur_OA_{shard}.klaar.txt")
        with open(marker, "w") as f:
            f.write(f"{time.strftime('%Y-%m-%d')} kandidaten={len(werken)} al_in_drive={len(werken)-len(nieuw)} "
                    f"gelukt={totaal_ok} mislukt={totaal_mis}\n")
        d.upload(marker, mappen["00"])
    print(f"KLAAR {shard}: kandidaten={len(werken)} al_in_drive={len(werken)-len(nieuw)} "
          f"gelukt={totaal_ok} mislukt={totaal_mis}", flush=True)
    shutil.rmtree(werk, ignore_errors=True)


# ---------- herhaalronde: alleen tijdelijke fouten (429/202) ----------

def herhaal_werken(d, mappen, tmp):
    ids = []
    for f in drive_lijst(d, mappen["00"], "Literatuur_OA_"):
        if not f["name"].endswith(".mislukt.csv.gz"):
            continue
        pad = os.path.join(tmp, "mis_" + f["id"])
        d.download(f["id"], pad)
        with gzip.open(pad, "rt", encoding="utf8", newline="") as fh:
            for r in csv.DictReader(fh):
                if re.match(r"http (429|202)(?!\d)", r["reden"]):
                    ids.append(r["openalex_id"])
        os.remove(pad)
    ids = sorted(set(ids))
    print(f"herhaal: {len(ids)} werken met 429/202 in de mislukt-lijsten", flush=True)
    uit = []
    for i in range(0, len(ids), 50):
        uit += openalex_alles("openalex_id:" + "|".join(ids[i:i + 50]))
    return uit


# ---------- planning ----------

def prep():
    keuze = os.environ.get("SHARDS", "").strip()
    if keuze:
        open_ = [s.strip() for s in keuze.split(",") if s.strip()]
        print("handmatige lijst:", json.dumps(open_))
    else:
        d = Drive()
        mappen, _ = d.mappen()
        klaar = {f["name"] for f in drive_lijst(d, mappen["00"], "Literatuur_OA_")
                 if f["name"].endswith(".klaar.txt")}
        open_ = [s for s in plan() if f"Literatuur_OA_{s}.klaar.txt" not in klaar]
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"nog te doen: {len(open_)} van {len(plan())} shards")
    print(json.dumps(open_))


def blokken(doel=1500):
    """Telt per jaar en pakt jaren tot blokken van hooguit `doel` werken."""
    def per_jaar(filters):
        som = {}
        for f in filters:
            j = openalex_groep(f)
            for k, v in j.items():
                som[k] = som.get(k, 0) + v         # bovengrens: overlap telt dubbel
        return som

    basis = "open_access.is_oa:true,has_pdf_url:true"
    nl = per_jaar([f"language:nl,{basis},topics.subfield.id:{SUBFIELD_LAW}",
                   f"language:nl,{basis},concepts.id:{CONCEPT_LAW}"])
    with open(BRONNEN, encoding="utf8") as f:
        ids = sorted(json.load(f))
    tj = per_jaar([f"primary_location.source.id:{'|'.join(ids[i:i+40])},{basis}"
                   for i in range(0, len(ids), 40)])
    for naam, tel in (("NLRECHT", nl), ("TIJDSCHR", tj)):
        jaren = sorted(tel)
        blok, som, uit = [], 0, []
        for j in range(jaren[0], jaren[-1] + 1):
            n = tel.get(j, 0)
            if blok and som + n > doel:
                uit.append((blok[0], blok[-1])); blok, som = [], 0
            blok.append(j); som += n
        if blok:
            uit.append((blok[0], blok[-1]))
        print(f"{naam}_BLOKKEN = {uit}   # totaal (bovengrens) {sum(tel.values())}")


def openalex_groep(filter_):
    j = openalex({"filter": filter_, "group_by": "publication_year", "per-page": 200})
    return {int(g["key"]): g["count"] for g in j["group_by"]}


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "plan":
        print(json.dumps(plan()))
    elif a[0] == "prep":
        prep()
    elif a[0] == "blokken":
        blokken()
    elif a[0] == "run":
        mx = int(a[a.index("--max") + 1]) if "--max" in a else 0
        run(a[1], mx, "--droog" in a)
    else:
        print(__doc__)
