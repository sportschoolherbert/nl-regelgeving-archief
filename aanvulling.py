"""Aanvulling op het archief: cao's, ongeschreven recht en open-access literatuur.

Drie soorten werk, elk als eigen shard:

  cao            de cao's die voor een sportschool kunnen gelden (CAO Sport e.d.)
  lit-<thema>    Nederlandstalige, vrij toegankelijke vakliteratuur (OpenAlex)
  ongeschreven   de kernarresten achter het ongeschreven recht + leeswijzer

Losstaand van archief.py en gaten.py. Schrijft naar dezelfde Drive-mappen en
maakt per zip een index in "00 Index en zoeken".

  python aanvulling.py plan
  python aanvulling.py prep
  python aanvulling.py run <shard> [--max N] [--droog]
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN
"""
import csv
import gzip
import io
import json
import os
import re
import sys
import time

import requests

from gaten import MAPPEN, Drive, haal, veilig

UA = {"User-Agent": "regelgeving-archief/1.0 (info@sportschool-herbert.nl)"}
OPENALEX = "https://api.openalex.org/works"
CONTACT = "info@sportschool-herbert.nl"
RECHTSPRAAK = "https://data.rechtspraak.nl/uitspraken"
S = requests.Session()
S.headers.update(UA)

# ---------- bronnen ----------

# CAO Sport: de enige cao die de sportsector kent. Partijen WOS + FNV/CNV/De Unie.
CAO_SPORT = [
    ("CAO_Sport_2026-2027.pdf",
     "https://www.sportwerkgever.nl/wp-content/uploads/2026/06/WOS-CAO-Sport-2026-2027-WEB.pdf",
     "CAO Sport 2026-2027 (WOS)"),
    ("CAO_Sport_2024-2025.pdf",
     "https://www.sportwerkgever.nl/wp-content/uploads/2025/03/WOS-CAO-Sport-2024-2025-versie-3-0125.pdf",
     "CAO Sport 2024-2025, versie 3 (WOS)"),
    ("CAO_Sport_2022-2023.pdf",
     "https://www.sportwerkgever.nl/wp-content/uploads/2023/01/WOS-CAO-Sport-2022-2023-versie-januari-2023-WEB-1.pdf",
     "CAO Sport 2022-2023 (WOS)"),
    ("CAO_Sport_2021.pdf",
     "https://www.sportwerkgever.nl/wp-content/uploads/2021/02/CAO-Sport-2021.pdf",
     "CAO Sport 2021 (WOS)"),
    ("Salarisschalen_CAO_Sport_2026-2027.pdf",
     "https://www.sportwerkgever.nl/wp-content/uploads/2025/12/Salarisschalen-CAO-Sport-en-KNVB-2026-2027-WEB-LANDSCAPE-1.pdf",
     "Salarisschalen CAO Sport en KNVB 2026-2027"),
    ("Salarisschalen_CAO_Sport_2024-2025.pdf",
     "https://www.sportwerkgever.nl/wp-content/uploads/2025/03/Salarisschalen-CAO-Sport-en-KNVB-2024-2025-WEB-LANDSCAPE-0125.pdf",
     "Salarisschalen CAO Sport en KNVB 2024-2025"),
    ("Salarisschalen_CAO_Sport_2022-2023.pdf",
     "https://www.sportwerkgever.nl/wp-content/uploads/2023/01/Salarisschalen-CAO-Sport-2022-2023-versie-januari-2023-WEB-LANDSCAPE-1.pdf",
     "Salarisschalen CAO Sport 2022-2023"),
    ("Salarisschalen_CAO_Sport_2021.pdf",
     "https://www.sportwerkgever.nl/wp-content/uploads/2021/02/Salarisschalen-CAO-Sport-2021.pdf",
     "Salarisschalen CAO Sport 2021"),
    ("Handleiding_Arbo_voor_de_sport_2024.pdf",
     "https://www.sportwerkgever.nl/wp-content/uploads/2024/02/Handleiding-Arbo-voor-de-sport-2024.pdf",
     "Handleiding Arbo voor de sport 2024 (branche-afspraken)"),
]

# Onderwerpen voor de literatuur. Bewust smal: wat een sportschool raakt.
THEMA = {
    "arbeidsrecht": "arbeidsrecht",
    "sportrecht": "sport aansprakelijkheid",
    "aansprakelijkheid": "aansprakelijkheid schadevergoeding",
    "ondernemingsrecht": "ondernemingsrecht",
    "privacy": "persoonsgegevens privacy",
    "consumentenrecht": "consumentenrecht",
    "contractenrecht": "contractenrecht overeenkomst",
    "huurrecht": "huurrecht bedrijfsruimte",
    "faillissementsrecht": "faillissementsrecht",
    "belastingrecht": "belastingrecht",
    "verzekeringsrecht": "verzekeringsrecht",
}

