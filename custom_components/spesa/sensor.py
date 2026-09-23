"""Sensori dell'integrazione Spesa alimentare.

Sei sensori, tutti derivati dai dati persistiti e ricalcolati a ogni
SIGNAL_UPDATED. Nessuna dipendenza dal recorder: lo storico vive nei file
mensili e le aggregazioni sono calcolate in memoria dal manager.

  mese_corrente      EUR del mese in corso, con confronti e proiezione
  mese_precedente    EUR del mese precedente
  oggi               EUR di oggi
  ultimi_scontrini   numero in archivio, con gli ultimi 20 negli attributi
  da_verificare      quanti scontrini richiedono controllo, piu' lo stato
                     di salute dell'archivio
  dettaglio          lo scontrino selezionato, con i suoi articoli

Tutti i totali di spesa usano included_total: e' la spesa effettivamente
attribuita, al netto degli articoli esclusi.

I nomi visualizzati vengono dalle traduzioni: la translation_key e' impostata
dalla classe base a partire dalla stessa chiave del unique_id.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CURRENCY, DOMAIN, REVIEW_LABELS, TODAY_RECEIPTS_SHOWN
from .entity import SpesaEntity, SpesaSelection
from .manager import SpesaManager, month_label

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    stored = hass.data[DOMAIN][entry.entry_id]
    manager: SpesaManager = stored["manager"]
    selection: SpesaSelection = stored["selection"]

    async_add_entities(
        [
            SpesaCurrentMonthSensor(manager, selection, entry.entry_id),
            SpesaPreviousMonthSensor(manager, selection, entry.entry_id),
            SpesaTodaySensor(manager, selection, entry.entry_id),
            SpesaRecentSensor(manager, selection, entry.entry_id),
            SpesaReviewSensor(manager, selection, entry.entry_id),
            SpesaDetailSensor(manager, selection, entry.entry_id),
        ]
    )


class SpesaMoneySensor(SpesaEntity, SensorEntity):
    """Base dei sensori monetari.

    Nessuna state_class, deliberatamente. Questi valori non sono misurazioni
    istantanee ma aggregati che si azzerano al cambio mese, possono diminuire
    quando si esclude un articolo e sono correggibili retroattivamente.
    Attribuire loro una semantica statistica produrrebbe long-term statistics
    fuorvianti, e la fonte autorevole dello storico restano i file mensili,
    esposti in 'storico_mensile'.

    MEASUREMENT non sarebbe comunque ammessa insieme a MONETARY.
    """

    _attr_native_unit_of_measurement = CURRENCY  # ISO 4217
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2


class SpesaCurrentMonthSensor(SpesaMoneySensor):
    """Spesa del mese in corso, con confronti, media e proiezione."""

    _attr_icon = "mdi:cart"

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "mese_corrente")

    @property
    def native_value(self) -> float:
        return self._manager.summary()["totale_mese_corrente"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._manager.summary()
        return {
            "mese": data["mese_corrente"],
            "etichetta": data["etichetta_mese"],
            "scontrini": data["scontrini_mese_corrente"],
            "media_giornaliera": data["media_giornaliera"],
            "proiezione_fine_mese": data["proiezione_fine_mese"],
            "mese_precedente": data["totale_mese_precedente"],
            "differenza_euro": data["differenza_euro"],
            "differenza_percentuale": data["differenza_percentuale"],
            "per_categoria": data["corrente"]["per_categoria"],
            "per_supermercato": data["corrente"]["per_supermercato"],
            "per_giorno": data["corrente"]["per_giorno"],
            "storico_mensile": data["storico_mensile"],
            "mesi_esclusi": sorted(data["mesi_degradati"]),
        }


class SpesaPreviousMonthSensor(SpesaMoneySensor):
    """Spesa del mese precedente, come termine di paragone."""

    _attr_icon = "mdi:calendar-arrow-left"

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "mese_precedente")

    @property
    def native_value(self) -> float:
        return self._manager.summary()["totale_mese_precedente"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._manager.summary()
        month = data["mese_precedente"]
        return {
            "mese": month,
            "etichetta": month_label(month),
            "scontrini": len(self._manager.months.get(month, [])),
            "per_categoria": data["precedente"]["per_categoria"],
            "per_supermercato": data["precedente"]["per_supermercato"],
        }


class SpesaTodaySensor(SpesaMoneySensor):
    """Spesa di oggi."""

    _attr_icon = "mdi:cart-outline"

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "oggi")

    @property
    def native_value(self) -> float:
        return self._manager.summary()["totale_oggi"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Conteggio reale, elenco eventualmente troncato ma dichiarato tale.

        'numero_scontrini' e' il conteggio completo del giorno, non la
        lunghezza dell'elenco esposto: una lista troncata non deve mai essere
        presentata come completa.
        """
        data = self._manager.summary()
        receipts = data["scontrini_oggi"]
        shown = receipts[:TODAY_RECEIPTS_SHOWN]
        return {
            "data": data["oggi"],
            "numero_scontrini": data["numero_scontrini_oggi"],
            "scontrini_visualizzati": shown,
            "elenco_troncato": len(shown) < len(receipts),
        }


