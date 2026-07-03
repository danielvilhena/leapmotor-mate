"""TEMPORARY T03 climate command probe (#67). Nobody in the ecosystem has the T03's remote A/C-OFF
(kerniger #28, markoceri #9), and its fan/recirc behaviour is unclear — so we let a T03 owner
(rossiadobe) fire candidate payloads at the real car and report what happens. Every press is logged with
a T03-TEST marker so the exact payload + the cloud's response land in the diagnostic he sends us.

Organised as SECTIONS so we can add more probes over time without touching the wiring. ⛔ REMOVE this
whole module + its /t03-offtest route + the Settings card once we're done. Gated to car_type == "T03"
(inert for every other model). Sends via api.ac_switch(params=…) (raw cmd-170, no library change) — the
T03 honors only operate=manual and ignores operate=auto/off/close (last proven on-car) — plus one
cmd-171 schedule lever."""
import json
import logging

import command_client

log = logging.getLogger("leapmotor_mate.t03_offtest")

# id spaces: 0-6 = OFF candidates, 10-15 = other climate probes. Each item is one distinct hypothesis;
# cmd 170 (ac_switch raw params) unless a cmd171 payload is set.
SECTIONS = [
    {
        "key": "off",
        "title": "1 · Spegnimento — quale comando spegne l'A/C?",
        "help": "Accendi con Raffredda (~15s), premi UN pulsante, poi guarda se l'auto si SPEGNE davvero.",
        "items": [
            {"id": 0, "label": "Baseline (operate=close)", "note": "Deve NON spegnere — controllo del metodo",
             "cmd170": {"circle": "out", "mode": "wind", "operate": "close", "position": "all",
                        "temperature": "26", "windlevel": "3", "wshld": "0"}},
            {"id": 1, "label": "Manual · vent · ventola 0", "note": "ventola a 0 in vent = sistema spento?",
             "cmd170": {"circle": "out", "mode": "wind", "operate": "manual", "position": "all",
                        "temperature": "26", "windlevel": "0", "wshld": "0"}},
            {"id": 2, "label": "Manual · cold · ventola 0", "note": "come sopra ma in cold (compressore)",
             "cmd170": {"circle": "in", "mode": "cold", "operate": "manual", "position": "all",
                        "temperature": "18", "windlevel": "0", "wshld": "0"}},
            {"id": 3, "label": "Manual · acSwitch=0", "note": "chiave nuova: nome del segnale stato (1938)",
             "cmd170": {"circle": "out", "mode": "wind", "operate": "manual", "position": "all",
                        "temperature": "26", "windlevel": "1", "wshld": "0", "acSwitch": "0"}},
            {"id": 4, "label": "Manual · enable=0", "note": "chiave nuova stile prepare enable:false",
             "cmd170": {"circle": "out", "mode": "wind", "operate": "manual", "position": "all",
                        "temperature": "26", "windlevel": "1", "wshld": "0", "enable": "0"}},
            {"id": 5, "label": "Close · campi 'acceso' (in/cold)", "note": "close mai provato coi campi on",
             "cmd170": {"circle": "in", "mode": "cold", "operate": "close", "position": "all",
                        "temperature": "18", "windlevel": "7", "wshld": "0"}},
            {"id": 6, "label": "Schedule cmd171 · on=0", "note": "leva diversa: flag on/off della programmazione",
             "cmd171": {"controls": [{"on": "0", "operate": "manual", "circle": "out", "mode": "wind",
                                      "position": "all", "temperature": "26", "windlevel": "1", "wshld": "0"}]}},
        ],
    },
    {
        "key": "clima",
        "title": "2 · Altri test clima",
        "help": "Accendi con Raffredda, prova questi e guarda se l'auto REAGISCE (ventola / caldo / aria).",
        "items": [
            {"id": 10, "label": "Ventola → 1", "note": "la ventola scende davvero a 1?",
             "cmd170": {"circle": "in", "mode": "cold", "operate": "manual", "position": "all",
                        "temperature": "18", "windlevel": "1", "wshld": "0"}},
            {"id": 11, "label": "Ventola → 4", "note": "cambia a 4?",
             "cmd170": {"circle": "in", "mode": "cold", "operate": "manual", "position": "all",
                        "temperature": "18", "windlevel": "4", "wshld": "0"}},
            {"id": 12, "label": "Ventola → 7", "note": "sale a 7? (se 1/4/7 non cambiano → l'auto la auto-gestisce)",
             "cmd170": {"circle": "in", "mode": "cold", "operate": "manual", "position": "all",
                        "temperature": "18", "windlevel": "7", "wshld": "0"}},
            {"id": 13, "label": "Riscalda (manual/hot)", "note": "parte il riscaldamento? (finora solo freddo provato)",
             "cmd170": {"circle": "in", "mode": "hot", "operate": "manual", "position": "all",
                        "temperature": "28", "windlevel": "5", "wshld": "0"}},
            {"id": 14, "label": "Ricircolo ON (aria interna)", "note": "l'aria passa a ricircolo?",
             "cmd170": {"circle": "in", "mode": "cold", "operate": "manual", "position": "all",
                        "temperature": "18", "windlevel": "5", "wshld": "0"}},
            {"id": 15, "label": "Ricircolo OFF (aria esterna)", "note": "torna ad aria esterna?",
             "cmd170": {"circle": "out", "mode": "cold", "operate": "manual", "position": "all",
                        "temperature": "18", "windlevel": "5", "wshld": "0"}},
        ],
    },
]

_BY_ID = {it["id"]: it for sec in SECTIONS for it in sec["items"]}


def fire(item_id: int):
    """Send one item's payload to the car, logged with a T03-TEST marker. Returns (ok, msg)."""
    it = _BY_ID.get(item_id)
    if not it:
        return False, "unknown item"
    if "cmd170" in it:
        log.info("T03-TEST #%s (%s) cmd170 → %s", it["id"], it["label"],
                 json.dumps(it["cmd170"], separators=(",", ":")))
        return command_client._session.execute(lambda api, vin: api.ac_switch(vin, params=it["cmd170"]))
    body = json.dumps(it["cmd171"], separators=(",", ":"))
    log.info("T03-TEST #%s (%s) cmd171 → %s", it["id"], it["label"], body)
    return command_client._session.execute(lambda api, vin: api._remote_control_raw(
        vin=vin, cmd_id="171", cmd_content=body, action_label="t03_test_schedule"))
