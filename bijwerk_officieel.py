"""Wekelijks bijwerken: officiële bekendmakingen en parlementaire stukken uit de SRU van
repository.overheid.nl (dezelfde index als officielebekendmakingen.nl).

Per bron één query op publicatienaam + `dt.modified>=<sinds>`: dat vangt nieuwe stukken
én nieuwe versies (correcties) van bestaande stukken. Zelfde bestandsbeleid als de oogst:
  - Gemeenteblad: xml; pdf alleen als er geen xml is (alle pdf ±1,4 TB, besluit Jan 15-11)
  - Kamerstukken, Staatsblad, Staatscourant, Tractatenblad: xml; pdf alleen als xml ontbreekt
  - Handelingen, Kamervragen, overige SG, Provinciaal blad, Waterschapsblad, bgr: xml + pdf
"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import os
import re
import time
import zipfile

import kst
import bijwerk_lib as L

THREADS = int(os.environ.get("THREADS", "12"))
START = time.time()
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "300")) * 60

SG = 'w.organisatietype=="staten generaal" and '
# bron: (label, mapsleutel, [basisqueries], pdfbeleid)   pdfbeleid: "nooit" | "zonder_xml" | "altijd"
BRONNEN = {
    "kamerstukken":     ("Kamerstukken", "08", ['w.publicatienaam=="Kamerstuk"'], "zonder_xml"),
    "handelingen":      ("Handelingen", "07", ['w.publicatienaam=="Handelingen"'], "altijd"),
    "aanhangsel":       ("Aanhangsel_Handelingen", "07", ['w.publicatienaam=="Kamervragen (Aanhangsel)"'], "altijd"),
    "kamervragen_za":   ("Kamervragen_zonder_antwoord", "07", ['w.publicatienaam=="Kamervragen zonder antwoord"'], "altijd"),
    "overig_parlement": ("Overig_Staten-Generaal", "07", [SG + 'w.publicatienaam=="Niet-dossierstuk"',
                                                          SG + 'w.publicatienaam=="Agenda"'], "altijd"),
    "gemeenteblad":     ("Gemeenteblad", "08", ['w.publicatienaam=="Gemeenteblad"'], "zonder_xml"),
    "provinciaal_blad": ("Provinciaal_blad", "08", ['w.publicatienaam=="Provinciaal blad"'], "altijd"),
    "waterschapsblad":  ("Waterschapsblad", "08", ['w.publicatienaam=="Waterschapsblad"'], "altijd"),
    "bgr":              ("Blad_gemeenschappelijke_regeling", "08",
                         ['w.publicatienaam=="Blad gemeenschappelijke regeling"'], "altijd"),
    "staatsblad":       ("Staatsblad", "08", ['w.publicatienaam=="Staatsblad"'], "zonder_xml"),
    "staatscourant":    ("Staatscourant", "08", ['w.publicatienaam=="Staatscourant"'], "zonder_xml"),
    "tractatenblad":    ("Tractatenblad", "08", ['w.publicatienaam=="Tractatenblad"'], "zonder_xml"),
}


def records(basis, van, tot):
    """Alle records met dt.modified in [van, tot); per id+versie één keer."""
    q = f"{basis} and dt.modified>={van.isoformat()} and dt.modified<{tot.isoformat()}"
    stukken = kst._knipper(q, "dt.modified", van, tot)
    if stukken is None:   # één dag > 9.500 (komt bij Gemeenteblad voor): dan op dt.date knippen
        stukken = []
        dag = van
        while dag < tot:
            qd = f"{basis} and dt.modified>={dag.isoformat()} and dt.modified<{(dag + dt.timedelta(days=1)).isoformat()}"
            s = kst._knipper(qd, "dt.date", dt.date(1800, 1, 1), tot + dt.timedelta(days=1))
            if s is None:
                raise RuntimeError(f"laat zich niet knippen: {qd}")
            stukken += s
            dag += dt.timedelta(days=1)
    gezien, totaal = {}, 0
    for qq, n in stukken:
        start = 1
        while start <= n:
            blokken = re.findall(r"<sru:record>.*?</sru:record>", kst.pagina(qq, start), re.S)
            if not blokken:
                break
            for b in blokken:
                r = kst.record_uit(b)
                totaal += 1
                oud = gezien.get(r["id"])
                if oud is None or int(r["versie"] or 0) > int(oud["versie"] or 0):
                    gezien[r["id"]] = r
            start += len(blokken)
        verwacht = n
        if start - 1 < verwacht:
            raise RuntimeError(f"{start - 1} van {verwacht} records opgehaald: {qq}")
    return list(gezien.values()), totaal


def haal_stuk(taak):
    r, pdfbeleid = taak
    uit = {"xml": None, "pdf": None, "fout": [], "controle": {}}
    if time.time() - START > GRENS_SEC:
        uit["fout"].append("tijd: niet meer aan begonnen binnen de tijdgrens van de job")
        return r, uit
    for soort in ("xml", "pdf"):
        url = r.get(soort) or ""
        if not url:
            continue
        if soort == "pdf" and (pdfbeleid == "nooit" or (pdfbeleid == "zonder_xml" and uit["xml"])):
            continue
        try:
            a = kst.haal(url, min_bytes=200 if soort == "xml" else 500)
        except Exception as e:
            uit["fout"].append(f"{soort}:{type(e).__name__}: {str(e)[:200]}")
            continue
        if a is None:
            uit["fout"].append(f"{soort}:404")
            continue
        fout = kst.controleer_xml(a.content, r["id"]) if soort == "xml" else kst.controleer_pdf(a.content)
        if fout:
            uit["fout"].append(f"{soort}:{fout}")
            continue
        uit[soort] = a.content
        uit["controle"][soort] = hashlib.sha256(a.content).hexdigest()
    return r, uit


def run(bron, droog=False, max_n=0, sinds=None):
    label, mapsleutel, basissen, pdfbeleid = BRONNEN[bron]
    stand = L.Stand(bron)
    van, tot = stand.venster(sinds)
    recs, totaal = {}, 0
    for basis in basissen:
        lijst, n = records(basis, van, tot)
        totaal += n
        for r in lijst:
            recs[(r["id"], r["versie"])] = r
    # automatische herhaalronde: wat vorige keer mislukte eerst
    herhaal = [r for r in stand.data.get("open", []) if (r["id"], r["versie"]) not in recs]
    taken = herhaal + list(recs.values())
    herhaal_keys = {(r["id"], r["versie"]) for r in herhaal}
    if max_n:
        taken = taken[:max_n]
    print(f"{bron}: dt.modified {van} t/m {tot - dt.timedelta(days=1)}: {totaal} records, "
          f"{len(recs)} uniek, + {len(herhaal)} uit herhaalronde -> {len(taken)} stukken", flush=True)

    zn = L.zipnaam(label, van, tot)
    rijen, mislukt, open_ = [], [], []
    met_xml = met_pdf = 0
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            for r, uit in ex.map(haal_stuk, [(r, pdfbeleid) for r in taken]):
                naam = f"{r['id']}/{r['versie'] or 'v'}"
                if uit["xml"]:
                    z.writestr(f"{naam}/{r['id']}.xml", uit["xml"]); met_xml += 1
                if uit["pdf"]:
                    z.writestr(f"{naam}/{r['id']}.pdf", uit["pdf"]); met_pdf += 1
                if uit["fout"] and not uit["xml"] and not uit["pdf"]:
                    mislukt.append(f"{r['id']}: " + ", ".join(uit["fout"]))
                    pog = int(r.get("_pogingen", 0)) + 1
                    if pog < L.MAX_POGINGEN:
                        open_.append({**r, "_pogingen": pog})
                elif uit["fout"]:
                    mislukt.append(f"{r['id']} (deels): " + ", ".join(uit["fout"]))
                rijen.append([r["id"], r["versie"], r["titel"], r["type"], r["maker"], r["datum"],
                              len(uit["xml"] or b""), uit["controle"].get("xml", ""),
                              len(uit["pdf"] or b""), uit["controle"].get("pdf", ""), r["xml"], r["pdf"],
                              "herhaal" if (r["id"], r["versie"]) in herhaal_keys else ""])
        z.writestr("_OVERZICHT.txt",
                   f"Wekelijkse bijwerking: {label}\nBron: SRU repository.overheid.nl, "
                   f"{' / '.join(basissen)}\nGewijzigd of nieuw (dt.modified) van {van} t/m "
                   f"{tot - dt.timedelta(days=1)}\nVerzameld: {time.strftime('%Y-%m-%d %H:%M')}\n"
                   f"Records: {totaal}, uniek: {len(recs)}, herhaalronde: {len(herhaal)}\n"
                   f"Xml: {met_xml}, pdf: {met_pdf} (pdf-beleid: {pdfbeleid}), mislukt: {len(mislukt)}\n"
                   "Mappen: <id>/<versie>/<id>.xml|pdf. Bestanden ongewijzigd; sha256 in de index (map 00).\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))
    print(f"{zn}: {len(taken)} stukken, xml {met_xml}, pdf {met_pdf}, mislukt {len(mislukt)}, "
          f"opnieuw proberen volgende week {len(open_)}", flush=True)
    idx = L.schrijf_index(zn, ["id", "versie", "titel", "type", "maker", "datum", "xml_bytes", "xml_sha256",
                               "pdf_bytes", "pdf_sha256", "url_xml", "url_pdf", "herhaal"], rijen)
    if taken:
        L.upload_resultaat(stand, zn, mapsleutel, idx, L.schrijf_mislukt(zn, mislukt), droog)
    else:
        print("niets nieuw: geen zip", flush=True)
        for f in (zn, idx):
            if os.path.exists(f): os.remove(f)
    verslag = {"run": time.strftime("%Y-%m-%d"), "van": van.isoformat(), "tot": tot.isoformat(),
               "stukken": len(taken), "xml": met_xml, "pdf": met_pdf, "mislukt": len(mislukt), "zip": zn}
    if not droog:
        stand.bewaar(dt.date.today().isoformat(), open_, verslag)
    return verslag
