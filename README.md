# LeapMotor Mate

**Trip tracking, charge logging and remote control for Leapmotor vehicles** — a self‑hosted companion (think *TeslaMate* for Leapmotor). Runs as a **Home Assistant add‑on** or as a **standalone Docker** container.

Supported models: **B10 · C10 · T03** (European spec).

> 🇮🇹 [Versione italiana più sotto.](#leapmotor-mate--italiano)

## Screenshots

| Overview | Charges |
|---|---|
| ![Overview](docs/screenshots/overview.png) | ![Charges](docs/screenshots/charges.png) |
| **Statistics** | **Settings** |
| ![Statistics](docs/screenshots/statistics.png) | ![Settings](docs/screenshots/settings.png) |

---

## Features

- **Overview** — live status, battery, range, location map, vehicle picture.
- **Trips** — automatic trip detection with route map, distance, energy, efficiency and regen.
- **Charges** — charge sessions with AC/DC detection, energy added, power and a distribution chart.
- **Wallbox (optional)** — pair a wallbox already in Home Assistant to see live charging power/status, set the max charging current, and compare **AC delivered by the wallbox** vs **DC into the battery** per session, with charging efficiency.
- **Statistics** — driving/AC/other energy split and a 6‑week consumption trend (from the Leapmotor cloud).
- **Remote control** — lock, windows, trunk, panoramic roof, climate, find car, battery preheat.
- **Independent** — polls the Leapmotor cloud directly (configurable 10–30 s). No dependency on the phone app or Home Assistant; polling the cloud does **not** wake or drain the car.
- **Bilingual UI** — English / Italiano.

## How it works

```
Leapmotor Cloud  ──►  Poller (state machine)  ──►  SQLite  ──►  Web UI (FastAPI + HTMX)
                       trips / charges / regen                   + remote commands
```

The data lives in a local SQLite database. Nothing is sent anywhere except to the official Leapmotor cloud.

---

## Requirements

1. **A Leapmotor account.** ⚠️ **Use a *dedicated* account, not the one on your phone.** The Leapmotor cloud binds a session per device, so a second client can evict your phone (or vice‑versa). Create a separate account and share the car with it from the official app.
2. **The Leapmotor app TLS certificate** (`app.crt` + `app.key`). This is the *same for everyone* (it identifies the Leapmotor app, not you) and is **not** included in this repository. Download the two files from:

   👉 **https://github.com/markoceri/leapmotor-certs**

   You upload them once during the setup wizard (see below).

---

## Installation

### Option A — Home Assistant add‑on

1. In Home Assistant: **Settings → Add‑ons → Add‑on Store → ⋮ → Repositories**, and add the **add‑on** repository URL (note the `-addon` suffix — this is a separate repo from the code):

   ```
   https://github.com/ProtossBlaster/leapmotor-mate-addon
   ```

2. Install **LeapMotor Mate**, start it, and open the panel (car icon in the sidebar).
3. Follow the setup wizard.

The database is stored in the add‑on's persistent `/data`, so it survives restarts and updates.

### Option B — Standalone Docker

```bash
git clone https://github.com/ProtossBlaster/leapmotor-mate.git
cd leapmotor-mate
docker compose up -d
```

Then open **http://localhost:4000** and follow the setup wizard.

The database is stored in `./data/` (mounted at `/data` in the container).

---

## Setup wizard

The first launch walks you through two steps:

1. **Certificate** — upload `app.crt` and `app.key` (or paste their PEM text). Get them from [markoceri/leapmotor-certs](https://github.com/markoceri/leapmotor-certs). Stored persistently in `/data/certs`.
2. **Login** — your Leapmotor account email, password and operation **PIN**. The wizard auto‑detects your model and battery (EU spec).

That's it — the poller starts and data begins to appear.

## Configuration

Everything is configured from the web UI (**Settings**), no YAML needed:

- **Polling interval** — parked (default 30 s) and driving (default 10 s). Faster catches trips/charges sooner; slower means fewer API calls. Polling the cloud does not wake or drain the car.
- **Charge price** — for cost estimates.
- **Language** — English / Italiano.

### Optional: boost from Home Assistant

If you run Home Assistant on the same network, you can trigger a temporary fast‑poll when a trip is about to start (e.g. from a Bluetooth/phone shortcut) by calling `POST http://<mate-host>:4000/api/boost`. With the default 30 s cadence this is optional.

### Wallbox (Home Assistant)

If you charge at home and have a **wallbox already integrated in Home Assistant** (Wallbox Pulsar, Easee, go‑e, Keba, OCPP, …), Mate can pair with it to show live charging data and compare what the **wallbox delivers (AC)** with what the **car receives into the battery (DC)**.

Enable it in **Settings → Wallbox present**, then connect to Home Assistant. How you connect depends on how you run Mate:

- **As a Home Assistant add‑on** — *nothing to configure.* Mate reaches HA through the internal Supervisor API automatically, regardless of how HA is exposed externally (HTTP, HTTPS, Nabu Casa). You'll just see a green **connection status** dot.
- **As standalone Docker** — enter your HA URL (e.g. `http://192.168.1.10:8123`) and a **Long‑Lived Access Token** (HA → your profile → *Security* → *Long‑Lived Access Tokens* → *Create Token*). Local HTTPS, even with a self‑signed certificate, works.

Then expand **Entity mapping** and assign the wallbox sensors (power, energy, status, max current, charging speed, max available power). Mate pre‑selects them automatically and only lists your wallbox device's own entities, so you don't have to scroll through every Home Assistant sensor.

What you get on the new **Wallbox** page:
- a **live panel** (power, status, session energy, charging speed, max available power) plus the session cost (reused from your home charges);
- a **max‑current control** to set the wallbox charging current — note your own HA load‑balancing automations may override it;
- an **AC‑vs‑DC comparison** per charge session (kWh delivered vs into the battery + efficiency), laid out as a year/month/day history; expand a session for its power chart. The wallbox curve uses Home Assistant's history (kept ~10 days), so the comparison appears for recent sessions.

---

## Notes & disclaimer

- Use a **dedicated Leapmotor account** (see Requirements).
- This is an **unofficial** project, not affiliated with Leapmotor. It relies on reverse‑engineered cloud APIs and may break if Leapmotor changes them. Use at your own risk.
- Built on the [`leapmotor-api`](https://github.com/markoceri/leapmotor-api) Python client.

## Credits

- [`markoceri/leapmotor-api`](https://github.com/markoceri/leapmotor-api) — Python cloud client.
- [`markoceri/leapmotor-certs`](https://github.com/markoceri/leapmotor-certs) — app certificate.
- Inspired by [TeslaMate](https://github.com/teslamate-org/teslamate) and the Leapmotor Home Assistant integrations.

## License

[GNU AGPL‑3.0](./LICENSE) © Silvio Bressani.

---
---

# LeapMotor Mate · Italiano

**Tracciamento viaggi, registro ricariche e controllo remoto per veicoli Leapmotor** — un companion self‑hosted (un *TeslaMate* per Leapmotor). Funziona come **add‑on di Home Assistant** o come **container Docker standalone**.

Modelli supportati: **B10 · C10 · T03** (spec. europea).

## Schermate

| Panoramica | Ricariche |
|---|---|
| ![Panoramica](docs/screenshots/overview.png) | ![Ricariche](docs/screenshots/charges.png) |
| **Statistiche** | **Impostazioni** |
| ![Statistiche](docs/screenshots/statistics.png) | ![Impostazioni](docs/screenshots/settings.png) |

## Funzionalità

- **Panoramica** — stato live, batteria, autonomia, mappa posizione, immagine del veicolo.
- **Viaggi** — rilevamento automatico con mappa del percorso, distanza, energia, efficienza e regen.
- **Ricariche** — sessioni con rilevamento AC/DC, energia aggiunta, potenza e grafico di distribuzione.
- **Wallbox (opzionale)** — abbina una wallbox già presente in Home Assistant per vedere potenza/stato di carica live, impostare la corrente max e confrontare l'**AC erogato dalla wallbox** con il **DC entrato in batteria** per sessione, col rendimento di carica.
- **Statistiche** — ripartizione energia guida/clima/altro e trend consumo a 6 settimane (dal cloud Leapmotor).
- **Controllo remoto** — blocco, finestrini, bagagliaio, tetto panoramico, clima, trova auto, preriscaldo batteria.
- **Indipendente** — interroga direttamente il cloud Leapmotor (configurabile 10–30 s). Nessuna dipendenza dall'app o da Home Assistant; interrogare il cloud **non** sveglia né scarica l'auto.
- **UI bilingue** — Italiano / English.

## Come funziona

```
Cloud Leapmotor  ──►  Poller (state machine)  ──►  SQLite  ──►  Web UI (FastAPI + HTMX)
                       viaggi / ricariche / regen              + comandi remoti
```

I dati restano in un database SQLite locale. Nulla viene inviato altrove se non al cloud ufficiale Leapmotor.

## Requisiti

1. **Un account Leapmotor.** ⚠️ **Usa un account *dedicato*, non quello del telefono.** Il cloud Leapmotor lega una sessione per dispositivo: un secondo client può sfrattare il telefono (e viceversa). Crea un account separato e condividi l'auto con esso dall'app ufficiale.
2. **Il certificato TLS dell'app Leapmotor** (`app.crt` + `app.key`). È *uguale per tutti* (identifica l'app, non te) e **non** è incluso in questo repository. Scarica i due file da:

   👉 **https://github.com/markoceri/leapmotor-certs**

   Li carichi una volta sola durante il wizard di setup.

## Installazione

### Opzione A — Add‑on Home Assistant

1. In Home Assistant: **Impostazioni → Add‑on → Store → ⋮ → Repository**, e aggiungi l'URL del repository **add‑on** (nota il suffisso `-addon` — è un repo separato dal codice):

   ```
   https://github.com/ProtossBlaster/leapmotor-mate-addon
   ```

2. Installa **LeapMotor Mate**, avvialo e apri il pannello (icona auto nella barra laterale).
3. Segui il wizard di setup.

Il database è salvato nella `/data` persistente dell'add‑on, quindi sopravvive a riavvii e aggiornamenti.

### Opzione B — Docker standalone

```bash
git clone https://github.com/ProtossBlaster/leapmotor-mate.git
cd leapmotor-mate
docker compose up -d
```

Poi apri **http://localhost:4000** e segui il wizard.

Il database è salvato in `./data/` (montato su `/data` nel container).

## Wizard di setup

Al primo avvio due passi:

1. **Certificato** — carica `app.crt` e `app.key` (oppure incolla il testo PEM). Li trovi su [markoceri/leapmotor-certs](https://github.com/markoceri/leapmotor-certs). Salvati in modo persistente in `/data/certs`.
2. **Login** — email account Leapmotor, password e **PIN** operativo. Il wizard rileva automaticamente modello e batteria (spec. EU).

Fatto — il poller parte e i dati iniziano a comparire.

## Configurazione

Tutto si configura dalla UI web (**Impostazioni**), senza YAML:

- **Intervallo di polling** — parcheggiata (default 30 s) e in marcia (default 10 s). Più veloce rileva prima viaggi/ricariche; più lento riduce le chiamate. Interrogare il cloud non sveglia né scarica l'auto.
- **Prezzo ricarica** — per la stima dei costi.
- **Lingua** — Italiano / English.

### Opzionale: boost da Home Assistant

Se hai Home Assistant sulla stessa rete, puoi attivare un polling veloce temporaneo all'inizio di un viaggio (es. da uno shortcut Bluetooth/telefono) chiamando `POST http://<host-mate>:4000/api/boost`. Con la cadenza di default a 30 s è opzionale.

### Wallbox (Home Assistant)

Se ricarichi a casa e hai una **wallbox già integrata in Home Assistant** (Wallbox Pulsar, Easee, go‑e, Keba, OCPP, …), Mate può abbinarla per mostrare i dati di ricarica live e confrontare ciò che la **wallbox eroga (AC)** con ciò che l'**auto riceve in batteria (DC)**.

Attivala in **Impostazioni → Wallbox presente**, poi connettiti a Home Assistant. Come ti connetti dipende da come esegui Mate:

- **Come add‑on di Home Assistant** — *niente da configurare.* Mate raggiunge HA tramite l'API interna del Supervisor in automatico, a prescindere da come HA è esposto all'esterno (HTTP, HTTPS, Nabu Casa). Vedrai solo lo **stato connessione** con la pallina verde.
- **Come Docker standalone** — inserisci l'URL di HA (es. `http://192.168.1.10:8123`) e un **Long‑Lived Access Token** (HA → tuo profilo → *Sicurezza* → *Token di accesso Long‑Lived* → *Crea token*). L'HTTPS locale, anche con certificato self‑signed, funziona.

Poi espandi **Mappatura entità** e assegna i sensori della wallbox (potenza, energia, stato, corrente max, velocità di carica, potenza max disponibile). Mate li pre‑seleziona da solo e mostra solo le entità del tuo dispositivo wallbox, così non devi scorrere tutti i sensori di Home Assistant.

Cosa ottieni nella nuova pagina **Wallbox**:
- un **pannello live** (potenza, stato, energia sessione, velocità di carica, potenza max disponibile) più il costo sessione (riusato dalle tue ricariche home);
- un **controllo della corrente max** per impostare la corrente di carica della wallbox — nota che le tue automazioni HA di bilanciamento del carico potrebbero sovrascriverlo;
- un **confronto AC‑vs‑DC** per sessione (kWh erogati vs entrati in batteria + rendimento), come storico anno/mese/giorno; espandi una sessione per il grafico di potenza. La curva wallbox usa lo storico di Home Assistant (conservato ~10 giorni), quindi il confronto compare per le sessioni recenti.

## Note e disclaimer

- Usa un **account Leapmotor dedicato** (vedi Requisiti).
- Progetto **non ufficiale**, non affiliato a Leapmotor. Usa API cloud ricavate per reverse‑engineering e può smettere di funzionare se Leapmotor le cambia. Usalo a tuo rischio.
- Basato sul client Python [`leapmotor-api`](https://github.com/markoceri/leapmotor-api).

## Licenza

[GNU AGPL‑3.0](./LICENSE) © Silvio Bressani.
