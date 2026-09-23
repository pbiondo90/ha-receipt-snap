"""Select dell'integrazione Spesa alimentare.

  scontrino            naviga fra gli ultimi scontrini
  articolo             naviga fra gli articoli dello scontrino selezionato
  articolo_categoria   modifica la categoria dell'articolo selezionato

I primi due cambiano soltanto lo stato di INTERFACCIA e non toccano
l'archivio: restano quindi utilizzabili anche mentre l'archivio e' bloccato,
coerentemente col principio che il blocco impedisce le mutazioni e non la
consultazione. Il terzo e' un controllo di MODIFICA e eredita da
SpesaEditEntity, quindi si disattiva durante un blocco o senza un articolo su
cui agire.

LABEL E IDENTIFICATIVI
----------------------
Home Assistant scambia stringhe, ma SpesaSelection conserva solo receipt_id e
item_id. Le label sono sempre RICALCOLATE dallo stato corrente: options e
current_option provengono dalla stessa funzione, nella stessa lettura, quindi
non possono divergere.

Conseguenza: quando i dati cambiano e la label cambia con loro - prezzo
corretto, articolo rinominato, needs_review che compare o sparisce - la
selezione non si perde e lo stato resta sempre uno degli elementi di options.
La conversione label -> id avviene solo al momento del tocco, sulle opzioni
appena generate.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CATEGORIES, DOMAIN, SELECT_NONE
from .entity import SpesaEditEntity, SpesaEntity, SpesaSelection
from .manager import ManagerError, SpesaManager
from .model import ValidationError
from .store import StoreError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    stored = hass.data[DOMAIN][entry.entry_id]
    manager: SpesaManager = stored["manager"]
    selection: SpesaSelection = stored["selection"]

    async_add_entities(
        [
            SpesaReceiptSelect(manager, selection, entry.entry_id),
            SpesaItemSelect(manager, selection, entry.entry_id),
            SpesaCategorySelect(manager, selection, entry.entry_id),
        ]
    )


class SpesaNavigationSelect(SpesaEntity, SelectEntity):
    """Base dei select di navigazione.

    Non modificano l'archivio, quindi nessun override di available.

    Quando non c'e' nulla da selezionare l'unica opzione e' il segnaposto
    SELECT_NONE: il menu resta valido e non servono opzioni finte ne' uno
    stato fuori dalle options, che Home Assistant segnalerebbe come errore.
    """

    def _pairs(self) -> list[tuple[str, str]]:
        """(identificativo, label) nello stato corrente. Da implementare."""
        raise NotImplementedError

    def _selected_id(self) -> str | None:
        """Identificativo attualmente selezionato. Da implementare."""
        raise NotImplementedError

    def _apply(self, identifier: str) -> None:
        """Registra la nuova selezione. Da implementare."""
        raise NotImplementedError

    @property
    def options(self) -> list[str]:
        pairs = self._pairs()
        return [label for _, label in pairs] or [SELECT_NONE]

    @property
    def current_option(self) -> str | None:
        """Label corrispondente alla selezione corrente.

        SOLA LETTURA. Ricavata dalle stesse coppie che generano options, nella
        stessa lettura: se la label e' cambiata perche' sono cambiati i dati,
        qui compare gia' quella nuova e resta un elemento valido di options.
        """
        pairs = self._pairs()
        if not pairs:
            return SELECT_NONE
        selected = self._selected_id()
        for identifier, label in pairs:
            if identifier == selected:
                return label

        # Non deve accadere: receipt_options() garantisce la presenza
        # dell'elemento selezionato e item_options() non applica limiti. Se
        # accade e' un bug dell'invariante, da correggere alla fonte: qui
        # viene segnalato e basta. Restituire la label di un altro elemento
        # farebbe divergere cio' che si vede da cio' che si modifica;
        # ripararlo in una property introdurrebbe mutazioni durante la lettura
        # dello stato. Uno stato momentaneamente non selezionato e' piu' sicuro
        # di entrambe le alternative.
        _LOGGER.error(
            "%s: la selezione %r non e' fra le opzioni disponibili. Invariante "
            "violata: nessuna opzione verra' mostrata come attiva.",
            self.entity_id,
            selected,
        )
        return None

    async def async_select_option(self, option: str) -> None:
        """Traduce la label toccata nel suo identificativo.

        La traduzione avviene sulle opzioni appena generate, quindi sulla
        stessa vista dei dati che l'utente ha davanti. Nessun dispatch manuale
        di SIGNAL_SELECTION: lo emette SpesaSelection quando la selezione
        cambia davvero.
        """
        if option == SELECT_NONE:
            return
        for identifier, label in self._pairs():
            if label == option:
                self._apply(identifier)
                self.async_write_ha_state()
                return
        _LOGGER.debug(
            "Opzione %r non piu' presente in %s: i dati sono cambiati nel frattempo, "
            "selezione invariata",
            option,
            self.entity_id,
        )


class SpesaReceiptSelect(SpesaNavigationSelect):
    """Scontrino visualizzato in dashboard."""

    _attr_icon = "mdi:receipt-text-check"

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "scontrino")

    def _pairs(self) -> list[tuple[str, str]]:
        return self._selection.receipt_options()

    def _selected_id(self) -> str | None:
        return self._selection.receipt_id

    def _apply(self, identifier: str) -> None:
        # select_receipt azzera anche l'articolo: quello precedente
        # apparteneva a un altro scontrino.
        self._selection.select_receipt(identifier)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "receipt_id": self._selection.receipt_id,
            "scontrini_disponibili": len(self._pairs()),
        }


class SpesaItemSelect(SpesaNavigationSelect):
    """Articolo su cui agiscono i controlli di modifica."""

    _attr_icon = "mdi:format-list-checks"

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "articolo")

    def _pairs(self) -> list[tuple[str, str]]:
        return self._selection.item_options()

    def _selected_id(self) -> str | None:
        return self._selection.item_id

    def _apply(self, identifier: str) -> None:
        self._selection.select_item(identifier)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._selection.item
        if item is None:
            return {"articoli_disponibili": 0}
        return {
            "scontrino": self._selection.receipt_id,
            "item_id": item["id"],
            "raw_name": item["raw_name"],
            "incluso": item["included"],
            "articoli_disponibili": len(self._pairs()),
        }


class SpesaCategorySelect(SpesaEditEntity, SelectEntity):
    """Categoria dell'articolo selezionato.

    Controllo di MODIFICA: eredita da SpesaEditEntity, quindi si disattiva
    automaticamente durante un blocco dell'archivio o quando non c'e' un
    articolo su cui agire.

    Qui le opzioni sono le categorie canoniche, fisse e gia' leggibili: non
    serve alcuna mappatura label -> identificativo.
    """

    _attr_icon = "mdi:tag-outline"
    _attr_options = list(CATEGORIES)

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "articolo_categoria")

    @property
    def current_option(self) -> str | None:
        item = self._selection.item
        return item["category"] if item else None

    async def async_select_option(self, option: str) -> None:
        """Applica la categoria passando dal manager.

        Modificare la categoria risolve anche category_was_unknown, quindi il
        motivo di verifica 'unknown_category' sparisce da solo: l'accoppiamento
        campo -> flag vive in apply_item_field, non qui.
        """
        item = self._selection.item
        if item is None:
            return
        if item["category"] == option:
            return  # nessuna scrittura per una selezione identica

        try:
            await self._async_apply("category", option)
        except (ValidationError, ManagerError, StoreError) as err:
            _LOGGER.error("Categoria non modificata: %s", err)
            raise HomeAssistantError(f"Categoria non modificata: {err}") from err

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes = super().extra_state_attributes
        item = self._selection.item
        if item is not None:
            attributes["categoria_incerta"] = item.get("category_was_unknown", False)
        return attributes
