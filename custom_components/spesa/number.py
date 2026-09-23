"""Number dell'integrazione Spesa alimentare.

  articolo_prezzo     prezzo finale della riga
  articolo_quantita   quantita' dell'articolo

Entrambi agiscono sull'articolo selezionato e scrivono passando dal manager:
lock unico, validazione, ricalcolo dei derivati, scrittura atomica. Sono
controlli di MODIFICA, quindi ereditano da SpesaEditEntity e si disattivano
durante un blocco dell'archivio o senza un articolo su cui agire.

Modalita' BOX e non SLIDER: questi valori si correggono digitando la cifra
letta sullo scontrino, non trascinando un cursore.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CURRENCY, DOMAIN, MAX_AMOUNT, MAX_QUANTITY, MIN_AMOUNT
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

    async_add_entities(
        [
            SpesaItemPriceNumber(manager, selection, entry.entry_id),
            SpesaItemQuantityNumber(manager, selection, entry.entry_id),
        ]
    )


class SpesaItemNumber(SpesaEditEntity, NumberEntity):
    """Base dei controlli numerici sull'articolo selezionato."""

    _attr_mode = NumberMode.BOX

    _field: str

    async def async_set_native_value(self, value: float) -> None:
        """Applica il nuovo valore passando dal manager.

        Il controllo di uguaglianza e' fatto qui e non delegato al frontend:
        number.set_value puo' essere richiamato da automazioni e script, e la
        deduplicazione preventiva del valore non fa parte del contratto di
        NumberEntity. Senza questo controllo una chiamata ripetuta creerebbe
        una transazione, aggiornerebbe updated_at e scriverebbe su disco senza
        alcuna modifica reale.

        Confronto diretto, senza tolleranza: i valori esposti derivano da
        Decimal gia' quantizzati a 0.01 (prezzo) e 0.001 (quantita'), quindi
        una soglia arbitraria introdurrebbe solo imprecisione.

        Cattura l'intero spettro di errori della pipeline: validazione,
        livello transazionale e persistenza. Una modifica da dashboard non
        deve mai produrre un'eccezione grezza al posto di un messaggio
        leggibile nella UI.
        """
        current = self.native_value
        if current is not None and value == current:
            return

        try:
            await self._async_apply(self._field, value)
        except (ValidationError, ManagerError, StoreError) as err:
            _LOGGER.error("%s non modificato: %s", self._field, err)
            raise HomeAssistantError(f"Valore non applicato: {err}") from err


class SpesaItemPriceNumber(SpesaItemNumber):
    """Prezzo finale della riga.

    Convenzione: price e' il prezzo effettivamente attribuito alla riga, gia'
    al netto di eventuali sconti. Correggerlo fa ricalcolare items_total,
    included_total e i motivi di verifica, incluso lo scarto rispetto al
    totale stampato sullo scontrino.

    Il minimo e' negativo perche' una riga puo' legittimamente esserlo: resi,
    sconti in riga, arrotondamenti.
    """

    _attr_icon = "mdi:currency-eur"
    _attr_native_unit_of_measurement = CURRENCY
    _attr_native_min_value = MIN_AMOUNT
    _attr_native_max_value = MAX_AMOUNT
    _attr_native_step = 0.01
    _field = "price"

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "articolo_prezzo")

    @property
    def native_value(self) -> float | None:
        item = self._selection.item
        return float(item["price"]) if item else None


class SpesaItemQuantityNumber(SpesaItemNumber):
    """Quantita' dell'articolo.

    Passo 0,001 per i prodotti a peso: 0,352 kg e' una quantita' legittima.
    Il minimo e' positivo perche' una quantita' nulla o negativa non ha
    significato e il manager la rifiuterebbe comunque.
    """

    _attr_icon = "mdi:numeric"
    _attr_native_min_value = 0.001
    _attr_native_max_value = MAX_QUANTITY
    _attr_native_step = 0.001
    _field = "quantity"

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "articolo_quantita")

    @property
    def native_value(self) -> float | None:
        item = self._selection.item
        return float(item["quantity"]) if item else None
