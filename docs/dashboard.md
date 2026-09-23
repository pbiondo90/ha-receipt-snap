# Dashboard

L'integrazione non crea una dashboard: le tredici entità funzionano con
qualunque card. Questo documento descrive una configurazione che copre l'uso
reale, usando solo card native più [Mushroom](https://github.com/piitaya/lovelace-mushroom).

---

## Struttura

Due viste in formato sezioni.

**Riepilogo** — totale del mese, confronti, ripartizioni, andamento.
**Scontrino** — selezione, dettaglio, correzione, eliminazione.

---

## Prima di iniziare

Gli `entity_id` dipendono dal nome del dispositivo. Con il nome predefinito
sono `sensor.spesa_alimentare_mese_corrente` e simili. Verifica i tuoi in
Strumenti per sviluppatori → Stati, cercando `spesa`.

Se hai rinominato il dispositivo, adatta i riferimenti.

---

## I due script

Servono a eliminare senza digitare identificativi: leggono la selezione
corrente dagli attributi dei select.

**Spesa - elimina scontrino selezionato**

```yaml
alias: Spesa - elimina scontrino selezionato
icon: mdi:trash-can-outline
mode: single
sequence:
  - variables:
      target_receipt: "{{ state_attr('select.spesa_alimentare_scontrino','receipt_id') }}"
  - if:
      - condition: template
        value_template: >
          {{ target_receipt is none or target_receipt | string in
             ['', 'None', 'unknown', 'unavailable'] }}
    then:
      - stop: >
          Nessuno scontrino selezionato. Scegline uno dal menu in alto nella
          vista Scontrino, poi riprova.
        error: true
  - action: spesa.elimina_scontrino
    data:
      receipt_id: "{{ target_receipt }}"
      conferma: true
```

**Spesa - elimina articolo selezionato**

```yaml
alias: Spesa - elimina articolo selezionato
icon: mdi:delete-outline
mode: single
sequence:
  - variables:
      target_receipt: "{{ state_attr('select.spesa_alimentare_articolo','scontrino') }}"
      target_item: "{{ state_attr('select.spesa_alimentare_articolo','item_id') }}"
  - if:
      - condition: template
        value_template: >
          {{ target_receipt is none or target_item is none
             or target_receipt | string in ['', 'None', 'unknown', 'unavailable']
             or target_item | string in ['', 'None', 'unknown', 'unavailable'] }}
    then:
      - stop: >
          Nessun articolo selezionato. Scegli uno scontrino e poi un articolo
          nella vista Scontrino, quindi riprova.
        error: true
  - action: spesa.elimina_articolo
    data:
      receipt_id: "{{ target_receipt }}"
      item_id: "{{ target_item }}"
      conferma: true
```

La guardia iniziale serve a dare un messaggio comprensibile: senza,
l'azione risponderebbe *«string value is None at receipt_id»*.

---

## Vista Riepilogo

### Totale del mese

```yaml
type: markdown
content: |
  {% set s = 'sensor.spesa_alimentare_mese_corrente' %}
  # {{ '%.2f'|format(states(s)|float(0)) }} €
  ### {{ state_attr(s,'etichetta') or '' }}
  {{ state_attr(s,'scontrini') or 0 }} scontrini registrati
```

### Confronto

```yaml
type: markdown
content: |
  {% set s = 'sensor.spesa_alimentare_mese_corrente' %}
  {% set d = state_attr(s,'differenza_euro') %}
  {% set p = state_attr(s,'differenza_percentuale') %}
  {% set m = state_attr(s,'media_giornaliera') %}
  {% set pr = state_attr(s,'proiezione_fine_mese') %}
  | | |
  |---|---:|
  | Mese precedente | {{ '%.2f'|format(state_attr(s,'mese_precedente')|float(0)) }} € |
  | Differenza | {% if d is not none %}{{ '%+.2f'|format(d) }} €{% if p is not none %} ({{ '%+.1f'|format(p) }} %){% endif %}{% else %}—{% endif %} |
  | Media giornaliera | {% if m is not none %}{{ '%.2f'|format(m) }} €{% else %}—{% endif %} |
  | Proiezione fine mese | {% if pr is not none %}{{ '%.2f'|format(pr) }} €{% else %}non ancora calcolabile{% endif %} |
  | Oggi | {{ '%.2f'|format(states('sensor.spesa_alimentare_oggi')|float(0)) }} € |
```

### Da verificare

Compare solo quando serve, grazie alla condizione di visibilità.

```yaml
type: grid
visibility:
  - condition: numeric_state
    entity: sensor.spesa_alimentare_da_verificare
    above: 0
cards:
  - type: markdown
    content: |
      {% set v = state_attr('sensor.spesa_alimentare_da_verificare','scontrini') or [] %}
      {% for r in v %}**{{ r.date[8:10] }}/{{ r.date[5:7] }} · {{ r.store }} · {{ '%.2f'|format(r.included_total) }} €**
      {% for m in r.motivi %}- {{ m }}
      {% endfor %}
      {% endfor %}
      {% set b = state_attr('sensor.spesa_alimentare_da_verificare','motivo_blocco') %}{% if b %}
      ---
      **Archivio in sola lettura:** {{ b }}{% endif %}
```

### Ultimi scontrini

```yaml
type: markdown
content: |
  {% set s = state_attr('sensor.spesa_alimentare_ultimi_scontrini','scontrini') or [] %}
  {% if s %}| Data | Negozio | Spesa |
  |---|---|---:|
  {% for r in s[:12] %}| {{ r.date[8:10] }}/{{ r.date[5:7] }} | {{ r.store }}{% if r.needs_review %} ⚠{% endif %} | {{ '%.2f'|format(r.included_total) }} € |
  {% endfor %}{% else %}Nessuno scontrino registrato.{% endif %}
```

### Ripartizione

```yaml
type: markdown
content: |
  {% set s = 'sensor.spesa_alimentare_mese_corrente' %}
  {% set c = state_attr(s,'per_categoria') or {} %}
  {% set n = state_attr(s,'per_supermercato') or {} %}
  {% if c %}**Per categoria**

  | | |
  |---|---:|
  {% for k, v in c.items() %}| {{ k }} | {{ '%.2f'|format(v) }} € |
  {% endfor %}
  {% endif %}
  {% if n %}
  **Per supermercato**

  | | |
  |---|---:|
  {% for k, v in n.items() %}| {{ k }} | {{ '%.2f'|format(v) }} € |
  {% endfor %}
  {% endif %}
  {% if not c and not n %}Nessun dato per questo mese.{% endif %}
```

### Andamento 12 mesi

Barre testuali dall'attributo `storico_mensile`, indipendente dal recorder.

```yaml
type: markdown
content: |
  {% set h = state_attr('sensor.spesa_alimentare_mese_corrente','storico_mensile') or [] %}
  {% set tot = h | map(attribute='totale') | list %}
  {% set mx = tot | max if tot else 0 %}
  {% if mx > 0 %}```
  {% for m in h %}{{ '%-15s'|format(m.etichetta) }} {{ '█' * ((m.totale / mx * 18) | round(0) | int) }}{{ ' ' }}{{ '%8.2f'|format(m.totale) }}
  {% endfor %}```
  {% else %}Nessun dato storico.{% endif %}
```

---

## Vista Scontrino

### Selettore

```yaml
type: tile
entity: select.spesa_alimentare_scontrino
hide_state: true
features:
  - type: select-options
```

La funzionalità `select-options` mette il menu direttamente sulla card, senza
aprire un dialogo.

### Dettaglio

```yaml
type: markdown
content: |
  {% set d = 'sensor.spesa_alimentare_dettaglio_scontrino' %}
  {% if state_attr(d,'presente') %}{% set dt = state_attr(d,'date') %}
  ### {{ state_attr(d,'store') }} — {{ dt[8:10] }}/{{ dt[5:7] }}/{{ dt[0:4] }}{% if state_attr(d,'time') %} · {{ state_attr(d,'time') }}{% endif %}

  | | |
  |---|---:|
  | Totale scontrino | {{ '%.2f'|format(state_attr(d,'receipt_total')) }} € |
  | Somma articoli | {{ '%.2f'|format(state_attr(d,'items_total')) }} € |
  | **Spesa alimentare** | **{{ '%.2f'|format(state_attr(d,'included_total')) }} €** |
  | Esclusi | {{ '%.2f'|format(state_attr(d,'esclusi')) }} € |
  {% if state_attr(d,'scarto')|abs > 0.05 %}| Scarto | {{ '%+.2f'|format(state_attr(d,'scarto')) }} € |
  {% endif %}
  {% if state_attr(d,'needs_review') %}
  **Da verificare:**
  {% for m in state_attr(d,'review_labels') %}- {{ m }}
  {% endfor %}{% endif %}
  {% set dup = state_attr(d,'possible_duplicate_of') or [] %}{% if dup %}
  Simile a: {{ dup | join(', ') }}{% endif %}
  {% else %}Nessuno scontrino selezionato.{% endif %}
```

### Articoli

```yaml
type: markdown
content: |
  {% set d = 'sensor.spesa_alimentare_dettaglio_scontrino' %}
  {% set items = state_attr(d,'items') or [] %}
  {% set sel = state_attr(d,'articolo_selezionato') %}
  {% if items %}| | Id | Articolo | Q.tà | Prezzo | Categoria |
  |---|---|---|---:|---:|---|
  {% for i in items %}| {{ '✓' if i.included else '✗' }} | {{ '**' ~ i.id ~ '**' if i.id == sel else i.id }} | {{ i.name }} | {{ '%g'|format(i.quantity) }} | {{ '%.2f'|format(i.price) }} € | {{ i.category }} |
  {% endfor %}
  L'articolo in grassetto è quello selezionato.{% else %}—{% endif %}
```

```yaml
type: tile
entity: select.spesa_alimentare_articolo
hide_state: true
features:
  - type: select-options
```

```yaml
type: tile
entity: button.spesa_alimentare_inverti_inclusione
hide_state: true
```

### Correzione

```yaml
type: markdown
content: |
  {% set a = 'select.spesa_alimentare_articolo' %}
  {% set r = state_attr(a,'raw_name') %}
  {% if r %}Descrizione originale sullo scontrino, non modificabile:

  `{{ r }}`{% else %}Nessun articolo selezionato.{% endif %}
```

```yaml
type: entities
entities:
  - entity: text.spesa_alimentare_nome_articolo
    name: Nome
  - entity: number.spesa_alimentare_prezzo_articolo
    name: Prezzo
  - entity: number.spesa_alimentare_quantita_articolo
    name: Quantità
  - entity: select.spesa_alimentare_categoria_articolo
    name: Categoria
```

### Eliminazione

```yaml
type: grid
columns: 2
square: false
cards:
  - type: button
    name: Elimina articolo
    icon: mdi:delete-outline
    show_state: false
    tap_action:
      action: perform-action
      perform_action: script.turn_on
      target:
        entity_id: script.spesa_elimina_articolo_selezionato
      confirmation:
        text: >
          Eliminare definitivamente l'articolo selezionato? Il dato originale
          non sarà più recuperabile dalla dashboard.
  - type: button
    name: Elimina scontrino
    icon: mdi:trash-can-outline
    show_state: false
    tap_action:
      action: perform-action
      perform_action: script.turn_on
      target:
        entity_id: script.spesa_elimina_scontrino_selezionato
      confirmation:
        text: Eliminare definitivamente lo scontrino selezionato e tutti i suoi articoli?
```

Il dialogo di conferma è la funzionalità nativa di Lovelace: la domanda
all'utente sta nell'interfaccia, non dentro lo script.

---

## Perché nessun grafico

I sensori monetari non hanno `state_class`, quindi non compaiono nei grafici
statistici di Home Assistant. È deliberato: sono aggregati correggibili
retroattivamente, e una semantica statistica produrrebbe long-term statistics
fuorvianti.

L'andamento nel tempo viene da `storico_mensile`, che copre dodici mesi ed è
indipendente dalla retention del recorder.

Se vuoi un grafico vero, ApexCharts può leggere quell'attributo — ma è una
dipendenza HACS in più che questo progetto non richiede.