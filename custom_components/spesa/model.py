"""Modello dati, validazione e calcoli dell'integrazione Spesa alimentare.

Modulo PURO: nessuna dipendenza da Home Assistant, nessun I/O, nessuno stato
globale. Riceve strutture Python e restituisce strutture Python, cosi' e'
testabile in isolamento e la logica economica resta in un unico posto.

Separazione dei dati, rispettata ovunque nel file:

  INPUT     campi provenienti dallo scontrino/ChatGPT, whitelist rigida
  INTERNO   stato gestito da HA, mai accettato dal payload HTTP, persistito
  DERIVATO  sempre ricalcolato, mai letto dal payload ne' scrivibile dai servizi

Le funzioni check_* sono le INVARIANTI CONDIVISE: le usano sia l'ingresso dal
payload HTTP sia la rilettura dal disco, cosi' non esistono due definizioni
divergenti di "dato valido".
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date as date_cls, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Final

from .const import (
    CATEGORIES,
    CATEGORY_FALLBACK,
    CLIENT_REPORTED_FIELDS,
    DEFAULT_CATEGORY_WAS_UNKNOWN,
    DEFAULT_DUPLICATE_DISMISSED,
    DEFAULT_EXCLUDED_CATEGORIES,
    DEFAULT_MANUAL_REVIEW,
    DEFAULT_NAME_WAS_MISSING,
    FUTURE_TOLERANCE_DAYS,
    ITEM_EDITABLE_FIELDS,
    ITEM_ID_PATTERN,
    ITEM_IMMUTABLE_FIELDS,
    ITEM_INPUT_FIELDS,
    ITEM_TOTAL_TOLERANCE,
    MAX_AMOUNT,
    MAX_ITEM_ID_LEN,
    MAX_ITEMS,
    MAX_NOTES_LEN,
    MAX_QUANTITY,
    MAX_RECEIPT_ID_LEN,
    MAX_STORE_LEN,
    MAX_STR_LEN,
    MIN_AMOUNT,
    MIN_ITEM_ID_LEN,
    MIN_RECEIPT_ID_LEN,
    MIN_YEAR,
    RECEIPT_EDITABLE_FIELDS,
    RECEIPT_ID_PATTERN,
    RECEIPT_INPUT_FIELDS,
    REVIEW_ITEM_MATH,
    REVIEW_MANUAL,
    REVIEW_NEGATIVE_PRICE,
    REVIEW_NO_NAME,
    REVIEW_POSSIBLE_DUPLICATE,
    REVIEW_TOTAL_MISMATCH,
    REVIEW_UNKNOWN_CATEGORY,
    SCHEMA_VERSION,
    STORE_ALIASES,
    SUPPORTED_SCHEMA_VERSIONS,
    TOTAL_TOLERANCE,
)

# --------------------------------------------------------------------------- #
# Eccezioni
# --------------------------------------------------------------------------- #


class ValidationError(Exception):
    """Payload o modifica non valida. Porta l'elenco completo dei problemi."""

    def __init__(self, errors: list[str], code: str = "invalid_payload") -> None:
        self.errors = errors
        self.code = code
        super().__init__("; ".join(errors))


class MissingSchemaError(ValidationError):
    """schema_version assente: nessun default silenzioso, mai assunto come 1."""

    def __init__(self) -> None:
        super().__init__(
            [
                "schema_version: campo obbligatorio mancante, il payload deve "
                f"dichiarare esplicitamente la versione (attesa: {SCHEMA_VERSION})"
            ],
            code="missing_schema_version",
        )


class UnsupportedSchemaError(ValidationError):
    """schema_version non gestito: mai interpretato silenziosamente come 1."""

    def __init__(self, found: Any) -> None:
        super().__init__(
            [
                f"schema_version {found!r} non supportato "
                f"(supportate: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
            ],
            code="unsupported_schema_version",
        )


class _ParseError(Exception):
    """Errore interno di conversione, catturato e tradotto dai validatori."""


# --------------------------------------------------------------------------- #
# Denaro e quantita'
# --------------------------------------------------------------------------- #
#
# Regola: tutti i calcoli monetari avvengono in Decimal, mai in float.
# I float entrano solo al confine con JSON, e rientrano sempre via
# Decimal(str(x)) per non trascinarsi il rumore della rappresentazione binaria.

_CENT: Final = Decimal("0.01")
_MILLI: Final = Decimal("0.001")

_MONEY_CLEAN_RE = re.compile(r"[^\d,.\-+]")
_WS_RE = re.compile(r"\s+")
_STORE_CLEAN_RE = re.compile(r"[^\w\s'\u00e0-\u00ff-]", re.UNICODE)


