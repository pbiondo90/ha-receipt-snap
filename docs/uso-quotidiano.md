# Uso quotidiano

Come si usa il sistema una volta installato. Se cerchi il perché delle
scelte, sta in [architettura.md](architettura.md).

---

## Il giro normale

Fotografi lo scontrino, lanci il Comando Rapido, e dopo una decina di secondi
ricevi una notifica con negozio e importo. Nella maggior parte dei casi finisce
lì: i totali mensili si aggiornano da soli.

Ogni tanto arriva uno scontrino **da verificare**. Lo vedi nella dashboard,
lo correggi in trenta secondi, e la segnalazione sparisce da sola.

---

## La dashboard

Due viste.

### Riepilogo

Il totale del mese in evidenza, il confronto col mese precedente in euro e
percentuale, la media giornaliera e la proiezione di fine mese.

Sotto, gli ultimi scontrini, la ripartizione per categoria e per supermercato,
e l'andamento dei dodici mesi con barre testuali.

La sezione **Da verificare** compare solo quando c'è qualcosa da verificare.

> La proiezione resta vuota nei primi tre giorni del mese: con così pochi
> dati produrrebbe un numero privo di senso.

### Scontrino

Qui si lavora. Il flusso tipico è di quattro tocchi.

1. **Scegli lo scontrino** dal menu in alto
2. **Guardi la tabella** degli articoli: il segno ✓ o ✗ dice se contano nella spesa
3. **Scegli l'articolo** dal secondo menu
4. **Premi Inverti inclusione**

Il totale si aggiorna nello stesso istante, insieme alla ripartizione per
categoria e al totale del mese.

Sotto ci sono i quattro campi di correzione — nome, prezzo, quantità,
categoria — che agiscono sempre sull'articolo selezionato. E in fondo i due
pulsanti di eliminazione, che chiedono conferma.

---

## Escludere invece di cancellare

È la distinzione più importante del sistema.

**Inverti inclusione** toglie l'articolo dalla spesa alimentare ma lo lascia
nello scontrino. Il totale originale non cambia, il dato resta, e puoi sempre
rimetterlo dentro.

**Elimina articolo** lo cancella davvero.

Nel caso tipico — detersivo e pile dentro la spesa del supermercato — vuoi
sempre la prima. Uno scontrino da 44 € con 35 € di alimentari resta uno
scontrino da 44 €: cambia solo quanto di quella spesa conti come alimentare.

---

## Correggere un articolo

Selezionalo, poi usa il campo che serve.

| Campo | Note |
|---|---|
| **Nome articolo** | il nome leggibile. Correggerlo risolve la segnalazione «nome mancante» |
| **Prezzo articolo** | il prezzo finale della riga, già al netto degli sconti. Può essere negativo per resi |
| **Quantità articolo** | ammette decimali: `0.352` per un prodotto a peso |
| **Categoria articolo** | correggerla risolve la segnalazione «categoria non riconosciuta» |

Sopra ai campi, la **descrizione originale** dello scontrino, in sola
lettura. Non è modificabile per scelta: è il dato di partenza e alimenta il
riconoscimento dei duplicati.

---

## Gli scontrini da verificare

Il sensore *Da verificare* dice quanti sono, e la card nel Riepilogo elenca i
motivi. Non è un errore: è il sistema che ti dice dove guardare.

### La somma non torna

`total_mismatch` — la somma degli articoli differisce dal totale stampato di
più di cinque centesimi.

Quasi sempre significa che l'estrazione ha letto male un prezzo o ha perso
una riga. Apri il dettaglio: lo **scarto** ti dice di quanto, e spesso basta
per capire quale riga guardare. Un prezzo letto `349,00` invece di `3,49`
produce uno scarto di 345,51 €, difficile da mancare.

Correggi il prezzo e la segnalazione sparisce.

Se invece mancano righe, aggiungile… non si può: il sistema non permette di
aggiungere articoli a uno scontrino esistente. In quel caso conviene
eliminare lo scontrino e rifotografarlo meglio.

### Possibile duplicato

`possible_duplicate` — c'è un altro scontrino con stessa data, stesso
negozio, stesso totale e stesso numero di articoli, ma contenuto diverso.

Due possibilità. **È davvero lo stesso scontrino**, letto due volte in modo
leggermente diverso: elimina quello meno accurato. **Sono due spese distinte**
che per caso coincidono: usa `spesa.aggiorna_scontrino` con
`dismiss_possible_duplicate: true`.

Il collegamento all'altro scontrino resta registrato come promemoria, ma la
segnalazione sparisce. Se in seguito modifichi data, negozio, totale o gli
articoli, la verifica viene annullata e la segnalazione può ricomparire:
quello che avevi controllato non è più la situazione attuale.

### Categoria non riconosciuta

`unknown_category` — la fonte ha mandato una categoria che non conosciamo, e
l'articolo è finito in `Altro`.

Scegli la categoria giusta dal menu. Se capita spesso con le stesse parole,
conviene aggiungerle alle parole chiave in `veryfi.py`.

### Nome mancante

`missing_normalized_name` — l'articolo non aveva un nome leggibile e ne è
stato ricavato uno dalla descrizione.

Scrivi il nome che preferisci nel campo Nome articolo.

### Prezzo negativo

`negative_price` — una riga ha prezzo negativo senza uno sconto dichiarato.

Di solito è legittimo: un reso, un arrotondamento, uno sconto globale.
Controlla che corrisponda allo scontrino e, se va bene, non serve fare nulla:
la segnalazione resta finché il dato resta così.

### Quantità e prezzo non tornano

`item_total_mismatch` — quantità × prezzo unitario meno lo sconto non dà il
prezzo di riga.

Uno dei tre valori è sbagliato. Guarda lo scontrino e correggi quello che
non torna.

