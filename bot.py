#!/usr/bin/env python3
"""
Bot Telegram d'alertes LEGO.

Lit les flux RSS de hotukdeals (UK) et Dealabs (FR), repère les numéros de sets,
et n'envoie une alerte que si les 3 règles sont validées :
  1. le set part à la retraite dans les N prochains jours (watchlist.csv)
  2. la remise est d'au moins X % par rapport au prix public
  3. le set est dans ta watchlist (donc déjà filtré selon tes critères)

Aucune dépendance externe : Python 3.9+ suffit.

Variables d'environnement :
  TELEGRAM_TOKEN    clé du bot (donnée par @BotFather)
  TELEGRAM_CHAT_ID  ton identifiant de chat
  MIN_DISCOUNT      remise minimum en %, défaut 30
  RETIRE_WINDOW     fenêtre de retraite en jours, défaut 183 (6 mois)
  FEEDS             liste de flux séparés par des virgules (optionnel)

Usage :
  python bot.py              # un passage (c'est ce que lance GitHub Actions)
  python bot.py --dry-run    # affiche les alertes sans rien envoyer
  python bot.py --test       # envoie un message de test sur Telegram
  python bot.py --chat-id    # affiche ton chat_id (envoie d'abord un message au bot)
"""

import csv
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
WATCHLIST = HERE / "watchlist.csv"
SEEN_FILE = HERE / "seen.json"
STOCK_FILE = HERE / "stock.json"     # tes achats : cache privé GitHub + sauvegarde dans Telegram
FLAGS = {"GB": "🇬🇧", "FR": "🇫🇷", "DE": "🇩🇪", "ES": "🇪🇸", "IT": "🇮🇹", "NL": "🇳🇱", "AT": "🇦🇹"}


def country_of(source):
    s = source.lower()
    return "GB" if "hotukdeals" in s else "FR" if "dealabs" in s else "DE" if "mydealz" in s else None

DEFAULT_FEEDS = [
    # Flux réservés aux LEGO : leurs 30 annonces = les 30 derniers deals LEGO (plusieurs jours),
    # alors que les flux généraux ne gardent que les 30 derniers deals tous produits confondus.
    "https://www.hotukdeals.com/rss/tag/lego",
    "https://www.dealabs.com/rss/groupe/lego",
    "https://www.mydealz.de/rss/gruppe/lego",
]

MIN_DISCOUNT = float(os.getenv("MIN_DISCOUNT", "30"))
# Plancher global : aucun set n'alerte en dessous, même si son seuil est plus bas
MIN_DISCOUNT_FLOOR = float(os.getenv("MIN_DISCOUNT_FLOOR", "0"))
RETIRE_WINDOW = int(os.getenv("RETIRE_WINDOW", "183"))
# On garde aussi les sets retirés depuis peu : les fins de stock bradées sont souvent les meilleurs deals.
RETIRED_GRACE = 90

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


# ---------- Watchlist ----------

