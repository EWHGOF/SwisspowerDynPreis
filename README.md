# Swisspower Dynamischer Strompreis API

Home-Assistant-Integration zum periodischen Abruf der dynamischen Tarife für das Abrufen von Dynamischen Strompreisen welche den Swisspower Standard folgen.
Energieversorger welche diesen Standard einhalten:



Wichtig: das ist meine Erste Integration ich bitte um unterstützung.


## Installation (HACS)

1. HACS öffnen → **Integrationen** → **⋮** → **Benutzerdefinierte Repositories**.
2. Repository-URL hinzufügen und als **Integration** markieren.
3. **Swisspower DynPreis** installieren.
4. Home Assistant neu starten.

## Konfiguration

Nach der Installation kann die Integration über **Einstellungen → Geräte & Dienste** hinzugefügt werden.
Ein Wizard führt durch die Konfiguration:

- **Messpunktnummer** (Metering Code) und **Authentifizierungstoken** für den produktiven API-Zugriff oder
- **Tarifname** (z. B. D1) ohne Token. 

Zusätzlich können die gewünschten Tariftypen ausgewählt werden (electricity, grid, dso, integrated, feed_in).

Electricity = Der Preis für die Energie
Grid = Netznutzung
Integrated = die Summe von allen Bestandteilen. Inklusive abgaben.
Feed_in = ist der Preis für die Rückvergütung von erzeugter energie.

## Sensoren

Für jeden ausgewählten Tariftyp werden Entities in zwei Domains erstellt.

Als `sensor.*`:

- **Current price** (CHF/kWh): aktueller Arbeitspreis, inklusive der kompletten Tarif-Slots im Attribut `prices`.
- **Next change** (Timestamp): Zeitpunkt, wann der aktuelle Preis endet.
- **Average price today** (CHF/kWh): Durchschnittspreis heute (inkl. min/max/average Statistik).
- **Average price tomorrow** (CHF/kWh): Durchschnittspreis morgen (inkl. Statistik).
- **Lowest 2h/4h window today/tomorrow** (CHF/kWh): günstigstes 2h/4h-Fenster.
- **Highest 2h/4h window today/tomorrow** (CHF/kWh): teuerstes 2h/4h-Fenster.

Als `binary_sensor.*`:

- **Cheapest 10/25/50% hours today**: Ein, wenn der aktuelle Preis zu den günstigsten 10/25/50% des Tages gehört.
- **Most expensive 10/25% hours today**: Ein, wenn er zu den teuersten 10/25% gehört.
- **In cheapest/most expensive 2h/4h window today**: Ein, wenn der aktuelle Zeitpunkt im günstigsten/teuersten Fenster liegt.

Liegen für den aktuellen Zeitpunkt keine Preise vor, sind die Binärsensoren
`unknown` und nicht `off` — «unbekannt» und «nicht im günstigen Fenster» sind
für Automationen zwei verschiedene Aussagen.

Der **Current price**-Sensor führt zusätzlich zwei Attribute zur Datenaktualität:

- `last_successful_fetch`: wann dieser Tariftyp zuletzt erfolgreich abgerufen wurde.
- `from_cache`: `true`, wenn gerade zwischengespeicherte Preise angezeigt werden,
  weil der letzte Abruf für diesen Tariftyp fehlschlug.

Das Attribut `prices` (die komplette Kurve) wird bewusst nicht in die
Langzeitdatenbank geschrieben — es ändert sich bei jedem Preiswechsel und würde
die Datenbank unnötig aufblähen. Für Diagramme ist es im aktuellen Zustand
weiterhin verfügbar.

Die 2h- und 4h-Fenster sind echte Zeitdauern. Bei einem Tarif mit
15-Minuten-Intervallen umfasst ein 2h-Fenster acht Intervalle, und der
Fenster-Durchschnitt ist nach Dauer gewichtet.

### Achtung beim Update

Die Binärsensoren lagen früher fälschlich in der `sensor`-Domain (mit dem
Zustand `on`/`off`). Beim ersten Start nach dem Update werden die alten
`sensor.*`-Einträge entfernt und als `binary_sensor.*` neu angelegt.
Automationen, Skripte und Dashboards, die eine dieser Entities über
`sensor.…` ansprechen, müssen auf `binary_sensor.…` umgestellt werden. Die
übrigen Sensoren behalten ihre Entity-ID.

## Optionen

In den Optionen werden zwei tägliche Abrufzeiten (lokale Zeit) definiert:

