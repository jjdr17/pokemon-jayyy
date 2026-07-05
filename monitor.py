#!/usr/bin/env python3
"""
Monitor Pokémon TCG — preordini/restock set inglesi.
Gira su GitHub Actions ogni ora, manda notifiche push via ntfy.sh.
"""
import json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

# MODE=fast: corsia preferenziale — solo negozi e set caldi, stato separato, nessuna dashboard
MODE = os.environ.get("MODE", "full")
if MODE == "fast":
    STATE_FILE = "state-fast.json"
    OTHER_STATE_FILE = "state.json"
else:
    STATE_FILE = "state.json"
    OTHER_STATE_FILE = "state-fast.json"

HOT_SHOP_NAMES = {"TCGplayer", "Pokemon Center", "Amazon.it", "GameStop US", "GameLife (IT)"}
HOT_PRODUCT_NAMES = {"Pitch Black (ME05)", "30th Celebration", "Storm Emerald (ME06)",
                     "Premium Deck Espeon & Umbreon (30th)"}

# Filtri per negozio: controlla SOLO questi prodotti (per ridurre notifiche inutili)
SHOP_PRODUCT_FILTER = {
    "Amazon.it": {"30th Celebration", "Storm Emerald (ME06)", "Premium Deck Espeon & Umbreon (30th)"},
}
import random
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Version/17.4 Safari/605.1.15 AppleWebKit/605.1.15 (KHTML, like Gecko)",
]
HEADERS = {
    "User-Agent": random.choice(USER_AGENTS),
    "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
}
TIMEOUT = 12
MAX_WORKERS = 16

# sessione condivisa: riusa le connessioni (molto più veloce) e ritenta 1 volta da sola
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
try:
    from requests.adapters import HTTPAdapter
    _ad = HTTPAdapter(pool_connections=MAX_WORKERS * 2, pool_maxsize=MAX_WORKERS * 2, max_retries=1)
    SESSION.mount("https://", _ad)
    SESSION.mount("http://", _ad)
except Exception:
    pass

# ---------------- PRODOTTI DA CERCARE ----------------
# name: etichetta notifica | q: query di ricerca | match: il prodotto è "trovato"
# se ALMENO UNA delle varianti compare nella pagina (e la pagina parla di Pokémon)
PRODUCTS = [
    # Nuove uscite / preordini
    {"name": "Pitch Black (ME05)",      "q": "pitch black",       "match": ["pitch black"]},
    {"name": "30th Celebration",        "q": "30th celebration",  "match": ["30th celebration", "30th anniversary", "30° anniversario", "30 anniversario"]},
    {"name": "Premium Deck Espeon & Umbreon (30th)", "q": "espeon umbreon premium deck",
     "match": ["espeon & umbreon", "espeon and umbreon", "espeon und umbreon", "espeon e umbreon"]},
    {"name": "Storm Emerald (ME06)",    "q": "storm emerald",     "match": ["storm emerald"]},
    {"name": "Chaos Rising (ME04)",     "q": "chaos rising",      "match": ["chaos rising"]},
    {"name": "Phantasmal Flames (ME02)","q": "phantasmal flames", "match": ["phantasmal flames"]},
    # Set storici caldi — restock a buon prezzo
    {"name": "Pokémon 151",             "q": "pokemon 151",       "match": ["pokemon 151", "pokémon 151", "151 booster", "151 elite trainer", "151 ultra premium"]},
    {"name": "Prismatic Evolutions",    "q": "prismatic evolutions", "match": ["prismatic evolutions"]},
    {"name": "Crown Zenith",            "q": "crown zenith",      "match": ["crown zenith"]},
    {"name": "Destined Rivals",         "q": "destined rivals",   "match": ["destined rivals"]},
]

# Prezzi di listino USA di riferimento (per giudicare le offerte in dashboard)
MSRP = {
    "Pitch Black (ME05)": "BB ~$162 · ETB ~$54",
    "30th Celebration": "ETB $49.99 — preordina appena possibile",
    "Premium Deck Espeon & Umbreon (30th)": "TBA — esce col 30°, 18 settembre",
    "Storm Emerald (ME06)": "TBA",
    "Chaos Rising (ME04)": "BB ~$162 · ETB ~$54 (mercato ~$230)",
    "Phantasmal Flames (ME02)": "BB $144 (mercato ~$450 ⚠️)",
    "Pokémon 151": "ETB ~$55 · UPC ~$120",
    "Prismatic Evolutions": "ETB ~$55 (mercato molto sopra)",
    "Crown Zenith": "ETB ~$50",
    "Destined Rivals": "BB ~$162 · ETB ~$54",
}

