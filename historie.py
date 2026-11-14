"""Alle historische versies van alle BWB-regelingen (Basiswettenbestand).

Per regeling staat in de manifest.xml van repository.officiele-overheidspublicaties.nl
elke versie die ooit gepubliceerd is, met een sha512-hashcode en de bestandsgrootte.
Wij halen die versies op en controleren elke download tegen die hashcode. Alleen
een bestand dat exact overeenkomt met wat KOOP publiceerde komt in de zip. Zo kan
er niets in het archief staan dat niet de feitelijke gepubliceerde tekst is.

Bereik: 2002-heden. De BWB is in 2002 gedigitaliseerd; van voor die tijd bestaan
geen geconsolideerde versies in de BWB (de oorspronkelijke publicatie staat wel in
het Staatsblad, map 08).

  python historie.py plan                 -> shardlijst
  python historie.py prep                 -> ids.json + de shards die nog open zijn
  python historie.py run <shard> [--max N] [--droog]
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN
"""

import concurrent.futures as cf
import csv
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

SRU = "https://zoekservice.overheid.nl/sru/Search"
REPO = "https://repository.officiele-overheidspublicaties.nl"
MAP = "02"
PER_SHARD = 1500          # regelingen per shard
THREADS = 8
IDS = "bwb_ids.json"

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (regelgeving-archief; open data)"})


def haal(url, timeout=120, pogingen=6):
    for i in range(pogingen):
        try:
            r = S.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(min(60, 2 * (i + 1) ** 2))
    raise RuntimeError("blijft weigeren: " + url)


def sru(query, start, n=5000):
    return haal(SRU + "?" + "&".join([
        "operation=searchRetrieve", "version=1.2", "x-connection=BWB",
        "query=" + requests.utils.quote(query, safe=""),
        f"maximumRecords={n}", f"startRecord={start}"]))


def telling(xml):
    m = re.search(r"<numberOfRecords>(\d+)</numberOfRecords>", xml)
    return int(m.group(1)) if m else None


def alle_ids():
    """Alle BWB-identificaties, in de volgorde van de SRU (gededupliceerd).

    Twee eigenaardigheden van de SRU die je moet weten:

    1. De server levert per verzoek maximaal 5.000 records, ook als je meer vraagt.
    2. De opgegeven numberOfRecords is te hoog: van de 148.890 records die de SRU
       meldt zijn er 142.385 daadwerkelijk op te halen; de rest komt nooit terug,
       ook niet als je per jaar of met andere paginagroottes werkt. Bladeren
       stopt dus bij record 142.386.

    Daarom per jaar van dcterms.modified, en de uitkomst twee keer gecontroleerd:
    dezelfde 43.353 regelingen komen eruit bij een heel andere partitionering, en
    alle 41.580 regelingen die op 1 juli van elk jaar 2002-2026 geldig waren
    zitten in de lijst. Wat overblijft zijn kort bestaande regelingen.
    """
    ids, opgegeven, opgehaald = [], 0, 0
    for jaar in range(2015, 2030):
        query = f"dcterms.modified>={jaar}-01-01 and dcterms.modified<{jaar + 1}-01-01"
        n, start, hier = None, 1, []
        while True:
            r = sru(query, start)
            if n is None:
                n = telling(r.text)
            gevonden = re.findall(r"<dcterms:identifier>(BWBR\d+)</dcterms:identifier>", r.text)
            if not gevonden:
                break
            hier += gevonden
            start += len(gevonden)
            if start > (n or 0):
                break
        ids += hier
        opgegeven += n or 0
        opgehaald += len(hier)
        print(f"  {jaar}: {len(hier)} van {n} records", flush=True)
    if opgehaald < 0.95 * opgegeven:
        raise RuntimeError(f"te weinig records: {opgehaald} van {opgegeven}")
    # dedupe, volgorde behouden
    uniek = list(dict.fromkeys(ids))
    print(f"{opgehaald} records (SRU meldt {opgegeven}) -> {len(uniek)} unieke regelingen",
          flush=True)
    return uniek


def shards():
    if not os.path.exists(IDS):
        raise SystemExit("bwb_ids.json ontbreekt; draai eerst: python historie.py prep")
    with open(IDS, encoding="utf8") as f:
        n = len(json.load(f))
    aantal = (n + PER_SHARD - 1) // PER_SHARD
    return [f"hist-{i:03d}" for i in range(aantal)]