---

## Le sette azioni

Si chiamano da **Strumenti per sviluppatori → Azioni**, o da automazioni.
Nell'uso normale non servono: la dashboard copre tutto.

### `spesa.aggiorna_articolo`

Modifica uno o più campi di un articolo.

```yaml
action: spesa.aggiorna_articolo
data:
  receipt_id: 20260921-conad-104522-a81f
  item_id: "02"
  included: false
  category: Casa
```

Campi ammessi: `included`, `name`, `category`, `price`, `quantity`,
`unit_price`, `discount`, `notes`, `product_id`.

`raw_name` e `id` sono immutabili e vengono rifiutati con un messaggio
esplicito.

> Omettere un campo significa «non modificarlo». Per **rimuovere** un valore
> già presente — per esempio uno sconto registrato per errore — serve la
> modalità YAML e `discount: null`.

### `spesa.aggiorna_scontrino`

```yaml
action: spesa.aggiorna_scontrino
data:
  receipt_id: 20260921-conad-104522-a81f
  date: "2026-09-01"
```

Cambiando la data in un mese diverso, lo scontrino viene **spostato** nel
file di quel mese. Entrambi i file vengono scritti nella stessa transazione,
quindi non esiste un istante in cui lo scontrino manchi da entrambi.

`needs_review` non è modificabile: è derivato. Per segnalare uno scontrino da
controllare usa `manual_review: true`, che aggiunge un motivo senza toccare
quelli automatici.

### `spesa.elimina_articolo` e `spesa.elimina_scontrino`

Richiedono `conferma: true`. Dalla dashboard c'è un dialogo di conferma.

Non si può eliminare l'ultimo articolo di uno scontrino: uno scontrino senza
articoli non è uno stato valido. Elimina l'intero scontrino.

Quando elimini l'ultimo scontrino di un mese, il file resta con
`"receipts": []`. È lo svuotamento intenzionale, distinto dall'assenza del
file, che significherebbe una cancellazione accidentale.

### `spesa.ricalcola`

Rilegge tutti i file dal disco, ricalcola i totali e ricontrolla la coerenza.

Serve dopo aver corretto un file a mano in `/config/spesa`, o dopo aver
ripristinato dati da un backup. Se i problemi segnalati sono stati risolti,
le notifiche spariscono.

**Non** rimuove un blocco dell'archivio: per quello serve
`spesa.sblocca_archivio`.

### `spesa.rigenera_token`

Genera un nuovo token e revoca il precedente. Richiede `conferma: true`.

L'ordine è deliberato: prima viene mostrata la notifica col nuovo valore,
solo dopo il token diventa attivo. Se la notifica non può essere creata,
**nulla cambia** e il token precedente resta valido.

Dopo la rigenerazione il Comando Rapido va aggiornato, altrimenti smette di
funzionare con un 401.

### `spesa.sblocca_archivio`

Vedi [risoluzione-problemi.md](risoluzione-problemi.md). Nell'uso normale non
lo toccherai mai.

---

## I sensori

### Mese corrente

Lo stato è il totale in euro. Negli attributi:

`media_giornaliera` · `proiezione_fine_mese` · `differenza_euro` ·
`differenza_percentuale` · `per_categoria` · `per_supermercato` ·
`per_giorno` · `storico_mensile` (dodici mesi) · `mesi_esclusi`

### Da verificare

Lo stato è il conteggio. Negli attributi, oltre alla coda, anche lo **stato
di salute dell'archivio**: `mesi_esclusi`, `archivio_bloccato`,
`motivo_blocco`, `violazioni`.

È il sensore da tenere d'occhio se qualcosa va storto.

### Dettaglio scontrino

Segue il select. Lo stato è il `receipt_id`, gli attributi contengono lo
scontrino completo con tutti i suoi articoli, più due valori calcolati:
`scarto` (somma articoli meno totale) e `esclusi` (quanto hai tolto).

---

## Automazioni utili

Il sistema non ne include nessuna, ma i sensori le rendono facili.

**Avviso quando qualcosa va verificato**

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.spesa_alimentare_da_verificare
    above: 0
actions:
  - action: notify.mobile_app_iphone
    data:
      message: >
        {{ states('sensor.spesa_alimentare_da_verificare') }} scontrini
        da verificare
```

**Riepilogo a fine mese**

```yaml
triggers:
  - trigger: time
    at: "20:00:00"
conditions:
  - "{{ now().day == (now().replace(day=28) + timedelta(days=4)).replace(day=1).day - 1 }}"
actions:
  - action: notify.mobile_app_iphone
    data:
      message: >
        Spesa di {{ state_attr('sensor.spesa_alimentare_mese_corrente','etichetta') }}:
        {{ states('sensor.spesa_alimentare_mese_corrente') }} €
```

**Avviso se l'archivio si blocca**

```yaml
triggers:
  - trigger: state
    entity_id: sensor.spesa_alimentare_da_verificare
    attribute: archivio_bloccato
    to: true
actions:
  - action: notify.mobile_app_iphone
    data:
      message: "Archivio spesa in sola lettura, controlla le notifiche"
```

---

## Cose da sapere

**Il token si vede una volta sola.** Se lo perdi, `spesa.rigenera_token`.

**Gli scontrini non si possono aggiungere a mano.** Non esiste un servizio
per creare uno scontrino da zero: arrivano solo dall'endpoint. Se ti serve
inserirne uno manualmente, puoi comporre il JSON e inviarlo con `curl`.

**La selezione della dashboard non è persistita.** Dopo un riavvio riparte
dallo scontrino più recente. È il comportamento atteso aprendo la pagina.

**I mesi vuoti restano.** Un file con `receipts: []` significa che hai
svuotato quel mese di proposito.