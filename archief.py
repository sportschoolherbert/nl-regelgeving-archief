"""Download Nederlandse wet- en regelgeving + rechtspraak in zips en zet ze in Google Drive.

Gebruik:
  python archief.py plan                 -> print de shardlijst (JSON) voor de GitHub-matrix
  python archief.py prep                 -> maakt de mappen in Drive, print map-ID's (JSON)
  python archief.py run <shard> [--droog] [--max N]
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN (Drive, scope drive.file), MAPPEN (JSON van prep).
Elke shard wordt overgeslagen als de zip al in Drive staat, dus opnieuw draaien is veilig.
"""
import os, sys, re, json, time, zipfile, io, concurrent.futures as cf
import requests
import uitbreiding as ub

UA = {"User-Agent": "Mozilla/5.0 (regelgeving-archief; open data)"}
SRU = "https://zoekservice.overheid.nl/sru/Search"
RS = "https://data.rechtspraak.nl/uitspraken"
VANDAAG = time.strftime("%Y-%m-%d")
HOOFDMAP = "Wet- en regelgeving Nederland"
MAPPEN = {
    "01": "01 Internationaal en Europees recht",
    "02": "02 Nationale wetgeving",
    "03": "03 Decentrale regels (gemeenten, provincies, waterschappen)",
    "04": "04 Jurisprudentie (rechtspraak)",
    "05": "05 Ongeschreven recht",
    "06": "06 Cao's en afspraken met wetskracht",
    "07": "07 Wetsgeschiedenis en literatuur",
    "08": "08 Officiele bekendmakingen (Staatsblad, Staatscourant, Tractatenblad)",
}
EU_SHARDS = {"eu-1": "^1", "eu-3-19": "^319", "eu-3-200": "^3200", "eu-3-201": "^3201", "eu-3-202": "^3202"}
OEP_START = {"stb": 1951, "trb": 1951, "stcrt": 1995}  # stb: SRU heeft niets van voor 1951
CVDR_TOTAAL_BLOK = 15000
s = requests.Session(); s.headers.update(UA)


def haal(url, params=None, pogingen=5, bin=False):
    for i in range(pogingen):
        try:
            r = s.get(url, params=params, timeout=120)
            if r.status_code == 200:
                return r.content if bin else r.text
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1) ** 2)
    return None


