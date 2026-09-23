"""Button dell'integrazione Spesa alimentare.

  inverti_inclusione   include o esclude l'articolo selezionato

E' l'operazione piu' frequente: chiude il flusso seleziona scontrino -> vedi
articoli -> seleziona articolo -> includi/escludi -> totali aggiornati.

Un pulsante e non uno switch, deliberatamente. Uno switch rappresenta lo stato
di UNA cosa, mentre qui l'oggetto cambia a ogni selezione: uno switch che salta
fra on e off perche' si e' scelto un altro articolo sarebbe fuorviante, e in
dashboard rischierebbe di essere letto come interruttore di una funzione. Lo
stato di inclusione e' gia' visibile nel segno della label del select, negli
attributi di questa entita' e nella tabella del dettaglio.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import SpesaEditEntity, SpesaSelection
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

    async_add_entities([SpesaToggleItemButton(manager, selection, entry.entry_id)])


class SpesaToggleItemButton(SpesaEditEntity, ButtonEntity):
    """Inverte il flag `included` dell'articolo selezionato."""

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "inverti_inclusione")

    @property
    def icon(self) -> str:
        """Icona che anticipa l'effetto del tocco.

        Articolo incluso -> l'azione lo escludera': icona di rimozione.
        Articolo escluso -> l'azione lo includera': icona di aggiunta.
        """
        item = self._selection.item
        if item is None:
            return "mdi:cart-remove"
        return "mdi:cart-remove" if item["included"] else "mdi:cart-plus"

    async def async_press(self) -> None:
        """Inverte l'inclusione.

        Delega ad async_toggle_item, che legge il valore corrente e lo inverte
        dentro UNA sola acquisizione del lock: due tocchi ravvicinati si
        serializzano e producono due inversioni, non due scritture dello stesso
        valore.

        Il ricalcolo dei totali e l'aggiornamento delle entita' avvengono nella
        stessa transazione, quindi il totale del mese cambia immediatamente.

        Il controllo su receipt_id e item_id e' ridondante rispetto ad
        available, ma copre il caso in cui la selezione cambi fra il rendering
        della card e il tocco.
        """
        receipt_id = self._selection.receipt_id
        item_id = self._selection.item_id
        if receipt_id is None or item_id is None:
            _LOGGER.warning("Inversione ignorata: nessun articolo selezionato")
            return

        try:
            await self._manager.async_toggle_item(receipt_id, item_id)
        except (ValidationError, ManagerError, StoreError) as err:
            _LOGGER.error("Inclusione non modificata: %s", err)
            raise HomeAssistantError(f"Inclusione non modificata: {err}") from err

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Cosa succede premendo, e su cosa.

        Permette alla dashboard di etichettare il pulsante con l'azione reale
        invece di un generico 'inverti', senza interrogare altre entita'.
        """
        attributes = super().extra_state_attributes
        item = self._selection.item
        if item is None:
            return attributes

        included = item["included"]
        attributes.update(
            {
                "nome": item["name"],
                "prezzo": float(item["price"]),
                "incluso": included,
                "azione": "Escludi dalla spesa" if included else "Includi nella spesa",
            }
        )
        return attributes
