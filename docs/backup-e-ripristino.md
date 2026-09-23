# Backup, ripristino e disinstallazione

---

## Cosa c'è da salvare

Tutto sta dentro `/config`, quindi **finisce nei normali backup di Home
Assistant** senza configurare nulla.

| Percorso | Contenuto | Perdere questo significa |
|---|---|---|
| `/config/spesa/` | l'archivio degli scontrini | perdere lo storico |
| `/config/custom_components/spesa/` | il codice dell'integrazione | reinstallare |
| `.storage/core.config_entries` | il token dell'endpoint | rigenerarlo |
| `.storage/lovelace.spesa_alimentare` | la dashboard | rifarla |
| `.storage/*.script*` | i due script di eliminazione | rifarli |

Solo il primo è insostituibile. Tutto il resto si ricrea in mezz'ora.

---

## I backup automatici

Home Assistant fa backup completi secondo la pianificazione in
**Impostazioni → Sistema → Backup**. Un backup completo include `/config`,
quindi include tutto l'elenco qui sopra.

Se usi l'add-on **Google Drive Backup** o simili, le copie finiscono anche
fuori casa: è la differenza fra sopravvivere a un file corrotto e
sopravvivere a un disco morto.

> Un backup che sta solo sulla stessa macchina non è un backup.

### Verificare

```bash
ha backups list
```

Controlla che ce ne sia uno recente e che includa `homeassistant`.

---

## La protezione interna

Indipendente dai backup di Home Assistant, l'integrazione tiene una rete di
sicurezza a breve termine.

**`.bak` per ogni mese** — a ogni scrittura, la versione precedente del file
viene copiata in `2026-09.json.bak`. Se il file principale diventa
illeggibile, il sistema carica il backup da solo e mette quello rotto in
quarantena.

Copre l'ultima scrittura, non lo storico: due scritture dopo un errore, il
`.bak` contiene già il dato sbagliato. Per andare più indietro servono i
backup di Home Assistant.

**File in quarantena** — mai cancellati, solo rinominati:

| Suffisso | Cos'è |
|---|---|
| `.json.corrupt-<data>` | file illeggibile, sostituito dal backup |
| `.json.bak.recovery-orphan-<data>` | backup di un mese tornato inesistente dopo un rollback |
| `.transaction.json.journal.orphan-<data>` | journal non interpretabile |

Puoi cancellarli quando sei sicuro che non servano. Il sistema non lo fa mai
da solo.

---

## Backup manuale rapido

Prima di un intervento rischioso:

```bash
cp -r /config/spesa /config/spesa-backup-$(date +%Y%m%d-%H%M)
```

Due secondi, e hai una copia esatta.

Per portarla fuori:

```bash
tar czf /config/spesa-$(date +%Y%m%d).tar.gz -C /config spesa
```

Il file lo scarichi poi da File editor o via SSH.

---

## Ripristinare i dati

### Un singolo mese

Il caso più comune: un mese si è corrotto e il `.bak` non basta.

```bash
# 1. Metti da parte quello corrente, non cancellarlo
mv /config/spesa/2026-09.json /config/spesa/2026-09.json.prima-del-ripristino

# 2. Copia la versione buona
cp /percorso/del/backup/2026-09.json /config/spesa/2026-09.json

# 3. Verifica che sia JSON valido
python3 -c "import json; json.load(open('/config/spesa/2026-09.json')); print('OK')"
```

Poi chiama **`spesa.ricalcola`**. Nessun riavvio necessario: rilegge tutto
dal disco e ricostruisce totali e statistiche.

### Tutto l'archivio

```bash
mv /config/spesa /config/spesa-prima-del-ripristino
mkdir -p /config/spesa
cp /percorso/del/backup/*.json /config/spesa/
```

Poi `spesa.ricalcola`.

### Da un backup di Home Assistant

Due strade.

