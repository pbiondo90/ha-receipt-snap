# Architettura

Come è fatto il sistema e perché. Questo documento spiega le decisioni, non
solo la struttura: se un giorno vorrai modificare qualcosa, qui trovi i
vincoli che quella modifica dovrebbe rispettare.

---

## Il flusso

```
iPhone
  └─ foto dello scontrino
       └─ Veryfi              estrazione, fuori da Home Assistant
            └─ POST /api/spesa/receipt/veryfi
                 └─ traduzione deterministica        veryfi.py
                      └─ validazione                 model.py
                           └─ transazione            manager.py
                                └─ file mensile      store.py
                                     └─ entità e dashboard
```

Una volta ricevuto il JSON, Home Assistant è **autonomo**: nessun calcolo
successivo dipende da Veryfi o da un modello linguistico. Correzioni,
statistiche, confronti e ricalcoli avvengono tutti in locale, sui dati
persistiti.

---

## I moduli

Quattordici file Python, con dipendenze in una sola direzione.

```
const ← util
  ↑
model ← store ← manager ← http ← __init__
  ↑                ↑        ↑        ↑
veryfi          entity ← sensor select number text button
```

| Modulo | Responsabilità | Dipendenze |
|---|---|---|
| `const.py` | valori, limiti, categorie, whitelist dei campi | nessuna |
| `util.py` | generazione del token | `const` |
| `model.py` | validazione e calcoli economici | `const` |
| `veryfi.py` | traduzione dal formato Veryfi | nessuna |
| `store.py` | tutto e solo l'I/O su disco | `const`, `model` |
| `manager.py` | stato, lock, transazioni, statistiche | `store`, `model` |
| `http.py` | i due endpoint | `manager`, `model`, `veryfi` |
| `__init__.py` | montaggio, servizi, notifiche | tutti |
| `entity.py` | base delle entità, stato di selezione | `manager` |
| `sensor` `select` `number` `text` `button` | le 13 entità | `entity`, `manager` |

`model.py` e `veryfi.py` sono **puri**: nessun import di Home Assistant,
nessun I/O, nessuno stato globale. Si possono eseguire e collaudare da soli.

---

## Persistenza

### Perché non il recorder

Il recorder di Home Assistant ha una retention configurabile, tipicamente
dieci giorni o un mese. Uno storico di spesa deve durare anni. Inoltre il
recorder registra *stati*, non *dati*: correggere il prezzo di un articolo di
tre mesi fa sarebbe impossibile.

### Formato

Un file JSON per mese in `/config/spesa`:

```
/config/spesa/
    2026-09.json              archivio del mese
    2026-09.json.bak          versione precedente
    .transaction.json         solo durante una transazione multi-mese
    .transaction.blocked      solo quando l'archivio è bloccato
```

```json
{
  "schema_version": 1,
  "month": "2026-09",
  "generated_at": "2026-09-22T15:16:04",
  "receipt_count": 2,
  "receipts": [ … ]
}
```

File mensili perché il mese è già l'unità naturale delle statistiche, i file
restano piccoli, e riscriverne uno costa millisecondi. Tutto l'archivio viene
caricato in memoria all'avvio: un anno di spesa sono un paio di megabyte.

`schema_version` sta nel contenitore, non in ogni scontrino: tutti gli
scontrini di un file appartengono allo stesso schema.

### Scrittura atomica

```
1. serializza in memoria          un errore qui non tocca il disco
2. scrive su .tmp + fsync
3. copia il file corrente su .bak, atomicamente
4. os.replace(.tmp → definitivo)  atomico su ext4
5. fsync della directory
```

**Il backup è vincolante**: se esiste un file precedente e la copia su `.bak`
non riesce, la sostituzione non avviene. Fra «salvare senza rete di
sicurezza» e «non salvare lasciando intatto il dato precedente», questo
progetto sceglie il secondo.

Un'interruzione prima del passo 4 lascia il file definitivo intatto e un
`.tmp` orfano, rimosso al riavvio successivo.

---

