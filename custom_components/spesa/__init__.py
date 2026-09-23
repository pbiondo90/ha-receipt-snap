"""Integrazione Spesa alimentare.

Registra l'archivio, gli endpoint HTTP, le entita' e i servizi.

Struttura del setup:

  store       -> accesso al disco, journal, backup
  manager     -> stato in memoria, lock, transazioni, statistiche
  selection   -> stato di interfaccia della dashboard
  view        -> endpoint /api/spesa/receipt, singleton di processo
  view_veryfi -> endpoint /api/spesa/receipt/veryfi, singleton di processo
  piattaforme -> sensor, select, number, text, button
  servizi     -> 7 azioni, registrate in async_setup

Le service action sono registrate in async_setup e NON in async_setup_entry:
devono restare note anche quando la config entry non e' caricata, cosi'
automazioni e script che le richiamano restano validi. E' l'handler a
restituire un errore leggibile se l'integrazione non e' attiva.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    API_PATH,
    API_PATH_VERYFI,
    CATEGORIES,
    CONF_INITIAL_TOKEN_SHOWN,
    CONF_TOKEN,
    DATA_DIR_NAME,
    DOMAIN,
    ITEM_EDITABLE_FIELDS,
    NOTIF_CORRUPT_ID,
    NOTIF_INVARIANT_ID,
    NOTIF_RECOVERY_ID,
    NOTIF_TOKEN_ID,
    PLATFORMS,
    RECEIPT_EDITABLE_FIELDS,
    SERVICE_DELETE_ITEM,
    SERVICE_DELETE_RECEIPT,
    SERVICE_FIELD_DISMISS_DUPLICATE,
    SERVICE_RECALC,
    SERVICE_REGEN_TOKEN,
    SERVICE_UNBLOCK,
    SERVICE_UPDATE_ITEM,
    SERVICE_UPDATE_RECEIPT,
    TOKEN_HEADER,
)
from .entity import SpesaSelection
from .http import get_or_register_veryfi_view, get_or_register_view
from .manager import DuplicateReceiptError, ManagerError, NotFoundError, SpesaManager
from .model import ValidationError
from .store import MonthDegradedError, SpesaStore, StoreError
from .util import generate_token

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Chiavi delle due view in hass.data[DOMAIN][entry_id]. Vengono disattivate
# insieme all'unload e riattivate insieme al reload.
VIEW_KEYS = ("view", "view_veryfi")


# --------------------------------------------------------------------------- #
# Schemi dei servizi
# --------------------------------------------------------------------------- #

_ITEM_TARGET = {
    vol.Required("receipt_id"): cv.string,
    vol.Required("item_id"): cv.string,
}

UPDATE_ITEM_SCHEMA = vol.Schema(
    {
        **_ITEM_TARGET,
        vol.Optional("included"): cv.boolean,
        vol.Optional("name"): cv.string,
        vol.Optional("category"): vol.In(CATEGORIES),
        vol.Optional("price"): vol.Coerce(float),
        vol.Optional("quantity"): vol.Coerce(float),
        vol.Optional("unit_price"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("discount"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("notes"): vol.Any(None, cv.string),
        vol.Optional("product_id"): vol.Any(None, cv.string),
    }
)

UPDATE_RECEIPT_SCHEMA = vol.Schema(
    {
        vol.Required("receipt_id"): cv.string,
        vol.Optional("date"): cv.string,
        vol.Optional("time"): vol.Any(None, cv.string),
        vol.Optional("store"): cv.string,
        vol.Optional("receipt_total"): vol.Coerce(float),
        vol.Optional("notes"): vol.Any(None, cv.string),
        vol.Optional("manual_review"): cv.boolean,
        vol.Optional(SERVICE_FIELD_DISMISS_DUPLICATE): cv.boolean,
    }
)

# vol.Required(...): True accetta letteralmente il solo valore True: il campo
# non e' un booleano qualsiasi e una chiamata senza conferma fallisce prima di
# raggiungere il manager.
DELETE_ITEM_SCHEMA = vol.Schema({**_ITEM_TARGET, vol.Required("conferma"): True})
DELETE_RECEIPT_SCHEMA = vol.Schema(
    {vol.Required("receipt_id"): cv.string, vol.Required("conferma"): True}
)
RECALC_SCHEMA = vol.Schema({})
REGEN_TOKEN_SCHEMA = vol.Schema({vol.Required("conferma"): True})
UNBLOCK_SCHEMA = vol.Schema({vol.Optional("conferma", default=False): cv.boolean})


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Nessuna configurazione YAML: l'integrazione si aggiunge dalla UI.

    Le service action vengono registrate QUI, come richiesto dalle linee guida
    di Home Assistant: devono restare note anche quando la config entry non e'
    caricata.
    """
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Monta store, manager, endpoint, entita' e notifiche."""
    data_dir = Path(hass.config.path(DATA_DIR_NAME))
    store = SpesaStore(data_dir)

    try:
        await hass.async_add_executor_job(store.ensure_dir)
    except StoreError as err:
        raise ConfigEntryNotReady(
            f"Cartella dati {data_dir} non utilizzabile: {err}"
        ) from err

    manager = SpesaManager(hass, store)

    try:
        await manager.async_load()
    except Exception as err:  # noqa: BLE001 - il caricamento non deve far cadere HA
        _LOGGER.exception("Caricamento dell'archivio spese fallito")
        raise ConfigEntryNotReady(f"Archivio spese non caricabile: {err}") from err

    token: str = entry.data[CONF_TOKEN]

    # Token iniziale PRIMA di montare qualunque risorsa runtime.
    #
    # La notifica e' attesa e puo' fallire: se accade qui, il setup viene
    # ritentato senza lasciare dietro di se' endpoint attivi, entita' montate
    # o stato in hass.data da ripulire. Il flag resta False, quindi il token
    # verra' mostrato al tentativo successivo.
    #
    # Se invece la notifica riesce e un passaggio successivo fallisce, il
    # token non e' perso: la notifica persistente esiste gia' e il valore
    # nella config entry non cambia.
    if not entry.data.get(CONF_INITIAL_TOKEN_SHOWN, False):
        try:
            await _async_notify_token(hass, token, initial=True)
        except Exception as err:  # noqa: BLE001
            raise ConfigEntryNotReady(
                "Non e' stato possibile creare la notifica contenente il token "
                f"iniziale: {err}"
            ) from err
        # Il flag si alza SOLO dopo che la notifica e' stata creata.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_INITIAL_TOKEN_SHOWN: True}
        )

    selection = SpesaSelection(hass, manager)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "manager": manager,
        "store": store,
        "selection": selection,
        "entry": entry,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # View singleton: registrate una sola volta per la vita del processo,
    # riattivate con manager e token correnti a ogni reload.
    stored = hass.data[DOMAIN][entry.entry_id]
    stored["view"] = get_or_register_view(hass, manager, token)
    stored["view_veryfi"] = get_or_register_veryfi_view(hass, manager, token)

    await _async_notify_startup_state(hass, manager)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info(
        "Integrazione Spesa pronta: %d scontrini, endpoint %s e %s, dati in %s",
        sum(len(r) for r in manager.months.values()),
        API_PATH,
        API_PATH_VERYFI,
        data_dir,
    )
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Riallinea entrambe le view quando i dati della entry cambiano."""
    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if stored is None:
        return
    for key in VIEW_KEYS:
        if key in stored:
            stored[key].update_token(entry.data[CONF_TOKEN])


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Smonta l'integrazione.

    Le route HTTP non sono rimovibili, quindi le view vengono DISABILITATE: da
    quel momento una POST riceve 503 e nessuna richiesta puo' raggiungere il
    manager di questa sessione.

    I servizi NON vengono rimossi: restano registrati per tutta la vita del
    processo, coerentemente con la registrazione in async_setup.
    """
    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if stored is not None:
        for key in VIEW_KEYS:
            if key in stored:
                stored[key].disable()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        # Lo smontaggio non e' avvenuto: l'integrazione resta viva e le view
        # vanno riattivate, altrimenti gli endpoint resterebbero morti.
        if stored is not None:
            for key in VIEW_KEYS:
                if key in stored:
                    stored[key].update_runtime(stored["manager"], entry.data[CONF_TOKEN])
        return False

    hass.data[DOMAIN].pop(entry.entry_id, None)
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN, None)

    _LOGGER.info("Integrazione Spesa smontata; endpoint disattivati")
    return True


