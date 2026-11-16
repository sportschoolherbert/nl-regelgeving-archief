"""Bijwerken van de overige bronnen: CAO-register (SZW), AVV-cao-bepalingen en open-access literatuur.

  python bijwerken.py run cao_register [--droog] [--max N] [--sinds JJJJ-MM-DD]
  python bijwerken.py run avv          [--droog] [--max N] [--sinds JJJJ-MM-DD]
  python bijwerken.py run literatuur_oa [--droog] [--max N] [--sinds JJJJ-MM-DD]

Hoe "nieuw of gewijzigd" per bron wordt gevonden:

cao_register (wekelijks, map 06)
  Het register (Mozard, achter Anubis) toont GEEN wijzigingsdatum per registratie en heeft geen bruikbare
  lijst gesorteerd op datum (de zoeklijst kapt af, zie cao_register.py). Elke aanmelding is een eigen,
  nieuw objectnummer (de kennisgevingsdatum zit in de naam), dus nieuw = objectnummers boven het hoogste
  dat al geoogst is. Het hoogste nummer staat in stand.data["hoogste_object"] (begin: 57414, het hoogste
  in de Drive-indexen op 16-11-2026; env CAO_START_OBJECT overschrijft dat voor een test). We lopen
  op tot LEEG_MAX (300) opeenvolgende nummers zonder toegankelijk object. Een later toegevoegd document
  bij een bestaand object is NIET te vinden zonder alles opnieuw te scannen (zie open punten in het rapport).
  Nodig: pakket `playwright` (+ `playwright install chromium`) en `requests`.

avv (wekelijks, map 06)
  Zelfde set als avv.py: Staatscourantstukken met dt.type "cao" en "bepalingen" (AVV-verzoeken en
  -besluiten) uit de SRU van repository.overheid.nl, met dt.modified >= sinds. xml + pdf, ongewijzigd,
  zelfde controles als avv.py. (De Staatscourant-xml wordt ook door bron `staatscourant` gehaald; dit is de
  aparte CAO_AVV-reeks, in dezelfde indeling <id>/xml|pdf/.)
  Nodig: `requests` (via kst.py).

literatuur_oa (maandelijks, map 07)
  OpenAlex: dezelfde selecties als literatuur_oa.py (Nederlandstalig recht + tijdschriftenlijst
  literatuur_oa_bronnen.json) met from_publication_date >= sinds - LOOKBACK_DAGEN. Andere datumfilters
  (from_created_date / from_updated_date) zijn bij OpenAlex betaald (403), en een open pdf verschijnt
  vaak later dan de publicatie; daarom een ruim terugkijkvenster, waarna alles wat al in een
  Literatuur_*/Bijwerking_Literatuur_* index in Drive staat (doi, openalex-id, handle, titel, sha256)
  wordt overgeslagen. Alleen open pdf's, robots.txt gevolgd, geen paywall; harde 403 (o.a. Radboud) wordt
  overgeslagen (literatuur_oa.haal_werk). Nodig: `requests`.

Alle bronnen: standbestand in Drive (bijwerk_lib.Stand), eerst `open` opnieuw, eigen zip met
_OVERZICHT.txt, index in map 00, mislukt-lijst, tijdgrens env TIJDGRENS_MIN (standaard 300).
"""
import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import hashlib
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import zipfile

import bijwerk_lib as L

REGISTER = {"cao_register": "week", "avv": "week", "literatuur_oa": "maand"}

START = time.time()
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "300")) * 60

CAO_LABEL, CAO_MAP = "CAO_register", "06"
CAO_START_DEFAULT = 57414    # hoogste objectnummer in de Drive-indexen (16-11-2026)
LEEG_MAX = 300               # zoveel opeenvolgende lege nummers = einde
AVV_LABEL, AVV_MAP = "CAO_AVV", "06"
LIT_LABEL, LIT_MAP = "Literatuur_OA", "07"
LOOKBACK_DAGEN = 120
LIT_ZIP_MAX = 1200 * 1024 * 1024


def _tijd_op():
    return time.time() - START > GRENS_SEC


def _pog(r):
    return int(r.get("_pogingen", 0)) + 1