## Il modello transazionale

### Copie di lavoro

Nessuna mutazione tocca lo stato pubblicato. Ogni operazione:

1. copia in profondità **solo** i mesi coinvolti
2. applica le modifiche alle copie
3. verifica le invarianti globali sul risultato
4. scrive su disco
5. **solo a scrittura completata** sostituisce lo stato in memoria

Conseguenze: un fallimento di scrittura non richiede di ricostruire la
memoria, perché quella autorevole non è mai stata toccata. E i sensori non
vedono mai uno stato non ancora persistito.

### Lock unico

Un solo `asyncio.Lock` protegge tutte le mutazioni: ingest HTTP, modifiche
dai servizi, pulsanti della dashboard, eliminazioni. Nessun metodo pubblico
ne chiama un altro già sotto lock — `asyncio.Lock` non è rientrante.

L'inversione dell'inclusione legge il valore corrente e lo inverte **dentro
la stessa acquisizione**: due tocchi ravvicinati producono due inversioni,
non due scritture dello stesso valore.

### Transaction journal

Una modifica può toccare due mesi: cambiare la data di uno scontrino lo
sposta di file, e la riconciliazione dei duplicati può aggiornarne altri.

Per queste, prima della prima scrittura viene creato `.transaction.json` con
lo **stato precedente completo** dei mesi coinvolti. Viene rimosso solo
quando tutte le scritture sono riuscite.

> Dopo qualunque interruzione, l'archivio rappresenta integralmente lo stato
> prima della transazione oppure integralmente quello dopo. Mai una
> combinazione.

Al riavvio, un journal presente significa transazione non conclusa con
certezza, e provoca il **rollback completo** — anche se le scritture erano in
realtà riuscite. Perdere l'ultima operazione è accettabile; conservarne metà
no.

### Un'eccezione non garantisce nulla

`save_month()` può fallire **dopo** `os.replace()`, per esempio durante il
`fsync` della directory. Il file nuovo è già visibile ma la chiamata solleva.

Per questo il rollback ripristina **tutti** i mesi della transazione, non
solo quelli che sappiamo di aver scritto. Ripristinare un mese mai toccato è
innocuo; lasciarne fuori uno già modificato lascerebbe metà transazione sul
disco.

### Il contratto degli errori

| Eccezione | Significato | Si può ritentare? |
|---|---|---|
| `StoreError` | scrittura fallita, stato precedente ripristinato | sì |
| `ConsistencyError` | stato non verificabile, archivio bloccato | **no** |

Via HTTP diventano rispettivamente `500` con `retry_safe: true` e `423` con
`retry_safe: false`.

---

## INPUT, INTERNO, DERIVATO

Ogni campo appartiene a una sola delle tre famiglie, e la distinzione governa
chi può scriverlo.

**INPUT** — arriva dal client e rappresenta il dato originale:
`receipt_id`, `date`, `time`, `store`, `receipt_total`, `notes`, e per ogni
articolo `raw_name`, `price`, `quantity`, `unit_price`, `discount`,
`category`, `included`.

**INTERNO** — gestito da Home Assistant, mai accettato dal payload, ma
persistito: `manual_review`, `possible_duplicate_dismissed`,
`name_was_missing`, `category_was_unknown`.

**DERIVATO** — ricalcolato a ogni mutazione e a ogni avvio:
`items_total`, `included_total`, `fingerprint`, `fingerprint_weak`,
`needs_review`, `review_reasons`, `possible_duplicate_of`.

I valori derivati eventualmente presenti nel payload finiscono in
`client_reported`, a titolo puramente diagnostico, per confrontare quanto
aveva calcolato la fonte con quanto calcola Home Assistant.

### Whitelist in entrambe le direzioni

Tutto ciò che non compare nelle whitelist di `const.py` viene scartato: dal
payload in ingresso **e** in rilettura dal disco. Un file modificato a mano
non può iniettare struttura arbitraria nello stato in memoria.

### Una sola definizione di dato valido

