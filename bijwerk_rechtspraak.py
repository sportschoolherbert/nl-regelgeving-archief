"""Wekelijks bijwerken: rechtspraak (rechtspraak.nl), tuchtrecht en Raad van State.

Bronnen en hun datumfilter (alle drie getest 16-11):
  rechtspraak     data.rechtspraak.nl/uitspraken/zoeken met `modified=<van>T00:00:00` en
                  `modified=<tot>T00:00:00` (bovengrens exclusief) + `return=DOC` (alleen
                  uitspraken mét tekst). `modified` vangt nieuwe én gewijzigde uitspraken;
                  `date` zou alleen op uitspraakdatum filteren en wijzigingen missen.
                  Inhoud: /uitspraken/content?id=<ECLI> -> ECLI_..xml (zoals de oogst, map 04).
  tucht           SRU repository.overheid.nl: `c.product-area==tuchtrecht and dt.modified>=van
                  and dt.modified<tot`; xml + pdf per uitspraak (zoals tucht.py, map 04).
  rvs_uitspraken  raadvanstate.nl/uitspraken/ (lijst staat op datum, nieuwste eerst): bladeren
                  met pager_rows=100 tot de datum onder de grens komt. Alleen ECLI's die NIET
                  op data.rechtspraak.nl staan (zoals rvs.py), map 04.
  rvs_adviezen    raadvanstate.nl/adviezen/ idem; alleen adviezen met datum (= tekst gepubliceerd),
                  map 07.
De RvS-lijst heeft geen datumfilter in de url (alleen `actualiteit` = week/maand/jaar), daarom
wordt op de gesorteerde lijst gebladerd. Items die eerder in deze bron zijn opgehaald staan in
de stand (`gezien`) en worden overgeslagen. Terugkijken: RVS_TERUGKIJK_DAGEN (7) voor late
publicaties; RVS_WACHT_DAGEN (7) houdt uitspraken tijdelijk achter zodat rechtspraak.nl eerst
kan publiceren.

TEMPO RvS (besluit eigenaar): 1 verbinding, RVS_PAUZE 2,5 s tussen verzoeken (ook lijstpagina's),
bij 429/503 verdubbelt rvs.get de pauze, stop na 15 weigeringen op rij. Niet versnellen.
"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import os
import re
import time
import zipfile

import requests

import kst
import tucht
import rvs
import bijwerk_lib as L

START = time.time()
GRENS_SEC = int(os.environ.get("TIJDGRENS_MIN", "300")) * 60
THREADS = int(os.environ.get("THREADS", "6"))
TERUGKIJK = int(os.environ.get("RVS_TERUGKIJK_DAGEN", "7"))
WACHT = int(os.environ.get("RVS_WACHT_DAGEN", "7"))   # rechtspraak.nl loopt 3-7 dagen achter op raadvanstate.nl
MAX_GEZIEN = 3000
RS = "https://data.rechtspraak.nl/uitspraken"

# bron: (label, mapsleutel)
BRONNEN = {
    "rechtspraak": ("Rechtspraak", "04"),
    "tucht": ("Tuchtrecht", "04"),
    "rvs_uitspraken": ("RvS_uitspraken", "04"),
    "rvs_adviezen": ("RvS_adviezen", "07"),
}
REGISTER = {b: "week" for b in BRONNEN}

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (regelgeving-archief; open data)"})


def veilig(i):
    return re.sub(r"[^\w.\-]", "_", i)


def te_laat():
    return time.time() - START > GRENS_SEC


# ---------------------------------------------------------------- rechtspraak
def rs_get(url, params=None, pogingen=5):
    """Bytes of None bij 404; bij blijvend weigeren een RuntimeError."""
    for i in range(pogingen):
        try:
            r = S.get(url, params=params, timeout=120)
            if r.status_code == 200:
                return r.content
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1) ** 2)
    raise RuntimeError(f"blijft weigeren: {url} {params}")


def rs_records(van, tot, max_n=0):
    """ECLI's met `modified` in [van, tot), mét tekst. Geeft (lijst, totaal volgens de server)."""
    uit, frm, totaal = {}, 0, None
    while True:
        x = rs_get(f"{RS}/zoeken", [("modified", f"{van.isoformat()}T00:00:00"),
                                    ("modified", f"{tot.isoformat()}T00:00:00"),
                                    ("return", "DOC"), ("max", 1000), ("from", frm)])
        if x is None:
            raise RuntimeError(f"rechtspraak zoeken 404 bij from={frm}")
        t = x.decode("utf8", "replace")
        if totaal is None:
            m = re.search(r"Aantal gevonden ECLI's: (\d+)", t)
            totaal = int(m.group(1)) if m else None
        entries = re.findall(r"<entry>(.*?)</entry>", t, re.S)
        for e in entries:
            i = re.search(r"<id>(ECLI:[^<]+)</id>", e)
            if not i:
                continue
            ti = re.search(r'<title type="text">([^<]*)<', e)
            u = re.search(r"<updated>([^<]*)<", e)
            uit[i.group(1)] = {"id": i.group(1), "versie": u.group(1) if u else "",
                               "titel": re.sub(r"\s+", " ", ti.group(1)) if ti else ""}
        frm += 1000
        if len(entries) < 1000 or (max_n and len(uit) >= max_n):
            break
    if totaal is not None and not max_n and len(uit) < totaal:
        raise RuntimeError(f"rechtspraak: {len(uit)} van {totaal} ECLI's opgehaald")
    return list(uit.values()), totaal or len(uit)


