"""Config flow dell'integrazione Spesa alimentare.

Flusso volutamente minimo: una schermata di conferma, nessun parametro da
inserire. La cartella dati e' fissa, le categorie e le tolleranze stanno in
const.py: non c'e' nulla di configurabile, quindi nessun options flow.

DOVE VIENE MOSTRATO IL TOKEN
----------------------------
NON in questa schermata. Il testo di una create_entry non e' recuperabile dopo
la chiusura del dialogo, e un token perso a quel punto costringerebbe a
rigenerarlo. Il token iniziale viene mostrato in una NOTIFICA PERSISTENTE,
creata da __init__.py al primo setup della entry: resta disponibile finche'
l'utente non la chiude, si consulta dal telefono mentre si configura il
Comando Rapido e sopravvive a un riavvio.

Il token non compare mai: nei log, negli attributi delle entita', nei nomi o
nei file dell'archivio spese. Vive nei dati della config entry
(.storage/core.config_entries) e nella notifica finche' l'utente la tiene.

Questo modulo non viene importato da alcun modulo runtime: la generazione del
token sta in util.py, condivisa con spesa.rigenera_token.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_TOKEN, DOMAIN, TOKEN_HEADER
from .util import generate_token

_LOGGER = logging.getLogger(__name__)

TITLE = "Spesa"


class SpesaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Aggiunta dell'integrazione dalla UI."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Unico passaggio: conferma e creazione.

        La seconda istanza e' impedita da `single_config_entry: true` nel
        manifest: Home Assistant rifiuta l'avvio di un secondo config flow
        quando una entry esiste gia'.

        La verifica della cartella dati NON avviene qui ma in
        async_setup_entry: un fallimento li' produce ConfigEntryNotReady con
        ritentativo automatico, che e' il comportamento corretto di Home
        Assistant e copre anche i permessi cambiati dopo l'installazione.
        """
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=None,
                description_placeholders={"header": TOKEN_HEADER},
            )

        token = generate_token()

        # Nessun log del token, nemmeno troncato: un prefisso e' comunque
        # informazione utile a chi tentasse di indovinarlo.
        _LOGGER.info(
            "Integrazione Spesa configurata; token dedicato generato "
            "(%d caratteri). Verra' mostrato in una notifica persistente.",
            len(token),
        )

        # entry.data contiene la sola configurazione. Il metadata interno
        # initial_token_shown viene aggiunto da __init__.py dopo aver creato
        # con successo la notifica: la sua assenza equivale a False.
        return self.async_create_entry(title=TITLE, data={CONF_TOKEN: token})
