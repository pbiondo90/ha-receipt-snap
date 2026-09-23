"""Trasformazione da risposta Veryfi al payload dell'integrazione Spesa.

Modulo PURO: nessuna dipendenza da Home Assistant, nessun I/O, nessuno stato.
Riceve il JSON restituito da Veryfi e produce il payload che validate_payload()
di model.py accetta, identico a quello che invierebbe ChatGPT.

DIVISIONE DEI COMPITI

Veryfi fornisce struttura, prezzi, sconti, quantita' e descrizioni. Questo
modulo si limita a tradurre in modo DETERMINISTICO: nessun valore economico
viene ricalcolato o corretto, perche' un errore dell'OCR corretto in automatico
diventa un dato falso indistinguibile da uno vero.

L'unico errore noto che NON correggiamo e' il punto decimale perso: un
"3,49" letto come "349". Lo scontrino arriva comunque, total_mismatch lo
segnala e l'utente corregge dalla dashboard. E' esattamente il caso per cui
quel meccanismo esiste.

DIFETTI DI VERYFI GESTITI, tutti verificati su scontrini reali

1. La descrizione include l'etichetta dello sconto su una seconda riga:
   "SUCCHI MIRTILLO MIX S/Z LT1 S\nSOTTOCOSTO NAZ."
   -> si taglia al primo a capo.

2. Il campo total e' AL LORDO dello sconto, e discount arriva negativo.
   Un succo con total 2.09 e discount -0.30 e' stato pagato 1.79.
   -> price = total - abs(discount), original_price = total.

3. Le righe separatrici dello scontrino ("Scontrino Bilancia Nr. 0724",
   "Fine sacchetto") diventano line_items che PORTANO i dati del prodotto
   successivo, lasciandolo senza quantita' e prezzo unitario.
   -> si fondono con la riga seguente.

4. Righe a totale zero (coupon non utilizzati) -> scartate.

5. Quantita' e prezzo unitario possono finire sulla riga sbagliata.
   -> se quantity * price non coincide con total, entrambi vengono scartati
      e la quantita' torna a 1. Il prezzo di riga resta corretto in ogni caso.

CATEGORIE

Assegnate su tre livelli, dal piu' affidabile al meno:
  1. parola chiave nella descrizione
  2. campo type di Veryfi (alcohol, product)
  3. aliquota IVA (4%, 5% e 10% sono beni di prima necessita')
Quando nessuno dei tre e' conclusivo si usa "Altro", che attiva il flag
category_was_unknown e fa comparire l'articolo fra quelli da verificare.
"""

from __future__ import annotations

import html
import re
from typing import Any

# --------------------------------------------------------------------------- #
# Costanti locali del modulo
# --------------------------------------------------------------------------- #

# Tolleranza sul controllo quantity * price == total.
QTY_PRICE_TOLERANCE = 0.011

# Descrizioni che identificano una riga separatrice dello scontrino e non un
# prodotto. Confronto in minuscolo, per sottostringa.
MARKER_HINTS = (
    "scontrino bilancia",
    "fine sacchetto",
    "inizio sacchetto",
    "reparto bilancia",
    "punti sponsor",
    "coupon bruciabile",
    "t.parziale",
    "subtotale",
)

# Aliquote IVA italiane dei beni di prima necessita': alimentari e affini.
FOOD_VAT_RATES = (4, 5, 10)

# --------------------------------------------------------------------------- #
# Classificazione per parole chiave
#
# L'ordine conta: la prima categoria che trova una corrispondenza vince.
# Le piu' specifiche vanno prima, perche' "carta igienica" deve battere
# "carta" e "acqua ossigenata" non deve finire fra le bevande.
# --------------------------------------------------------------------------- #

CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Farmacia",
        (
            "tachipirina", "aspirina", "moment", "oki ", "cerotti", "cerotto",
            "garza", "siringa", "termometro", "integratore", "vitamin",
            "paracetamol", "ibuprofen", "disinfettante", "amuchina",
            "acqua ossigenata", "soluzione fisiologica", "collirio",
        ),
    ),
    (
        "Animali",
        (
            "gatto", "gatti", "cane", "cani", "felix", "whiskas", "friskies",
            "pedigree", "cesar", "kitekat", "lettiera", "crocchette",
            "canaglia", "purina", "monge", "schesir",
        ),
    ),
    (
        "Igiene personale",
        (
            "shampoo", "shamp.", "balsamo", "bagno sc", "bagnoschiuma",
            "doccia", "sapone", "dentifricio", "dent.", "spazzolino",
            "collutorio", "deodorante", "deod.", "rasoio", "schiuma barba",
            "assorbent", "salviett", "salv.", "fazzolett", "cotone",
            "crema viso", "crema corpo", "crema mani", "siero capelli",
            "siero viso", "tinta capelli", "lacca", "gel capelli",
            "profumo", "talco", "pannolin", "carta igienica", "carta ig",
            "struccante", "intim",
        ),
    ),
    (
        "Casa",
        (
            "detersiv", "detergente", "ammorbidente", "candeggina", "sgrassat",
            "anticalcare", "lavastoviglie", "piatti limone", "brillantante",
            "sacchi", "sacc.", "pattumiera", "spugna", "spugne", "panno",
            "straccio", "scopa", "paletta", "guanti", "pile", "batterie",
            "lampadina", "tovagliol", "carta cucina", "scottex", "pellicola",
            "alluminio", "forno carta", "bicchier", "bicch.", "piatti carta",
            "posate", "stuzzicad", "fiammiferi", "accendino", "insetticid",
            "deodorante ambiente", "profumatore", "cera", "lucidante",
        ),
    ),
    (
        "Bevande",
        (
            "acqua", "coca cola", "cocacola", "pepsi", "fanta", "sprite",
            "chinotto", "gassosa", "aranciata", "the freddo", "the'", "tè",
            "succo", "succhi", "nettare", "spremuta", "birra", "peroni",
            "moretti", "heineken", "ichnusa", "vino", "prosecco", "spumante",
            "champagne", "amaro", "grappa", "liquore", "vodka", "gin ",
            "whisky", "rum ", "aperol", "campari", "tonica", "energy drink",
            "red bull", "gatorade", "powerade", "waterstick", "estathe",
        ),
    ),
)


# --------------------------------------------------------------------------- #
# Eccezione
# --------------------------------------------------------------------------- #


