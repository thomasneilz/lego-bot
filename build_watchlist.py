#!/usr/bin/env python3
"""
Construit watchlist.csv à partir des données de retraite et des prix publics.

  python build_watchlist.py            # à partir des fichiers data/ (hors ligne)
  python build_watchlist.py --online   # rafraîchit d'abord les données sur internet
                                       # (lancé chaque mois par GitHub Actions)

Sources :
  - retraites.txt  date;numero;nom;theme   (BrickRanker, retirement tracker)
  - prix.txt       numero;gbp;eur          (Brick Fanatics + Brickset)
  - manuel.csv     tes ajouts à la main, prioritaires sur tout le reste

En mode --online :
  - lit https://brickranker.com/retirement-tracker et met à jour dates et nouveaux sets
  - si BRICKSET_KEY est défini, complète les prix publics manquants via l'API Brickset
  - si TELEGRAM_TOKEN / TELEGRAM_CHAT_ID sont définis, t'envoie le résumé des changements
"""

import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE
RETRAITES = DATA / "retraites.txt"
PRIX = DATA / "prix.txt"
MANUEL = DATA / "manuel.csv"
OUT = HERE / "watchlist.csv"
A_COMPLETER = DATA / "a_completer.txt"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# ---------- Classement par potentiel de revente ----------
# A = thèmes adultes / licences fortes  -> alerte dès 30 % de remise
# B = potentiel moyen                  -> alerte seulement à partir de 40 %
# Tout thème absent de ces listes est ignoré (Friends, Duplo, City enfant...).
TIER_A = {
    "Star Wars", "Icons", "Ideas", "Harry Potter", "Marvel", "DC", "Architecture",
    "Art", "Zelda", "Editions", "Pokemon", "One Piece",
}
TIER_B = {
    "BrickHeadz", "Ninjago", "Disney", "Jurassic World", "Creator", "Botanicals",
    "Wicked", "Wednesday", "Fortnite", "Minecraft", "Speed Champions", "Sonic",
    "Animal Crossing", "City", "Miscellaneous", "Super Mario", "Technic",
}
SEUIL = {"A": 30, "B": 40}
PRIX_MIN_GBP = 15            # en dessous : trop petit pour valoir l'envoi
EXCEPTIONS_PRIX_MIN = {"BrickHeadz"}
TECHNIC_A_PARTIR_DE = 100    # les gros Technic (voitures, F1) passent en A

# Correspondance des noms de thèmes BrickRanker -> nos noms
THEME_ALIAS = {
    "Super Heroes Marvel": "Marvel", "Super Heroes DC": "DC", "Brickheadz": "BrickHeadz",
    "The Legend of Zelda": "Zelda", "Pokémon": "Pokemon", "Town": "City",
    "Sonic the Hedgehog": "Sonic", "Ideas and CUUSOO": "Ideas", "Ideas & CUUSOO": "Ideas",
}


def tier_for(theme, gbp):
    if theme == "Technic":
        return "A" if gbp and gbp >= TECHNIC_A_PARTIR_DE else "B"
    if theme == "Super Mario":
        return "A" if gbp and gbp >= 50 else "B"
    if theme in TIER_A:
        return "A"
    if theme in TIER_B:
        return "B"
    return None


# ---------- Lecture / écriture des fichiers de données ----------

def read_lines(path):
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


def load_retraites():
    out = {}
    for l in read_lines(RETRAITES):
        d, num, name, theme = (l.split(";") + ["", "", "", ""])[:4]
        out[num] = {"retraite": d, "nom": name, "theme": THEME_ALIAS.get(theme, theme)}
    return out


def load_prix():
    out = {}
    for l in read_lines(PRIX):
        num, gbp, eur = (l.split(";") + ["", ""])[:3]
        out[num] = (float(gbp) if gbp else None, float(eur) if eur else None)
    return out


def save_retraites(r):
    lines = ["# Source : brickranker.com/retirement-tracker (mis à jour automatiquement)",
             "# date;numero;nom;theme"]
    for num, v in sorted(r.items(), key=lambda kv: (kv[1]["theme"], kv[1]["retraite"], kv[0])):
        lines.append(f"{v['retraite']};{num};{v['nom']};{v['theme']}")
    RETRAITES.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_prix(p):
    lines = ["# Prix publics GBP;EUR (Brick Fanatics, Brickset)"]
    for num, (g, e) in sorted(p.items()):
        lines.append(f"{num};{'' if g is None else g};{'' if e is None else e}")
    PRIX.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_manuel():
    if not MANUEL.exists():
        return []
    with open(MANUEL, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("set") and not r["set"].startswith("#")]


# ---------- Mise à jour en ligne ----------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(20\d\d)", re.I)
SET_RE = re.compile(r"/rankings/set/(\d{4,6})-1/([a-z0-9\-]+)")
HEADING_RE = re.compile(r"<h[23][^>]*>(.*?)</h[23]>", re.I | re.S)


def parse_brickranker(html):
    """Renvoie {numero: {retraite, nom, theme}}. Chaque set prend la date la plus proche dans la page."""
    dates = [(m.start(), date(int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1))))
             for m in DATE_RE.finditer(html)]
    heads = [(m.start(), re.sub(r"<[^>]+>", "", m.group(1)).strip()) for m in HEADING_RE.finditer(html)]
    out = {}
    for m in SET_RE.finditer(html):
        num, slug = m.group(1), m.group(2)
        if num in out or not dates:
            continue
        pos = m.start()
        d = min(dates, key=lambda x: abs(x[0] - pos))[1]
        theme = next((h for p, h in reversed(heads) if p < pos), "")
        theme = re.sub(r"(?i)^lego\s+|\s+sets?.*$|\s+retiring.*$", "", theme).strip()
        out[num] = {"retraite": d.isoformat(), "nom": slug.replace("-", " ").title(),
                    "theme": THEME_ALIAS.get(theme, theme)}
    return out