class SpesaRecentSensor(SpesaEntity, SensorEntity):
    """Ultimi scontrini registrati.

    Lo stato e' il numero di scontrini attualmente in archivio: un conteggio
    corrente, che puo' anche diminuire quando si usa spesa.elimina_scontrino.
    L'elenco vero sta negli attributi, senza gli articoli per non appesantire
    il payload.
    """

    _attr_icon = "mdi:receipt-text"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "ultimi_scontrini")

    @property
    def native_value(self) -> int:
        return self._manager.summary()["scontrini_totali"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"scontrini": self._manager.recent_receipts()}


class SpesaReviewSensor(SpesaEntity, SensorEntity):
    """Scontrini che richiedono una verifica."""

    _attr_icon = "mdi:alert-decagram"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "da_verificare")

    @property
    def native_value(self) -> int:
        return len(self._manager.review_queue())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Coda di verifica, piu' lo stato di salute dell'archivio.

        I problemi strutturali stanno qui perche' e' il sensore che la
        dashboard mostra gia' in evidenza: non serve una settima entita' per
        comunicare che un mese e' escluso o che l'archivio e' bloccato.
        """
        return {
            "scontrini": self._manager.review_queue(),
            "motivi_possibili": REVIEW_LABELS,
            "mesi_esclusi": sorted(self._manager.degraded_months),
            "dettaglio_mesi_esclusi": self._manager.degraded_months,
            "archivio_bloccato": self._manager.journal_blocked is not None,
            "motivo_blocco": self._manager.journal_blocked,
            "violazioni": [v["detail"] for v in self._manager.invariant_violations],
        }


class SpesaDetailSensor(SpesaEntity, SensorEntity):
    """Dettaglio dello scontrino selezionato.

    Segue select.spesa_scontrino: lo stato e' il receipt_id corrente, gli
    attributi contengono lo scontrino completo con tutti i suoi articoli.
    Tenere qui un solo scontrino invece di venti mantiene il payload piccolo
    anche con archivi grandi.
    """

    _attr_icon = "mdi:receipt-text-outline"

    def __init__(self, manager, selection, entry_id) -> None:
        super().__init__(manager, selection, entry_id, "dettaglio")

    @property
    def native_value(self) -> str | None:
        return self._selection.receipt_id

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        receipt = self._selection.receipt
        if receipt is None:
            return {"presente": False}

        described = self._manager.describe_receipt(receipt)
        described["presente"] = True
        described["articolo_selezionato"] = self._selection.item_id
        # Scarto fra somma articoli e totale stampato: la dashboard lo mostra
        # accanto allo stato di verifica, cosi' si capisce subito di quanto
        # non torna il conto.
        described["scarto"] = round(
            described["items_total"] - described["receipt_total"], 2
        )
        described["esclusi"] = round(
            described["items_total"] - described["included_total"], 2
        )
        return described