# La pagina deve riguardare Pokémon, altrimenti il prodotto viene ignorato
# (evita omonimi di altri giochi/TCG con nomi simili)
POKEMON_MARKERS = ["pokemon", "pokémon"]

# Parole che indicano disponibilità / preordine
POSITIVE = ["pre-order", "preorder", "pre order", "add to cart", "add to basket",
            "in stock", "buy now", "in den warenkorb", "vorbestellen", "disponibile",
            "aggiungi al carrello", "ajouter au panier", "précommande", "añadir al carrito",
            "do koszyka", "in winkelwagen", "læg i kurv", "lisää koriin", "købe",
            "preordina", "preordine", "preordini", "prenota", "acquista"]
NEGATIVE = ["sold out", "out of stock", "esaurito", "esaurita", "ausverkauft", "épuisé", "agotado",
            "not available", "unavailable", "wyprzedane", "uitverkocht", "non disponibile",
            "coda al completo", "avvisami", "notify me", "notify when", "email me when",
            "email when", "waitlist", "wait list", "back in stock", "restock alert",
            "niet leverbaar", "backorder", "sold-out"]

# Se vicino al nome del prodotto compaiono questi termini, è roba di un altro gioco: scarta
OTHER_TCG = ["magic", "mtg", "the gathering", "yu-gi-oh", "yugioh", "one piece card",
             "lorcana", "digimon", "flesh and blood", "dragon ball", "weiss schwarz",
             "union arena", "star wars unlimited", "altered tcg"]