# --------------------------------------------------------------------------- #
# Notifiche
# --------------------------------------------------------------------------- #


def _notify(hass: HomeAssistant, notification_id: str, title: str, message: str) -> None:
    """Notifica informativa, inviata senza attendere.

    Usata per recovery, blocchi e violazioni: sono avvisi di stato, gia'
    presenti nei log e ricostruibili, quindi la loro consegna non deve
    rallentare il setup.
    """
    hass.async_create_task(
        hass.services.async_call(
            "persistent_notification",
            "create",
            {"notification_id": notification_id, "title": title, "message": message},
            blocking=False,
        )
    )


async def _async_notify_token(hass: HomeAssistant, token: str, *, initial: bool) -> None:
    """Mostra il token. UNICO punto in cui compare in chiaro.

    Attesa (blocking=True) e non un task: il chiamante deve sapere con certezza
    se la notifica e' stata creata, perche' da quell'esito dipendono il flag
    initial_token_shown e, per la rigenerazione, l'attivazione del nuovo token.

    Il token non viene mai scritto nei log, negli attributi delle entita', nei
    nomi o nei file dell'archivio.
    """
    # Indirizzo esterno dell'istanza, non un dominio scritto a mano: ogni
    # installazione mostra il proprio. prefer_external punta all'URL che il
    # telefono puo' raggiungere da fuori casa.
    try:
        base_url = get_url(hass, prefer_external=True, allow_internal=True)
    except NoURLAvailableError:
        base_url = "https://IL-TUO-INDIRIZZO-HOME-ASSISTANT"
    intro = (
        "L'integrazione Spesa e' stata configurata."
        if initial
        else (
            "Il token e' stato rigenerato. Subito dopo la creazione di questa "
            "notifica l'integrazione attiva il nuovo token e revoca il precedente: "
            "aggiorna il Comando Rapido con il valore qui sotto."
        )
    )
    recovery_hint = (
        ""
        if initial
        else (
            "\n- Se il nuovo token non dovesse funzionare, richiama "
            "`spesa.rigenera_token`: il precedente potrebbe essere rimasto attivo "
            "perche' Home Assistant si e' interrotto durante la rigenerazione."
        )
    )
    fence = "`" * 3
    message = (
        f"{intro}\n\n"
        "Questo e' il token dedicato all'invio degli scontrini. "
        "**Viene mostrato una sola volta**: copialo ora nel Comando Rapido iOS "
        "prima di chiudere questa notifica.\n\n"
        f"{fence}\n{token}\n{fence}\n\n"
        "**Come usarlo nel Comando Rapido**\n\n"
        f"- Payload gia' pronto: `{base_url}{API_PATH}`\n"
        f"- Risposta Veryfi: `{base_url}{API_PATH_VERYFI}`\n"
        "- Metodo: `POST`\n"
        f"- Header: `{TOKEN_HEADER}` con il valore qui sopra\n"
        "- Header: `Content-Type` con valore `application/json`\n\n"
        "**Note di sicurezza**\n\n"
        "- Non e' un token di Home Assistant: chi lo possiede puo' solo inviare "
        "scontrini, non controllare la casa ne' leggere lo stato delle entita'.\n"
        "- Non condividerlo e non inserirlo in dashboard, automazioni o backup "
        "di testo.\n"
        "- Se lo perdi o sospetti che sia compromesso, chiama il servizio "
        "`spesa.rigenera_token`: ne ricevi uno nuovo in una notifica come questa "
        f"e il precedente viene revocato.{recovery_hint}\n\n"
        "Puoi chiudere questa notifica dopo aver copiato il token."
    )
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "notification_id": NOTIF_TOKEN_ID,
            "title": "Spesa - token dell'endpoint scontrini",
            "message": message,
        },
        blocking=True,
    )