def _sluit_af(stand, bron, label, mapsleutel, van, tot, zn, kop, rijen, mislukt, open_, droog, verslag, heeft_stukken):
    idx = L.schrijf_index(zn, kop, rijen)
    if heeft_stukken:
        L.upload_resultaat(stand, zn, mapsleutel, idx, L.schrijf_mislukt(zn, mislukt), droog)
    else:
        print("niets nieuw: geen zip", flush=True)
        for f in (zn, idx):
            if os.path.exists(f):
                os.remove(f)
    if not droog:
        stand.bewaar(dt.date.today().isoformat(), open_, verslag)
    return verslag


# ====================================================================== cao_register

def _cao_object(c, ctx, pg, zip_, oid, alleen_hrefs=None, herhaal=False):
    """Haalt één object. -> (toegankelijk, rijen, mislukt_regels, open_doc_hrefs)."""
    h = c.detail(pg, oid)
    if h is None:
        return None, [], [(oid, "", "detailpagina niet op te halen")], None
    d = c.parse(h)
    time.sleep(c.PAUZE)
    if not d or not d["velden"]:       # niet bestaand of "Geen Rechten" (leeg object)
        return False, [], [], None
    v = d["velden"]
    reg = v.get("Registratie", "")
    if not d["docs"] and not reg.lower().startswith(c.REGISTRATIES):
        return True, [], [], None
    sector = " | ".join(x for x in (v.get("Soort cao", ""),
                                    ("SBI " + v["SBI-code"]) if v.get("SBI-code") else "") if x)
    basis = {"cao_id": oid, "naam": d["titel"], "registratie": reg, "sector": sector,
             "type": v.get("Type cao", ""),
             "looptijd": f"{v.get('Ingangsdatum', '')} t/m {v.get('Expiratiedatum', '')}",
             "kvo": v.get("Datum formele Kennisgeving van Ontvangst", ""),
             "pagina": f"{c.BASIS}!suite92.scherm1007?mObj={oid}"}
    rijen, mislukt, open_hrefs = [], [], []
    if not d["docs"] and not alleen_hrefs:
        rijen.append({**basis, "documentsoort": "(geen document)", "bestand": "", "url": "",
                      "bytes": 0, "sha256": ""})
    for i, doc in enumerate(d["docs"], 1):
        if alleen_hrefs is not None and doc["href"] not in alleen_hrefs:
            continue
        url, data = c.haal_doc(ctx, pg, doc["href"])
        if data is None:
            mislukt.append((oid, url, "document niet op te halen"))
            open_hrefs.append(doc["href"])
            continue
        ext = os.path.splitext(urllib.parse.unquote(doc["href"].split("/")[-1]))[1] \
            or ("." + doc["ext"] if doc["ext"] else "")
        pad = f"{oid}/{i:02d}_{c.bestandsnaam(doc['naam'])}{ext}"
        zip_.writestr(pad, data, zipfile.ZIP_STORED)
        rijen.append({**basis, "documentsoort": doc["soort"] or doc["ext"] or "document", "bestand": pad,
                      "url": url, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                      "herhaal": "herhaal" if herhaal else ""})
    return True, rijen, mislukt, open_hrefs


