"""Utilita' condivise dell'integrazione Spesa alimentare.

Modulo neutro: comportamento comune a piu' moduli, senza dipendenze fra loro.
Le costanti restano in const.py, che contiene valori e non logica.
"""

from __future__ import annotations

import secrets

from .const import TOKEN_BYTES


def generate_token() -> str:
    """Genera il token dedicato all'endpoint scontrini.

    secrets.token_urlsafe(32) produce 256 bit di entropia in 43 caratteri
    dell'alfabeto URL-safe, sicuri in un header HTTP e incollabili in un
    Comando Rapido iOS senza escape.

    Fonte di entropia e lunghezza definite qui una volta sola: la usano il
    config flow alla creazione e spesa.rigenera_token alla revoca.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)