class VeryfiPayloadError(Exception):
    """Il JSON ricevuto non ha la forma attesa di una risposta Veryfi."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


# --------------------------------------------------------------------------- #
# Utilita' di testo
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def clean_description(value: Any) -> str:
    """Prima riga della descrizione, senza entita' HTML.

    Veryfi allega l'etichetta dello sconto su una seconda riga della stessa
    descrizione, quindi si taglia al primo a capo.

    Le entita' HTML vengono decodificate: BICCH.CAFFE&#039; e' un artefatto di
    trasporto, non contenuto dello scontrino. Lasciarle renderebbe raw_name, e
    quindi i fingerprint anti-duplicati, dipendente dalle scelte di codifica di
    Veryfi invece che dal testo stampato.
    """
    if not isinstance(value, str):
        return ""
    first = value.split("\n")[0]
    first = html.unescape(first)
    # Alcune righe portano il marcatore di reparto in testa.
    first = re.sub(r"^[A-Z*]\s+(?=[A-Za-z0-9])", "", first.strip())
    return _WS_RE.sub(" ", first).strip()


def slugify_store(value: str) -> str:
    """Nome del negozio ridotto a minuscole e cifre, per il receipt_id."""
    slug = _NON_SLUG_RE.sub("", value.lower())
    return slug or "negozio"


# --------------------------------------------------------------------------- #
# Normalizzazione del nome leggibile
# --------------------------------------------------------------------------- #

# Abbreviazioni ricorrenti sugli scontrini italiani. Chiave in minuscolo,
# confronto sull'intera parola. Estendibile: e' l'unico punto da toccare.
ABBREVIATIONS = {
    "gr": "g", "gr.": "g", "kg.": "kg", "lt": "L", "lt.": "L", "ml.": "ml",
    "cl.": "cl", "pz": "pz", "pz.": "pz", "conf.": "confezione",
    "bicch.": "bicchieri", "sacc.": "sacchi", "patt.": "pattumiera",
    "dent.": "dentifricio", "shamp.": "shampoo", "salv.": "salviette",
    "bisc.": "biscotti", "form.": "formaggio", "gratt.": "grattugiato",
    "prosc.": "prosciutto", "mozz.": "mozzarella", "yog.": "yogurt",
    "lat.": "latte", "latt.": "lattosio", "past.": "pasta",
    "pomod.": "pomodoro", "pom.": "pomodoro", "insapor.": "insaporitore",
    "secc.": "secchi", "porc.": "porcini", "rig.": "rigati",
    "integr.": "integrale", "screm.": "scremato", "ps": "parz. scremato",
    "s/z": "senza zucchero", "s.z.": "senza zucchero",
    "fr.": "frutti", "vegetal.": "vegetale", "artig.": "artigianale",
    "sfogl.": "sfogliata", "crem.": "cremoso", "trip.": "tripack",
    "rigen.": "rigenerante", "proteg.": "proteggente",
    "ig.": "igienica", "prof.": "profumati", "delic.": "delicate",
}

# Parole che restano in maiuscolo: sigle e marche riconoscibili.
KEEP_UPPER = {"dop", "igp", "doc", "docg", "bio", "aa", "aaa", "uht", "led"}


def readable_name(raw: str) -> str | None:
    """Versione leggibile della descrizione, a regole.

    Deliberatamente modesta: espande le abbreviazioni note, normalizza le
    maiuscole e lascia intatto tutto il resto. Copre bene i casi frequenti e
    sbaglia sempre allo stesso modo, il che la rende prevedibile.

    Restituisce SEMPRE un nome quando la descrizione non e' vuota, anche
    quando l'unica differenza dal raw_name sono le maiuscole. E' una scelta
    deliberata: se omettessimo il nome, model.py marcherebbe l'articolo con
    name_was_missing e lo farebbe comparire fra quelli da verificare. Su uno
    scontrino da 65 righe finirebbero in coda decine di articoli perfettamente
    corretti, rendendo inutile la segnalazione.

    Meglio un nome mediocre ma leggibile, correggibile dalla dashboard quando
    davvero infastidisce, che una coda di verifica sempre piena.
    """
    if not raw:
        return None

    words = raw.split()
    out: list[str] = []
    for word in words:
        lower = word.lower()
        if lower in ABBREVIATIONS:
            out.append(ABBREVIATIONS[lower])
            continue
        if lower in KEEP_UPPER:
            out.append(word.upper())
            continue
        # Formati come 500GR, 1LT, 4X25CL: separa cifra e unita'.
        match = re.fullmatch(r"(\d+(?:[.,]\d+)?)(gr|g|kg|lt|l|ml|cl|pz)\.?", lower)
        if match:
            unit = {"gr": "g", "lt": "L", "l": "L"}.get(match.group(2), match.group(2))
            out.append(f"{match.group(1)} {unit}")
            continue
        out.append(word.lower())

    text = _WS_RE.sub(" ", " ".join(out)).strip()
    if not text:
        return None

    # Maiuscola iniziale, il resto invariato per non rovinare le sigle.
    return text[0].upper() + text[1:]


# --------------------------------------------------------------------------- #
# Classificazione
# --------------------------------------------------------------------------- #


def classify(description: str, item_type: Any, tax_rate: Any) -> str:
    """Categoria dell'articolo, su tre livelli decrescenti di affidabilita'.

    1. Parola chiave nella descrizione: la piu' specifica, vince sempre.
    2. Campo type di Veryfi: 'alcohol' e' inequivocabile, 'product' indica
       un non alimentare.
    3. Aliquota IVA: 4%, 5% e 10% sono beni di prima necessita', quindi
       alimentari. Il 22% da solo non basta a decidere fra Casa, Igiene e
       Bevande, quindi si ricade su Altro.

    "Altro" attiva category_was_unknown e fa comparire l'articolo fra quelli da
    verificare: e' una segnalazione onesta di incertezza, non un ripiego
    silenzioso.
    """
    haystack = f" {description.lower()} "

    for category, keywords in CATEGORY_KEYWORDS:
        for keyword in keywords:
            if keyword in haystack:
                return category

    if isinstance(item_type, str):
        lowered = item_type.lower()
        if lowered == "alcohol":
            return "Bevande"
        if lowered in {"product", "non_food", "merchandise"}:
            return "Casa"

    if isinstance(tax_rate, (int, float)) and int(tax_rate) in FOOD_VAT_RATES:
        return "Alimentari"

    return "Altro"


# --------------------------------------------------------------------------- #
# Lettura difensiva dei campi Veryfi
# --------------------------------------------------------------------------- #


def _num(value: Any) -> float | None:
    """Numero o None. Rifiuta booleani, NaN e stringhe non numeriche."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None  # esclude NaN
    if isinstance(value, str):
        try:
            return float(value.replace(",", ".").strip())
        except ValueError:
            return None
    return None


