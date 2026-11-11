"""Controleert of een GitHub-runner door Cloudflare (HUDOC) en Anubis (CAO-register) komt.

Beide sites weren geen inhoud, maar niet-browsers: Cloudflare zet een
JS-challenge voor, Anubis een proof-of-work. Een echte browser lost die zelf op.
Deze test zegt of dat vanaf een runner ook lukt, voordat er 29.000 uitspraken
in gang worden gezet.
"""

import sys

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(locale="nl-NL")
        pg = ctx.new_page()

        for naam, url, verwacht in (
                ("HUDOC", "https://hudoc.echr.coe.int/", "HUDOC"),
                ("CAO-register", "https://www.uitvoeringarbeidsvoorwaardenwetgeving.nl/", "Cao")):
            try:
                r = pg.goto(url, timeout=120000, wait_until="domcontentloaded")
                pg.wait_for_timeout(15000)          # challenge oplossen
                titel = pg.title()
                door = verwacht.lower() in titel.lower()
                print(f"{naam}: {'DOOR' if door else 'GEBLOKKEERD'}  "
                      f"status={r.status if r else '?'}  titel={titel[:70]!r}", flush=True)
            except Exception as fout:
                print(f"{naam}: FOUT {type(fout).__name__}: "
                      f"{str(fout).splitlines()[0][:110]}", flush=True)

        try:
            j = ctx.request.get("https://hudoc.echr.coe.int/app/query/results",
                                params={"query": 'contentsitename:ECHR AND doctype="HEJUD"',
                                        "select": "itemid,docname", "length": 1},
                                timeout=120000)
            print("HUDOC API:", j.status, "resultcount", j.json().get("resultcount"), flush=True)
        except Exception as fout:
            print("HUDOC API: FOUT", type(fout).__name__, flush=True)

        b.close()


if __name__ == "__main__":
    sys.exit(main())