async def _async_notify_startup_state(hass: HomeAssistant, manager: SpesaManager) -> None:
    """Segnala recovery, blocchi e violazioni delle invarianti."""
    report = manager.recovery_report
    if report and report.get("status") == "rolled_back":
        restored = ", ".join(report.get("restored") or []) or "nessuno"
        removed = ", ".join(report.get("removed") or []) or "nessuno"
        _notify(
            hass,
            NOTIF_RECOVERY_ID,
            "Spesa - transazione interrotta ripristinata",
            "Home Assistant si e' interrotto durante un'operazione sull'archivio "
            "spese. All'avvio l'archivio e' stato riportato allo stato precedente."
            f"\n\n- Mesi ripristinati: {restored}"
            f"\n- Mesi rimossi perche' non esistevano prima: {removed}"
            "\n\n**L'ultima operazione in corso e' andata persa.** Controlla in "
            "dashboard che l'ultimo scontrino inserito o l'ultima modifica siano "
            "quelli attesi e, se serve, ripetili.",
        )

    if manager.journal_blocked:
        _notify(
            hass,
            NOTIF_CORRUPT_ID,
            "Spesa - archivio in sola lettura",
            f"{manager.journal_blocked}"
            "\n\nFinche' questo blocco e' attivo non e' possibile registrare nuovi "
            "scontrini ne' modificare quelli esistenti. Gli endpoint rispondono con "
            "codice 423 e i dati sul disco non vengono toccati."
            "\n\n**Cosa fare**"
            "\n\n1. Ispeziona i file in `/config/spesa` con File editor."
            "\n2. Confronta gli ultimi scontrini con quelli che ti aspetti."
            "\n3. Chiama `spesa.sblocca_archivio` **senza** conferma per una "
            "diagnosi: ti dira' se i file sono formalmente validi."
            "\n4. Solo se sei convinto che lo stato sia corretto, richiamalo con "
            "`conferma: true` per accettarlo come autorevole.",
        )

    if manager.invariant_violations:
        details = "\n".join(
            f"- {violation['detail']}" for violation in manager.invariant_violations[:10]
        )
        affected = sorted({m for v in manager.invariant_violations for m in v["months"]})
        _notify(
            hass,
            NOTIF_INVARIANT_ID,
            "Spesa - conflitti nell'archivio",
            "Sono stati trovati dati che violano le invarianti fondamentali "
            "dell'archivio:"
            f"\n\n{details}"
            "\n\n**I mesi coinvolti sono esclusi dalle statistiche e dalle "
            f"scritture**: {', '.join(affected)}."
            "\n\nI file sul disco **non** sono stati modificati. Correggili in "
            "`/config/spesa` rimuovendo o rinominando gli scontrini in conflitto, "
            "poi chiama `spesa.ricalcola`: se le invarianti tornano valide il "
            "blocco sparisce da solo.",
        )