def _looks_like_marker(description: str) -> bool:
    lowered = description.lower()
    return any(hint in lowered for hint in MARKER_HINTS)


def _vendor_name(raw: dict) -> str | None:
    vendor = raw.get("vendor")
    if isinstance(vendor, dict):
        for key in ("name", "raw_name", "vendor_name"):
            value = vendor.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    value = raw.get("vendor_name")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _split_datetime(raw: dict) -> tuple[str | None, str | None]:
    """Veryfi restituisce 'AAAA-MM-GG HH:MM:SS' nel campo date."""
    value = raw.get("date")
    if not isinstance(value, str) or not value.strip():
        return None, None
    parts = value.strip().split()
    date_part = parts[0] if parts else None
    time_part = parts[1][:5] if len(parts) > 1 and len(parts[1]) >= 5 else None
    if date_part and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part):
        date_part = None
    if time_part and not re.fullmatch(r"\d{2}:\d{2}", time_part):
        time_part = None
    return date_part, time_part


# --------------------------------------------------------------------------- #
# Trasformazione delle righe
# --------------------------------------------------------------------------- #


def _prepare_lines(line_items: list[Any]) -> list[dict[str, Any]]:
    """Normalizza le righe Veryfi e applica fusioni e scarti.

    Ritorna una lista di dizionari gia' ripuliti, pronti per la conversione.
    """
    prepared: list[dict[str, Any]] = []
    for entry in line_items:
        if not isinstance(entry, dict):
            continue
        prepared.append(
            {
                "description": clean_description(entry.get("description")),
                "quantity": _num(entry.get("quantity")),
                "price": _num(entry.get("price")),
                "total": _num(entry.get("total")),
                "discount": _num(entry.get("discount")),
                "tax_rate": entry.get("tax_rate"),
                "type": entry.get("type"),
            }
        )

    merged: list[dict[str, Any]] = []
    skip_next = False
    for index, line in enumerate(prepared):
        if skip_next:
            skip_next = False
            continue

        nxt = prepared[index + 1] if index + 1 < len(prepared) else None

        # FUSIONE dei marcatori che hanno assorbito i dati del prodotto.
        #
        # Condizioni tutte necessarie, verificate su scontrini reali:
        #   - questa riga non ha aliquota IVA: i prodotti veri ce l'hanno
        #   - la descrizione corrisponde a un marcatore noto
        #   - questa riga porta un prezzo unitario, la successiva no
        #   - i due totali coincidono: e' lo stesso importo letto due volte
        #
        # Senza tutte e quattro, due prodotti consecutivi allo stesso prezzo
        # verrebbero fusi per errore.
        if (
            nxt is not None
            and line["tax_rate"] is None
            and _looks_like_marker(line["description"])
            and line["price"] is not None
            and nxt["price"] is None
            and line["total"] is not None
            and nxt["total"] is not None
            and abs(line["total"] - nxt["total"]) < 0.005
        ):
            merged.append(
                {
                    **nxt,
                    "quantity": line["quantity"],
                    "price": line["price"],
                    "merged_from_marker": True,
                }
            )
            skip_next = True
            continue

        merged.append(line)

    kept: list[dict[str, Any]] = []
    for line in merged:
        # Righe senza descrizione o senza importo: non sono articoli.
        if not line["description"] or line["total"] is None:
            continue
        # Totale a zero: coupon non utilizzati, righe informative.
        if abs(line["total"]) < 0.005:
            continue
        # Marcatori rimasti soli, senza un prodotto con cui fondersi.
        if line["tax_rate"] is None and _looks_like_marker(line["description"]):
            continue
        kept.append(line)

    return kept