def to_decimal(value: Any) -> Decimal:
    """Converte in Decimal un numero o una stringa monetaria.

    Accetta: int, float, Decimal, stringhe tipo '1,29', 'EUR 2.58', '-3,00'.
    Rifiuta: bool (sottoclasse di int in Python), None, NaN, infiniti,
    stringhe vuote o non numeriche.
    """
    if isinstance(value, bool):
        raise _ParseError("valore booleano dove e' atteso un numero")
    if value is None:
        raise _ParseError("valore nullo")

    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise _ParseError("valore numerico non finito")
        dec = Decimal(str(value))
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise _ParseError("stringa vuota")
        cleaned = _MONEY_CLEAN_RE.sub("", raw)
        # '1.234,56' -> '1234.56' ; '1,29' -> '1.29' ; '1234.56' invariato
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        if not cleaned or cleaned in {"-", "+", "."}:
            raise _ParseError(f"'{value}' non e' un numero")
        try:
            dec = Decimal(cleaned)
        except InvalidOperation as err:
            raise _ParseError(f"'{value}' non e' un numero") from err
    else:
        raise _ParseError(f"tipo {type(value).__name__} non convertibile in numero")

    if not dec.is_finite():
        raise _ParseError("valore numerico non finito")
    return dec


def money(value: Any) -> Decimal:
    """Arrotonda a 2 decimali con ROUND_HALF_UP (arrotondamento commerciale)."""
    return to_decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def qty(value: Any) -> Decimal:
    """Arrotonda a 3 decimali: copre i prodotti a peso (0,352 kg)."""
    return to_decimal(value).quantize(_MILLI, rounding=ROUND_HALF_UP)


def fmt_money(value: Decimal) -> str:
    """Formato deterministico per i fingerprint: sempre 2 decimali, punto."""
    return f"{value:.2f}"


def fmt_qty(value: Decimal) -> str:
    """Formato deterministico per i fingerprint: sempre 3 decimali, punto."""
    return f"{value:.3f}"


def dec_to_float(value: Decimal) -> float:
    """Confine verso JSON. Il valore e' gia' quantizzato dal chiamante."""
    return float(value)


# --------------------------------------------------------------------------- #
# Normalizzazioni
# --------------------------------------------------------------------------- #


def normalize_store(value: str) -> str:
    """Uniforma la grafia del supermercato senza chiudere la lista.

    'CONAD CITY' / 'conad  city' -> 'Conad'
    'DECO' / "deco'" / 'Deco'    -> 'Deco' canonico
    'Alimentari da mario'        -> 'Alimentari Da Mario' (accettato comunque)
    "iN's Mercato"               -> invariato: grafia mista, gia' voluta cosi'
    """
    collapsed = _WS_RE.sub(" ", value.strip())
    key = _STORE_CLEAN_RE.sub("", collapsed).strip().lower()
    key = _WS_RE.sub(" ", key)
    if key in STORE_ALIASES:
        return STORE_ALIASES[key]
    return collapsed.title() if collapsed.islower() or collapsed.isupper() else collapsed


def normalize_store_key(value: str) -> str:
    """Chiave di confronto per i fingerprint: negozio normalizzato, maiuscolo."""
    return normalize_store(value).upper()


def normalize_raw_name_key(value: str) -> str:
    """Chiave di confronto del raw_name nel fingerprint forte.

    Solo maiuscolo e spazi collassati: NON tocca il dato salvato, che resta
    immutabile e fedele allo scontrino. Serve a rendere il fingerprint stabile
    quando l'OCR restituisce spaziature leggermente diverse.
    """
    return _WS_RE.sub(" ", value.strip()).upper()


def normalize_category(value: Any) -> tuple[str, bool]:
    """Ritorna (categoria canonica, riconosciuta)."""
    if not isinstance(value, str) or not value.strip():
        return CATEGORY_FALLBACK, False
    needle = _WS_RE.sub(" ", value.strip()).casefold()
    for category in CATEGORIES:
        if category.casefold() == needle:
            return category, True
    return CATEGORY_FALLBACK, False


def month_key(date_str: str) -> str:
    """'2026-09-21' -> '2026-09'. Determina il file mensile di appartenenza."""
    return date_str[:7]


# --------------------------------------------------------------------------- #
# Invarianti condivise
#
# Unica definizione di "dato valido", usata sia in ingresso dal payload HTTP
# sia in rilettura dal disco. Sollevano ValidationError; il chiamante decide
# se accumularle (validate_payload) o tradurle (store.deserialize_*).
# --------------------------------------------------------------------------- #


def check_bool(value: Any, path: str, *, default: bool | None = None) -> bool:
    """Booleano JSON reale: true/false, non le stringhe 'true'/'false'.

    Rigoroso ovunque, sia in ingresso dal payload sia in rilettura dal disco.
    Il prompt di ChatGPT deve produrre booleani corretti e i servizi di Home
    Assistant li forniscono gia' tipizzati: accettare una stringa nasconderebbe
    un errore invece di segnalarlo.
    """
    if value is None:
        if default is None:
            raise ValidationError([f"{path}: valore booleano obbligatorio mancante"])
        return default
    if isinstance(value, bool):
        return value
    raise ValidationError(
        [
            f"{path}: atteso booleano JSON true/false, ricevuto "
            f"{type(value).__name__} {value!r}"
        ]
    )