def load_watchlist():
    sets = {}
    with open(WATCHLIST, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            num = row["set"].strip()
            if not num or num.startswith("#"):
                continue
            sets[num] = {
                "name": row["nom"].strip(),
                "theme": (row.get("theme") or "").strip(),
                "gbp": float(row["prix_public_gbp"]) if row["prix_public_gbp"].strip() else None,
                "eur": float(row["prix_public_eur"]) if row["prix_public_eur"].strip() else None,
                "retire": datetime.strptime(row["retraite"].strip(), "%Y-%m-%d").date(),
                # Seuil propre au set (30 % thèmes A, 40 % thèmes B), sinon MIN_DISCOUNT
                "min_discount": float(row["seuil"]) if (row.get("seuil") or "").strip() else MIN_DISCOUNT,
            }
    return sets


# ---------- Lecture des flux ----------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def parse_feed(xml_bytes):
    """Renvoie une liste de dicts {id, title, link, price_attr}."""
    items = []
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        # Les sites du réseau Pepper (hotukdeals, Dealabs) mettent souvent le prix
        # dans une balise <pepper:merchant price="..."/>. On la cherche sans dépendre du namespace.
        price_attr, merchant = None, None
        for el in item.iter():
            if el.tag.endswith("merchant"):
                price_attr = price_attr or el.get("price")
                merchant = merchant or el.get("name")
        if not merchant:  # "... @ Amazon" (hotukdeals) ou "... chez Fnac" (Dealabs)
            m = re.search(r"(?:@|\bchez)\s+([A-Za-z0-9&'. -]{2,30})\s*$", title)
            merchant = m.group(1).strip() if m else None
        items.append({"id": guid, "title": title, "link": link, "price_attr": price_attr,
                      "merchant": merchant})
    return items


# ---------- Extraction prix / numéro ----------

NUM = r"(?:\d{1,3}(?:[ ,.]\d{3})+(?:[.,]\d{1,2})?|\d{1,4}(?:[.,]\d{1,2})?)"
PRICE_RE = re.compile(
    rf"(£|€)\s?({NUM})(?![\d])"                      # £48.99  €48,99  £1,299.99
    rf"|(?<![\d.,])({NUM})\s?(€|£)(?!\s?\d)"         # 48,99€  1 299,99 €
)


def to_float(s):
    s = s.replace(" ", "")
    # Le dernier séparateur suivi de 1-2 chiffres = décimales ; les autres = milliers.
    m = re.match(r"^(.*?)[.,](\d{1,2})$", s)
    if m:
        return float(re.sub(r"[.,]", "", m.group(1)) + "." + m.group(2))
    return float(re.sub(r"[.,]", "", s))


def extract_price(text, price_attr=None):
    """Renvoie (prix, devise) ou (None, None). Prend le premier prix trouvé."""
    candidates = []
    if price_attr:
        candidates.append(price_attr)
    candidates.append(text)
    for c in candidates:
        m = PRICE_RE.search(c)
        if m:
            if m.group(1):
                return to_float(m.group(2)), ("GBP" if m.group(1) == "£" else "EUR")
            return to_float(m.group(3)), ("GBP" if m.group(4) == "£" else "EUR")
    return None, None


def extract_sets(title, watchlist):
    nums = re.findall(r"(?<!\d)(\d{4,6})(?!\d)", title)
    return [n for n in dict.fromkeys(nums) if n in watchlist]


# ---------- Règles ----------

def evaluate(item, watchlist, today=None):
    """Renvoie une alerte (dict) si l'annonce passe les 3 règles, sinon None."""
    today = today or date.today()
    if "lego" not in item["title"].lower():
        return None
    for num in extract_sets(item["title"], watchlist):
        s = watchlist[num]
        days = (s["retire"] - today).days
        if days > RETIRE_WINDOW or days < -RETIRED_GRACE:
            continue
        price, cur = extract_price(item["title"], item["price_attr"])
        if price is None:
            continue
        rrp = s["gbp"] if cur == "GBP" else s["eur"]
        if not rrp:
            continue
        discount = (1 - price / rrp) * 100
        if discount < max(s["min_discount"], MIN_DISCOUNT_FLOOR):
            continue
        return {
            "set": num, "name": s["name"], "theme": s["theme"], "price": price, "cur": cur, "rrp": rrp,
            "discount": discount, "days": days, "retire": s["retire"],
            "title": item["title"], "link": item["link"], "merchant": item.get("merchant"),
            "country": item.get("country"),
            "seuil": s["min_discount"],
        }
    return None


# ---------- Indicateur de qualité ----------
# Hypothèses prudentes (voir README) :
#   - valeur après retraite : +15 % du prix public pour un set de classe A (seuil 30 %),
#     +5 % pour la classe B (seuil 40 %) ; la moyenne du marché tourne autour de +6 %/an
#   - 15 % de frais à la revente (plateforme + envoi)
REVALO = {"A": 1.15, "B": 1.05}
FRAIS = 0.15


def quality(a):
    """Renvoie (pastille, libellé, score, plus-value brute, marge brute %, bénéfice net)."""
    classe = "A" if a.get("seuil", 30) <= 30 else "B"
    resale = a["rrp"] * REVALO[classe]
    profit = resale - a["price"]                       # brut : revente - prix d'achat
    margin = profit / a["price"] * 100
    net = resale * (1 - FRAIS) - a["price"]            # après commission + envoi
    # Score sur 100 : la remise compte le plus, puis l'urgence (retraite proche), puis la classe
    score = min(a["discount"], 60) * 1.2                       # jusqu'à 72 pts
    score += 18 if a["days"] <= 60 else 10 if a["days"] <= 120 else 0
    score += 10 if classe == "A" else 0
    score = round(min(score, 100))
    if score >= 75:
        return "🟢", "Excellente affaire", score, profit, margin, net
    if score >= 62:
        return "🟡", "Bonne affaire", score, profit, margin, net
    return "🟠", "Affaire correcte", score, profit, margin, net


def esc(t):
    return html.escape(str(t), quote=False)


def format_alert(a):
    """Renvoie (texte HTML, lien du deal)."""
    sym = "£" if a["cur"] == "GBP" else "€"
    dot, label, score, profit, margin, net = quality(a)
    if a["days"] >= 0:
        retire_txt = f"retraite le {a['retire']:%d/%m/%Y} (dans {a['days']} j)"
    else:
        retire_txt = f"retiré depuis {-a['days']} j"
    fire = "🔥 " if a["discount"] >= 40 else ""
    lines = [
        f"{dot} <b>{label}</b> · {score}/100",
        f"{fire}<b>-{a['discount']:.0f} %</b> · {esc(a['name'])} ({a['set']})",
        f"💰 <b>{sym}{a['price']:.2f}</b> au lieu de {sym}{a['rrp']:.2f}",
        f"📈 Plus-value brute estimée : <b>~{sym}{profit:.0f}</b> par boîte ({margin:+.0f} %)",
        f"   ↳ net après commission et envoi : ~{sym}{net:.0f}",
        f"⏳ {retire_txt}",
    ]
    if re.search(r"cagnott|fid[ée]lit|cashback|clubcard|nectar|bon d'achat", a["title"], re.I):
        lines.append("💳 Remise versée en cagnotte / carte fidélité : pas une baisse de prix en cash")
    if os.getenv("BRICKFACT_MCP", "1") == "1":
        mv = market_value(a["set"])
        if mv:
            if a["cur"] == "EUR":
                lines.append(f"📊 Valeur marché actuelle (scellé, Brickfact) : <b>€{mv:.0f}</b>"
                             f" → revente immédiate {mv - a['price']:+.0f} € brut")
            else:
                lines.append(f"📊 Valeur marché actuelle (scellé, Brickfact) : <b>€{mv:.0f}</b>")
    meta = " · ".join(x for x in [
        ("🏪 " + (esc(a["merchant"]) if a.get("merchant") else "Magasin non précisé")
         + (f" {FLAGS.get(a['country'], a['country'])}" if a.get("country") else "")),
        f"⭐ {esc(a['theme'])}" if a.get("theme") else "",
    ] if x)
    if meta:
        lines.append(meta)
    lines.append(f"<i>{esc(a['title'])}</i>")
    return "\n".join(lines), a["link"]


# ---------- Telegram ----------

def tg(method, **params):
    token = os.environ["TELEGRAM_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
        return json.loads(r.read())


def send(text, link=None):
    params = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"}
    if link:
        params["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "🛒 Voir le deal", "url": link}]]})
    return tg("sendMessage", **params)


# ---------- Mémoire des annonces déjà vues ----------

def load_state():
    """seen.json : annonces déjà vues, deals déjà envoyés, santé des sources."""
    st = {"ids": [], "deals": [], "health": {}}
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        if isinstance(data, list):          # ancien format
            st["ids"] = data
        else:
            st.update(data)
    return st


def save_state(st):
    st["ids"] = st["ids"][-3000:]
    st["deals"] = st["deals"][-3000:]
    SEEN_FILE.write_text(json.dumps(st))


# ---------- Source 2 : Brickfact (prix relevés directement chez les marchands) ----------
# Une page par pays ; on lit les N premières pages de chaque liste de promos.
BRICKFACT = {
    "UK": "https://brickfact.com/en-gb/lego-deals",
    "FR": "https://brickfact.com/fr-fr/lego-offres",
    "DE": "https://brickfact.com/de-de/lego-angebote",
}
BF_PAGES = int(os.getenv("BF_PAGES", "3"))
BF_LINK_RE = re.compile(r'href="(?:https://brickfact\.com)?(/[a-z]{2}(?:-[a-z]{2})?/[a-z\-]+/[a-z0-9\-]*?-(\d{4,6}))"')


def parse_brickfact(html_text, country):
    """Renvoie une liste d'annonces au même format que les flux RSS.
    Pour chaque lien vers une fiche de set, on prend le plus petit prix affiché
    entre ce lien et le lien suivant (prix actuel < prix public)."""
    items, links = [], list(BF_LINK_RE.finditer(html_text))
    for i, m in enumerate(links):
        path, num = m.group(1), m.group(2)
        chunk = html_text[m.end(): links[i + 1].start() if i + 1 < len(links) else m.end() + 3000]
        text = re.sub(r"<[^>]+>", " ", chunk)
        prices = []
        for pm in PRICE_RE.finditer(text):
            if pm.group(1):
                prices.append((to_float(pm.group(2)), "GBP" if pm.group(1) == "£" else "EUR"))
            else:
                prices.append((to_float(pm.group(3)), "GBP" if pm.group(4) == "£" else "EUR"))
        prices = [p for p in prices if p[0] > 0]
        if not prices:
            continue
        price, cur = min(prices)
        sym = "£" if cur == "GBP" else "€"
        items.append({
            "id": f"bf:{country}:{num}:{price:.2f}",
            "title": f"LEGO {num} {sym}{price:.2f} (Brickfact {country})",
            "link": "https://brickfact.com" + path,
            "price_attr": None,
            "merchant": f"meilleur prix {country} via Brickfact",
        })
    # un set peut apparaître plusieurs fois sur la page : on garde la 1re occurrence
    uniq = {}
    for it in items:
        uniq.setdefault(it["id"], it)
    return list(uniq.values())


def brickfact_items():
    """Renvoie [(nom, items, erreur)] : une source par pays, suivie séparément."""
    results = []
    for country, base in BRICKFACT.items():
        found, err = [], None
        for page in range(1, BF_PAGES + 1):
            url = base if page == 1 else f"{base}/{page}"
            try:
                found.extend(parse_brickfact(fetch(url).decode("utf-8", "replace"), country))
            except Exception as e:
                err = f"{url} : {e}"
                break
        if not found and not err:
            err = "page lue mais aucun prix trouvé (format changé ?)"
        results.append((f"Brickfact {country}", found, err if not found else None))
    return results


# ---------- Source 3 : serveur MCP public de Brickfact (officiel, sans clé) ----------
# https://mcp.brickfact.com/mcp : offres des marchands surveillés (Fnac, Amazon, Darty, LEGO…)
# et valeur de marché quotidienne des sets scellés. On interroge quelques sets par passage,
# à tour de rôle, pour rester raisonnable avec leur serveur.
BF_MCP_URL = "https://mcp.brickfact.com/mcp"
BF_MARKETS = [m.strip() for m in os.getenv("BF_MARKETS", "GB,FR").split(",") if m.strip()]
BF_BATCH = int(os.getenv("BF_BATCH", "10"))         # sets interrogés par passage et par pays
BF_CUR = {"GB": "GBP", "FR": "EUR", "DE": "EUR", "ES": "EUR", "IT": "EUR", "NL": "EUR", "AT": "EUR"}


class RateLimited(Exception):
    pass


class BrickfactMCP:
    def __init__(self):
        self.sid, self.n = None, 0

    def _post(self, body):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "User-Agent": "lego-bot/1.0 (+https://github.com/thomasneilz/lego-bot)"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(BF_MCP_URL, data=json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self.sid = r.headers.get("Mcp-Session-Id") or self.sid
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(e.headers.get("Retry-After", "?"))
            raise
        for line in raw.splitlines():          # réponse JSON directe ou flux SSE
            if line.startswith("data:"):
                raw = line[5:].strip()
        return json.loads(raw) if raw.strip() else None

    def start(self):
        self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "lego-bot", "version": "1.0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool, **args):
        self.n += 1
        res = self._post({"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}})
        r = (res or {}).get("result") or {}
        if r.get("isError"):
            raise RuntimeError(str(r.get("content"))[:200])
        if "structuredContent" in r:
            return r["structuredContent"]
        for c in r.get("content") or []:        # repli : JSON dans le texte
            try:
                return json.loads(c.get("text", ""))
            except Exception:
                pass
        return {}


_bf = None
_valuations = {}


def bf_client():
    global _bf
    if _bf is None:
        _bf = BrickfactMCP()
        _bf.start()
    return _bf


def in_window(s, today=None):
    days = (s["retire"] - (today or date.today())).days
    return -RETIRED_GRACE <= days <= RETIRE_WINDOW


def brickfact_offer_items(watchlist, st):
    """Interroge à tour de rôle BF_BATCH sets de la watchlist (dans la fenêtre de retraite)."""
    nums = sorted(n for n, s in watchlist.items() if in_window(s))
    if not nums:
        return [], None
    cur = st.get("bf_cursor", 0) % len(nums)
    batch = (nums + nums)[cur:cur + min(BF_BATCH, len(nums))]
    st["bf_cursor"] = cur + len(batch)
    items, errors = [], []
    client = bf_client()
    for num in batch:
        for market in BF_MARKETS:
            try:
                data = client.call("get_set_offers", market=market, set_id=int(num), price_basis="item")
            except RateLimited as e:
                return items, f"limite de requêtes atteinte (Retry-After {e})"
            except Exception as e:
                errors.append(f"{num}/{market} : {e}")
                continue
            offers = [o for o in data.get("offers") or [] if o.get("item_price")]
            if not offers:
                continue
            # Prix livré (article + port) : c'est ce que tu paies vraiment.
            # Port inconnu = non compté comme gratuit : on écarte ces offres.
            offers = [o for o in offers if o.get("shipping") is not None]
            if not offers:
                continue
            best = min(offers, key=lambda o: o.get("total") or o["item_price"] + o["shipping"])
            price = float(best.get("total") or best["item_price"] + best["shipping"])
            sym = "£" if (best.get("currency") or BF_CUR.get(market)) == "GBP" else "€"
            ship = float(best["shipping"])
            ship_txt = " (livraison offerte)" if ship == 0 else f" (dont {sym}{ship:.2f} de port)"
            items.append({
                "id": f"bfo:{market}:{num}:{best.get('retailer')}:{price:.2f}",
                "title": f"LEGO {num} {sym}{price:.2f} @ {best.get('retailer')}{ship_txt}",
                "link": (data.get("comparison_url") or "").split("?")[0],
                "price_attr": None,
                "merchant": f"{best.get('retailer')} (via Brickfact)",
                "country": market,
            })
    err = None
    if errors and not items:
        err = "; ".join(errors[:3])
    return items, err


def market_value(num):
    """Valeur de marché actuelle d'un set scellé (EUR, Brickfact). None si indisponible."""
    if num in _valuations:
        return _valuations[num]
    val = None
    try:
        data = bf_client().call("get_price_history", market="FR", set_id=int(num), range="1M")
        pts = [p for p in data.get("points") or [] if p.get("value")]
        if pts:
            val = float(sorted(pts, key=lambda p: p["date"])[-1]["value"])
    except Exception as e:
        print(f"[!] valeur Brickfact {num} : {e}", file=sys.stderr)
    _valuations[num] = val
    return val


# ---------- Santé des sources ----------
FAIL_ALERT_AFTER = 8          # 8 passages ratés d'affilée (~2 h) avant de te prévenir


def track_health(st, name, ok, detail, dry_run):
    h = st["health"].setdefault(name, {"fails": 0, "warned": False, "ever_ok": False})
    if ok:
        if h["warned"] and not dry_run:
            send(f"✅ Source rétablie : {esc(name)}")
        h.update(fails=0, warned=False, ever_ok=True)
        return
    h["fails"] += 1
    if h["fails"] >= FAIL_ALERT_AFTER and not h["warned"]:
        msg = (f"⚠️ <b>Source en panne</b> : {esc(name)}\n"
               f"{h['fails']} passages ratés d'affilée. Les autres sources continuent.\n"
               f"<i>{esc(detail)[:300]}</i>")
        print(msg, file=sys.stderr)
        if not dry_run:
            send(msg)
        h["warned"] = True


# ---------- Commandes Telegram : /achat, /stock, /deals ----------
# Lues à chaque passage (toutes les 15 min) via getUpdates.
# Seuls les messages venant de TELEGRAM_CHAT_ID sont pris en compte.
SELL_TARGET = float(os.getenv("SELL_TARGET", "50"))   # alerte « moment de vendre » à +50 %
HELP = ("🧱 <b>Commandes du bot LEGO</b>\n"
        "/achat 75415 48.99 : enregistre un achat. Virgule ou point, € ou £ avant ou après.\n"
        "   Options : x2 (quantité), nom du magasin. Ex. : /achat 75399 35,99£ x2 amazon\n"
        "/stock : ton stock, sa valeur et ta plus-value (bouton ✅ Vendu)\n"
        "/deals : meilleures affaires des 7 derniers jours\n"
        "/deals fnac : seulement ce magasin (leclerc, amazon, argos…)\n"
        "/aide : réaffiche et réépingle ce message\n"
        "<i>Réponse en 15 min max (mode secours, sans Cloudflare).</i>")


NEW_DEALS = []


def remember_deal(st, a):
    NEW_DEALS.append(None)
    st.setdefault("recent", []).append({
        "ts": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="minutes"), "set": a["set"], "name": a["name"],
        "price": a["price"], "cur": a["cur"], "rrp": a["rrp"], "discount": round(a["discount"]),
        "score": quality(a)[2], "merchant": a.get("merchant") or "", "country": a.get("country") or "",
        "link": a["link"], "title": a["title"]})
    NEW_DEALS[-1] = st["recent"][-1]
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None).timestamp() - 7 * 86400)
    st["recent"] = [d for d in st["recent"]
                    if datetime.fromisoformat(d["ts"]).timestamp() >= cutoff][-300:]


def load_stock_defaults():
    return {"offset": 0, "items": [], "sold": [], "pending_sale": None, "next_id": 1,
            "menu_set": False, "last_sell_check": ""}


def load_stock():
    data = load_stock_defaults()
    if STOCK_FILE.exists():
        data.update(json.loads(STOCK_FILE.read_text(encoding="utf-8")))
    return data


def save_stock(stock):
    STOCK_FILE.write_text(json.dumps(stock, ensure_ascii=False, indent=1), encoding="utf-8")


def backup_stock():
    """Envoie stock.json en silence dans Telegram. Pour restaurer : renvoie ce fichier au bot."""
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    boundary = "----legobot" + str(int(datetime.now(timezone.utc).replace(tzinfo=None).timestamp()))
    parts = []
    for k, v in {"chat_id": chat, "disable_notification": "true",
                 "caption": "💾 Sauvegarde de ton stock (renvoie ce fichier au bot pour le restaurer)"}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="stock.json"\r\n'
                  'Content-Type: application/json\r\n\r\n').encode() + STOCK_FILE.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=30).read()


def restore_stock(file_id, stock):
    """Recharge un stock.json envoyé au bot (en gardant la position de lecture des messages)."""
    token = os.environ["TELEGRAM_TOKEN"]
    path = tg("getFile", file_id=file_id)["result"]["file_path"]
    data = json.loads(fetch(f"https://api.telegram.org/file/bot{token}/{path}"))
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError("ce fichier n'est pas une sauvegarde de stock")
    offset = stock["offset"]
    stock.clear()
    stock.update(load_stock_defaults())
    stock.update(data)
    stock["offset"] = offset


_fx = {}


def eur_to_gbp():
    """Taux BCE du jour (frankfurter.app). None si indisponible : on n'invente pas de taux."""
    if "r" not in _fx:
        try:
            _fx["r"] = float(json.loads(fetch("https://api.frankfurter.app/latest?from=EUR&to=GBP"))["rates"]["GBP"])
        except Exception as e:
            print(f"[!] taux de change : {e}", file=sys.stderr)
            _fx["r"] = None
    return _fx["r"]


def value_in(cur, num):
    """Valeur de marché d'un set dans la devise d'achat, ou None."""
    mv = market_value(num)
    if mv is None:
        return None
    if cur == "EUR":
        return mv
    r = eur_to_gbp()
    return mv * r if r else None


def parse_achat(args):
    """'/achat 75415 48.99 x2 fnac' -> dict ou message d'erreur."""
    num, price, cur, qty, shop = None, None, "EUR", 1, []
    for tok in args:
        t = tok.strip()
        if num is None and re.fullmatch(r"\d{4,6}(-1)?", t):
            num = t.split("-")[0]
        elif price is None and re.fullmatch(r"[£€]?\d+(?:[.,]\d{1,2})?[£€]?", t):
            if "£" in t:
                cur = "GBP"
            price = to_float(t.strip("£€"))
        elif re.fullmatch(r"[xX]\d{1,3}", t):
            qty = int(t[1:])
        elif t.lower() in ("£", "gbp"):
            cur = "GBP"
        elif t.lower() in ("€", "eur"):
            cur = "EUR"
        else:
            shop.append(t)
    if not num or price is None:
        return "Format : /achat 75415 48.99 (options : x2, £, magasin)"
    return {"set": num, "price": price, "cur": cur, "qty": qty, "shop": " ".join(shop)}


def sym(cur):
    return "£" if cur == "GBP" else "€"


def stock_message(stock, watchlist):
    if not stock["items"]:
        return "📦 Ton stock est vide. Ajoute un achat avec /achat 75415 48.99", None
    lines, buttons, tot = ["📦 <b>Ton stock</b>"], [], {}
    for it in stock["items"]:
        name = watchlist.get(it["set"], {}).get("name", "")
        cost = it["price"] * it["qty"]
        v = value_in(it["cur"], it["set"])
        s = sym(it["cur"])
        row = f"\n<b>{it['set']}</b> {esc(name)} · x{it['qty']} à {s}{it['price']:.2f}"
        if it.get("shop"):
            row += f" ({esc(it['shop'])})"
        if v:
            gain = (v - it["price"]) * it["qty"]
            row += f"\n   Valeur marché ~{s}{v:.0f}/boîte → <b>{gain:+.0f} {s}</b> ({(v / it['price'] - 1) * 100:+.0f} %)"
            t = tot.setdefault(it["cur"], [0, 0])
            t[0] += cost
            t[1] += v * it["qty"]
        else:
            row += "\n   Valeur marché indisponible"
        lines.append(row)
        buttons.append([{"text": f"✅ Vendu : {it['set']} (x{it['qty']})", "callback_data": f"sold:{it['id']}"}])
    for cur, (c, v) in tot.items():
        lines.append(f"\n💰 Total {sym(cur)} : payé {sym(cur)}{c:.0f}, vaut ~{sym(cur)}{v:.0f} ({v - c:+.0f})")
    if stock["sold"]:
        realised = {}
        for x in stock["sold"]:
            realised[x["cur"]] = realised.get(x["cur"], 0) + (x["sale"] - x["price"]) * x["qty"]
        lines.append("🏁 Déjà réalisé : " + ", ".join(f"{v:+.0f} {sym(c)}" for c, v in realised.items()))
    lines.append("\n<i>Valeur marché : set scellé, Brickfact. Indicatif.</i>")
    return "\n".join(lines), {"inline_keyboard": buttons}


def deals_message(st, shop=None):
    recent = st.get("recent", [])
    if shop:
        recent = [d for d in recent if shop.lower() in (d["merchant"] + " " + d["title"]).lower()]
    if not recent:
        return (f"Aucun deal {'chez ' + esc(shop) + ' ' if shop else ''}sur les 7 derniers jours "
                "qui passe tes règles.")
    best = {}
    for d in recent:                       # le meilleur prix par set
        k = (d["set"], d["cur"])
        if k not in best or d["price"] < best[k]["price"]:
            best[k] = d
    top = sorted(best.values(), key=lambda d: -d["score"])[:10]
    lines = [f"🔥 <b>Meilleurs deals{' · ' + esc(shop) if shop else ''}</b> (7 derniers jours)"]
    for d in top:
        dot = "🟢" if d["score"] >= 75 else "🟡" if d["score"] >= 62 else "🟠"
        flag = FLAGS.get(d["country"], "")
        lines.append(f"{dot} {d['score']} · <b>-{d['discount']} %</b> · {d['set']} {esc(d['name'])}\n"
                     f"   {sym(d['cur'])}{d['price']:.2f} · {esc(d['merchant'] or '?')} {flag} · "
                     f"<a href=\"{html.escape(d['link'], quote=True)}\">voir</a> · {d['ts'][5:10].replace('-', '/')}")
    lines.append("<i>Le prix a pu changer depuis : vérifie avant d'acheter.</i>")
    return "\n".join(lines)


def tg_send(text, markup=None, reply_markup_force=False):
    params = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"}
    if markup:
        params["reply_markup"] = json.dumps(markup)
    if reply_markup_force:
        params["reply_markup"] = json.dumps({"force_reply": True, "input_field_placeholder": "Prix de vente"})
    return tg("sendMessage", **params)


WORKER_URL = os.getenv("WORKER_URL", "").rstrip("/")
SHARED_SECRET = os.getenv("SHARED_SECRET", "")


def worker_call(method, path, payload=None):
    req = urllib.request.Request(WORKER_URL + path, method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"X-Shared-Secret": SHARED_SECRET, "Content-Type": "application/json",
                                          "User-Agent": "lego-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def fetch_updates(stock):
    """Renvoie (updates, mode).
    mode "worker" : Cloudflare répond lui-même aux commandes, instantanément.
                    Ici on garde juste une copie du stock (secours) et on vérifie le branchement.
    mode "poll"   : pas de Cloudflare (ou Cloudflare en panne) : lecture toutes les 15 min."""
    if WORKER_URL and SHARED_SECRET:
        try:
            worker_call("GET", "/health")
            remote = worker_call("GET", "/export")
            local = {"items": stock.get("items", []), "sold": stock.get("sold", [])}
            if stock.get("fallback") or (not remote["items"] and not remote["sold"]
                                         and (local["items"] or local["sold"]) and not stock.get("migrated")):
                # Cloudflare revient (ou premier branchement) : on lui renvoie le stock tenu en secours
                worker_call("POST", "/import", local)
                remote = local
            stock["migrated"] = True
            stock["items"], stock["sold"] = remote["items"], remote["sold"]
            info = tg("getWebhookInfo").get("result", {})
            if info.get("url") != WORKER_URL + "/":
                worker_call("GET", "/setup")          # branche Telegram sur Cloudflare + menu + épinglage
                if stock.get("fallback"):
                    tg_send("✅ Réponses instantanées rétablies (Cloudflare répond de nouveau).")
            stock["fallback"] = False
            return [], "worker"
        except Exception as e:
            print(f"[!] Cloudflare indisponible : {e}", file=sys.stderr)
            try:
                tg("deleteWebhook")                   # secours : on relit les messages toutes les 15 min
            except Exception:
                pass
            if not stock.get("fallback"):
                stock["fallback"] = True
                tg_send("⚠️ Cloudflare ne répond pas : je repasse en mode 15 minutes pour tes commandes. "
                        "Je réessaie à chaque passage.")
    updates = tg("getUpdates", offset=stock["offset"], timeout=0).get("result", [])
    return updates, "poll"


def pin_help():
    """Envoie la liste des commandes et l'épingle en haut de la conversation."""
    r = tg_send(HELP)
    tg("pinChatMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"],
       message_id=r["result"]["message_id"], disable_notification="true")


def process_commands(st, watchlist):
    """Lit les nouveaux messages Telegram et y répond. Renvoie True si le stock a changé."""
    stock = load_stock()
    changed = False
    before = json.dumps([stock["items"], stock["sold"]], sort_keys=True)
    chat = str(os.environ["TELEGRAM_CHAT_ID"])
    worker_mode = bool(WORKER_URL and SHARED_SECRET)
    if not worker_mode and stock.get("menu_set") != "v2":
        try:
            tg("setMyCommands", commands=json.dumps([
                {"command": "achat", "description": "Enregistrer un achat : /achat 75415 48.99"},
                {"command": "stock", "description": "Mon stock, sa valeur, bouton Vendu"},
                {"command": "deals", "description": "Meilleurs deals (option : magasin)"},
                {"command": "aide", "description": "Liste des commandes (épinglée)"}]))
            stock["menu_set"], changed = "v2", True
        except Exception as e:
            print(f"[!] menu Telegram : {e}", file=sys.stderr)
    if not worker_mode and not stock.get("help_pinned"):
        try:
            pin_help()
            stock["help_pinned"], changed = True, True
        except Exception as e:
            print(f"[!] épinglage : {e}", file=sys.stderr)
    fb_before = stock.get("fallback")
    updates, mode = fetch_updates(stock)
    if stock.get("fallback") != fb_before:
        changed = True
    done = set(stock.get("done_ids", []))
    processed = []
    for u in updates:
        processed.append(u["update_id"])
        if mode == "poll":
            stock["offset"] = u["update_id"] + 1
        changed = True
        if u["update_id"] in done:
            continue                      # déjà traité (accusé non reçu par Cloudflare)
        done.add(u["update_id"])
        stock.setdefault("done_ids", []).append(u["update_id"])
        stock["done_ids"] = stock["done_ids"][-300:]
        cb = u.get("callback_query")
        msg = u.get("message") or {}
        from_chat = str((cb or {}).get("message", {}).get("chat", {}).get("id") or msg.get("chat", {}).get("id"))
        if from_chat != chat:
            continue                      # quelqu'un d'autre écrit au bot : ignoré
        try:
            if cb:
                try:
                    tg("answerCallbackQuery", callback_query_id=cb["id"])
                except Exception:
                    pass                  # trop tard pour ce bouton, pas grave
                if cb.get("data", "").startswith("sold:"):
                    iid = int(cb["data"].split(":")[1])
                    it = next((x for x in stock["items"] if x["id"] == iid), None)
                    if it:
                        stock["pending_sale"] = iid
                        tg_send(f"Prix de vente par boîte pour {it['set']} (x{it['qty']}) ?\n"
                                f"Réponds juste le prix, par ex. 85 (devise : {sym(it['cur'])}).",
                                reply_markup_force=True)
                continue
            doc = msg.get("document")
            if doc and doc.get("file_name", "").endswith(".json"):
                text = "le fichier"
                restore_stock(doc["file_id"], stock)
                tg_send(f"♻️ Stock restauré : {len(stock['items'])} set(s) en stock, {len(stock['sold'])} vente(s).")
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            if stock.get("pending_sale") and re.fullmatch(r"[£€]?\d+(?:[.,]\d{1,2})?[£€]?", text):
                it = next((x for x in stock["items"] if x["id"] == stock["pending_sale"]), None)
                stock["pending_sale"] = None
                if it:
                    sale = to_float(text.strip("£€"))
                    stock["items"].remove(it)
                    stock["sold"].append({**it, "sale": sale, "date": date.today().isoformat()})
                    gain = (sale - it["price"]) * it["qty"]
                    tg_send(f"🏁 Vente enregistrée : {it['set']} x{it['qty']} à {sym(it['cur'])}{sale:.2f}\n"
                            f"Plus-value brute : <b>{gain:+.2f} {sym(it['cur'])}</b> "
                            f"({(sale / it['price'] - 1) * 100:+.0f} %)")
                continue
            parts = text.split()
            cmd = parts[0].split("@")[0].lower()
            if cmd == "/achat":
                r = parse_achat(parts[1:])
                if isinstance(r, str):
                    tg_send(r)
                    continue
                r["id"], r["date"] = stock["next_id"], date.today().isoformat()
                stock["next_id"] += 1
                stock["items"].append(r)
                name = watchlist.get(r["set"], {}).get("name", "")
                tg_send(f"✅ Achat enregistré : {r['set']} {esc(name)} x{r['qty']} à {sym(r['cur'])}{r['price']:.2f}"
                        + (f" ({esc(r['shop'])})" if r["shop"] else "")
                        + f"\nJe te préviens quand il prend +{SELL_TARGET:.0f} %. /stock pour voir le tout.")
            elif cmd == "/stock":
                text_, markup = stock_message(stock, watchlist)
                tg_send(text_, markup)
            elif cmd in ("/aide", "/help", "/start"):
                pin_help()
            elif cmd == "/deals":
                tg_send(deals_message(st, " ".join(parts[1:]) or None))
            else:
                tg_send(HELP)
        except Exception as e:
            print(f"[!] commande : {e}", file=sys.stderr)
            tg_send(f"⚠️ Je n'ai pas pu traiter « {esc(text if not cb else 'le bouton')[:60]} » : {esc(e)[:120]}")
    if mode == "worker":
        try:
            worker_call("POST", "/daily", {})          # contrôle « moment de vendre » (1 fois par jour)
        except Exception as e:
            print(f"[!] contrôle quotidien Cloudflare : {e}", file=sys.stderr)
        save_stock(stock)                            # copie de secours du stock (cache privé GitHub)
        return changed
    # Une fois par jour : alerte « moment de vendre »
    today = date.today().isoformat()
    if stock["items"] and stock.get("last_sell_check") != today and datetime.now(timezone.utc).replace(tzinfo=None).hour >= 8:
        stock["last_sell_check"], changed = today, True
        for it in stock["items"]:
            v = value_in(it["cur"], it["set"])
            if v and (v / it["price"] - 1) * 100 >= SELL_TARGET and not it.get("sell_alerted"):
                it["sell_alerted"] = True
                tg_send(f"📈 <b>Moment de vendre ?</b> {it['set']} vaut maintenant ~{sym(it['cur'])}{v:.0f} "
                        f"(acheté {sym(it['cur'])}{it['price']:.2f}, {(v / it['price'] - 1) * 100:+.0f} %).\n"
                        "Vérifie les ventes réelles avant de mettre en vente.")
    if changed:
        save_stock(stock)
        if json.dumps([stock["items"], stock["sold"]], sort_keys=True) != before:
            try:
                backup_stock()
            except Exception as e:
                print(f"[!] sauvegarde Telegram : {e}", file=sys.stderr)
    return changed


# ---------- Programme principal ----------

def collect(feed_files=None, watchlist=None, st=None):
    """Renvoie [(nom_source, items, erreur)]."""
    if feed_files:
        return [(f, parse_feed(Path(f).read_bytes()), None) for f in feed_files]
    results = []
    feeds = [f.strip() for f in os.getenv("FEEDS", ",".join(DEFAULT_FEEDS)).split(",") if f.strip()]
    for src in feeds:
        try:
            results.append((src, parse_feed(fetch(src)), None))
        except Exception as e:      # un flux en panne ne bloque pas les autres
            results.append((src, [], str(e)))
    # Désactivé par défaut : Brickfact bloque les serveurs de GitHub (erreur 403).
    if os.getenv("BRICKFACT", "0") == "1":
        results.extend(brickfact_items())
    if os.getenv("BRICKFACT_MCP", "1") == "1" and watchlist is not None:
        try:
            items, err = brickfact_offer_items(watchlist, st)
        except Exception as e:
            items, err = [], str(e)
        results.append(("Brickfact (offres marchands)", items, err))
    return results


def run(dry_run=False, feed_files=None, status=False):
    watchlist = load_watchlist()
    st = load_state()
    seen, sent_deals = set(st["ids"]), set(st["deals"])
    sent, report = 0, []
    if not feed_files and not dry_run and os.getenv("TELEGRAM_TOKEN"):
        try:
            process_commands(st, watchlist)
        except Exception as e:
            print(f"[!] commandes Telegram : {e}", file=sys.stderr)
    for name, items, err in collect(feed_files, watchlist, st):
        ok = err is None
        report.append(f"{'✅' if ok else '❌'} {name.replace('https://', '')} : "
                      f"{len(items)} annonces" + ("" if ok else f" ({err[:80]})"))
        if err:
            print(f"[!] {name} : {err}", file=sys.stderr)
        if not feed_files:
            track_health(st, name, ok, err or "", dry_run)
        for item in items:
            item.setdefault("country", country_of(name))
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            st["ids"].append(item["id"])
            alert = evaluate(item, watchlist)
            if not alert:
                continue
            # Même set, même prix, même devise = même deal, quelle que soit la source
            key = f"{alert['set']}|{alert['cur']}|{alert['price']:.0f}"
            if key in sent_deals:
                continue
            sent_deals.add(key)
            st["deals"].append(key)
            remember_deal(st, alert)
            msg, link = format_alert(alert)
            print(msg, "\n")
            if not dry_run:
                send(msg, link)
            sent += 1
    print("\n".join(report))
    if NEW_DEALS and not dry_run and WORKER_URL and SHARED_SECRET:
        try:
            worker_call("POST", "/deals", NEW_DEALS)    # pour /deals instantané
        except Exception as e:
            print(f"[!] envoi des deals à Cloudflare : {e}", file=sys.stderr)
    if status and not dry_run:
        send("🩺 <b>État des sources</b>\n" + "\n".join(esc(r) for r in report)
             + f"\n\n{sent} nouvelle(s) alerte(s) sur ce passage.")
    if not dry_run:
        save_state(st)
    print(f"{sent} alerte(s).")
    return sent


def main():
    args = sys.argv[1:]
    if "--test" in args:
        send("✅ Bot LEGO connecté. Tu recevras ici les deals qui passent tes 3 règles.")
        print("Message de test envoyé.")
    elif "--chat-id" in args:
        for u in tg("getUpdates").get("result", []):
            chat = (u.get("message") or {}).get("chat", {})
            if chat:
                print(f"chat_id = {chat['id']}  ({chat.get('first_name', '')})")
    else:
        files = [a.split("=", 1)[1] for a in args if a.startswith("--feed-file=")]
        run(dry_run="--dry-run" in args, feed_files=files or None, status="--status" in args)


if __name__ == "__main__":
    main()
