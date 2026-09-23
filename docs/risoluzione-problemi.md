# Quando qualcosa va storto

Questo documento è scritto per il momento in cui ti serve: qualcosa non
funziona e non ricordi più i dettagli.

**Prima di tutto: i tuoi dati sono in `/config/spesa`.** Il sistema è
costruito per non toccarli quando è in dubbio. Un file corrotto viene messo
da parte, mai sovrascritto; una transazione interrotta viene annullata, mai
lasciata a metà. Nel peggiore dei casi perdi l'ultima operazione, non
l'archivio.

---

## Da dove partire

| Sintomo | Vai a |
|---|---|
| Il Comando Rapido dà errore | [Errori dall'iPhone](#errori-dalliphone) |
| I sensori non si aggiornano | [I sensori sono fermi](#i-sensori-sono-fermi) |
| Notifica «archivio in sola lettura» | [Archivio bloccato](#archivio-bloccato) |
| Notifica «conflitti nell'archivio» | [Conflitti](#conflitti-nellarchivio) |
| Notifica «transazione interrotta» | [Transazione interrotta](#transazione-interrotta) |
| Un mese è sparito dai totali | [Mese non caricabile](#mese-non-caricabile) |
| L'integrazione non parte | [Setup fallito](#setup-fallito) |

Il primo posto dove guardare è sempre **`sensor.…_da_verificare`**: i suoi
attributi contengono `archivio_bloccato`, `motivo_blocco`, `mesi_esclusi` e
`violazioni`.

Il secondo sono i log:

```bash
grep custom_components.spesa /config/home-assistant.log | tail -40
```

---

## Errori dall'iPhone

Ogni risposta di errore contiene `retry_safe`, che risponde a una sola
domanda: **ha senso ripetere ora questa identica richiesta?**

| Codice | `error` | Causa | Cosa fare |
|---|---|---|---|
| 400 | `invalid_payload` | validazione fallita | leggi `details`: dice campo e problema |
| 400 | `missing_schema_version` | manca `"schema_version": 1` | correggi la fonte |
| 400 | `unsupported_schema_version` | probabilmente `"1"` con le virgolette | dev'essere il numero |
| 400 | `invalid_json` | JSON malformato | il corpo è stato alterato dal Comando Rapido |
| 400 | `invalid_veryfi_payload` | la risposta Veryfi non ha la forma attesa | vedi sotto |
| 401 | `unauthorized` | token errato o header assente | controlla `X-Spesa-Token` |
| 408 | `request_timeout` | corpo non completato in 30 s | rete lenta, **riprova** |
| 409 | `duplicate_receipt` | già registrato | normale dopo un doppio lancio |
| 413 | `payload_too_large` | corpo oltre il limite | 256 KB sull'endpoint normale, 2 MB su quello Veryfi |
| 423 | `archive_locked` o `month_degraded` | scritture bloccate | **non reinviare**, vedi sotto |
| 500 | `internal_error` | errore di salvataggio | se `retry_safe` è `true`, riprova |
| 503 | `integration_unavailable` | integrazione non attiva o in ricarica | riprova fra poco |

### 401 che prima funzionava

Hai rigenerato il token e non hai aggiornato il Comando Rapido. Prendi il
valore dalla nuova notifica.

### 400 `invalid_veryfi_payload`

Quasi sempre il corpo della richiesta a Home Assistant è impostato su **JSON**
invece che su **File**. Con JSON, Comandi Rapidi ricostruisce il corpo a modo
suo e la risposta di Veryfi arriva alterata.

Se il tipo è corretto, leggi `details`: dice quale campo manca — di solito
`vendor.name`, `date`, `total` o `line_items`, cioè Veryfi non è riuscito a
leggere lo scontrino.

### 401 da Veryfi, non da Home Assistant

Nell'header `AUTHORIZATION` manca la parola `apikey` prima di
`username:chiave`, oppure è finito uno spazio in coda incollando.

### Come capire dove si rompe

Inserisci un'azione **Anteprima rapida** subito dopo il passaggio che
sospetti: mostra cosa sta passando e ferma lì il comando. Poi la rimuovi.

---

## I sensori sono fermi

Se uno scontrino arriva con **201** ma i sensori non si muovono, cerca nei
log:

```
Detected that custom integration 'spesa' calls async_write_ha_state
from a thread other than the event loop
```

Significa che il decoratore `@callback` su `_handle_update` in `entity.py`
manca. Senza, Home Assistant esegue l'aggiornamento in un thread
dell'executor, dove la chiamata non è ammessa.

I dati sono al sicuro: è solo la propagazione verso le entità a essere rotta.
Un riavvio dopo aver rimesso il decoratore risolve.

---

## Archivio bloccato

**Sintomo** — notifica «archivio in sola lettura», endpoint che rispondono
423, controlli di modifica grigi nella dashboard.

**Cosa significa** — Home Assistant non è in grado di stabilire se i file su
disco rappresentino lo stato prima o dopo un'operazione interrotta. Ha scelto
di fermarsi invece di scriverci sopra.

**Cosa NON è successo** — nessun dato è stato cancellato. I file sono dove
erano.

### La procedura

**1. Leggi il motivo.** Sta nella notifica e nell'attributo `motivo_blocco`.
Le due cause tipiche: un journal di transazione illeggibile, oppure un
ripristino fallito dopo un errore di scrittura.

**2. Guarda i file.**

```bash
ls -la /config/spesa/
```

Cerca `.transaction.blocked`, che contiene la causa e dove è finito il
journal, e file con suffisso `.journal.orphan-…` o `.corrupt-…`.

**3. Controlla i dati.**

```bash
python3 -c "
import json, glob
for f in sorted(glob.glob('/config/spesa/2*.json')):
    d = json.load(open(f))
    tot = sum(r['included_total'] for r in d['receipts'])
    print(f, len(d['receipts']), 'scontrini,', round(tot,2), 'EUR')
"
```

Confronta con quello che ti aspetti. È l'unico passaggio che richiede il tuo
giudizio: solo tu sai se l'ultimo scontrino inserito doveva esserci.

**4. Chiedi una diagnosi.**

Strumenti per sviluppatori → Azioni → `spesa.sblocca_archivio`, **senza**
spuntare la conferma, con **Rispondi con dati** attivo.

Risponde se i file sono formalmente validi. Se trova mesi non caricabili,
violazioni o un journal ancora presente, **rifiuta lo sblocco** e te lo dice:
in quel caso l'archivio non è solo di provenienza incerta, è rotto, e vanno
prima risolti quei problemi.

**5. Sblocca.**

Solo quando sei convinto che i dati siano quelli giusti, richiama l'azione
con `conferma: true`.

> **Non è una riparazione.** È una tua dichiarazione: *ho verificato, accetto
> lo stato corrente come autorevole*. Home Assistant non può stabilirlo da
> solo, perché l'informazione necessaria è andata persa insieme al journal.

### Se c'è ancora un journal valido

La notifica lo dice. In quel caso **riavvia Home Assistant**: il recovery
all'avvio lo applica e riporta l'archivio allo stato precedente
automaticamente. Non serve sbloccare nulla.

---

## Transazione interrotta

**Sintomo** — notifica «transazione interrotta ripristinata» all'avvio.

Home Assistant si è fermato nel mezzo di un'operazione che toccava più mesi.
Al riavvio ha riportato l'archivio allo stato precedente.

**L'ultima operazione è andata persa.** La notifica elenca i mesi
ripristinati e quelli rimossi perché non esistevano prima.

Controlla in dashboard che l'ultimo scontrino inserito o l'ultima modifica
siano quelli attesi, e se serve ripetili.

Questo è il funzionamento corretto, non un guasto: il sistema preferisce
annullare l'operazione piuttosto che conservarne metà.

---

## Conflitti nell'archivio

**Sintomo** — notifica «conflitti nell'archivio», uno o più mesi spariti dai
totali.

Due scontrini violano un'invariante fondamentale: stesso `receipt_id` in file
diversi, oppure due scontrini identici articolo per articolo.

Succede quasi solo dopo un ripristino parziale da backup, o modificando i
file a mano.

**I mesi coinvolti sono esclusi dalle statistiche**, non solo segnalati: dati
in conflitto non devono entrare nei totali come se fossero validi.

### Risolvere

La notifica dice quali `receipt_id` sono in conflitto e in quali mesi.

```bash
grep -n "IL-RECEIPT-ID" /config/spesa/*.json
```

Apri i file con File editor, decidi quale copia tenere, e rimuovi l'altra
dall'array `receipts`. Attenzione alle virgole: dopo la modifica il JSON
deve restare valido.

```bash
python3 -c "import json; json.load(open('/config/spesa/2026-09.json')); print('OK')"
```

Poi chiama `spesa.ricalcola`. Se le invarianti tornano valide, il blocco
sparisce da solo e i mesi rientrano nelle statistiche.

---

## Mese non caricabile

**Sintomo** — un mese compare in `mesi_esclusi`, i suoi scontrini spariscono
dai totali, le scritture su quel mese vengono rifiutate.

Il file principale è illeggibile **e** anche il backup lo è. Se solo il
principale fosse rotto, il sistema avrebbe caricato il `.bak` da solo,
mettendo il file problematico in quarantena con suffisso `.corrupt-…`.

### Due cause distinte

**JSON sintatticamente rotto.** Il log dice riga e colonna. Di solito una
virgola di troppo o una parentesi mancante dopo un'edizione a mano.

**Struttura non valida.** Il JSON si legge ma non rispetta lo schema. Il log
indica il percorso preciso, per esempio
`2026-09.receipts[3].items[7].raw_name: campo obbligatorio mancante`.

### Risolvere

```bash
ls -la /config/spesa/
python3 -m json.tool /config/spesa/2026-09.json
```

Se il file è irrecuperabile, guarda i file in quarantena e il `.bak`: uno dei
due potrebbe essere buono.

```bash
cp /config/spesa/2026-09.json.bak /config/spesa/2026-09.json
```

Poi `spesa.ricalcola`.

Se nessuna copia è utilizzabile, recupera il file da un backup di Home
Assistant — vedi [backup-e-ripristino.md](backup-e-ripristino.md).

---

## Setup fallito

**Sintomo** — l'integrazione risulta «Non riuscito il setup» in Impostazioni.

I log dicono perché. Le cause possibili sono tre.

**Cartella non scrivibile** — `/config/spesa` non esiste e non può essere
creata, o i permessi sono sbagliati.

```bash
ls -ld /config/spesa
```

**Archivio non caricabile** — un errore imprevisto durante la lettura. Il log
riporta il traceback completo.

**Notifica del token non creata** — il setup si ferma di proposito prima di
montare qualunque risorsa, così da non lasciare stato a metà. Viene ritentato
automaticamente.

In tutti e tre i casi Home Assistant ritenta da solo. Se il problema persiste,
risolvi la causa e ricarica l'integrazione da Impostazioni.

---

## Il Comando Rapido non parte più

Controlla in ordine.

**Il token è ancora valido?** Se hai chiamato `spesa.rigenera_token`, no.

**L'indirizzo esterno funziona?** Prova ad aprirlo dal browser del telefono
con la rete dati, senza Wi-Fi.

**L'integrazione è caricata?** Impostazioni → Dispositivi e servizi: deve
dire «1 servizio».

**Veryfi ha ancora credito?** Il piano gratuito ha un limite mensile. Il
portale lo mostra.

---

## Riportare tutto a zero

Se vuoi ricominciare da capo mantenendo l'integrazione:

```bash
# Ferma Home Assistant prima, o almeno rimuovi l'integrazione dalla UI
mv /config/spesa /config/spesa-vecchio-$(date +%Y%m%d)
```

Riavvia. L'integrazione ricrea la cartella vuota e riparte da zero. I vecchi
dati restano dove li hai spostati.

Per rimuovere tutto, vedi la sezione disinstallazione in
[backup-e-ripristino.md](backup-e-ripristino.md).

---

## Log e diagnostica

Per vedere più dettagli, in `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.spesa: debug
```

Il livello `debug` mostra ogni salvataggio, le riconciliazioni dei duplicati
e le transazioni. **Non lasciarlo attivo**: riempie il log.

Il token non compare **mai** nei log, a nessun livello.

### Cosa il sistema registra normalmente

Una riga per scontrino ricevuto, accettato o respinto, con il motivo. Un
avviso quando uno scontrino risulta da verificare. Un errore quando un
salvataggio fallisce o un file non è caricabile. Nient'altro: circa due righe
al giorno nell'uso normale.

I tentativi non autorizzati sono limitati a una riga al minuto, anche sotto
scansione.