def haal_rs(r):
    uit = {"xml": None, "pdf": None, "fout": [], "controle": {}}
    if te_laat():
        uit["fout"].append("tijd: niet meer aan begonnen binnen de tijdgrens van de job")
        return r, uit
    try:
        d = rs_get(f"{RS}/content", {"id": r["id"]})
    except Exception as e:
        uit["fout"].append(f"xml:{type(e).__name__}: {str(e)[:150]}")
        return r, uit
    if d is None:
        uit["fout"].append("xml:404")
    elif len(d) < 300 or r["id"].encode() not in d[:20000]:
        uit["fout"].append("xml:leeg of noemt eigen ECLI niet")
    else:
        uit["xml"] = d
        uit["controle"]["xml"] = hashlib.sha256(d).hexdigest()
    return r, uit


# -------------------------------------------------------------------- tucht
def tucht_records(van, tot):
    q = f"{tucht.TOTAAL_Q} and dt.modified>={van.isoformat()} and dt.modified<{tot.isoformat()}"
    stukken = kst._knipper(q, "dt.modified", van, tot)
    if stukken is None:
        raise RuntimeError(f"laat zich niet knippen: {q}")
    gezien, totaal = {}, 0
    for qq, n in stukken:
        start = 1
        while start <= n:
            blokken = re.findall(r"<sru:record>.*?</sru:record>", kst.pagina(qq, start), re.S)
            if not blokken:
                break
            for b in blokken:
                r = tucht.rec(b)
                totaal += 1
                oud = gezien.get(r["id"])
                if oud is None or int(r["versie"] or 0) > int(oud["versie"] or 0):
                    gezien[r["id"]] = r
            start += len(blokken)
        if start - 1 < n:
            raise RuntimeError(f"{start - 1} van {n} records opgehaald: {qq}")
    return list(gezien.values()), totaal


def haal_tucht(r):
    uit = {"xml": None, "pdf": None, "fout": [], "controle": {}}
    if te_laat():
        uit["fout"].append("tijd: niet meer aan begonnen binnen de tijdgrens van de job")
        return r, uit
    for soort in ("xml", "pdf"):
        url = r.get(soort) or ""
        if not url:
            continue
        try:
            a = kst.haal(url, min_bytes=300)
        except Exception as e:
            uit["fout"].append(f"{soort}:{type(e).__name__}: {str(e)[:150]}")
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