# Arresten die het ongeschreven recht dragen. Alle ECLI's zijn nagelopen: ze
# geven een bestaande uitspraak van de Hoge Raad terug.
ARRESTEN = [
    ("ECLI:NL:HR:1919:AG1776", "Lindenbaum/Cohen - het begin van de onrechtmatige daad"),
    ("ECLI:NL:HR:1957:AG2023", "Baris/Riezenkamp - gerechtvaardigd vertrouwen bij onderhandelen"),
    ("ECLI:NL:HR:1965:AB7079", "Kelderluik - de zorgvuldigheidsnorm"),
    ("ECLI:NL:HR:1966:AC4642", "Zutphense juffrouw - gevaarzetting en eigen schuld"),
    ("ECLI:NL:HR:1968:AB6963", "Saladin/HBU - maatstaf bij onrechtmatige daad"),
    ("ECLI:NL:HR:1981:AG4158", "Haviltex - uitleg van overeenkomsten"),
    ("ECLI:NL:HR:2005:AR5213", "Baby Kelly - aansprakelijkheid en eigen keuze"),
]


def leeswijzer():
    """Tekst die uitlegt wat ongeschreven recht is en waar het in het archief zit."""
    return """# Ongeschreven recht - leeswijzer

Ongeschreven recht is geen aparte wetbundel. Het zit in drie lagen, en die staan
allemaal al in dit archief:

## 1. Rechterlijke rechtsvorming (het zwaartepunt)
De Hoge Raad vult open normen in: zorgvuldigheid (art. 6:162 BW), redelijkheid en
billijkheid (art. 6:248 en 6:2 BW), goed werkgeverschap (art. 7:611 BW). Die
invulling staat in de arresten zelf, niet in de wet. In deze map staan de
kernarresten; de volledige stroom zit in map 04 (rechtspraak).

## 2. Gewoonterecht en bestendige praktijk
Wat partijen jarenlang zo doen dat het als recht geldt. Te vinden in de
rechtspraak die erover gaat, en in de parlementaire geschiedenis (map 07).

## 3. Branche- en gedragsnormen (soft law)
Geen wet, maar wel het kader waaraan een rechter en de Arbeidsinspectie je houden:
arbocatalogi, branchecodes, NOC*NSF-reglementen, de Arbo-handleiding voor de sport.
Deze map bevat de voor de sportsector relevante stukken.

## Wat hier bewust niet in zit
- Literatuur over ongeschreven recht: zie map 07 (open access) - de belangrijkste
  handboeken zijn paywall.
- Tuchtrecht: zie tuchtrecht.overheid.nl, niet in dit archief.

## Leesvolgorde voor een sportschool
1. Zorgplicht en veiligheid (Kelderluik-lijn) - wat mag je van een deelnemer
   verwachten en wat moet je zelf regelen.
2. Goed werkgeverschap - omgang met instructeurs en stagaires.
3. Redelijkheid en billijkheid - uitleg van je eigen voorwaarden en contracten.
4. Branchenormen - de concrete invulling voor de sportsector.
"""


# ---------- hulpfuncties ----------

def is_pdf(data):
    return bool(data) and data[:4] == b"%PDF"


def haal_pdf(url):
    r = haal(url, timeout=180, pogingen=3)
    if r is None or not is_pdf(r.content):
        return None
    return r.content