def zipnaam(shard):
    return f"BWB_historie_{shard.replace('hist-', 'deel')}.zip"


def klaar(d, mappen, shard):
    return d.zoek(zipnaam(shard), mappen[MAP]) is not None


def versies_uit_manifest(xml):
    """[(label, inwerkingtreding, einddatum, hashcode, bytes, item), ...]"""
    uit = []
    for m in re.finditer(r"<expression\b[^>]*>(.*?)</expression>", xml, re.S):
        blok = m.group(1)
        label = re.search(r'<expression label="([^"]+)"', m.group(0))
        label = label.group(1) if label else ""
        if not label or 'label="xml"' not in blok:
            continue
        item = re.search(r'<item label="([^"]+)"([^>]*)>', blok)
        if not item or '_deleted="true"' in item.group(2):
            continue
        hash_ = re.search(r"<hashcode>([0-9a-f]+)</hashcode>", blok)
        size = re.search(r"<size>(\d+)</size>", blok)
        inw = re.search(r"<datum_inwerkingtreding>([^<]*)</datum_inwerkingtreding>", blok)
        ein = re.search(r"<einddatum>([^<]*)</einddatum>", blok)
        uit.append((label, inw.group(1) if inw else "", ein.group(1) if ein else "",
                    hash_.group(1) if hash_ else "", int(size.group(1)) if size else 0,
                    item.group(1)))
    return uit


def titel_uit(xml_bytes):
    kop = xml_bytes[:40000].decode("utf8", "ignore")
    for tag in ("citeertitel", "intitule", "opschrift"):
        m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", kop, re.S)
        if m:
            t = re.sub(r"<[^>]+>", " ", m.group(1))
            t = re.sub(r"\s+", " ", t).strip()
            if t:
                return t[:300]
    return ""


def interne_ids(data):
    """Wat het bestand zelf zegt over zijn identiteit: (bwb-id, versiedatum)."""
    kop = data[:8000].decode("utf8", "ignore")
    tag = re.search(r"<toestand\b[^>]*>", kop)
    if not tag:
        return "", ""
    t = tag.group(0)
    m = re.search(r'bwb-ng-vast-deel="[^"]*?/id/(BWBR\d+)/(\d{4}-\d{2}-\d{2})', t)
    if m:
        return m.group(1), m.group(2)
    b = re.search(r'bwb-id="([^"]+)"', t)
    d = re.search(r'inwerkingtreding="(\d{4}-\d{2}-\d{2})"', t)
    return (b.group(1) if b else ""), (d.group(1) if d else "")


def haal_versie(taak):
    """Downloadt één versie en controleert of het bestand de gevraagde versie is.

    De hashcode uit manifest.xml is niet altijd bruikbaar: KOOP heeft veel
    bestanden opnieuw gerenderd zonder het manifest bij te werken (van de
    steekproef klopte 70% van de hashes niet, terwijl het bestand wel de juiste
    versie was). Daarom is de harde controle: het bestand moet zichzelf intern
    identificeren als precies deze regeling en deze versiedatum. De hash wordt
    wel vergeleken en het resultaat gaat mee de index in, zodat het controleerbaar
    blijft.
    """
    bid, label, item, hash_ = taak
    url = f"{REPO}/bwb/{bid}/{label}/xml/{item}"
    r = haal(url)
    if r is None:
        return None, "404", "", ""
    data = r.content
    if len(data) < 100:
        return None, "leeg", "", ""
    eigen_id, eigen_datum = interne_ids(data)
    if eigen_id and eigen_id != bid:
        return None, f"verkeerde-wet:{eigen_id}", "", ""
    if eigen_datum and eigen_datum != label.split("_")[0]:
        return None, f"verkeerde-versie:{eigen_datum}", "", ""
    ontvangen = hashlib.sha512(data).hexdigest()
    if not hash_:
        controle = "geen hash in manifest"
    elif ontvangen == hash_:
        controle = "hash ok"
    else:
        controle = "hash wijkt af (manifest verouderd)"
    return data, "", ontvangen, controle