# ---------------------------------------------------------------------- RvS
def rvs_lijst(soort, grens):
    """Items van raadvanstate.nl/<soort>/ met datum >= grens. De lijst staat op datum, nieuwste
    eerst; we bladeren (100 per pagina, 2,5 s pauze) tot een pagina helemaal onder de grens zit.
    Items zonder datum (adviezen nog aanhangig) tellen niet mee en worden overgeslagen."""
    uit, p, paginas = [], 0, 0
    while True:
        r = rvs.get(f"{rvs.BASIS}/{soort}/", {"pager_rows": 100, "pager_page": p})
        if r is None or r.status_code != 200:
            raise RuntimeError(f"lijstpagina {soort} {p} niet op te halen")
        paginas += 1
        blokken = re.split(r'(?=<div class="grid-blok[^"]*rol-entry)', r.text)[1:]
        gedateerd = oud = 0
        for b in blokken:
            m = re.search(r"/" + soort + r"/(@\d+)/([^/\"]+)/", b)
            if not m:
                continue
            t = rvs.plat(b)
            e = re.search(r"ECLI[ |]+(ECLI:[A-Za-z]+:[A-Za-z0-9]+:\d{4}:[A-Za-z0-9.]+)", t)
            k = re.search(r"Kenmerk[ |]+([A-Z0-9./-]+)", t)
            d = re.search(r"Datum (?:uitspraak|advies)[ |]+(\d{1,2} \w+ \d{4})", t)
            datum = rvs.iso(d.group(1)) if d else ""
            if not datum:
                continue
            gedateerd += 1
            if datum < grens:
                oud += 1
                continue
            uit.append({"id": m.group(1), "slug": m.group(2), "soort": soort,
                        "url": f"{rvs.BASIS}/{soort}/{m.group(1)}/{m.group(2)}/",
                        "ecli": e.group(1) if e else "", "kenmerk": k.group(1) if k else m.group(2),
                        "datum": datum})
        time.sleep(rvs.PAUZE)
        if len(blokken) < 100 or (gedateerd and oud == gedateerd):
            break
        p += 1
        if p > 200:
            raise RuntimeError(f"{soort}: meer dan 200 lijstpagina's, grens {grens} niet bereikt")
    print(f"  {soort}: {paginas} lijstpagina's, {len(uit)} items vanaf {grens}", flush=True)
    return uit


def rvs_bekend(grens, tot):
    """ECLI's van de Raad van State op data.rechtspraak.nl in de periode (= al in map 04)."""
    ids, start = set(), 0
    while True:
        x = rs_get(f"{RS}/zoeken", [
            ("creator", "http://standaarden.overheid.nl/owms/terms/Raad_van_State"),
            ("date", grens), ("date", tot), ("max", 1000), ("from", start), ("return", "DOC")])
        e = re.findall(r"<id>(ECLI:[^<]+)</id>", (x or b"").decode("utf8", "replace"))
        ids.update(e)
        if len(e) < 1000:
            return ids
        start += 1000


def rvs_taken(bron, van, tot):
    """Te halen items (na filter op rechtspraak.nl, wachttijd en gezien) + tellingen."""
    soort = "uitspraken" if bron == "rvs_uitspraken" else "adviezen"
    grens = (van - dt.timedelta(days=max(TERUGKIJK, WACHT + 1))).isoformat()
    items = rvs_lijst(soort, grens)
    tel = {"lijst": len(items), "op_rechtspraak": 0, "te_vers": 0}
    if soort == "uitspraken":
        bekend = rvs_bekend(grens, (tot - dt.timedelta(days=1)).isoformat())
        nieuw = []
        for x in items:
            if x["ecli"] and x["ecli"] in bekend:
                tel["op_rechtspraak"] += 1
            elif WACHT and x["datum"] > (dt.date.today() - dt.timedelta(days=WACHT)).isoformat():
                tel["te_vers"] += 1
            else:
                nieuw.append(x)
        items = nieuw
    return items, tel


def haal_rvs(x):
    """(data, fout) voor één RvS-pagina. Geen pauze hier; die doet de aanroeper."""
    r = rvs.get(x["url"])
    if r is None:
        return None, "geweigerd"
    data = r.content
    if r.status_code == 404 or len(data) < 3000:
        return None, f"HTTP {r.status_code}, {len(data)} bytes"
    tekst = data.decode("utf8", "replace")
    ident = x["ecli"] or x["kenmerk"]
    if ident not in tekst and x["slug"] not in tekst:
        return None, "pagina noemt het eigen kenmerk niet"
    return data, ""