# ---------------- NEGOZI ----------------
# (nome, dominio, template ricerca o None per autodetect, gruppo)
# gruppo: A=retailer grandi, B=confermati spedizione Italia, C=non verificati
SHOPS = [
    ("TCGplayer", "www.tcgplayer.com", "https://www.tcgplayer.com/search/pokemon/product?q={q}", "A"),
    ("Pokemon Center", "www.pokemoncenter.com", "https://www.pokemoncenter.com/search/{q}", "A"),
    ("Amazon.it", "www.amazon.it", "https://www.amazon.it/s?k=pokemon+tcg+{q}", "A"),
    ("GameStop US", "www.gamestop.com", "https://www.gamestop.com/search/?q=pokemon+{q}", "A"),
    ("Cardmarket", "www.cardmarket.com", "https://www.cardmarket.com/en/Pokemon/Products/Search?searchString={q}", "A"),
    # nota: gamestop.it reindirizza a gamelife.it (stesso gruppo) — un solo ingresso per evitare doppioni
    ("GameLife (IT)", "www.gamelife.it", "https://www.gamelife.it/ricerca?controller=search&s={q}", "A"),
    # --- Gruppo B: confermati, spediscono in Italia ---
    ("eFantasy", "www.efantasy.gr", None, "B"),
    ("EuroTCG", "eurotcg.com", None, "B"),
    ("Rogerz", "rogerz.dk", None, "B"),
    ("Poke-Power", "poke-power.eu", None, "B"),
    ("SunnyStore", "sunnystore.es", None, "B"),
    ("Kaissa Games", "kaissagames.com", None, "B"),
    ("PokeCardShop", "www.pokecardshop.be", None, "B"),
    ("Kerailykortti", "tradingcard.fi", None, "B"),
    ("LottiCards", "www.lotticards.de", None, "B"),
    ("Blackfire", "www.blackfire.eu", None, "B"),
    ("PokeBros", "pokebros.com.hr", None, "B"),
    ("CardZone", "cardzone.es", None, "B"),
    ("God of Cards", "godofcards.com", None, "B"),
    ("AllDayTCG", "alldaytcg.com", None, "B"),
    ("OutpostBrussels", "outpostbrussels.be", None, "B"),
    ("Pokeseller14", "pokeseller14.com", None, "B"),
    ("Chaos Cards (UK)", "www.chaoscards.co.uk", None, "B"),
    ("Total Cards (UK)", "totalcards.net", None, "B"),
    ("Packratt (UK)", "packratt.co.uk", None, "B"),
    ("Firestorm Games (UK)", "www.firestormgames.co.uk", None, "B"),
    ("Wayland Games (UK)", "www.waylandgames.co.uk", None, "B"),
    # --- Gruppo C: candidati non verificati (spedizione Italia da confermare) ---
    ("Card-Corner", "www.card-corner.de", None, "C"),
    ("CrispyCards", "crispycards.de", None, "C"),
    ("Sapphire-Cards", "sapphire-cards.de", None, "C"),
    ("TCG-Trade", "tcg-trade.de", None, "C"),
    ("Cardmex", "cardmex-shop.de", None, "C"),
    ("Geco-Shop", "geco-shop.de", None, "C"),
    ("Deckshop", "www.deckshop.de", None, "C"),
    ("Pokechest", "pokechest.at", None, "C"),
    ("MistiCards", "misticards.com", None, "C"),
    ("Fabscards", "fabscards.at", None, "C"),
    ("TCG-Shop.at", "en.tcg-shop.at", None, "C"),
    ("Butticards", "www.butticards.at", None, "C"),
    ("CardCorner.at", "cardcorner.at", None, "C"),
    ("Pokevend", "pokevend.at", None, "C"),
    ("Games-Island", "games-island.eu", None, "C"),
    ("RareCards.nl", "rarecards.nl", None, "C"),
    ("BESCARDS", "bescards.com", None, "C"),
    ("TCG Company", "tcgcompany.nl", None, "C"),
    ("OppaCards", "oppacards.com", None, "C"),
    ("TCGFanshop", "tcgfanshop.nl", None, "C"),
    ("TCGStore.se", "tcgstore.se", None, "C"),
    ("Hobbykort", "hobbykort.se", None, "C"),
    ("Poketalk", "www.poketalk.se", None, "C"),
    ("Pokemons.dk", "www.pokemons.dk", None, "C"),
    ("PokecTCG", "pokectcg.cz", None, "C"),
    ("WakuWaku", "www.wakuwaku.cz", None, "C"),
    ("Nerdom", "www.nerdom.gr", None, "C"),
    ("ExtremePokeCorner", "extremepokecorner.com", None, "C"),
    ("Pokipair", "www.pokipair.com", None, "C"),
    ("Pokemillon", "www.pokemillon.com", None, "C"),
    ("Pokebank", "pokebank.es", None, "C"),
    ("Pokewoke", "pokewoke.store", None, "C"),
    ("Vinticards", "vinticards.com", None, "C"),
    ("Pokebox", "pokeboxstore.pt", None, "C"),
    ("Papelinho", "papelinho.pt", None, "C"),
    ("LorenZone", "lorenzone.fr", None, "C"),
    ("MagicBazar", "www.magicbazar.fr", None, "C"),
    ("Fuji-Store", "fuji-store.fr", None, "C"),
    ("TCGDistribution", "tcgdistribution.fr", None, "C"),
    ("Pokekarty", "www.pokekarty.pl", None, "C"),
    ("Pikashop", "pikashop.pl", None, "C"),
    ("LetsGoTry", "letsgotry.pl", None, "C"),
    ("Pokecollect", "pokecollect.pl", None, "C"),
    ("Pokeka.hu", "pokeka.hu", None, "C"),
    ("Pokemonia.ro", "www.pokemonia.ro", None, "C"),
    ("Pokemania.ro", "pokemania.ro", None, "C"),
    ("TCGPikas", "tcgpikas.lt", None, "C"),
    ("PokemonWorld.lt", "pokemonworld.lt", None, "C"),
    ("Stoyanov Games", "stoyanov-gamesbg.com", None, "C"),
    ("iTCG.bg", "itcg.bg", None, "C"),
]

# In modalità fast si controllano solo negozi e prodotti caldi
if MODE == "fast":
    SHOPS = [s for s in SHOPS if s[0] in HOT_SHOP_NAMES]
    PRODUCTS = [p for p in PRODUCTS if p["name"] in HOT_PRODUCT_NAMES]

# Pattern di ricerca comuni (Shopify, WooCommerce, PrestaShop, ...)
SEARCH_PATTERNS = [
    "https://{d}/search?q={q}",
    "https://{d}/?s={q}&post_type=product",
    "https://{d}/search?controller=search&s={q}",
    "https://{d}/catalogsearch/result/?q={q}",
]


