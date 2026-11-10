# Wet- en regelgeving Nederland

Officiële open data, automatisch gedownload. Een hogere laag gaat voor een lagere.

| Map | Inhoud | Bron |
|---|---|---|
| 01 Internationaal en Europees recht | Alle geldende verdragen (o.a. EVRM) en rijkswetten (o.a. Statuut voor het Koninkrijk) | https://wetten.overheid.nl · https://eur-lex.europa.eu · https://www.echr.coe.int |
| 02 Nationale wetgeving | Alle geldende regelingen per soort: wetten, AMvB's, ministeriële regelingen, beleidsregels, KB's, zbo/pbo-regelingen (Grondwet in de wetten-zip) | https://wetten.overheid.nl |
| 03 Decentrale regels | Alle regelingen van gemeenten, provincies en waterschappen (CVDR), per blok; in elke zip één map per organisatie | https://lokaleregelgeving.overheid.nl |
| 04 Jurisprudentie | Alle gepubliceerde uitspraken met tekst, één zip per jaar (alles vóór 2000 in één zip) | https://uitspraken.rechtspraak.nl |
| 05 Ongeschreven recht | Uitleg: niet te downloaden | — |
| 06 Cao's | Uitleg + vindplaats | — |
| 07 Wetsgeschiedenis en literatuur | Uitleg + vindplaats | https://zoek.officielebekendmakingen.nl |

Bestanden zijn XML (het officiële formaat). Elke zip bevat `_OVERZICHT.txt` met de downloaddatum en wat eventueel niet lukte.
Let op: dit is een momentopname. Regels veranderen dagelijks; voor de actuele tekst altijd de bron raadplegen.
Niet inbegrepen: EU-wetgeving (EUR-Lex blokkeert automatische downloads), uitspraken van de Raad van State die niet op rechtspraak.nl staan, uitspraken van het EHRM (https://hudoc.echr.coe.int) en het Hof van Justitie EU (https://curia.europa.eu).

## Zoeken
In map 02, 03 en 04 staat per bron één zoekbestand: `Zoeken_Landelijke_regels_BWB.sqlite`, `Zoeken_Lokale_regels_CVDR.sqlite` en `Zoeken_Rechtspraak.sqlite`.
Het zijn SQLite-databases met een zoekindex (FTS5) over de volledige tekst. Open ze met het gratis programma DB Browser for SQLite (https://sqlitebrowser.org), tabblad "SQL uitvoeren". Voorbeeld:

    SELECT titel, organisatie, url, snippet(docs, 5, '[', ']', '...', 20)
    FROM docs WHERE docs MATCH 'sportschool AND aansprakelijkheid'
    ORDER BY rank LIMIT 20;

Velden: id, soort, organisatie, titel, datum, tekst, url, zip, bestand. Zoeken mag met AND, OR, NOT, "letterlijke zin" en voorvoegsel* (bijv. `huur*`).
Tip: zet het bestand eerst op "Offline beschikbaar" in Google Drive; de rechtspraak-database is groot.
