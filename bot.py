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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).parent
WATCHLIST = HERE / "watchlist.csv"
SEEN_FILE = HERE / "seen.json"

DEFAULT_FEEDS = [
    "https://www.hotukdeals.com/rss/new",
    "https://www.hotukdeals.com/rss/hot",
    "https://www.dealabs.com/rss/new",
    "https://www.dealabs.com/rss/hot",
    "https://www.mydealz.de/rss/new",
    "https://www.mydealz.de/rss/hot",
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
    meta = " · ".join(x for x in [
        f"🏪 {esc(a['merchant'])}" if a.get("merchant") else "",
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


# ---------- Programme principal ----------

def collect(feed_files=None):
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
    return results


def run(dry_run=False, feed_files=None, status=False):
    watchlist = load_watchlist()
    st = load_state()
    seen, sent_deals = set(st["ids"]), set(st["deals"])
    sent, report = 0, []
    for name, items, err in collect(feed_files):
        ok = err is None
        report.append(f"{'✅' if ok else '❌'} {name.replace('https://', '')} : "
                      f"{len(items)} annonces" + ("" if ok else f" ({err[:80]})"))
        if err:
            print(f"[!] {name} : {err}", file=sys.stderr)
        if not feed_files:
            track_health(st, name, ok, err or "", dry_run)
        for item in items:
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
            msg, link = format_alert(alert)
            print(msg, "\n")
            if not dry_run:
                send(msg, link)
            sent += 1
    print("\n".join(report))
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