def maak_zip(zipnaam, taken, fn, threads=6):
    """taken: lijst (bestandsnaam, argument, metadata-dict)."""
    import concurrent.futures as cf
    import zipfile

    mislukt, gelukt = [], []
    with zipfile.ZipFile(zipnaam, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with cf.ThreadPoolExecutor(threads) as ex:
            futs = {ex.submit(fn, arg): (naam, meta) for naam, arg, meta in taken}
            for i, fut in enumerate(cf.as_completed(futs), 1):
                naam, meta = futs[fut]
                try:
                    data = fut.result()
                except Exception:
                    data = None
                if data:
                    z.writestr(naam, data)
                    gelukt.append((naam, meta))
                else:
                    mislukt.append(naam)
                if i % 200 == 0:
                    print(f"  {i}/{len(taken)}", flush=True)
        z.writestr("_OVERZICHT.txt",
                   f"Gedownload op {time.strftime('%Y-%m-%d')}\nGelukt: {len(gelukt)}\n"
                   f"Mislukt: {len(mislukt)}\n" + "".join(u + "\n" for u in mislukt))
    print(f"{zipnaam}: {len(gelukt)} gelukt, {len(mislukt)} mislukt", flush=True)
    return gelukt, mislukt


def schrijf_index(pad, shard, rijen):
    """rijen: lijst (bestandsnaam, meta) met titel, datum, url, auteurs, bron."""
    uit = io.StringIO()
    w = csv.writer(uit)
    w.writerow(["shard", "bestand", "id", "titel", "datum", "auteurs", "bron", "url"])
    for naam, meta in rijen:
        w.writerow([shard, naam, meta.get("id", naam.rsplit(".", 1)[0]),
                    meta.get("titel", ""), meta.get("datum", ""),
                    meta.get("auteurs", ""), meta.get("tijdschrift", ""),
                    meta.get("url", "")])
    with gzip.open(pad, "wt", encoding="utf8", newline="") as f:
        f.write(uit.getvalue())
    return pad, len(rijen)


# ---------- shards ----------

def bouw_cao(max_n):
    taken = [(naam, url, {"titel": titel, "url": url, "datum": ""})
             for naam, url, titel in CAO_SPORT]
    if max_n:
        taken = taken[:max_n]
    print(f"cao: {len(taken)} documenten", flush=True)
    gelukt, _ = maak_zip("CAO_sport.zip", taken, haal_pdf)
    return "CAO_sport.zip", "06", gelukt


def openalex(params, pogingen=8):
    """OpenAlex met beleefd mailadres en eerlijke backoff.

    Zeven literatuur-shards klopten tegelijk aan en kregen een weigering terug;
    na drie snelle pogingen gaf het script op. Nu wordt Retry-After gerespecteerd,
    langer doorgeprobeerd en de statuscode gemeld, zodat een volgende storing
    meteen in het log staat.
    """
    for i in range(pogingen):
        try:
            r = S.get(OPENALEX, params=params, timeout=120)
        except requests.RequestException as fout:
            print(f"  netwerkfout {type(fout).__name__}, opnieuw", flush=True)
            time.sleep(20 * (i + 1))
            continue
        if r.status_code == 200:
            return r.json()
        wacht = int(r.headers.get("Retry-After") or 0) or min(180, 20 * (i + 1))
        print(f"  OpenAlex {r.status_code}: {r.text[:140].strip()} - {wacht}s wachten",
              flush=True)
        time.sleep(wacht)
    raise RuntimeError("OpenAlex blijft weigeren: " + str(params.get("search")))


def openalex_lijst(thema, max_n):
    filter_ = "language:nl,open_access.is_oa:true,type:article,has_fulltext:true"
    cursor, uit = "*", []
    while cursor and len(uit) < max_n:
        j = openalex({"filter": filter_, "search": THEMA[thema], "per-page": 200,
                      "cursor": cursor, "mailto": CONTACT})

        for w in j.get("results", []):
            urls = []
            if w.get("best_oa_location") and w["best_oa_location"].get("pdf_url"):
                urls.append(w["best_oa_location"]["pdf_url"])
            for loc in (w.get("locations") or []):
                if loc.get("pdf_url"):
                    urls.append(loc["pdf_url"])
            if urls:
                uit.append((w, urls[:3]))
        cursor = (j.get("meta") or {}).get("next_cursor")
        if not j.get("results"):
            break
    return uit[:max_n]


def _werk_meta(w):
    auteurs = [a["author"]["display_name"] for a in (w.get("authorships") or [])[:4]
               if a.get("author", {}).get("display_name")]
    bron = ""
    if w.get("primary_location") and w["primary_location"].get("source"):
        bron = w["primary_location"]["source"].get("display_name", "")
    return {
        "id": (w.get("doi") or w.get("id") or "").rsplit("/", 1)[-1],
        "titel": w.get("title") or "",
        "datum": str(w.get("publication_year") or ""),
        "url": w.get("doi") or w.get("id") or "",
        "auteurs": "; ".join(auteurs),
        "tijdschrift": bron,
    }


def bouw_literatuur(thema, max_n):
    werken = openalex_lijst(thema, max_n or 400)
    print(f"lit-{thema}: {len(werken)} kandidaten", flush=True)
    taken = []
    gezien = set()
    for w, urls in werken:
        meta = _werk_meta(w)
        naam = f"{veilig(meta['titel'] or meta['id'], 70)}_{meta['datum']}.pdf"
        if naam in gezien:                      # zelfde titel en jaar: uniek maken
            naam = f"{veilig(meta['titel'] or meta['id'], 66)}_{meta['datum']}_{meta['id'][-6:]}.pdf"
        gezien.add(naam)
        taken.append((naam, urls, meta))
    gelukt, _ = maak_zip(f"Literatuur_{thema}.zip", taken,
                         lambda urls: next((d for d in (haal_pdf(u) for u in urls) if d), None),
                         threads=8)
    return f"Literatuur_{thema}.zip", "07", gelukt


def bouw_ongeschreven(max_n):
    import zipfile

    lijst = ARRESTEN[:max_n] if max_n else ARRESTEN
    print(f"ongeschreven: {len(lijst)} arresten", flush=True)

    taken = []
    for ecli, label in lijst:
        taken.append((ecli.replace(":", "_") + ".xml",
                      RECHTSPRAAK + "/content?id=" + ecli,
                      {"titel": label, "id": ecli,
                       "url": "https://uitspraken.rechtspraak.nl/" + ecli}))

    gelukt, mislukt = maak_zip("Ongeschreven_recht_kernarresten.zip", taken,
                               lambda u: (lambda r: r.content if r is not None else None)(haal(u, timeout=90)))
    if mislukt:
        print("  niet gevonden:", ", ".join(mislukt), flush=True)

    # leeswijzer toevoegen en titels uit de XML halen voor de index
    with zipfile.ZipFile("Ongeschreven_recht_kernarresten.zip", "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("LEESWIJZER.md", leeswijzer())

    rijen = []
    with zipfile.ZipFile("Ongeschreven_recht_kernarresten.zip") as z:
        for naam, meta in gelukt:
            kop = z.read(naam)[:20000].decode("utf8", "replace")
            d = re.search(r"<dcterms:date[^>]*>(\d{4}-\d{2}-\d{2})", kop)
            rijen.append((naam, {"id": meta["id"], "titel": meta["titel"],
                                 "datum": d.group(1) if d else "", "url": meta["url"]}))
    return "Ongeschreven_recht_kernarresten.zip", "05", rijen


# ---------- planning ----------

def plan():
    return ["cao", "ongeschreven"] + [f"lit-{t}" for t in THEMA]


def zipnaam(shard):
    if shard == "cao":
        return "CAO_sport.zip"
    if shard == "ongeschreven":
        return "Ongeschreven_recht_kernarresten.zip"
    if shard.startswith("lit-"):
        return f"Literatuur_{shard[4:]}.zip"
    raise SystemExit("onbekende shard " + shard)


def prep():
    keuze = os.environ.get("SHARDS", "").strip()
    if keuze:
        # handmatige lijst uit de workflow: precies die shards, ook als de zip
        # al in Drive staat (bedoeld om één shard opnieuw te draaien)
        open_ = [s.strip() for s in keuze.split(",") if s.strip()]
        print("handmatige lijst:", json.dumps(open_))
    else:
        d = Drive()
        d.mappen()
        open_ = [s for s in plan() if not d.zoek(zipnaam(s))]
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(open_) + "\n")
    print(f"nog te doen: {len(open_)} van {len(plan())} shards")
    print(json.dumps(open_))


def run(shard, max_n, droog):
    if shard == "cao":
        zn, m, rijen = bouw_cao(max_n)
    elif shard.startswith("lit-"):
        zn, m, rijen = bouw_literatuur(shard[4:], max_n)
    elif shard == "ongeschreven":
        zn, m, rijen = bouw_ongeschreven(max_n)
    else:
        raise SystemExit("onbekende shard " + shard)

    idx, n = schrijf_index(zn + ".index.csv.gz", shard, rijen)
    print(f"index: {n} regels", flush=True)
    if droog:
        print(f"DROOG: {zn} {os.path.getsize(zn)/1024:.0f} KB -> {MAPPEN[m]}")
        print(f"DROOG: {idx} {os.path.getsize(idx)/1024:.0f} KB -> {MAPPEN['00']}")
        return
    d = Drive()
    mappen, _ = d.mappen()
    d.upload(zn, mappen[m])
    os.remove(zn)
    d.upload(idx, mappen["00"])
    os.remove(idx)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "plan":
        print(json.dumps(plan()))
    elif a[0] == "prep":
        prep()
    elif a[0] == "run":
        mx = int(a[a.index("--max") + 1]) if "--max" in a else 0
        run(a[1], mx, "--droog" in a)
    else:
        print(__doc__)
