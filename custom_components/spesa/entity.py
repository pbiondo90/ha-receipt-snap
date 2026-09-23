"""Classe base e stato di selezione delle entita' Spesa alimentare.

Due responsabilita':

  SpesaSelection  stato di selezione della dashboard (scontrino e articolo
                  correnti). E' stato di INTERFACCIA, non dell'archivio: vive
                  in memoria, non viene persistito e non compare nei file.

  SpesaEntity     base comune alle 13 entita': dispositivo condiviso,
                  sottoscrizione agli aggiornamenti, accesso al manager.

Due segnali distinti, perche' le cause sono diverse:

  SIGNAL_UPDATED    i dati sono cambiati (ingest, modifica, eliminazione)
  SIGNAL_SELECTION  e' cambiato cio' che l'utente sta guardando
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SELECTABLE_RECEIPTS_LIMIT, SIGNAL_SELECTION, SIGNAL_UPDATED
from .manager import SpesaManager

_LOGGER = logging.getLogger(__name__)


class SpesaSelection:
    """Scontrino e articolo correntemente selezionati in dashboard.

    Non persistito: dopo un riavvio la dashboard riparte dallo scontrino piu'
    recente, che e' il comportamento atteso quando si apre la pagina.

    Emette SIGNAL_SELECTION da sola, e solo quando il valore cambia davvero:
    la responsabilita' di notificare sta qui, dove si conosce il cambiamento,
    non nei chiamanti, che potrebbero dimenticarlo.
    """

    def __init__(self, hass: HomeAssistant, manager: SpesaManager) -> None:
        self._hass = hass
        self._manager = manager
        self._receipt_id: str | None = None
        self._item_id: str | None = None

    def _notify(self) -> None:
        async_dispatcher_send(self._hass, SIGNAL_SELECTION)

    # ---------------------------------------------------------- scontrino #

    @property
    def receipt_id(self) -> str | None:
        """Scontrino selezionato, con ripiego sul piu' recente.

        Se la selezione punta a uno scontrino eliminato o a un mese escluso
        dalle statistiche, ricade sul piu' recente disponibile invece di
        restare appesa a un riferimento morto.
        """
        if self._receipt_id and self._manager.get_receipt(self._receipt_id):
            return self._receipt_id
        recent = self._manager.recent_receipts(1)
        return recent[0]["receipt_id"] if recent else None

    @property
    def receipt(self) -> dict | None:
        receipt_id = self.receipt_id
        return self._manager.get_receipt(receipt_id) if receipt_id else None

    def select_receipt(self, receipt_id: str | None) -> None:
        """Cambia scontrino e azzera la selezione dell'articolo.

        L'articolo precedente appartiene a un altro scontrino: mantenerlo
        selezionato lascerebbe le entita' di editing puntate su qualcosa che
        non e' piu' visibile nella tabella.

        Il confronto usa receipt_id, la proprieta' RISOLTA: riselezionare
        esplicitamente lo scontrino su cui si e' gia' per ripiego non e' un
        cambiamento e non deve generare dispatch.
        """
        if receipt_id == self.receipt_id and self._item_id is None:
            return
        self._receipt_id = receipt_id
        self._item_id = None
        self._notify()

    # ----------------------------------------------------------- articolo #

    @property
    def item_id(self) -> str | None:
        """Articolo selezionato, con ripiego sul primo dello scontrino."""
        receipt = self.receipt
        if receipt is None:
            return None
        ids = [item["id"] for item in receipt["items"]]
        if self._item_id in ids:
            return self._item_id
        return ids[0] if ids else None

    @property
    def item(self) -> dict | None:
        receipt = self.receipt
        item_id = self.item_id
        if receipt is None or item_id is None:
            return None
        for item in receipt["items"]:
            if item["id"] == item_id:
                return item
        return None

    def select_item(self, item_id: str | None) -> None:
        if item_id == self.item_id:
            return
        self._item_id = item_id
        self._notify()

    # ------------------------------------------------------------ opzioni #

    @staticmethod
    def _receipt_suffix(receipt_id: str) -> str:
        """Frammento dell'id che rende l'etichetta univoca.

        Data, ora, negozio e totale possono coincidere fra due scontrini
        distinti: senza un frammento dell'id due opzioni del menu sarebbero
        identiche e non ricondurrebbero a un receipt_id preciso. Gli ultimi
        caratteri sono la parte piu' variabile nei nostri id, che finiscono
        con un suffisso casuale.
        """
        return receipt_id[-4:].upper() if len(receipt_id) > 4 else receipt_id.upper()

    def _receipt_label(self, entry: dict) -> str:
        """Label di uno scontrino. Unico formato, usato per ogni opzione.

        Estratta in un helper perche' lo scontrino selezionato puo' essere
        aggiunto fuori dalla lista dei recenti: deve avere esattamente la
        stessa forma degli altri.

        Formato: 21/09 10:45 - Conad - 45,33 EUR - #A81F [segnale verifica]
        """
        day = f"{entry['date'][8:10]}/{entry['date'][5:7]}"
        moment = f"{day} {entry['time']}" if entry.get("time") else day
        flag = " \u26a0" if entry["needs_review"] else ""
        return (
            f"{moment} \u00b7 {entry['store']} \u00b7 {entry['included_total']:.2f} \u20ac "
            f"\u00b7 #{self._receipt_suffix(entry['receipt_id'])}{flag}"
        )

    def _receipt_summary(self, receipt: dict) -> dict:
        """Riduce uno scontrino ai campi che servono alla label.

        Stessa forma delle voci di recent_receipts(), cosi' _receipt_label non
        deve distinguere la provenienza.
        """
        return {
            "receipt_id": receipt["receipt_id"],
            "date": receipt["date"],
            "time": receipt.get("time"),
            "store": receipt["store"],
            "included_total": float(receipt["included_total"]),
            "needs_review": receipt["needs_review"],
        }

    def receipt_options(self) -> list[tuple[str, str]]:
        """(receipt_id, etichetta) degli scontrini selezionabili.

        INVARIANTE: se receipt_id identifica ancora uno scontrino esistente,
        quell'id compare SEMPRE fra le coppie restituite.

        Senza questa garanzia uno scontrino selezionato e poi uscito dai piu'
        recenti resterebbe il bersaglio delle modifiche mentre il menu mostra
        la label di un altro: si correggerebbe un articolo credendo di agire
        su cio' che si vede. Il limite dei recenti e' una scelta di interfaccia,
        non un'invariante: una voce in piu' e' irrilevante.
        """
        entries = self._manager.recent_receipts(SELECTABLE_RECEIPTS_LIMIT)

        selected_id = self.receipt_id
        if selected_id and not any(e["receipt_id"] == selected_id for e in entries):
            selected = self._manager.get_receipt(selected_id)
            if selected is not None:
                entries.append(self._receipt_summary(selected))
                entries.sort(
                    key=lambda e: (e["date"], e.get("time") or "", e["receipt_id"]),
                    reverse=True,
                )

        options = [(entry["receipt_id"], self._receipt_label(entry)) for entry in entries]

        # Univocita' finale: due scontrini con data, ora, negozio, totale e
        # suffisso identici darebbero due opzioni indistinguibili, e il menu
        # non ricondurrebbe a un receipt_id preciso.
        seen: dict[str, int] = {}
        resolved: list[tuple[str, str]] = []
        for receipt_id, label in options:
            if label in seen:
                seen[label] += 1
                label = f"{label} ({seen[label]})"
            else:
                seen[label] = 1
            resolved.append((receipt_id, label))
        return resolved

    def item_options(self) -> list[tuple[str, str]]:
        """(item_id, etichetta) degli articoli dello scontrino selezionato.

        L'etichetta porta il segno di inclusione in testa, cosi' si vede a
        colpo d'occhio cosa si sta per invertire. Nessun limite: l'invariante
        'l'elemento selezionato e' fra le opzioni' e' rispettata per
        costruzione finche' l'articolo esiste.
        """
        receipt = self.receipt
        if receipt is None:
            return []
        options: list[tuple[str, str]] = []
        for item in receipt["items"]:
            mark = "\u2713" if item["included"] else "\u2717"
            name = item["name"][:40]
            options.append(
                (item["id"], f"{mark} {item['id']} \u00b7 {name} \u00b7 {item['price']:.2f} \u20ac")
            )
        return options


class SpesaEntity(Entity):
    """Base delle entita' Spesa.

    Nessun override di available: le entita' di sola lettura restano
    consultabili in ogni condizione. Un blocco dell'archivio impedisce le
    MUTAZIONI, non la consultazione dei dati gia' caricati in memoria: durante
    un blocco deve restare possibile vedere totali, ultimi scontrini, elementi
    da verificare e dettaglio.

    Nessun polling: il manager emette SIGNAL_UPDATED dopo ogni commit riuscito
    e SpesaSelection emette SIGNAL_SELECTION quando cambia cio' che l'utente
    sta guardando.

    Il nome visualizzato viene dalle traduzioni tramite translation_key, che
    coincide con la chiave passata al costruttore: una sola fonte, nessun
    disallineamento possibile fra codice e file di traduzione.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        manager: SpesaManager,
        selection: SpesaSelection,
        entry_id: str,
        key: str,
    ) -> None:
        self._manager = manager
        self._selection = selection
        self._key = key
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Spesa alimentare",
            model="Archivio scontrini",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Sottoscrive entrambi i segnali.

        async_on_remove garantisce la disiscrizione allo smontaggio: senza, un
        reload lascerebbe ascoltatori orfani che scrivono su entita' morte.
        """
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._handle_update)
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SELECTION, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class SpesaEditEntity(SpesaEntity):
    """Base delle entita' che modificano l'articolo selezionato.

    Disponibili solo quando c'e' davvero un articolo su cui agire e le
    scritture sono permesse: senza scontrini, o con l'archivio bloccato, i
    controlli restano visibili ma inattivi invece di accettare comandi che il
    manager rifiuterebbe comunque.
    """

    @property
    def available(self) -> bool:
        return (
            self._manager.journal_blocked is None
            and self._selection.item is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Contesto minimo: su cosa sto agendo.

        Permette alla dashboard di mostrare, accanto ai controlli, a quale
        articolo si riferiscono senza interrogare altre entita'.
        """
        item = self._selection.item
        if item is None:
            return {}
        return {
            "scontrino": self._selection.receipt_id,
            "articolo": item["id"],
            "raw_name": item["raw_name"],
        }

    async def _async_apply(self, field: str, value: Any) -> None:
        """Applica una modifica al campo dell'articolo selezionato.

        Passa sempre dal manager, quindi dal lock unico, dalla validazione, dal
        ricalcolo dei derivati e dalla scrittura atomica: un'entita' non tocca
        mai direttamente i dati.

        Le eccezioni risalgono al chiamante, che le traduce in
        HomeAssistantError per mostrarle nella UI.
        """
        receipt_id = self._selection.receipt_id
        item_id = self._selection.item_id
        if receipt_id is None or item_id is None:
            _LOGGER.warning("Modifica di %s ignorata: nessun articolo selezionato", self._key)
            return
        await self._manager.async_update_item(receipt_id, item_id, {field: value})