async def _async_dismiss(hass: HomeAssistant, notification_id: str) -> None:
    await hass.services.async_call(
        "persistent_notification",
        "dismiss",
        {"notification_id": notification_id},
        blocking=True,
    )


# --------------------------------------------------------------------------- #
# Servizi
# --------------------------------------------------------------------------- #


def _get_stored(hass: HomeAssistant) -> dict[str, Any]:
    """Dati della entry attiva, o errore leggibile se non c'e'."""
    for stored in hass.data.get(DOMAIN, {}).values():
        if "manager" in stored:
            return stored
    raise ServiceValidationError(
        "L'integrazione Spesa non e' attiva: controlla il suo stato in Impostazioni, "
        "Dispositivi e servizi."
    )


def _get_manager(hass: HomeAssistant) -> SpesaManager:
    return _get_stored(hass)["manager"]


def _response(call: ServiceCall, result: ServiceResponse) -> ServiceResponse | None:
    """Restituisce i dati solo quando il chiamante li ha richiesti.

    Contratto di SupportsResponse.OPTIONAL: l'handler deve consultare
    call.return_response e restituire None quando e' False.
    """
    return result if call.return_response else None


def _translate(err: Exception) -> HomeAssistantError:
    """Traduce le eccezioni del dominio in messaggi leggibili nella UI.

    ServiceValidationError per quanto dipende da input o stato dell'utente:
    Home Assistant lo presenta come errore d'uso e non stampa lo stack trace
    nel log. HomeAssistantError per i guasti veri, che nel log vanno visti.

    Contratto degli errori di persistenza:
      StoreError       operazione annullata, stato precedente ripristinato
      ConsistencyError stato non verificabile, archivio in sola lettura
    La seconda deriva da ManagerError e viene intercettata prima.
    """
    if isinstance(err, ValidationError):
        return ServiceValidationError("Dati non validi: " + "; ".join(err.errors[:5]))
    if isinstance(err, DuplicateReceiptError):
        return ServiceValidationError(
            f"Operazione annullata: renderebbe questo scontrino identico a "
            f"{err.existing_receipt_id}. Due scontrini non possono coincidere "
            "articolo per articolo."
        )
    if isinstance(err, NotFoundError):
        return ServiceValidationError(str(err))
    if isinstance(err, MonthDegradedError):
        return ServiceValidationError(f"Il mese {err.month} non e' scrivibile: {err.reason}")
    if isinstance(err, ManagerError):
        return HomeAssistantError(str(err))
    if isinstance(err, StoreError):
        return HomeAssistantError(
            f"Salvataggio fallito: {err}. L'operazione e' stata annullata e lo stato "
            "precedente ripristinato, puoi riprovare."
        )
    return HomeAssistantError(f"Errore imprevisto: {err}")


