# Comando Rapido iOS

Il flusso dall'iPhone all'archivio. Fotografi lo scontrino, tocchi il
comando, dopo una decina di secondi la spesa è registrata.

```
foto → Veryfi → Home Assistant
```

Nessun modello linguistico: l'estrazione la fa Veryfi, la traduzione avviene
lato server in modo deterministico.

---

## Prima di cominciare

Ti servono due set di credenziali.

**Veryfi** — registrati su [veryfi.com](https://www.veryfi.com/) e prendi
`CLIENT_ID`, `USERNAME` e `API_KEY` da **Settings → Keys**. Il `CLIENT_SECRET`
non serve: firma le richieste con HMAC, che Comandi Rapidi non sa calcolare.
Verifica che il tuo account accetti richieste non firmate con un test da
terminale prima di costruire il comando.

**Home Assistant** — il token dalla notifica persistente comparsa
all'installazione.

Le credenziali Veryfi stanno **nel Comando Rapido**, non in Home Assistant: è
l'iPhone a chiamare Veryfi, Home Assistant riceve solo il risultato.

> Un comando rapido condiviso porta con sé le credenziali. Non condividerlo.

---

## Verifica preliminare

Prima di costruire sei azioni, verifica che Veryfi legga bene i tuoi
scontrini.

```bash
CLIENT_ID="..." ; USERNAME="..." ; API_KEY="..."

curl -s -X POST 'https://api.veryfi.com/api/v8/partner/documents' \
  -H "CLIENT-ID: $CLIENT_ID" \
  -H "AUTHORIZATION: apikey $USERNAME:$API_KEY" \
  -F "file=@scontrino.jpg" -o /tmp/veryfi.json

python3 -c "
import json
d = json.load(open('/tmp/veryfi.json'))
print('Negozio:', (d.get('vendor') or {}).get('name'))
print('Data   :', d.get('date'))
print('Totale :', d.get('total'))
print('Righe  :', len(d.get('line_items') or []))
"
```

Confronta con lo scontrino. Se il numero di righe e il totale corrispondono,
la strada è buona.

---

## Le sei azioni

### 1. Scatta foto

Espandi e attiva **Mostra anteprima fotocamera**, così controlli
l'inquadratura prima dello scatto.

In alternativa **Seleziona foto**, se preferisci fotografare prima e inviare
dopo.

### 2. Codifica

Verifica che dica *Codifica Foto in Base64*. Se dice «Decodifica», toccalo
per invertirlo.

L'input è la foto del passaggio 1.

### 3. Ottieni contenuto dall'URL → Veryfi

**URL**

```
https://api.veryfi.com/api/v8/partner/documents
```

**Metodo** `POST`

**Intestazioni**

| Chiave | Valore |
|---|---|
| `CLIENT-ID` | il tuo Client ID |
| `AUTHORIZATION` | `apikey tuo_username:tua_api_key` |
| `Content-Type` | `application/json` |

**Corpo richiesta** `JSON`, con due campi di testo:

| Chiave | Valore |
|---|---|
| `file_name` | `scontrino.jpg` |
| `file_data` | la variabile **Testo codificato** del passaggio 2 |

Per il secondo, tocca il campo e seleziona la variabile dai suggerimenti
sopra la tastiera. Non scriverla a mano.

### 4. Ottieni contenuto dall'URL → Home Assistant

**URL**

```
https://tuo-indirizzo/api/spesa/receipt/veryfi
```

**Metodo** `POST`

**Intestazioni**

| Chiave | Valore |
|---|---|
| `X-Spesa-Token` | il token dalla notifica |
| `Content-Type` | `application/json` |

**Corpo richiesta** `File`, e come contenuto la variabile **Contenuti
dell'URL** del passaggio 3.

> Il tipo di corpo deve essere **File**, non JSON. Con JSON, Comandi Rapidi
> ricostruisce il corpo a modo suo e la risposta di Veryfi arriva alterata.
> Vuoi inoltrarla così com'è.

### 5. Ottieni valore dal dizionario

**Ottieni** `Valore` · **Per** `success` · **In** il risultato del passaggio 4.

### 6. Se

**Condizione** `Valore del dizionario` **è** `vero`.

Nel ramo **Se**, aggiungi due altre azioni *Ottieni valore dal dizionario*
sul risultato del passaggio 4 — chiavi `store` e `included_total` — e poi
**Mostra notifica**:

```
Registrato: [store] — [included_total] €
```

Nel ramo **Altrimenti**, una *Mostra notifica* con la chiave `message`.

---

## Rifiniture

Rinomina il comando e dagli un'icona.

Nelle impostazioni attiva **Mostra nel foglio di condivisione** accettando
**Immagini**: potrai selezionare una foto già scattata, toccare condividi e
lanciare il comando da lì. Più comodo dello scatto in linea, perché controlli
la foto prima.

---

## L'endpoint alternativo

Se preferisci usare un modello linguistico invece di Veryfi, esiste il
secondo endpoint:

```
https://tuo-indirizzo/api/spesa/receipt
```

Accetta il payload già strutturato. Il flusso diventa: foto → modello →
Home Assistant, con un prompt che produca il JSON descritto nel
[README](../README.md).

Funziona, ma richiede un modello capace di leggere uno scontrino lungo senza
perdere righe né inventarne. Sui modelli piccoli è un problema reale: tendono
a completare le parti illeggibili con prodotti plausibili, e un articolo
inventato entra nelle statistiche senza che te ne accorga.

---

## Quando qualcosa non va

| Sintomo | Causa più probabile |
|---|---|
| 401 da Veryfi | manca `apikey` nell'header, o uno spazio in coda |
| Corpo vuoto verso Veryfi | il passaggio 2 non ha prodotto il base64 |
| 400 `invalid_veryfi_payload` | corpo del passaggio 4 impostato su JSON invece che File |
| 401 da Home Assistant | token cambiato dopo una rigenerazione |
| 409 `duplicate_receipt` | scontrino già registrato, normale dopo un doppio lancio |
| Nessuna risposta | indirizzo esterno non raggiungibile dalla rete dati |

Per capire dove si rompe, inserisci **Anteprima rapida** subito dopo il
passaggio che sospetti: mostra cosa sta passando e ferma lì il comando.

---

## Tempi

Fra cinque e quindici secondi per uno scontrino da 65 righe, quasi tutti
spesi da Veryfi a leggere l'immagine. Home Assistant registra in meno di un
decimo di secondo.

---

## Cosa aspettarsi dalla qualità

Su uno scontrino reale da 65 righe, Veryfi ha estratto tutte le righe con
prezzi e sconti corretti, riconoscendo anche il prodotto a peso e le
quantità multiple. L'unico errore è stato un punto decimale perso — `3,49`
letto `349` — intercettato dal controllo di coerenza e corretto dalla
dashboard in dieci secondi.

Il sistema **non corregge automaticamente** errori del genere. Dividere per
cento un importo che sembra troppo grande è il tipo di euristica che un
giorno rovina un dato vero.