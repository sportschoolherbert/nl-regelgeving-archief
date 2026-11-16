"""Gedeelde onderdelen voor het wekelijks bijwerken van het archief.

Per bron staat in Drive (map 00) een standbestand `_bijwerk_stand_<bron>.json`:

  {"bron": "...", "sinds": "2026-11-09", "laatste_run": "2026-11-16T03:10:00",
   "open": [ {...record dat vorige keer mislukte...} ], "historie": [ {...per run...} ]}

- `sinds`: alles wat vanaf deze datum nieuw of gewijzigd is, moet nog gehaald worden.
  Schuift pas op als de zip en de index veilig in Drive staan. Mist een week, dan haalt
  de volgende run vanzelf alles sinds de laatste gelukte keer op (geen gat).
- `open`: stukken die vorige keer niet lukten; die worden de volgende run eerst geprobeerd
  (automatische herhaalronde). Na MAX_POGINGEN runs komen ze alleen nog in de mislukt-lijst.
- De nieuwe stukken komen in een eigen zip met datum (`Bijwerking_<Bron>_<van>_tot_<tot>.zip`)
  in dezelfde map als de oogst; bestaande zips worden nooit aangeraakt.
"""
import csv
import datetime as dt
import gzip
import json
import os
import time

from gaten import Drive

OVERLAP_DAGEN = 1       # dt.modified is een datum: de dag van de vorige run nog een keer meenemen
MAX_POGINGEN = 4        # zo vaak (runs) een mislukt stuk opnieuw proberen
STARTDATUM = "2026-11-09"   # alle oogsten liepen 10-11 t/m 16-11; een dag ervoor beginnen = geen gat


def standnaam(bron):
    return f"_bijwerk_stand_{bron}.json"


class Stand:
    def __init__(self, bron, drive=None):
        self.bron = bron
        self.d = drive or Drive()
        self.mappen, _ = self.d.mappen()
        self.data = {"bron": bron, "sinds": STARTDATUM, "open": [], "historie": []}
        fid = self.d.zoek(standnaam(bron), self.mappen["00"])
        if fid:
            pad = standnaam(bron)
            self.d.download(fid, pad)
            with open(pad, encoding="utf8") as f:
                self.data.update(json.load(f))
            os.remove(pad)

    def venster(self, sinds=None):
        """(van, tot) als datums; tot = morgen (exclusief)."""
        if sinds:   # handmatig opgegeven: precies vanaf die datum
            van = dt.date.fromisoformat(sinds)
        else:
            van = dt.date.fromisoformat(self.data["sinds"]) - dt.timedelta(days=OVERLAP_DAGEN)
        return van, dt.date.today() + dt.timedelta(days=1)

    def bewaar(self, nieuw_sinds, open_, verslag):
        self.data["sinds"] = nieuw_sinds
        self.data["open"] = open_
        self.data["laatste_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.data["historie"] = (self.data.get("historie") or [])[-100:] + [verslag]
        pad = standnaam(self.bron)
        with open(pad, "w", encoding="utf8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        self.d.upload(pad, self.mappen["00"])
        os.remove(pad)


def zipnaam(bron_label, van, tot):
    return f"Bijwerking_{bron_label}_{van.isoformat()}_tot_{(tot - dt.timedelta(days=1)).isoformat()}.zip"


def schrijf_index(zn, kop, rijen):
    idx = zn + ".index.csv.gz"
    with gzip.open(idx, "wt", encoding="utf8", newline="") as f:
        w = csv.writer(f)
        w.writerow(kop)
        w.writerows(rijen)
    return idx


def schrijf_mislukt(zn, mislukt):
    if not mislukt:
        return None
    pad = zn + ".mislukt.csv"
    with open(pad, "w", encoding="utf8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["shard", "fout"])
        for regel in mislukt:
            w.writerow([zn.replace(".zip", ""), regel])
    print(f"::warning::{zn}: {len(mislukt)} stukken niet compleet; zie {os.path.basename(pad)} in map 00",
          flush=True)
    return pad


def upload_resultaat(stand, zn, mapsleutel, idx, mislukt_pad, droog):
    """Zip naar de bronmap, index + mislukt naar map 00. Bij droog alleen melden."""
    if droog:
        mb = os.path.getsize(zn) / 1024 / 1024 if zn and os.path.exists(zn) else 0
        print(f"DROOG: {zn} {mb:.1f} MB -> map {mapsleutel} (niet geüpload, stand niet bijgewerkt)",
              flush=True)
        return
    d = stand.d
    if zn and os.path.exists(zn):
        d.upload(zn, stand.mappen[mapsleutel])
        os.remove(zn)
    if idx and os.path.exists(idx):
        d.upload(idx, stand.mappen["00"])
        os.remove(idx)
    if mislukt_pad and os.path.exists(mislukt_pad):
        d.upload(mislukt_pad, stand.mappen["00"])
        os.remove(mislukt_pad)
