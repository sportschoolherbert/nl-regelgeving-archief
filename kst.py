"""Kamerstukken: alle officiële bekendmakingen met publicatienaam Kamerstuk.

Bron: de SRU van repository.overheid.nl (dezelfde index als de zoekmachine van
officielebekendmakingen.nl). Elke Kamerstuk heeft daar een record met de
manifestaties die de overheid zelf publiceert: xml (de gestructureerde tekst),
pdf (de vastgestelde weergave), html, odt en metadata. Wij halen de xml en de pdf
op, ongewijzigd, en controleren ze:

  - xml moet goed gevormd zijn en zichzelf identificeren met het kst-nummer;
  - pdf moet met %PDF- beginnen en een %%EOF bevatten.

Van elk bestand wordt de sha256 in de index gezet, zodat het later na te rekenen
is. Ontbreekt een manifestatie (bijlagen zijn er soms alleen als pdf), dan wordt
dat in de index vastgelegd; een mislukte download laat de shard stoppen zodat er
nooit een halve shard als 'klaar' in Drive staat.

De SRU geeft per verzoek maximaal 10.000 records terug (startRecord 10.000 geeft
een 504). Daarom wordt de set eerst in stukken geknipt die elk onder de 9.500
blijven: per maand, waar nodig per dag, en waar nodig per vergaderjaar. De som
van alle stukken moet exact het opgegeven totaal zijn, anders stopt prep.

  python kst.py plan                 -> laten zien hoe de set verdeeld wordt
  python kst.py prep                 -> kst_plan/ + de shards die nog open zijn
  python kst.py run <shard> [--max N] [--droog]
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN
"""

import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile

import requests

from gaten import Drive

SRU = "https://repository.overheid.nl/sru"
BASIS = 'w.publicatienaam=="Kamerstuk"'
MAP = "08"
PER_SHARD = 5000
LIMIET = 9500             # server staat max 10.000 records per query toe
PLAN = "kst_plan"
THREADS = 8

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (regelgeving-archief; open data)"})


def haal(url, timeout=180, pogingen=6, allow_redirects=True, min_bytes=0):
    """GET met backoff. 404 geeft None; blijft het weigeren, dan een fout.

    De repository antwoordt onder belasting soms met status 200 en een body van
    nog geen 200 bytes (een lege placeholder). Dat is geen bestand; met
    min_bytes wordt zo'n antwoord opnieuw geprobeerd tot het echte bestand komt.
    """
    for i in range(pogingen):
        try:
            r = S.get(url, timeout=timeout, allow_redirects=allow_redirects)
            if r.status_code == 200:
                if len(r.content) < min_bytes:
                    time.sleep(min(60, 3 * (i + 1) ** 2))
                    continue
                return r
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(60, 3 * (i + 1) ** 2))
                continue
            return None
        except requests.RequestException:
            time.sleep(min(60, 3 * (i + 1) ** 2))
    raise RuntimeError("blijft weigeren: " + url)


def tel(query):
    """Aantal records voor een query, met dezelfde backoff als de downloads."""
    laatste = ""
    for i in range(6):
        try:
            r = S.get(SRU, params={"operation": "searchRetrieve", "version": "2.0",
                                   "query": query, "maximumRecords": 1}, timeout=300)
        except requests.RequestException as e:
            laatste = type(e).__name__
            time.sleep(min(60, 3 * (i + 1) ** 2))
            continue
        if r.status_code == 200:
            m = re.search(r"<sru:numberOfRecords>(\d+)", r.text)
            if m:
                return int(m.group(1))
            d = re.search(r"<ns3:message>([^<]+)", r.text)
            raise RuntimeError("SRU-telling zonder aantal: " + (d.group(1) if d else query))
        laatste = f"HTTP {r.status_code}"
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(60, 3 * (i + 1) ** 2))
            continue
        raise RuntimeError(f"SRU-telling mislukt ({r.status_code}): {query}")
    raise RuntimeError(f"SRU-telling blijft mislukken ({laatste}): {query}")


def pagina(query, start, n=1000):
    r = haal(SRU + "?" + "&".join([
        "operation=searchRetrieve", "version=2.0",
        "query=" + requests.utils.quote(query, safe=""),
        f"maximumRecords={n}", f"startRecord={start}"]), timeout=300)
    if r is None:
        raise RuntimeError(f"SRU geeft 404 op {query} vanaf {start}")
    return r.text


