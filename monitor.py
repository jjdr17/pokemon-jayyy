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
    {"name": "30th Celebration",        "q": "30th celebration",  "match": ["30th celebration", "30th anniversary", "30° anniversario", "30 anniversario"]},
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
            "do koszyka", "in winkelwagen", "læg i kurv", "lisää koriin", "købe",
            "preordina", "preordine", "preordini", "prenota", "acquista"]
NEGATIVE = ["sold out", "out of stock", "esaurito", "ausverkauft", "épuisé", "agotado",
            "not available", "unavailable", "wyprzedane", "uitverkocht", "non disponibile",
            "coda al completo"]

# ---------------- NEGOZI ----------------
# (nome, dominio, template ricerca o None per autodetect, gruppo)
# gruppo: A=retailer grandi, B=confermati spedizione Italia, C=non verificati
SHOPS = [
    ("TCGplayer", "www.tcgplayer.com", "https://www.tcgplayer.com/search/pokemon/product?q={q}", "A"),
    ("Pokemon Center", "www.pokemoncenter.com", "https://www.pokemoncenter.com/search/{q}", "A"),
    ("Amazon.it", "www.amazon.it", "https://www.amazon.it/s?k=pokemon+tcg+{q}", "A"),
    ("GameStop US", "www.gamestop.com", "https://www.gamestop.com/search/?q=pokemon+{q}", "A"),
    ("Cardmarket", "www.cardmarket.com", None, "A"),
    ("GameLife (IT)", "www.gamelife.it", "https://www.gamelife.it/ricerca?controller=search&s={q}", "A"),
    ("GameStop Italia", "www.gamestop.it", "https://www.gamestop.it/SearchResult/QuickSearch?q={q}", "A"),
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
DIGEST_HOURS = [10, 15, 18, 22]   # riepiloghi giornalieri, ora italiana (Europe/Rome)
DASHBOARD_URL = "https://jjdr17.github.io/pokemon-jayyy/"
PRICE_DROP_RATIO = 0.85           # avvisa se il prezzo scende di almeno il 15% nello stesso negozio
# prodotti "caldi": notifica con priorità urgente (suona anche in non disturbare, se configurato)
URGENT_PRODUCTS = {"Pitch Black (ME05)", "30th Celebration", "Storm Emerald (ME06)"}
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
            return s
    except Exception:
        return {"shops": {}, "search_url": {}, "alerts": {}, "prices": {}, "first_run": True}


def save_state(state):
    state.pop("first_run", None)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)


PRIO_MAP = {"urgent": 5, "high": 4, "default": 3}


