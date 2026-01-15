# Swisspower DynPreis

Home-Assistant-Integration zum periodischen Abruf der dynamischen Tarife über die Swisspower ESIT API.

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

## Sensoren

Für jeden ausgewählten Tariftyp wird ein Sensor erstellt. Der Sensorwert zeigt den aktuellen Arbeitspreis
in CHF/kWh. Die vollständigen Tarif-Slots werden im Attribut `prices` bereitgestellt.

## Optionen

In den Optionen kann das Update-Intervall (in Minuten) für den periodischen Abruf angepasst werden.

## API-Dokumentation

Die API-Dokumentation findet sich unter:
https://esit.code-fabrik.ch/doc_scalar
