"""Wekelijks bijwerken van het archief: haalt per bron op wat sinds de vorige keer nieuw
of gewijzigd is (zie bijwerk_lib.py voor de stand per bron in Drive, map 00).

  python bijwerken.py plan                          -> bronnen voor deze run (JSON, GitHub-matrix)
  python bijwerken.py run <bron> [--droog] [--max N] [--sinds JJJJ-MM-DD]
  python bijwerken.py stand                         -> per bron: sinds, laatste run, open

Elke bron is een module met run(bron, droog, max_n, sinds) -> verslag (dict).
Frequentie (besluit Jan 16-11): alles wekelijks, open vakliteratuur maandelijks (eerste week
van de maand). Staten-Generaal Digitaal 1814-1995 is een gesloten archief en zit er niet in.
Omgeving: GD_CLIENT_ID, GD_CLIENT_SECRET, GD_REFRESH_TOKEN; ALLEEN=bron1,bron2 (optioneel).
"""
import datetime as dt
import importlib
import json
import os
import sys

# bron -> (module, frequentie)
BRONNEN = {
    # officiële bekendmakingen en parlement (repository.overheid.nl)
    "kamerstukken": ("bijwerk_officieel", "week"),
    "handelingen": ("bijwerk_officieel", "week"),
    "aanhangsel": ("bijwerk_officieel", "week"),
    "kamervragen_za": ("bijwerk_officieel", "week"),
    "overig_parlement": ("bijwerk_officieel", "week"),
    "gemeenteblad": ("bijwerk_officieel", "week"),
    "provinciaal_blad": ("bijwerk_officieel", "week"),
    "waterschapsblad": ("bijwerk_officieel", "week"),
    "bgr": ("bijwerk_officieel", "week"),
    "staatsblad": ("bijwerk_officieel", "week"),
    "staatscourant": ("bijwerk_officieel", "week"),
    "tractatenblad": ("bijwerk_officieel", "week"),
}

# aanvullende bronnen registreren zich via hun eigen module (BRONNEN-dict met frequentie)
for extra in ("bijwerk_wetten", "bijwerk_rechtspraak", "bijwerk_europa", "bijwerk_overig"):
    try:
        m = importlib.import_module(extra)
        for naam, freq in getattr(m, "REGISTER", {}).items():
            BRONNEN[naam] = (extra, freq)
    except ModuleNotFoundError as e:
        if e.name != extra:
            raise


def plan():
    vandaag = dt.date.today()
    keuze = [s.strip() for s in os.environ.get("ALLEEN", "").split(",") if s.strip()]
    uit = []
    for naam, (_, freq) in BRONNEN.items():
        if keuze and naam not in keuze:
            continue
        if freq == "maand" and vandaag.day > 7 and not keuze:
            continue
        uit.append(naam)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("bronnen=" + json.dumps(uit) + "\n")
    print(json.dumps(uit))
    return uit


def stand():
    import bijwerk_lib as L
    from gaten import Drive
    d = Drive()
    for naam in BRONNEN:
        s = L.Stand(naam, d).data
        h = (s.get("historie") or [{}])[-1]
        print(f"{naam:20s} sinds {s['sinds']}  laatste run {s.get('laatste_run', '-')}  "
              f"open {len(s.get('open', []))}  laatst: {h.get('stukken', '-')} stukken, "
              f"{h.get('mislukt', '-')} mislukt")


def run(naam, droog, max_n, sinds):
    module, _ = BRONNEN[naam]
    verslag = importlib.import_module(module).run(naam, droog=droog, max_n=max_n, sinds=sinds)
    print("VERSLAG " + json.dumps(verslag, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "plan":
        plan()
    elif a[0] == "stand":
        stand()
    elif a[0] == "run":
        run(a[1], "--droog" in a, int(a[a.index("--max") + 1]) if "--max" in a else 0,
            a[a.index("--sinds") + 1] if "--sinds" in a else None)