def check_text(
    value: Any, path: str, *, max_len: int, required: bool, verbatim: bool = False
) -> str | None:
    """Testo validato. verbatim=True conserva il valore esatto (raw_name)."""
    if value is None:
        if required:
            raise ValidationError([f"{path}: campo obbligatorio mancante"])
        return None
    if not isinstance(value, str):
        raise ValidationError([f"{path}: atteso testo, ricevuto {type(value).__name__}"])
    if len(value) > max_len:
        raise ValidationError([f"{path}: supera {max_len} caratteri"])
    if verbatim:
        if required and not value.strip():
            raise ValidationError([f"{path}: non puo' essere vuoto"])
        return value
    cleaned = _WS_RE.sub(" ", value.strip())
    if not cleaned:
        if required:
            raise ValidationError([f"{path}: non puo' essere vuoto"])
        return None
    return cleaned


def check_amount(
    value: Any,
    path: str,
    *,
    required: bool,
    min_v: float = MIN_AMOUNT,
    max_v: float = MAX_AMOUNT,
) -> Decimal | None:
    """Importo monetario entro i limiti ammessi."""
    if value is None:
        if required:
            raise ValidationError([f"{path}: importo obbligatorio mancante"])
        return None
    if isinstance(value, bool):
        raise ValidationError([f"{path}: atteso importo, ricevuto booleano"])
    try:
        parsed = money(value)
    except _ParseError as err:
        raise ValidationError([f"{path}: {err}"]) from err
    if not (Decimal(str(min_v)) <= parsed <= Decimal(str(max_v))):
        raise ValidationError(
            [f"{path}: importo {parsed} fuori dall'intervallo [{min_v}, {max_v}]"]
        )
    return parsed


def check_quantity(value: Any, path: str, *, default: Decimal | None = None) -> Decimal | None:
    """Quantita' positiva entro il massimo ammesso."""
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValidationError([f"{path}: attesa quantita', ricevuto booleano"])
    try:
        parsed = qty(value)
    except _ParseError as err:
        raise ValidationError([f"{path}: {err}"]) from err
    if parsed <= 0:
        raise ValidationError([f"{path}: deve essere maggiore di zero (ricevuto {parsed})"])
    if parsed > Decimal(str(MAX_QUANTITY)):
        raise ValidationError([f"{path}: {parsed} supera il massimo {MAX_QUANTITY}"])
    return parsed


def check_receipt_id(value: Any, path: str = "receipt_id") -> str:
    """Identificativo dello scontrino: lunghezza e alfabeto ammessi."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError([f"{path}: identificativo obbligatorio"])
    candidate = value.strip()
    if not (MIN_RECEIPT_ID_LEN <= len(candidate) <= MAX_RECEIPT_ID_LEN):
        raise ValidationError(
            [
                f"{path}: lunghezza {len(candidate)} fuori da "
                f"[{MIN_RECEIPT_ID_LEN}, {MAX_RECEIPT_ID_LEN}]"
            ]
        )
    if not re.match(RECEIPT_ID_PATTERN, candidate):
        raise ValidationError([f"{path}: ammessi solo lettere, cifre e . _ : -"])
    return candidate


def check_item_id(value: Any, path: str) -> str:
    """Identificativo dell'articolo. Nessun troncamento silenzioso."""
    if isinstance(value, bool):
        raise ValidationError([f"{path}: atteso testo o numero, ricevuto booleano"])
    if not isinstance(value, (str, int)):
        raise ValidationError([f"{path}: atteso testo o numero, ricevuto {type(value).__name__}"])
    candidate = str(value).strip()
    if not candidate:
        raise ValidationError([f"{path}: non puo' essere vuoto"])
    if not (MIN_ITEM_ID_LEN <= len(candidate) <= MAX_ITEM_ID_LEN):
        raise ValidationError(
            [f"{path}: lunghezza {len(candidate)} fuori da [{MIN_ITEM_ID_LEN}, {MAX_ITEM_ID_LEN}]"]
        )
    if not re.match(ITEM_ID_PATTERN, candidate):
        raise ValidationError([f"{path}: ammessi solo lettere, cifre e . _ : -"])
    return candidate


