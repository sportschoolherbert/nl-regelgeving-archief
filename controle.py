"""Controle: staat alles wat we zouden moeten hebben ook echt in Drive?

Per verzameling is bekend hoeveel shards er zijn (uit de shardlijst van het
bijbehorende script). Dit script kijkt in de Drive-map en noemt precies wat
ontbreekt en wat er onverwacht bij staat. Alleen lezen.

  python controle.py
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN
"""

import json
import os
import sys

from gaten import Drive


def verwacht_hist():
    if not os.path.exists("bwb_ids.json"):
        return None
    with open("bwb_ids.json", encoding="utf8") as f:
        n = len(json.load(f))
    return [f"BWB_historie_deel{i:03d}.zip" for i in range((n + 1499) // 1500)]


def verwacht_kst():
    if not os.path.isdir("kst_plan"):
        return None
    n = len([f for f in os.listdir("kst_plan") if f.endswith(".json")])
    return [f"Kamerstukken_deel{i:03d}.zip" for i in range(n)]


def verwacht_avv():
    if not os.path.isdir("avv_plan"):
        return None
    n = len([f for f in os.listdir("avv_plan") if f.endswith(".json")])
    return [f"CAO_AVV_deel{i:03d}.zip" for i in range(n)]


COLLECTIES = [
    ("02", "historische BWB-versies", verwacht_hist),
    ("06", "CAO's met wetskracht (AVV)", verwacht_avv),
    ("08", "Kamerstukken", verwacht_kst),
]


def main():
    d = Drive()
    mappen, _ = d.mappen()
    for sleutel, naam, functie in COLLECTIES:
        wil = functie()
        if wil is None:
            print(f"{naam}: shardlijst ontbreekt lokaal, overgeslagen")
            continue
        heb = set()
        for bestand in wil:
            if d.zoek(bestand, mappen[sleutel]) is not None:
                heb.add(bestand)
        mis = [b for b in wil if b not in heb]
        print(f"{naam}: {len(heb)} van {len(wil)} in map {sleutel}"
              + (f", ontbreekt: {', '.join(mis[:12])}" + (" ..." if len(mis) > 12 else "")
                 if mis else ", compleet"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