ALERT_COOLDOWN_H = 6      # max 1 notifica per prodotto/negozio ogni N ore
MAX_ALERTS_PER_RUN = 8    # tetto anti-spam per singolo run
PROXIMITY = 400           # le parole chiave valgono solo entro N caratteri dal nome prodotto
DIGEST_HOURS = [10, 15, 18, 22]   # riepiloghi giornalieri, ora italiana (Europe/Rome)
DASHBOARD_URL = "https://jjdr17.github.io/pokemon-jayyy/"
PRICE_DROP_RATIO = 0.85           # avvisa se il prezzo scende di almeno il 15% nello stesso negozio
# prodotti "caldi": notifica con priorità urgente (suona anche in non disturbare, se configurato)
URGENT_PRODUCTS = {"Pitch Black (ME05)", "30th Celebration", "Storm Emerald (ME06)",
                   "Premium Deck Espeon & Umbreon (30th)"}
PRICE_RE = re.compile(r'(?:€|\$|£)\s?\d{1,4}(?:[.,]\d{1,2})?')


def price_value(p):
    """'€54,99' -> 54.99 (solo per confronti nello stesso negozio/valuta)."""
    try:
        return float(p.lstrip("€$£ ").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
            s.setdefault("alerts", {})
            s.setdefault("prices", {})
            s.setdefault("pending", {})
            return s
    except Exception:
        return {"shops": {}, "search_url": {}, "alerts": {}, "prices": {}, "pending": {}, "first_run": True}


def save_state(state):
    state.pop("first_run", None)
    # scrittura atomica: se il run viene interrotto a metà, lo stato non si corrompe
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


PRIO_MAP = {"urgent": 5, "high": 4, "default": 3}


def notify(title, message, url=None, priority="high"):
    if not NTFY_TOPIC:
        print(f"[NO TOPIC] {title}: {message}")
        return
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": PRIO_MAP.get(priority, 4), "tags": ["zap"]}
    if url:
        payload["click"] = url
    # la notifica È lo scopo del sistema: 3 tentativi prima di arrendersi
    for attempt in range(3):
        try:
            r = requests.post("https://ntfy.sh", json=payload, timeout=15)
            if r.status_code < 500:
                return
        except Exception as e:
            print(f"ntfy tentativo {attempt + 1} fallito: {e}")
        time.sleep(2 * (attempt + 1))
    print(f"ntfy: notifica PERSA dopo 3 tentativi: {title}")


def fetch(url, raw=False):
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 500:
            return r.text if raw else r.text.lower()
    except Exception:
        pass
    return None


HREF_RE = re.compile(r'href="([^"#]+)"', re.IGNORECASE)


def extract_product_link(raw, low, pos, domain, query=""):
    """Cerca il link del prodotto più vicino alla posizione del match nella pagina.
    Il link deve contenere almeno una parola del nome del set (evita link di altri prodotti)."""
    if raw is None or len(raw) != len(low):
        return None
    tokens = [t for t in query.lower().split() if len(t) > 3]
    start = max(0, pos - 1500)
    window = raw[start:pos + 300]
    best = None
    for m in HREF_RE.finditer(window):
        u = m.group(1)
        if any(x in u.lower() for x in ("cart", "login", "account", "javascript:", "mailto:", ".css", ".js", ".png", ".jpg", ".svg", ".ico")):
            continue
        if tokens and not any(t in u.lower() for t in tokens):
            continue  # il link non parla del nostro prodotto: scarta
        best = u  # l'ultimo href valido prima del match è di solito il link del prodotto
    if not best:
        return None
    if best.startswith("//"):
        return "https:" + best
    if best.startswith("/"):
        return f"https://{domain}{best}"
    if best.startswith("http"):
        return best
    return None


def get_search_url(state, name, domain, template, q):
    """Trova (e memorizza) il pattern di ricerca che funziona per il negozio.
    Se il negozio risponde ma nessun pattern funziona, lo marca '-' (incompatibile)
    per non sprecare richieste a ogni run."""
    qq = q.replace(" ", "+")
    if template:
        return template.format(q=qq)
    cached = state["search_url"].get(domain)
    if cached == "-":
        return None
    if cached:
        return cached.format(q=qq)
    responded = False
    for pat in SEARCH_PATTERNS:
        url = pat.format(d=domain, q=qq)
        html = fetch(url)
        if html:
            responded = True
            if any(m in html for m in q.split()):
                state["search_url"][domain] = pat.format(d=domain, q="{q}")
                return url
    if responded:
        state["search_url"][domain] = "-"   # sito ok ma ricerca incompatibile: skip in futuro
    return None


def extract_price(windows):
    """Prende il prezzo più basso trovato vicino al nome del prodotto."""
    prices = []
    for w in windows:
        for m in PRICE_RE.findall(w):
            try:
                val = float(m.lstrip("€$£ ").replace(",", "."))
                if 3 <= val <= 2000:
                    prices.append((val, m.strip()))
            except ValueError:
                pass
    return min(prices)[1] if prices else None


def check_shop(state, shop):
    """Ritorna lista di (product_name, status, url) per un negozio."""
    name, domain, template, group = shop
    results = []
    allowed = SHOP_PRODUCT_FILTER.get(name)
    for prod in PRODUCTS:
        if allowed is not None and prod["name"] not in allowed:
            continue
        url = get_search_url(state, name, domain, template, prod["q"])
        if not url:
            results.append((prod["name"], "irraggiungibile", None, None))
            continue
        raw = fetch(url, raw=True)
        html = raw.lower() if raw else None
        if html is None:
            results.append((prod["name"], "irraggiungibile", url, None))
            continue
        # SOLO prodotti Pokémon: la pagina deve contenere un riferimento a Pokémon
        is_pokemon_page = any(mk in html for mk in POKEMON_MARKERS)
        variants_found = [v for v in prod["match"] if v in html]
        price = None
        if not variants_found or not is_pokemon_page:
            status = "non listato"
        else:
            # le parole chiave contano solo se VICINE al nome del prodotto,
            # e SOLO se il contesto è Pokémon (non Magic/altri giochi)
            pos = neg = False
            pos_windows = []
            first_pos = None
            found_valid = False
            for key in variants_found:
                for m in re.finditer(re.escape(key), html):
                    window = html[max(0, m.start() - PROXIMITY): m.end() + PROXIMITY]
                    if not any(mk in window for mk in POKEMON_MARKERS):
                        continue  # il nome compare ma senza contesto Pokémon vicino
                    if any(t in window for t in OTHER_TCG):
                        continue  # vicino c'è un altro gioco (Magic ecc.): scarta
                    found_valid = True
                    if any(k in window for k in POSITIVE):
                        pos = True
                        pos_windows.append(window)
                        if first_pos is None:
                            first_pos = m.start()
                    neg = neg or any(k in window for k in NEGATIVE)
            if not found_valid:
                status = "non listato"
            elif pos and not neg:
                status = "disponibile"
                price = extract_price(pos_windows)
                link = extract_product_link(raw, html, first_pos, domain, prod["q"])
                if link:
                    url = link  # link diretto al prodotto invece della pagina di ricerca
            elif neg:
                status = "esaurito"
            else:
                status = "listato"
        results.append((prod["name"], status, url, price))
        time.sleep(0.1)
    return name, group, results


def load_other_state():
    """Stato dell'altra corsia: cooldown condiviso + conoscenza già acquisita (evita doppioni)."""
    try:
        with open(OTHER_STATE_FILE) as f:
            s = json.load(f)
            return s.get("alerts", {}), s.get("shops", {})
    except Exception:
        return {}, {}


def main():
    if not NTFY_TOPIC:
        # senza topic il monitor sarebbe un guscio muto: meglio fallire rumorosamente
        print("::error::NTFY_TOPIC mancante — controlla il secret su GitHub")
        sys.exit(1)
    state = load_state()
    first_run = state.get("first_run", False)
    other_alerts, other_shops = load_other_state()

    if first_run and MODE == "full":
        notify("Monitor Pokémon attivo ✅",
               "Il monitor cloud è partito. Controllerò i negozi ogni 5 minuti e ti avviserò qui per preordini e restock.",
               priority="default")

    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    sent = {"n": 0}

    def send_alert(title, msg, url, prio):
        """Invio IMMEDIATO: la notifica parte appena rilevata, non a fine scansione."""
        sent["n"] += 1
        if sent["n"] == MAX_ALERTS_PER_RUN + 1:
            notify("🚨 Altre novità Pokémon", "Troppi aggiornamenti in questo giro — guarda la dashboard.",
                   DASHBOARD_URL, "high")
            return
        if sent["n"] > MAX_ALERTS_PER_RUN:
            return
        ts = _dt.now(_ZI("Europe/Rome")).strftime("%d/%m %H:%M")
        log = state.setdefault("log", [])
        log.append({"ts": ts, "title": title, "url": url or ""})
        state["log"] = log[-30:]
        notify(title, msg, url, priority=prio)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(check_shop, state, s): s for s in SHOPS}
        for fut in as_completed(futures):
            try:
                shop_name, group, results = fut.result()
            except Exception as e:
                print(f"errore {futures[fut][0]}: {e}")
                continue
            prev_shop = state["shops"].setdefault(shop_name, {})
            now = time.time()
            for prod_name, status, url, price in results:
                prev = prev_shop.get(prod_name, "sconosciuto")
                if status == "irraggiungibile" and prev not in ("sconosciuto", "irraggiungibile"):
                    # sito momentaneamente giù: non perdere lo stato precedente
                    print(f"{shop_name:22s} | {prod_name:26s} | {prev} (sito giù, stato conservato)")
                    continue
                # ---- anti-sfarfallio: un cambio di stato vale solo se CONFERMATO
                # da 2 letture consecutive (le pagine dei negozi variano tra una
                # richiesta e l'altra e creano finti restock) ----
                dkey_p = f"{shop_name}|{prod_name}"
                if prev != "sconosciuto" and not first_run and status != prev:
                    pend = state["pending"].get(dkey_p)
                    if pend and pend[0] == status:
                        pend[1] += 1
                    else:
                        pend = [status, 1]
                    state["pending"][dkey_p] = pend
                    if pend[1] < 2:
                        print(f"{shop_name:22s} | {prod_name:26s} | {prev} -> {status}? (da confermare)")
                        continue
                state["pending"].pop(dkey_p, None)
                prev_shop[prod_name] = status
                # Avvisa SOLO quando un prodotto diventa DISPONIBILE (non al primo giro,
                # non per semplici comparse a listino, non quando un sito torna raggiungibile)
                interesting = status == "disponibile" and prev in ("esaurito", "non listato", "listato")
                # anti-doppione: se l'altra corsia sa già che è disponibile, ha già avvisato lei
                if interesting and other_shops.get(shop_name, {}).get(prod_name) == "disponibile":
                    interesting = False
                if not first_run and prev != "sconosciuto" and interesting:
                    akey = f"{shop_name}|{prod_name}"
                    last = max(state["alerts"].get(akey, 0), other_alerts.get(akey, 0))
                    if now - last >= ALERT_COOLDOWN_H * 3600:
                        state["alerts"][akey] = now
                        tag = " ⚠️ spedizione Italia da verificare" if group == "C" else ""
                        ptxt = f" a {price}" if price else " (prezzo non rilevato, verifica sul link)"
                        prio = "urgent" if prod_name in URGENT_PRODUCTS else "high"
                        kind = "RESTOCK" if prev == "esaurito" else "DISPONIBILE/PREORDER"
                        send_alert(f"🚨 {kind}: {prod_name} — {shop_name}",
                                   f"{prod_name} {kind.lower()}{ptxt} su {shop_name}{tag}\n{url or ''}", url, prio)
                # ---- memoria prezzi + avviso calo prezzo ----
                pkey = f"{shop_name}|{prod_name}"
                if status == "disponibile" and url:
                    state.setdefault("urls", {})[pkey] = url
                if status == "disponibile" and price:
                    old = price_value(state["prices"].get(pkey))
                    new = price_value(price)
                    # avvisi di calo prezzo solo dai negozi affidabili (A/B): i non verificati fanno rumore
                    if (not first_run and old and new and new <= old * PRICE_DROP_RATIO and group != "C"):
                        dkey = f"drop|{pkey}"
                        last_d = max(state["alerts"].get(dkey, 0), other_alerts.get(dkey, 0))
                        if now - last_d >= ALERT_COOLDOWN_H * 3600:
                            state["alerts"][dkey] = now
                            tag = " ⚠️ spedizione Italia da verificare" if group == "C" else ""
                            send_alert(f"📉 Prezzo giù: {prod_name} — {shop_name}",
                                       f"{prod_name} sceso a {price} (era {state['prices'][pkey]}) su {shop_name}{tag}\n{url or ''}",
                                       url, "high")
                    state["prices"][pkey] = price
                print(f"{shop_name:22s} | {prod_name:26s} | {prev} -> {status}" + (f" ({price})" if price else ""))

    # ---- riepiloghi giornalieri (10, 15, 18, 22 ora italiana) — solo scansione completa ----
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_it = datetime.now(ZoneInfo("Europe/Rome"))
    slot = f"{now_it.strftime('%Y-%m-%d')}-{now_it.hour}"
    if MODE == "full" and not first_run and now_it.hour in DIGEST_HOURS and state.get("digest_slot") != slot:
        state["digest_slot"] = slot
        lines = []
        for prod in PRODUCTS:
            n = prod["name"]
            disp = [s for s, prods in state["shops"].items() if prods.get(n) == "disponibile"]
            if disp:
                # prezzo più basso registrato tra i negozi disponibili
                best = None
                for s_name in disp:
                    p = state["prices"].get(f"{s_name}|{n}")
                    v = price_value(p)
                    if v and (best is None or v < best[0]):
                        best = (v, p, s_name)
                ptxt = f", da {best[1]} ({best[2]})" if best else ""
                lines.append(f"✅ {n}: {len(disp)} negozi{ptxt}")
            else:
                lines.append(f"— {n}: nessuna disponibilità")
        notify(f"📋 Riepilogo Pokémon — ore {now_it.hour}", "\n".join(lines),
               url=DASHBOARD_URL, priority="default")

    # ---- watchdog: se quasi tutti i negozi risultano irraggiungibili, avvisa (1 volta al giorno) ----
    today_it = now_it.strftime("%Y-%m-%d")
    tot = sum(1 for prods in state["shops"].values() for v in prods.values())
    down = sum(1 for prods in state["shops"].values() for v in prods.values() if v == "irraggiungibile")
    if (MODE == "full" and not first_run and tot > 20 and down / tot > 0.7 and state.get("watchdog_date") != today_it):
        state["watchdog_date"] = today_it
        notify("⚠️ Monitor Pokémon: problema",
               f"{down}/{tot} controlli irraggiungibili — possibile blocco IP o problema di rete. "
               "Controlla i log su GitHub Actions.", priority="high")

    # ---- pulizia: elimina cooldown più vecchi di 7 giorni per tenere lo stato leggero ----
    cutoff = time.time() - 7 * 86400
    state["alerts"] = {k: v for k, v in state["alerts"].items() if v > cutoff}

    # ---- pulizia: rimuovi dallo stato i negozi non più monitorati ----
    valid = {s[0] for s in SHOPS}
    state["shops"] = {k: v for k, v in state["shops"].items() if k in valid}
    # e i prodotti esclusi dai filtri per negozio
    for shop_name, allowed in SHOP_PRODUCT_FILTER.items():
        if shop_name in state["shops"]:
            state["shops"][shop_name] = {k: v for k, v in state["shops"][shop_name].items() if k in allowed}
    # e le voci pending/alerts di negozi rimossi
    state["pending"] = {k: v for k, v in state.get("pending", {}).items() if k.split("|")[0] in valid}
    _shop_of = lambda k: k.split("|")[1] if k.startswith("drop|") else k.split("|")[0]
    state["alerts"] = {k: v for k, v in state["alerts"].items() if _shop_of(k) in valid}
    for key in ("prices", "urls"):
        state[key] = {k: v for k, v in state.get(key, {}).items() if k.split("|")[0] in valid}

    if MODE == "full":
        build_dashboard(state)
    save_state(state)
    print(f"\nFatto ({MODE}). Alert inviati: {sent['n']}")


