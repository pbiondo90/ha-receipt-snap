"""Endpoint HTTP dell'integrazione Spesa alimentare.

  POST /api/spesa/receipt          payload gia' nel nostro schema
  POST /api/spesa/receipt/veryfi   risposta Veryfi, tradotta prima dell'ingest

Autenticazione: header X-Spesa-Token, credenziale dedicata a questa sola
funzione, identica per entrambi gli endpoint. Le view dichiarano
requires_auth = False e quindi bypassano il middleware di Home Assistant: e'
l'unico modo per avere un token che non sia un Long-Lived Access Token, il
quale darebbe accesso a tutta l'API.

L'header custom e' obbligatorio, non una preferenza stilistica: il middleware
di Home Assistant intercetta Authorization PRIMA della view e risponde 401 se
il bearer non e' un token HA valido, quindi una richiesta con
Authorization: Bearer <token_spesa> non arriverebbe mai qui.

Superficie in caso di compromissione del token: inserire scontrini
nell'archivio spese. Nessun accesso a stati, servizi o WebSocket.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    API_PATH,
    API_PATH_VERYFI,
    AUTH_LOG_THROTTLE_S,
    BODY_CHUNK_SIZE,
    BODY_READ_TIMEOUT_S,
    DOMAIN,
    ERR_ARCHIVE_LOCKED,
    ERR_DUPLICATE,
    ERR_INTEGRATION_UNAVAILABLE,
    ERR_INTERNAL,
    ERR_INVALID_JSON,
    ERR_INVALID_PAYLOAD,
    ERR_INVALID_VERYFI,
    ERR_MISSING_SCHEMA,
    ERR_MONTH_DEGRADED,
    ERR_PAYLOAD_TOO_LARGE,
    ERR_REQUEST_TIMEOUT,
    ERR_UNAUTHORIZED,
    ERR_UNSUPPORTED_SCHEMA,
    MAX_BODY_BYTES,
    MAX_BODY_BYTES_VERYFI,
    TOKEN_HEADER,
    VIEW_KEY,
    VIEW_KEY_VERYFI,
)
from .manager import DuplicateReceiptError, ManagerError, SpesaManager
from .model import ValidationError
from .store import MonthDegradedError, StoreError
from .veryfi import VeryfiPayloadError
from .veryfi import transform as veryfi_transform

_LOGGER = logging.getLogger(__name__)


def _json_response(status: int, payload: dict[str, Any]) -> web.Response:
    """Risposta JSON esplicita.

    Costruita a mano invece di usare self.json() per controllare interamente
    corpo e header: il Comando Rapido iOS legge sia lo status sia il campo
    'error', e il formato deve restare stabile nel tempo.
    """
    return web.Response(
        status=status,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


class _PayloadTooLarge(Exception):
    """Il corpo supera il limite della view."""


class SpesaReceiptView(HomeAssistantView):
    """Riceve uno scontrino in JSON e lo affida al manager.

    LIFECYCLE
    ---------
    Home Assistant non permette di rimuovere una route gia' registrata, quindi
    questa view e' un singleton per la vita del processo:

      primo setup   -> istanza creata, route registrata, runtime attivo
      unload        -> disable(): la route resta ma i riferimenti sono
                       rilasciati e una POST riceve 503
      reload        -> update_runtime(): STESSA istanza, nuovo manager, token
                       corrente, runtime riattivato

    La `generation` cambia a ogni attivazione, sostituzione o disattivazione.
    Una richiesta in volo la cattura all'ingresso e la riverifica prima
    dell'ingest: se e' cambiata, il runtime sotto di lei non e' piu' quello con
    cui era stata autenticata.

    ESTENSIONE
    ----------
    Le sottoclassi possono sovrascrivere _max_body per accettare corpi piu'
    grandi e _translate_payload per convertire un formato esterno nel nostro
    schema. Tutto il resto - autenticazione, limiti, timeout, codici di
    errore - resta identico.
    """

    url = API_PATH
    name = f"api:{DOMAIN}:receipt"
    requires_auth = False  # autenticazione interna, vedi il docstring del modulo

    # Limite del corpo, sovrascrivibile dalle sottoclassi.
    _max_body = MAX_BODY_BYTES

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._manager: SpesaManager | None = None
        self._token: str | None = None
        self._generation = 0
        self._last_auth_log = 0.0
        self._auth_failures = 0

    # --------------------------------------------------------- lifecycle #

    def update_runtime(self, manager: SpesaManager, token: str) -> None:
        """Attiva o riattiva la view con manager e token correnti."""
        self._generation += 1
        self._manager = manager
        self._token = token

    def update_token(self, token: str) -> None:
        """Sostituisce il solo token, lasciando invariato il manager.

        Incrementa comunque la generation: una richiesta autenticata con il
        token precedente e ancora in corso viene invalidata prima dell'ingest.
        E' la semantica giusta per spesa.rigenera_token, che esiste anche come
        revoca dopo una compromissione: un token revocato non deve poter
        completare un'operazione iniziata un istante prima.
        """
        self._generation += 1
        self._token = token

    def disable(self) -> None:
        """Disattiva la view durante l'unload."""
        self._generation += 1
        self._manager = None
        self._token = None

    @property
    def enabled(self) -> bool:
        return self._manager is not None and self._token is not None

    # ------------------------------------------------------------- token #

    @staticmethod
    def _authorized(request: web.Request, token: str) -> bool:
        """Confronto in tempo costante contro il token catturato all'ingresso.

        hmac.compare_digest evita che il tempo di risposta riveli quanti
        caratteri iniziali del token sono corretti.
        """
        presented = request.headers.get(TOKEN_HEADER)
        if not presented:
            return False
        return hmac.compare_digest(presented, token)

    def _log_unauthorized(self, request: web.Request, reason: str) -> None:
        """Log con throttling: una riga al minuto anche sotto scansione.

        Il token presentato non viene mai registrato, nemmeno parzialmente.
        """
        self._auth_failures += 1
        now = time.monotonic()
        if now - self._last_auth_log < AUTH_LOG_THROTTLE_S:
            return
        self._last_auth_log = now
        _LOGGER.warning(
            "Richiesta non autorizzata su %s da %s (%s). Tentativi falliti dall'avvio: %d.",
            self.url,
            request.remote or "origine sconosciuta",
            reason,
            self._auth_failures,
        )

    # ------------------------------------------------------------- corpo #

    async def _read_limited_body(self, request: web.Request) -> bytes:
        """Legge il corpo a chunk fino a EOF o al superamento del limite.

        Una singola read(n) su uno StreamReader puo' restituire meno di n byte
        anche senza EOF: la suddivisione dipende da TCP e dal buffering di
        aiohttp. Leggere una volta sola lascerebbe passare un corpo
        sovradimensionato come JSON troncato, classificato 400 invece di 413.

        Il ciclo si interrompe appena il totale supera il limite, senza
        consumare il resto dello stream.
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await request.content.read(BODY_CHUNK_SIZE)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > self._max_body:
                raise _PayloadTooLarge
            chunks.append(chunk)

    # --------------------------------------------------------- risposte #

    def _too_large(self) -> web.Response:
        return _json_response(
            413,
            {
                "success": False,
                "error": ERR_PAYLOAD_TOO_LARGE,
                "message": f"Il corpo supera il limite di {self._max_body} byte",
                "retry_safe": False,
            },
        )

    @staticmethod
    def _unavailable(message: str) -> web.Response:
        return _json_response(
            503,
            {"success": False, "error": ERR_INTEGRATION_UNAVAILABLE, "message": message},
        )

    # ------------------------------------------------------- traduzione #

    def _translate_payload(self, payload: Any) -> Any:
        """Gancio per le sottoclassi.

        Qui il payload e' gia' nel nostro schema, quindi non serve alcuna
        conversione. SpesaVeryfiView lo sovrascrive.
        """
        return payload

    # -------------------------------------------------------------- POST #

    async def post(self, request: web.Request) -> web.Response:
        """Riceve, valida e registra uno scontrino.

          201 registrato
          400 payload non valido / JSON malformato / schema mancante o ignoto
          401 token assente o errato
          408 corpo non completato entro il timeout
          409 duplicato certo
          413 corpo oltre il limite
          423 archivio bloccato o mese non scrivibile
          503 integrazione non attiva o runtime sostituito durante la richiesta
          500 errore interno
        """
        # 0. Cattura atomica del runtime: manager, token e generation vengono
        #    letti insieme, senza await in mezzo, quindi appartengono per certo
        #    allo stesso runtime.
        manager = self._manager
        token = self._token
        generation = self._generation

        if manager is None or token is None:
            _LOGGER.warning(
                "Richiesta su %s con integrazione non attiva da %s",
                self.url,
                request.remote or "origine sconosciuta",
            )
            return self._unavailable(
                "L'integrazione Spesa non e' attiva in questo momento. Lo scontrino "
                "NON e' stato registrato: riprova piu' tardi."
            )

        # 1. Autenticazione contro il token CATTURATO, prima di qualunque
        #    lettura del corpo: una richiesta non autenticata non deve poter
        #    far allocare memoria.
        if not self._authorized(request, token):
            self._log_unauthorized(
                request,
                "header assente" if not request.headers.get(TOKEN_HEADER) else "token errato",
            )
            return _json_response(401, {"success": False, "error": ERR_UNAUTHORIZED})

        # 2. Dimensione dichiarata: evita di leggere un corpo dichiaratamente
        #    troppo grande.
        declared = request.content_length
        if declared is not None and declared > self._max_body:
            _LOGGER.warning(
                "Payload rifiutato: %d byte dichiarati, massimo %d", declared, self._max_body
            )
            return self._too_large()

        # 3. Lettura effettiva, con limite reale e timeout complessivo. Il
        #    timeout impedisce a un client autenticato che invia il corpo
        #    lentissimamente di tenere aperta la richiesta a tempo indefinito.
        try:
            async with asyncio.timeout(BODY_READ_TIMEOUT_S):
                body = await self._read_limited_body(request)
        except _PayloadTooLarge:
            _LOGGER.warning("Payload rifiutato: corpo oltre %d byte", self._max_body)
            return self._too_large()
        except TimeoutError:
            _LOGGER.warning(
                "Lettura del corpo interrotta dopo %d s da %s",
                BODY_READ_TIMEOUT_S,
                request.remote or "origine sconosciuta",
            )
            return _json_response(
                408,
                {
                    "success": False,
                    "error": ERR_REQUEST_TIMEOUT,
                    "message": (
                        f"Invio del corpo non completato entro {BODY_READ_TIMEOUT_S} "
                        "secondi. Lo scontrino NON e' stato registrato."
                    ),
                    "retry_safe": True,
                },
            )
        except (ConnectionError, web.HTTPException) as err:
            _LOGGER.warning("Lettura del corpo fallita: %s", err)
            return _json_response(
                400,
                {
                    "success": False,
                    "error": ERR_INVALID_JSON,
                    "message": "Corpo non leggibile",
                    "retry_safe": False,
                },
            )

        if not body.strip():
            return _json_response(
                400,
                {
                    "success": False,
                    "error": ERR_INVALID_JSON,
                    "message": "Corpo vuoto",
                    "retry_safe": False,
                },
            )

        # 4. Parsing.
        try:
            payload = json.loads(body.decode("utf-8"))
        except UnicodeDecodeError:
            return _json_response(
                400,
                {
                    "success": False,
                    "error": ERR_INVALID_JSON,
                    "message": "Il corpo non e' UTF-8 valido",
                    "retry_safe": False,
                },
            )
        except json.JSONDecodeError as err:
            return _json_response(
                400,
                {
                    "success": False,
                    "error": ERR_INVALID_JSON,
                    "message": f"JSON non valido a riga {err.lineno}, colonna {err.colno}",
                    "retry_safe": False,
                },
            )

        # 5. Traduzione, per le sottoclassi che ricevono un formato esterno.
        #    Deterministica: nessun valore economico viene ricalcolato.
        try:
            payload = self._translate_payload(payload)
        except VeryfiPayloadError as err:
            _LOGGER.warning(
                "Payload Veryfi non utilizzabile: %s",
                "; ".join(err.errors[:5]) + (" ..." if len(err.errors) > 5 else ""),
            )
            return _json_response(
                400,
                {
                    "success": False,
                    "error": ERR_INVALID_VERYFI,
                    "message": "La risposta Veryfi non ha la forma attesa",
                    "details": err.errors[:20],
                    "error_count": len(err.errors),
                    "retry_safe": False,
                },
            )
        except Exception:  # noqa: BLE001 - una traduzione rotta non deve dare 500 muto
            _LOGGER.exception("Errore durante la traduzione del payload")
            return _json_response(
                500,
                {
                    "success": False,
                    "error": ERR_INTERNAL,
                    "message": "Errore nella conversione del payload: controlla i log",
                    "retry_safe": False,
                },
            )

        # 6. Riverifica del runtime PRIMA dell'ingest.
        #
        #    Fra la cattura e questo punto ci sono stati await: unload, reload o
        #    rigenerazione del token possono essere avvenuti nel frattempo.
        #    Consegnare il payload al manager catturato significherebbe
        #    scrivere attraverso un runtime che non esiste piu', o con una
        #    credenziale gia' revocata.
        if (
            generation != self._generation
            or self._manager is not manager
            or self._manager is None
        ):
            _LOGGER.warning(
                "Richiesta su %s annullata: il runtime e' cambiato durante la "
                "ricezione del corpo. Lo scontrino non e' stato registrato.",
                self.url,
            )
            return self._unavailable(
                "L'integrazione Spesa e' stata ricaricata durante l'invio. Lo "
                "scontrino NON e' stato registrato: riprova."
            )

        # 7. Delega al manager catturato, ora verificato ancora corrente.
        try:
            result = await manager.async_ingest(payload)

        except ValidationError as err:
            # Copre anche MissingSchemaError e UnsupportedSchemaError, che ne
            # sono sottoclassi e portano il proprio codice.
            code = {
                "missing_schema_version": ERR_MISSING_SCHEMA,
                "unsupported_schema_version": ERR_UNSUPPORTED_SCHEMA,
            }.get(err.code, ERR_INVALID_PAYLOAD)
            _LOGGER.warning(
                "Scontrino rifiutato (%s): %s",
                code,
                "; ".join(err.errors[:5]) + (" ..." if len(err.errors) > 5 else ""),
            )
            return _json_response(
                400,
                {
                    "success": False,
                    "error": code,
                    "message": "Il payload non ha superato la validazione",
                    "details": err.errors[:20],
                    "error_count": len(err.errors),
                    "retry_safe": False,
                },
            )

        except DuplicateReceiptError as err:
            _LOGGER.info(
                "Scontrino duplicato respinto: corrisponde a %s (%s)",
                err.existing_receipt_id,
                err.reason,
            )
            return _json_response(
                409,
                {
                    "success": False,
                    "error": ERR_DUPLICATE,
                    "message": "Questo scontrino risulta gia' registrato",
                    "existing_receipt_id": err.existing_receipt_id,
                    "reason": err.reason,
                    "retry_safe": False,
                },
            )

        except MonthDegradedError as err:
            _LOGGER.error(
                "Ingest rifiutato: mese %s non scrivibile (%s)", err.month, err.reason
            )
            return _json_response(
                423,
                {
                    "success": False,
                    "error": ERR_MONTH_DEGRADED,
                    "message": (
                        f"Il mese {err.month} non e' scrivibile: {err.reason}. "
                        "Lo scontrino NON e' stato registrato."
                    ),
                    "month": err.month,
                    "retry_safe": False,
                },
            )

        except ManagerError as err:
            # Archivio bloccato: journal non risolto, stato incerto dopo un
            # rollback fallito, o transazione non verificabile.
            _LOGGER.error("Ingest rifiutato: %s", err)
            return _json_response(
                423,
                {
                    "success": False,
                    "error": ERR_ARCHIVE_LOCKED,
                    "message": (
                        f"{err} Lo stato dell'archivio non e' verificabile e le "
                        "scritture sono bloccate. NON reinviare lo scontrino finche' "
                        "il problema non e' stato risolto."
                    ),
                    "retry_safe": False,
                },
            )

        except StoreError as err:
            # A questo punto il manager ha gia' ripristinato lo stato
            # precedente: un errore di persistenza che lascia lo stato incerto
            # arriva come ConsistencyError e viene intercettato dal ramo
            # ManagerError con codice 423.
            _LOGGER.error("Ingest fallito per un errore di persistenza: %s", err)
            return _json_response(
                500,
                {
                    "success": False,
                    "error": ERR_INTERNAL,
                    "message": (
                        "Errore durante il salvataggio. L'operazione e' stata annullata "
                        "e lo stato precedente ripristinato: lo scontrino NON e' stato "
                        "registrato. Puoi riprovare."
                    ),
                    "retry_safe": True,
                },
            )

        except Exception:  # noqa: BLE001 - nessuna eccezione deve sfuggire
            _LOGGER.exception("Errore interno durante la registrazione di uno scontrino")
            return _json_response(
                500,
                {
                    "success": False,
                    "error": ERR_INTERNAL,
                    "message": "Errore interno: controlla i log di Home Assistant",
                    "retry_safe": False,
                },
            )

        # 8. Successo. La risposta riporta i totali CALCOLATI da Home Assistant,
        #    cosi' il Comando Rapido mostra l'esito reale e non quello che la
        #    fonte aveva stimato.
        return _json_response(
            201,
            {
                "success": True,
                "receipt_id": result["receipt_id"],
                "date": result["date"],
                "store": result["store"],
                "receipt_total": result["receipt_total"],
                "items_total": result["items_total"],
                "included_total": result["included_total"],
                "item_count": result["item_count"],
                "needs_review": result["needs_review"],
                "review_reasons": result["review_reasons"],
                "possible_duplicate_of": result["possible_duplicate_of"],
            },
        )


class SpesaVeryfiView(SpesaReceiptView):
    """Riceve la risposta di Veryfi e la traduce prima dell'ingest.

    Eredita dalla view principale: stessa autenticazione col token dedicato,
    stessa gestione della generation, stessi codici di errore. L'unica
    differenza e' un passaggio di traduzione fra il parsing e l'ingest, piu'
    un limite di corpo piu' alto.

    La traduzione e' deterministica e vive in veryfi.py: nessun valore
    economico viene ricalcolato o corretto. Il payload che ne esce attraversa
    poi validate_payload() come qualunque altro, quindi le invarianti sui dati
    non dipendono dalla provenienza.
    """

    url = API_PATH_VERYFI
    name = f"api:{DOMAIN}:receipt:veryfi"
    requires_auth = False

    # Il JSON di Veryfi include ocr_text e una cinquantina di metadati: serve
    # un limite piu' alto di quello del payload compatto.
    _max_body = MAX_BODY_BYTES_VERYFI

    def _translate_payload(self, payload: Any) -> Any:
        """Converte il JSON Veryfi nel nostro schema.

        Solleva VeryfiPayloadError quando il JSON non ha la forma attesa; il
        chiamante la traduce in un 400 con l'elenco dei problemi.
        """
        translated, warnings = veryfi_transform(payload)
        for warning in warnings:
            _LOGGER.info("Veryfi: %s", warning)
        return translated


def get_or_register_view(
    hass: HomeAssistant, manager: SpesaManager, token: str
) -> SpesaReceiptView:
    """Restituisce la view singleton, registrandola solo la prima volta.

    Idempotente: al reload della config entry la route NON viene registrata una
    seconda volta, ma l'istanza esistente riceve il nuovo runtime.

    Sincrona di proposito: non contiene await, quindi il prefisso async_
    sarebbe fuorviante nella convenzione di Home Assistant.
    """
    existing: SpesaReceiptView | None = hass.data.get(VIEW_KEY)
    if existing is not None:
        existing.update_runtime(manager, token)
        _LOGGER.debug("Endpoint scontrini riattivato su %s", API_PATH)
        return existing

    view = SpesaReceiptView(hass)
    view.update_runtime(manager, token)
    hass.http.register_view(view)
    hass.data[VIEW_KEY] = view
    _LOGGER.info("Endpoint scontrini registrato su %s", API_PATH)
    return view


def get_or_register_veryfi_view(
    hass: HomeAssistant, manager: SpesaManager, token: str
) -> SpesaVeryfiView:
    """Come get_or_register_view, per l'endpoint Veryfi.

    Chiave separata in hass.data: sono due route distinte, e disattivarne una
    non deve toccare l'altra.
    """
    existing: SpesaVeryfiView | None = hass.data.get(VIEW_KEY_VERYFI)
    if existing is not None:
        existing.update_runtime(manager, token)
        _LOGGER.debug("Endpoint Veryfi riattivato su %s", API_PATH_VERYFI)
        return existing

    view = SpesaVeryfiView(hass)
    view.update_runtime(manager, token)
    hass.http.register_view(view)
    hass.data[VIEW_KEY_VERYFI] = view
    _LOGGER.info("Endpoint Veryfi registrato su %s", API_PATH_VERYFI)
    return view