def brickset_price(num, key):
    params = urllib.parse.urlencode({"apiKey": key, "userHash": "",
                                     "params": json.dumps({"setNumber": f"{num}-1"})})
    data = json.loads(fetch(f"https://brickset.com/api/v3.asmx/getSets?{params}"))
    if data.get("status") != "success" or not data.get("sets"):
        return None, None, None
    s = data["sets"][0]
    lc = s.get("LEGOCom", {})
    gbp = (lc.get("UK") or {}).get("retailPrice")
    eur = (lc.get("DE") or {}).get("retailPrice")
    return gbp, eur, s.get("theme")


def refresh_online(retraites, prix):
    changes = []
    html = fetch("https://brickranker.com/retirement-tracker")
    fresh = parse_brickranker(html)
    if len(fresh) < 150:
        raise RuntimeError(f"Seulement {len(fresh)} sets lus sur BrickRanker : la page a dû changer. "
                           "Rien n'a été modifié.")
    for num, v in fresh.items():
        if num in retraites:
            old = retraites[num]
            if old["retraite"] != v["retraite"]:
                changes.append(f"📅 {num} {old['nom']} : {old['retraite']} → {v['retraite']}")
                old["retraite"] = v["retraite"]
        else:
            retraites[num] = v
            changes.append(f"➕ {num} {v['nom']} ({v['theme'] or '?'}) retraite {v['retraite']}")

    key = os.getenv("BRICKSET_KEY")
    if key:
        for num, v in retraites.items():
            if num in prix and prix[num][0]:
                continue
            try:
                gbp, eur, theme = brickset_price(num, key)
            except Exception as e:
                print(f"[!] Brickset {num} : {e}", file=sys.stderr)
                continue
            if gbp or eur:
                prix[num] = (gbp, eur)
                changes.append(f"💷 {num} prix public £{gbp} / €{eur}")
            if theme and not v["theme"]:
                v["theme"] = THEME_ALIAS.get(theme, theme)
    return changes


# ---------- Construction de la watchlist ----------

def build(retraites, prix):
    today = date.today()
    rows, missing = [], []
    for num, v in retraites.items():
        try:
            d = datetime.strptime(v["retraite"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today - d).days > 120:          # retiré depuis trop longtemps : plus en magasin
            continue
        gbp, eur = prix.get(num, (None, None))
        tier = tier_for(v["theme"], gbp)
        if not tier:
            continue
        if not gbp and not eur:
            missing.append(f"{num};{v['nom']};{v['theme']}")
            continue
        if gbp and gbp < PRIX_MIN_GBP and v["theme"] not in EXCEPTIONS_PRIX_MIN:
            continue
        rows.append({"set": num, "nom": v["nom"], "theme": v["theme"],
                     "prix_public_gbp": gbp or "", "prix_public_eur": eur or "",
                     "retraite": v["retraite"], "seuil": SEUIL[tier]})

    # Les ajouts manuels passent devant tout le reste
    manual = {r["set"]: r for r in load_manuel()}
    rows = [r for r in rows if r["set"] not in manual] + [
        {"set": r["set"], "nom": r.get("nom", ""), "theme": r.get("theme", ""),
         "prix_public_gbp": r.get("prix_public_gbp", ""), "prix_public_eur": r.get("prix_public_eur", ""),
         "retraite": r["retraite"], "seuil": r.get("seuil") or 30}
        for r in manual.values()]

    rows.sort(key=lambda r: (r["retraite"], r["theme"], r["set"]))
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["set", "nom", "theme", "prix_public_gbp",
                                          "prix_public_eur", "retraite", "seuil"])
        w.writeheader()
        w.writerows(rows)
    A_COMPLETER.write_text("# Sets intéressants sans prix public connu : ajoute-les dans manuel.csv\n"
                           + "\n".join(sorted(missing)) + "\n", encoding="utf-8")
    return rows, missing


def notify(text):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)


def main():
    retraites, prix = load_retraites(), load_prix()
    changes = []
    if "--online" in sys.argv:
        try:
            changes = refresh_online(retraites, prix)
            save_retraites(retraites)
            save_prix(prix)
        except Exception as e:
            msg = f"⚠️ Mise à jour de la watchlist impossible : {e}"
            print(msg, file=sys.stderr)
            notify(msg)
            sys.exit(1)
    rows, missing = build(retraites, prix)
    a = sum(1 for r in rows if int(r["seuil"]) == 30)
    summary = (f"🗂 Watchlist : {len(rows)} sets surveillés ({a} en A à -30 %, {len(rows) - a} en B à -40 %). "
               f"{len(missing)} sans prix public (voir a_completer.txt).")
    print(summary)
    if "--online" in sys.argv:
        detail = "\n".join(changes[:40]) + (f"\n… et {len(changes) - 40} autres" if len(changes) > 40 else "")
        notify(summary + ("\n\nChangements :\n" + detail if changes else "\n\nAucun changement ce mois-ci."))


if __name__ == "__main__":
    main()