def run_cao_register(bron, droog, max_n, sinds):
    import cao_register as c
    stand = L.Stand(bron)
    van, tot = stand.venster(sinds)
    hoogste = int(stand.data.get("hoogste_object") or CAO_START_DEFAULT)
    begin = int(os.environ.get("CAO_START_OBJECT") or hoogste + 1)
    zn = L.zipnaam(CAO_LABEL, van, tot)
    kol = ["cao_id", "naam", "registratie", "sector", "type", "looptijd", "kvo", "documentsoort",
           "bestand", "url", "bytes", "sha256", "pagina", "herhaal"]
    rijen, mislukt, open_ = [], [], []
    nieuw_hoogste, bewaard, geprobeerd, leeg, toegank = hoogste, 0, 0, 0, 0
    herhaal = [r for r in stand.data.get("open", []) if r.get("object") is not None]
    p, b, ctx, pg = c.browser()
    try:
        with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            # 1. herhaalronde
            for r in herhaal:
                if _tijd_op():
                    open_.append(r)
                    continue
                oid = int(r["object"])
                ok, rr, mm, oh = _cao_object(c, ctx, pg, z, oid, r.get("hrefs") or None, True)
                rijen += rr
                mislukt += [f"{o}: {u} {t}".strip() for o, u, t in mm]
                if mm:
                    if _pog(r) < L.MAX_POGINGEN:
                        open_.append({"object": oid, "hrefs": (oh or None) if ok else None, "_pogingen": _pog(r)})
            # 2. nieuwe objectnummers tot LEEG_MAX opeenvolgend leeg
            oid = begin
            while leeg < LEEG_MAX:
                if max_n and bewaard >= max_n:
                    print(f"  --max {max_n} bereikt bij {oid}", flush=True)
                    break
                if _tijd_op():
                    mislukt.append(f"tijd: vanaf object {oid} niet meer aan begonnen (volgende run gaat verder)")
                    break
                geprobeerd += 1
                ok, rr, mm, oh = _cao_object(c, ctx, pg, z, oid)
                if ok is None:                         # detailpagina onbereikbaar: later opnieuw
                    leeg += 1
                    mislukt += [f"{o}: {u} {t}".strip() for o, u, t in mm]
                    open_.append({"object": oid, "hrefs": None, "_pogingen": 1})
                elif ok is False:
                    leeg += 1
                else:
                    leeg = 0
                    toegank += 1
                    nieuw_hoogste = max(nieuw_hoogste, oid)
                    if rr or mm:
                        bewaard += 1
                    rijen += rr
                    mislukt += [f"{o}: {u} {t}".strip() for o, u, t in mm]
                    if mm:
                        open_.append({"object": oid, "hrefs": oh or None, "_pogingen": 1})
                oid += 1
            overzicht = (f"Wekelijkse bijwerking: CAO-register SZW (cao.minszw.nl)\n"
                         f"Nieuwe objectnummers vanaf {begin} (hoogste eerder: {hoogste}); gestopt na {leeg} lege "
                         f"nummers, laatste object met inhoud: {nieuw_hoogste}\n"
                         f"Venster (stand): {van} t/m {tot - dt.timedelta(days=1)}; verzameld {time.strftime('%Y-%m-%d %H:%M')}\n"
                         f"Nummers geprobeerd: {geprobeerd}, toegankelijk: {toegank}, bewaard: {bewaard}, "
                         f"herhaalronde: {len(herhaal)}\nDocumenten: {sum(1 for r in rijen if r['bestand'])}, "
                         f"mislukt: {len(mislukt)}\nBestanden ongewijzigd; mappen per objectnummer.\n"
                         "Let op: het register toont geen wijzigingsdatum; later toegevoegde documenten bij een "
                         "bestaand object worden hier niet gevonden.\n"
                         + ("" if not mislukt else "\nMislukt:\n" + "\n".join(map(str, mislukt))))
            z.writestr("_OVERZICHT.txt", overzicht)
    finally:
        b.close()
        p.stop()
    stand.data["hoogste_object"] = nieuw_hoogste
    print(f"{zn}: {geprobeerd} nummers, {toegank} toegankelijk, {bewaard} objecten, "
          f"{sum(1 for r in rijen if r['bestand'])} documenten, {len(mislukt)} mislukt, hoogste {nieuw_hoogste}", flush=True)
    verslag = {"run": time.strftime("%Y-%m-%d"), "van": van.isoformat(), "tot": tot.isoformat(),
               "stukken": len(rijen), "objecten": bewaard, "hoogste_object": nieuw_hoogste,
               "mislukt": len(mislukt), "zip": zn}
    return _sluit_af(stand, bron, CAO_LABEL, CAO_MAP, van, tot, zn, ["bron"] + kol,
                     [["cao_register"] + [r.get(k, "") for k in kol] for r in rijen], mislukt, open_, droog,
                     verslag, bool(rijen or mislukt))


# ====================================================================== avv

