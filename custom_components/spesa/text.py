"""Text dell'integrazione Spesa alimentare.

  articolo_nome   nome normalizzato dell'articolo selezionato

Modifica il solo campo `name`. `raw_name` NON ha un'entita' di modifica: e' la
trascrizione originale dello scontrino ed e' immutabile per contratto,
rifiutata anche da spesa.aggiorna_articolo. E' consultabile negli attributi di
questa entita' e nel dettaglio dello scontrino.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MAX_STR_LEN
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

    async_add_entities([SpesaItemNameText(manager, selection, entry.entry_id)])


class SpesaItemNameText(SpesaEditEntity, TextEntity):
    """Nome normalizzato dell'articolo selezionato."""

    _attr_icon = "mdi:rename-outline"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 1
    _attr_native_max = MAX_STR_LEN

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "articolo_nome")

    @property
    def native_value(self) -> str | None:
        item = self._selection.item
        return item["name"] if item else None

    async def async_set_value(self, value: str) -> None:
        """Applica il nuovo nome passando dal manager.

        Correggere il nome risolve anche name_was_missing, quindi il motivo di
        verifica 'missing_normalized_name' sparisce da solo: l'accoppiamento
        campo -> flag vive in apply_item_field.
        """
        item = self._selection.item
        if item is None:
            return
        if item["name"] == value:
            return  # nessuna scrittura per un valore identico

        try:
            await self._async_apply("name", value)
        except (ValidationError, ManagerError, StoreError) as err:
            _LOGGER.error("Nome non modificato: %s", err)
            raise HomeAssistantError(f"Nome non applicato: {err}") from err

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes = super().extra_state_attributes
        item = self._selection.item
        if item is not None:
            # raw_name e' gia' negli attributi della base: qui si aggiunge solo
            # il flag che spiega perche' il nome potrebbe essere un ripiego.
            attributes["nome_mancante_all_origine"] = item.get("name_was_missing", False)
        return attributes