def record_uit(blok):
    def één(pat, tekst=blok):
        m = re.search(pat, tekst, re.S)
        return m.group(1).strip() if m else ""
    ident = één(r"<dcterms:identifier>([^<]+)</dcterms:identifier>")
    urls = dict(re.findall(r'<gzd:itemUrl manifestation="([^"]+)">([^<]+)</gzd:itemUrl>', blok))
    versie = ""
    m = re.search(r"/" + re.escape(ident) + r"/(\d+)/", urls.get("xml", "") or urls.get("pdf", ""))
    if m:
        versie = m.group(1)
    return {
        "id": ident,
        "titel": één(r"<dcterms:title>([^<]*)</dcterms:title>"),
        "type": één(r"<dcterms:type[^>]*>([^<]*)</dcterms:type>"),
        "maker": één(r"<dcterms:creator[^>]*>([^<]*)</dcterms:creator>"),
        "datum": één(r"<dcterms:modified>([^<]*)</dcterms:modified>"),
        "vergaderjaar": één(r"<overheidwetgeving:vergaderjaar>([^<]*)<"),
        "dossier": één(r"<overheidwetgeving:dossiernummer>([^<]*)<"),
        "onder": één(r"<overheidwetgeving:ondernummer>([^<]*)<"),
        "rubriek": één(r"<overheidwetgeving:subrubriek[^>]*>([^<]*)<"),
        "versie": versie,
        "xml": urls.get("xml", ""),
        "pdf": urls.get("pdf", ""),
    }


def records(query, verwacht=None):
    """Alle records van een query.

    De index wordt dagelijks bijgewerkt, dus tussen het tellen en het ophalen kan
    er een stuk bijkomen. Minder dan `verwacht` is een fout (dan missen we iets);
    meer is toegestaan en wordt later samengevoegd.
    """
    start, uit = 1, []
    while True:
        tekst = pagina(query, start)
        blokken = re.findall(r"<sru:record>.*?</sru:record>", tekst, re.S)
        if not blokken:
            break
        uit += [record_uit(b) for b in blokken]
        start += len(blokken)
        if verwacht is not None and len(uit) > verwacht + 2000:
            break
    if verwacht is not None and len(uit) < verwacht:
        raise RuntimeError(f"{len(uit)} records opgehaald, {verwacht} verwacht: {query}")
    return uit