def notify(title, message, url=None, priority="high"):
    if not NTFY_TOPIC:
        print(f"[NO TOPIC] {title}: {message}")
        return
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": PRIO_MAP.get(priority, 4), "tags": ["zap"]}
    if url:
        payload["click"] = url
    try:
        requests.post("https://ntfy.sh", json=payload, timeout=15)
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
    for prod in PRODUCTS:
        url = get_search_url(state, name, domain, template, prod["q"])
        if not url:
            results.append((prod["name"], "irraggiungibile", None, None))
            continue
        html = fetch(url)
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
            # per evitare falsi positivi da altri articoli sulla stessa pagina
            pos = neg = False
            pos_windows = []
            for key in variants_found:
                for m in re.finditer(re.escape(key), html):
                    window = html[max(0, m.start() - PROXIMITY): m.end() + PROXIMITY]
                    if any(k in window for k in POSITIVE):
                        pos = True
                        pos_windows.append(window)
                    neg = neg or any(k in window for k in NEGATIVE)
            if pos and not neg:
                status = "disponibile"
                price = extract_price(pos_windows)
            elif neg:
                status = "esaurito"
            else:
                status = "listato"
        results.append((prod["name"], status, url, price))
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
            for prod_name, status, url, price in results:
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
                        ptxt = f" a {price}" if price else ""
                        prio = "urgent" if prod_name in URGENT_PRODUCTS and interesting else "high"
                        alerts.append((f"🚨 {prod_name} — {shop_name}",
                                       f"{prod_name} {verb}{ptxt} su {shop_name}{tag}\n{url or ''}", url, prio))
                # ---- memoria prezzi + avviso calo prezzo ----
                pkey = f"{shop_name}|{prod_name}"
                if status == "disponibile" and url:
                    state.setdefault("urls", {})[pkey] = url
                if status == "disponibile" and price:
                    old = price_value(state["prices"].get(pkey))
                    new = price_value(price)
                    if (not first_run and old and new and new <= old * PRICE_DROP_RATIO):
                        dkey = f"drop|{pkey}"
                        if now - state["alerts"].get(dkey, 0) >= ALERT_COOLDOWN_H * 3600:
                            state["alerts"][dkey] = now
                            tag = " ⚠️ spedizione Italia da verificare" if group == "C" else ""
                            alerts.append((f"📉 Prezzo giù: {prod_name} — {shop_name}",
                                           f"{prod_name} sceso a {price} (era {state['prices'][pkey]}) su {shop_name}{tag}\n{url or ''}",
                                           url, "high"))
                    state["prices"][pkey] = price
                print(f"{shop_name:22s} | {prod_name:26s} | {prev} -> {status}" + (f" ({price})" if price else ""))

    if len(alerts) > MAX_ALERTS_PER_RUN:
        extra = len(alerts) - MAX_ALERTS_PER_RUN
        alerts = alerts[:MAX_ALERTS_PER_RUN]
        alerts.append(("🚨 Altre novità Pokémon",
                       f"E altri {extra} aggiornamenti in questo giro — controlla i negozi.", None, "high"))
    for title, msg, url, prio in alerts:
        notify(title, msg, url, priority=prio)
        time.sleep(0.5)

    # ---- riepiloghi giornalieri (10, 15, 18, 22 ora italiana) ----
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_it = datetime.now(ZoneInfo("Europe/Rome"))
    slot = f"{now_it.strftime('%Y-%m-%d')}-{now_it.hour}"
    if not first_run and now_it.hour in DIGEST_HOURS and state.get("digest_slot") != slot:
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
    if (not first_run and tot > 20 and down / tot > 0.7 and state.get("watchdog_date") != today_it):
        state["watchdog_date"] = today_it
        notify("⚠️ Monitor Pokémon: problema",
               f"{down}/{tot} controlli irraggiungibili — possibile blocco IP o problema di rete. "
               "Controlla i log su GitHub Actions.", priority="high")

    # ---- pulizia: elimina cooldown più vecchi di 7 giorni per tenere lo stato leggero ----
    cutoff = time.time() - 7 * 86400
    state["alerts"] = {k: v for k, v in state["alerts"].items() if v > cutoff}

    build_dashboard(state)
    save_state(state)
    print(f"\nFatto. Alert inviati: {len(alerts)}")


def build_dashboard(state):
    """Genera docs/index.html: dashboard con stato e prezzi, pubblicata su GitHub Pages."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_it = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y %H:%M")
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
                avail.append((price_value(p) or 9e9, shop_name, p, u))
        avail.sort()
        shops_html = " ".join(
            f'<a class="shop" href="{u}" target="_blank">{s}{(" · " + p) if p else ""}</a>'
            for _, s, p, u in avail[:12]) or '<span class="none">nessuno</span>'
        badge = f'<span class="ok">{counts["disponibile"]} disponibili</span>' if counts["disponibile"] else '<span class="ko">0 disponibili</span>'
        rows.append(f'<div class="card"><h2>{n} {badge}</h2>'
                    f'<div class="meta">{counts["esaurito"]} esauriti · {counts["listato"]} listati</div>'
                    f'<div class="shops">{shops_html}</div></div>')
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
footer{{color:#9aa3c7;font-size:.75rem;margin-top:14px}}</style></head><body>
<h1>⚡ Pokémon TCG Monitor</h1>
{''.join(rows)}
<footer>Aggiornato: {now_it} (ora italiana) · si aggiorna ogni 5 minuti · ⚠️ verifica sempre prezzo e spedizione sul negozio</footer>
</body></html>"""
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w") as f:
        f.write(html)


if __name__ == "__main__":
    main()
