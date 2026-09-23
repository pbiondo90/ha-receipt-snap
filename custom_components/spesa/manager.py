"""Livello transazionale dell'integrazione Spesa alimentare.

Responsabilita':

  * stato in memoria: mesi -> scontrini, piu' gli indici derivati
  * UNICO asyncio.Lock: ogni mutazione passa da qui, senza eccezioni
  * transazioni multi-mese protette dal journal, con rollback completo
  * invarianti globali: receipt_id unico, fingerprint forte unico
  * riconciliazione simmetrica dei possibili duplicati
  * spostamento fra file mensili al cambio data
  * aggregazioni e statistiche, tutte dai dati persistiti

MODELLO TRANSAZIONALE
---------------------
Nessuna mutazione tocca lo stato pubblicato. Ogni operazione:

  1. apre una transazione che deep-copia SOLO i mesi coinvolti
  2. applica le modifiche alle copie di lavoro
  3. verifica le invarianti globali sullo stato risultante
  4. scrive su disco tutti i mesi modificati
  5. solo a scrittura completata sostituisce lo stato in memoria
     e ricostruisce gli indici

CONTRATTO DEGLI ERRORI DI PERSISTENZA
-------------------------------------
  StoreError        scrittura fallita, stato precedente ripristinato,
                    operazione NON applicata, ritentare e' sicuro
  ConsistencyError  stato non verificabile, archivio in sola lettura,
                    NON ritentare finche' non e' risolto

Una funzione di I/O che solleva NON garantisce che il disco sia intatto: dopo
os.replace il file nuovo e' gia' visibile. Per questo il rollback ripristina
sempre l'intera transazione, e non solo i mesi che sappiamo di aver scritto.

Nessuna dipendenza dal recorder: lo storico vive nei file mensili.
"""

from __future__ import annotations

import asyncio
import calendar
import copy
import logging
import uuid
from collections import defaultdict
from datetime import date as date_cls, datetime
from decimal import Decimal
from functools import partial
from typing import Any, Iterator

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    ITEM_TRANSIENT_FIELDS,
    MONTHLY_HISTORY_MONTHS,
    RECENT_RECEIPTS_LIMIT,
    REVIEW_LABELS,
    SIGNAL_UPDATED,
)
from .model import (
    ValidationError,
    apply_duplicate_matches,
    apply_item_field,
    coerce_receipt_field,
    month_key,
    recompute,
    touch,
    validate_payload,
)
from .store import LoadResult, MonthDegradedError, SpesaStore, StoreError

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Eccezioni
# --------------------------------------------------------------------------- #


class ManagerError(Exception):
    """Errore del livello transazionale."""


class NotFoundError(ManagerError):
    """Scontrino o articolo inesistente."""


class DuplicateReceiptError(ManagerError):
    """Violazione dell'unicita' globale: stesso receipt_id o stesso fingerprint."""

    def __init__(self, existing_receipt_id: str, reason: str) -> None:
        self.existing_receipt_id = existing_receipt_id
        self.reason = reason
        super().__init__(f"Duplicato di {existing_receipt_id} ({reason})")


class ConsistencyError(ManagerError):
    """Stato su disco incerto dopo un fallimento non compensabile."""


# --------------------------------------------------------------------------- #
# Utilita' di calendario
# --------------------------------------------------------------------------- #


def days_in_month(month: str) -> int:
    year, mon = int(month[:4]), int(month[5:7])
    return calendar.monthrange(year, mon)[1]


def previous_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    return f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"


def month_range(latest: str, count: int) -> list[str]:
    """Elenco di `count` mesi terminante in `latest`, dal piu' vecchio."""
    months = [latest]
    for _ in range(count - 1):
        months.append(previous_month(months[-1]))
    return list(reversed(months))


def month_label(month: str) -> str:
    names = (
        "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
        "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre",
    )
    return f"{names[int(month[5:7]) - 1]} {month[:4]}"


def _f(value: Decimal | None) -> float | None:
    """Decimal -> float al confine verso le entita'. Mai per fare aritmetica."""
    return None if value is None else float(value)


# --------------------------------------------------------------------------- #
# Transazione
# --------------------------------------------------------------------------- #


class _Txn:
    """Copie di lavoro dei mesi coinvolti in una mutazione.

    I mesi vengono deep-copiati alla prima richiesta. Le letture su mesi non
    ancora coinvolti attingono allo stato pubblicato, cosi' i controlli globali
    vedono sempre lo stato completo senza copiare l'intero archivio.
    """

    def __init__(self, manager: SpesaManager) -> None:
        self._manager = manager
        self.working: dict[str, list[dict]] = {}

    def open(self, month: str) -> list[dict]:
        """Porta un mese nella transazione, rifiutando i mesi non scrivibili."""
        if month not in self.working:
            self._manager.assert_writable(month)
            self.working[month] = copy.deepcopy(self._manager.months.get(month, []))
        return self.working[month]

    def snapshot(self, month: str) -> list[dict]:
        """Lettura senza portare il mese nella transazione."""
        if month in self.working:
            return self.working[month]
        return self._manager.months.get(month, [])

    def iter_all(self) -> Iterator[tuple[str, dict]]:
        """Tutti gli scontrini nello stato risultante dalla transazione."""
        months = set(self._manager.months) | set(self.working)
        for month in sorted(months):
            for receipt in self.snapshot(month):
                yield month, receipt

    def locate(self, receipt_id: str) -> tuple[str, dict]:
        """Trova uno scontrino nello stato della transazione."""
        for month, receipt in self.iter_all():
            if receipt["receipt_id"] == receipt_id:
                return month, receipt
        raise NotFoundError(f"Scontrino {receipt_id!r} inesistente")

    def open_containing(self, receipt_id: str) -> tuple[str, dict]:
        """Come locate(), ma porta il mese nella transazione per modificarlo."""
        month, _ = self.locate(receipt_id)
        for receipt in self.open(month):
            if receipt["receipt_id"] == receipt_id:
                return month, receipt
        raise NotFoundError(f"Scontrino {receipt_id!r} inesistente")


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #


