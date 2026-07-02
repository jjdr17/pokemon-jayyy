#!/usr/bin/env python3
"""
Monitor Pokémon TCG — preordini/restock set inglesi.
Gira su GitHub Actions ogni ora, manda notifiche push via ntfy.sh.
"""
import json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE_FILE = "state.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
}
TIMEOUT = 20

# ---------------- PRODOTTI DA CERCARE ----------------
# name: etichetta notifica | q: query di ricerca | match: il prodotto è "trovato"
# se ALMENO UNA delle varianti compare nella pagina (e la pagina parla di Pokémon)
PRODUCTS = [
    # Nuove uscite / preordini
    {"name": "Pitch Black (ME05)",      "q": "pitch black",       "match": ["pitch black"]},
    {"name": "30th Celebration",        "q": "30th celebration",  "match": ["30th celebration", "30th anniversary"]},
    {"name": "Storm Emerald (ME06)",    "q": "storm emerald",     "match": ["storm emerald"]},
    {"name": "Chaos Rising (ME04)",     "q": "chaos rising",      "match": ["chaos rising"]},
    {"name": "Phantasmal Flames (ME02)","q": "phantasmal flames", "match": ["phantasmal flames"]},
    # Set storici caldi — restock a buon prezzo
    {"name": "Pokémon 151",             "q": "pokemon 151",       "match": ["pokemon 151", "pokémon 151", "151 booster", "151 elite trainer", "151 ultra premium"]},
    {"name": "Prismatic Evolutions",    "q": "prismatic evolutions", "match": ["prismatic evolutions"]},
    {"name": "Crown Zenith",            "q": "crown zenith",      "match": ["crown zenith"]},
    {"name": "Destined Rivals",         "q": "destined rivals",   "match": ["destined rivals"]},
]

# La pagina deve riguardare Pokémon, altrimenti il prodotto viene ignorato
# (evita omonimi di altri giochi/TCG con nomi simili)
POKEMON_MARKERS = ["pokemon", "pokémon"]

# Parole che indicano disponibilità / preordine
POSITIVE = ["pre-order", "preorder", "pre order", "add to cart", "add to basket",
            "in stock", "buy now", "in den warenkorb", "vorbestellen", "disponibile",
            "aggiungi al carrello", "ajouter au panier", "précommande", "añadir al carrito",
            "do koszyka", "in winkelwagen", "læg i kurv", "lisää koriin", "købe"]
NEGATIVE = ["sold out", "out of stock", "esaurito", "ausverkauft", "épuisé", "agotado",
            "not available", "unavailable", "wyprzedane", "uitverkocht"]

# ---------------- NEGOZI ----------------
# (nome, dominio, template ricerca o None per autodetect, gruppo)
# gruppo: A=retailer grandi, B=confermati spedizione Italia, C=non verificati
SHOPS = [
    ("TCGplayer", "www.tcgplayer.com", "https://www.tcgplayer.com/search/pokemon/product?q={q}", "A"),
    ("Pokemon Center", "www.pokemoncenter.com", "https://www.pokemoncenter.com/search/{q}", "A"),
    ("Amazon.it", "www.amazon.it", "https://www.amazon.it/s?k=pokemon+tcg+{q}", "A"),
    ("GameStop US", "www.gamestop.com", "https://www.gamestop.com/search/?q=pokemon+{q}", "A"),
    ("Cardmarket", "www.cardmarket.com", None, "A"),
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
    ("Cardstore.cz", "www.cardstore.cz", None, "C"),
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


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
            s.setdefault("alerts", {})
            return s
    except Exception:
        return {"shops": {}, "search_url": {}, "alerts": {}, "first_run": True}


def save_state(state):
    state.pop("first_run", None)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)


def notify(title, message, url=None, priority="high"):
    if not NTFY_TOPIC:
        print(f"[NO TOPIC] {title}: {message}")
        return
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": "rotating_light,zap"}
    if url:
        headers["Click"] = url
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers=headers, timeout=15)
    except Exception as e:
        print(f"ntfy error: {e}")


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 500:
            return r.text.lower()
    except Exception:
        pass
    return None