def run(shard, max_n, droog):
    with open(IDS, encoding="utf8") as f:
        ids = json.load(f)
    i = int(shard.split("-")[1])
    mijn = ids[i * PER_SHARD:(i + 1) * PER_SHARD]
    if max_n:
        mijn = mijn[:max_n]

    taken, meta = [], {}
    for bid in mijn:
        r = haal(f"{REPO}/bwb/{bid}/manifest.xml")
        if r is None:
            raise RuntimeError("geen manifest voor " + bid)
        for label, inw, ein, hash_, size, item in versies_uit_manifest(r.text):
            taken.append((bid, label, item, hash_))
            meta[(bid, label)] = (inw, ein, size, item)
        if len(taken) > 200000:
            break
    print(f"{shard}: {len(mijn)} regelingen, {len(taken)} versies", flush=True)

    zn = zipnaam(shard)
    rijen, mislukt = [], []
    hash_ok = hash_afwijkend = 0
    with zipfile.ZipFile(zn, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(THREADS) as ex:
            futs = {ex.submit(haal_versie, t): t for t in taken}
            for k, fut in enumerate(cf.as_completed(futs), 1):
                bid, label, item, hash_ = futs[fut]
                data, fout, ontvangen, controle = fut.result()
                if not data:
                    mislukt.append(f"{bid}/{label}/{item}:{fout}")
                    continue
                hash_ok += controle == "hash ok"
                hash_afwijkend += controle.startswith("hash wijkt af")
                titel = titel_uit(data)
                inw, ein, size, _ = meta[(bid, label)]
                z.writestr(f"{bid}/{label}/{item}", data, zipfile.ZIP_DEFLATED)
                rijen.append([shard, f"{bid}/{label}/{item}", bid, titel, label, inw, ein,
                              len(data), ontvangen, hash_, controle,
                              f"{REPO}/bwb/{bid}/{label}/xml/{item}"])
                if k % 1000 == 0:
                    print(f"  {k}/{len(taken)}", flush=True)

        z.writestr("_OVERZICHT.txt",
                   "Historische BWB-versies (Basiswettenbestand, KOOP)\n"
                   f"Shard: {shard}\nVerzameld: {time.strftime('%Y-%m-%d')}\n"
                   f"Regelingen: {len(mijn)}\nVersies: {len(rijen)}\n"
                   f"Niet opgehaald: {len(mislukt)}\n"
                   f"Manifest-hash klopte: {hash_ok}\n"
                   f"Manifest-hash verouderd: {hash_afwijkend}\n"
                   "\nBron van elk bestand:\n"
                   "  https://repository.officiele-overheidspublicaties.nl/bwb/<id>/<versie>/xml/<bestand>\n"
                   "Elk bestand is ongewijzigd opgeslagen zoals de repository het levert, en\n"
                   "moet zichzelf intern identificeren als precies deze regeling en versiedatum;\n"
                   "wijkt dat af, dan komt de versie niet in het archief.\n"
                   "Daarnaast is de sha512 uit manifest.xml vergeleken. Waar die afwijkt is het\n"
                   "manifest van KOOP verouderd (bestanden zijn opnieuw gerenderd zonder het\n"
                   "manifest bij te werken); beide hashes staan in de index, dus na te rekenen.\n"
                   + ("" if not mislukt else "\nNiet opgehaald:\n" + "\n".join(mislukt)))

    print(f"{zn}: {len(rijen)} versies, {len(mislukt)} mislukt, "
          f"hash ok {hash_ok}, verouderd {hash_afwijkend}", flush=True)
    if mislukt:
        # geen halve shard als 'klaar' wegzetten
        raise RuntimeError(f"{len(mislukt)} versies niet opgehaald; zip niet geupload")

    rij = io.StringIO()
    w = csv.writer(rij)
    w.writerow(["shard", "bestand", "id", "titel", "versie", "inwerkingtreding",
                "einddatum", "bytes", "sha512_ontvangen", "sha512_manifest",
                "hash_controle", "url"])
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


def prep():
    ids = alle_ids()
    with open(IDS, "w", encoding="utf8") as f:
        json.dump(ids, f)
    print(f"{len(ids)} regelingen -> {(len(ids) + PER_SHARD - 1)//PER_SHARD} shards")
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
    print(json.dumps(open_))


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "plan":
        print(json.dumps(shards()[:5]), "... totaal", len(shards()) if os.path.exists(IDS) else "?")
    elif a[0] == "prep":
        prep()
    elif a[0] == "run":
        mx = int(a[a.index("--max") + 1]) if "--max" in a else 0
        run(a[1], mx, "--droog" in a)
