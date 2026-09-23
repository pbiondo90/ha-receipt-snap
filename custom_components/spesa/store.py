"""Persistenza su disco dell'integrazione Spesa alimentare.

Unico modulo che tocca il filesystem. Tutte le funzioni qui dentro sono
SINCRONE e bloccanti: e' manager.py a eseguirle in executor e a serializzarle
sotto un unico asyncio.Lock. Nessuna funzione di questo file va chiamata
direttamente dall'event loop.

Garanzie offerte:

  * scrittura atomica  .tmp -> fsync -> .bak atomico -> os.replace -> fsync dir
  * backup obbligatorio: se il .bak non riesce, il file principale non cambia
  * nessuna sovrascrittura automatica di un file corrotto o invalido
  * distinzione netta fra corruzione sintattica e struttura non valida
  * allowlist positiva sia in serializzazione sia in deserializzazione
  * invarianti condivise con model.py: una sola definizione di dato valido
  * Decimal su tutti gli importi, float solo dentro il JSON
  * un mese non caricabile entra in stato DEGRADATO e rifiuta ogni scrittura
  * transaction journal (WAL) per le transazioni multi-mese
  * blocco persistente quando lo stato su disco e' incerto
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .const import (
    BAK_EXT,
    BAK_ORPHAN_EXT,
    BLOCKED_MARKER_NAME,
    BLOCKED_MARKER_VERSION,
    CLIENT_REPORTED_FIELDS,
    CORRUPT_EXT,
    DEFAULT_CATEGORY_WAS_UNKNOWN,
    DEFAULT_DUPLICATE_DISMISSED,
    DEFAULT_MANUAL_REVIEW,
    DEFAULT_NAME_WAS_MISSING,
    FILE_EXT,
    ITEM_PERSISTED_FIELDS,
    JOURNAL_NAME,
    JOURNAL_ORPHAN_EXT,
    JOURNAL_VERSION,
    MAX_NOTES_LEN,
    MAX_STORE_LEN,
    MAX_STR_LEN,
    MONTH_PERSISTED_FIELDS,
    RECEIPT_PERSISTED_FIELDS,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    TMP_EXT,
)
from .model import (
    ValidationError,
    check_amount,
    check_bool,
    check_canonical_category,
    check_date,
    check_item_id,
    check_quantity,
    check_receipt_id,
    check_text,
    check_time,
    recompute,
)

_LOGGER = logging.getLogger(__name__)

# Mese valido: 01-12. Rifiuta 2026-00, 2026-13, 2026-99 anche sui file vuoti.
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
MONTH_FILE_RE = re.compile(r"^(\d{4}-(?:0[1-9]|1[0-2]))\.json$")

_BOOL_ITEM_FIELDS = {
    "included": True,
    "name_was_missing": DEFAULT_NAME_WAS_MISSING,
    "category_was_unknown": DEFAULT_CATEGORY_WAS_UNKNOWN,
}
_BOOL_RECEIPT_FIELDS = {
    "manual_review": DEFAULT_MANUAL_REVIEW,
    "possible_duplicate_dismissed": DEFAULT_DUPLICATE_DISMISSED,
}


# --------------------------------------------------------------------------- #
# Esiti e errori
# --------------------------------------------------------------------------- #


class StoreError(Exception):
    """Errore generico del livello di persistenza."""


class CorruptFileError(StoreError):
    """JSON sintatticamente non valido: il parser non arriva in fondo."""


class InvalidStructureError(StoreError):
    """JSON valido ma struttura persistita non conforme.

    Caso tipico: file modificato a mano con items non lista, uno scontrino
    senza receipt_id, un articolo senza raw_name, un importo non numerico.
    Trattato con la stessa prudenza della corruzione: niente caricamento
    parziale, niente riscrittura.
    """


class MonthDegradedError(StoreError):
    """Tentativo di scrittura su un mese in stato degradato."""

    def __init__(self, month: str, reason: str) -> None:
        self.month = month
        self.reason = reason
        super().__init__(f"Mese {month} in stato degradato: {reason}")


@dataclass(slots=True)
class LoadResult:
    """Esito del caricamento di un singolo file mensile."""

    month: str
    receipts: list[dict] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None
    recovered_from_bak: bool = False
    quarantined_path: str | None = None
    unknown_keys: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Conversioni Decimal <-> JSON
# --------------------------------------------------------------------------- #
#
# In memoria gli importi sono SEMPRE Decimal. Nel file JSON sono numeri, perche'
# il file deve restare leggibile e correggibile a mano con un editor.
#
# La conversione passa per float, e questo e' sicuro entro i nostri limiti.
# Dimostrazione, non atto di fede:
#
#   * gli importi sono quantizzati a 2 decimali e vincolati a |v| <= 10_000,
#     quindi hanno al massimo 7 cifre significative; le quantita' sono
#     quantizzate a 3 decimali con |v| <= 1_000, ancora al massimo 7 cifre.
#   * un float64 ha 53 bit di mantissa, circa 15-17 cifre decimali
#     significative: ogni valore con <= 15 cifre ha un float piu' vicino unico.
#   * json.dumps usa repr() sui float, che produce la stringa PIU' CORTA che
#     rilegge allo stesso identico float: repr(float(Decimal('2.58'))) e'
#     '2.58', non '2.5800000000000001'.
#   * in rilettura si passa sempre da check_amount/check_quantity, che fanno
#     Decimal(str(valore)) e riquantizzano.
#
# Round-trip: Decimal('2.58') -> 2.58 -> '2.58' -> Decimal('2.58'). Esatto.
# La rappresentazione binaria intermedia non e' mai usata per fare aritmetica:
# tutti i calcoli avvengono in Decimal, prima e dopo.


def _decimal_to_json(value: Any) -> Any:
    """Converte ricorsivamente i Decimal in float per la serializzazione."""
    if isinstance(value, Decimal):
        if value == 0:
            return 0.0  # evita '-0.0' nel file quando il Decimal e' -0.00
        return float(value)
    if isinstance(value, dict):
        return {k: _decimal_to_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimal_to_json(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# Serializzazione: memoria -> disco (allowlist positiva)
# --------------------------------------------------------------------------- #


def serialize_item(item: dict) -> dict:
    """Riduce un articolo alle sole chiavi persistite.

    Allowlist POSITIVA: si scrivono esattamente ITEM_PERSISTED_FIELDS. Le
    chiavi di lavoro (_position, _id_explicit) e qualunque chiave inattesa non
    raggiungono il disco, senza dipendere da convenzioni sul nome.
    """
    out: dict[str, Any] = {}
    for key in sorted(ITEM_PERSISTED_FIELDS):
        if key not in item:
            continue
        out[key] = _decimal_to_json(item[key])
    # I flag interni sono sempre scritti esplicitamente: un file senza di essi
    # sarebbe ambiguo alla rilettura.
    for key, default in _BOOL_ITEM_FIELDS.items():
        out.setdefault(key, default)
    return out


def serialize_receipt(receipt: dict) -> dict:
    """Riduce uno scontrino alle sole chiavi persistite.

    schema_version NON compare: e' protocollo, dichiarato dal contenitore del
    file mensile, non ripetuto in ogni scontrino.
    """
    out: dict[str, Any] = {}
    for key in sorted(RECEIPT_PERSISTED_FIELDS):
        if key == "items" or key not in receipt:
            continue
        value = receipt[key]
        if value is None and key == "client_reported":
            continue  # chiave assente invece di null
        out[key] = _decimal_to_json(value)
    for key, default in _BOOL_RECEIPT_FIELDS.items():
        out.setdefault(key, default)
    out["items"] = [serialize_item(item) for item in receipt.get("items", [])]
    return out


def serialize_month(month: str, receipts: list[dict]) -> dict:
    """Costruisce il contenuto completo di un file mensile."""
    ordered = sorted(
        receipts,
        key=lambda r: (r.get("date", ""), r.get("time") or "", r.get("receipt_id", "")),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "month": month,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "receipt_count": len(ordered),
        "receipts": [serialize_receipt(r) for r in ordered],
    }


# --------------------------------------------------------------------------- #
# Deserializzazione: disco -> memoria (stessa allowlist, invarianti condivise)
# --------------------------------------------------------------------------- #


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidStructureError(message)


def _shared(fn, *args, **kwargs):
    """Applica un checker condiviso di model.py traducendone l'esito.

    Le invarianti sono le stesse dell'ingresso HTTP: un file modificato a mano
    deve rispettare esattamente le regole che rispetta un payload valido.
    Cambia solo la reazione: qui l'errore significa file non caricabile, non
    richiesta respinta.
    """
    try:
        return fn(*args, **kwargs)
    except ValidationError as err:
        raise InvalidStructureError("; ".join(err.errors)) from err


def deserialize_item(raw: Any, path: str, unknown: list[str]) -> dict:
    """Ricostruisce un articolo dal JSON verificando le invarianti complete.

    Nessun caricamento parziale: un articolo non conforme fa fallire l'intero
    file, che entra nella procedura di recupero senza essere toccato.
    """
    _require(isinstance(raw, dict), f"{path}: atteso oggetto, trovato {type(raw).__name__}")

    for key in raw:
        if key not in ITEM_PERSISTED_FIELDS:
            unknown.append(f"{path}.{key}")

    item: dict[str, Any] = {}

    item["id"] = _shared(check_item_id, raw.get("id"), f"{path}.id")
    item["raw_name"] = _shared(
        check_text,
        raw.get("raw_name"),
        f"{path}.raw_name",
        max_len=MAX_STR_LEN,
        required=True,
        verbatim=True,
    )
    item["name"] = _shared(
        check_text, raw.get("name"), f"{path}.name", max_len=MAX_STR_LEN, required=True
    )
    item["category"] = _shared(check_canonical_category, raw.get("category"), f"{path}.category")
    item["unit"] = _shared(check_text, raw.get("unit"), f"{path}.unit", max_len=16, required=False)
    item["notes"] = _shared(
        check_text, raw.get("notes"), f"{path}.notes", max_len=MAX_NOTES_LEN, required=False
    )
    item["product_id"] = _shared(
        check_text, raw.get("product_id"), f"{path}.product_id", max_len=64, required=False
    )

    item["price"] = _shared(check_amount, raw.get("price"), f"{path}.price", required=True)
    item["unit_price"] = _shared(
        check_amount, raw.get("unit_price"), f"{path}.unit_price", required=False
    )
    item["original_price"] = _shared(
        check_amount, raw.get("original_price"), f"{path}.original_price", required=False
    )
    item["discount"] = _shared(
        check_amount, raw.get("discount"), f"{path}.discount", required=False, min_v=0.0
    )

    item["quantity"] = _shared(
        check_quantity, raw.get("quantity"), f"{path}.quantity", default=Decimal("1.000")
    )
    item["weight"] = _shared(check_quantity, raw.get("weight"), f"{path}.weight", default=None)

    for bool_field, default in _BOOL_ITEM_FIELDS.items():
        item[bool_field] = _shared(
            check_bool, raw.get(bool_field), f"{path}.{bool_field}", default=default
        )

    return item


def _deserialize_client_reported(raw: Any, path: str, unknown: list[str]) -> dict | None:
    """Rilegge il blocco diagnostico applicando la stessa whitelist di model.py.

    Puramente informativo, ma non per questo puo' contenere qualunque cosa: un
    file modificato a mano non deve poter iniettare struttura arbitraria nello
    stato in memoria.
    """
    if raw is None:
        return None
    _require(isinstance(raw, dict), f"{path}: atteso oggetto, trovato {type(raw).__name__}")

    reported: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in CLIENT_REPORTED_FIELDS:
            unknown.append(f"{path}.{key}")
            continue
        if value is None:
            continue
        if key == "needs_review":
            reported[key] = _shared(check_bool, value, f"{path}.{key}", default=False)
        else:
            amount = _shared(check_amount, value, f"{path}.{key}", required=False)
            if amount is not None:
                reported[key] = float(amount)
    return reported or None


def deserialize_receipt(raw: Any, path: str, unknown: list[str]) -> dict:
    """Ricostruisce uno scontrino dal JSON verificando le invarianti.

    I campi derivati presenti nel file NON sono autorevoli: recompute() li
    ricostruisce da zero, cosi' un file modificato a mano non puo' falsificare
    i totali. Si leggono solo quelli che recompute() non puo' ricostruire:
    created_at, updated_at, possible_duplicate_of e client_reported.

    schema_version non viene cercato qui: la versione appartiene al
    contenitore del file mensile ed e' verificata da deserialize_month().
    """
    _require(isinstance(raw, dict), f"{path}: atteso oggetto, trovato {type(raw).__name__}")

    for key in raw:
        if key not in RECEIPT_PERSISTED_FIELDS:
            unknown.append(f"{path}.{key}")

    receipt: dict[str, Any] = {}

    receipt["receipt_id"] = _shared(check_receipt_id, raw.get("receipt_id"), f"{path}.receipt_id")
    receipt["date"] = _shared(check_date, raw.get("date"), f"{path}.date")
    receipt["time"] = _shared(check_time, raw.get("time"), f"{path}.time")
    # check_text e non check_store: rinormalizzare a ogni avvio riscriverebbe
    # un nome eventualmente corretto a mano.
    receipt["store"] = _shared(
        check_text, raw.get("store"), f"{path}.store", max_len=MAX_STORE_LEN, required=True
    )
    receipt["notes"] = _shared(
        check_text, raw.get("notes"), f"{path}.notes", max_len=MAX_NOTES_LEN, required=False
    )
    receipt["receipt_total"] = _shared(
        check_amount, raw.get("receipt_total"), f"{path}.receipt_total", required=True
    )

    for bool_field, default in _BOOL_RECEIPT_FIELDS.items():
        receipt[bool_field] = _shared(
            check_bool, raw.get(bool_field), f"{path}.{bool_field}", default=default
        )

    duplicates = raw.get("possible_duplicate_of") or []
    _require(
        isinstance(duplicates, list),
        f"{path}.possible_duplicate_of: attesa lista, trovato {type(duplicates).__name__}",
    )
    receipt["possible_duplicate_of"] = sorted(
        {
            _shared(check_receipt_id, entry, f"{path}.possible_duplicate_of[{i}]")
            for i, entry in enumerate(duplicates)
        }
    )

    receipt["created_at"] = _shared(
        check_text, raw.get("created_at"), f"{path}.created_at", max_len=64, required=False
    )
    receipt["updated_at"] = _shared(
        check_text, raw.get("updated_at"), f"{path}.updated_at", max_len=64, required=False
    )

    receipt["client_reported"] = _deserialize_client_reported(
        raw.get("client_reported"), f"{path}.client_reported", unknown
    )

    raw_items = raw.get("items")
    _require(
        isinstance(raw_items, list),
        f"{path}.items: attesa lista di articoli, trovato {type(raw_items).__name__}",
    )
    _require(bool(raw_items), f"{path}.items: elenco articoli vuoto")

    items: list[dict] = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        item = deserialize_item(raw_item, f"{path}.items[{index}]", unknown)
        _require(
            item["id"] not in seen_ids,
            f"{path}.items[{index}].id: identificativo duplicato {item['id']!r}",
        )
        seen_ids.add(item["id"])
        items.append(item)
    receipt["items"] = items

    recompute(receipt)
    return receipt


def deserialize_month(raw: Any, month: str, unknown: list[str]) -> list[dict]:
    """Ricostruisce il contenuto di un file mensile.

    Allowlist anche al livello root: le chiavi inattese vengono segnalate e
    ignorate, ma quelle previste devono avere il tipo corretto. Un metadato con
    un tipo impossibile indica un file manomesso, non un dettaglio da ignorare.
    """
    _require(
        isinstance(raw, dict),
        f"{month}: il file deve contenere un oggetto JSON, trovato {type(raw).__name__}",
    )

    for key in raw:
        if key not in MONTH_PERSISTED_FIELDS:
            unknown.append(f"{month}.{key}")

    # --- schema_version: obbligatorio e supportato -------------------------
    declared = raw.get("schema_version")
    _require(declared is not None, f"{month}: schema_version mancante nel file mensile")
    _require(
        not isinstance(declared, bool) and isinstance(declared, int),
        f"{month}.schema_version: atteso intero, trovato {type(declared).__name__}",
    )
    _require(
        declared in SUPPORTED_SCHEMA_VERSIONS,
        f"{month}: schema_version {declared!r} non supportata da questa installazione. "
        "Il file appartiene a una versione piu' recente dell'integrazione: "
        "aggiornala invece di modificare il file.",
    )

    # --- month: obbligatorio, deve combaciare col filename -----------------
    declared_month = raw.get("month")
    _require(declared_month is not None, f"{month}: campo 'month' mancante nel file")
    _require(
        isinstance(declared_month, str),
        f"{month}.month: atteso testo, trovato {type(declared_month).__name__}",
    )
    _require(
        declared_month == month,
        f"{month}: il file dichiara month={declared_month!r} ma si trova in "
        f"{month}{FILE_EXT}. Nome file e contenuto devono coincidere.",
    )

    # --- generated_at: metadato, ma tipizzato ------------------------------
    generated_at = raw.get("generated_at")
    if generated_at is not None:
        _require(
            isinstance(generated_at, str) and 0 < len(generated_at) <= 64,
            f"{month}.generated_at: attesa stringa temporale, trovato {generated_at!r}",
        )

    # --- receipts: obbligatorio e lista ------------------------------------
    raw_receipts = raw.get("receipts")
    _require(
        isinstance(raw_receipts, list),
        f"{month}: 'receipts' deve essere una lista, trovato {type(raw_receipts).__name__}",
    )

    receipts: list[dict] = []
    seen: set[str] = set()
    for index, raw_receipt in enumerate(raw_receipts):
        receipt = deserialize_receipt(raw_receipt, f"{month}.receipts[{index}]", unknown)
        _require(
            receipt["receipt_id"] not in seen,
            f"{month}.receipts[{index}]: receipt_id duplicato "
            f"{receipt['receipt_id']!r} nello stesso file",
        )
        seen.add(receipt["receipt_id"])
        _require(
            receipt["date"][:7] == month,
            f"{month}.receipts[{index}]: lo scontrino {receipt['receipt_id']!r} ha "
            f"data {receipt['date']} che non appartiene al mese {month}",
        )
        receipts.append(receipt)

    # --- receipt_count: metadato informativo, ma tipizzato -----------------
    declared_count = raw.get("receipt_count")
    if declared_count is not None:
        _require(
            not isinstance(declared_count, bool) and isinstance(declared_count, int),
            f"{month}.receipt_count: atteso intero, trovato "
            f"{type(declared_count).__name__} {declared_count!r}",
        )
        _require(
            declared_count >= 0, f"{month}.receipt_count: valore negativo {declared_count}"
        )
        if declared_count != len(receipts):
            _LOGGER.info(
                "%s: receipt_count dichiara %d ma il file contiene %d scontrini. "
                "Metadato informativo, verra' riallineato al prossimo salvataggio.",
                month,
                declared_count,
                len(receipts),
            )

    if unknown:
        _LOGGER.info(
            "%s: chiavi non riconosciute ignorate in lettura: %s",
            month,
            ", ".join(unknown[:10]) + ("..." if len(unknown) > 10 else ""),
        )

    return receipts


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class SpesaStore:
    """Accesso al disco per i file mensili. Tutte le operazioni sono sincrone."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        # month -> motivo, popolato quando un mese non e' caricabile
        self.degraded: dict[str, str] = {}

    # ----------------------------------------------------------------- path #

    def path_for(self, month: str) -> Path:
        return self.base_dir / f"{month}{FILE_EXT}"

    def bak_for(self, month: str) -> Path:
        return self.base_dir / f"{month}{BAK_EXT}"

    def journal_path(self) -> Path:
        return self.base_dir / JOURNAL_NAME

    def blocked_marker_path(self) -> Path:
        return self.base_dir / BLOCKED_MARKER_NAME

    # ------------------------------------------------------------ primitive #

    def ensure_dir(self) -> None:
        """Crea /config/spesa se assente e verifica di potervi scrivere."""
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            raise StoreError(f"Creazione di {self.base_dir} fallita: {err}") from err
        if not os.access(self.base_dir, os.W_OK | os.X_OK):
            raise StoreError(f"Directory {self.base_dir} non scrivibile")

    def _fsync_dir(self) -> None:
        dir_fd = os.open(str(self.base_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _write_atomic(self, target: Path, text: str) -> None:
        """Scrittura atomica generica: .tmp -> fsync -> replace -> fsync dir."""
        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.base_dir), prefix=f"{target.name}.", suffix=TMP_EXT
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(tmp_path), str(target))
            tmp_path = None
            self._fsync_dir()
        except OSError as err:
            raise StoreError(f"Scrittura di {target.name} fallita: {err}") from err
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    _LOGGER.warning("Temporaneo %s non rimosso", tmp_path)

    def _read_json(self, path: Path) -> Any:
        """Legge e parsa un file. Distingue i due tipi di problema."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as err:
            raise StoreError(f"Lettura di {path.name} fallita: {err}") from err
        if not text.strip():
            raise CorruptFileError(f"{path.name}: file vuoto")
        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            raise CorruptFileError(
                f"{path.name}: JSON non valido a riga {err.lineno}, colonna {err.colno}: {err.msg}"
            ) from err

    def cleanup_stale_tmp(self) -> list[str]:
        """Rimuove i .tmp rimasti da una scrittura interrotta.

        Un .tmp presente all'avvio significa che il processo e' morto fra la
        creazione del temporaneo e os.replace(). Il file definitivo non e' mai
        stato toccato, quindi il dato buono e' ancora al suo posto: il .tmp e'
        scarto e va solo rimosso. Nessun dato viene perso.
        """
        removed: list[str] = []
        for candidate in sorted(self.base_dir.glob(f"*{TMP_EXT}")):
            try:
                candidate.unlink()
                removed.append(candidate.name)
            except OSError as err:
                _LOGGER.warning("Impossibile rimuovere il temporaneo %s: %s", candidate, err)
        if removed:
            _LOGGER.warning(
                "Rimossi %d file temporanei da una scrittura interrotta: %s. "
                "I dati definitivi non sono stati modificati.",
                len(removed),
                ", ".join(removed),
            )
        return removed

    def list_months(self) -> list[str]:
        """Elenca i mesi con file principale presente, dal piu' recente."""
        months: list[str] = []
        for candidate in self.base_dir.glob(f"*{FILE_EXT}"):
            match = MONTH_FILE_RE.match(candidate.name)
            if match:
                months.append(match.group(1))
        return sorted(months, reverse=True)

    def _quarantine(self, path: Path) -> str:
        """Mette da parte un file illeggibile SENZA cancellarlo."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = path.with_name(f"{path.stem}{CORRUPT_EXT}-{stamp}")
        counter = 1
        while target.exists():
            target = path.with_name(f"{path.stem}{CORRUPT_EXT}-{stamp}_{counter:02d}")
            counter += 1
        shutil.move(str(path), str(target))
        # Il rename deve essere durevole: senza fsync della directory, un crash
        # potrebbe lasciare il file illeggibile ancora al suo posto originale.
        self._fsync_dir()
        return str(target)

    # -------------------------------------------------------------- lettura #

    def load_month(self, month: str) -> LoadResult:
        """Carica un mese applicando la scala di recupero.

        1. file principale valido            -> caricato
        2. principale illeggibile, .bak ok   -> .bak caricato, principale in
                                                quarantena, NON sovrascritto
        3. entrambi illeggibili              -> mese DEGRADATO: vuoto in
                                                memoria, scritture rifiutate,
                                                nessun file toccato

        Invariante: se questo metodo termina validamente, il mese NON resta in
        self.degraded, nemmeno se una degradazione precedente lo aveva incluso.
        """
        _require(bool(MONTH_RE.match(month)), f"Mese {month!r} non valido")
        result = LoadResult(month=month)
        path = self.path_for(month)
        bak = self.bak_for(month)

        if not path.exists() and not bak.exists():
            return result  # mese semplicemente non ancora esistente

        primary_error: str | None = None

        if path.exists():
            try:
                raw = self._read_json(path)
                result.receipts = deserialize_month(raw, month, result.unknown_keys)
                # Caricamento riuscito: una degradazione di un tentativo
                # precedente non e' piu' vera e non deve sopravvivere.
                self.degraded.pop(month, None)
                return result
            except CorruptFileError as err:
                primary_error = f"JSON corrotto - {err}"
                _LOGGER.error("%s non leggibile: %s", path.name, err)
            except InvalidStructureError as err:
                primary_error = f"struttura non valida - {err}"
                _LOGGER.error(
                    "%s ha una struttura non valida: %s. Il file NON verra' "
                    "sovrascritto automaticamente.",
                    path.name,
                    err,
                )
            except StoreError as err:
                primary_error = str(err)
                _LOGGER.error("%s: %s", path.name, err)
        else:
            primary_error = "file principale assente"
            _LOGGER.error(
                "%s assente ma il backup %s esiste: possibile cancellazione accidentale.",
                path.name,
                bak.name,
            )

        # --- tentativo di recupero dal .bak --------------------------------
        if bak.exists():
            try:
                raw = self._read_json(bak)
                receipts = deserialize_month(raw, month, result.unknown_keys)
            except StoreError as err:
                reason = f"{primary_error}; backup pure inutilizzabile - {err}"
                self.degraded[month] = reason
                result.degraded = True
                result.degraded_reason = reason
                _LOGGER.error(
                    "Mese %s NON recuperabile: %s. Nessun file e' stato modificato. "
                    "Le scritture su questo mese sono bloccate.",
                    month,
                    reason,
                )
                return result

            result.receipts = receipts
            result.recovered_from_bak = True
            self.degraded.pop(month, None)
            if path.exists():
                result.quarantined_path = self._quarantine(path)
            _LOGGER.warning(
                "Mese %s recuperato dal backup (%d scontrini). File problematico "
                "conservato in %s per ispezione manuale.",
                month,
                len(receipts),
                result.quarantined_path or "(nessuno)",
            )
            return result

        reason = f"{primary_error}; nessun backup disponibile"
        self.degraded[month] = reason
        result.degraded = True
        result.degraded_reason = reason
        _LOGGER.error(
            "Mese %s NON recuperabile: %s. Il file originale resta intatto sul "
            "disco e le scritture su questo mese sono bloccate.",
            month,
            reason,
        )
        return result

    def load_all(self, *, expect_absent: set[str] | None = None) -> list[LoadResult]:
        """Carica tutti i mesi presenti. Un mese degradato non blocca gli altri.

        expect_absent: mesi che il recovery ha appena riportato a 'inesistenti'.
        Per questi, un .bak residuo non viene interpretato come cancellazione
        accidentale: sarebbe la resurrezione di dati appena rimossi. La
        protezione permanente e' comunque orphan_bak(), che toglie il backup
        dal namespace attivo.
        """
        self.ensure_dir()
        self.cleanup_stale_tmp()
        expect_absent = expect_absent or set()

        months = set(self.list_months())
        for candidate in self.base_dir.glob(f"*{BAK_EXT}"):
            name = candidate.name[: -len(BAK_EXT)]
            if MONTH_RE.match(name) and name not in expect_absent:
                months.add(name)

        for month in sorted(expect_absent):
            if self.path_for(month).exists():
                _LOGGER.error(
                    "%s esiste nonostante il recovery lo avesse rimosso: "
                    "verra' caricato normalmente.",
                    self.path_for(month).name,
                )

        return [self.load_month(month) for month in sorted(months)]

    # ------------------------------------------------------------ scrittura #

    def is_degraded(self, month: str) -> bool:
        return month in self.degraded

    def clear_degraded(self, month: str) -> None:
        """Toglie il blocco dopo un intervento manuale, via spesa.ricalcola."""
        self.degraded.pop(month, None)

    def save_month(self, month: str, receipts: list[dict]) -> None:
        """Scrive un mese in modo atomico.

        Sequenza:
          1. rifiuto se il mese e' degradato
          2. serializzazione completa IN MEMORIA (un errore qui non tocca il disco)
          3. scrittura su .tmp nella stessa directory + flush + fsync
          4. aggiornamento ATOMICO del .bak con la versione precedente
          5. os.replace(.tmp -> definitivo)
          6. fsync della directory

        Semantica FORTE del backup: se esiste un file precedente e il suo
        backup non riesce, la sostituzione NON avviene. Fra "salvare senza rete
        di sicurezza" e "non salvare lasciando intatto il dato precedente",
        questo progetto sceglie il secondo. Unica eccezione: il primo
        salvataggio di un mese, quando non c'e' nulla da mettere al sicuro.

        ATTENZIONE per i chiamanti: un'eccezione da questo metodo NON garantisce
        che il disco sia intatto. Il fallimento puo' avvenire dopo os.replace,
        per esempio durante il fsync della directory. Il rollback deve quindi
        ripristinare comunque lo stato precedente.
        """
        if self.is_degraded(month):
            raise MonthDegradedError(month, self.degraded[month])

        _require(bool(MONTH_RE.match(month)), f"Mese {month!r} non valido")
        self.ensure_dir()

        payload = serialize_month(month, receipts)
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
        except (TypeError, ValueError) as err:
            raise StoreError(f"Serializzazione del mese {month} fallita: {err}") from err

        path = self.path_for(month)
        bak = self.bak_for(month)
        tmp_path: Path | None = None
        tmp_bak_path: Path | None = None

        try:
            # 1. Nuovo contenuto sul temporaneo, nella STESSA directory:
            #    os.replace e' atomico solo dentro lo stesso filesystem.
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.base_dir), prefix=f"{month}.", suffix=TMP_EXT
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())

            # 2. Backup della versione precedente, anch'esso atomico: una
            #    interruzione a meta' non puo' lasciare un .bak troncato,
            #    perche' il .bak esistente viene sostituito solo da un file
            #    gia' completo e sincronizzato.
            if path.exists():
                try:
                    bak_fd, tmp_bak_name = tempfile.mkstemp(
                        dir=str(self.base_dir), prefix=f"{month}.bak.", suffix=TMP_EXT
                    )
                    os.close(bak_fd)
                    tmp_bak_path = Path(tmp_bak_name)

                    shutil.copy2(str(path), str(tmp_bak_path))
                    with open(str(tmp_bak_path), "rb") as bak_handle:
                        os.fsync(bak_handle.fileno())

                    os.replace(str(tmp_bak_path), str(bak))
                    tmp_bak_path = None
                except OSError as err:
                    raise StoreError(
                        f"Backup di {path.name} non riuscito ({err}): la scrittura del "
                        f"mese {month} e' stata annullata e il file precedente resta "
                        f"intatto. Verifica spazio su disco e permessi di {self.base_dir}."
                    ) from err

            # 3. Sostituzione atomica del file principale.
            os.replace(str(tmp_path), str(path))
            tmp_path = None

            # 4. fsync della directory: rende durevoli entrambe le voci
            #    appena sostituite (.bak e file principale).
            self._fsync_dir()

        except OSError as err:
            raise StoreError(f"Scrittura del mese {month} fallita: {err}") from err
        finally:
            for leftover in (tmp_path, tmp_bak_path):
                if leftover is not None and leftover.exists():
                    try:
                        leftover.unlink()
                    except OSError:
                        _LOGGER.warning("Temporaneo %s non rimosso", leftover)

        _LOGGER.debug("Mese %s salvato: %d scontrini", month, len(receipts))

    def remove_month_file(self, month: str) -> bool:
        """Rimuove il file principale di un mese. SOLO per il recovery.

        Significato: 'questo file non esisteva nello stato precedente la
        transazione'. E' diverso dallo svuotamento intenzionale di un mese, che
        si esprime con save_month(month, []) e lascia il file con receipts: [].

        La rimozione riguarda il solo file principale: il .bak viene gestito
        separatamente da orphan_bak(), perche' lasciarlo attivo permetterebbe
        al mese di risorgere a un avvio successivo.

        Idempotente: file gia' assente -> False, nessun effetto.
        """
        path = self.path_for(month)
        if not path.exists():
            return False
        try:
            path.unlink()
            self._fsync_dir()
        except OSError as err:
            raise StoreError(
                f"Rimozione del file {path.name} durante il recovery fallita: {err}"
            ) from err
        _LOGGER.warning(
            "Recovery: %s rimosso perche' il mese non esisteva prima della "
            "transazione interrotta",
            path.name,
        )
        return True

    def orphan_bak(self, month: str) -> str | None:
        """Toglie un .bak dal namespace dei backup attivi senza cancellarlo.

        Usato quando il rollback stabilisce che un mese non esisteva nello
        stato precedente. Rinominandolo con un suffisso che load_all() non
        riconosce, il mese non puo' piu' risorgere a nessun avvio futuro, ma il
        contenuto resta sul disco per un'eventuale ispezione.

        Idempotente: se il .bak non c'e', non fa nulla.
        """
        bak = self.bak_for(month)
        if not bak.exists():
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = bak.with_name(f"{bak.name}{BAK_ORPHAN_EXT}-{stamp}")
        counter = 1
        while target.exists():
            target = bak.with_name(f"{bak.name}{BAK_ORPHAN_EXT}-{stamp}_{counter:02d}")
            counter += 1
        try:
            shutil.move(str(bak), str(target))
            self._fsync_dir()
        except OSError as err:
            raise StoreError(
                f"Messa da parte del backup {bak.name} durante il recovery fallita: {err}"
            ) from err
        _LOGGER.warning(
            "Recovery: %s messo da parte come %s. Il mese %s non esisteva prima "
            "della transazione interrotta e non verra' piu' ricaricato.",
            bak.name,
            target.name,
            month,
        )
        return str(target)

    def restore_month_snapshot(self, month: str, snapshot: dict) -> str:
        """Riporta un mese allo stato LOGICO descritto dallo snapshot.

        UNICO punto in cui uno snapshot di transazione diventa stato su disco.
        Usata sia dal rollback immediato del manager sia dal recovery via
        journal all'avvio, cosi' i due percorsi non possono divergere.

        snapshot: {"existed": bool, "receipts": [...]}

          existed=True   -> il contenuto viene riscritto, anche se vuoto: un
                            mese esistente ma senza scontrini resta un file
                            valido con receipts: []
          existed=False  -> il mese torna inesistente: il .bak eventualmente
                            prodotto dalla transazione esce dal namespace
                            attivo e il file principale viene rimosso

        Idempotente. Ritorna "restored" oppure "removed".
        """
        # Una degradazione precedente non deve impedire un ripristino: lo
        # snapshot e' per definizione l'ultima versione buona conosciuta.
        self.degraded.pop(month, None)

        if snapshot["existed"]:
            self.save_month(month, snapshot["receipts"])
            return "restored"

        self.orphan_bak(month)
        self.remove_month_file(month)
        return "removed"

    # ------------------------------------------------------- journal (WAL) #
    #
    # Protegge le transazioni che toccano piu' di un mese. Una singola
    # save_month e' gia' atomica e non ha bisogno del journal.
    #
    # Proprieta' garantita: dopo qualunque crash l'archivio rappresenta
    # integralmente lo stato PRIMA della transazione oppure integralmente
    # quello DOPO, mai una combinazione dei due.
    #
    # Politica di recovery deliberatamente conservativa: un journal presente
    # all'avvio provoca SEMPRE il rollback allo snapshot precedente, anche se
    # tutte le scritture erano riuscite e a mancare era solo la cancellazione
    # del journal. Perdere l'ultima operazione e' accettabile; conservare
    # meta' operazione no.

    def begin_transaction(self, txn_id: str, snapshots: dict[str, dict]) -> None:
        """Scrive il journal PRIMA di toccare qualunque file mensile.

        snapshots: mese -> {"existed": bool, "receipts": [...]}, fornito dal
        manager. Lo store NON deduce l'esistenza precedente dalla presenza del
        file: un mese recuperato dal .bak esiste nello stato applicativo pur
        senza file principale, e solo il manager conosce quella distinzione.
        """
        self.ensure_dir()
        if self.journal_path().exists():
            raise StoreError(
                "Journal gia' presente: una transazione precedente non e' stata "
                "conclusa. Nessuna nuova scrittura finche' non viene recuperata."
            )

        payload = {
            "journal_version": JOURNAL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "transaction_id": txn_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "months": sorted(snapshots),
            "snapshots": {
                month: {
                    "existed": bool(entry["existed"]),
                    "content": serialize_month(month, entry["receipts"]),
                }
                for month, entry in sorted(snapshots.items())
            },
        }
        self._write_atomic(
            self.journal_path(), json.dumps(payload, ensure_ascii=False, indent=2)
        )
        _LOGGER.debug("Journal %s aperto sui mesi %s", txn_id, ", ".join(sorted(snapshots)))

    def _read_journal_id(self) -> str | None:
        """Legge il solo transaction_id, senza validare tutto il journal."""
        path = self.journal_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise StoreError(f"Journal presente ma illeggibile: {err}") from err
        if not isinstance(raw, dict) or not isinstance(raw.get("transaction_id"), str):
            raise StoreError("Journal presente ma privo di un transaction_id valido")
        return raw["transaction_id"]

    def _close_journal(self, txn_id: str, action: str) -> None:
        """Rimuove il journal solo se appartiene davvero a questa transazione.

        Difesa contro un bug futuro che cancellerebbe il journal di un'altra
        transazione, distruggendo la possibilita' di recovery.
        """
        found = self._read_journal_id()

        if found is None:
            raise StoreError(
                f"{action} della transazione {txn_id}: journal atteso ma non trovato. "
                "Qualcuno lo ha rimosso durante la transazione: lo stato su disco "
                "non e' verificabile."
            )
        if found != txn_id:
            raise StoreError(
                f"{action} della transazione {txn_id}: il journal sul disco appartiene "
                f"alla transazione {found}. Il journal NON e' stato rimosso."
            )

        try:
            self.journal_path().unlink()
            self._fsync_dir()
        except OSError as err:
            raise StoreError(f"{action} del journal {txn_id} fallito: {err}") from err

    def commit_transaction(self, txn_id: str) -> None:
        """Rimuove il journal: da questo istante la transazione e' definitiva."""
        self._close_journal(txn_id, "Commit")

    def abort_transaction(self, txn_id: str) -> None:
        """Rimuove il journal dopo un rollback gia' eseguito."""
        self._close_journal(txn_id, "Abort")

    def read_journal(self) -> dict | None:
        """Legge e valida il journal. Un journal corrotto non viene ignorato.

        Solleva CorruptFileError / InvalidStructureError: il chiamante mette
        l'intero archivio in sola lettura invece di proseguire alla cieca.
        """
        path = self.journal_path()
        if not path.exists():
            return None

        raw = self._read_json(path)
        _require(isinstance(raw, dict), "journal: atteso oggetto JSON")

        version = raw.get("journal_version")
        _require(
            not isinstance(version, bool) and isinstance(version, int),
            f"journal.journal_version: atteso intero, trovato {version!r}",
        )
        _require(
            version == JOURNAL_VERSION,
            f"journal: versione {version} non supportata (attesa {JOURNAL_VERSION})",
        )

        txn_id = raw.get("transaction_id")
        _require(
            isinstance(txn_id, str) and bool(txn_id.strip()),
            "journal.transaction_id: identificativo mancante",
        )

        months = raw.get("months")
        _require(
            isinstance(months, list) and bool(months), "journal.months: lista non vuota attesa"
        )
        for month in months:
            _require(
                isinstance(month, str) and bool(MONTH_RE.match(month)),
                f"journal.months: mese non valido {month!r}",
            )

        snapshots = raw.get("snapshots")
        _require(isinstance(snapshots, dict), "journal.snapshots: atteso oggetto")
        _require(
            sorted(snapshots) == sorted(months),
            "journal: 'months' e 'snapshots' non coincidono",
        )

        for month, entry in snapshots.items():
            _require(isinstance(entry, dict), f"journal.snapshots.{month}: atteso oggetto")
            _require(
                isinstance(entry.get("existed"), bool),
                f"journal.snapshots.{month}.existed: atteso booleano",
            )
            content = entry.get("content")
            _require(
                isinstance(content, dict),
                f"journal.snapshots.{month}.content: atteso oggetto",
            )
            # Lo snapshot deve essere ricaricabile: se non lo fosse, il
            # rollback produrrebbe file invalidi.
            deserialize_month(content, month, [])

        return raw

    def rollback_journal(self, journal: dict) -> dict[str, list[str]]:
        """Riporta l'archivio allo stato LOGICO precedente la transazione.

        Delega a restore_month_snapshot, la stessa primitiva del rollback
        immediato: i due percorsi producono risultati identici.

        Il journal viene rimosso solo a ripristino completato: se il recovery
        stesso viene interrotto, al riavvio successivo riparte da capo.
        """
        restored: list[str] = []
        removed: list[str] = []

        for month in sorted(journal["snapshots"]):
            entry = journal["snapshots"][month]
            snapshot = {
                "existed": entry["existed"],
                "receipts": (
                    deserialize_month(entry["content"], month, []) if entry["existed"] else []
                ),
            }
            outcome = self.restore_month_snapshot(month, snapshot)
            (restored if outcome == "restored" else removed).append(month)

        self.commit_transaction(journal["transaction_id"])
        return {"restored": restored, "removed": removed}

    def _quarantine_journal(self, target: Path) -> str:
        """Sposta il journal nella destinazione GIA' decisa dal chiamante.

        Non sceglie nulla: nome, timestamp e gestione collisioni vengono da
        block_and_quarantine_journal, che li ha gia' scritti nel marker. Cosi'
        il percorso registrato e quello reale non possono divergere.
        """
        shutil.move(str(self.journal_path()), str(target))
        self._fsync_dir()
        return str(target)

    def block_and_quarantine_journal(self, reason: str) -> dict[str, Any]:
        """Blocca l'archivio e mette da parte un journal non interpretabile.

        ORDINE OBBLIGATORIO, ed e' la ragione per cui questa primitiva esiste:

          1. hash del journal, finche' e' ancora al suo posto
          2. scelta della destinazione DEFINITIVA, collisioni comprese
          3. scrittura atomica e sincronizzata del marker, con quel percorso
          4. solo DOPO, spostamento del journal esattamente li'

        Invertire 3 e 4 non e' crash-safe: un processo che muore fra la
        quarantena e la scrittura del marker lascerebbe l'archivio senza
        journal e senza blocco, cioe' apparentemente sano pur potendo
        contenere una transazione parziale.

        Se la scrittura del marker fallisce, il journal NON viene spostato: al
        prossimo avvio .transaction.json resta visibile e il recovery non puo'
        ignorarlo.

        Ritorna {"journal_path", "journal_sha256", "blocked", "moved"}.
        """
        path = self.journal_path()
        digest: str | None = None
        if path.exists():
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as err:
                _LOGGER.warning("Hash del journal non calcolabile: %s", err)

        # Destinazione definitiva, decisa qui una volta sola: e' il percorso
        # che finisce nel marker ed e' lo stesso che ricevera' il file.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = path.with_name(f"{JOURNAL_NAME}{JOURNAL_ORPHAN_EXT}-{stamp}")
        counter = 1
        while target.exists():
            target = path.with_name(f"{JOURNAL_NAME}{JOURNAL_ORPHAN_EXT}-{stamp}_{counter:02d}")
            counter += 1

        self.write_blocked_marker(
            reason=reason, journal_path=str(target), journal_sha256=digest
        )

        if not path.exists():
            return {
                "journal_path": None,
                "journal_sha256": digest,
                "blocked": True,
                "moved": False,
            }

        try:
            quarantined = self._quarantine_journal(target)
        except OSError as err:
            # Il marker e' gia' durevole: non viene riscritto, per preservare
            # la causa iniziale. Il journal resta dov'era, che e' sicuro.
            _LOGGER.error(
                "Marker di blocco scritto ma journal NON spostato (%s). Il journal si "
                "trova ancora in %s, non in %s come indicato dal marker. L'archivio "
                "resta bloccato e il journal verra' rivalutato al prossimo avvio.",
                err,
                path,
                target,
            )
            return {
                "journal_path": str(path),
                "journal_sha256": digest,
                "blocked": True,
                "moved": False,
            }

        return {
            "journal_path": quarantined,
            "journal_sha256": digest,
            "blocked": True,
            "moved": True,
        }

    # ------------------------------------------------- blocco persistente #
    #
    # Un journal non interpretabile significa che non sappiamo se sul disco ci
    # sia una transazione parziale. Quel dubbio non puo' essere risolto da un
    # riavvio: viene scritto un marker che blocca ogni scrittura finche' non
    # si interviene esplicitamente.

    def read_blocked_marker(self) -> dict | None:
        """Legge il marker di blocco.

        Un marker illeggibile blocca comunque: in caso di dubbio si resta
        fermi. Non solleva mai, per non impedire l'avvio dell'integrazione.
        """
        path = self.blocked_marker_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.error("Marker di blocco illeggibile (%s): il blocco resta attivo", err)
            return {
                "blocked_marker_version": BLOCKED_MARKER_VERSION,
                "reason": "marker di blocco presente ma illeggibile",
                "created_at": None,
                "journal_path": None,
            }
        if not isinstance(raw, dict):
            return {
                "blocked_marker_version": BLOCKED_MARKER_VERSION,
                "reason": "marker di blocco con contenuto inatteso",
                "created_at": None,
                "journal_path": None,
            }
        return raw

    def write_blocked_marker(
        self, *, reason: str, journal_path: str | None, journal_sha256: str | None = None
    ) -> None:
        """Scrive atomicamente il marker di blocco. Non sovrascrive il primo.

        Il marker originale descrive la causa iniziale: un secondo avvio non
        deve riscriverlo con informazioni derivate.
        """
        path = self.blocked_marker_path()
        if path.exists():
            return
        payload = {
            "blocked_marker_version": BLOCKED_MARKER_VERSION,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "reason": reason,
            "journal_path": journal_path,
            "journal_sha256": journal_sha256,
            "how_to_resolve": (
                "Ispeziona i file mensili in questa cartella e il journal "
                "quarantinato, correggi eventuali incoerenze, poi chiama il "
                "servizio spesa.sblocca_archivio per rimuovere questo blocco."
            ),
        }
        self._write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))
        _LOGGER.error(
            "Archivio spesa BLOCCATO in sola lettura: %s. Marker scritto in %s.",
            reason,
            path,
        )

    def clear_blocked_marker(self) -> bool:
        """Rimuove il blocco. Solo su azione esplicita dell'utente."""
        path = self.blocked_marker_path()
        if not path.exists():
            return False
        try:
            path.unlink()
            self._fsync_dir()
        except OSError as err:
            raise StoreError(f"Rimozione del marker di blocco fallita: {err}") from err
        _LOGGER.warning("Blocco dell'archivio rimosso su richiesta esplicita")
        return True