def check_date(value: Any, path: str = "date", *, today: date_cls | None = None) -> str:
    """Data realmente valida sul calendario, non solo nel formato.

    `today` va passato SOLO in ingresso dal payload: un dato gia' persistito
    non viene rifiutato perche' l'orologio del sistema e' cambiato.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValidationError([f"{path}: data obbligatoria in formato AAAA-MM-GG"])
    candidate = value.strip()
    try:
        parsed = datetime.strptime(candidate, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError(
            [f"{path}: '{candidate}' non e' una data valida in formato AAAA-MM-GG"]
        ) from None
    if parsed.year < MIN_YEAR:
        raise ValidationError([f"{path}: anno {parsed.year} precedente al {MIN_YEAR}"])
    if today is not None and parsed > today + timedelta(days=FUTURE_TOLERANCE_DAYS):
        raise ValidationError([f"{path}: {parsed.isoformat()} e' nel futuro"])
    return parsed.isoformat()


def check_time(value: Any, path: str = "time") -> str | None:
    """Orario opzionale, normalizzato in HH:MM."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%H.%M"):
        try:
            return datetime.strptime(candidate, fmt).strftime("%H:%M")
        except ValueError:
            continue
    raise ValidationError([f"{path}: '{candidate}' non e' un orario valido (atteso HH:MM)"])


def check_store(value: Any, path: str = "store") -> str:
    """Negozio validato e normalizzato nella grafia canonica."""
    raw_store = check_text(value, path, max_len=MAX_STORE_LEN, required=True)
    return normalize_store(raw_store)


def check_canonical_category(value: Any, path: str = "category") -> str:
    """Categoria gia' canonica.

    Usato in rilettura: sul disco non ci possono essere categorie fuori da
    CATEGORIES, sarebbero frutto di un'edizione manuale sbagliata e
    falserebbero le statistiche per categoria.
    """
    if not isinstance(value, str):
        raise ValidationError([f"{path}: attesa categoria testuale"])
    for category in CATEGORIES:
        if category == value:
            return category
    raise ValidationError(
        [f"{path}: '{value}' non e' una categoria canonica (ammesse: {', '.join(CATEGORIES)})"]
    )


def collect(errors: list[str], fn, *args, **kwargs):
    """Adatta un checker che solleva a un validatore che accumula."""
    try:
        return fn(*args, **kwargs)
    except ValidationError as err:
        errors.extend(err.errors)
        return None


# --------------------------------------------------------------------------- #
# Adattatori per la validazione del payload
# --------------------------------------------------------------------------- #


def _req_str(raw: dict, field: str, errors: list[str], *, max_len: int, path: str) -> str | None:
    return collect(errors, check_text, raw.get(field), path, max_len=max_len, required=True)


def _opt_str(raw: dict, field: str, errors: list[str], *, max_len: int, path: str) -> str | None:
    return collect(errors, check_text, raw.get(field), path, max_len=max_len, required=False)


def _req_raw_str(raw: dict, field: str, errors: list[str], *, max_len: int, path: str) -> str | None:
    """raw_name: validato ma NON normalizzato, conservato verbatim."""
    return collect(
        errors, check_text, raw.get(field), path, max_len=max_len, required=True, verbatim=True
    )


def _money_field(
    raw: dict,
    field: str,
    errors: list[str],
    *,
    path: str,
    required: bool,
    min_v: float = MIN_AMOUNT,
    max_v: float = MAX_AMOUNT,
) -> Decimal | None:
    return collect(
        errors, check_amount, raw.get(field), path, required=required, min_v=min_v, max_v=max_v
    )


def _bool_field(raw: dict, field: str, errors: list[str], *, path: str, default: bool) -> bool:
    result = collect(errors, check_bool, raw.get(field), path, default=default)
    return default if result is None else result


# --------------------------------------------------------------------------- #
# Articoli
# --------------------------------------------------------------------------- #