Le funzioni `check_*` di `model.py` sono usate sia dall'ingresso HTTP sia
dalla rilettura dal disco. Cambia solo la reazione all'errore: richiesta
respinta nel primo caso, file non caricabile nel secondo.

Senza questa condivisione esisterebbero due nozioni divergenti di validità, e
un file scritto dal sistema potrebbe risultare non rileggibile dal sistema
stesso.

---

## Denaro

Tutti i calcoli monetari avvengono in `Decimal`, mai in `float`. I float
entrano solo al confine con JSON, e rientrano sempre via `Decimal(str(x))`.

Arrotondamento `ROUND_HALF_UP`, quello commerciale: 2 decimali per gli
importi, 3 per le quantità — i prodotti a peso vogliono `0.252 kg`.

Il round-trip verso JSON è esatto entro questi limiti. Gli importi sono
quantizzati a 2 decimali e vincolati a ±10 000, quindi hanno al massimo 7
cifre significative; un float a doppia precisione ne regge 15, e `repr()`
produce la stringa più corta che rilegge allo stesso float. `Decimal('2.58')`
diventa `2.58` nel file, e torna `Decimal('2.58')`.

### I tre totali

| Campo | Cos'è |
|---|---|
| `receipt_total` | il totale **stampato** sullo scontrino. Non cambia mai |
| `items_total` | somma di **tutte** le righe, escluse comprese |
| `included_total` | somma delle sole righe con `included: true` |

**Tutte le statistiche usano `included_total`.** È la risposta al requisito
originario: scontrino da 44 € con 35 € di alimentari, 4 € di detersivo e 5 €
di pile — escludi gli ultimi due, il totale dello scontrino resta 44 € e la
spesa alimentare diventa 35 €.

---

## Anti-duplicati

Due impronte, entrambe calcolate da Home Assistant ignorando qualunque hash
fornito dal client.

**Forte** — `SHA-256` di data, negozio normalizzato, totale, numero articoli
e la serializzazione canonica di ogni articolo: `raw_name` normalizzato,
quantità, prezzo finale. Gli articoli sono ordinati alfabeticamente, così
l'impronta non dipende dall'ordine in cui la fonte li elenca.

`unit_price` e `discount` sono **esclusi** deliberatamente: sono opzionali e
dipendono da quanto bene la fonte interpreta lo scontrino, quindi la stessa
foto può produrli una volta valorizzati e una volta no. Il loro effetto
economico è comunque già dentro `price`.

`category` e `included` sono esclusi perché sono decisioni modificabili dopo
l'inserimento: se entrassero nell'impronta, correggere una categoria farebbe
riemergere confronti già risolti.

**Debole** — solo data, negozio, totale e numero articoli.

| Corrispondenza | Esito |
|---|---|
| stesso `receipt_id` | **409**, rifiutato |
| stessa impronta forte | **409**, rifiutato |
| solo impronta debole | **201**, accettato e segnalato come possibile duplicato |

Il terzo caso esiste perché due spese distinte possono davvero coincidere
per data, negozio, totale e numero di articoli. Rifiutarle sarebbe un falso
positivo; ignorarle, un rischio. Vengono quindi accettate e marcate.

### Riconciliazione simmetrica

Quando A smette di somigliare a B, anche B smette di indicare A. La
rivalutazione copre i membri dei gruppi lasciati e raggiunti, oltre a chi
citava lo scontrino modificato.

Il riferimento in `possible_duplicate_of` **non viene mai cancellato** finché
la somiglianza esiste: resta come memoria storica anche dopo che hai
verificato. È `possible_duplicate_dismissed` a togliere la segnalazione.

Quella verifica decade se cambiano i dati che partecipano alle impronte,
perché la situazione che avevi controllato non è più quella attuale.
Modificare `included`, categoria, nome o note non la invalida: non toccano
le impronte.

---

## Verifica

`needs_review` non è mai scritto direttamente. È sempre il risultato di:

```
review_reasons = motivi automatici degli articoli
               + total_mismatch
               + possible_duplicate (rilevato e non archiviato)
               + manual_flag (se manual_review)

needs_review   = bool(review_reasons)
```