def get_search_url(state, name, domain, template, q):
    """Trova (e memorizza) il pattern di ricerca che funziona per il negozio."""
    qq = q.replace(" ", "+")
    if template:
        return template.format(q=qq)
    cached = state["search_url"].get(domain)
    if cached:
        return cached.format(q=qq)
    for pat in SEARCH_PATTERNS:
        url = pat.format(d=domain, q=qq)
        html = fetch(url)
        if html and any(m in html for m in q.split()):
            state["search_url"][domain] = pat.format(d=domain, q="{q}")
            return url
    return None


def check_shop(state, shop):
    """Ritorna lista di (product_name, status, url) per un negozio."""
    name, domain, template, group = shop
    results = []
    for prod in PRODUCTS:
        url = get_search_url(state, name, domain, template, prod["q"])
        if not url:
            results.append((prod["name"], "irraggiungibile", None))
            continue
        html = fetch(url)
        if html is None:
            results.append((prod["name"], "irraggiungibile", url))
            continue
        # SOLO prodotti Pokémon: la pagina deve contenere un riferimento a Pokémon
        is_pokemon_page = any(mk in html for mk in POKEMON_MARKERS)
        variants_found = [v for v in prod["match"] if v in html]
        if not variants_found or not is_pokemon_page:
            status = "non listato"
        else:
            # le parole chiave contano solo se VICINE al nome del prodotto,
            # per evitare falsi positivi da altri articoli sulla stessa pagina
            pos = neg = False
            for key in variants_found:
                for m in re.finditer(re.escape(key), html):
                    window = html[max(0, m.start() - PROXIMITY): m.end() + PROXIMITY]
                    pos = pos or any(k in window for k in POSITIVE)
                    neg = neg or any(k in window for k in NEGATIVE)
            if pos and not neg:
                status = "disponibile"
            elif neg:
                status = "esaurito"
            else:
                status = "listato"
        results.append((prod["name"], status, url))
        time.sleep(0.3)
    return name, group, results


def main():
    state = load_state()
    first_run = state.get("first_run", False)

    if first_run:
        notify("Monitor Pokémon attivo ✅",
               "Il monitor cloud è partito. Controllerò i negozi ogni 5 minuti e ti avviserò qui per preordini e restock.",
               priority="default")

    alerts = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(check_shop, state, s): s for s in SHOPS}
        for fut in as_completed(futures):
            try:
                shop_name, group, results = fut.result()
            except Exception as e:
                print(f"errore {futures[fut][0]}: {e}")
                continue
            prev_shop = state["shops"].setdefault(shop_name, {})
            now = time.time()
            for prod_name, status, url in results:
                prev = prev_shop.get(prod_name, "sconosciuto")
                if status == "irraggiungibile" and prev not in ("sconosciuto", "irraggiungibile"):
                    # sito momentaneamente giù: non perdere lo stato precedente
                    print(f"{shop_name:22s} | {prod_name:26s} | {prev} (sito giù, stato conservato)")
                    continue
                prev_shop[prod_name] = status
                # Avvisa solo su transizioni interessanti (non al primo giro,
                # e non quando un sito torna semplicemente raggiungibile)
                interesting = status == "disponibile" and prev in ("esaurito", "non listato", "listato")
                newly_listed = status == "listato" and prev == "non listato"
                if not first_run and prev != "sconosciuto" and (interesting or newly_listed):
                    akey = f"{shop_name}|{prod_name}"
                    last = state["alerts"].get(akey, 0)
                    if now - last >= ALERT_COOLDOWN_H * 3600:
                        state["alerts"][akey] = now
                        tag = " ⚠️ spedizione Italia da verificare" if group == "C" else ""
                        verb = "DISPONIBILE/PREORDER" if interesting else "ora listato"
                        alerts.append((f"🚨 {prod_name} — {shop_name}",
                                       f"{prod_name} {verb} su {shop_name}{tag}\n{url or ''}", url))
                print(f"{shop_name:22s} | {prod_name:26s} | {prev} -> {status}")

    if len(alerts) > MAX_ALERTS_PER_RUN:
        extra = len(alerts) - MAX_ALERTS_PER_RUN
        alerts = alerts[:MAX_ALERTS_PER_RUN]
        alerts.append(("🚨 Altre novità Pokémon",
                       f"E altri {extra} aggiornamenti in questo giro — controlla i negozi.", None))
    for title, msg, url in alerts:
        notify(title, msg, url)
        time.sleep(0.5)

    save_state(state)
    print(f"\nFatto. Alert inviati: {len(alerts)}")


if __name__ == "__main__":
    main()