def _validate_item(raw: Any, index: int, errors: list[str], dropped: set[str]) -> dict | None:
    path = f"items[{index}]"
    if not isinstance(raw, dict):
        errors.append(f"{path}: atteso oggetto JSON")
        return None

    for key in raw:
        if key not in ITEM_INPUT_FIELDS:
            dropped.add(f"{path}.{key}")

    item: dict[str, Any] = {}

    # --- id: assegnato solo se assente, mai corretto silenziosamente --------
    raw_id = raw.get("id")
    if raw_id is None:
        item["id"] = f"{index + 1:02d}"
        item["_id_explicit"] = False
    else:
        item_id = collect(errors, check_item_id, raw_id, f"{path}.id")
        if item_id is None:
            return None
        item["id"] = item_id
        item["_id_explicit"] = True

    item["_position"] = index

    # --- raw_name: obbligatorio, immutabile, conservato verbatim ------------
    raw_name = _req_raw_str(raw, "raw_name", errors, max_len=MAX_STR_LEN, path=f"{path}.raw_name")
    if raw_name is None:
        return None
    item["raw_name"] = raw_name

    # --- name: fallback sul raw_name ripulito -------------------------------
    # name_was_missing e' stato PERSISTENTE, non una variabile di lavoro:
    # senza di esso, dopo un riavvio non sapremmo distinguere un nome
    # normalizzato da ChatGPT da un ripiego generato da noi.
    name = _opt_str(raw, "name", errors, max_len=MAX_STR_LEN, path=f"{path}.name")
    item["name"] = name or _WS_RE.sub(" ", raw_name.strip())
    item["name_was_missing"] = name is None

    # --- quantity -----------------------------------------------------------
    quantity = collect(
        errors, check_quantity, raw.get("quantity"), f"{path}.quantity", default=Decimal("1.000")
    )
    if quantity is None:
        return None
    item["quantity"] = quantity

    # --- prezzi -------------------------------------------------------------
    price = _money_field(raw, "price", errors, path=f"{path}.price", required=True)
    if price is None:
        return None
    item["price"] = price

    item["unit_price"] = _money_field(
        raw, "unit_price", errors, path=f"{path}.unit_price", required=False
    )
    item["original_price"] = _money_field(
        raw, "original_price", errors, path=f"{path}.original_price", required=False
    )
    item["discount"] = _money_field(
        raw, "discount", errors, path=f"{path}.discount", required=False, min_v=0.0
    )

    # --- categoria ----------------------------------------------------------
    # category_was_unknown distingue una classificazione DELIBERATA da un
    # ripiego tecnico:
    #   "Altro" / "Alimentari" / ...  -> riconosciuta, flag False
    #   assente / null / "" / ignota  -> fallback su "Altro", flag True
    # E' il dato persistente che permette di sapere, anche dopo un riavvio,
    # se "Altro" era una scelta o una mancanza.
    raw_category = raw.get("category")
    category, recognised = normalize_category(raw_category)
    item["category"] = category
    item["category_was_unknown"] = not recognised

    # --- included: default True, salvo valore esplicito del client ----------
    default_included = category not in DEFAULT_EXCLUDED_CATEGORIES
    item["included"] = _bool_field(
        raw, "included", errors, path=f"{path}.included", default=default_included
    )

    # --- campi opzionali ----------------------------------------------------
    weight = collect(errors, check_quantity, raw.get("weight"), f"{path}.weight", default=None)
    item["weight"] = weight

    item["unit"] = _opt_str(raw, "unit", errors, max_len=16, path=f"{path}.unit")
    item["notes"] = _opt_str(raw, "notes", errors, max_len=MAX_NOTES_LEN, path=f"{path}.notes")
    item["product_id"] = _opt_str(raw, "product_id", errors, max_len=64, path=f"{path}.product_id")

    return item


def item_review_reasons(item: dict) -> set[str]:
    """Motivi automatici generati dal singolo articolo.

    Legge SOLO campi persistiti: ricalcolare dopo un riavvio deve produrre
    esattamente lo stesso insieme di motivi.
    """
    reasons: set[str] = set()

    price: Decimal = item["price"]
    discount: Decimal | None = item.get("discount")
    unit_price: Decimal | None = item.get("unit_price")
    quantity: Decimal = item["quantity"]

    if price < 0 and discount is None:
        reasons.add(REVIEW_NEGATIVE_PRICE)
    if item.get("name_was_missing", DEFAULT_NAME_WAS_MISSING):
        reasons.add(REVIEW_NO_NAME)
    if item.get("category_was_unknown", DEFAULT_CATEGORY_WAS_UNKNOWN):
        reasons.add(REVIEW_UNKNOWN_CATEGORY)

    # Convenzione: unit_price e' il prezzo unitario PRIMA dello sconto,
    # discount lo sconto totale della riga, price il prezzo finale.
    #   quantity x unit_price - discount  ==  price
    # Il controllo scatta solo se unit_price e' dichiarato: se manca non si
    # inventa nulla. Un price a 0 (omaggio, 3x2, campione) NON e' un'anomalia.
    if unit_price is not None:
        expected = (quantity * unit_price).quantize(_CENT, rounding=ROUND_HALF_UP)
        if discount is not None:
            expected = (expected - discount).quantize(_CENT, rounding=ROUND_HALF_UP)
        if abs(expected - price) > Decimal(str(ITEM_TOTAL_TOLERANCE)):
            reasons.add(REVIEW_ITEM_MATH)

    return reasons


# --------------------------------------------------------------------------- #
# Fingerprint
# --------------------------------------------------------------------------- #
#
# Separatori scelti fra i caratteri di controllo ASCII (US / RS): non possono
# comparire nei dati, quindi nessun valore puo' "travestirsi" da separatore.

_US: Final = "\x1f"  # unit separator: campi dentro un articolo
_RS: Final = "\x1e"  # record separator: articoli fra loro e sezioni


