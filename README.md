# Bot LEGO pour Telegram

Toutes les 15 minutes, le bot regarde deux types de sources :
- **les deals publiés par la communauté** : hotukdeals (UK), Dealabs (France), mydealz (Allemagne). C'est là qu'on trouve les codes promo, les remises en magasin, les cagnottes et les erreurs de prix ;
- **les prix relevés directement chez les marchands** : Brickfact UK, France et Allemagne (Amazon, Fnac, Carrefour…), même quand personne n'a publié le deal.

Un même deal vu par plusieurs sources ne t'est envoyé qu'une fois. Si une source ne répond plus pendant environ 2 heures, tu reçois un message « ⚠️ Source en panne », puis « ✅ Source rétablie » quand elle revient. Quand tu lances le bot à la main (bouton « Run workflow »), il t'envoie aussi l'état de chaque source. Il ne t'envoie un message que si un deal passe tes 3 règles :

1. le set part à la retraite dans les 6 mois (ou est retiré depuis moins de 3 mois) ;
2. le prix est au moins 30 % sous le prix public ;
3. le set est dans ta `watchlist.csv`.

Il tourne gratuitement sur GitHub, sans serveur et sans IA.

## Installation (environ 20 minutes)

### 1. Créer le bot Telegram
1. Dans Telegram, ouvre **@BotFather** et envoie `/newbot`.
2. Choisis un nom, par exemple `Lego Deals Thomas`, et un identifiant qui finit par `bot`.
3. BotFather te donne une **clé** (token) qui ressemble à `123456:ABC-xyz...`. Garde-la pour toi.
4. Ouvre ton nouveau bot et envoie-lui « salut » (obligatoire pour qu'il puisse t'écrire).

### 2. Récupérer ton chat_id
Sur ton ordi, dans le dossier du bot :
```bash
export TELEGRAM_TOKEN="ta_clé"
python3 bot.py --chat-id
```
Tu obtiens un nombre, par exemple `chat_id = 123456789`.

Tu peux vérifier que tout marche :
```bash
export TELEGRAM_CHAT_ID="123456789"
python3 bot.py --test
```

### 3. Mettre le bot sur GitHub
1. Crée un compte sur github.com si tu n'en as pas.
2. Crée un nouveau dépôt **privé**, par exemple `lego-bot`.
3. Envoie-y tous les fichiers de ce dossier, y compris le dossier `.github`. Le plus simple : bouton « Add file » puis « Upload files », en glissant le dossier.
4. Va dans **Settings → Secrets and variables → Actions → New repository secret** et ajoute :
   - `TELEGRAM_TOKEN` : ta clé
   - `TELEGRAM_CHAT_ID` : ton chat_id
5. Va dans l'onglet **Actions**, ouvre « Bot LEGO » et clique sur **Run workflow** pour un premier essai.

Ensuite, le bot tourne tout seul toutes les 15 minutes.

## Premier lancement : vérifier les flux
Dans **Actions**, clique sur le dernier passage et lis le journal. Une ligne comme `[!] https://www.hotukdeals.com/rss/new : HTTP Error 403` veut dire que le site a bloqué ce flux. Dans ce cas :
1. Va sur **rss.app** et colle `https://www.hotukdeals.com/tag/lego` (et `https://www.dealabs.com/groupe/lego` pour la France).
2. Copie l'adresse du flux généré.
3. Dans le fichier `.github/workflows/lego-bot.yml`, ajoute sous `RETIRE_WINDOW` une ligne :
   `FEEDS: "https://rss.app/feeds/xxx.xml,https://rss.app/feeds/yyy.xml"`

## La watchlist (environ 290 sets, mise à jour automatique)
`watchlist.csv` est générée par `build_watchlist.py` à partir de deux sources :
- `retraites.txt` : dates de retraite de tous les sets en vente (BrickRanker) ;
- `prix.txt` : prix publics UK et zone euro (Brick Fanatics).

Chaque set reçoit un seuil selon son potentiel de revente :
| Classe | Thèmes | Alerte à partir de |
|---|---|---|
| A | Star Wars, Icons, Ideas, Harry Potter, Marvel, DC, Architecture, Art, Zelda, Editions, Pokémon, One Piece, gros Technic (≥ £100) | -30 % |
| B | BrickHeadz, Ninjago, Disney, Jurassic World, Creator, Botanicals, Speed Champions, Fortnite, Minecraft, petits Technic… | -40 % |

Les thèmes pour enfants (Friends, Duplo, City de base, DreamZzz…) et les sets à moins de £15 sont exclus.

### Mise à jour mensuelle, pour les sets futurs
Le 1er de chaque mois, le workflow « Mise à jour de la watchlist » :
1. relit la page de BrickRanker : nouveaux sets annoncés, dates de retraite modifiées ;
2. complète les prix publics manquants si tu as ajouté une clé Brickset (voir plus bas) ;
3. reconstruit `watchlist.csv`, l'enregistre dans le dépôt et t'envoie le résumé sur Telegram.

Si la page source change de format et que la lecture échoue, rien n'est modifié et tu reçois un message d'alerte.

**Clé Brickset (gratuite, conseillée)** : crée un compte sur brickset.com, demande une clé API sur brickset.com/tools/webservices/requestkey, puis ajoute-la dans les Secrets GitHub sous le nom `BRICKSET_KEY`. Sans elle, les nouveaux sets sans prix public atterrissent dans `a_completer.txt` au lieu d'être surveillés.

### Ajouter tes propres sets
Mets-les dans `manuel.csv`. Ils passent devant la liste automatique :
```
set,nom,theme,prix_public_gbp,prix_public_eur,retraite,seuil
72164,Pikachu et la cabane d'entraînement,Pokemon,,69.99,2027-12-31,30
```
Puis lance `python3 build_watchlist.py`, ou attends la mise à jour du mois suivant.

### Changer les classes ou les seuils
En haut de `build_watchlist.py` : les listes `TIER_A` et `TIER_B`, les seuils `SEUIL`, le prix minimum `PRIX_MIN_GBP`.

## Autres réglages
Dans `.github/workflows/lego-bot.yml` :
- `RETIRE_WINDOW: "183"` : n'alerte que pour les sets qui partent dans les 6 mois (les sets retirés depuis moins de 3 mois restent surveillés, pour les fins de stock) ;
- `MIN_DISCOUNT_FLOOR` (optionnel) : remise minimum appliquée à tous les sets, quel que soit leur seuil.

## Tester sans rien envoyer
```bash
python3 bot.py --dry-run --feed-file=sample_feed.xml
```

## L'indicateur de qualité dans les alertes
Chaque alerte affiche une pastille et une note sur 100 :
- 🟢 **Excellente affaire** (75 et plus), 🟡 **Bonne affaire** (62 à 74), 🟠 **Affaire correcte** (en dessous)
- La note combine la remise (jusqu'à 72 points), l'urgence (+18 si la retraite est à moins de 60 jours ou déjà passée, +10 à moins de 120 jours) et la classe du set (+10 pour la classe A).

La **plus-value brute** suppose, de façon prudente, une revente à +15 % du prix public après la retraite pour la classe A (+5 % pour la classe B), moins ton prix d'achat. La ligne **net** retire en plus 15 % de frais de revente (commission eBay/Vinted/BrickLink et envoi du colis). C'est un ordre de grandeur : vérifie toujours les ventes réelles sur BrickLink avant d'acheter en quantité. Les réglages sont dans `bot.py` (`REVALO`, `FRAIS`).