# ---------- plan ----------
def plan():
    shards = ["bwb"]
    x = haal(SRU, {"operation": "searchRetrieve", "version": "1.2", "x-connection": "cvdr",
                   "maximumRecords": 1, "query": "cql.allRecords=1"})
    n = int(re.search(r"<numberOfRecords>(\d+)", x).group(1))
    shards += [f"cvdr-{i:02d}" for i in range(n // CVDR_TOTAAL_BLOK + 1)]
    shards += ["rs-tot1999"] + [f"rs-{y}" for y in range(2000, int(VANDAAG[:4]) + 1)]
    shards += list(EU_SHARDS) + ["oep-trb"]
    # Staatsblad in blokken van vijf jaar: de PDF's van voor 1995 zijn samen
    # ~13 GB en passen niet als één zip op een runner. Vanaf 1995 is het XML
    # en klein genoeg voor één shard.
    shards += [f"oep-stb-{j}-{min(j + 4, 1994)}" for j in range(OEP_START["stb"], 1995, 5)]
    shards += [f"oep-stb-1995-{int(VANDAAG[:4])}"]
    shards += [f"oep-stcrt-{y}" for y in range(OEP_START["stcrt"], int(VANDAAG[:4]) + 1)]
    return shards


# ---------- Drive ----------
class Drive:
    def __init__(self):
        self._ververs(forceer=True)

    def _ververs(self, forceer=False):
        """Google-toegangstokens verlopen na een uur.

        Een shard die eerst lang downloadt en daarna pas uploadt kreeg een
        401 Unauthorized bij de upload (oep-stb na 1u45m, oep-trb na 1u22m).
        Daarom wordt het token hier elke 50 minuten vernieuwd.
        """
        if not forceer and time.time() < getattr(self, "_geldig_tot", 0):
            return
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": os.environ["GD_CLIENT_ID"], "client_secret": os.environ["GD_CLIENT_SECRET"],
            "refresh_token": os.environ["GD_REFRESH_TOKEN"], "grant_type": "refresh_token"}, timeout=60)
        r.raise_for_status()
        self.h = {"Authorization": "Bearer " + r.json()["access_token"]}
        self._geldig_tot = time.time() + 3000

    def zoek(self, naam, ouder=None, map_=False, begint=False):
        self._ververs()
        op = "contains" if begint else "="
        q = f"name {op} '{naam.replace(chr(39), chr(92) + chr(39))}' and trashed = false"
        if ouder: q += f" and '{ouder}' in parents"
        if map_: q += " and mimeType = 'application/vnd.google-apps.folder'"
        r = requests.get("https://www.googleapis.com/drive/v3/files", headers=self.h,
                         params={"q": q, "fields": "files(id,name)"}, timeout=60)
        r.raise_for_status(); f = r.json()["files"]
        return f[0]["id"] if f else None

    def map(self, naam, ouder=None):
        fid = self.zoek(naam, ouder, True)
        if fid: return fid
        meta = {"name": naam, "mimeType": "application/vnd.google-apps.folder"}
        if ouder: meta["parents"] = [ouder]
        r = requests.post("https://www.googleapis.com/drive/v3/files", headers=self.h, json=meta, timeout=60)
        r.raise_for_status(); return r.json()["id"]

    def lijst(self, ouder, begint=""):
        self._ververs()
        uit, tok = [], None
        while True:
            r = requests.get("https://www.googleapis.com/drive/v3/files", headers=self.h, timeout=60, params={
                "q": f"'{ouder}' in parents and trashed = false and name contains '{begint}'",
                "fields": "nextPageToken,files(id,name,size)", "pageSize": 1000, "pageToken": tok})
            r.raise_for_status(); j = r.json(); uit += j["files"]; tok = j.get("nextPageToken")
            if not tok: return [f for f in uit if f["name"].startswith(begint)]

    def download(self, fid, pad):
        self._ververs()
        for i in range(5):
            try:
                with requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}?alt=media", headers=self.h,
                                  stream=True, timeout=600) as r:
                    r.raise_for_status()
                    with open(pad, "wb") as f:
                        for b in r.iter_content(8 * 1024 * 1024): f.write(b)
                return
            except requests.RequestException:
                time.sleep(20 * (i + 1))
        raise RuntimeError("download mislukt " + pad)

    def upload(self, pad, ouder, naam=None):
        naam = naam or os.path.basename(pad)
        oud = self.zoek(naam, ouder)
        size = os.path.getsize(pad)
        if oud:  # vervangen (bijv. LEESMIJ)
            url = f"https://www.googleapis.com/upload/drive/v3/files/{oud}?uploadType=resumable"
            r = requests.patch(url, headers=self.h, json={}, timeout=60)
        else:
            url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
            r = requests.post(url, headers=self.h, json={"name": naam, "parents": [ouder]}, timeout=60)
        r.raise_for_status(); sess = r.headers["Location"]
        blok = 64 * 1024 * 1024
        with open(pad, "rb") as f:
            pos = 0
            while pos < size:
                data = f.read(blok); eind = pos + len(data) - 1
                for i in range(6):
                    try:
                        r = requests.put(sess, data=data, timeout=600, headers={
                            "Content-Range": f"bytes {pos}-{eind}/{size}"})
                        if r.status_code in (200, 201, 308): break
                    except requests.RequestException:
                        pass
                    time.sleep(10 * (i + 1))
                else:
                    raise RuntimeError("upload mislukt " + naam)
                pos = eind + 1
        print(f"geüpload: {naam} ({size/1024/1024:.1f} MB)", flush=True)


# ---------- ophalen ----------
def sru_records(conn, query, start, aantal):
    """Geeft (start, recordxml) voor records start..start+aantal-1."""
    eind = start + aantal
    while start < eind:
        x = haal(SRU, {"operation": "searchRetrieve", "version": "1.2", "x-connection": conn,
                       "maximumRecords": min(1000, eind - start), "startRecord": start, "query": query})
        if not x: raise RuntimeError(f"SRU faalt bij {start}")
        recs = re.findall(r"<record>.*?</record>", x, re.S)
        if not recs: break
        yield from recs
        start += len(recs)


def veilig(t, n=90):
    return re.sub(r"[^\w\-.]+", "_", t)[:n].strip("_")


