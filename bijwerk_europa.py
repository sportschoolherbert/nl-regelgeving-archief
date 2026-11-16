"""Wekelijks bijwerken: Europees recht (Cellar) en EHRM (HUDOC). Alles naar map 01.

Bronnen (zie REGISTER; bijwerken.py leest die):
  eu_wetgeving     CELEX-sectoren 1 (verdragen), 3 (wetgeving, geldend en vervallen) en 0
                   (geconsolideerde teksten, CELEX 0jjjjXnnnn-jjjjmmdd). Nederlandse xhtml,
                   anders pdf, anders html. Zip: Bijwerking_EU_wetgeving_<van>_tot_<tot>.zip
  eu_voorbereidend sector 5 (COM-voorstellen, mededelingen, SEC, JOIN ...), zelfde vorm.
  eu_rechtspraak   sector 6: arresten/beschikkingen Hof en Gerecht, conclusies AG, samenvattingen.
  ehrm             HUDOC, doctype HEJUD (arresten + beslissingen), docx + txt per uitspraak.

Datumfilter (getest 16-11 voor venster vanaf 2026-11-09):
  EU:   cmr:lastModificationDate (http://publications.europa.eu/ontology/cdm/cmr#) op de
        NEDERLANDSE MANIFESTATIE (expression_uses_language NLD -> manifestation). Dat geeft
        precies de stukken waarvan de Nederlandse tekst nieuw is of is vervangen (nieuwe akte,
        corrigendum, nieuwe geconsolideerde versie, later toegevoegde vertaling). Op werkniveau
        (cmr:lastModificationDate op ?w) geeft het ruis: 7.357 sector-3-werken, want elke
        metadatawijziging telt mee. cdm:work_date_document en cmr:creationDate missen
        gewijzigde en later vertaalde stukken.
  EHRM: kpdate:[van TO tot] (publicatiedatum in HUDOC) binnen contentsitename:ECHR AND
        doctype="HEJUD", precies de query-vorm van echr.jaarquery.

Gedrag: eerst stand.data["open"] (max bijwerk_lib.MAX_POGINGEN runs), dan het venster; één
mislukte download laat de run niet crashen; stukken waarvan Cellar wel een Nederlandse manifestatie
meldt maar nog geen bestand geeft komen als 'geen_nl' in de index en gaan de volgende run
opnieuw mee (open-lijst, max MAX_POGINGEN); meerdelige stukken (HTTP 300: akte + bijlagen)
komen als <CELEX>_deel1.pdf, _deel2.pdf ... in de zip; volgorde xhtml, pdf, html. Tijdgrens: env TIJDGRENS_MIN (standaard 300 minuten).

Benodigd (de workflow installeert dit): pip install requests playwright (gaten.Drive gebruikt alleen
requests); voor ehrm ook: playwright install --with-deps chromium. EU-bronnen hebben alleen requests nodig.
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN; optioneel THREADS_EU (standaard 6).
"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import os
import re
import time
import zipfile

import requests

import bijwerk_lib as L
from gaten import CELLAR, SPARQL, UA, veilig

REGISTER = {"eu_wetgeving": "week", "eu_voorbereidend": "week", "eu_rechtspraak": "week", "ehrm": "week"}

START = time.time()
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "300")) * 60
THREADS = int(os.environ.get("THREADS_EU", "6"))
MAP = "01"

NL = "<http://publications.europa.eu/resource/authority/language/NLD>"
PREFIX = ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> "
          "PREFIX cmr: <http://publications.europa.eu/ontology/cdm/cmr#> "
          "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> ")
# bron: (label, CELEX-regex, omschrijving)
EU_BRONNEN = {
    "eu_wetgeving": ("EU_wetgeving", "^[013]", "verdragen (1), wetgeving (3) en geconsolideerde teksten (0)"),
    "eu_voorbereidend": ("EU_voorbereidend", "^5", "voorbereidende handelingen (5)"),
    "eu_rechtspraak": ("EU_rechtspraak", "^6", "rechtspraak Hof/Gerecht en conclusies AG (6)"),
}
S = requests.Session()
S.headers.update(UA)


# ---------------------------------------------------------------- EU (Cellar)
def sparql_rijen(body):
    laatste = ""
    for i in range(5):
        try:
            r = S.post(SPARQL, data={"query": PREFIX + body}, timeout=900,
                       headers={"Accept": "application/sparql-results+json"})
            if r.status_code == 200:
                return [{k: v["value"] for k, v in b.items()} for b in r.json()["results"]["bindings"]]
            laatste = f"HTTP {r.status_code}"
        except (requests.RequestException, KeyError, ValueError) as e:
            laatste = type(e).__name__
        time.sleep(20 * (i + 1))
    raise RuntimeError("SPARQL faalt: " + laatste)


def eu_records(rx, van, tot):
    """Per CELEX de laatste wijziging van de Nederlandse manifestatie in [van, tot)."""
    rijen = sparql_rijen(
        "SELECT DISTINCT ?c ?d WHERE { ?w cdm:resource_legal_id_celex ?c . "
        "?e cdm:expression_belongs_to_work ?w . "
        f"?e cdm:expression_uses_language {NL} . "
        "?m cdm:manifestation_manifests_expression ?e . ?m cmr:lastModificationDate ?d . "
        f'FILTER(?d >= "{van.isoformat()}T00:00:00"^^xsd:dateTime && '
        f'?d < "{tot.isoformat()}T00:00:00"^^xsd:dateTime) '
        f'FILTER(regex(str(?c), "{rx}")) }}')
    uit, totaal = {}, len(rijen)
    for r in rijen:
        c = r["c"]
        if c not in uit or r["d"] > uit[c]["wijziging"]:
            uit[c] = {"id": c, "wijziging": r["d"]}
    return sorted(uit.values(), key=lambda x: x["id"]), totaal


VORMEN = (("xhtml", "application/xhtml+xml", "xhtml"), ("pdf", "application/pdf", "pdf"),
          ("html", "text/html", "html"))


def eu_delen(r):
    """HTTP 300 (Multiple Choice): een manifestatie met meer bestanden (akte + bijlagen als DOC_1, DOC_2 ...).
    -> [(bestandsnaam-in-Cellar, url)]."""
    delen = re.findall(r'<a href="([^"]+/DOC_\d+)">.*?title="stream_name">([^<]+)<', r.text, re.S)
    return [(naam, url) for url, naam in delen]


def eu_een(rec):
    """(soort, [(bestandsnaam, bytes)], fout): soort xhtml|pdf|html|geen_nl|fout.
    Volgorde: Nederlandse xhtml, pdf, html (sommige arresten bestaan alleen als html).
    Meerdelige manifestaties (HTTP 300) worden per deel opgehaald: <CELEX>_deel1.pdf, ..."""
    celex = rec["id"]
    if time.time() - START > GRENS_SEC:
        return "fout", None, "tijd: niet meer aan begonnen binnen de tijdgrens van de job"
    pad = requests.utils.quote(celex, safe="")
    laatste = ""
    for soort, acc, ext in VORMEN:
        for i in range(4):
            try:
                r = S.get(CELLAR + pad, timeout=120, headers={"Accept": acc, "Accept-Language": "nld"})
            except requests.RequestException as e:
                laatste = type(e).__name__
                time.sleep(5 * (i + 1) ** 2)
                continue
            if r.status_code == 200 and len(r.content) > 800 and r.headers.get("Content-Type", "").startswith(acc):
                if soort == "pdf" and not r.content.startswith(b"%PDF-"):
                    return "fout", None, "pdf zonder kop"
                return soort, [(veilig(celex) + "." + ext, r.content)], ""
            if r.status_code == 300:
                uit = []
                for k, (naam, url) in enumerate(eu_delen(r), 1):
                    d = None
                    for j in range(3):
                        try:
                            x = S.get(url, timeout=180)
                            if x.status_code == 200 and len(x.content) > 200:
                                d = x.content
                                break
                        except requests.RequestException:
                            pass
                        time.sleep(5 * (j + 1))
                    if d is None:
                        return "fout", None, f"{soort}: deel {k} ({naam}) niet op te halen"
                    e2 = os.path.splitext(naam)[1].lstrip(".").lower() or ext
                    uit.append((f"{veilig(celex)}_deel{k}.{e2}", d))
                if uit:
                    return soort, uit, ""
                break
            if r.status_code in (200, 404, 406):
                break           # geen versie in deze vorm; volgende vorm proberen
            laatste = f"HTTP {r.status_code}"
            time.sleep(5 * (i + 1) ** 2)
        else:
            return "fout", None, f"{soort}: {laatste}"
    # de SPARQL zei dat er een Nederlandse manifestatie is: geen bestand = nog niet geladen, volgende week opnieuw
    return "geen_nl", None, ""


def run_eu(bron, droog, max_n, sinds):
    label, rx, omschr = EU_BRONNEN[bron]
    stand = L.Stand(bron)
    van, tot = stand.venster(sinds)
    recs, totaal = eu_records(rx, van, tot)
    nieuw = {r["id"]: r for r in recs}
    herhaal = [r for r in stand.data.get("open", []) if r["id"] not in nieuw]
    herhaal_ids = {r["id"] for r in herhaal}
    taken = herhaal + recs
    if max_n:
        taken = taken[:max_n]
    print(f"{bron}: {van} t/m {tot - dt.timedelta(days=1)}: {totaal} regels, {len(recs)} CELEX-nummers, "
          f"+ {len(herhaal)} uit herhaalronde -> {len(taken)} stukken", flush=True)

    zn = L.zipnaam(label, van, tot)
    rijen, mislukt, open_ = [], [], []
    n = {"xhtml": 0, "pdf": 0, "html": 0, "geen_nl": 0}
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            for r, (soort, delen, fout) in zip(taken, ex.map(eu_een, taken)):
                pog = int(r.get("_pogingen", 0)) + 1
                if soort == "fout":
                    mislukt.append(f"{r['id']}: {fout}")
                    if pog < L.MAX_POGINGEN:
                        open_.append({**r, "_pogingen": pog})
                    continue
                n[soort] = n.get(soort, 0) + 1
                if soort == "geen_nl" and pog < L.MAX_POGINGEN:
                    # SPARQL kent een Nederlandse manifestatie maar het bestand is er nog niet: volgende run opnieuw
                    open_.append({**r, "_pogingen": pog})
                for naam, data in (delen or []):
                    z.writestr(naam, data)
                rijen.append([r["id"], ";".join(nm for nm, _ in (delen or [])), soort, r.get("wijziging", ""),
                              sum(len(d) for _, d in (delen or [])),
                              hashlib.sha256(b"".join(d for _, d in delen)).hexdigest() if delen else "",
                              "herhaal" if r["id"] in herhaal_ids else ""])
        z.writestr("_OVERZICHT.txt",
                   f"Wekelijkse bijwerking: {label} ({omschr})\n"
                   "Bron: Cellar (publications.europa.eu), SPARQL op cmr:lastModificationDate van de "
                   "Nederlandse manifestatie\n"
                   f"Nieuw of gewijzigd van {van} t/m {tot - dt.timedelta(days=1)}\n"
                   f"Verzameld: {time.strftime('%Y-%m-%d %H:%M')}\n"
                   f"CELEX-nummers: {len(recs)}, herhaalronde: {len(herhaal)}, in deze zip behandeld: {len(taken)}\n"
                   f"Nederlandse xhtml: {n['xhtml']}, pdf: {n['pdf']}, html: {n['html']}, "
                   f"bestand (nog) niet beschikbaar: {n['geen_nl']}, mislukt: {len(mislukt)}\n"
                   "Bestanden ongewijzigd, bestandsnaam = CELEX; sha256 in de index (map 00).\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))
    print(f"{zn}: {n} mislukt {len(mislukt)}, opnieuw proberen volgende week {len(open_)}", flush=True)
    idx = L.schrijf_index(zn, ["celex", "bestand", "soort", "gewijzigd", "bytes", "sha256", "herhaal"], rijen)
    return afronden(stand, bron, droog, zn, idx, mislukt, open_, taken, van, tot,
                    dict(n))


# ---------------------------------------------------------------- EHRM (HUDOC)
def hudoc_query(van, tot):
    """kpdate van t/m (tot - 1 dag); zelfde vorm als echr.jaarquery."""
    laatste = tot - dt.timedelta(days=1)
    return (f'contentsitename:ECHR AND doctype="HEJUD" '
            f'AND kpdate:[{van.isoformat()} TO {laatste.isoformat()}]')


def hudoc_docx_lijst(ctx, pg, taken, echr):
    """DOCX per record, 2 rondes, met tijdgrens. -> (data, mislukt-records)."""
    data, open_ = {}, list(taken)
    for ronde in (1, 2):
        rest = []
        for c in open_:
            if time.time() - START > GRENS_SEC:
                c["_fout"] = "tijd: niet meer aan begonnen binnen de tijdgrens van de job"
                rest.append(c)
                continue
            try:
                d = echr.haal_docx(ctx, c["itemid"])
            except Exception as e:
                d = None
                c["_fout"] = type(e).__name__
            if d:
                data[c["itemid"]] = d
            else:
                c.setdefault("_fout", "docx niet op te halen")
                rest.append(c)
        print(f"  ronde {ronde}: {len(data)} binnen, {len(rest)} nog niet", flush=True)
        if not rest or ronde == 2:
            return data, rest
        open_ = rest
        time.sleep(30)
        try:
            pg.goto(echr.HOME, timeout=120000, wait_until="load")
            pg.wait_for_timeout(8000)
        except Exception:
            pass
    return data, open_


def run_ehrm(bron, droog, max_n, sinds):
    import echr
    stand = L.Stand(bron)
    van, tot = stand.venster(sinds)
    query = hudoc_query(van, tot)
    p = b = None
    try:
        p, b, ctx, pg = echr.browser()
        totaal, recs = echr.lijst(ctx, pg, query)
        nieuw = {c["itemid"] for c in recs}
        herhaal = [{"itemid": r["id"], "docname": r.get("titel", ""), "kpdate": r.get("datum", ""),
                    "appno": r.get("appno", ""), "_pogingen": r.get("_pogingen", 0)}
                   for r in stand.data.get("open", []) if r["id"] not in nieuw]
        herhaal_ids = {c["itemid"] for c in herhaal}
        taken = herhaal + recs
        if max_n:
            taken = taken[:max_n]
        print(f"ehrm: kpdate {van} t/m {tot - dt.timedelta(days=1)}: {totaal} in HUDOC, "
              f"+ {len(herhaal)} uit herhaalronde -> {len(taken)} stukken", flush=True)
        data, rest = hudoc_docx_lijst(ctx, pg, taken, echr) if taken else ({}, [])
    finally:
        if b:
            b.close()
        if p:
            p.stop()

    zn = L.zipnaam("EHRM_uitspraken", van, tot)
    kop = (f"Wekelijkse bijwerking: EHRM-uitspraken (HUDOC, doctype HEJUD), kpdate {van} t/m "
           f"{tot - dt.timedelta(days=1)}; herhaalronde: {len(herhaal)}")
    mislukt = [f"{c['itemid']}: {c.get('_fout', 'onbekend')}" for c in rest]
    rijen_zip = echr.schrijf_zip(zn, "ehrm", taken, data, rest, totaal, kop)
    ok_ids = {m["id"] for _, m in rijen_zip}
    rijen, open_ = [], []
    for c in taken:
        i = c["itemid"]
        rijen.append([i, f"ehrm/{i}.docx" if i in ok_ids else "", " ".join(str(c.get("docname", "")).split()),
                      str(c.get("kpdate", ""))[:10], c.get("appno", ""),
                      len(data.get(i) or b""), hashlib.sha256(data[i]).hexdigest() if i in data else "",
                      f"https://hudoc.echr.coe.int/eng?i={i}", "herhaal" if i in herhaal_ids else ""])
    for c in rest:
        pog = int(c.get("_pogingen", 0)) + 1
        if pog < L.MAX_POGINGEN:
            open_.append({"id": c["itemid"], "titel": " ".join(str(c.get("docname", "")).split()),
                          "datum": str(c.get("kpdate", ""))[:10], "appno": c.get("appno", ""), "_pogingen": pog})
    print(f"{zn}: {len(rijen_zip)} uitspraken, {len(mislukt)} mislukt van {len(taken)}", flush=True)
    idx = L.schrijf_index(zn, ["id", "bestand", "titel", "datum", "bron", "docx_bytes", "docx_sha256", "url",
                               "herhaal"], rijen)
    return afronden(stand, bron, droog, zn, idx, mislukt, open_, taken, van, tot,
                    {"docx": len(rijen_zip)})


# ---------------------------------------------------------------- gedeeld
def afronden(stand, bron, droog, zn, idx, mislukt, open_, taken, van, tot, extra):
    if taken:
        L.upload_resultaat(stand, zn, MAP, idx, L.schrijf_mislukt(zn, mislukt), droog)
    else:
        print("niets nieuw: geen zip", flush=True)
        for f in (zn, idx):
            if os.path.exists(f):
                os.remove(f)
    verslag = {"run": time.strftime("%Y-%m-%d"), "van": van.isoformat(), "tot": tot.isoformat(),
               "stukken": len(taken), "mislukt": len(mislukt), "zip": zn, **extra}
    if not droog:
        stand.bewaar(dt.date.today().isoformat(), open_, verslag)
    return verslag


def run(bron, droog=False, max_n=0, sinds=None):
    if bron == "ehrm":
        return run_ehrm(bron, droog, max_n, sinds)
    return run_eu(bron, droog, max_n, sinds)