**Ripristino parziale dalla UI** — Impostazioni → Sistema → Backup, scegli il
backup, spunta solo *Home Assistant*. Ripristina l'intera `/config`, quindi
anche automazioni e dashboard modificate dopo quel backup. Comodo ma
grossolano.

**Estrazione manuale** — più chirurgica, tocca solo i file che vuoi.

```bash
cd /tmp
tar xf /backup/abc12345.tar
tar xf homeassistant.tar.gz
ls data/spesa/
cp data/spesa/2026-09.json /config/spesa/
```

Se il backup è protetto da password, va decrittato prima: la UI lo fa durante
il download.

Poi `spesa.ricalcola`.

---

## Cosa NON serve ripristinare

**Il `.bak`** — viene ricreato alla prima scrittura.

**Il journal** — se presente in un backup, al primo avvio provoca un rollback
allo stato che aveva quando fu creato. Non è un guasto, ma se stai
ripristinando di proposito, cancellalo prima:

```bash
rm -f /config/spesa/.transaction.json
```

**Il marker di blocco** — stesso discorso: se `.transaction.blocked` finisce
in un archivio ripristinato, l'archivio parte bloccato. Cancellalo se sei
sicuro dei dati.

```bash
rm -f /config/spesa/.transaction.blocked
```

---

## Migrare su un'altra installazione

```bash
# Sulla vecchia
tar czf /config/spesa-migrazione.tar.gz -C /config spesa

# Sulla nuova, dopo aver installato l'integrazione
tar xzf spesa-migrazione.tar.gz -C /config
```

Poi `spesa.ricalcola`.

Il **token non si migra**: la nuova installazione ne genera uno proprio alla
creazione della config entry. Aggiorna il Comando Rapido.

Dashboard e script vanno rifatti, oppure copiati dai rispettivi file in
`.storage` — ma copiare file di `.storage` a mano è delicato e conviene solo
se sai cosa stai facendo.

---

## Disinstallazione completa

In ordine. I passaggi 1 e 2 sono reversibili, dal 3 in poi no.

### 1. Rimuovi la config entry

Impostazioni → Dispositivi e servizi → Spesa → menu ⋮ → **Elimina**.

Sparisce il dispositivo, spariscono le tredici entità, il token viene
eliminato. **I dati in `/config/spesa` restano intatti.**

### 2. Rimuovi dashboard e script

Impostazioni → Dashboard → Spesa → Elimina.

Impostazioni → Automazioni e scene → Script: elimina
`Spesa - elimina articolo selezionato` e
`Spesa - elimina scontrino selezionato`.

### 3. Salva i dati, se li vuoi

```bash
tar czf /config/spesa-archivio-finale.tar.gz -C /config spesa
```

Scaricalo prima di procedere.

### 4. Rimuovi il codice

Da HACS, se l'avevi installata così. Altrimenti:

```bash
rm -rf /config/custom_components/spesa
```

### 5. Rimuovi i dati

```bash
rm -rf /config/spesa
```

**Irreversibile.** Tutto lo storico sparisce.

### 6. Riavvia

Impostazioni → Sistema → Riavvia.

### Cosa resta comunque

La route HTTP `/api/spesa/receipt` **resta registrata fino al riavvio**: Home
Assistant non permette di rimuovere una route. Finché non riavvii risponde
`503`, mai `404`. Dopo il riavvio sparisce.

Nel database del recorder restano gli stati storici delle entità, che
decadranno da soli con la normale retention.

I backup che hai già fatto contengono ancora tutto: se vuoi cancellare
davvero ogni traccia, vanno eliminati anche quelli.

---

## In sintesi

**Cosa proteggere davvero** — `/config/spesa`. È l'unica cosa insostituibile.

**Come** — i backup automatici di Home Assistant, purché ne esista una copia
fuori dalla macchina.

**Come tornare indietro** — copia i file, chiama `spesa.ricalcola`. Nessun
riavvio.

**Prima di un intervento rischioso** — `cp -r /config/spesa /config/spesa-backup`.
Costa due secondi.