- **Abrufzeit** (Standard 06:00): der Morgen-Abruf.
- **Abrufzeit nachmittags** (Standard 14:00): der Nachmittags-Abruf.

Zwei Zeitpunkte sind nötig, weil die meisten Energieversorger die Preise für
den Folgetag erst am Nachmittag publizieren. Ein reiner Morgen-Abruf kann sie
also nie sehen, und alle «morgen»-Sensoren bleiben leer.

Zusätzlich gilt:

- Sind die Preise für morgen nach dem Nachmittags-Abruf noch nicht vollständig
  publiziert, wird in grösseren Abständen (30, 30, 60, 60, 120, 120 Minuten)
  erneut nachgefragt und ab 23:00 bis zum nächsten Morgen-Abruf nicht mehr.
- Fehlt der Preis für den aktuellen Zeitpunkt ganz (die API antwortet zwar,
  aber ohne Daten), wird unabhängig von der Tageszeit schneller nachgefragt
  (5, 10, 15, 30, 30, 60, 60, 120 Minuten).
- Schlägt ein Abruf fehl, wird mit 1, 2, 5, 10 und 30 Minuten Abstand erneut
  versucht, maximal sechsmal. Danach wird die nächste Abrufzeit abgewartet;
  jede Abrufzeit beginnt wieder mit einer frischen Wiederholungsreihe.
- Ein normaler Tag ergibt damit zwei API-Abfragen pro Tariftyp.
- **Testjahr** (optional) schreibt das Abfragejahr um und ist nur zum Testen
  gedacht. In diesem Modus wird nicht nach den Preisen für morgen nachgefragt,
  es gelten nur die beiden Abrufzeiten.

Die Sensorwerte werden unabhängig von den Abrufen laufend aktualisiert: bei
jedem Preiswechsel und um lokal 00:00 Uhr wird aus den bereits geladenen Daten
neu gerechnet, ohne zusätzliche API-Abfrage. Die Länge der Preisintervalle wird
dabei aus den Daten gelesen, 15-Minuten-Tarife funktionieren also ebenso wie
Stundentarife.

Änderungen an den Optionen werden sofort übernommen; ein Neustart von Home
Assistant ist nicht nötig.

Hinweis: Wird für die Integration in Home Assistant «Abfrage aktiviert»
ausgeschaltet, entfallen die Wiederholungen (Nachfragen und Fehler-Retry), die
beiden festen Abrufzeiten bleiben aber aktiv.

## Wenn etwas nicht stimmt

Schlägt ein Abruf für einen Tariftyp fehl, behält dieser Typ seine bereits
geladenen Preise, statt auf `unavailable` zu gehen: ein für 15:00 publizierter
Preis wird nicht falsch, weil der Server gerade nicht antwortet. Die anderen
Tariftypen sind davon nicht betroffen. Dass zwischengespeicherte Daten
angezeigt werden, steht in den Attributen `from_cache` und
`last_successful_fetch`. Antwortet kein einziger Tariftyp und liegen auch keine
gespeicherten Preise vor, wird die Integration als nicht verfügbar gemeldet.

Wird die Anfrage dauerhaft abgelehnt (HTTP 401/403/404 — z. B. falscher
Messpunkt, falsches Token oder falscher Tarifname), werden die Wiederholungen
ausgesetzt und der Grund im Log genannt; weiter zu probieren würde nichts
bringen.

Unter **Einstellungen → Geräte & Dienste → Swisspower DynPreis → ⋮ →
Diagnose herunterladen** gibt es den kompletten Zustand des Zeitplans (welcher
Abruf wann geplant ist, wann jeder Tariftyp zuletzt erfolgreich war, ob die
Preise für morgen als vollständig gelten). Token und Messpunktnummer sind darin
entfernt.

Für mehr Details im Log:

```yaml
logger:
  logs:
    custom_components.swisspower_dynpreis: debug
```

Der Authentifizierungstoken wird nie geloggt, und die Messpunktnummer wird in
Log-URLs maskiert.

## Weiterführende Links 

Die API-Dokumentation findet sich unter:
https://esit.code-fabrik.ch/doc_scalar

Für Tests (z. B. bei von Codex generiertem Code) kann folgende Basis-URL verwendet werden:
https://esit-test.code-fabrik.ch/api/v1

übersicht EVU mit Dynamischen Tarifen:
https://smartgridready.ch/loesungen/dynamischetarife

Weiterführende Intressante Website:
https://dynamische-stromtarife.ch/