class SpesaManager:
    """Stato, transazioni e statistiche."""

    def __init__(self, hass: HomeAssistant, store: SpesaStore) -> None:
        self.hass = hass
        self.store = store
        self._lock = asyncio.Lock()

        # Stato pubblicato: sostituito solo a scrittura completata.
        self.months: dict[str, list[dict]] = {}

        # Indici derivati: unica fonte di verita' restano self.months.
        self._month_by_id: dict[str, str] = {}
        self._id_by_strong: dict[str, str] = {}
        self._ids_by_weak: dict[str, set[str]] = defaultdict(set)

        self.load_results: list[LoadResult] = []
        self.invariant_violations: list[dict[str, Any]] = []
        self.recovery_report: dict[str, Any] | None = None
        self.journal_blocked: str | None = None

    # ------------------------------------------------------------- indici #

    def rebuild_indexes(self) -> list[dict[str, Any]]:
        """Ricostruisce gli indici e RIPORTA le violazioni delle invarianti.

        Non risolve nulla e non scarta nulla: decidere quale di due scontrini
        in conflitto sia 'quello buono' non spetta al codice. Entrambi restano
        sul disco intatti e il chiamante degrada i mesi coinvolti.
        """
        self._month_by_id = {}
        self._id_by_strong = {}
        self._ids_by_weak = defaultdict(set)

        by_id: dict[str, list[str]] = defaultdict(list)
        by_strong: dict[str, list[tuple[str, str]]] = defaultdict(list)

        for month in sorted(self.months):
            for receipt in self.months[month]:
                receipt_id = receipt["receipt_id"]
                by_id[receipt_id].append(month)
                by_strong[receipt["fingerprint"]].append((receipt_id, month))
                self._month_by_id.setdefault(receipt_id, month)
                self._id_by_strong.setdefault(receipt["fingerprint"], receipt_id)
                self._ids_by_weak[receipt["fingerprint_weak"]].add(receipt_id)

        violations: list[dict[str, Any]] = []

        for receipt_id, months in sorted(by_id.items()):
            if len(months) > 1:
                violations.append(
                    {
                        "kind": "duplicate_receipt_id",
                        "receipt_id": receipt_id,
                        "months": sorted(set(months)),
                        "detail": (
                            f"Il receipt_id {receipt_id} compare in piu' file mensili: "
                            f"{', '.join(sorted(set(months)))}"
                        ),
                    }
                )

        for _strong, entries in sorted(by_strong.items()):
            distinct = {receipt_id for receipt_id, _ in entries}
            if len(entries) > 1 and len(distinct) > 1:
                violations.append(
                    {
                        "kind": "duplicate_fingerprint",
                        "receipt_ids": sorted(distinct),
                        "months": sorted({month for _, month in entries}),
                        "detail": (
                            "Scontrini identici articolo per articolo: "
                            + ", ".join(sorted(distinct))
                        ),
                    }
                )

        return violations

    def assert_writable(self, month: str) -> None:
        """Verifica che il mese possa essere scritto.

        Il blocco globale ha la precedenza: finche' l'archivio e' in sola
        lettura nessun mese e' scrivibile, degradato o meno.
        """
        if self.journal_blocked:
            raise ManagerError(self.journal_blocked)
        if self.store.is_degraded(month):
            raise MonthDegradedError(month, self.store.degraded[month])

    @property
    def degraded_months(self) -> dict[str, str]:
        return dict(self.store.degraded)

    # ------------------------------------------------------------ startup #

    async def _async_recover_journal(self) -> dict[str, Any] | None:
        """Recovery all'avvio. Il blocco persistente ha la precedenza su tutto."""

        # 1. Un blocco preesistente non viene mai risolto da un riavvio.
        marker = await self.hass.async_add_executor_job(self.store.read_blocked_marker)
        if marker is not None:
            self.journal_blocked = (
                f"Archivio in sola lettura dal {marker.get('created_at') or 'avvio precedente'}: "
                f"{marker.get('reason')}. "
                f"Journal conservato in {marker.get('journal_path') or 'nessun percorso registrato'}. "
                "Risolvi manualmente e chiama spesa.sblocca_archivio."
            )
            _LOGGER.error(self.journal_blocked)
            return {"status": "blocked", "marker": marker, "detail": self.journal_blocked}

        # 2. Journal illeggibile: blocco durevole PRIMA della quarantena.
        try:
            journal = await self.hass.async_add_executor_job(self.store.read_journal)
        except StoreError as err:
            reason = f"journal di transazione non interpretabile: {err}"
            outcome = await self.hass.async_add_executor_job(
                self.store.block_and_quarantine_journal, reason
            )
            self.journal_blocked = (
                f"Archivio in sola lettura: {reason}. "
                f"Journal conservato in {outcome['journal_path'] or 'nessun percorso'}"
                + ("" if outcome["moved"] else " (non e' stato possibile spostarlo)")
                + ". Non e' possibile stabilire quali mesi ripristinare: il blocco "
                "resta attivo anche dopo un riavvio, finche' non chiami "
                "spesa.sblocca_archivio."
            )
            _LOGGER.error(self.journal_blocked)
            return {
                "status": "blocked",
                "detail": self.journal_blocked,
                "journal_path": outcome["journal_path"],
            }

        if journal is None:
            return None

        # 3. Journal valido: rollback conservativo.
        txn_id = journal["transaction_id"]
        months = sorted(journal["snapshots"])
        _LOGGER.warning(
            "Transazione %s interrotta (mesi: %s). Rollback allo stato precedente.",
            txn_id,
            ", ".join(months),
        )
        try:
            outcome = await self.hass.async_add_executor_job(
                self.store.rollback_journal, journal
            )
        except StoreError as err:
            self.journal_blocked = (
                f"Rollback della transazione {txn_id} fallito: {err}. Il journal resta "
                "sul disco e il ripristino verra' ritentato al prossimo avvio."
            )
            _LOGGER.error(self.journal_blocked)
            return {"status": "failed", "transaction_id": txn_id, "detail": str(err)}

        _LOGGER.warning(
            "Recovery completato: %d mesi ripristinati (%s), %d rimossi perche' "
            "inesistenti prima della transazione (%s). L'operazione in corso al "
            "momento dell'interruzione e' andata persa.",
            len(outcome["restored"]),
            ", ".join(outcome["restored"]) or "nessuno",
            len(outcome["removed"]),
            ", ".join(outcome["removed"]) or "nessuno",
        )
        return {
            "status": "rolled_back",
            "transaction_id": txn_id,
            "restored": outcome["restored"],
            "removed": outcome["removed"],
        }

    async def async_load(self) -> None:
        """Recovery, caricamento dei mesi e validazione delle invarianti globali."""
        self.journal_blocked = None
        self.recovery_report = await self._async_recover_journal()

        expect_absent = set((self.recovery_report or {}).get("removed") or [])
        results = await self.hass.async_add_executor_job(
            partial(self.store.load_all, expect_absent=expect_absent)
        )

        self.months = {r.month: r.receipts for r in results if not r.degraded}
        for result in results:
            if result.degraded:
                self.months.setdefault(result.month, [])
        self.load_results = results

        # Le invarianti globali si verificano DOPO il caricamento dei singoli
        # file: una collisione fra mesi diversi e' invisibile a chi legge un
        # file alla volta.
        self.invariant_violations = self.rebuild_indexes()
        if self.invariant_violations:
            affected: set[str] = set()
            for violation in self.invariant_violations:
                affected |= set(violation["months"])
                _LOGGER.error("Invariante violata: %s", violation["detail"])
            for month in sorted(affected):
                self.store.degraded[month] = (
                    "invariante globale violata: i dati di questo mese sono esclusi "
                    "dalle statistiche e le scritture sono bloccate finche' il "
                    "conflitto non viene risolto sui file"
                )
                # Escluso dai dati economici: non 'quasi giusto', proprio fuori.
                self.months[month] = []
            _LOGGER.error(
                "%d mesi esclusi dalle statistiche per violazione delle invarianti: %s. "
                "I file sul disco NON sono stati modificati. Correggili e chiama "
                "spesa.ricalcola.",
                len(affected),
                ", ".join(sorted(affected)),
            )
            self.rebuild_indexes()

        total = sum(len(r) for r in self.months.values())
        _LOGGER.info(
            "Archivio spesa caricato: %d scontrini in %d mesi%s",
            total,
            len(self.months),
            f", {len(self.store.degraded)} mesi non utilizzabili" if self.store.degraded else "",
        )

    # --------------------------------------------------------------- commit #

    def _transaction_snapshot(self, months: list[str]) -> dict[str, dict]:
        """Stato LOGICO precedente dei mesi coinvolti.

        `existed` deriva dalla presenza del mese in self.months, cioe' dallo
        stato autorevole dell'archivio, non dall'esistenza del file .json:

          mese mai esistito            -> existed=False
          mese esistente ma vuoto      -> existed=True,  receipts=[]
          mese recuperato dal .bak     -> existed=True
          mese con scontrini           -> existed=True
        """
        return {
            month: {
                "existed": month in self.months,
                "receipts": self.months.get(month, []),
            }
            for month in months
        }

    async def _commit(self, txn: _Txn, *, changed_months: set[str]) -> None:
        """Persiste i mesi modificati e pubblica lo stato solo a buon fine."""
        if not changed_months:
            return

        ordered = sorted(changed_months)
        for month in ordered:
            self.assert_writable(month)

        if len(ordered) == 1:
            await self._commit_single(txn, ordered[0])
        else:
            await self._commit_journaled(txn, ordered)

        # Pubblicazione: nessun await fra la sostituzione dei mesi e la
        # ricostruzione degli indici, cosi' nessun lettore vede stati misti.
        for month in ordered:
            self.months[month] = txn.working[month]
        violations = self.rebuild_indexes()
        if violations:
            _LOGGER.error(
                "Invarianti violate dopo un commit riuscito: %s. Questo indica un bug "
                "nei controlli pre-commit.",
                "; ".join(v["detail"] for v in violations[:3]),
            )
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    async def _commit_single(self, txn: _Txn, month: str) -> None:
        """Scrittura di un solo mese, con ripristino conservativo in caso di errore.

        Il journal non serve: save_month e' atomica, quindi il file resta
        interamente vecchio o interamente nuovo e non esiste mezzo mese. Serve
        pero' lo stesso ripristino del ramo multi-mese, perche' vale la stessa
        regola: save_month puo' fallire DOPO os.replace - per esempio durante
        il fsync della directory - lasciando sul disco lo stato nuovo mentre
        self.months conserva ancora quello vecchio.

        Ripristinare e' sempre sicuro:
          fallimento prima di os.replace -> riscrive lo stesso stato precedente
          fallimento dopo os.replace     -> rimuove davvero lo stato nuovo
          mese prima inesistente         -> torna inesistente
        """
        snapshot = self._transaction_snapshot([month])[month]

        try:
            await self.hass.async_add_executor_job(
                self.store.save_month, month, txn.working[month]
            )
        except (StoreError, OSError) as err:
            _LOGGER.error(
                "Scrittura del mese %s fallita: %s. Ripristino dello stato precedente.",
                month,
                err,
            )
            try:
                await self.hass.async_add_executor_job(
                    self.store.restore_month_snapshot, month, snapshot
                )
            except (StoreError, OSError) as rollback_err:
                reason = (
                    f"Scrittura del mese {month} fallita ({err}) e anche il ripristino "
                    f"dello stato precedente e' fallito ({rollback_err}). Lo stato su "
                    "disco non e' verificabile: l'archivio e' in sola lettura."
                )
                self.store.degraded[month] = reason
                await self._async_persist_block(reason)
                raise ConsistencyError(reason) from err

            # Ripristino riuscito: disco e RAM tornano coerenti sullo stato
            # precedente. L'operazione non e' stata applicata, e il chiamante
            # puo' ritentarla in sicurezza.
            _LOGGER.warning(
                "Mese %s riportato allo stato precedente: l'operazione e' stata "
                "annullata e puo' essere ripetuta.",
                month,
            )
            raise

    async def _commit_journaled(self, txn: _Txn, ordered: list[str]) -> None:
        """Transazione multi-mese protetta dal journal.

          1. journal con lo snapshot PRECEDENTE di tutti i mesi coinvolti
          2. scrittura dei mesi, uno a uno
          3. rimozione del journal

        Crash fra 1 e 3: al riavvio il journal e' presente e l'archivio viene
        riportato per intero allo stato precedente. Crash dopo 3: la
        transazione e' completa su disco.
        """
        txn_id = f"{int(dt_util.utcnow().timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"
        snapshots = self._transaction_snapshot(ordered)

        await self.hass.async_add_executor_job(
            self.store.begin_transaction, txn_id, snapshots
        )

        # `completed` serve SOLO al log. Non decide cosa ripristinare.
        #
        # save_month() puo' fallire DOPO os.replace() - per esempio durante il
        # fsync della directory - quindi un mese che non ha completato la
        # chiamata puo' comunque avere il file nuovo sul disco. Un'eccezione
        # non garantisce "non ho modificato nulla".
        #
        # Il journal registra lo stato precedente dell'INTERA transazione, e in
        # caso di dubbio si ripristina l'intera transazione: il rollback e'
        # idempotente, quindi riscrivere lo snapshot di un mese mai toccato non
        # ha alcun costo oltre una scrittura.
        completed = 0
        try:
            for month in ordered:
                await self.hass.async_add_executor_job(
                    self.store.save_month, month, txn.working[month]
                )
                completed += 1
        except (StoreError, OSError) as err:
            _LOGGER.error(
                "Transazione %s fallita dopo %d/%d salvataggi completati: %s. "
                "Rollback di TUTTI i mesi coinvolti.",
                txn_id,
                completed,
                len(ordered),
                err,
            )
            outcome = await self._rollback_journaled(txn_id, ordered, snapshots, closing=True)

            if not outcome["restored_all"]:
                reason = (
                    f"Transazione {txn_id}: ripristino non riuscito sui mesi "
                    + ", ".join(outcome["failed"])
                    + ". Lo stato su disco non e' verificabile e l'archivio e' in "
                    "sola lettura."
                )
                if await self._async_journal_is_reliable(txn_id):
                    self.journal_blocked = reason + " Il recovery all'avvio lo risolvera'."
                    _LOGGER.error(self.journal_blocked)
                else:
                    await self._async_persist_block(reason)
                raise ConsistencyError(self.journal_blocked or reason) from err

            # Rollback completo: l'operazione e' stata annullata e lo stato
            # precedente e' tornato autorevole. Errore di persistenza normale.
            raise

        # Scritture completate. Finche' la chiusura del journal non e'
        # confermata, sul disco possono convivere lo stato nuovo e un WAL che
        # descrive quello precedente: nessun'altra mutazione e' ammessa.
        try:
            await self.hass.async_add_executor_job(self.store.commit_transaction, txn_id)
        except StoreError as err:
            base_reason = (
                f"Transazione {txn_id}: le scritture sono riuscite ma la chiusura del "
                f"journal e' fallita ({err})."
            )
            self.journal_blocked = base_reason + " Archivio in sola lettura."
            _LOGGER.error(self.journal_blocked)

            # Tutti i mesi sono stati scritti, inclusi quelli creati ex novo
            # dalla transazione: il rollback rispetta `existed` su ognuno.
            outcome = await self._rollback_journaled(txn_id, ordered, snapshots, closing=False)
            reliable = await self._async_journal_is_reliable(txn_id)

            if outcome["restored_all"]:
                if reliable:
                    self.journal_blocked = (
                        base_reason
                        + " L'archivio e' stato riportato allo stato precedente e "
                        "l'operazione annullata. Riavvia Home Assistant o chiama "
                        "spesa.ricalcola per riprendere le scritture."
                    )
                    _LOGGER.warning(self.journal_blocked)
                    raise ConsistencyError(self.journal_blocked) from err

                if outcome["journal_closed"] or outcome["journal_absent"]:
                    # Dati ripristinati e nessun journal pertinente: stato
                    # certo, archivio di nuovo operativo.
                    self.journal_blocked = None
                    _LOGGER.warning(
                        "%s L'archivio e' stato riportato allo stato precedente e "
                        "l'operazione annullata. Le scritture riprendono normalmente.",
                        base_reason,
                    )
                    raise  # errore di persistenza, operazione annullata

                reason = (
                    base_reason
                    + " I mesi sono stati ripristinati, ma sul disco resta un journal "
                    "non attribuibile a questa transazione. L'archivio e' in sola "
                    "lettura: ispeziona /config/spesa e usa spesa.sblocca_archivio."
                )
                await self._async_persist_block(reason)
                raise ConsistencyError(reason) from err

            reason = (
                base_reason
                + " Il ripristino non e' riuscito sui mesi "
                + ", ".join(outcome["failed"])
                + ". NON ripetere l'operazione: potrebbe essere gia' stata applicata. "
                "Verifica /config/spesa prima di procedere."
            )
            if reliable:
                self.journal_blocked = reason + " Il recovery all'avvio lo risolvera'."
                _LOGGER.error(self.journal_blocked)
                raise ConsistencyError(self.journal_blocked) from err

            await self._async_persist_block(reason)
            raise ConsistencyError(reason) from err

    async def _rollback_journaled(
        self,
        txn_id: str,
        months: list[str],
        snapshots: dict[str, dict],
        *,
        closing: bool,
    ) -> dict[str, Any]:
        """Riporta allo stato LOGICO precedente TUTTI i mesi della transazione.

        `months` sono i mesi coinvolti, non quelli che hanno certamente
        completato la scrittura: una funzione di I/O che solleva non garantisce
        di non aver modificato nulla, in particolare dopo os.replace().
        Ripristinare un mese mai toccato e' innocuo, perche'
        restore_month_snapshot e' idempotente; lasciarne fuori uno gia'
        modificato lascerebbe meta' transazione sul disco.

        Distingue nettamente il ripristino dei dati dalla chiusura del journal:
        un ripristino riuscito non viene classificato come fallito solo perche'
        il journal era gia' scomparso.

        Non solleva: restituisce l'esito e lascia decidere al chiamante.
        Non modifica self.months, che rappresenta gia' lo stato precedente.
        """
        failed: list[str] = []
        restored: list[str] = []
        removed: list[str] = []

        for month in months:
            snapshot = snapshots.get(month)
            if snapshot is None:
                failed.append(month)
                self.store.degraded[month] = (
                    "rollback impossibile: snapshot della transazione mancante"
                )
                _LOGGER.error(
                    "Transazione %s: nessuno snapshot per il mese %s, ripristino "
                    "impossibile.",
                    txn_id,
                    month,
                )
                continue
            try:
                outcome = await self.hass.async_add_executor_job(
                    self.store.restore_month_snapshot, month, snapshot
                )
            except (StoreError, OSError) as err:
                failed.append(month)
                self.store.degraded[month] = f"rollback fallito: {err}"
                _LOGGER.error("Rollback fallito sul mese %s: %s", month, err)
            else:
                (restored if outcome == "restored" else removed).append(month)
                _LOGGER.warning(
                    "Mese %s %s",
                    month,
                    "riportato al contenuto precedente"
                    if outcome == "restored"
                    else "rimosso: non esisteva prima della transazione",
                )

        result: dict[str, Any] = {
            "restored": restored,
            "removed": removed,
            "failed": failed,
            "restored_all": not failed,
            "journal_closed": False,
            "journal_absent": False,
            "journal_foreign": False,
        }

        # Chiusura del journal: operazione INDIPENDENTE dall'esito sui dati.
        if closing:
            try:
                journal_id = await self.hass.async_add_executor_job(
                    self.store._read_journal_id
                )
            except StoreError as err:
                _LOGGER.error(
                    "Transazione %s: journal presente ma illeggibile durante il "
                    "rollback (%s). Non verra' toccato.",
                    txn_id,
                    err,
                )
                return result

            if journal_id is None:
                result["journal_absent"] = True
                _LOGGER.warning(
                    "Transazione %s: journal gia' assente al momento della chiusura. "
                    "I dati %s stati ripristinati correttamente.",
                    txn_id,
                    "sono" if result["restored_all"] else "NON sono tutti",
                )
            elif journal_id != txn_id:
                result["journal_foreign"] = True
                _LOGGER.error(
                    "Transazione %s: sul disco c'e' il journal della transazione %s. "
                    "NON verra' rimosso.",
                    txn_id,
                    journal_id,
                )
            else:
                try:
                    await self.hass.async_add_executor_job(
                        self.store.abort_transaction, txn_id
                    )
                    result["journal_closed"] = True
                except StoreError as err:
                    _LOGGER.error(
                        "Transazione %s: rimozione del journal fallita (%s). I dati %s "
                        "stati ripristinati; il recovery all'avvio ripetera' "
                        "l'operazione, che e' idempotente.",
                        txn_id,
                        err,
                        "sono" if result["restored_all"] else "NON sono tutti",
                    )

        if failed:
            _LOGGER.error(
                "Transazione %s: mesi non ripristinati: %s", txn_id, ", ".join(failed)
            )
        return result

    async def _async_journal_is_reliable(self, txn_id: str) -> bool:
        """Verifica se sul disco resta un journal valido di QUESTA transazione.

        Solo in quel caso il recovery all'avvio risolvera' da solo: se il
        journal e' assente, illeggibile o appartiene ad altra transazione,
        serve il marker persistente.
        """
        try:
            found = await self.hass.async_add_executor_job(self.store._read_journal_id)
        except StoreError:
            return False
        return found == txn_id

    async def _async_persist_block(self, reason: str) -> None:
        """Blocca l'archivio in modo che il blocco sopravviva a un riavvio.

        Da usare quando lo stato su disco e' incerto E non esiste un journal
        valido che garantisca il recovery automatico al prossimo avvio. Per il
        caso di journal illeggibile si usa invece
        store.block_and_quarantine_journal, che gestisce anche la quarantena
        nell'ordine corretto.
        """
        self.journal_blocked = reason
        try:
            await self.hass.async_add_executor_job(
                partial(self.store.write_blocked_marker, reason=reason, journal_path=None)
            )
        except StoreError as err:
            _LOGGER.critical(
                "Impossibile scrivere il marker di blocco (%s). L'archivio resta in "
                "sola lettura in questa sessione, ma il blocco NON sopravvivera' a un "
                "riavvio: verifica manualmente lo stato di /config/spesa prima di "
                "riavviare Home Assistant. Causa originale: %s",
                err,
                reason,
            )

    # -------------------------------------------------- invarianti globali #

    def _assert_unique(self, txn: _Txn) -> None:
        """Verifica receipt_id e fingerprint forte unici su TUTTI i mesi.

        Il controllo e' globale per costruzione: iter_all() attraversa lo stato
        risultante dell'intera transazione, non il singolo file mensile. Viene
        eseguito PRIMA del commit, quindi una modifica che renderebbe due
        scontrini identici viene respinta senza scrivere nulla.
        """
        seen_ids: dict[str, str] = {}
        seen_strong: dict[str, str] = {}

        for month, receipt in txn.iter_all():
            receipt_id = receipt["receipt_id"]
            if receipt_id in seen_ids:
                raise DuplicateReceiptError(
                    receipt_id,
                    f"receipt_id presente in {seen_ids[receipt_id]} e in {month}",
                )
            seen_ids[receipt_id] = month

            strong = receipt["fingerprint"]
            if strong in seen_strong:
                raise DuplicateReceiptError(
                    seen_strong[strong],
                    "la modifica renderebbe questo scontrino identico, articolo "
                    f"per articolo, a {seen_strong[strong]}",
                )
            seen_strong[strong] = receipt_id

    # --------------------------------------------- riconciliazione duplicati #

    @staticmethod
    def _fingerprint_snapshot(txn: _Txn, receipt_ids: set[str]) -> dict[str, tuple[str, str]]:
        """(strong, weak) prima della mutazione, per gli scontrini indicati."""
        return {
            receipt["receipt_id"]: (receipt["fingerprint"], receipt["fingerprint_weak"])
            for _, receipt in txn.iter_all()
            if receipt["receipt_id"] in receipt_ids
        }

    def _reconcile_duplicates(
        self, txn: _Txn, touched_ids: set[str], before: dict[str, tuple[str, str]]
    ) -> set[str]:
        """Rivaluta i possibili duplicati in modo simmetrico.

        `before` mappa receipt_id -> (strong, weak) prima della mutazione.
        Serve a due cose: individuare i gruppi deboli abbandonati, e stabilire
        se un dismissal debba decadere perche' i dati verificati sono cambiati.

        Se A smette di somigliare a B, anche B smette di indicare A: la
        rivalutazione copre i membri dei gruppi lasciati e raggiunti, oltre a
        chi citava uno degli scontrini modificati.
        """
        weak_groups: set[str] = {weak for _, weak in before.values()}
        for _, receipt in txn.iter_all():
            if receipt["receipt_id"] in touched_ids:
                weak_groups.add(receipt["fingerprint_weak"])

        candidates: set[str] = set(touched_ids)
        for _, receipt in txn.iter_all():
            if receipt["fingerprint_weak"] in weak_groups:
                candidates.add(receipt["receipt_id"])
            if touched_ids & set(receipt.get("possible_duplicate_of") or []):
                candidates.add(receipt["receipt_id"])

        # Chi ha cambiato impronta: il proprio dismissal decade, e decade anche
        # quello di chi lo cita, perche' la relazione verificata non e' piu'
        # la stessa.
        fingerprint_changed: set[str] = set()
        for receipt_id, (old_strong, old_weak) in before.items():
            try:
                _, receipt = txn.locate(receipt_id)
            except NotFoundError:
                fingerprint_changed.add(receipt_id)
                continue
            if (receipt["fingerprint"], receipt["fingerprint_weak"]) != (old_strong, old_weak):
                fingerprint_changed.add(receipt_id)

        by_weak: dict[str, list[dict]] = defaultdict(list)
        for _, receipt in txn.iter_all():
            by_weak[receipt["fingerprint_weak"]].append(receipt)

        changed_months: set[str] = set()
        for receipt_id in sorted(candidates):
            try:
                month, receipt = txn.locate(receipt_id)
            except NotFoundError:
                continue  # eliminato nella stessa transazione

            matches = sorted(
                other["receipt_id"]
                for other in by_weak.get(receipt["fingerprint_weak"], [])
                if other["receipt_id"] != receipt_id
                and other["fingerprint"] != receipt["fingerprint"]
            )
            current = sorted(receipt.get("possible_duplicate_of") or [])

            invalidate = bool(
                fingerprint_changed & ({receipt_id} | set(matches) | set(current))
            )
            if matches == current and not invalidate:
                continue

            # Il mese entra nella transazione solo se cambia davvero.
            receipts = txn.open(month)
            target = next(r for r in receipts if r["receipt_id"] == receipt_id)
            if apply_duplicate_matches(target, matches, invalidate_dismissal=invalidate):
                recompute(target)
                changed_months.add(month)
                _LOGGER.debug(
                    "Possibili duplicati di %s: %s -> %s%s",
                    receipt_id,
                    current or "nessuno",
                    matches or "nessuno",
                    " (verifica manuale invalidata)" if invalidate else "",
                )

        return changed_months

    # ------------------------------------------------------------- ingest #

    async def async_ingest(self, payload: Any) -> dict[str, Any]:
        """Valida e registra uno scontrino ricevuto dall'endpoint HTTP.

        Solleva ValidationError, DuplicateReceiptError, MonthDegradedError,
        ManagerError, StoreError.
        """
        async with self._lock:
            now = dt_util.now()
            receipt, dropped = validate_payload(payload, now)

            # Le chiavi di lavoro della validazione non entrano nello stato.
            for item in receipt["items"]:
                for key in ITEM_TRANSIENT_FIELDS:
                    item.pop(key, None)

            receipt_id = receipt["receipt_id"]
            month = month_key(receipt["date"])

            if receipt_id in self._month_by_id:
                raise DuplicateReceiptError(receipt_id, "receipt_id gia' registrato")
            existing_strong = self._id_by_strong.get(receipt["fingerprint"])
            if existing_strong:
                raise DuplicateReceiptError(
                    existing_strong, "fingerprint forte identico articolo per articolo"
                )

            txn = _Txn(self)
            txn.open(month).append(receipt)

            changed = {month}
            changed |= self._reconcile_duplicates(txn, {receipt_id}, {})
            self._assert_unique(txn)

            await self._commit(txn, changed_months=changed)

            _, stored = txn.locate(receipt_id)
            if dropped:
                _LOGGER.info(
                    "Scontrino %s: %d campi non previsti scartati (%s)",
                    receipt_id,
                    len(dropped),
                    ", ".join(dropped[:8]),
                )
            _LOGGER.info(
                "Scontrino %s registrato: %s %s, incluso %.2f su %.2f%s",
                receipt_id,
                stored["date"],
                stored["store"],
                stored["included_total"],
                stored["receipt_total"],
                " [DA VERIFICARE: " + ", ".join(stored["review_reasons"]) + "]"
                if stored["needs_review"]
                else "",
            )
            return self.describe_receipt(stored)

    # -------------------------------------------------- modifica articolo #

    async def _update_item_locked(
        self, receipt_id: str, item_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Pipeline di modifica articolo. Richiede il lock GIA' acquisito."""
        if not changes:
            raise ValidationError(["Nessun campo da modificare"])

        txn = _Txn(self)
        month, receipt = txn.open_containing(receipt_id)
        item = self._find_item(receipt, item_id)

        before = self._fingerprint_snapshot(txn, {receipt_id})
        for field_name, value in changes.items():
            apply_item_field(item, field_name, value)

        recompute(receipt)
        touch(receipt, dt_util.now())

        changed = {month}
        changed |= self._reconcile_duplicates(txn, {receipt_id}, before)
        self._assert_unique(txn)

        await self._commit(txn, changed_months=changed)
        return self.describe_receipt(txn.locate(receipt_id)[1])

    async def async_update_item(
        self, receipt_id: str, item_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Applica modifiche a un articolo e ricalcola tutto il derivato."""
        async with self._lock:
            result = await self._update_item_locked(receipt_id, item_id, changes)
        _LOGGER.info(
            "Articolo %s dello scontrino %s aggiornato (%s); incluso ora %.2f",
            item_id,
            receipt_id,
            ", ".join(sorted(changes)),
            result["included_total"],
        )
        return result

    async def async_toggle_item(self, receipt_id: str, item_id: str) -> dict[str, Any]:
        """Inverte `included`. Lettura, inversione e commit in un solo lock.

        Nessuna finestra fra la lettura del valore corrente e la scrittura:
        due tocchi ravvicinati si serializzano e producono due inversioni, non
        due scritture dello stesso valore.
        """
        async with self._lock:
            _, receipt = _Txn(self).locate(receipt_id)
            current = bool(self._find_item(receipt, item_id)["included"])
            result = await self._update_item_locked(
                receipt_id, item_id, {"included": not current}
            )
        _LOGGER.info(
            "Articolo %s dello scontrino %s %s; incluso ora %.2f",
            item_id,
            receipt_id,
            "escluso" if current else "incluso",
            result["included_total"],
        )
        return result

    async def async_delete_item(self, receipt_id: str, item_id: str) -> dict[str, Any]:
        """Rimozione definitiva di un articolo. Azione esplicita, non l'esclusione."""
        async with self._lock:
            txn = _Txn(self)
            month, receipt = txn.open_containing(receipt_id)
            self._find_item(receipt, item_id)

            if len(receipt["items"]) == 1:
                raise ValidationError(
                    [
                        "Impossibile eliminare l'ultimo articolo: uno scontrino senza "
                        "articoli non e' uno stato valido. Elimina lo scontrino."
                    ]
                )

            before = self._fingerprint_snapshot(txn, {receipt_id})
            receipt["items"] = [i for i in receipt["items"] if i["id"] != item_id]
            recompute(receipt)
            touch(receipt, dt_util.now())

            changed = {month}
            changed |= self._reconcile_duplicates(txn, {receipt_id}, before)
            self._assert_unique(txn)

            await self._commit(txn, changed_months=changed)
            _LOGGER.info("Articolo %s eliminato dallo scontrino %s", item_id, receipt_id)
            return self.describe_receipt(txn.locate(receipt_id)[1])

    # ------------------------------------------------ modifica scontrino #

    async def async_update_receipt(
        self, receipt_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Applica modifiche a uno scontrino, spostandolo di mese se la data cambia.

        Lo spostamento avviene dentro la stessa transazione: entrambi i mesi
        vengono scritti nello stesso commit, quindi non esiste una finestra in
        cui lo scontrino sia assente da entrambi o presente in tutti e due.
        """
        if not changes:
            raise ValidationError(["Nessun campo da modificare"])

        async with self._lock:
            now = dt_util.now()
            txn = _Txn(self)
            source_month, receipt = txn.open_containing(receipt_id)

            before = self._fingerprint_snapshot(txn, {receipt_id})
            for field_name, value in changes.items():
                receipt[field_name] = coerce_receipt_field(field_name, value, now)

            recompute(receipt)
            touch(receipt, now)

            changed = {source_month}

            target_month = month_key(receipt["date"])
            if target_month != source_month:
                destination = txn.open(target_month)  # rifiuta se non scrivibile
                txn.working[source_month] = [
                    r for r in txn.working[source_month] if r["receipt_id"] != receipt_id
                ]
                destination.append(receipt)
                changed.add(target_month)
                _LOGGER.info(
                    "Scontrino %s spostato da %s a %s per cambio data",
                    receipt_id,
                    source_month,
                    target_month,
                )

            changed |= self._reconcile_duplicates(txn, {receipt_id}, before)
            self._assert_unique(txn)

            await self._commit(txn, changed_months=changed)

            _LOGGER.info(
                "Scontrino %s aggiornato (%s)", receipt_id, ", ".join(sorted(changes))
            )
            return self.describe_receipt(txn.locate(receipt_id)[1])

    async def async_delete_receipt(self, receipt_id: str) -> None:
        """Eliminazione definitiva di uno scontrino. Azione esplicita.

        Un mese rimasto senza scontrini viene salvato con receipts: [], non
        rimosso: il file vuoto rappresenta uno svuotamento intenzionale, e la
        sua assenza significherebbe un'altra cosa.
        """
        async with self._lock:
            txn = _Txn(self)
            month, _ = txn.open_containing(receipt_id)

            before = self._fingerprint_snapshot(txn, {receipt_id})
            txn.working[month] = [
                r for r in txn.working[month] if r["receipt_id"] != receipt_id
            ]

            changed = {month}
            changed |= self._reconcile_duplicates(txn, {receipt_id}, before)
            self._assert_unique(txn)

            await self._commit(txn, changed_months=changed)
            _LOGGER.info("Scontrino %s eliminato definitivamente", receipt_id)

    # ------------------------------------------------------------ ricalcola #

    async def async_reload(self) -> dict[str, Any]:
        """Rilegge tutto dal disco, azzera i degradati e rivalida le invarianti.

        Usato da spesa.ricalcola dopo un intervento manuale sui file. NON
        rimuove il marker di blocco: solo spesa.sblocca_archivio puo' farlo.
        """
        async with self._lock:
            self.store.degraded.clear()
            await self.async_load()
            async_dispatcher_send(self.hass, SIGNAL_UPDATED)
            return {
                "mesi": len(self.months),
                "scontrini": sum(len(r) for r in self.months.values()),
                "mesi_non_utilizzabili": sorted(self.store.degraded),
                "violazioni": len(self.invariant_violations),
                "bloccato": self.journal_blocked is not None,
            }

    async def async_unblock(self, *, confirm: bool) -> dict[str, Any]:
        """Accetta lo stato corrente dei file come nuovo stato autorevole.

        NON e' una riparazione. Quando il blocco deriva da un journal
        irrecuperabile, Home Assistant non puo' sapere se i file rappresentino
        lo stato precedente, quello successivo o una combinazione dei due:
        l'informazione necessaria e' andata persa insieme al journal. Questo
        servizio e' una dichiarazione dell'utente, non una verifica.
        """
        async with self._lock:
            previous_block = self.journal_blocked

            # Diagnosi sullo stato ATTUALE dei file: le degradazioni residue di
            # caricamenti precedenti non devono far risultare non valido un
            # archivio nel frattempo corretto a mano. Il marker resta al suo
            # posto e continua a bloccare le scritture durante la verifica.
            self.store.degraded.clear()
            await self.async_load()

            degraded = sorted(self.store.degraded)
            violations = list(self.invariant_violations)
            journal_present = await self.hass.async_add_executor_job(
                lambda: self.store.journal_path().exists()
            )

            problems: list[str] = []
            if degraded:
                problems.append(f"mesi non caricabili: {', '.join(degraded)}")
            if violations:
                problems.append(
                    f"{len(violations)} violazioni delle invarianti globali: "
                    + "; ".join(v["detail"] for v in violations[:3])
                )
            if journal_present:
                problems.append(
                    "e' presente un journal di transazione: riavvia Home Assistant "
                    "per lasciare che il recovery lo applichi"
                )

            if problems:
                _LOGGER.error(
                    "Sblocco rifiutato, l'archivio non e' formalmente valido: %s",
                    "; ".join(problems),
                )
                return {
                    "sbloccato": False,
                    "motivo": "archivio non valido",
                    "problemi": problems,
                    "mesi_non_utilizzabili": degraded,
                    "violazioni": len(violations),
                }

            if not confirm:
                return {
                    "sbloccato": False,
                    "motivo": "conferma mancante",
                    "problemi": [],
                    "avviso": (
                        "I file sono formalmente validi, ma Home Assistant non puo' "
                        "stabilire se rappresentino lo stato prima o dopo la "
                        "transazione interrotta. Ispeziona /config/spesa, confronta "
                        "gli ultimi scontrini con quelli attesi, poi richiama il "
                        "servizio con conferma: true per accettare lo stato corrente "
                        "come autorevole."
                    ),
                    "scontrini": sum(len(r) for r in self.months.values()),
                    "mesi": sorted(self.months),
                }

            cleared = await self.hass.async_add_executor_job(self.store.clear_blocked_marker)
            self.journal_blocked = None
            async_dispatcher_send(self.hass, SIGNAL_UPDATED)

            _LOGGER.warning(
                "Archivio sbloccato su dichiarazione esplicita dell'utente. Blocco "
                "precedente: %s. Stato accettato come autorevole: %d scontrini in "
                "%d mesi.",
                previous_block or "(nessuno)",
                sum(len(r) for r in self.months.values()),
                len(self.months),
            )
            return {
                "sbloccato": True,
                "marker_rimosso": cleared,
                "scontrini": sum(len(r) for r in self.months.values()),
                "mesi": sorted(self.months),
            }

    # --------------------------------------------------------------- letture #
    #
    # Da qui in poi: SOLO lettura. Nessuna funzione modifica lo stato interno.
    # I valori restituiti sono strutture nuove con float, mai riferimenti ai
    # dizionari interni, cosi' un'entita' non puo' alterare l'archivio.

    @staticmethod
    def _find_item(receipt: dict, item_id: str) -> dict:
        for item in receipt["items"]:
            if item["id"] == item_id:
                return item
        raise NotFoundError(
            f"Articolo {item_id!r} inesistente nello scontrino {receipt['receipt_id']!r}"
        )

    def iter_receipts(self) -> Iterator[dict]:
        for month in sorted(self.months):
            yield from self.months[month]

    def get_receipt(self, receipt_id: str) -> dict | None:
        month = self._month_by_id.get(receipt_id)
        if month is None:
            return None
        for receipt in self.months.get(month, []):
            if receipt["receipt_id"] == receipt_id:
                return receipt
        return None

    def describe_receipt(self, receipt: dict) -> dict[str, Any]:
        """Vista serializzabile di uno scontrino, per servizi ed entita'."""
        return {
            "receipt_id": receipt["receipt_id"],
            "date": receipt["date"],
            "time": receipt.get("time"),
            "store": receipt["store"],
            "receipt_total": _f(receipt["receipt_total"]),
            "items_total": _f(receipt["items_total"]),
            "included_total": _f(receipt["included_total"]),
            "needs_review": receipt["needs_review"],
            "review_reasons": list(receipt["review_reasons"]),
            "review_labels": [
                REVIEW_LABELS.get(reason, reason) for reason in receipt["review_reasons"]
            ],
            "possible_duplicate_of": list(receipt.get("possible_duplicate_of") or []),
            "possible_duplicate_dismissed": receipt.get("possible_duplicate_dismissed", False),
            "manual_review": receipt.get("manual_review", False),
            "notes": receipt.get("notes"),
            "created_at": receipt.get("created_at"),
            "updated_at": receipt.get("updated_at"),
            "client_reported": receipt.get("client_reported"),
            "item_count": len(receipt["items"]),
            "items": [self.describe_item(item) for item in receipt["items"]],
        }

    @staticmethod
    def describe_item(item: dict) -> dict[str, Any]:
        return {
            "id": item["id"],
            "raw_name": item["raw_name"],
            "name": item["name"],
            "quantity": _f(item["quantity"]),
            "unit_price": _f(item.get("unit_price")),
            "price": _f(item["price"]),
            "discount": _f(item.get("discount")),
            "category": item["category"],
            "included": item["included"],
            "unit": item.get("unit"),
            "weight": _f(item.get("weight")),
            "notes": item.get("notes"),
            "product_id": item.get("product_id"),
        }

    # ---------------------------------------------------------- statistiche #
    #
    # Tutte le cifre di spesa usano included_total: e' la spesa effettivamente
    # attribuita, al netto degli articoli esclusi. receipt_total resta il dato
    # originale dello scontrino e items_total serve al controllo di coerenza.

    def month_total(self, month: str) -> Decimal:
        return sum(
            (r["included_total"] for r in self.months.get(month, [])), Decimal("0.00")
        )

    def day_total(self, day: date_cls) -> Decimal:
        iso = day.isoformat()
        return sum(
            (r["included_total"] for r in self.months.get(iso[:7], []) if r["date"] == iso),
            Decimal("0.00"),
        )

    def receipts_for_date(self, day: date_cls) -> list[dict]:
        """Scontrini di una data specifica, dall'archivio completo.

        Fonte semantica del 'cosa ho speso oggi': non va derivata da
        recent_receipts(), che e' una lista per la UI e limitata a N.
        """
        iso = day.isoformat()
        return [r for r in self.months.get(iso[:7], []) if r["date"] == iso]

    def breakdown(self, month: str) -> dict[str, Any]:
        """Ripartizione per categoria, per supermercato e per giorno."""
        by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
        by_store: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
        by_day: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))

        for receipt in self.months.get(month, []):
            by_store[receipt["store"]] += receipt["included_total"]
            by_day[receipt["date"]] += receipt["included_total"]
            for item in receipt["items"]:
                if item["included"]:
                    by_category[item["category"]] += item["price"]

        return {
            "per_categoria": {
                k: float(v) for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])
            },
            "per_supermercato": {
                k: float(v) for k, v in sorted(by_store.items(), key=lambda kv: -kv[1])
            },
            "per_giorno": {k: float(v) for k, v in sorted(by_day.items())},
        }

    def projection(self, month: str, now: datetime) -> float | None:
        """Proiezione di fine mese.

        Denominatore: giorni interi trascorsi piu' la frazione di oggi. Sotto
        i 3 giorni di dati restituisce None invece di un numero privo di senso.
        Su un mese passato la proiezione non ha significato: None.
        """
        if month != now.strftime("%Y-%m"):
            return None
        elapsed = (now.day - 1) + now.hour / 24 + now.minute / 1440
        if elapsed < 3:
            return None
        total = float(self.month_total(month))
        return round(total / elapsed * days_in_month(month), 2)

    def daily_average(self, month: str, now: datetime) -> float | None:
        receipts = self.months.get(month, [])
        if not receipts:
            return None
        if month == now.strftime("%Y-%m"):
            elapsed = max(1, now.day)
        else:
            elapsed = days_in_month(month)
        return round(float(self.month_total(month)) / elapsed, 2)

    def monthly_history(self, now: datetime, count: int = MONTHLY_HISTORY_MONTHS) -> list[dict]:
        """Andamento degli ultimi mesi, indipendente dal recorder."""
        history = []
        for month in month_range(now.strftime("%Y-%m"), count):
            history.append(
                {
                    "mese": month,
                    "etichetta": month_label(month),
                    "totale": float(self.month_total(month)),
                    "scontrini": len(self.months.get(month, [])),
                }
            )
        return history

    def recent_receipts(self, limit: int = RECENT_RECEIPTS_LIMIT) -> list[dict]:
        ordered = sorted(
            self.iter_receipts(),
            key=lambda r: (r["date"], r.get("time") or "", r["receipt_id"]),
            reverse=True,
        )
        return [
            {
                "receipt_id": r["receipt_id"],
                "date": r["date"],
                "time": r.get("time"),
                "store": r["store"],
                "receipt_total": _f(r["receipt_total"]),
                "included_total": _f(r["included_total"]),
                "item_count": len(r["items"]),
                "needs_review": r["needs_review"],
            }
            for r in ordered[:limit]
        ]

    def review_queue(self) -> list[dict]:
        pending = [r for r in self.iter_receipts() if r["needs_review"]]
        pending.sort(key=lambda r: (r["date"], r["receipt_id"]), reverse=True)
        return [
            {
                "receipt_id": r["receipt_id"],
                "date": r["date"],
                "store": r["store"],
                "included_total": _f(r["included_total"]),
                "motivi": [REVIEW_LABELS.get(x, x) for x in r["review_reasons"]],
                "codici": list(r["review_reasons"]),
                "possible_duplicate_of": list(r.get("possible_duplicate_of") or []),
            }
            for r in pending
        ]

    def summary(self) -> dict[str, Any]:
        """Tutto quello che serve ai sensori, in una sola passata."""
        now = dt_util.now()
        today = now.date()
        current = now.strftime("%Y-%m")
        previous = previous_month(current)

        current_total = float(self.month_total(current))
        previous_total = float(self.month_total(previous))
        delta = round(current_total - previous_total, 2)
        delta_pct = round(delta / previous_total * 100, 1) if previous_total else None

        receipts_today = self.receipts_for_date(today)

        return {
            "now": now,
            "mese_corrente": current,
            "mese_precedente": previous,
            "etichetta_mese": month_label(current),
            "totale_mese_corrente": round(current_total, 2),
            "totale_mese_precedente": round(previous_total, 2),
            "differenza_euro": delta,
            "differenza_percentuale": delta_pct,
            "media_giornaliera": self.daily_average(current, now),
            "proiezione_fine_mese": self.projection(current, now),
            "oggi": today.isoformat(),
            "totale_oggi": float(self.day_total(today)),
            "numero_scontrini_oggi": len(receipts_today),
            "scontrini_oggi": [
                {
                    "receipt_id": r["receipt_id"],
                    "time": r.get("time"),
                    "store": r["store"],
                    "receipt_total": _f(r["receipt_total"]),
                    "included_total": _f(r["included_total"]),
                    "needs_review": r["needs_review"],
                }
                for r in sorted(
                    receipts_today, key=lambda r: (r.get("time") or "", r["receipt_id"])
                )
            ],
            "scontrini_mese_corrente": len(self.months.get(current, [])),
            "scontrini_totali": sum(len(r) for r in self.months.values()),
            "corrente": self.breakdown(current),
            "precedente": self.breakdown(previous),
            "storico_mensile": self.monthly_history(now),
            "da_verificare": self.review_queue(),
            "mesi_degradati": self.degraded_months,
        }