I motivi automatici:

| Codice | Quando |
|---|---|
| `total_mismatch` | somma articoli ≠ totale, oltre 0,05 € |
| `possible_duplicate` | impronta debole coincidente, forte diversa |
| `unknown_category` | categoria non riconosciuta, degradata ad `Altro` |
| `missing_normalized_name` | nome assente, ricavato dal `raw_name` |
| `negative_price` | prezzo negativo senza sconto dichiarato |
| `item_total_mismatch` | quantità × prezzo unitario − sconto ≠ prezzo |

Disattivare `manual_review` rimuove **solo** `manual_flag`: i motivi
automatici restano e spariscono solo correggendo i dati.

### L'invariante del riavvio

> A parità di dati persistiti, un riavvio non modifica `needs_review` né
> `review_reasons`.

È il motivo per cui `name_was_missing` e `category_was_unknown` sono campi
persistiti e non variabili di lavoro: senza di essi, dopo un riavvio non
sapremmo distinguere un nome fornito dalla fonte da un ripiego generato, né
un `Altro` deliberato da uno degradato.

---

## Sicurezza dell'endpoint

Le view dichiarano `requires_auth = False` e gestiscono l'autenticazione
internamente. È l'unico modo per avere una credenziale che **non** sia un
Long-Lived Access Token di Home Assistant, il quale darebbe accesso a tutta
l'API: stati di ogni entità, quasi tutti i servizi, WebSocket.

Superficie in caso di compromissione del token: inserire scontrini
nell'archivio spese. Nient'altro.

### Perché un header custom

`Authorization` non si può usare: il middleware di Home Assistant lo
intercetta **prima** della view e risponde 401 se il bearer non è un token
valido. La richiesta non arriverebbe mai al nostro codice.

### L'ordine dei controlli

```
token → Content-Length → lettura limitata → parsing → traduzione → ingest
```

Una richiesta anonima costa un confronto di stringhe. Non fa leggere il
corpo, non invoca il parser.

Il confronto usa `hmac.compare_digest`, in tempo costante: il tempo di
risposta non rivela quanti caratteri iniziali sono corretti. Il token non
compare mai nei log, nemmeno troncato.

### Generation

Un contatore cambia a ogni attivazione, sostituzione o disattivazione del
runtime. Una richiesta in volo lo cattura all'ingresso e lo riverifica prima
dell'ingest: se è cambiato — reload, unload, o token rigenerato — la
richiesta viene annullata invece di scrivere attraverso un runtime che non
esiste più.

---

## Le entità

Tredici, sotto un unico dispositivo. Nessun polling: aggiornate da due
segnali distinti, perché le cause sono diverse.

`SIGNAL_UPDATED` quando i dati cambiano. `SIGNAL_SELECTION` quando cambia
ciò che stai guardando.

### Lettura sempre, scrittura condizionata

Le entità di sola lettura non hanno override di `available`: durante un
blocco dell'archivio resta possibile consultare totali e dettagli. Il blocco
impedisce le **mutazioni**, non la consultazione.

Le sei entità di modifica si disattivano da sole quando non c'è un articolo
selezionato o l'archivio è bloccato: meglio un controllo grigio di uno che
accetta il tocco e poi fallisce.

### Label e identificativi

I select scambiano stringhe, ma lo stato di selezione conserva solo
`receipt_id` e `item_id`. Le label sono **sempre ricalcolate**: `options` e
`current_option` vengono dalla stessa funzione, nella stessa lettura, quindi
non possono divergere.

Conseguenza: quando correggi un prezzo e la label cambia con lui, la
selezione non si perde.

Invariante correlata: se lo scontrino selezionato esiste ancora, compare
**sempre** fra le opzioni, anche se è uscito dai trenta più recenti. Senza
questa garanzia, il menu mostrerebbe la label di uno scontrino mentre le
modifiche colpirebbero un altro.

### I sensori monetari non hanno `state_class`

Sono aggregati che si