def build_dashboard(state):
    """Genera docs/index.html: dashboard con stato e prezzi, pubblicata su GitHub Pages."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_it = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y %H:%M")
    group_of = {s[0]: s[3] for s in SHOPS}
    rows = []
    for prod in PRODUCTS:
        n = prod["name"]
        avail = []
        counts = {"disponibile": 0, "esaurito": 0, "listato": 0}
        for shop_name, prods in state["shops"].items():
            st = prods.get(n)
            if st in counts:
                counts[st] += 1
            if st == "disponibile":
                p = state["prices"].get(f"{shop_name}|{n}", "")
                u = state.get("urls", {}).get(f"{shop_name}|{n}", "#")
                warn = " ⚠️" if group_of.get(shop_name) == "C" else ""
                avail.append((price_value(p) or 9e9, shop_name + warn, p, u))
        avail.sort()
        shops_html = " ".join(
            f'<a class="shop" href="{u}" target="_blank">{s}{(" · " + p) if p else ""}</a>'
            for _, s, p, u in avail[:12]) or '<span class="none">nessuno</span>'
        badge = f'<span class="ok">{counts["disponibile"]} disponibili</span>' if counts["disponibile"] else '<span class="ko">0 disponibili</span>'
        msrp = MSRP.get(n, "")
        msrp_html = f' · <b>MSRP:</b> {msrp}' if msrp else ''
        rows.append(f'<div class="card"><h2>{n} {badge}</h2>'
                    f'<div class="meta">{counts["esaurito"]} esauriti · {counts["listato"]} listati{msrp_html}</div>'
                    f'<div class="shops">{shops_html}</div></div>')
    log_entries = state.get("log", [])[::-1]
    if log_entries:
        items = "".join(
            f'<div class="logrow"><span class="ts">{e["ts"]}</span> '
            + (f'<a href="{e["url"]}" target="_blank">{e["title"]}</a>' if e["url"] else e["title"])
            + '</div>' for e in log_entries[:15])
        log_html = f'<div class="card"><h2>🔔 Ultimi avvisi</h2>{items}</div>'
    else:
        log_html = '<div class="card"><h2>🔔 Ultimi avvisi</h2><span class="none">nessun avviso finora</span></div>'
    html = f"""<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Pokémon TCG Monitor</title><style>