# ---------------------------------------------------------------------- run
def run(bron, droog=False, max_n=0, sinds=None):
    label, mapsleutel = BRONNEN[bron]
    stand = L.Stand(bron)
    van, tot = stand.venster(sinds)
    is_rvs = bron.startswith("rvs_")
    if bron == "rechtspraak":
        recs_l, totaal = rs_records(van, tot, max_n)
        sleutel = lambda r: r["id"]
        bronzin = "data.rechtspraak.nl zoeken modified (return=DOC)"
    elif bron == "tucht":
        recs_l, totaal = tucht_records(van, tot)
        sleutel = lambda r: r["id"]
        bronzin = "SRU repository.overheid.nl c.product-area==tuchtrecht dt.modified"
    else:
        recs_l, tel = rvs_taken(bron, van, tot)
        totaal = tel["lijst"]
        sleutel = lambda r: r["id"]
        bronzin = (f"raadvanstate.nl/{'uitspraken' if bron == 'rvs_uitspraken' else 'adviezen'}/ "
                   f"(lijst op datum, terugkijk {TERUGKIJK} d; op site {tel['lijst']}, al op "
                   f"rechtspraak.nl {tel['op_rechtspraak']}, achtergehouden {tel['te_vers']})")
        gezien = list(stand.data.get("gezien", []))
        gezien_set = set(gezien)
        recs_l = [r for r in recs_l if r["soort"] + r["id"] not in gezien_set]
    recs = {sleutel(r): r for r in recs_l}
    herhaal = [r for r in stand.data.get("open", []) if sleutel(r) not in recs]
    herhaal_ids = {sleutel(r) for r in herhaal}
    taken = herhaal + list(recs.values())
    if max_n:
        taken = taken[:max_n]
    print(f"{bron}: {van} t/m {tot - dt.timedelta(days=1)}: {totaal} records, {len(recs)} te halen, "
          f"+ {len(herhaal)} uit herhaalronde -> {len(taken)} stukken", flush=True)

    zn = L.zipnaam(label, van, tot)
    rijen, mislukt, open_ = [], [], []
    n_xml = n_pdf = n_html = 0

    def noteer_fout(r, fouten, gelukt):
        nonlocal mislukt, open_
        if fouten and not gelukt:
            mislukt.append(f"{r['id']}: " + ", ".join(fouten))
            structureel = not any(f.startswith(("tijd", "geweigerd")) for f in fouten)
            pog = int(r.get("_pogingen", 0)) + (1 if structureel else 0)
            if pog < L.MAX_POGINGEN:
                open_.append({**r, "_pogingen": pog})
        elif fouten:
            mislukt.append(f"{r['id']} (deels): " + ", ".join(fouten))

    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        if bron == "rechtspraak":
            with cf.ThreadPoolExecutor(THREADS) as ex:
                for r, u in ex.map(haal_rs, taken):
                    if u["xml"]:
                        z.writestr(r["id"].replace(":", "_") + ".xml", u["xml"]); n_xml += 1
                    noteer_fout(r, u["fout"], u["xml"])
                    rijen.append([r["id"], r["versie"], r["titel"], len(u["xml"] or b""),
                                  u["controle"].get("xml", ""), f"{RS}/content?id={r['id']}",
                                  "herhaal" if r["id"] in herhaal_ids else ""])
            kop = ["ecli", "gewijzigd", "titel", "xml_bytes", "xml_sha256", "url_xml", "herhaal"]
        elif bron == "tucht":
            with cf.ThreadPoolExecutor(THREADS) as ex:
                for r, u in ex.map(haal_tucht, taken):
                    base = veilig(r["id"])
                    naam = f"{base}/{r['versie'] or 'v'}"
                    if u["xml"]:
                        z.writestr(f"{naam}/{base}.xml", u["xml"]); n_xml += 1
                    if u["pdf"]:
                        z.writestr(f"{naam}/{base}.pdf", u["pdf"]); n_pdf += 1
                    noteer_fout(r, u["fout"], u["xml"] or u["pdf"])
                    rijen.append([r["id"], r["versie"], r["instantie"], r["domein"], r["datum"], r["titel"],
                                  len(u["xml"] or b""), u["controle"].get("xml", ""),
                                  len(u["pdf"] or b""), u["controle"].get("pdf", ""), r["xml"], r["pdf"],
                                  "herhaal" if r["id"] in herhaal_ids else ""])
            kop = ["ecli", "versie", "instantie", "domein", "datum", "titel", "xml_bytes", "xml_sha256",
                   "pdf_bytes", "pdf_sha256", "url_xml", "url_pdf", "herhaal"]
        else:
            weigeringen = 0
            for k, x in enumerate(taken, 1):
                fouten, data = [], None
                if te_laat():
                    fouten.append("tijd: niet meer aan begonnen binnen de tijdgrens van de job")
                elif weigeringen >= 15:
                    fouten.append("geweigerd: aanhoudende 403/429, gestopt om de site te ontzien")
                else:
                    data, f = haal_rvs(x)
                    time.sleep(rvs.PAUZE)   # 2,5 s (of verdubbeld na 429/503) na elk verzoek
                    if f.startswith("geweigerd"):
                        weigeringen += 1
                        fouten.append(f)
                    elif f:
                        weigeringen = 0
                        fouten.append(f)
                    else:
                        weigeringen = 0
                ident = x["ecli"] or x["kenmerk"]
                sha = ""
                if data:
                    z.writestr(f"{x['soort']}/{veilig(ident)}_{x['id'][1:]}.html", data)
                    n_html += 1
                    sha = hashlib.sha256(data).hexdigest()
                    gezien.append(x["soort"] + x["id"])
                noteer_fout({**x, "id": x["id"]}, fouten, data)
                rijen.append([x["soort"], ident, "Raad van State", x["datum"], x["url"],
                              len(data or b""), sha, "herhaal" if x["id"] in herhaal_ids else ""])
                if k % 50 == 0:
                    print(f"  {k}/{len(taken)}", flush=True)
            kop = ["soort", "id", "instantie", "datum", "url", "bytes", "sha256", "herhaal"]
        z.writestr("_OVERZICHT.txt",
                   f"Wekelijkse bijwerking: {label}\nBron: {bronzin}\n"
                   f"Nieuw of gewijzigd van {van} t/m {tot - dt.timedelta(days=1)}\n"
                   f"Verzameld: {time.strftime('%Y-%m-%d %H:%M')}\n"
                   f"Records: {totaal}, te halen: {len(recs)}, herhaalronde: {len(herhaal)}\n"
                   f"Xml: {n_xml}, pdf: {n_pdf}, html: {n_html}, mislukt: {len(mislukt)}\n"
                   "Bestanden ongewijzigd; sha256 staat in de index (map 00).\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))
    print(f"{zn}: {len(taken)} stukken, xml {n_xml}, pdf {n_pdf}, html {n_html}, mislukt {len(mislukt)}, "
          f"opnieuw proberen volgende week {len(open_)}", flush=True)
    idx = L.schrijf_index(zn, kop, rijen)
    if taken:
        L.upload_resultaat(stand, zn, mapsleutel, idx, L.schrijf_mislukt(zn, mislukt), droog)
    else:
        print("niets nieuw: geen zip", flush=True)
        for f in (zn, idx):
            if os.path.exists(f):
                os.remove(f)
    verslag = {"run": time.strftime("%Y-%m-%d"), "van": van.isoformat(), "tot": tot.isoformat(),
               "stukken": len(taken), "xml": n_xml, "pdf": n_pdf, "html": n_html,
               "mislukt": len(mislukt), "zip": zn}
    if not droog:
        if is_rvs:
            stand.data["gezien"] = gezien[-MAX_GEZIEN:]
        stand.bewaar(dt.date.today().isoformat(), open_, verslag)
    return verslag