def _item_fingerprint_part(item: dict) -> str:
    """Serializzazione canonica di un articolo per i fingerprint.

    Contiene solo i tre dati stabili fra due letture della stessa foto:
    raw_name normalizzato, quantita', prezzo finale di riga.

    unit_price e discount sono deliberatamente esclusi: sono opzionali e
    dipendono da quanto bene il modello interpreta lo scontrino, quindi la
    stessa fotografia puo' produrli una volta valorizzati e una volta null.
    Il loro effetto economico e' comunque gia' contenuto in price.

    category e included sono esclusi perche' sono decisioni classificatorie
    modificabili dopo l'inserimento: se entrassero nell'impronta, correggere
    una categoria farebbe riemergere confronti gia' risolti.
    """
    return _US.join(
        (
            normalize_raw_name_key(item["raw_name"]),
            fmt_qty(item["quantity"]),
            fmt_money(item["price"]),
        )
    )


def compute_strong_fingerprint(receipt: dict) -> str:
    """Impronta forte: due scontrini identici articolo per articolo.

    Gli articoli sono ordinati alfabeticamente sulla loro serializzazione,
    cosi' l'impronta non dipende dall'ordine in cui il modello li elenca: due
    letture della stessa foto possono restituirli in sequenza diversa.
    """
    parts = sorted(_item_fingerprint_part(item) for item in receipt["items"])
    blob = _RS.join(
        (
            receipt["date"],
            normalize_store_key(receipt["store"]),
            fmt_money(receipt["receipt_total"]),
            str(len(receipt["items"])),
            _RS.join(parts),
        )
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compute_weak_fingerprint(receipt: dict) -> str:
    """Impronta debole: stessa data, negozio, totale e numero articoli.

    Da sola non prova nulla: segnala solo una somiglianza da verificare.
    """
    blob = _RS.join(
        (
            receipt["date"],
            normalize_store_key(receipt["store"]),
            fmt_money(receipt["receipt_total"]),
            str(len(receipt["items"])),
        )
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Calcoli derivati
# --------------------------------------------------------------------------- #


def compute_totals(receipt: dict) -> None:
    """Ricalcola items_total e included_total. Sempre, da zero, in Decimal.

    items_total somma TUTTE le righe, incluse quelle escluse e quelle
    negative. included_total somma le sole righe con included=True.
    """
    items_total = Decimal("0.00")
    included_total = Decimal("0.00")
    for item in receipt["items"]:
        price: Decimal = item["price"]
        items_total += price
        if item["included"]:
            included_total += price
    receipt["items_total"] = items_total.quantize(_CENT, rounding=ROUND_HALF_UP)
    receipt["included_total"] = included_total.quantize(_CENT, rounding=ROUND_HALF_UP)


def compute_review_reasons(receipt: dict) -> None:
    """Ricalcola review_reasons e needs_review.

      review_reasons = motivi automatici degli articoli
                     + total_mismatch
                     + possible_duplicate (rilevato e non archiviato)
                     + manual_flag (se manual_review)

      needs_review   = bool(review_reasons)

    needs_review non e' mai scritto direttamente: e' sempre il risultato di
    questa funzione, rieseguita per intero a ogni mutazione. Disattivare
    manual_review rimuove solo manual_flag e lascia intatti i motivi
    automatici, che spariscono solo correggendo i dati.
    """
    reasons: set[str] = set()

    for item in receipt["items"]:
        reasons |= item_review_reasons(item)

    delta = abs(receipt["items_total"] - receipt["receipt_total"])
    if delta > Decimal(str(TOTAL_TOLERANCE)):
        reasons.add(REVIEW_TOTAL_MISMATCH)

    if receipt.get("possible_duplicate_of") and not receipt.get(
        "possible_duplicate_dismissed", DEFAULT_DUPLICATE_DISMISSED
    ):
        reasons.add(REVIEW_POSSIBLE_DUPLICATE)

    if receipt.get("manual_review", DEFAULT_MANUAL_REVIEW):
        reasons.add(REVIEW_MANUAL)

    receipt["review_reasons"] = sorted(reasons)
    receipt["needs_review"] = bool(reasons)


def recompute(receipt: dict) -> None:
    """Ricalcola TUTTO cio' che e' derivato. Da chiamare dopo ogni mutazione.

    Non tocca possible_duplicate_of / possible_duplicate_dismissed: quelli
    dipendono dal confronto con l'archivio e li gestisce apply_duplicate_matches.
    """
    receipt["fingerprint"] = compute_strong_fingerprint(receipt)
    receipt["fingerprint_weak"] = compute_weak_fingerprint(receipt)
    compute_totals(receipt)
    compute_review_reasons(receipt)


def apply_duplicate_matches(
    receipt: dict, matches: list[str], *, invalidate_dismissal: bool = False
) -> bool:
    """Aggiorna il riferimento ai possibili duplicati.

    matches: receipt_id degli scontrini con fingerprint DEBOLE coincidente e
    fingerprint FORTE diverso. I duplicati certi non arrivano qui: sono
    respinti prima.

    Il riferimento non viene mai cancellato finche' la somiglianza esiste:
    resta come memoria storica anche dopo la verifica manuale.

    Il dismissal decade in tre casi:
      - l'insieme dei corrispondenti cambia: la decisione riguardava altri
      - invalidate_dismissal=True: i dati che partecipano ai fingerprint sono
        cambiati, quindi la situazione verificata non e' piu' quella attuale
      - non c'e' piu' alcuna corrispondenza: non resta nulla da archiviare

    Ritorna True se qualcosa e' cambiato.
    """
    previous = list(receipt.get("possible_duplicate_of") or [])
    current = sorted(set(matches))
    was_dismissed = receipt.get("possible_duplicate_dismissed", DEFAULT_DUPLICATE_DISMISSED)

    changed = False

    if previous != current:
        receipt["possible_duplicate_of"] = current
        changed = True

    should_dismiss = was_dismissed
    if previous != current:
        should_dismiss = DEFAULT_DUPLICATE_DISMISSED
    elif invalidate_dismissal and current:
        should_dismiss = DEFAULT_DUPLICATE_DISMISSED
    if not current:
        should_dismiss = DEFAULT_DUPLICATE_DISMISSED

    if should_dismiss != was_dismissed:
        receipt["possible_duplicate_dismissed"] = should_dismiss
        changed = True

    return changed


# --------------------------------------------------------------------------- #
# Ingresso dal payload HTTP
# --------------------------------------------------------------------------- #


def build_client_reported(raw: dict) -> dict | None:
    """Conserva i soli tre valori con reale utilita' diagnostica.

    Servono a confrontare quanto aveva calcolato ChatGPT con quanto calcola
    HA. Non influenzano nulla. Se nessuno e' presente ritorna None, cosi' la
    chiave resta assente dal file invece di comparire come null.
    """
    reported: dict[str, Any] = {}
    for field in sorted(CLIENT_REPORTED_FIELDS):
        if field not in raw or raw[field] is None:
            continue
        value = raw[field]
        if field == "needs_review":
            if isinstance(value, bool):
                reported[field] = value
            continue
        try:
            reported[field] = dec_to_float(money(value))
        except _ParseError:
            continue  # valore illeggibile: e' diagnostica, non blocca nulla
    return reported or None


def validate_payload(raw: Any, now: datetime) -> tuple[dict, list[str]]:
    """Valida il payload HTTP e costruisce lo scontrino completo.

    Ritorna (scontrino, campi_scartati).
    Solleva ValidationError, MissingSchemaError o UnsupportedSchemaError.

    Nessun campo derivato viene letto dal payload: items_total, included_total,
    needs_review, fingerprint, created_at e updated_at sono calcolati qui.

    schema_version e' obbligatorio nel payload ma NON entra nello scontrino:
    e' protocollo, e la versione viene dichiarata dal contenitore del file
    mensile, non da ogni singolo receipt.
    """
    if not isinstance(raw, dict):
        raise ValidationError(["Il payload deve essere un oggetto JSON"])

    # --- schema_version: obbligatorio, intero JSON, nessun default ---------
    if "schema_version" not in raw or raw["schema_version"] is None:
        raise MissingSchemaError()

    # Rigore coerente con booleani, id e date: la versione del protocollo fra
    # Comando Rapido e Home Assistant deve essere un intero JSON. Sono
    # rifiutati "1", 1.0, true e qualunque altra forma.
    declared = raw["schema_version"]
    if (
        isinstance(declared, bool)
        or not isinstance(declared, int)
        or declared not in SUPPORTED_SCHEMA_VERSIONS
    ):
        raise UnsupportedSchemaError(declared)

    errors: list[str] = []
    dropped: set[str] = set()

    for key in raw:
        if key not in RECEIPT_INPUT_FIELDS and key not in CLIENT_REPORTED_FIELDS:
            dropped.add(key)

    receipt_id = collect(errors, check_receipt_id, raw.get("receipt_id"))
    date_value = collect(errors, check_date, raw.get("date"), "date", today=now.date())
    time_value = collect(errors, check_time, raw.get("time"), "time")
    store = _req_str(raw, "store", errors, max_len=MAX_STORE_LEN, path="store")
    receipt_total = _money_field(
        raw, "receipt_total", errors, path="receipt_total", required=True
    )
    notes = _opt_str(raw, "notes", errors, max_len=MAX_NOTES_LEN, path="notes")

    # --- articoli -----------------------------------------------------------
    raw_items = raw.get("items")
    items: list[dict] = []
    if not isinstance(raw_items, list):
        errors.append("items: atteso un elenco di articoli")
    elif not raw_items:
        errors.append("items: l'elenco degli articoli non puo' essere vuoto")
    elif len(raw_items) > MAX_ITEMS:
        errors.append(f"items: {len(raw_items)} articoli, massimo {MAX_ITEMS}")
    else:
        for index, raw_item in enumerate(raw_items):
            item = _validate_item(raw_item, index, errors, dropped)
            if item is not None:
                items.append(item)

        # Collisioni di id: mai risolte in automatico, il payload e' invalido.
        # Copre anche il caso misto: id generato '01' per un articolo senza id
        # che collide con un '01' dichiarato esplicitamente piu' avanti.
        by_id: dict[str, list[dict]] = {}
        for item in items:
            by_id.setdefault(item["id"], []).append(item)
        for item_id, colliding in sorted(by_id.items()):
            if len(colliding) < 2:
                continue
            detail = ", ".join(
                f"items[{it['_position']}]"
                + ("" if it["_id_explicit"] else " (id generato automaticamente)")
                for it in colliding
            )
            errors.append(
                f"items: id articolo duplicato '{item_id}' usato da {detail}. "
                "Assegna id univoci oppure ometti il campo id in tutti gli articoli."
            )

    if errors:
        raise ValidationError(errors)

    timestamp = now.isoformat(timespec="seconds")

    receipt: dict[str, Any] = {
        # ---------------- INPUT ----------------
        "receipt_id": receipt_id,
        "date": date_value,
        "time": time_value,
        "store": normalize_store(store),
        "receipt_total": receipt_total,
        "notes": notes,
        "items": items,
        # ---------------- INTERNO ----------------
        "manual_review": DEFAULT_MANUAL_REVIEW,
        "possible_duplicate_dismissed": DEFAULT_DUPLICATE_DISMISSED,
        # ---------------- DERIVATO ----------------
        "possible_duplicate_of": [],
        "created_at": timestamp,
        "updated_at": None,
        "client_reported": build_client_reported(raw),
    }

    recompute(receipt)
    return receipt, sorted(dropped)


# --------------------------------------------------------------------------- #
# Modifiche dai servizi
# --------------------------------------------------------------------------- #


def coerce_item_field(field: str, value: Any) -> Any:
    """Converte e valida un singolo campo in modifica su un articolo."""
    if field in ITEM_IMMUTABLE_FIELDS:
        raise ValidationError(
            [f"{field} e' immutabile: rappresenta il dato originale dello scontrino"]
        )
    if field not in ITEM_EDITABLE_FIELDS:
        raise ValidationError([f"{field} non e' un campo modificabile di un articolo"])

    if field == "included":
        return check_bool(value, field)
    if field == "name":
        return check_text(value, field, max_len=MAX_STR_LEN, required=True)
    if field == "notes":
        return check_text(value, field, max_len=MAX_NOTES_LEN, required=False)
    if field == "product_id":
        return check_text(value, field, max_len=64, required=False)
    if field == "category":
        category, recognised = normalize_category(value)
        if not recognised:
            raise ValidationError(
                [f"category: '{value}' non valida (ammesse: {', '.join(CATEGORIES)})"]
            )
        return category
    if field == "quantity":
        result = check_quantity(value, field)
        if result is None:
            raise ValidationError(["quantity: valore obbligatorio"])
        return result
    if field == "discount":
        return check_amount(value, field, required=False, min_v=0.0)
    # price, unit_price
    return check_amount(value, field, required=(field == "price"))


# Correggere un campo risolve il motivo di verifica che quel campo aveva
# generato: la correzione dell'utente deve avere un effetto reale.
_ITEM_FLAG_CLEARED_BY: Final = {
    "name": "name_was_missing",
    "category": "category_was_unknown",
}


def apply_item_field(item: dict, field: str, value: Any) -> None:
    """Valida, assegna e risolve il flag interno associato al campo.

    UNICO punto di scrittura su un articolo: i servizi e le entita' passano da
    qui, cosi' l'accoppiamento campo -> flag non puo' sfuggire a un chiamante
    distratto.

    La coercizione avviene PRIMA dell'assegnazione: un valore invalido lascia
    l'articolo esattamente com'era, flag compresi.
    """
    coerced = coerce_item_field(field, value)
    item[field] = coerced

    flag = _ITEM_FLAG_CLEARED_BY.get(field)
    if flag is not None:
        item[flag] = False


def coerce_receipt_field(field: str, value: Any, now: datetime) -> Any:
    """Converte e valida un singolo campo in modifica su uno scontrino."""
    if field == "needs_review":
        raise ValidationError(
            [
                "needs_review e' un campo derivato: usa manual_review per "
                "richiedere una verifica manuale"
            ]
        )
    if field not in RECEIPT_EDITABLE_FIELDS:
        raise ValidationError([f"{field} non e' un campo modificabile di uno scontrino"])

    if field in {"manual_review", "possible_duplicate_dismissed"}:
        return check_bool(value, field, default=False)
    if field == "date":
        return check_date(value, field, today=now.date())
    if field == "time":
        return check_time(value, field)
    if field == "store":
        return check_store(value, field)
    if field == "notes":
        return check_text(value, field, max_len=MAX_NOTES_LEN, required=False)
    # receipt_total
    return check_amount(value, field, required=True)


def touch(receipt: dict, now: datetime) -> None:
    """Marca lo scontrino come modificato."""
    receipt["updated_at"] = now.isoformat(timespec="seconds")