body{{font-family:-apple-system,sans-serif;background:#0f1220;color:#eef1ff;margin:0;padding:16px}}
h1{{font-size:1.2rem}} h2{{font-size:1rem;margin:0 0 4px}}
.card{{background:#1a1f35;border-radius:12px;padding:14px;margin-bottom:10px}}
.ok{{background:#14532d;color:#4ade80;border-radius:99px;padding:2px 10px;font-size:.75rem}}
.ko{{background:#3f1d1d;color:#f87171;border-radius:99px;padding:2px 10px;font-size:.75rem}}
.meta{{color:#9aa3c7;font-size:.78rem;margin-bottom:8px}}
.shop{{display:inline-block;background:#222846;color:#7dd3fc;text-decoration:none;border-radius:99px;padding:4px 10px;font-size:.8rem;margin:2px}}
.none{{color:#9aa3c7;font-style:italic;font-size:.85rem}}
.logrow{{font-size:.82rem;padding:4px 0;border-bottom:1px solid #262d4f}}
.logrow a{{color:#7dd3fc;text-decoration:none}}
.ts{{color:#9aa3c7;font-size:.72rem;margin-right:6px}}
footer{{color:#9aa3c7;font-size:.75rem;margin-top:14px}}</style></head><body>
<h1>⚡ Pokémon TCG Monitor</h1>
{''.join(rows)}
{log_html}
<footer>Aggiornato: {now_it} (ora italiana) · si aggiorna ogni 5 minuti · ⚠️ verifica sempre prezzo e spedizione sul negozio</footer>
</body></html>"""
    # GitHub Pages accetta ~10 pubblicazioni/ora: riscrivi la dashboard solo se
    # i DATI sono cambiati, o comunque non più spesso di ogni 15 minuti
    try:
        with open("docs/index.html") as f:
            old = f.read()
        strip_ts = lambda h: re.sub(r"Aggiornato: [^<]+", "", h)
        if strip_ts(old) == strip_ts(html):
            mts = re.search(r"Aggiornato: (\d{2}/\d{2}/\d{4} \d{2}:\d{2})", old)
            if mts:
                old_dt = datetime.strptime(mts.group(1), "%d/%m/%Y %H:%M").replace(tzinfo=ZoneInfo("Europe/Rome"))
                if (datetime.now(ZoneInfo("Europe/Rome")) - old_dt).total_seconds() < 900:
                    return  # nessun cambiamento e aggiornata da poco: non toccarla
    except Exception:
        pass
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w") as f:
        f.write(html)


if __name__ == "__main__":
    main()
