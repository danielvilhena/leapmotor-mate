# LeapMotor Mate

**Trip tracking, charge logging and remote control for Leapmotor vehicles** — a self‑hosted companion (think *TeslaMate* for Leapmotor). Runs as a **Home Assistant add‑on** or as a **standalone Docker** container.

Supported models: **B10 · C10 · T03** (European spec).

> 🇮🇹 [Versione italiana più sotto.](#leapmotor-mate--italiano)

## ☕ Support

LeapMotor Mate is free and open-source, developed in my spare time. If it's useful to you, you can support its development with a coffee — thank you! ☕

<a href="https://www.buymeacoffee.com/protossblaster" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="48"></a>

## Screenshots

| Overview | Trips |
|---|---|
| ![Overview](docs/screenshots/overview.png) | ![Trips](docs/screenshots/trips.png) |
| **Charges** | **Wallbox** |
| ![Charges](docs/screenshots/charges.png) | ![Wallbox](docs/screenshots/wallbox.png) |
| **Statistics** | **Commands** |
| ![Statistics](docs/screenshots/statistics.png) | ![Commands](docs/screenshots/commands.png) |

---

## Features

- **Overview** — live status, battery, range, **READY state**, location map, vehicle picture.
- **Trips** — automatic trip detection with route map, distance, energy, efficiency and regen. Each trip also shows its **total kWh consumed** and its **cost** (energy × the price per kWh of the last charge before the trip, in your currency). You can **delete a trip** (with confirmation) to drop bad data, or **merge trips** that a short, non-charging stop split apart — pick the joinable pairs with a gap slider, preview the combined route, and it's fully reversible (Unmerge any time).
- **Charges** — charge sessions with AC/DC detection, energy added, power and a distribution chart.
- **Battery card on the Charges page** — battery %, range and a battery bar with a **marker at your charge limit**, refreshing live while you watch a charge — no more flipping back to Overview. The **Unlock cable** button now lives here too (it's a charging action, after all).
- **Charge prices** — flat 24h pricing or **time-of-use bands**: set prices per time window, per **day of the week** and per charge type, and each session is costed correctly (energy split across the bands it spans by the real power curve).
- **One-touch vehicle preparation** — a dedicated **Prepare car** page mirroring the official app's *"prepare the vehicle with one tap"*: bundle **A/C** (cool / heat / ventilation / defrost / auto + temperature), **front-seat heating / ventilation**, **steering-wheel & mirror heating**, and run it **now** (immediate) or on a **schedule** (time + weekdays). Scheduled preparations are read from the car, **editable** and removable, and a **Cancel preparation (all off)** button turns everything back off.
- **Charge & climate scheduling** — a dedicated **Scheduling** page. Program a **charge window** (on/off, target SoC, start/end, days of the week) and the **climate pre-conditioning** schedule (quick **cool / heat / ventilation / defrost / auto** presets, time, days, target temperature). Both write to the car and stay in sync with the Leapmotor app; "Active" is a master switch (off = no schedule / charge anytime).
- **CSV export** — export your trips and charges to **CSV** (plus per-trip **GPX**), from the Trips/Charges pages or *Settings → Export/Backup*.
- **Wallbox (optional)** — pair a wallbox already in Home Assistant to see live charging power/status, set the max charging current, and compare **AC delivered by the wallbox** vs **DC into the battery** per session, with charging efficiency. A **"show all entities" advanced mode** lets you map *any* sensor — handy for foreign-language entity names or a generic energy-meter/relay.
- **ABRP (optional)** — forward live telemetry to **A Better Route Planner** for live route planning (enable it with your ABRP token).
- **MQTT → Home Assistant (optional)** — publish the car to Home Assistant via **MQTT Discovery** as native entities (sensors, binary sensors, GPS tracker) plus command buttons.
- **EVCC (optional)** — publish EVCC-friendly MQTT topics so an **EVCC** `type: custom` vehicle reads SoC, plug/charging status, range and odometer (ready-to-paste config in `docs/EVCC.md`).
- **Statistics** — driving/AC/other energy split and a 6‑week consumption trend (from the Leapmotor cloud).
- **Remote control** — lock, windows, trunk, panoramic roof, **climate** (cool / heat / ventilation / defrost, A/C on-off, target temperature), **heated & ventilated seats** (per-seat level), **heated steering wheel & mirrors**, find car, battery preheat, **unlock charge cable**.
- **Navigation** — search an address and **send the destination straight to the car's built‑in navigation**. Shows the car's current address too. Address lookup is keyless by default (OpenStreetMap) with an optional API key (Geoapify/LocationIQ/TomTom) for better house‑number coverage.
- **Independent** — polls the Leapmotor cloud directly (configurable 10–30 s). No dependency on the phone app or Home Assistant; polling the cloud does **not** wake or drain the car. It isn't real-time, so a **Refresh** button (in the sidebar, and the mobile header) pulls the car's latest state on demand.
- **Multilingual UI** — English · Italiano · Français · Deutsch.
- **Currency** — pick your display currency from 30 world currencies (€, $, £, CHF, kr, zł…); every cost reformats to it, with the right symbol placement and decimals.
- **Units** — choose **Metric**, **Imperial UK** (miles & mph, but °C) or **Imperial US** (miles, °F, psi). Distances, speeds, temperatures and tyre pressures display in your chosen system. It's display-only — your stored data always stays metric, so you can switch any time with nothing lost.
- **Diagnostics** — a *Settings → Diagnostics* card with a read-only system snapshot, the recent poller/web logs and the car's current raw signals (with Copy), plus a one-click **downloadable bundle** to attach to a GitHub issue. Personal info (VIN, credentials, e-mail) is **always masked** in the exported logs.
- **OTA-update indicator** — the Overview card tells you when the car has a **vehicle software update** waiting, without opening the official app.
- **Mate self-update badge** — a small badge next to the version number when a **newer Mate release** is on GitHub (checked in the background every 6 h) — handy for standalone-Docker installs.
- **🆕 Editable battery capacity** — pre-filled per model (usable/net kWh); edit it if yours differs, or click **“use measured”** to adopt the value Mate worked out from your own charges. Changing it never rewrites past charges.
- **🆕 Advanced settings** — a collapsible card to tune the edge cases: missed-charge detection threshold, vampire-drain noise floor, and the AC/DC power threshold (for 22 kW AC wallboxes). Sane defaults, one-tap reset.
- **🆕 Recover missed charges** — scan your history for charges that happened while the car was asleep before automatic detection existed; previews what it finds before adding anything.
- **🆕 Single Home Assistant lock toggle** — an MQTT *lock* entity for dashboards **plus a “Door Lock Toggle” switch** for launcher widgets that can't toggle locks (e.g. Samsung's): one tap locks, the next unlocks — perfect as a phone front-screen button. The classic buttons stay too.

## How it works

```
Leapmotor Cloud  ──►  Poller (state machine)  ──►  SQLite  ──►  Web UI (FastAPI + HTMX)
                       trips / charges / regen                   + remote commands
```

The data lives in a local SQLite database. Nothing is sent anywhere except to the official Leapmotor cloud.

> ℹ️ **Mate isn't real-time — it polls.** It reads the car's state from the Leapmotor cloud on an interval: about every **30 s while parked** and **10 s while driving** (tunable in Settings). So a change you make in the official app (opening the trunk, changing the charge limit…) shows on Mate within that window, not instantly. Mate reads **passively** and never wakes the car, so it doesn't drain your battery — the official app feels instant because opening it *wakes* the car. Need it sooner? The **🔄 Refresh** button (top of the sidebar) pulls the latest state on demand. If the car is asleep, the cloud serves its last reported state until the car next wakes.

---

## Requirements

1. **A Leapmotor account.** ⚠️ **Use a *dedicated* account, not the one on your phone.** The Leapmotor cloud binds a session per device, so a second client can evict your phone (or vice‑versa). Create a separate account, then **share the car with it from the official app**: logged in on the account that *owns* the car, share/authorise the vehicle to the new account with **all permissions** and a **permanent** duration (a temporary share expires and breaks Mate later). **Check it worked:** **set the *second* account up in the official Leapmotor app on a device** (not just logging into the account on the web) and confirm the car appears there — if it doesn't, the share isn't active yet and Mate will report *“No vehicle found on this account.”*
2. **The Leapmotor app TLS certificate** (`app.crt` + `app.key`). This is the *same for everyone* (it identifies the Leapmotor app, not you) and is **not** included in this repository. Download the two files from:

   👉 **https://github.com/markoceri/leapmotor-certs**

   You upload them once during the setup wizard (see below).

---

## Installation

### Option A — Home Assistant add‑on

1. In Home Assistant: **Settings → Apps → Install app → ⋮ → Repositories** (on Home Assistant before 2026.2: **Settings → Add‑ons → Add‑on Store → ⋮ → Repositories**), and add the repository URL (note the `-addon` suffix — this is a separate repo from the code):

   ```
   https://github.com/ProtossBlaster/leapmotor-mate-addon
   ```

2. Install **LeapMotor Mate**, start it, and open the panel (car icon in the sidebar).
3. Follow the setup wizard.

The database is stored in the add‑on's persistent `/data`, so it survives restarts and updates.

### Option B — Standalone Docker

**Easiest — run the prebuilt image** (no clone, no build):

```bash
docker run -d --name leapmotor-mate \
  -p 4000:4000 \
  -v "$(pwd)/data:/data" \
  ghcr.io/protossblaster/leapmotor-mate:latest
```

To update later: `docker pull ghcr.io/protossblaster/leapmotor-mate:latest` then recreate the container (or use [Watchtower](https://containrrr.dev/watchtower/) for automatic updates).

**Or build from source:**

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

To switch to a **different Leapmotor account** later, use **Settings → Vehicle → Log out**: it clears only the stored login and re‑opens this wizard (your app certificate stays). All your trips and charges are kept — they're tied to the car's VIN, so the same car carries straight over.

## Configuration

Everything is configured from the web UI (**Settings**), no YAML needed:

- **Polling interval** — parked (default 30 s) and driving (default 10 s). Faster catches trips/charges sooner; slower means fewer API calls. Polling the cloud does not wake or drain the car.
- **Charge prices** — flat or time-of-use, on the dedicated *Charge Prices* page (see below).
- **Language & currency** — English / Italiano / Français / Deutsch, and your display currency (€, $, £, CHF… 30 currencies). The number format (decimal/thousands separator) follows the selected language.

### Charge prices

Set what each kWh costs on the dedicated **Charge Prices** page (💰 in the sidebar), so Mate prices your sessions. Two modes:

- **Fixed (24h)** — one price per charge type (Home / AC / DC / HPC).
- **Time-of-use bands** — add one or more time windows, choose the **days of the week** each applies to (All / Weekdays / Weekend shortcuts), and set a price per charge type for every band. Leave a price blank to fall back to the base price, or enter `0` if it's free in that band. A session spanning two bands is split by its real power curve, and one crossing midnight on a Sat→Sun boundary is priced per day correctly.

Cost changes apply to **new charges only**: a charge's cost is frozen when you confirm its type, so editing prices or bands later never changes past sessions.

**How the kWh are counted (home charges):** if your wallbox is paired and exposes a **kWh energy counter**, Mate samples it **throughout the charge** and bills the **energy it added** — the sum of the counter's increases over the session, i.e. the exact energy the wallbox delivered (conversion losses included), measured, not estimated. It's **reset/race-safe**: it works whether the counter is a lifetime total (like an odometer) or a per-session meter that zeroes mid-charge, no matter when it resets. The charge card leads with the **🔌 wallbox (billed)** kWh and shows the **🔋 in-battery (DC, from SoC)** energy with the AC→DC efficiency beneath it; the cost is simply *wallbox kWh × price*. Without a wallbox counter (or for public charges), Mate bills the **battery (SoC) energy × price**. The instantaneous power is used only for the chart, never for the cost.

> ⚠️ This applies to charges recorded from **v1.12.0 onward** (the counter readings are captured live during the session). Older charges keep the value they were calculated with and **can't be recomputed** with the new method — if you want, you can delete an old session with the 🗑 button on its card.

### Optional: boost from Home Assistant

If you run Home Assistant on the same network, you can trigger a temporary fast‑poll when a trip is about to start (e.g. from a Bluetooth/phone shortcut) by calling `POST http://<mate-host>:4000/api/boost`. With the default 30 s cadence this is optional.

### Wallbox (Home Assistant)

If you charge at home and have a **wallbox already integrated in Home Assistant** (Wallbox Pulsar, Easee, go‑e, Keba, OCPP, …), Mate can pair with it to show live charging data and compare what the **wallbox delivers (AC)** with what the **car receives into the battery (DC)**.

Enable it in **Settings → Wallbox present**, then connect to Home Assistant. How you connect depends on how you run Mate:

- **As a Home Assistant add‑on** — *nothing to configure.* Mate reaches HA through the internal Supervisor API automatically, regardless of how HA is exposed externally (HTTP, HTTPS, Nabu Casa). You'll just see a green **connection status** dot.
- **As standalone Docker** — enter your HA URL (e.g. `http://192.168.1.10:8123`) and a **Long‑Lived Access Token** (HA → your profile → *Security* → *Long‑Lived Access Tokens* → *Create Token*). Local HTTPS, even with a self‑signed certificate, works.

Then expand **Entity mapping** and assign the wallbox sensors. Mate pre‑selects them automatically and only lists your wallbox device's own entities. Each field's label shows the expected **unit**, and the dropdown offers **only sensors of that unit** for the two that feed the maths — **Charging power** lists only kW, **Session energy** only kWh — so you can't accidentally map a kWh meter as power (which would corrupt the stored power and cost figures). The **Show all entities** toggle lifts this for non‑standard setups, and a sensor you already mapped is never hidden.

**What each setting means** — all optional (Mate auto‑detects them; override one only if auto‑mapping picks the wrong entity, e.g. foreign‑language names):

| Setting | What it is |
| --- | --- |
| **Charging power (kW)** | The power the wallbox is delivering **right now** (AC). Drives the live "charging" indicator and the **AC** side of the AC‑vs‑DC comparison. W is auto‑converted to kW. |
| **Status** | The wallbox's own state text from Home Assistant (e.g. *Charging / Connected / Idle / Error*). |
| **Session energy (kWh)** | Energy delivered in the session (kWh; Wh auto‑converted). This is the **AC kWh** Mate bills home charges on (you pay the wallbox AC, conversion losses included) and uses for the efficiency figure. |
| **Max charging current (A)** | The **only writable** setting (a `number` entity): sets the wallbox **max charging current** in **amps** from the Wallbox page. Your own HA load‑balancing automations may override what you set. |
| **Charging speed (km/h)** | Your wallbox's own "charging speed" reading, if it exposes one (shown live). |
| **Max available (kW or A)** | The maximum currently available to the wallbox (e.g. after dynamic load balancing or a tariff cap), if exposed — in **kW or A** depending on the wallbox (V2C/Pulsar report it in amps). Shown as‑is with its own unit. |

Only **Max charging current** writes to the wallbox; everything else is read‑only.

What you get on the new **Wallbox** page:
- a **live panel** (power, status, session energy, charging speed, max available power) plus the session cost (reused from your home charges);
- a **max‑current control** to set the wallbox charging current — note your own HA load‑balancing automations may override it;
- an **AC‑vs‑DC comparison** per charge session (kWh delivered vs into the battery + efficiency), laid out as a year/month/day history; expand a session for its power chart. The wallbox curve uses Home Assistant's history (kept ~10 days), so the comparison appears for recent sessions.

### ABRP (A Better Route Planner)

Forward the car's live data to **A Better Route Planner** for live route planning. In **Settings → ABRP**, enable it and paste your personal ABRP token (in the ABRP app: *Settings → Car → Live Data*, "Generic"). It's off until you enable it, and nothing is sent without a token.

### MQTT → Home Assistant

Publish the car to Home Assistant as **native entities** (in parallel to the Mate UI), via MQTT Discovery. In **Settings → MQTT**, enable it and enter your broker (host, port, username/password; TLS optional). Home Assistant then auto‑creates a *Leapmotor Mate* device with sensors (SOC, range, individual tyres, temperatures, charge…), binary sensors (doors/windows/lock/charging), a GPS tracker, and command buttons (lock/unlock, trunk, find car, unlock charge cable, climate — Quick Cool / Quick Heat / Quick Ventilation / Defrost / A/C Off — and comfort: heated/ventilated seats, steering-wheel & mirror heating). Turning the A/C fully **off** now works on the B10 (using the `operate=off` command found by on‑car testing); the comfort commands use the payloads captured by [@kerniger](https://github.com/kerniger/leapmotor-ha). Works with any MQTT broker (e.g. the Mosquitto add‑on). Use **Test connection** to verify the broker before saving. After a command the state now updates in Home Assistant immediately (no waiting for the next poll), and the **topic prefix** scopes the device — so you can run a second instance on a different prefix without it clashing with the first.

---

## Notes & disclaimer

- **"Vehicle not reporting live data" in the logs is normal.** When the car is parked long enough it goes into **deep sleep** and the cloud returns no live signals. Mate backs off to 15‑minute polling (logged once, not every cycle) and recovers automatically the moment the car reports again — when it's driven, or woken by the official Leapmotor app. To be sure a short trip is captured even straight out of deep sleep, use the boost trigger above.
- **Your credentials are encrypted at rest.** The Leapmotor password/PIN (and any HA / ABRP / MQTT / geocoder tokens) are stored encrypted in the local database, with a per‑install key in `/data/secret.key` (auto‑generated, or set your own via the `MATE_SECRET_KEY` env var). ⚠️ Keep `secret.key` together with your backups — restoring only the database without it will ask you to re‑enter the credentials.
- **Standalone: optional login.** When running standalone (not as an add‑on), set the `MATE_AUTH_PASSWORD` environment variable to require a password to open the app — useful if it's reachable beyond localhost. As a Home Assistant add‑on this is unnecessary (ingress already authenticates) and is ignored.
- Use a **dedicated Leapmotor account** (see Requirements).
- This is an **unofficial** project, not affiliated with Leapmotor. It relies on reverse‑engineered cloud APIs and may break if Leapmotor changes them. Use at your own risk.
- Built on the [`leapmotor-api`](https://github.com/markoceri/leapmotor-api) Python client.

## Credits

- [`kerniger/leapmotor-ha`](https://github.com/kerniger/leapmotor-ha) — original Leapmotor cloud API reverse-engineering / Home Assistant integration.
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

## ☕ Sostieni il progetto

LeapMotor Mate è gratuito e open-source, sviluppato nel tempo libero. Se ti è utile, puoi sostenerne lo sviluppo con un caffè — grazie! ☕

<a href="https://www.buymeacoffee.com/protossblaster" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="48"></a>

## Schermate

| Panoramica | Viaggi |
|---|---|
| ![Panoramica](docs/screenshots/overview.png) | ![Viaggi](docs/screenshots/trips.png) |
| **Ricariche** | **Wallbox** |
| ![Ricariche](docs/screenshots/charges.png) | ![Wallbox](docs/screenshots/wallbox.png) |
| **Statistiche** | **Comandi** |
| ![Statistiche](docs/screenshots/statistics.png) | ![Comandi](docs/screenshots/commands.png) |

## Funzionalità

- **Panoramica** — stato live, batteria, autonomia, **stato READY**, mappa posizione, immagine del veicolo.
- **Viaggi** — rilevamento automatico con mappa del percorso, distanza, energia, efficienza e regen. Ogni viaggio mostra anche i **kWh totali consumati** e il **costo** (energia × prezzo per kWh dell'ultima ricarica prima del viaggio, nella tua valuta). Puoi **eliminare un viaggio** (con conferma) per togliere dati sbagliati, o **unire viaggi** separati da una sosta breve senza ricarica — scegli le coppie unibili con uno slider, vedi l'anteprima del percorso combinato, ed è totalmente reversibile (Separa quando vuoi).
- **Ricariche** — sessioni con rilevamento AC/DC, energia aggiunta, potenza e grafico di distribuzione.
- **Scheda Batteria nella pagina Ricariche** — % batteria, autonomia e barra con un **marker sul tuo limite di carica**, aggiornate live mentre segui una ricarica — senza più tornare in Panoramica. Anche il pulsante **Sblocca cavo** ora vive qui (in fondo è un'azione di ricarica).
- **Prezzi di ricarica** — prezzo fisso 24h o **fasce orarie**: prezzi per fascia, per **giorno della settimana** e per tipo di ricarica, e ogni sessione viene calcolata correttamente (energia ripartita tra le fasce attraversate dalla curva di potenza reale).
- **Preparazione veicolo con un tocco** — una pagina **Preparazione veicolo** dedicata che rispecchia la *"preparazione del veicolo con un solo tocco"* dell'app ufficiale: combina **A/C** (raffreddamento / riscaldamento / ventilazione / sbrinamento / auto + temperatura), **riscaldamento / ventilazione dei sedili anteriori**, **riscaldamento volante e specchietti**, ed eseguila **subito** (immediata) o su **programmazione** (orario + giorni). Le programmazioni si leggono dall'auto, sono **modificabili** e rimovibili, e un pulsante **Annulla preparazione (spegni tutto)** riporta tutto in off.
- **Schedulazione ricarica e clima** — una pagina **Schedulazione** dedicata. Programma una **fascia di ricarica** (on/off, SoC obiettivo, inizio/fine, giorni della settimana) e la **pre-climatizzazione** (preset rapidi **raffreddamento / riscaldamento / ventilazione / sbrinamento / auto**, orario, giorni, temperatura). Entrambe scrivono sull'auto e restano allineate con l'app Leapmotor; "Attivo" è l'interruttore principale (off = nessuna schedulazione / ricarica sempre).
- **Esportazione CSV** — esporta viaggi e ricariche in **CSV** (più **GPX** per viaggio), dalle pagine Viaggi/Ricariche o da *Impostazioni → Esporta/Backup*.
- **Wallbox (opzionale)** — abbina una wallbox già presente in Home Assistant per vedere potenza/stato di carica live, impostare la corrente max e confrontare l'**AC erogato dalla wallbox** con il **DC entrato in batteria** per sessione, col rendimento di carica. Una **modalità avanzata "mostra tutte le entità"** permette di mappare *qualsiasi* sensore — utile per nomi in altre lingue o un contatore/relè generico.
- **ABRP (opzionale)** — invia la telemetria live ad **A Better Route Planner** per la pianificazione dei percorsi (attivala col tuo token ABRP).
- **MQTT → Home Assistant (opzionale)** — pubblica l'auto a Home Assistant via **MQTT Discovery** come entità native (sensori, binary sensor, tracker GPS) più pulsanti comando.
- **EVCC (opzionale)** — pubblica topic MQTT compatibili con **EVCC** così un veicolo `type: custom` legge SoC, stato spina/ricarica, autonomia e odometro (configurazione pronta in `docs/EVCC.md`).
- **Statistiche** — ripartizione energia guida/clima/altro e trend consumo a 6 settimane (dal cloud Leapmotor).
- **Controllo remoto** — blocco, finestrini, bagagliaio, tetto panoramico, **clima** (raffredda / riscalda / ventilazione / sbrinamento, A/C on-off, temperatura), **sedili riscaldati e ventilati** (livello per sedile), **volante e specchietti riscaldati**, trova auto, preriscaldo batteria, **sblocco cavo di ricarica**.
- **Navigazione** — cerca un indirizzo e **invia la destinazione direttamente al navigatore di bordo dell'auto**. Mostra anche l'indirizzo attuale dell'auto. La ricerca indirizzi funziona senza chiave (OpenStreetMap) con una chiave API opzionale (Geoapify/LocationIQ/TomTom) per una copertura migliore dei civici.
- **Indipendente** — interroga direttamente il cloud Leapmotor (configurabile 10–30 s). Nessuna dipendenza dall'app o da Home Assistant; interrogare il cloud **non** sveglia né scarica l'auto. Non è in tempo reale, quindi un pulsante **Aggiorna** (barra laterale, e nell'header su mobile) recupera lo stato attuale dell'auto su richiesta.
- **UI multilingua** — Italiano · English · Français · Deutsch.
- **Valuta** — scegli la valuta di visualizzazione tra 30 valute mondiali (€, $, £, CHF, kr, zł…); ogni costo si riformatta con simbolo e decimali corretti.
- **Unità di misura** — scegli **Metrico**, **Imperiale UK** (miglia e mph, ma °C) o **Imperiale US** (miglia, °F, psi). Distanze, velocità, temperature e pressioni gomme si mostrano nel sistema scelto. È **solo visualizzazione**: i dati restano sempre in metrico, quindi puoi cambiare quando vuoi senza perdere nulla.
- **Diagnostica** — una scheda in *Impostazioni → Diagnostica* con uno snapshot di sistema in sola lettura, i log recenti poller/web e i segnali grezzi attuali dell'auto (con Copia), più un **bundle scaricabile** con un clic da allegare a una issue su GitHub. Le info personali (VIN, credenziali, e-mail) sono **sempre mascherate** nei log esportati.
- **Indicatore aggiornamenti OTA** — la scheda Panoramica ti dice quando l'auto ha un **aggiornamento software del veicolo** in attesa, senza aprire l'app ufficiale.
- **Badge auto-aggiornamento di Mate** — un piccolo badge accanto al numero di versione quando su GitHub c'è una **release di Mate più recente** (controllo in background ogni 6 h) — comodo per le installazioni Docker standalone.
- **🆕 Capacità batteria modificabile** — precompilata per modello (kWh netti/utilizzabili); modificala se la tua è diversa, o clicca **“usa misurata”** per adottare il valore che Mate ha calcolato dalle tue ricariche. Cambiarla non riscrive mai le ricariche passate.
- **🆕 Impostazioni avanzate** — una scheda richiudibile per regolare i casi particolari: soglia rilevamento ricariche perse, soglia rumore consumo-da-fermo e soglia potenza AC/DC (per wallbox AC da 22 kW). Valori predefiniti sensati, reset con un tocco.
- **🆕 Recupero ricariche perse** — cerca nella cronologia le ricariche avvenute mentre l'auto dormiva prima che esistesse il rilevamento automatico; mostra cosa trova prima di aggiungere qualcosa.
- **🆕 Toggle blocco singolo per Home Assistant** — un'entità MQTT *lock* per i dashboard **più uno switch “Door Lock Toggle”** per i widget launcher che non sanno toggleare i lock (es. Samsung): un tocco blocca, il successivo sblocca — perfetto come bottone singolo sulla home del telefono. I pulsanti classici restano.

## Come funziona

```
Cloud Leapmotor  ──►  Poller (state machine)  ──►  SQLite  ──►  Web UI (FastAPI + HTMX)
                       viaggi / ricariche / regen              + comandi remoti
```

I dati restano in un database SQLite locale. Nulla viene inviato altrove se non al cloud ufficiale Leapmotor.

> ℹ️ **Mate non è in tempo reale — fa polling.** Legge lo stato dell'auto dal cloud Leapmotor a intervalli: circa ogni **30 s da fermo** e **10 s in marcia** (regolabile nelle Impostazioni). Quindi un cambiamento fatto dall'app ufficiale (apertura baule, modifica del limite di carica…) compare su Mate entro quel lasso, non all'istante. Mate legge **passivamente** e non sveglia mai l'auto, così non scarica la batteria — l'app ufficiale sembra istantanea perché aprirla *sveglia* l'auto. Ti serve prima? Il pulsante **🔄 Aggiorna** (in cima alla barra laterale) recupera lo stato su richiesta. Se l'auto dorme, il cloud restituisce l'ultimo stato noto finché l'auto non si risveglia.

## Requisiti

1. **Un account Leapmotor.** ⚠️ **Usa un account *dedicato*, non quello del telefono.** Il cloud Leapmotor lega una sessione per dispositivo: un secondo client può sfrattare il telefono (e viceversa). Crea un account separato, poi **condividi l'auto con esso dall'app ufficiale**: dall'account che *possiede* l'auto, condividi/autorizza il veicolo al nuovo account con **tutti i permessi** e durata **permanente** (una condivisione temporanea scade e poi rompe Mate). **Verifica che funzioni:** **configura il *secondo* account nell'app ufficiale Leapmotor su un dispositivo** (non solo accedere all'account via web) e controlla che l'auto compaia — se non c'è, la condivisione non è ancora attiva e Mate dirà *«No vehicle found on this account».*
2. **Il certificato TLS dell'app Leapmotor** (`app.crt` + `app.key`). È *uguale per tutti* (identifica l'app, non te) e **non** è incluso in questo repository. Scarica i due file da:

   👉 **https://github.com/markoceri/leapmotor-certs**

   Li carichi una volta sola durante il wizard di setup.

## Installazione

### Opzione A — Add‑on Home Assistant

1. In Home Assistant: **Impostazioni → Applicazioni → Installa app → ⋮ → Archivi digitali** (su Home Assistant prima della 2026.2: **Impostazioni → Add‑on → Store → ⋮ → Repository**), e aggiungi l'URL del repository (nota il suffisso `-addon` — è un repo separato dal codice):

   ```
   https://github.com/ProtossBlaster/leapmotor-mate-addon
   ```

2. Installa **LeapMotor Mate**, avvialo e apri il pannello (icona auto nella barra laterale).
3. Segui il wizard di setup.

Il database è salvato nella `/data` persistente dell'add‑on, quindi sopravvive a riavvii e aggiornamenti.

### Opzione B — Docker standalone

**Più semplice — immagine già pronta** (niente clone, niente build):

```bash
docker run -d --name leapmotor-mate \
  -p 4000:4000 \
  -v "$(pwd)/data:/data" \
  ghcr.io/protossblaster/leapmotor-mate:latest
```

Per aggiornare in seguito: `docker pull ghcr.io/protossblaster/leapmotor-mate:latest` e ricrea il container (oppure usa [Watchtower](https://containrrr.dev/watchtower/) per gli aggiornamenti automatici).

**Oppure build da sorgente:**

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
- **Prezzi di ricarica** — fisso o a fasce orarie, dalla pagina dedicata *Prezzi di ricarica* (vedi sotto).
- **Lingua e valuta** — Italiano / English / Français / Deutsch, e la valuta di visualizzazione (€, $, £, CHF… 30 valute). Il formato numero (separatore decimale/migliaia) segue la lingua selezionata.

### Prezzi di ricarica

Imposta quanto costa ogni kWh dalla pagina dedicata **Prezzi di ricarica** (💰 nella barra laterale), così Mate calcola il costo delle ricariche. Due modalità:

- **Fisso (24h)** — un prezzo per tipo di ricarica (Home / AC / DC / HPC).
- **Fasce orarie** — aggiungi una o più fasce, scegli i **giorni della settimana** in cui valgono (scorciatoie Tutti / Feriali / Weekend) e imposta un prezzo per tipo di ricarica per ogni fascia. Lascia un prezzo vuoto per usare il prezzo base, oppure metti `0` se in quella fascia è gratis. Una sessione a cavallo di due fasce viene ripartita dalla sua curva di potenza reale, e una che attraversa la mezzanotte sab→dom è tariffata per giorno correttamente.

Le modifiche ai costi valgono solo per le **ricariche future**: il costo si congela alla conferma del tipo, quindi cambiare prezzi o fasce non altera le sessioni già fatte.

**Come vengono contati i kWh (ricariche di casa):** se la tua wallbox è abbinata ed espone un **contatore di kWh**, Mate lo campiona **per tutta la ricarica** e fattura l'**energia aggiunta** — la somma degli incrementi del contatore durante la sessione, cioè l'energia esatta erogata dalla wallbox (perdite di conversione incluse), misurata, non stimata. È **a prova di reset/race**: funziona sia che il contatore sia un totale a vita (come un contachilometri) sia che sia un contatore per-sessione che si azzera a metà ricarica, indipendentemente da quando si resetta. La card della ricarica mostra in primo piano i kWh **🔌 wallbox (da pagare)** e, sotto, l'energia **🔋 in batteria (DC, da SoC)** con il rendimento AC→DC; il costo è semplicemente *kWh wallbox × prezzo*. Senza contatore wallbox (o per le ricariche pubbliche) Mate fattura l'**energia in batteria (SoC) × prezzo**. La potenza istantanea serve solo al grafico, mai al costo.

> ⚠️ Vale per le ricariche registrate **da v1.12.0 in poi** (le letture del contatore vengono catturate dal vivo durante la sessione). Le ricariche più vecchie mantengono il valore con cui erano state calcolate e **non sono ricalcolabili** col nuovo metodo — se vuoi puoi eliminare una vecchia sessione col pulsante 🗑 sulla sua card.

### Opzionale: boost da Home Assistant

Se hai Home Assistant sulla stessa rete, puoi attivare un polling veloce temporaneo all'inizio di un viaggio (es. da uno shortcut Bluetooth/telefono) chiamando `POST http://<host-mate>:4000/api/boost`. Con la cadenza di default a 30 s è opzionale.

### Wallbox (Home Assistant)

Se ricarichi a casa e hai una **wallbox già integrata in Home Assistant** (Wallbox Pulsar, Easee, go‑e, Keba, OCPP, …), Mate può abbinarla per mostrare i dati di ricarica live e confrontare ciò che la **wallbox eroga (AC)** con ciò che l'**auto riceve in batteria (DC)**.

Attivala in **Impostazioni → Wallbox presente**, poi connettiti a Home Assistant. Come ti connetti dipende da come esegui Mate:

- **Come add‑on di Home Assistant** — *niente da configurare.* Mate raggiunge HA tramite l'API interna del Supervisor in automatico, a prescindere da come HA è esposto all'esterno (HTTP, HTTPS, Nabu Casa). Vedrai solo lo **stato connessione** con la pallina verde.
- **Come Docker standalone** — inserisci l'URL di HA (es. `http://192.168.1.10:8123`) e un **Long‑Lived Access Token** (HA → tuo profilo → *Sicurezza* → *Token di accesso Long‑Lived* → *Crea token*). L'HTTPS locale, anche con certificato self‑signed, funziona.

Poi espandi **Mappatura entità** e assegna i sensori della wallbox (potenza, energia, stato, corrente max, velocità di carica, potenza max disponibile). Mate li pre‑seleziona da solo e mostra solo le entità del tuo dispositivo wallbox, così non devi scorrere tutti i sensori di Home Assistant.

**Cosa significa ogni impostazione** — tutte opzionali (Mate le rileva da solo; sovrascrivi una voce solo se la mappatura automatica sceglie l'entità sbagliata, es. nomi in altra lingua):

| Impostazione | Cos'è |
| --- | --- |
| **Potenza** | La potenza che la wallbox eroga **in questo momento** (AC). Pilota l'indicatore "in carica" live e il lato **AC** del confronto AC‑vs‑DC. I W vengono convertiti automaticamente in kW. |
| **Stato** | Il testo di stato della wallbox da Home Assistant (es. *In carica / Connessa / Inattiva / Errore*). |
| **Energia sessione** | Energia erogata nella sessione (kWh; i Wh sono convertiti). È l'**energia AC in kWh** con cui Mate addebita le ricariche di casa (paghi l'AC della wallbox, perdite di conversione incluse) e calcola il rendimento. |
| **Controllo potenza** | L'**unica** impostazione scrivibile (entità `number`): imposta la **corrente di carica massima** (A) della wallbox dalla pagina Wallbox. Le tue automazioni HA di bilanciamento del carico potrebbero sovrascrivere il valore impostato. |
| **Velocità di carica** | La lettura "velocità di carica" della tua wallbox, se la espone (mostrata live). |
| **Potenza max disponibile** | La potenza massima attualmente disponibile per la wallbox (es. dopo bilanciamento dinamico o limite tariffario), se esposta. |

Solo **Controllo potenza** scrive sulla wallbox; tutto il resto è in sola lettura.

Cosa ottieni nella nuova pagina **Wallbox**:
- un **pannello live** (potenza, stato, energia sessione, velocità di carica, potenza max disponibile) più il costo sessione (riusato dalle tue ricariche home);
- un **controllo della corrente max** per impostare la corrente di carica della wallbox — nota che le tue automazioni HA di bilanciamento del carico potrebbero sovrascriverlo;
- un **confronto AC‑vs‑DC** per sessione (kWh erogati vs entrati in batteria + rendimento), come storico anno/mese/giorno; espandi una sessione per il grafico di potenza. La curva wallbox usa lo storico di Home Assistant (conservato ~10 giorni), quindi il confronto compare per le sessioni recenti.

### ABRP (A Better Route Planner)

Invia i dati live dell'auto ad **A Better Route Planner** per la pianificazione dei percorsi. In **Impostazioni → ABRP**, attivala e incolla il tuo token ABRP personale (nell'app ABRP: *Impostazioni → Auto → Dati live*, "Generic"). È disattivata finché non la abiliti, e non invia nulla senza token.

### MQTT → Home Assistant

Pubblica l'auto a Home Assistant come **entità native** (in parallelo all'interfaccia di Mate), via MQTT Discovery. In **Impostazioni → MQTT**, attivala e inserisci il tuo broker (host, porta, utente/password; TLS opzionale). Home Assistant crea automaticamente un dispositivo *Leapmotor Mate* con sensori (SOC, autonomia, gomme singole, temperature, carica…), binary sensor (porte/finestrini/serratura/ricarica), un tracker GPS, e pulsanti comando (lock/unlock, baule, trova auto, sblocco cavo di ricarica, clima — Quick Cool / Quick Heat / Ventilazione / Sbrinamento / A/C Off — e comfort: sedili riscaldati/ventilati, riscaldamento volante e specchietti). Lo spegnimento **completo** dell'A/C ora funziona sulla B10 (usa il comando `operate=off`, individuato con i test sull'auto); i comandi comfort usano i payload catturati da [@kerniger](https://github.com/kerniger/leapmotor-ha). Funziona con qualsiasi broker MQTT (es. l'add‑on Mosquitto). Usa **Prova connessione** per verificare il broker prima di salvare. Dopo un comando lo stato ora si aggiorna in Home Assistant all'istante (senza aspettare il polling successivo), e il **prefisso topic** delimita il dispositivo — così puoi far girare una seconda istanza con un prefisso diverso senza che entri in conflitto con la prima.

## Note e disclaimer

- **Il messaggio "Vehicle not reporting live data" nei log è normale.** Quando l'auto resta parcheggiata abbastanza a lungo va in **deep sleep** e il cloud non restituisce segnali live. Mate passa al polling ogni 15 minuti (loggato una volta sola, non ad ogni ciclo) e si riprende da solo appena l'auto torna a riportare — quando viene guidata, o svegliata dall'app ufficiale Leapmotor. Per essere sicuro di registrare anche un viaggio breve subito dopo il deep sleep, usa il trigger boost qui sopra.
- **Le tue credenziali sono cifrate a riposo.** La password/PIN Leapmotor (e gli eventuali token HA / ABRP / MQTT / geocoder) sono salvati cifrati nel database locale, con una chiave per‑installazione in `/data/secret.key` (auto‑generata, oppure la tua tramite la variabile `MATE_SECRET_KEY`). ⚠️ Conserva `secret.key` insieme ai backup — ripristinando solo il database senza la chiave dovrai re‑inserire le credenziali.
- **Standalone: login opzionale.** In modalità standalone (non add‑on), imposta la variabile d'ambiente `MATE_AUTH_PASSWORD` per richiedere una password all'apertura dell'app — utile se è raggiungibile oltre localhost. Come add‑on Home Assistant non serve (l'ingress autentica già) e viene ignorata.
- Usa un **account Leapmotor dedicato** (vedi Requisiti).
- Progetto **non ufficiale**, non affiliato a Leapmotor. Usa API cloud ricavate per reverse‑engineering e può smettere di funzionare se Leapmotor le cambia. Usalo a tuo rischio.
- Basato sul client Python [`leapmotor-api`](https://github.com/markoceri/leapmotor-api).

## Licenza

[GNU AGPL‑3.0](./LICENSE) © Silvio Bressani.