def run_avv(bron, droog, max_n, sinds):
    import bijwerk_officieel as bo
    import kst
    stand = L.Stand(bron)
    van, tot = stand.venster(sinds)
    lijst, totaal = bo.records('dt.type any "cao" and dt.type any "bepalingen"', van, tot)
    recs = {(r["id"], r["versie"]): r for r in lijst}
    herhaal = [r for r in stand.data.get("open", []) if (r["id"], r["versie"]) not in recs]
    herhaal_keys = {(r["id"], r["versie"]) for r in herhaal}
    taken = herhaal + list(recs.values())
    if max_n:
        taken = taken[:max_n]
    print(f"avv: dt.modified {van} t/m {tot - dt.timedelta(days=1)}: {totaal} records, {len(recs)} uniek, "
          f"+ {len(herhaal)} herhaal -> {len(taken)} stukken", flush=True)
    zn = L.zipnaam(AVV_LABEL, van, tot)
    rijen, mislukt, open_ = [], [], []
    met_xml = met_pdf = 0
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(8) as ex:
            for r, uit in ex.map(bo.haal_stuk, [(r, "altijd") for r in taken]):
                if uit["xml"]:
                    z.writestr(f"{r['id']}/xml/{r['id']}.xml", uit["xml"]); met_xml += 1
                if uit["pdf"]:
                    z.writestr(f"{r['id']}/pdf/{r['id']}.pdf", uit["pdf"]); met_pdf += 1
                if uit["fout"]:
                    mislukt.append(f"{r['id']}" + ("" if not (uit["xml"] or uit["pdf"]) else " (deels)")
                                   + ": " + ", ".join(uit["fout"]))
                    if _pog(r) < L.MAX_POGINGEN and not (uit["xml"] and uit["pdf"]):
                        open_.append({**r, "_pogingen": _pog(r)})
                rijen.append([r["id"], r["versie"], r["titel"], r["type"], r["maker"], r["datum"],
                              len(uit["xml"] or b""), uit["controle"].get("xml", ""),
                              len(uit["pdf"] or b""), uit["controle"].get("pdf", ""), r["xml"], r["pdf"],
                              "herhaal" if (r["id"], r["versie"]) in herhaal_keys else ""])
        z.writestr("_OVERZICHT.txt",
                   "Algemeen verbindend verklaarde CAO-bepalingen (Staatscourant) - wekelijkse bijwerking\n"
                   f"Bron: SRU repository.overheid.nl, dt.type any \"cao\" and dt.type any \"bepalingen\"\n"
                   f"Nieuw of gewijzigd (dt.modified) van {van} t/m {tot - dt.timedelta(days=1)}\n"
                   f"Verzameld: {time.strftime('%Y-%m-%d %H:%M')}\nRecords: {totaal}, uniek: {len(recs)}, "
                   f"herhaalronde: {len(herhaal)}\nXml: {met_xml}, pdf: {met_pdf}, mislukt: {len(mislukt)}\n"
                   "Mappen: <id>/xml/<id>.xml en <id>/pdf/<id>.pdf; bestanden ongewijzigd, sha256 in de index.\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))
    print(f"{zn}: {len(taken)} stukken, xml {met_xml}, pdf {met_pdf}, mislukt {len(mislukt)}, "
          f"opnieuw volgende run {len(open_)}", flush=True)
    verslag = {"run": time.strftime("%Y-%m-%d"), "van": van.isoformat(), "tot": tot.isoformat(),
               "stukken": len(taken), "xml": met_xml, "pdf": met_pdf, "mislukt": len(mislukt), "zip": zn}
    return _sluit_af(stand, bron, AVV_LABEL, AVV_MAP, van, tot, zn,
                     ["id", "versie", "titel", "type", "maker", "datum", "xml_bytes", "xml_sha256",
                      "pdf_bytes", "pdf_sha256", "url_xml", "url_pdf", "herhaal"], rijen, mislukt, open_,
                     droog, verslag, bool(taken))


# ====================================================================== literatuur_oa

def _bestaande_sleutels(lo, d, mappen, tmp):
    """Sleutels van alle Literatuur_* indexen (lo.laad_bestaand) plus eerdere Bijwerking_Literatuur_*."""
    sleutelset = lo.laad_bestaand(d, mappen, tmp)
    for f in lo.drive_lijst(d, mappen["00"], "Bijwerking_Literatuur"):
        if not f["name"].endswith(".index.csv.gz"):
            continue
        pad = os.path.join(tmp, "bw_" + f["id"])
        d.download(f["id"], pad)
        with gzip.open(pad, "rt", encoding="utf8", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("doi"):
                    sleutelset.add("doi:" + r["doi"].lower())
                if r.get("openalex_id"):
                    sleutelset.add("oa:" + r["openalex_id"])
                if r.get("handle"):
                    sleutelset.add("hdl:" + r["handle"])
                if r.get("sha256"):
                    sleutelset.add("sha:" + r["sha256"])
                sleutelset.add(lo.titelsleutel(r.get("titel"), r.get("jaar"), r.get("auteurs")))
        os.remove(pad)
    sleutelset.discard("")
    return sleutelset


def _lit_kandidaten(lo, vanaf, max_n):
    """Werken (nl-recht + tijdschriftenlijst) met publicatiedatum >= vanaf, ontdubbeld op OpenAlex-id."""
    import json
    basis = f"open_access.is_oa:true,has_pdf_url:true,from_publication_date:{vanaf.isoformat()}"
    gezien, uit = set(), []

    def neem(lijst, uitsluiten=None):
        for w in lijst:
            if w["id"] in gezien or (uitsluiten and uitsluiten(w)):
                continue
            gezien.add(w["id"])
            uit.append(w)

    for f in (f"topics.subfield.id:{lo.SUBFIELD_LAW}", f"concepts.id:{lo.CONCEPT_LAW}"):
        neem(lo.openalex_alles(f"language:nl,{basis},{f}"))
        print(f"  nlrecht {f}: {len(uit)} werken tot nu toe", flush=True)
    with open(lo.BRONNEN, encoding="utf8") as fh:
        ids = sorted(json.load(fh))
    for i in range(0, len(ids), 40):
        neem(lo.openalex_alles(f"primary_location.source.id:{'|'.join(ids[i:i + 40])},{basis}"), lo.is_nlrecht)
        if max_n and len(uit) >= max_n * 5:      # test: niet alle batches doorlopen
            break
    print(f"  tijdschriften: {len(uit)} werken in totaal", flush=True)
    return uit


def run_literatuur_oa(bron, droog, max_n, sinds):
    import literatuur_oa as lo
    stand = L.Stand(bron)
    van, tot = stand.venster(sinds)
    vanaf = van - dt.timedelta(days=LOOKBACK_DAGEN)
    werk = tempfile.mkdtemp(prefix="lit_bijwerk_")
    tmp = os.path.join(werk, "tmp")
    os.makedirs(tmp)
    zn = L.zipnaam(LIT_LABEL, van, tot)
    rijen, mislukt, open_ = [], [], []
    try:
        bestaand = _bestaande_sleutels(lo, stand.d, stand.mappen, tmp)
        print(f"bestaand in Drive: {len(bestaand)} sleutels", flush=True)
        # herhaalronde: eerder mislukte werken opnieuw ophalen via hun OpenAlex-id
        herhaal_ids = sorted({r["openalex_id"] for r in stand.data.get("open", []) if r.get("openalex_id")})
        herhaal, pogingen = [], {r["openalex_id"]: _pog(r) for r in stand.data.get("open", []) if r.get("openalex_id")}
        for i in range(0, len(herhaal_ids), 50):
            herhaal += lo.openalex_alles("openalex_id:" + "|".join(herhaal_ids[i:i + 50]))
        herhaal_set = {w["id"] for w in herhaal}
        kand = _lit_kandidaten(lo, vanaf, max_n)
        alle = herhaal + [w for w in kand if w["id"] not in herhaal_set]
        nieuw = [w for w in alle if w["id"] in herhaal_set or not (lo.sleutels(w) & bestaand)]
        print(f"literatuur_oa: publicaties vanaf {vanaf} (sinds {van} - {LOOKBACK_DAGEN} dagen): {len(kand)} kandidaten, "
              f"{len(kand) - len([w for w in nieuw if w['id'] not in herhaal_set])} al in Drive, "
              f"{len(nieuw)} te halen (waarvan {len(herhaal)} herhaal)", flush=True)
        if max_n:
            nieuw = nieuw[:max_n]
        groot, gezien_sha, grootte = 0, set(), 0
        z = zipfile.ZipFile(zn, "w", zipfile.ZIP_STORED)
        namen = set()
        for i in range(0, len(nieuw), 100):
            groep = nieuw[i:i + 100]
            if _tijd_op() or grootte > LIT_ZIP_MAX:
                for w in groep + nieuw[i + 100:]:        # rest volgende run, zonder poging te verbranden
                    open_.append({"openalex_id": w["id"].rsplit("/", 1)[-1], "titel": w.get("title") or "",
                                  "_pogingen": pogingen.get(w["id"].rsplit("/", 1)[-1], 0)})
                mislukt.append(f"{len(groep) + len(nieuw[i + 100:])} werken niet meer aan begonnen "
                               f"({'tijdgrens' if _tijd_op() else 'zipgrootte'}); volgende run")
                break
            with cf.ThreadPoolExecutor(lo.THREADS) as ex:
                res = list(ex.map(lambda w: lo.haal_werk(w, tmp), groep))
            for w, r in zip(groep, res):
                wid = w["id"].rsplit("/", 1)[-1]
                if "fout" in r:
                    mislukt.append(f"{wid} | {w.get('title') or ''} | {r['fout']}")
                    p = pogingen.get(wid, 0) + 1
                    if p < L.MAX_POGINGEN and not r["fout"].startswith(("robots", "geen pdf", "http 403", "http 401", "te groot")) \
                            and "http 403" not in r["fout"] and "http 401" not in r["fout"]:
                        open_.append({"openalex_id": wid, "titel": w.get("title") or "", "_pogingen": p})
                    continue
                if r["sha"] in gezien_sha or ("sha:" + r["sha"]) in bestaand:
                    os.remove(r["pad"])
                    continue
                gezien_sha.add(r["sha"])
                bestaand |= lo.sleutels(w, r["sha"])
                naam = lo.bestandsnaam(w, namen)
                z.write(r["pad"], naam)
                grootte += r["bytes"]
                src = (w.get("primary_location") or {}).get("source") or {}
                rijen.append([wid, w.get("title") or "", lo.auteurs_van(w), w.get("publication_year") or "",
                              src.get("display_name") or "", w.get("language") or "", w.get("type") or "",
                              lo.doi_van(w), lo.handle_van(w), r["licentie"],
                              (w.get("open_access") or {}).get("oa_status") or "", r["url"], r["bytes"], r["sha"],
                              naam, "herhaal" if w["id"] in herhaal_set else ""])
                os.remove(r["pad"])
            print(f"  {min(i + 100, len(nieuw))}/{len(nieuw)} verwerkt ({len(rijen)} gelukt, {len(mislukt)} mislukt)",
                  flush=True)
        regels = [f"Open-access literatuur - maandelijkse bijwerking, publicaties vanaf {vanaf} (stand {van} t/m "
                  f"{tot - dt.timedelta(days=1)})", f"Samengesteld op {time.strftime('%Y-%m-%d')} uit OpenAlex/Unpaywall; "
                  "alleen openlijk gepubliceerde pdf's, ongewijzigd. Rechten blijven bij auteurs/uitgevers; licentie per "
                  "werk staat in de index.", f"Gelukt: {len(rijen)}   Mislukt: {len(mislukt)}", ""]
        regels += [f"{r[14]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[7] or r[8]} | {r[9]}" for r in rijen]
        if mislukt:
            regels += ["", "Mislukt:"] + mislukt
        z.writestr("_OVERZICHT.txt", "\n".join(regels) + "\n")
        z.close()
    finally:
        shutil.rmtree(werk, ignore_errors=True)
    print(f"{zn}: {len(rijen)} pdf's, {len(mislukt)} mislukt, opnieuw volgende run {len(open_)}", flush=True)
    verslag = {"run": time.strftime("%Y-%m-%d"), "van": van.isoformat(), "tot": tot.isoformat(),
               "stukken": len(rijen), "mislukt": len(mislukt), "zip": zn}
    return _sluit_af(stand, bron, LIT_LABEL, LIT_MAP, van, tot, zn,
                     ["openalex_id", "titel", "auteurs", "jaar", "tijdschrift", "taal", "type", "doi", "handle",
                      "licentie", "oa_status", "url", "bytes", "sha256", "bestand", "herhaal"],
                     rijen, mislukt, open_, droog, verslag, bool(rijen))


def run(bron, droog=False, max_n=0, sinds=None):
    return {"cao_register": run_cao_register, "avv": run_avv, "literatuur_oa": run_literatuur_oa}[bron](
        bron, droog, max_n, sinds)
