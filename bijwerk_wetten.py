"""Wekelijks bijwerken: landelijke wetgeving (BWB) en lokale regelgeving (CVDR).

bwb   SRU x-connection BWB, `dcterms.modified>=<sinds>` -> per gewijzigde regeling het manifest
      uit de repository; daar worden de toestanden (versies) opgehaald waarvan
      tijdstip_toegevoegd of tijdstip_laatste_wijziging op/na <sinds> ligt. Zelfde xml, zelfde
      interne controle (id + versiedatum) als historie.py: `<id>/<versie>/<bestand>.xml`.
      Verdragen en rijkswetten gaan naar map 01, de rest naar map 02 (twee zips).
cvdr  SRU x-connection cvdr, `dcterms.modified>=<sinds>` -> publicatieurl_xml per record
      (elk CVDR-record is al één versie: CVDR<id>_<versie>). Map 03, `<organisatie>/<id>.xml`
      zoals archief.py.

Zonder bovengrens op dcterms.modified: de SRU kent ook toekomstige data (bij CVDR de
datum van inwerkingtreding), die zo alvast meekomen; overlap wordt gededupliceerd.
Snelle telling zonder downloaden: omgeving WETTEN_TEL=1.
"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
import zipfile

import requests

import bijwerk_lib as L
import historie as H

REGISTER = {"bwb": "week", "cvdr": "week"}

THREADS = min(6, int(os.environ.get("THREADS", "6")))
START = time.time()
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "300")) * 60
PAGINA = 1000
MAX_VENSTER = 8000        # grotere vensters in tweeën knippen (SRU is boven ~10.000 onbetrouwbaar)
TOP = dt.date(2100, 1, 1)

CONN = {"bwb": "BWB", "cvdr": "cvdr"}
LABEL = {"bwb": ("BWB_wetgeving", "BWB_verdragen_rijkswetten"), "cvdr": ("CVDR_lokaal",)}
LOCK = threading.Lock()


def te_laat():
    return time.time() - START > GRENS_SEC


# ---------- SRU ----------
def _sru(bron, query, start, n):
    r = H.haal(H.SRU + "?" + "&".join([
        "operation=searchRetrieve", "version=1.2", f"x-connection={CONN[bron]}",
        "query=" + requests.utils.quote(query, safe=""),
        f"maximumRecords={n}", f"startRecord={start}"]))
    return r.text if r is not None else None


def _telling(bron, q):
    x = _sru(bron, q, 1, 1)
    n = H.telling(x) if x else None
    if n is None:
        raise RuntimeError(f"SRU geeft geen telling: {q}")
    return n


def _veld(blok, tag):
    m = re.search(rf"<{tag}\b[^>]*>([^<]*)</{tag}>", blok)
    return m.group(1).strip() if m else ""


def _record(bron, blok):
    if bron == "bwb":
        return {"id": _veld(blok, "dcterms:identifier"), "titel": _veld(blok, "dcterms:title"),
                "typ": _veld(blok, "dcterms:type"), "modified": _veld(blok, "dcterms:modified")}
    cre = re.search(r"<dcterms:creator[^>]*>([^<]*)", blok)
    return {"id": _veld(blok, "dcterms:identifier"), "titel": _veld(blok, "dcterms:title"),
            "typ": _veld(blok, "organisatietype"), "org": cre.group(1).strip() if cre else "onbekend",
            "modified": _veld(blok, "dcterms:modified"), "issued": _veld(blok, "dcterms:issued"),
            "inw": _veld(blok, "overheidrg:inwerkingtredingDatum"),
            "uitw": _veld(blok, "overheidrg:uitwerkingtredingDatum"),
            "url": _veld(blok, "publicatieurl_xml")}


def venster(bron, a, b, fouten, uit):
    """Alle records met dcterms.modified in [a, b); b=TOP betekent zonder bovengrens."""
    q = f"dcterms.modified>={a.isoformat()}" + ("" if b == TOP else f" and dcterms.modified<{b.isoformat()}")
    n = _telling(bron, q)
    if n == 0:
        return
    if n > MAX_VENSTER and (b - a).days > 1:
        mid = a + (b - a) // 2 if b != TOP else dt.date.today() + dt.timedelta(days=2)
        if mid <= a:
            mid = a + dt.timedelta(days=1)
        venster(bron, a, mid, fouten, uit)
        venster(bron, mid, b, fouten, uit)
        return
    hier, start = [], 1
    while start <= n:
        x = _sru(bron, q, start, PAGINA)
        blokken = re.findall(r"<record>.*?</record>", x or "", re.S)
        if not blokken:
            break
        hier += [_record(bron, bl) for bl in blokken]
        start += len(blokken)
    if len(hier) < n:
        if (b - a).days > 1:    # ontbrekende records: kleiner knippen
            mid = a + (b - a) // 2 if b != TOP else a + dt.timedelta(days=1)
            if a < mid < b:
                venster(bron, a, mid, fouten, uit)
                venster(bron, mid, b, fouten, uit)
                return
        fouten.append(f"SRU-venster {q}: {len(hier)} van {n} records opgehaald")
    uit.extend(hier)


def records(bron, van):
    fouten, ruw = [], []
    venster(bron, van, TOP, fouten, ruw)
    uniek = {}
    for r in ruw:
        if r["id"]:
            uniek[r["id"]] = r       # bwb: één per regeling; cvdr: id = werk_versie
    return uniek, len(ruw), fouten


# ---------- BWB ----------
def expressies(xml):
    """[(label, item, inw, einddatum, hash, toegevoegd, gewijzigd)] voor alle xml-versies."""
    uit = []
    for m in re.finditer(r'<expression label="([^"]+)">(.*?)</expression>', xml, re.S):
        blok = m.group(2)
        if 'label="xml"' not in blok:
            continue
        item = re.search(r'<item label="([^"]+)"([^>]*)>', blok)
        if not item or '_deleted="true"' in item.group(2):
            continue
        uit.append((m.group(1), item.group(1), _veld(blok, "datum_inwerkingtreding"),
                    _veld(blok, "einddatum"), _veld(blok, "hashcode"),
                    _veld(blok, "tijdstip_toegevoegd"), _veld(blok, "tijdstip_laatste_wijziging")))
    return uit


def bwb_taak(t):
    """t: record met id, optioneel versie+item (herhaling). Geeft (t, [resultaat...], fout)."""
    bid, van = t["id"], t["van"]
    if te_laat():
        return t, [], "tijd: niet meer aan begonnen binnen de tijdgrens van de job"
    if t.get("versie") and t.get("item"):
        lijst = [(t["versie"], t["item"], "", "", t.get("hash", ""), "", "")]
    else:
        try:
            r = H.haal(f"{H.REPO}/bwb/{bid}/manifest.xml")
        except Exception as e:
            return t, [], f"manifest:{type(e).__name__}: {str(e)[:200]}"
        if r is None:
            return t, [], "geen manifest in repository (404)"
        lijst = [e for e in expressies(r.text) if max(e[5], e[6])[:10] >= van]
    uit, fout = [], []
    for label, item, inw, ein, hash_, toe, wijz in lijst:
        if te_laat():
            fout.append(f"{label}:tijd")
            uit.append({"label": label, "item": item, "data": None, "fout": "tijd", "hash": hash_})
            continue
        with LOCK:
            sleutel = (bid, label)
            if sleutel in GEDAAN:
                continue
            GEDAAN.add(sleutel)
        try:
            data, f, sha, controle = H.haal_versie((bid, label, item, hash_))
        except Exception as e:
            data, f, sha, controle = None, f"{type(e).__name__}: {str(e)[:200]}", "", ""
        uit.append({"label": label, "item": item, "data": data, "fout": f, "sha": sha,
                    "controle": controle, "inw": inw, "ein": ein, "hash": hash_,
                    "toe": toe, "wijz": wijz})
    return t, uit, ""


GEDAAN = set()


# ---------- CVDR ----------
def cvdr_taak(r):
    if te_laat():
        return r, None, "tijd: niet meer aan begonnen binnen de tijdgrens van de job"
    if not r.get("url"):
        return r, None, "geen publicatieurl_xml"
    try:
        a = H.haal(r["url"])
    except Exception as e:
        return r, None, f"{type(e).__name__}: {str(e)[:200]}"
    if a is None:
        return r, None, "404"
    d = a.content
    if len(d) < 200:
        return r, None, "leeg"
    try:
        ET.fromstring(d)
    except ET.ParseError as e:
        return r, None, f"ongeldige xml: {e}"
    return r, d, ""


def veilig(t, n=90):
    return re.sub(r"[^\w\-.]+", "_", t)[:n].strip("_")


# ---------- run ----------
def run(bron, droog=False, max_n=0, sinds=None):
    stand = L.Stand(bron)
    van, tot = stand.venster(sinds)
    recs, ruw, sru_fouten = records(bron, van)
    herhaal = [dict(r) for r in stand.data.get("open", [])]
    if bron == "bwb":
        n_wet = len(recs)
        print(f"bwb: dcterms.modified>={van}: {ruw} records, {n_wet} unieke regelingen, "
              f"+ {len(herhaal)} uit herhaalronde", flush=True)
    else:
        print(f"cvdr: dcterms.modified>={van}: {ruw} records, {len(recs)} uniek, "
              f"+ {len(herhaal)} uit herhaalronde", flush=True)
    if os.environ.get("WETTEN_TEL"):
        return {"run": time.strftime("%Y-%m-%d"), "van": van.isoformat(), "records": ruw,
                "uniek": len(recs), "herhaal": len(herhaal), "tel": True}

    if bron == "bwb":
        # herhaling: versie-niveau (met label/item) of regeling-niveau; verse regelingen erachter
        taken = [{**r, "van": van.isoformat()} for r in herhaal]
        vers = [{"id": i, "typ": r["typ"], "titel": r["titel"], "van": van.isoformat()}
                for i, r in recs.items()]
        taken += vers
        if max_n:
            taken = taken[:max_n]
        return _run_bwb(stand, van, tot, taken, herhaal, ruw, sru_fouten, droog)
    taken = [r for r in herhaal if r.get("id") not in recs] + list(recs.values())
    if max_n:
        taken = taken[:max_n]
    return _run_cvdr(stand, van, tot, taken, herhaal, ruw, sru_fouten, droog)


def _run_bwb(stand, van, tot, taken, herhaal, ruw, sru_fouten, droog):
    kop = ["shard", "bestand", "id", "titel", "versie", "inwerkingtreding", "einddatum", "bytes",
           "sha512_ontvangen", "sha512_manifest", "hash_controle", "toegevoegd", "gewijzigd", "url", "herhaal"]
    per_map = {"02": [], "01": []}      # rijen
    zips, open_, mislukt = {}, [], list(sru_fouten)
    namen = {"02": L.zipnaam(LABEL["bwb"][0], van, tot), "01": L.zipnaam(LABEL["bwb"][1], van, tot)}
    handles = {k: zipfile.ZipFile(n, "w", zipfile.ZIP_DEFLATED, compresslevel=6) for k, n in namen.items()}
    aantal = {"02": 0, "01": 0}
    mis_per = {"02": [], "01": []}
    herhaal_ids = {(r["id"], r.get("versie", "")) for r in herhaal}
    geen_nieuw = 0
    try:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            for t, res, fout in ex.map(bwb_taak, taken):
                k = "01" if t.get("typ") in ("verdrag", "rijkswet") else "02"
                bid = t["id"]
                if fout:
                    mis_per[k].append(f"{bid}: {fout}")
                    if not fout.startswith("tijd"):
                        pog = int(t.get("_pogingen", 0)) + 1
                    else:
                        pog = int(t.get("_pogingen", 0))
                    if pog < L.MAX_POGINGEN and not fout.startswith("geen manifest"):
                        open_.append({"id": bid, "typ": t.get("typ", ""), "titel": t.get("titel", ""),
                                      "versie": "", "item": "", "_pogingen": pog})
                    continue
                if not res and not t.get("versie"):
                    geen_nieuw += 1
                for v in res:
                    if v["data"]:
                        handles[k].writestr(f"{bid}/{v['label']}/{v['item']}", v["data"], zipfile.ZIP_DEFLATED)
                        aantal[k] += 1
                        per_map[k].append([zips_naam(namen[k]), f"{bid}/{v['label']}/{v['item']}", bid,
                                           H.titel_uit(v["data"]) or t.get("titel", ""), v["label"], v["inw"],
                                           v["ein"], len(v["data"]), v["sha"], v["hash"], v["controle"],
                                           v["toe"], v["wijz"],
                                           f"{H.REPO}/bwb/{bid}/{v['label']}/xml/{v['item']}",
                                           "herhaal" if (bid, v["label"]) in herhaal_ids else ""])
                    else:
                        mis_per[k].append(f"{bid}/{v['label']}/{v['item']}: {v['fout']}")
                        pog = int(t.get("_pogingen", 0)) + (0 if v["fout"] == "tijd" else 1)
                        if pog < L.MAX_POGINGEN and v["fout"] != "404":
                            open_.append({"id": bid, "typ": t.get("typ", ""), "titel": t.get("titel", ""),
                                          "versie": v["label"], "item": v["item"], "hash": v["hash"],
                                          "_pogingen": pog})
        for k in ("02", "01"):
            mislukt += mis_per[k]
        for k, h in handles.items():
            h.writestr("_OVERZICHT.txt",
                       f"Wekelijkse bijwerking BWB (Basiswettenbestand), map {k}\n"
                       f"Bron: SRU zoekservice.overheid.nl (BWB) dcterms.modified>={van}; "
                       f"manifest + xml uit repository.officiele-overheidspublicaties.nl\n"
                       f"Versies met tijdstip_toegevoegd of tijdstip_laatste_wijziging op/na {van}\n"
                       f"Verzameld: {time.strftime('%Y-%m-%d %H:%M')}\n"
                       f"SRU-records: {ruw}; versies in deze zip: {aantal[k]}; mislukt: {len(mis_per[k])}\n"
                       "Mappen: <bwb-id>/<versie>/<bestand>.xml, ongewijzigd; interne controle op id + versiedatum.\n"
                       + ("" if not mis_per[k] else "\nMislukt:\n" + "\n".join(mis_per[k])))
    finally:
        for h in handles.values():
            h.close()
    totaal = sum(aantal.values())
    print(f"bwb: {totaal} versies (map 02: {aantal['02']}, map 01: {aantal['01']}), "
          f"regelingen zonder nieuwe versie {geen_nieuw}, mislukt {len(mislukt)}, "
          f"opnieuw proberen {len(open_)}", flush=True)
    for k in ("02", "01"):
        zn = namen[k]
        idx = L.schrijf_index(zn, kop, per_map[k])
        if aantal[k] or mis_per[k]:
            L.upload_resultaat(stand, zn, k, idx, L.schrijf_mislukt(zn, mis_per[k]), droog)
        else:
            for f in (zn, idx):
                if os.path.exists(f):
                    os.remove(f)
    if not totaal and not mislukt:
        print("niets nieuw: geen zip", flush=True)
    verslag = {"run": time.strftime("%Y-%m-%d"), "van": van.isoformat(), "tot": tot.isoformat(),
               "stukken": totaal, "xml": totaal, "pdf": 0, "mislukt": len(mislukt),
               "zip": [namen[k] for k in ("02", "01") if aantal[k]]}
    if not droog:
        stand.bewaar(dt.date.today().isoformat(), open_, verslag)
    return verslag


def zips_naam(zn):
    return zn.replace(".zip", "")


def _run_cvdr(stand, van, tot, taken, herhaal, ruw, sru_fouten, droog):
    zn = L.zipnaam(LABEL["cvdr"][0], van, tot)
    kop = ["id", "titel", "type", "organisatie", "modified", "issued", "inwerkingtreding",
           "uitwerkingtreding", "bytes", "sha256", "url", "herhaal"]
    herhaal_ids = {r["id"] for r in herhaal}
    rijen, mislukt, open_ = [], list(sru_fouten), []
    ok = 0
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            for r, data, fout in ex.map(cvdr_taak, taken):
                if data is None:
                    mislukt.append(f"{r['id']}: {fout}")
                    pog = int(r.get("_pogingen", 0)) + (0 if fout.startswith("tijd") else 1)
                    if pog < L.MAX_POGINGEN and fout != "404":
                        open_.append({**r, "_pogingen": pog})
                    continue
                z.writestr(f"{veilig(r.get('org', 'onbekend'), 50)}/{veilig(r['id'])}.xml", data)
                ok += 1
                rijen.append([r["id"], r["titel"], r["typ"], r.get("org", ""), r["modified"], r.get("issued", ""),
                              r.get("inw", ""), r.get("uitw", ""), len(data), hashlib.sha256(data).hexdigest(),
                              r["url"], "herhaal" if r["id"] in herhaal_ids else ""])
        z.writestr("_OVERZICHT.txt",
                   f"Wekelijkse bijwerking CVDR (lokale regelgeving)\n"
                   f"Bron: SRU zoekservice.overheid.nl (cvdr) dcterms.modified>={van}; xml uit repository\n"
                   f"Verzameld: {time.strftime('%Y-%m-%d %H:%M')}\n"
                   f"SRU-records: {ruw}; stukken in zip: {ok}; mislukt: {len(mislukt)}\n"
                   "Mappen: <organisatie>/<CVDR-id_versie>.xml, ongewijzigd; sha256 in de index (map 00).\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))
    print(f"{zn}: {ok} stukken, mislukt {len(mislukt)}, opnieuw proberen {len(open_)}", flush=True)
    idx = L.schrijf_index(zn, kop, rijen)
    if taken:
        L.upload_resultaat(stand, zn, "03", idx, L.schrijf_mislukt(zn, mislukt), droog)
    else:
        print("niets nieuw: geen zip", flush=True)
        for f in (zn, idx):
            if os.path.exists(f):
                os.remove(f)
    verslag = {"run": time.strftime("%Y-%m-%d"), "van": van.isoformat(), "tot": tot.isoformat(),
               "stukken": ok, "xml": ok, "pdf": 0, "mislukt": len(mislukt), "zip": zn}
    if not droog:
        stand.bewaar(dt.date.today().isoformat(), open_, verslag)
    return verslag