def parallel_naar_zip(zipnaam, taken, threads=4):
    """taken: lijst (bestandsnaam_in_zip, url). Schrijft zip, geeft (ok, mislukt)."""
    mislukt = []; ok = 0
    with zipfile.ZipFile(zipnaam, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(threads) as ex:
            futs = {ex.submit(haal, u, None, 5, True): (n, u) for n, u in taken}
            for i, fut in enumerate(cf.as_completed(futs), 1):
                n, u = futs[fut]; data = fut.result()
                if data: z.writestr(n, data); ok += 1
                else: mislukt.append(u)
                if i % 2000 == 0: print(f"  {i}/{len(taken)}", flush=True)
        z.writestr("_OVERZICHT.txt", f"Gedownload op {VANDAAG}\nGelukt: {ok}\nMislukt: {len(mislukt)}\n" +
                   "".join(u + "\n" for u in mislukt))
    print(f"{zipnaam}: {ok} gelukt, {len(mislukt)} mislukt", flush=True)
    return ok, mislukt


def shard_bwb(max_n):
    q = f"overheidbwb.geldigheidsdatum=={VANDAAG}"
    per_type = {}
    for rec in sru_records("BWB", q, 1, max_n or 10**6):
        bid = re.search(r"<dcterms:identifier>([^<]*)", rec)
        url = re.search(r"<overheidbwb:locatie_toestand>([^<]*)", rec)
        typ = re.search(r"<dcterms:type>([^<]*)", rec)
        tit = re.search(r"<dcterms:title>([^<]*)", rec)
        if not (bid and url): continue
        t = typ.group(1) if typ else "overig"
        naam = f"{bid.group(1)}_{veilig(tit.group(1) if tit else '', 60)}.xml"
        per_type.setdefault(t, {})[naam] = url.group(1)
    uit = []
    for t, d in sorted(per_type.items()):
        zn = f"BWB_{veilig(t)}_geldig_{VANDAAG}.zip"
        parallel_naar_zip(zn, list(d.items()), threads=6)
        uit.append((zn, "01" if t in ("verdrag", "rijkswet") else "02"))
    return uit


def shard_cvdr(i, max_n):
    start = i * CVDR_TOTAAL_BLOK + 1
    taken = {}
    for rec in sru_records("cvdr", "cql.allRecords=1", start, max_n or CVDR_TOTAAL_BLOK):
        url = re.search(r"<publicatieurl_xml>([^<]*)", rec)
        cid = re.search(r"<dcterms:identifier>([^<]*)", rec)
        cre = re.search(r"<dcterms:creator[^>]*>([^<]*)", rec)
        if not url: continue
        org = veilig(cre.group(1) if cre else "onbekend", 50)
        taken[f"{org}/{veilig(cid.group(1) if cid else url.group(1)[-30:])}.xml"] = url.group(1)
    zn = f"CVDR_lokaal_blok{i:02d}_records_{start}-{start + CVDR_TOTAAL_BLOK - 1}.zip"
    parallel_naar_zip(zn, list(taken.items()))
    return [(zn, "03")]


def rs_eclis(van, tot, max_n):
    eclis, frm = [], 0
    while True:
        x = haal(f"{RS}/zoeken", {"return": "DOC", "max": 1000, "from": frm, "date": [van, tot]})
        if x is None: raise RuntimeError(f"rechtspraak zoeken faalt {van} from {frm}")
        e = re.findall(r"<id>(ECLI:[^<]*)</id>", x)
        eclis += e; frm += 1000
        if len(e) < 1000 or (max_n and len(eclis) >= max_n): break
    return eclis[:max_n] if max_n else eclis


def shard_rs(naam, max_n):
    jaar = naam[3:]
    van, tot = ("1000-01-01", "1999-12-31") if jaar == "tot1999" else (f"{jaar}-01-01", f"{jaar}-12-31")
    eclis = sorted(set(rs_eclis(van, tot, max_n)))
    taken = [(e.replace(":", "_") + ".xml", f"{RS}/content?id={e}") for e in eclis]
    zn = f"Rechtspraak_{jaar}.zip"
    parallel_naar_zip(zn, taken)
    return [(zn, "04")]


def shard_eu(naam, max_n):
    import uitbreiding as u
    rx = EU_SHARDS[naam]
    q = ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> SELECT DISTINCT ?c WHERE { "
         "?w cdm:resource_legal_id_celex ?c . ?w cdm:resource_legal_in-force true . "
         f'FILTER(regex(str(?c), "{rx}")) }}')
    for i in range(4):
        try:
            r = s.post("https://publications.europa.eu/webapi/rdf/sparql", data={"query": q}, timeout=900,
                       headers={"Accept": "application/sparql-results+json"})
            celex = sorted({b["c"]["value"] for b in r.json()["results"]["bindings"]}); break
        except Exception:
            time.sleep(30 * (i + 1))
    else:
        raise RuntimeError("SPARQL faalt " + rx)
    if max_n: celex = celex[:max_n]
    zn = f"EU_{naam[3:]}_geldig.zip"
    mislukt, ok = [], 0
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        def een(c):
            for i in range(3):
                try:
                    r = s.get(u.CELLAR + requests.utils.quote(c, safe=""), timeout=180, headers={"Accept": "application/xhtml+xml", "Accept-Language": "nld"})
                    if r.status_code == 200 and len(r.content) > 400: return r.content
                    if r.status_code in (404, 406): return None
                except requests.RequestException:
                    pass
                time.sleep(5 * (i + 1))
        with cf.ThreadPoolExecutor(4) as ex:
            for c, data in zip(celex, ex.map(een, celex)):
                if data: z.writestr(veilig(c) + ".xhtml", data); ok += 1
                else: mislukt.append(c)
        nl = chr(10)
        z.writestr("_OVERZICHT.txt", f"Gedownload op {VANDAAG}{nl}CELEX-filter {rx}, alleen geldend{nl}Gelukt: {ok}{nl}"
                   f"Niet beschikbaar in het Nederlands of mislukt: {len(mislukt)}{nl}" + nl.join(mislukt))
    print(f"{zn}: {ok} gelukt, {len(mislukt)} mislukt", flush=True)
    return [(zn, "01")]


def oep_bestandsnaam(naam):
    """OEP_stb_1951-1955.zip voor 'oep-stb-1951-1955', OEP_trb_alle_jaren.zip voor 'oep-trb'."""
    de = naam.split("-")
    return f"OEP_{de[1]}_{'-'.join(de[2:]) if len(de) > 2 else 'alle_jaren'}.zip"


def shard_oep(naam, max_n):
    import uitbreiding as u
    delen = naam.split("-"); pub = delen[1]
    if len(delen) > 3:      # bereik, bijv. oep-stb-1951-1955
        jaren = range(int(delen[2]), int(delen[3]) + 1)
    elif len(delen) > 2:    # een enkel jaar
        jaren = [int(delen[2])]
    else:
        jaren = range(OEP_START[pub], int(VANDAAG[:4]) + 1)
    taken, verwacht, zonder_bestand = {}, 0, 0
    for j in jaren:
        n = u.oep_telling(s, pub, j)
        verwacht += n
        st = 1
        while st <= n:
            ruw, blok = u.oep_records(s, pub, j, st, min(1000, n - st + 1))
            if not ruw: break
            for mid, _, url, ext in blok:
                taken[f"{j}/{mid}.{ext}"] = url
            zonder_bestand += ruw - len(blok)
            st += ruw
            if max_n and len(taken) >= max_n: break
        if max_n and len(taken) >= max_n: break
    if max_n: taken = dict(list(taken.items())[:max_n])
    # nooit een halve zip als 'klaar' wegzetten: prep kijkt alleen of het
    # bestand bestaat, dus een stille afkapping zou ongemerkt blijven
    if not max_n:
        opgehaald = len(taken) + zonder_bestand
        if opgehaald < verwacht:
            raise RuntimeError(f"OEP {pub}: {opgehaald} van {verwacht} records opgehaald")
        print(f"OEP {pub}: {len(taken)} bestanden, "
              f"{zonder_bestand} records zonder XML of PDF", flush=True)
    zn = oep_bestandsnaam(naam)
    parallel_naar_zip(zn, list(taken.items()))
    return [(zn, "08")]


EXTRA = []  # EVRM, Grondwet en Statuut zitten al in de BWB-zips


def shard_extra():
    uit = []
    for m, n, u in EXTRA:
        d = haal(u, pogingen=2, bin=True)
        if d:
            open(n, "wb").write(d); uit.append((n, m))
        else:
            print("niet gelukt:", u)
    for m in MAPPEN:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leesmij", f"{m}.md")
        if os.path.exists(p): uit.append((p, m, "00_LEESMIJ.md"))
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leesmij", "hoofd.md")
    uit.append((p, None, "00_LEESMIJ.md"))
    return uit


def run(shard, droog, max_n):
    mappen = json.loads(os.environ["MAPPEN"]) if not droog else {}
    drive = None if droog else Drive()
    if not droog and klaar(drive, mappen, shard): return print("bestaat al, overslaan")
    if shard == "bwb": res = shard_bwb(max_n)
    elif shard.startswith("cvdr-"): res = shard_cvdr(int(shard[5:]), max_n)
    elif shard.startswith("rs-"): res = shard_rs(shard, max_n)
    elif shard == "extra": res = shard_extra()
    elif shard.startswith("eu-"): res = shard_eu(shard, max_n)
    elif shard.startswith("oep-"): res = shard_oep(shard, max_n)
    elif shard.startswith("fts-"): res = shard_fts(shard[4:], drive, mappen)
    else: raise SystemExit("onbekende shard " + shard)
    for item in res:
        pad, m = item[0], item[1]; naam = item[2] if len(item) > 2 else None
        if droog:
            print("DROOG, zou uploaden:", pad, "->", m, f"{os.path.getsize(pad)/1024:.0f} KB")
        else:
            drive.upload(pad, mappen[m] if m else mappen["hoofd"], naam)
            if pad.endswith((".zip", ".sqlite")): os.remove(pad)


# ---------- doorzoekbare tekstlaag (SQLite FTS5) ----------
FTS = {  # bron: (map, voorvoegsel zips, bestandsnaam db)
    "bwb": ("02", "BWB_", "Zoeken_Landelijke_regels_BWB.sqlite"),
    "cvdr": ("03", "CVDR_", "Zoeken_Lokale_regels_CVDR.sqlite"),
    "rs": ("04", "Rechtspraak_", "Zoeken_Rechtspraak.sqlite"),
    "eu": ("01", "EU_", "Zoeken_EU_wetgeving.sqlite"),
    "oep": ("08", "OEP_", "Zoeken_Officiele_bekendmakingen.sqlite"),
}


def eerste(x, *tags):
    for t in tags:
        m = re.search(rf"<(?:\w+:)?{t}\b[^>]*>(.*?)</(?:\w+:)?{t}>", x, re.S)
        if m:
            v = re.sub(r"<[^>]+>", " ", m.group(1)); v = re.sub(r"\s+", " ", v).strip()
            if v: return v[:500]
    return ""


def platte_tekst(x):
    x = re.sub(r"<\?[^>]*\?>|<!--.*?-->", " ", x, flags=re.S)  # geen zware meta-regex: die werd kwadratisch op grote wetten
    x = re.sub(r"<[^>]+>", " ", x)
    x = x.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", x).strip()


def shard_fts(bron, drive, mappen):
    import sqlite3
    m, voor, dbnaam = FTS[bron]
    zips = sorted(drive.lijst(mappen[m], voor), key=lambda f: f["name"])
    if bron == "bwb": zips += drive.lijst(mappen["01"], voor)
    zips = [z for z in zips if z["name"].endswith(".zip")]
    if os.path.exists(dbnaam): os.remove(dbnaam)
    db = sqlite3.connect(dbnaam)
    db.executescript("""PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
      CREATE VIRTUAL TABLE docs USING fts5(id UNINDEXED, soort, organisatie, titel, datum UNINDEXED, tekst,
                                           url UNINDEXED, zip UNINDEXED, bestand UNINDEXED,
                                           tokenize='unicode61 remove_diacritics 2');
      CREATE TABLE info(sleutel TEXT, waarde TEXT);""")
    n = 0
    for z in zips:
        drive.download(z["id"], "tmp.zip"); rij = []
        with zipfile.ZipFile("tmp.zip") as zf:
            for b in zf.namelist():
                if not b.endswith((".xml", ".xhtml")): continue
                x = zf.read(b).decode("utf8", "ignore")
                if bron == "rs":
                    did = b[:-4].replace("_", ":"); org = eerste(x, "creator"); soort = eerste(x, "subject")
                    url = f"https://uitspraken.rechtspraak.nl/details?id={did}"
                    datum = eerste(x, "date"); titel = eerste(x, "inhoudsindicatie")[:300]
                    if len(titel) < 3: titel = eerste(x, "title") or did
                elif bron == "bwb":
                    did = b.split("_")[0]; org = ""; soort = z["name"].split("_geldig")[0][4:]
                    url = f"https://wetten.overheid.nl/{did}"; datum = eerste(x, "inwerkingtreding", "datum")
                    titel = eerste(x, "citeertitel", "intitule", "titel") or b[len(did) + 1:-4].replace("_", " ")
                elif bron == "eu":
                    did = re.sub(r"_(\d\d)$", r"(\1)", b[:-6]); org = "Europese Unie"; soort = z["name"][3:-11]
                    url = f"https://eur-lex.europa.eu/legal-content/NL/TXT/?uri=CELEX:{did}"; datum = ""
                    ti = [re.sub(r"<[^>]+>|\s+", " ", t).strip() for t in
                          re.findall(r'class="(?:oj-)?doc-ti[^"]*"[^>]*>(.*?)</p>', x, re.S)[:3]]
                    titel = " ".join(t for t in ti if t)[:400] or did
                elif bron == "oep":
                    did = b.split("/")[-1][:-4]; org = did.split("-")[0]; soort = eerste(x, "type") or org
                    url = f"https://zoek.officielebekendmakingen.nl/{did}.html"; datum = b.split("/")[0]
                    tt = [re.sub(r"\s+", " ", t).strip() for t in re.findall(r"<titel[^>]*>([^<]+)</titel>", x)]
                    tt = [t for t in tt if t.upper() not in ("STAATSCOURANT", "STAATSBLAD", "TRACTATENBLAD")]
                    titel = (tt[0] if tt else eerste(x, "intitule", "title")) or did
                else:
                    did = b.split("/")[-1][:-4]; org = b.split("/")[0].replace("_", " "); soort = eerste(x, "type")
                    url = f"https://lokaleregelgeving.overheid.nl/{did}"; datum = eerste(x, "issued", "modified")
                    titel = eerste(x, "title", "citeertitel", "intitule")
                rij.append((did, soort, org, titel, datum, platte_tekst(x), url, z["name"], b))
        db.executemany("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?)", rij); db.commit()
        n += len(rij); os.remove("tmp.zip"); print(f"  {z['name']}: {len(rij)} (totaal {n})", flush=True)
    db.executemany("INSERT INTO info VALUES (?,?)", [("gemaakt", VANDAAG), ("documenten", str(n)),
        ("voorbeeld", "SELECT titel, url, snippet(docs,5,'[',']','...',20) FROM docs WHERE docs MATCH 'sportschool AND aansprakelijkheid' ORDER BY rank LIMIT 20;")])
    db.commit(); db.execute("INSERT INTO docs(docs) VALUES('optimize')"); db.commit(); db.close()
    print(f"{dbnaam}: {n} documenten, {os.path.getsize(dbnaam)/1024**3:.2f} GB", flush=True)
    return [(dbnaam, m)]


def klaar(d, m, shard):
    if shard == "extra": return d.zoek("00_LEESMIJ.md", m["hoofd"])
    if shard == "bwb": return d.zoek("BWB_wet_geldig_", m["02"], begint=True)
    if shard.startswith("rs-"): return d.zoek(f"Rechtspraak_{shard[3:]}.zip", m["04"])
    if shard.startswith("cvdr-"): return d.zoek(f"CVDR_lokaal_blok{int(shard[5:]):02d}_", m["03"], begint=True)
    if shard.startswith("fts-"):
        mp, _, naam = FTS[shard[4:]]; return d.zoek(naam, m[mp])
    if shard.startswith("eu-"): return d.zoek(f"EU_{shard[3:]}_geldig.zip", m["01"])
    if shard.startswith("oep-"): return d.zoek(oep_bestandsnaam(shard), m["08"])


def prep():
    d = Drive(); hoofd = d.map(HOOFDMAP)
    uit = {"hoofd": hoofd}
    for k, v in MAPPEN.items(): uit[k] = d.map(v, hoofd)
    for item in shard_extra():  # LEESMIJ-bestanden altijd verversen (klein)
        d.upload(item[0], uit[item[1]] if item[1] else hoofd, item[2] if len(item) > 2 else None)
    _plan = plan()
    open_ = [sh for sh in _plan if not klaar(d, uit, sh)]
    for bron in FTS:  # zoeklaag pas als alle zips van die bron er zijn
        if not klaar(d, uit, "fts-" + bron) and not any(
                (x == "bwb" if bron == "bwb" else x.startswith(bron + "-")) for x in open_ if not x.startswith("fts-")):
            open_.append("fts-" + bron)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("mappen=" + json.dumps(uit) + "\n")
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"nog te doen: {len(open_)} shards", open_)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "plan": print(json.dumps(plan()))
    elif a[0] == "prep": prep()
    elif a[0] == "run":
        mx = int(a[a.index("--max") + 1]) if "--max" in a else 0
        run(a[1], "--droog" in a, mx)
