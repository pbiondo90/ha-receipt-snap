# Spesa alimentare per Home Assistant

Registra, corregge e analizza la spesa del supermercato. Fotografi lo
scontrino con l'iPhone, e dopo una decina di secondi la spesa è in archivio,
articolo per articolo.

I dati vivono in file JSON mensili sotto `/config/spesa`, **indipendenti dal
recorder**: lo storico non scade con la retention della cronologia e finisce
nei normali backup di Home Assistant.

---

## Cosa fa

- Riceve gli scontrini via HTTP da un Comando Rapido iOS
- Accetta sia un payload già strutturato, sia la risposta grezza di
  [Veryfi](https://www.veryfi.com/), tradotta lato server
- Calcola totali mensili, giornalieri, per categoria e per supermercato
- Permette di **escludere singoli articoli** dalla spesa senza cancellarli:
  il totale originale dello scontrino resta intatto
- Segnala gli scontrini che non tornano, invece di rifiutarli
- Riconosce i duplicati anche quando l'identificativo è diverso

## Cosa NON fa

- Non legge le foto: l'estrazione avviene fuori da Home Assistant
- Non corregge automaticamente gli errori di lettura, per scelta
- Non dipende dal recorder per alcuna statistica

---

## Installazione

### HACS

1. HACS → Integrazioni → menu ⋮ → **Repository personalizzati**
2. URL `https://github.com/pbiondo90/ha-receipt-snap`, categoria **Integration**
3. Cerca **Spesa alimentare**, scarica, riavvia Home Assistant
4. Impostazioni → Dispositivi e servizi → **Aggiungi integrazione** → Spesa

### Manuale

Copia `custom_components/spesa/` dentro la tua cartella `config/custom_components/`,
riavvia, poi aggiungi l'integrazione dalla UI.

### Dopo l'installazione

Compare una **notifica persistente con il token** dell'endpoint. Viene
mostrata una sola volta: copiala nel Comando Rapido prima di chiuderla. Se la
perdi, il servizio `spesa.rigenera_token` ne produce una nuova e revoca la
precedente.

---

## Endpoint

Due, con la stessa autenticazione.

| Percorso | Accetta |
|---|---|
| `/api/spesa/receipt` | payload già nello schema dell'integrazione |
| `/api/spesa/receipt/veryfi` | risposta di Veryfi, tradotta lato server |

Autenticazione tramite header **`X-Spesa-Token`**, una credenziale dedicata a
questa sola funzione. Non è un token di Home Assistant: chi lo possiede può
solo inviare scontrini, non controllare la casa né leggere lo stato delle
entità.

### Esempio

```bash
curl -X POST https://tuo-indirizzo/api/spesa/receipt \
  -H "X-Spesa-Token: il-tuo-token" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": 1,
    "receipt_id": "20260921-conad-104522-a81f",
    "date": "2026-09-21",
    "time": "10:45",
    "store": "Conad",
    "receipt_total": 12.45,
    "items": [
      {"id":"01","raw_name":"BARILLA SPAGH N5 500GR",
       "name":"Spaghetti Barilla n.5 500 g","quantity":2,
       "unit_price":1.29,"price":2.58,"category":"Alimentari"}
    ]
  }'
```

Risposta `201` con i totali **calcolati da Home Assistant**, non quelli
dichiarati dal client.

---

## Entità

Tredici, raccolte sotto un unico dispositivo.

| Entità | Cosa mostra |
|---|---|
| `sensor.…_mese_corrente` | spesa del mese, con confronto, media e proiezione |
| `sensor.…_mese_precedente` | termine di paragone |
| `sensor.…_oggi` | spesa di oggi |
| `sensor.…_ultimi_scontrini` | numero in archivio, ultimi 20 negli attributi |
| `sensor.…_da_verificare` | quanti richiedono controllo, più lo stato di salute |
| `sensor.…_dettaglio_scontrino` | lo scontrino selezionato, con i suoi articoli |
| `select.…_scontrino` | naviga fra gli scontrini |
| `select.…_articolo` | naviga fra gli articoli |
| `select.…_categoria_articolo` | corregge la categoria |
| `number.…_prezzo_articolo` | corregge il prezzo |
| `number.…_quantita_articolo` | corregge la quantità |
| `text.…_nome_articolo` | corregge il nome |
| `button.…_inverti_inclusione` | include o esclude dalla spesa |

Le ultime cinque agiscono sull'articolo selezionato: quattro tap dalla
dashboard e il totale si aggiorna.

## Azioni

`spesa.aggiorna_articolo` · `spesa.aggiorna_scontrino` ·
`spesa.elimina_articolo` · `spesa.elimina_scontrino` · `spesa.ricalcola` ·
`spesa.rigenera_token` · `spesa.sblocca_archivio`

Le azioni distruttive richiedono una conferma esplicita.

---

## Documentazione

| | |
|---|---|
| [Architettura](docs/architettura.md) | com'è fatto e perché |
| [Uso quotidiano](docs/uso-quotidiano.md) | dashboard, azioni, correzioni |
| [Comando Rapido](docs/comando-rapido.md) | il flusso dall'iPhone |
| [Dashboard](docs/dashboard.md) | configurazione delle viste |
| [Risoluzione problemi](docs/risoluzione-problemi.md) | quando qualcosa va storto |
| [Backup e ripristino](docs/backup-e-ripristino.md) | e disinstallazione completa |

---

## Come tratta i dati

Tre principi, che spiegano quasi tutte le scelte di questo progetto.

**Il dato originale non si tocca.** `raw_name` conserva la descrizione dello
scontrino esattamente com'è stampata, ed è immutabile. `receipt_total` resta
il totale stampato anche quando escludi metà degli articoli.

**I valori calcolati sono sempre ricalcolati.** Totali, motivi di verifica e
impronte anti-duplicato vengono derivati dai dati a ogni modifica e a ogni
riavvio. Quello che dichiara il client finisce solo in diagnostica.

**Un dato dubbio viene segnalato, non corretto.** Se la somma degli articoli
non torna col totale, lo scontrino viene comunque registrato e marcato da
verificare. Un articolo mancante lo correggi in dieci secondi; uno inventato
non te ne accorgi mai.

---

## Requisiti

- Home Assistant 2024.11 o successivo
- Un modo per estrarre i dati dallo scontrino: Veryfi, oppure un modello che
  produca il JSON descritto sopra
- Accesso HTTPS all'istanza dall'esterno, per inviare dallo smartphone

## Licenza

MIT — vedi [LICENSE](LICENSE).