def _helft(a, b):
    m = a + dt.timedelta(days=max(1, (b - a).days // 2))
    return m


def _knipper(query, veld, a, b):
    """Knipt [a, b) op `veld` tot elk stuk hooguit LIMIET records heeft.

    Geeft None als het niet lukt (één dag blijft te groot); de aanroeper kiest
    dan een ander verdeelveld. Elk stuk wordt apart geteld, dus de som van de
    stukken is per definitie wat er in dit bereik zit.
    """
    n = tel(query)
    if n == 0:
        return []
    if n <= LIMIET:
        return [(query, n)]
    if (b - a).days <= 1:
        return None
    m = _helft(a, b)
    links = _knipper(f"{query} and {veld}>={a.isoformat()} and {veld}<{m.isoformat()}", veld, a, m)
    if links is None:
        return None
    rechts = _knipper(f"{query} and {veld}>={m.isoformat()} and {veld}<{b.isoformat()}", veld, m, b)
    if rechts is None:
        return None
    return links + rechts


def verdeel(query, a, b, diepte=0):
    """Knipt een query in stukken van maximaal LIMIET records.

    Eerst op dt.modified (de datum waarop het record in de index is gezet), want
    dat is de as waarop de bulk van 2014 ligt. Blijft één dag te groot, dan op
    dt.date (de datum van het stuk zelf; die is altijd gevuld en enkelvoudig).
    Als ook dat niet lukt, op vergaderjaar. De som van de stukken moet exact het
    aantal van de query zijn, anders stopt het plan.
    """
    n = tel(query)
    if n == 0:
        return []
    if n <= LIMIET:
        return [(query, n)]
    if (b - a).days > 1:
        m = _helft(a, b)
        links = f"{query} and dt.modified>={a.isoformat()} and dt.modified<{m.isoformat()}"
        rechts = f"{query} and dt.modified>={m.isoformat()} and dt.modified<{b.isoformat()}"
        uit = verdeel(links, a, m, diepte + 1) + verdeel(rechts, m, b, diepte + 1)
    else:
        uit = _knipper(query, "dt.date", dt.date(1900, 1, 1),
                       dt.date.today() + dt.timedelta(days=2))
        if uit is None or sum(k for _, k in uit) != n:
            uit, som = [], 0
            for jaar in range(1988, 2030):
                vj = f"{jaar}-{jaar + 1}"
                deel = f'{query} and w.vergaderjaar=="{vj}"'
                k = tel(deel)
                if not k:
                    continue
                som += k
                if k > LIMIET:
                    raise RuntimeError(f"stuk van {k} records blijft te groot: {deel}")
                uit.append((deel, k))
            if som != n:
                raise RuntimeError(f"verdelen op vergaderjaar dekt {som} van {n}: {query}")
    som = sum(k for _, k in uit)
    if som != n:
        raise RuntimeError(f"verdelen dekt {som} van {n}: {query}")
    if diepte == 0:
        print(f"  plan: {len(uit)} stukken, samen {som} records", flush=True)
    return uit


def plan():
    totaal = tel(BASIS)
    print(f"{totaal} Kamerstukken", flush=True)
    a = dt.date(1990, 1, 1)
    b = dt.date.today() + dt.timedelta(days=2)
    stukken = verdeel(BASIS, a, b)
    som = sum(k for _, k in stukken)
    print(f"{len(stukken)} stukken, samen {som} records")
    if som != totaal:
        raise RuntimeError(f"plan dekt {som} van {totaal} records")
    return stukken, totaal


def shardnaam(i):
    return f"kst-{i:03d}"


def shardbestand(shard):
    return os.path.join(PLAN, shard + ".json")


def zipnaam(shard):
    return f"Kamerstukken_{shard.replace('kst-', 'deel')}.zip"


def klaar(d, mappen, shard):
    return d.zoek(zipnaam(shard), mappen[MAP]) is not None


def shards():
    if not os.path.isdir(PLAN):
        raise SystemExit("kst_plan/ ontbreekt; draai eerst: python kst.py prep")
    return sorted(f[:-5] for f in os.listdir(PLAN) if f.endswith(".json"))


def prep():
    stukken, totaal = plan()
    alle, gezien = [], {}
    for i, (query, n) in enumerate(stukken, 1):
        hier = records(query, n)
        for r in hier:
            oud = gezien.get(r["id"])
            if oud is None:
                gezien[r["id"]] = r
            else:
                # meerdere versies van hetzelfde stuk: de hoogste versie wint
                if int(r["versie"] or 0) > int(oud["versie"] or 0):
                    gezien[r["id"]] = r
        alle += hier
        print(f"  {i}/{len(stukken)} {n} records (totaal {len(alle)})", flush=True)
    if len(gezien) < totaal:
        raise RuntimeError(f"{len(gezien)} unieke stukken opgehaald van {totaal}; "
                           "er ontbreekt dus iets")
    uniek = sorted(gezien.values(), key=lambda r: r["id"])
    dubbel = len(alle) - len(uniek)
    extra = len(uniek) - totaal
    met_xml = sum(1 for r in uniek if r["xml"])
    met_pdf = sum(1 for r in uniek if r["pdf"])
    met_geen = sum(1 for r in uniek if not r["xml"] and not r["pdf"])
    print(f"{len(alle)} records -> {len(uniek)} unieke stukken "
          f"({dubbel} dubbele versies samengevoegd, {extra} nieuw sinds het tellen)")
    print(f"  met xml: {met_xml}, met pdf: {met_pdf}, met geen van beide: {met_geen}")

    if os.path.isdir(PLAN):
        for f in os.listdir(PLAN):
            os.remove(os.path.join(PLAN, f))
    os.makedirs(PLAN, exist_ok=True)
    aantal = (len(uniek) + PER_SHARD - 1) // PER_SHARD
    for i in range(aantal):
        deel = uniek[i * PER_SHARD:(i + 1) * PER_SHARD]
        with open(shardbestand(shardnaam(i)), "w", encoding="utf8") as f:
            json.dump(deel, f)
    print(f"{aantal} shards van {PER_SHARD} stukken")

    keuze = [s.strip() for s in os.environ.get("SHARDS", "").split(",") if s.strip()]
    if keuze:
        open_ = keuze
    else:
        d = Drive()
        mappen, _ = d.mappen()
        open_ = [s for s in shards() if not klaar(d, mappen, s)]
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"nog te doen: {len(open_)}")
    print(json.dumps(open_[:10]), "..." if len(open_) > 10 else "")


def controleer_xml(data, ident):
    tekst = data[:8000].decode("utf8", "ignore")
    if ident not in tekst:
        return "xml noemt het eigen nummer niet"
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(data)
    except ET.ParseError as e:
        return f"xml niet goed gevormd ({e})"
    return ""


def controleer_pdf(data):
    if not data.startswith(b"%PDF-"):
        return "geen pdf-kop"
    if b"%%EOF" not in data[-2048:]:
        return "pdf zonder %%EOF"
    if len(data) < 1000:
        return "pdf te klein"
    return ""


def haal_stuk(taak):
    r, shard = taak
    uit = {"xml": None, "pdf": None, "fout": [], "controle": {}}
    for soort in ("xml", "pdf"):
        url = r.get(soort) or ""
        if not url:
            continue
        antwoord = haal(url, min_bytes=500)
        if antwoord is None:
            uit["fout"].append(f"{soort}:404")
            continue
        data = antwoord.content
        if soort == "xml":
            fout = controleer_xml(data, r["id"])
        else:
            fout = controleer_pdf(data)
        if fout:
            uit["fout"].append(f"{soort}:{fout}")
            continue
        uit[soort] = data
        uit["controle"][soort] = hashlib.sha256(data).hexdigest()
    return r, uit


def run(shard, max_n, droog):
    with open(shardbestand(shard), encoding="utf8") as f:
        stukken = json.load(f)
    if max_n:
        stukken = stukken[:max_n]
    print(f"{shard}: {len(stukken)} stukken", flush=True)

    zn = zipnaam(shard)
    rijen, mislukt = [], []
    met_xml = met_pdf = 0
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            taken = [(r, shard) for r in stukken]
            for k, (r, uit) in enumerate(ex.map(haal_stuk, taken), 1):
                if uit["fout"]:
                    mislukt.append(f"{r['id']}: " + ", ".join(uit["fout"]))
                if uit["xml"]:
                    z.writestr(f"{r['id']}/xml/{r['id']}.xml", uit["xml"], zipfile.ZIP_DEFLATED)
                    met_xml += 1
                if uit["pdf"]:
                    z.writestr(f"{r['id']}/pdf/{r['id']}.pdf", uit["pdf"], zipfile.ZIP_DEFLATED)
                    met_pdf += 1
                rijen.append([shard, r["id"], r["titel"], r["type"], r["maker"], r["datum"],
                              r["vergaderjaar"], r["dossier"], r["onder"], r["rubriek"],
                              "xml" if uit["xml"] else "", len(uit["xml"] or b""),
                              uit["controle"].get("xml", ""),
                              "pdf" if uit["pdf"] else "", len(uit["pdf"] or b""),
                              uit["controle"].get("pdf", ""),
                              r["xml"], r["pdf"]])
                if k % 500 == 0:
                    print(f"  {k}/{len(taken)}", flush=True)

        z.writestr("_OVERZICHT.txt",
                   "Kamerstukken (officiële bekendmakingen, publicatienaam Kamerstuk)\n"
                   f"Shard: {shard}\nVerzameld: {time.strftime('%Y-%m-%d')}\n"
                   f"Stukken: {len(stukken)}\nXml-bestanden: {met_xml}\nPdf-bestanden: {met_pdf}\n"
                   f"Mislukt: {len(mislukt)}\n"
                   "\nBron van elk bestand:\n"
                   "  de manifestatie-url's uit het SRU-record van repository.overheid.nl,\n"
                   "  bijvoorbeeld https://repository.overheid.nl/frbr/officielepublicaties/kst/<dossier>/\n"
                   "  <nummer>/<versie>/xml/<nummer>.xml\n"
                   "Elk bestand is ongewijzigd opgeslagen. De xml is gecontroleerd op goed\n"
                   "gevormdheid en op het eigen kst-nummer; de pdf op %PDF- en %%EOF. De sha256\n"
                   "van beide staat in de index, dus het is na te rekenen.\n"
                   + ("" if not mislukt else "\nMislukt:\n" + "\n".join(mislukt)))

    print(f"{zn}: {len(rijen)} stukken, xml {met_xml}, pdf {met_pdf}, mislukt {len(mislukt)}",
          flush=True)
    if mislukt:
        raise RuntimeError(f"{len(mislukt)} downloads mislukt; zip niet geupload")

    rij = io.StringIO()
    w = csv.writer(rij)
    w.writerow(["shard", "id", "titel", "type", "maker", "datum", "vergaderjaar", "dossier",
                "ondernummer", "rubriek", "xml", "xml_bytes", "xml_sha256",
                "pdf", "pdf_bytes", "pdf_sha256", "url_xml", "url_pdf"])
    for r in rijen:
        w.writerow(r)
    idx = zn + ".index.csv.gz"
    with gzip.open(idx, "wt", encoding="utf8", newline="") as f:
        f.write(rij.getvalue())

    if droog:
        print(f"DROOG: {zn} {os.path.getsize(zn)/1024/1024:.1f} MB -> map {MAP}", flush=True)
        return
    d = Drive()
    mappen, _ = d.mappen()
    d.upload(zn, mappen[MAP])
    os.remove(zn)
    d.upload(idx, mappen["00"])
    os.remove(idx)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "plan":
        plan()
    elif a[0] == "prep":
        prep()
    elif a[0] == "run":
        mx = int(a[a.index("--max") + 1]) if "--max" in a else 0
        run(a[1], mx, "--droog" in a)