def _finalize_line(line: dict[str, Any], position: int) -> dict[str, Any]:
    """Converte una riga preparata in un articolo del nostro schema."""
    raw_name = line["description"]

    quantity = line["quantity"] if line["quantity"] and line["quantity"] > 0 else 1.0
    unit_price = line["price"]
    total = line["total"]

    # CONTROLLO DI COERENZA su quantita' e prezzo unitario.
    # Veryfi puo' attribuirli alla riga sbagliata: quando il prodotto non
    # torna, i due valori non sono affidabili e vengono scartati. Il prezzo
    # di riga resta corretto in ogni caso, quindi il totale non cambia.
    if unit_price is not None and abs(quantity * unit_price - total) > QTY_PRICE_TOLERANCE:
        quantity = 1.0
        unit_price = None

    # LO SCONTO E' GIA' SCORPORATO NEL TOTALE? NO.
    #
    # Verificato su scontrini reali: il campo total di Veryfi e' il prezzo AL
    # LORDO dello sconto. Un succo con total 2.09 e discount -0.30 e' stato
    # pagato 1.79.
    #
    # Il nostro schema vuole price = prezzo finale pagato e original_price =
    # prezzo prima dello sconto, quindi lo sconto va sottratto qui.
    discount_raw = line.get("discount")
    discount = abs(discount_raw) if discount_raw is not None and abs(discount_raw) >= 0.005 else None
    net = round(total - discount, 2) if discount is not None else round(total, 2)

    item: dict[str, Any] = {
        "id": f"{position:02d}",
        "raw_name": raw_name,
        "quantity": round(quantity, 3),
        "price": net,
        "category": classify(raw_name, line.get("type"), line.get("tax_rate")),
        "included": True,
    }

    name = readable_name(raw_name)
    if name:
        item["name"] = name

    if unit_price is not None:
        item["unit_price"] = round(unit_price, 2)

    if discount is not None:
        item["discount"] = round(discount, 2)
        item["original_price"] = round(total, 2)

    return item


# --------------------------------------------------------------------------- #
# Ingresso pubblico
# --------------------------------------------------------------------------- #


def transform(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Converte una risposta Veryfi nel payload dell'integrazione Spesa.

    Ritorna (payload, avvisi). Il payload NON e' ancora validato: lo valida
    validate_payload() di model.py, esattamente come farebbe con un invio da
    ChatGPT. Cosi' esiste una sola definizione di dato valido.

    Solleva VeryfiPayloadError se il JSON non ha la forma attesa.
    """
    if not isinstance(raw, dict):
        raise VeryfiPayloadError(["Il payload deve essere un oggetto JSON"])

    errors: list[str] = []
    warnings: list[str] = []

    store = _vendor_name(raw)
    if not store:
        errors.append("vendor.name: nome del negozio mancante nella risposta Veryfi")

    date_part, time_part = _split_datetime(raw)
    if not date_part:
        errors.append("date: data mancante o non interpretabile nella risposta Veryfi")

    total = _num(raw.get("total"))
    if total is None:
        total = _num(raw.get("subtotal"))
        if total is not None:
            warnings.append("total assente: usato subtotal")
    if total is None:
        errors.append("total: totale mancante nella risposta Veryfi")

    line_items = raw.get("line_items")
    if not isinstance(line_items, list) or not line_items:
        errors.append("line_items: elenco degli articoli mancante o vuoto")
        line_items = []

    if errors:
        raise VeryfiPayloadError(errors)

    prepared = _prepare_lines(line_items)
    if not prepared:
        raise VeryfiPayloadError(
            ["line_items: nessuna riga utilizzabile dopo la pulizia"]
        )

    dropped = len(line_items) - len(prepared)
    if dropped:
        warnings.append(
            f"{dropped} righe scartate o fuse: marcatori dello scontrino e "
            "righe a importo zero"
        )

    items = [_finalize_line(line, n) for n, line in enumerate(prepared, start=1)]

    # receipt_id tracciabile: data, negozio, ora e identificativo del documento
    # in Veryfi. Quest'ultimo rende l'id univoco anche fra due spese nello
    # stesso minuto, e permette di risalire al documento originale.
    veryfi_id = raw.get("id")
    suffix = f"v{veryfi_id}" if isinstance(veryfi_id, int) else "v0"
    receipt_id = (
        f"{date_part.replace('-', '')}"
        f"-{slugify_store(store)}"
        f"-{(time_part or '00:00').replace(':', '')}00"
        f"-{suffix}"
    )[:64]

    payload: dict[str, Any] = {
        "schema_version": 1,
        "receipt_id": receipt_id,
        "date": date_part,
        "store": store,
        "receipt_total": round(total, 2),
        "items": items,
    }
    if time_part:
        payload["time"] = time_part

    # Diagnostica: quanto aveva calcolato Veryfi rispetto a quanto calcolera'
    # Home Assistant. Non influenza nulla.
    computed = round(sum(i["price"] for i in items), 2)
    if abs(computed - payload["receipt_total"]) > 0.05:
        warnings.append(
            f"somma articoli {computed:.2f} contro totale {payload['receipt_total']:.2f}: "
            "lo scontrino verra' segnalato da verificare"
        )
    payload["items_total"] = computed

    return payload, warnings
