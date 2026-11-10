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
}
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
    shards = ["extra", "bwb"]
    x = haal(SRU, {"operation": "searchRetrieve", "version": "1.2", "x-connection": "cvdr",
                   "maximumRecords": 1, "query": "cql.allRecords=1"})
    n = int(re.search(r"<numberOfRecords>(\d+)", x).group(1))
    shards += [f"cvdr-{i:02d}" for i in range(n // CVDR_TOTAAL_BLOK + 1)]
    shards += ["rs-tot1999"] + [f"rs-{y}" for y in range(2000, int(VANDAAG[:4]) + 1)]
    return shards


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


EXTRA = [  # (map, bestandsnaam, url) — EVRM/Grondwet/Statuut zitten al in de BWB-zips; EUR-Lex blokkeert bots
]


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
    else: raise SystemExit("onbekende shard " + shard)
    for item in res:
        pad, m = item[0], item[1]; naam = item[2] if len(item) > 2 else None
        if droog:
            print("DROOG, zou uploaden:", pad, "->", m, f"{os.path.getsize(pad)/1024:.0f} KB")
        else:
            drive.upload(pad, mappen[m] if m else mappen["hoofd"], naam)
            if pad.endswith(".zip"): os.remove(pad)


def klaar(d, m, shard):
    if shard == "extra": return d.zoek("00_LEESMIJ.md", m["hoofd"])
    if shard == "bwb": return d.zoek("BWB_wet_geldig_", m["02"], begint=True)
    if shard.startswith("rs-"): return d.zoek(f"Rechtspraak_{shard[3:]}.zip", m["04"])
    if shard.startswith("cvdr-"): return d.zoek(f"CVDR_lokaal_blok{int(shard[5:]):02d}_", m["03"], begint=True)


def prep():
    d = Drive(); hoofd = d.map(HOOFDMAP)
    uit = {"hoofd": hoofd}
    for k, v in MAPPEN.items(): uit[k] = d.map(v, hoofd)
    open_ = [sh for sh in plan() if not klaar(d, uit, sh)]
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
