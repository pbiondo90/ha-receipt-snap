"""Costanti condivise dell'integrazione Spesa alimentare."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "spesa"
PLATFORMS: Final = ["sensor", "select", "number", "text", "button"]

# --------------------------------------------------------------------------- #
# Config entry
# --------------------------------------------------------------------------- #
CONF_TOKEN: Final = "token"

# Metadata INTERNO della config entry, non un'opzione configurabile:
# ricorda se il token iniziale e' gia' stato mostrato all'utente.
# Assente == False, cosi' il config flow non deve inizializzarlo.
CONF_INITIAL_TOKEN_SHOWN: Final = "initial_token_shown"

TOKEN_BYTES: Final = 32          # secrets.token_urlsafe(32) -> 43 caratteri
TOKEN_HEADER: Final = "X-Spesa-Token"

# Chiavi in hass.data per le view singleton: vivono oltre la config entry,
# perche' una route registrata non e' rimovibile.
VIEW_KEY: Final = f"{DOMAIN}_view"
VIEW_KEY_VERYFI: Final = f"{DOMAIN}_view_veryfi"

# --------------------------------------------------------------------------- #
# Persistenza
# --------------------------------------------------------------------------- #
DATA_DIR_NAME: Final = "spesa"   # -> /config/spesa
FILE_EXT: Final = ".json"
BAK_EXT: Final = ".json.bak"
TMP_EXT: Final = ".json.tmp"
CORRUPT_EXT: Final = ".json.corrupt"
BAK_ORPHAN_EXT: Final = ".recovery-orphan"

SCHEMA_VERSION: Final = 1
SUPPORTED_SCHEMA_VERSIONS: Final = frozenset({1})

# --------------------------------------------------------------------------- #
# Transaction journal (WAL) per le transazioni multi-mese
# --------------------------------------------------------------------------- #
JOURNAL_NAME: Final = ".transaction.json"
JOURNAL_VERSION: Final = 1
JOURNAL_ORPHAN_EXT: Final = ".journal.orphan"

BLOCKED_MARKER_NAME: Final = ".transaction.blocked"
BLOCKED_MARKER_VERSION: Final = 1

# --------------------------------------------------------------------------- #
# Endpoint HTTP
# --------------------------------------------------------------------------- #
API_PATH: Final = "/api/spesa/receipt"
MAX_BODY_BYTES: Final = 256 * 1024
BODY_CHUNK_SIZE: Final = 64 * 1024
BODY_READ_TIMEOUT_S: Final = 30
AUTH_LOG_THROTTLE_S: Final = 60

ERR_UNAUTHORIZED: Final = "unauthorized"
ERR_PAYLOAD_TOO_LARGE: Final = "payload_too_large"
ERR_REQUEST_TIMEOUT: Final = "request_timeout"
ERR_INVALID_JSON: Final = "invalid_json"
ERR_INVALID_PAYLOAD: Final = "invalid_payload"
ERR_MISSING_SCHEMA: Final = "missing_schema_version"
ERR_UNSUPPORTED_SCHEMA: Final = "unsupported_schema_version"
ERR_DUPLICATE: Final = "duplicate_receipt"
ERR_MONTH_DEGRADED: Final = "month_degraded"
ERR_ARCHIVE_LOCKED: Final = "archive_locked"
ERR_INTEGRATION_UNAVAILABLE: Final = "integration_unavailable"
ERR_INTERNAL: Final = "internal_error"

# --------------------------------------------------------------------------- #
# Endpoint Veryfi
#
# Accetta la risposta di Veryfi cosi' com'e'. La trasformazione avviene in
# veryfi.py e il risultato passa dalla STESSA validazione dell'endpoint
# principale: una sola definizione di dato valido.
# --------------------------------------------------------------------------- #
API_PATH_VERYFI: Final = "/api/spesa/receipt/veryfi"

# Il JSON di Veryfi contiene ocr_text e una cinquantina di metadati: molto piu'
# grande del payload compatto. Su uno scontrino da 65 righe sono circa 60 KB.
MAX_BODY_BYTES_VERYFI: Final = 2 * 1024 * 1024

ERR_INVALID_VERYFI: Final = "invalid_veryfi_payload"

# --------------------------------------------------------------------------- #
# Validazione
# --------------------------------------------------------------------------- #
TOTAL_TOLERANCE: Final = 0.05        # euro, somma articoli vs totale scontrino
ITEM_TOTAL_TOLERANCE: Final = 0.02   # euro, quantity * unit_price - discount vs price

MAX_ITEMS: Final = 200
MAX_STR_LEN: Final = 200
MAX_STORE_LEN: Final = 64
MAX_NOTES_LEN: Final = 500

MAX_RECEIPT_ID_LEN: Final = 64
MIN_RECEIPT_ID_LEN: Final = 6
RECEIPT_ID_PATTERN: Final = r"^[A-Za-z0-9._:-]+$"

MIN_ITEM_ID_LEN: Final = 1
MAX_ITEM_ID_LEN: Final = 16
ITEM_ID_PATTERN: Final = r"^[A-Za-z0-9._:-]+$"

MIN_AMOUNT: Final = -10_000.0
MAX_AMOUNT: Final = 10_000.0
MAX_QUANTITY: Final = 1_000.0

MIN_YEAR: Final = 2000
FUTURE_TOLERANCE_DAYS: Final = 1

# --------------------------------------------------------------------------- #
# Categorie (estendibili: basta aggiungere una voce)
#
# ATTENZIONE: aggiungere una categoria e' sicuro, toglierne o rinominarne una
# NO. In rilettura check_canonical_category rifiuta qualunque categoria fuori
# da questo elenco, quindi un file mensile che contiene articoli con una
# categoria non piu' prevista diventa illeggibile e il mese finisce in stato
# degradato. Prima di rimuovere una voce, correggi gli articoli che la usano.
# --------------------------------------------------------------------------- #

# Ripiego strutturale, non una categoria come le altre: e' il valore assegnato
# quando il client manda una categoria che non riconosciamo. Attiva il flag
# category_was_unknown, quindi l'articolo finisce fra quelli da verificare.
CATEGORY_FALLBACK: Final = "Altro"

CATEGORIES: Final = (
    "Alimentari",
    "Bevande",
    "Casa",
    "Igiene personale",
    "Animali",
    "Farmacia",
    "Altro",
)

# Categorie considerate alimentari. I totali NON dipendono da questa lista:
# usano il flag `included`. Serve solo a eventuali statistiche future.
FOOD_CATEGORIES: Final = ("Alimentari", "Bevande")

# Categorie che di default arrivano con included=False se il client non lo dice.
# DELIBERATAMENTE VUOTA: ogni articolo nasce included=True salvo valore
# esplicito nel payload. L'esclusione e' una decisione manuale dell'utente.
DEFAULT_EXCLUDED_CATEGORIES: Final = ()

# --------------------------------------------------------------------------- #
# Default degli stati interni
# --------------------------------------------------------------------------- #
DEFAULT_MANUAL_REVIEW: Final = False
DEFAULT_DUPLICATE_DISMISSED: Final = False
DEFAULT_NAME_WAS_MISSING: Final = False
DEFAULT_CATEGORY_WAS_UNKNOWN: Final = False

# --------------------------------------------------------------------------- #
# Supermercati: normalizzazione della grafia, non una lista chiusa.
# Un negozio non presente qui viene accettato comunque.
# --------------------------------------------------------------------------- #
STORE_ALIASES: Final = {
    "conad": "Conad",
    "conad city": "Conad",
    "conad superstore": "Conad",
    "famila": "Famila",
    "famila superstore": "Famila",
    "eurospin": "Eurospin",
    "lidl": "Lidl",
    "coop": "Coop",
    "ipercoop": "Coop",
    "deco": "Decò",
    "deco'": "Decò",
    "decò": "Decò",
    "carrefour": "Carrefour",
    "carrefour express": "Carrefour",
    "carrefour market": "Carrefour",
    "md": "MD",
    "todis": "Todis",
    "esselunga": "Esselunga",
    "penny": "Penny Market",
    "penny market": "Penny Market",
    "crai": "Crai",
    "sisa": "Sisa",
    "despar": "Despar",
    "eurospar": "Despar",
    "interspar": "Despar",
}

# --------------------------------------------------------------------------- #
# Motivi di verifica (review_reasons) - sempre calcolati da HA
# --------------------------------------------------------------------------- #
REVIEW_TOTAL_MISMATCH: Final = "total_mismatch"
REVIEW_POSSIBLE_DUPLICATE: Final = "possible_duplicate"
REVIEW_UNKNOWN_CATEGORY: Final = "unknown_category"
REVIEW_NEGATIVE_PRICE: Final = "negative_price"
REVIEW_ZERO_PRICE: Final = "zero_price"   # non generato: riservato a uso futuro
REVIEW_ITEM_MATH: Final = "item_total_mismatch"
REVIEW_NO_NAME: Final = "missing_normalized_name"
REVIEW_MANUAL: Final = "manual_flag"

REVIEW_LABELS: Final = {
    REVIEW_TOTAL_MISMATCH: "Somma articoli diversa dal totale scontrino",
    REVIEW_POSSIBLE_DUPLICATE: "Possibile duplicato di uno scontrino esistente",
    REVIEW_UNKNOWN_CATEGORY: "Categoria non riconosciuta, impostata su Altro",
    REVIEW_NEGATIVE_PRICE: "Articolo con prezzo negativo senza sconto dichiarato",
    REVIEW_ZERO_PRICE: "Articolo con prezzo zero",
    REVIEW_ITEM_MATH: "Quantita x prezzo unitario diverso dal prezzo riga",
    REVIEW_NO_NAME: "Articolo senza nome normalizzato",
    REVIEW_MANUAL: "Segnalato manualmente",
}

# --------------------------------------------------------------------------- #
# WHITELIST DEI CAMPI
# Tutto cio' che non compare qui viene scartato dal payload in ingresso e non
# entra in memoria in rilettura dal disco.
# --------------------------------------------------------------------------- #

# Campi dello scontrino accettati dal client e conservati come dato originale
RECEIPT_INPUT_FIELDS: Final = frozenset({
    "schema_version",
    "receipt_id",
    "date",
    "time",
    "store",
    "receipt_total",
    "notes",
    "items",
})

# Campi del client conservati SOLO per confronto diagnostico
CLIENT_REPORTED_FIELDS: Final = frozenset({
    "items_total",
    "included_total",
    "needs_review",
})

# Stato interno dello scontrino gestito da HA: non accettato dal payload HTTP,
# non derivato da calcolo, persistito insieme allo scontrino.
RECEIPT_INTERNAL_FIELDS: Final = frozenset({
    "manual_review",
    "possible_duplicate_dismissed",
})

# Campi gestiti internamente da HA e non scrivibili direttamente dal client
# o dai servizi. Comprendono valori derivati (totali, fingerprint, motivi di
# verifica), metadati (client_reported, possible_duplicate_of) e timestamp.
RECEIPT_DERIVED_FIELDS: Final = frozenset({
    "items_total",
    "included_total",
    "fingerprint",
    "fingerprint_weak",
    "needs_review",
    "review_reasons",
    "possible_duplicate_of",
    "created_at",
    "updated_at",
    "client_reported",
})

# Campi dello scontrino modificabili via spesa.aggiorna_scontrino.
# needs_review NON e' presente: e' derivato. Per chiedere una verifica manuale
# si agisce su manual_review, che entra in review_reasons come 'manual_flag'
# e concorre a needs_review senza cancellare i motivi automatici.
RECEIPT_EDITABLE_FIELDS: Final = frozenset({
    "date",
    "time",
    "store",
    "receipt_total",
    "notes",
    "manual_review",
    "possible_duplicate_dismissed",
})

# Nome pubblico del campo nel servizio; mappa su possible_duplicate_dismissed.
SERVICE_FIELD_DISMISS_DUPLICATE: Final = "dismiss_possible_duplicate"

# Campi dell'articolo accettati dal client
ITEM_INPUT_FIELDS: Final = frozenset({
    "id",
    "raw_name",
    "name",
    "quantity",
    "unit_price",
    "price",
    "category",
    "included",
    "discount",
    "original_price",
    "weight",
    "unit",
    "notes",
    "product_id",
})

# Stato interno dell'articolo gestito da HA: mai accettato dal payload, mai
# modificabile direttamente dai servizi, PERSISTITO.
# Serve a ricostruire i motivi di verifica identici dopo un riavvio.
ITEM_INTERNAL_FIELDS: Final = frozenset({
    "name_was_missing",
    "category_was_unknown",
})

# Campi dell'articolo modificabili via spesa.aggiorna_articolo.
# raw_name e id sono deliberatamente esclusi: immutabili.
ITEM_EDITABLE_FIELDS: Final = frozenset({
    "name",
    "quantity",
    "unit_price",
    "price",
    "category",
    "included",
    "discount",
    "notes",
    "product_id",
})

ITEM_IMMUTABLE_FIELDS: Final = frozenset({"id", "raw_name"})

# Allowlist positiva di serializzazione: store.py scrive ESATTAMENTE queste
# chiavi. Tutto il resto, chiavi di lavoro comprese, non raggiunge il disco.
ITEM_PERSISTED_FIELDS: Final = ITEM_INPUT_FIELDS | ITEM_INTERNAL_FIELDS

# schema_version e' protocollo, non dato dello scontrino: obbligatorio nel
# payload HTTP e nel root del file mensile, ma NON ripetuto dentro ogni
# receipt. Tutti gli scontrini di un file appartengono allo schema dichiarato
# dal contenitore.
RECEIPT_PERSISTED_FIELDS: Final = (
    (RECEIPT_INPUT_FIELDS - {"schema_version"})
    | RECEIPT_INTERNAL_FIELDS
    | RECEIPT_DERIVED_FIELDS
)

# Allowlist del contenitore del file mensile.
MONTH_PERSISTED_FIELDS: Final = frozenset({
    "schema_version",
    "month",
    "generated_at",
    "receipt_count",
    "receipts",
})

# Chiavi realmente transienti: vivono solo durante la validazione di un
# payload e servono a produrre messaggi d'errore leggibili.
ITEM_TRANSIENT_FIELDS: Final = frozenset({"_position", "_id_explicit"})

# --------------------------------------------------------------------------- #
# Servizi
# --------------------------------------------------------------------------- #
SERVICE_UPDATE_ITEM: Final = "aggiorna_articolo"
SERVICE_UPDATE_RECEIPT: Final = "aggiorna_scontrino"
SERVICE_DELETE_ITEM: Final = "elimina_articolo"
SERVICE_DELETE_RECEIPT: Final = "elimina_scontrino"
SERVICE_RECALC: Final = "ricalcola"
SERVICE_REGEN_TOKEN: Final = "rigenera_token"
SERVICE_UNBLOCK: Final = "sblocca_archivio"

# --------------------------------------------------------------------------- #
# Entita' / UI
# --------------------------------------------------------------------------- #
SIGNAL_UPDATED: Final = f"{DOMAIN}_updated"
SIGNAL_SELECTION: Final = f"{DOMAIN}_selection"

RECENT_RECEIPTS_LIMIT: Final = 20      # attributi di sensor.spesa_ultimi_scontrini
SELECTABLE_RECEIPTS_LIMIT: Final = 30  # opzioni di select.spesa_scontrino
TODAY_RECEIPTS_SHOWN: Final = 20       # limite di VISUALIZZAZIONE, non di conteggio
MONTHLY_HISTORY_MONTHS: Final = 12

SELECT_NONE: Final = "\u2014"          # em dash, segnaposto quando non c'e' nulla

NOTIF_TOKEN_ID: Final = f"{DOMAIN}_token"
NOTIF_CORRUPT_ID: Final = f"{DOMAIN}_corrupt"
NOTIF_SAVE_ERROR_ID: Final = f"{DOMAIN}_save_error"
NOTIF_RECOVERY_ID: Final = f"{DOMAIN}_recovery"
NOTIF_INVARIANT_ID: Final = f"{DOMAIN}_invariant"

CURRENCY: Final = "EUR"