def _register_services(hass: HomeAssistant) -> None:
    """Registra i 7 servizi. Idempotente: gia' presenti, non li ridefinisce."""
    if hass.services.has_service(DOMAIN, SERVICE_UPDATE_ITEM):
        return

    async def _update_item(call: ServiceCall) -> ServiceResponse | None:
        manager = _get_manager(hass)
        changes = {k: v for k, v in call.data.items() if k in ITEM_EDITABLE_FIELDS}
        if not changes:
            raise ServiceValidationError(
                "Nessun campo da modificare. Campi ammessi: "
                + ", ".join(sorted(ITEM_EDITABLE_FIELDS))
            )
        try:
            result = await manager.async_update_item(
                call.data["receipt_id"], call.data["item_id"], changes
            )
        except Exception as err:
            raise _translate(err) from err
        return _response(call, result)

    async def _update_receipt(call: ServiceCall) -> ServiceResponse | None:
        manager = _get_manager(hass)
        changes: dict[str, Any] = {
            k: v for k, v in call.data.items() if k in RECEIPT_EDITABLE_FIELDS
        }
        # Il campo del servizio ha un nome parlante; internamente e' lo stato
        # possible_duplicate_dismissed, che conserva il riferimento storico.
        if SERVICE_FIELD_DISMISS_DUPLICATE in call.data:
            changes["possible_duplicate_dismissed"] = call.data[
                SERVICE_FIELD_DISMISS_DUPLICATE
            ]
        if not changes:
            raise ServiceValidationError("Nessun campo da modificare")
        try:
            result = await manager.async_update_receipt(call.data["receipt_id"], changes)
        except Exception as err:
            raise _translate(err) from err
        return _response(call, result)

    async def _delete_item(call: ServiceCall) -> ServiceResponse | None:
        manager = _get_manager(hass)
        try:
            result = await manager.async_delete_item(
                call.data["receipt_id"], call.data["item_id"]
            )
        except Exception as err:
            raise _translate(err) from err
        return _response(call, result)

    async def _delete_receipt(call: ServiceCall) -> None:
        manager = _get_manager(hass)
        try:
            await manager.async_delete_receipt(call.data["receipt_id"])
        except Exception as err:
            raise _translate(err) from err

    async def _recalc(call: ServiceCall) -> ServiceResponse | None:
        manager = _get_manager(hass)
        try:
            result = await manager.async_reload()
        except Exception as err:
            raise _translate(err) from err

        # Le notifiche riflettono lo stato DOPO la rilettura: se i problemi
        # sono stati risolti spariscono, altrimenti riappaiono aggiornate.
        for notification_id in (NOTIF_INVARIANT_ID, NOTIF_CORRUPT_ID, NOTIF_RECOVERY_ID):
            await _async_dismiss(hass, notification_id)
        await _async_notify_startup_state(hass, manager)
        return _response(call, result)

    async def _regen_token(call: ServiceCall) -> ServiceResponse | None:
        """Rigenera il token degli endpoint.

        Ordine deliberato: PRIMA la notifica, POI la revoca. La notifica e'
        l'unico canale attraverso cui l'utente puo' conoscere il nuovo token,
        quindi la rigenerazione non e' completata finche' quel valore non e'
        stato reso visibile.

        Un crash fra la notifica e l'aggiornamento della entry lascia una
        notifica con un token mai attivato: basta ripetere il servizio, e nel
        frattempo il token precedente continua a funzionare.
        """
        stored = _get_stored(hass)
        entry: ConfigEntry = stored["entry"]
        new_token = generate_token()

        try:
            await _async_notify_token(hass, new_token, initial=False)
        except Exception as err:
            _LOGGER.error(
                "Impossibile mostrare il nuovo token; rigenerazione annullata: %s", err
            )
            raise HomeAssistantError(
                "Non e' stato possibile creare la notifica con il nuovo token. Nulla "
                "e' stato modificato: il token precedente e' ancora valido."
            ) from err

        # Da qui il nuovo token diventa autorevole. Merge esplicito: mai
        # ricostruire entry.data da zero, per non perdere initial_token_shown
        # ne' campi aggiunti da versioni future.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: new_token}
        )
        # Applicazione immediata a ENTRAMBE le view: update_token incrementa la
        # generation, quindi anche una richiesta gia' in volo autenticata col
        # vecchio token viene invalidata prima dell'ingest.
        for key in VIEW_KEYS:
            if key in stored:
                stored[key].update_token(new_token)

        _LOGGER.warning(
            "Token degli endpoint scontrini rigenerato: il precedente e' stato revocato"
        )
        return _response(call, {"rigenerato": True, "header": TOKEN_HEADER})

    async def _unblock(call: ServiceCall) -> ServiceResponse | None:
        manager = _get_manager(hass)
        try:
            result = await manager.async_unblock(confirm=call.data["conferma"])
        except Exception as err:
            raise _translate(err) from err

        if result.get("sbloccato"):
            await _async_dismiss(hass, NOTIF_CORRUPT_ID)
        return _response(call, result)

    registrations = (
        (SERVICE_UPDATE_ITEM, _update_item, UPDATE_ITEM_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_UPDATE_RECEIPT, _update_receipt, UPDATE_RECEIPT_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_DELETE_ITEM, _delete_item, DELETE_ITEM_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_DELETE_RECEIPT, _delete_receipt, DELETE_RECEIPT_SCHEMA, SupportsResponse.NONE),
        (SERVICE_RECALC, _recalc, RECALC_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_REGEN_TOKEN, _regen_token, REGEN_TOKEN_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_UNBLOCK, _unblock, UNBLOCK_SCHEMA, SupportsResponse.OPTIONAL),
    )
    for name, handler, schema, supports in registrations:
        hass.services.async_register(
            DOMAIN, name, handler, schema=schema, supports_response=supports
        )

    _LOGGER.debug("Servizi Spesa registrati: %d", len(registrations))
