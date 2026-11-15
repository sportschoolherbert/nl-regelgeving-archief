"""Controle: staat alles wat we zouden moeten hebben ook echt in Drive?

Kijkt per Drive-map welke bestanden er staan, of de shardnummering aaneengesloten
is (geen gat, geen dubbel) en of het aantal klopt met de shardlijst die lokaal
bekend is. Alleen lezen.

  python controle.py
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN
"""

import json
import os
import re
import sys

import requests

from gaten import Drive


def lokaal_aantal(plan, ids=None, per=0):
    if ids and os.path.exists(ids):
        with open(ids, encoding="utf8") as f:
            return (len(json.load(f)) + per - 1) // per
    if plan and os.path.isdir(plan):
        return len([f for f in os.listdir(plan) if f.endswith(".json")])
    return None


def lokaal_aantal_soort(plan, voorvoegsel):
    if plan and os.path.isdir(plan):
        return len([f for f in os.listdir(plan) if f.startswith(voorvoegsel) and f.endswith(".json")])
    return None


COLLECTIES = [
    ("01", "EHRM-uitspraken", r"^EHRM.*\.zip$", None),
    ("02", "historische BWB-versies", r"^BWB_historie_deel(\d{3})\.zip$",
     lambda: lokaal_aantal(None, "bwb_ids.json", 1500)),
    ("06", "CAO's met wetskracht (AVV)", r"^CAO_AVV_deel(\d{3})\.zip$",
     lambda: lokaal_aantal("avv_plan")),
    ("06", "CAO-register SZW", r"^CAO_register_deel(\d{3})(?:_[1-4])?\.zip$", None),
    ("08", "Kamerstukken", r"^Kamerstukken_deel(\d{3})\.zip$",
     lambda: lokaal_aantal("kst_plan")),
    ("01", "EU vervallen wetgeving (Cellar)", r"^EU_vervallen_wetgeving_deel(\d{3})\.zip$",
     lambda: lokaal_aantal_soort("eucellar_plan", "wet-")),
    ("01", "EU voorbereidende handelingen (Cellar)", r"^EU_voorbereidend_deel(\d{3})\.zip$",
     lambda: lokaal_aantal_soort("eucellar_plan", "com-")),
    ("01", "EU conclusies advocaten-generaal (Cellar)", r"^EU_conclusies_AG_deel(\d{3})\.zip$",
     lambda: lokaal_aantal_soort("eucellar_plan", "ag-")),
    ("04", "Tuchtrecht", r"^Tuchtrecht_deel(\d{3})\.zip$",
     lambda: lokaal_aantal("tucht_plan")),
    ("07", "open-access literatuur (OA)", r"^Literatuur_OA_.*\.zip$", None),
]


def main():
    d = Drive()
    mappen, _ = d.mappen()
    for sleutel, naam, patroon, aantal in COLLECTIES:
        r = requests.get("https://www.googleapis.com/drive/v3/files", headers=d.h,
                         params={"q": f"'{mappen[sleutel]}' in parents and trashed = false",
                                 "fields": "files(id,name,size)", "pageSize": 1000},
                         timeout=60)
        r.raise_for_status()
        bestanden = r.json()["files"]
        passend = sorted((x for x in bestanden if re.match(patroon, x["name"])),
                         key=lambda x: x["name"])
        nummers = [int(m.group(1)) for x in passend
                   if (m := re.match(patroon, x["name"])) and m.groups()]
        gb = sum(int(x.get("size", 0)) for x in passend) / 1024 ** 3
        wil = aantal() if aantal else None
        regel = f"{naam}: {len(passend)} bestanden, {gb:.1f} GB in map {sleutel}"
        if nummers:
            gaten = [n for n in range(nummers[0], nummers[-1] + 1) if n not in nummers]
            if sleutel == "06" and "register" in naam:
                # gesplitste blokken (deelNNN_1..4): de vier delen horen bij een nummer; dubbel = gewone zip + delen
                namen = [x["name"] for x in passend]
                dubbel = sorted({n for n in nummers if f"CAO_register_deel{n:03d}.zip" in namen
                                 and any(f"CAO_register_deel{n:03d}_{k}.zip" in namen for k in range(1, 5))})
                onvol = sorted(n for n in set(nummers) if f"CAO_register_deel{n:03d}.zip" not in namen
                               and sum(f"CAO_register_deel{n:03d}_{k}.zip" in namen for k in range(1, 5)) not in (0, 4))
                if onvol:
                    regel += f" | ONVOLLEDIG GESPLITST: {', '.join(str(x) for x in onvol)}"
                nummers = sorted(set(nummers))
            else:
                dubbel = sorted({n for n in nummers if nummers.count(n) > 1})
            if gaten:
                regel += f" | GAT: {', '.join(str(g) for g in gaten[:15])}"
            if dubbel:
                regel += f" | DUBBEL: {', '.join(str(x) for x in dubbel)}"
            if wil:
                regel += f" | verwacht {wil}" + (" — compleet" if len(passend) == wil and not gaten
                                                 else " — NIET compleet")
        elif wil:
            regel += f" | verwacht {wil} — nog niets"
        print(regel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
