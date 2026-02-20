# 🤖 CopyTrading — Skill Polymarket pour JARVIS

> Copy trading automatique des top traders Polymarket, piloté depuis Telegram.  
> Surveille le leaderboard, copie les trades crypto/politique, circuit breaker intégré.  
> **Mode paper trading par défaut** — aucun fonds réel tant que tu ne l'actives pas.

---

## 📦 Installation

### 1. Placer les deux skills
```bash
# Les deux fichiers doivent être présents
cp skill_polyjarvis.py  jarvis/skills/user/
cp skill_copytrading.py jarvis/skills/user/
```

> ⚠️ **CopyTrading dépend de PolyJarvis** pour l'exécution des trades en mode live.  
> Les deux skills doivent être dans le même dossier.

### 2. Dépendances (déjà installées si PolyJarvis est en place)
```bash
pip install py-clob-client web3 aiohttp
```

### 3. Variables `.env`
```bash
# ── CopyTrading ───────────────────────────────
COPY_MAX_POSITION=10        # USDC max par trade copié (défaut: 10)
COPY_INTERVAL_MIN=15        # Minutes entre chaque cycle (défaut: 15)
COPY_PAPER_MODE=true        # true=simulation | false=trades réels
COPY_LEADERBOARD_WINDOW=30d # Fenêtre : 1d | 7d | 30d | all

# ── PolyJarvis (requis pour le mode live) ─────
POLY_PRIVATE_KEY=0x...
POLY_FUNDER=0x...
POLY_SIG_TYPE=0
```

### 4. Lancer JARVIS
```bash
source venv/bin/activate
python main.py
```

Dans les logs tu verras :
```
📦 Skill chargée: copytrading v1.0.0 (7 cmd)
📦 Skill chargée: polyjarvis v1.0.0 (10 cmd)
```

---

## 🚀 Démarrage rapide

```
/skill list      → vérifier que les deux skills sont chargées
/copystart       → lancer la surveillance (paper mode par défaut)
/copyleaders     → voir les top 10 traders suivis
/copystatus      → positions + P&L en temps réel
```

---

## 📱 Commandes

| Commande | Description |
|---|---|
| `/copystart` | Démarrer la surveillance automatique |
| `/copystop` | Arrêter la surveillance |
| `/copystatus` | Positions copiées + P&L en temps réel |
| `/copyleaders` | Top 10 traders actuellement suivis |
| `/copymode paper` | Passer en simulation (aucun fonds réel) |
| `/copymode live` | Passer en trading réel sur Polygon |
| `/copylog` | Voir les 10 derniers trades copiés |
| `/copyreset` | Réinitialiser le circuit breaker |

---

## ⚙️ Comment ça marche

### Cycle de surveillance (toutes les 15 min)

```
1. Leaderboard Polymarket (top 10 par profit)
         ↓
2. Activité récente de chaque trader (trades des 17 dernières min)
         ↓
3. Filtre : BUY uniquement + catégories crypto/politique
         ↓
4. Déduplication (un trade n'est jamais copié deux fois)
         ↓
5. Paper : simuler + logger
   Live  : exécuter via PolyJarvis + logger
         ↓
6. Notification Telegram pour chaque trade copié
```

### Rafraîchissement du leaderboard

Le leaderboard est rechargé **toutes les 3 heures** (12 cycles de 15 min) pour toujours suivre les traders les plus performants du moment.

### Catégories surveillées

Seuls les marchés **crypto** et **politique** sont copiés :

```
crypto, bitcoin, ethereum, defi, blockchain,
politics, elections, us-politics, president,
congress, government, trump, fed, economy, finance
```

---

## 📄 Paper Trading

Le mode paper trading est **actif par défaut**. Il simule tous les trades sans utiliser de fonds réels.

**Ce qui est simulé :**
- Achat au prix actuel du marché
- Calcul du P&L en temps réel (prix récupéré depuis Gamma API)
- Stockage dans `config/copy_state.json`

**Exemple de sortie `/copystatus` en paper mode :**
```
📊 CopyTrading Status
━━━━━━━━━━━━━━━
🔘 État: 🟢 Actif
🎮 Mode: 📄 PAPER
⚡ Circuit breaker: 🟢 OK (0/3)
👥 Traders suivis: 10
📋 Trades copiés: 7

📄 Positions Paper Trading
━━━━━━━━━━━━━━━
🟢 YES  Will BTC hit $150k before July 2025?…
   $10.00 → $12.40 | 🟢 +$2.40 (+24.0%)
🔴 NO   Will Trump sign crypto bill in Q1?…
   $10.00 → $8.10  | 🔴 -$1.90 (-19.0%)

━━━━━━━━━━━━━━━
💰 Investi (paper): $20.00
💹 Valeur actuelle: $20.50
🟢 P&L total: +$0.50 (+2.5%)
```

---

## 💸 Mode Live

Pour passer en trading réel :

```
/copymode live
```

JARVIS vérifiera que PolyJarvis est correctement configuré avant d'autoriser le switch.

**Chaque trade en mode live :**
- Passe par `PolyJarvisSkill._cmd_buy()` (Market Order FOK)
- Est enregistré dans `config/copy_trades.json`
- Déclenche une notification Telegram instantanée

> ⚠️ Assure-toi d'avoir fait `/polyapprove` au moins une fois pour autoriser les contrats Polymarket.

---

## 🔴 Circuit Breaker

Protection automatique contre les pertes en série.

**Déclenchement :** 3 pertes consécutives en mode live  
**Effet :** arrêt immédiat du copy trading + alerte Telegram  
**Réinitialisation :** `/copyreset` (manuelle)

```
🔴 CIRCUIT BREAKER DÉCLENCHÉ !
━━━━━━━━━━━━━━━
3 pertes consécutives détectées.
Le copy trading a été automatiquement arrêté.

💡 /copyreset pour réinitialiser
💡 /copystatus pour voir le bilan
```

> En mode paper, le circuit breaker ne se déclenche pas (aucun risque réel).

---

## 📁 Fichiers générés

| Fichier | Contenu |
|---|---|
| `config/copy_trades.json` | Historique complet de tous les trades copiés |
| `config/copy_state.json` | État runtime (paper positions, circuit breaker, seen trades) |
| `config/copy_leaders.json` | Snapshot du dernier leaderboard chargé |

### Format `copy_trades.json`
```json
[
  {
    "timestamp":      "2025-02-20 14:35",
    "trader_name":    "CryptoWhale",
    "trader_wallet":  "0xabc123...",
    "cond_id":        "0xbd31dc8a...",
    "market_title":   "Will BTC reach $150k before July 2025?",
    "side":           "YES",
    "entry_price":    0.62,
    "amount_usd":     10.0,
    "paper":          true,
    "result":         "simulated",
    "tx_hash":        "0x1234..."
  }
]
```

---

## 🏗 Architecture

```
skill_copytrading.py
│
├── CopyTradingSkill (BaseSkill)
│   │
│   ├── setup()                  ← Config .env + chargement état
│   ├── handle()                 ← Router des 7 commandes
│   │
│   ├── _main_loop()             ← Boucle asyncio (toutes les N min)
│   │   └── _run_cycle()         ← Un cycle complet
│   │       ├── _fetch_leaderboard()      → Data API + fallback Gamma
│   │       ├── _fetch_trader_activity()  → Trades récents par wallet
│   │       ├── _process_trade()          → Filtre + déduplication
│   │       └── _execute_or_simulate()    → Paper ou Live via PolyJarvis
│   │
│   ├── _handle_loss()           ← Circuit breaker logic
│   ├── _get_current_price()     ← Prix temps réel depuis Gamma
│   └── _notify()                ← Push Telegram proactif
│
├── config/copy_trades.json      ← Log persistant (1000 trades max)
├── config/copy_state.json       ← État runtime
└── config/copy_leaders.json     ← Snapshot leaderboard
```

**APIs utilisées :**
- `https://data-api.polymarket.com/leaderboard` — classement des traders
- `https://data-api.polymarket.com/activity` — activité récente par wallet
- `https://gamma-api.polymarket.com/markets` — prix et métadonnées des marchés
- PolyJarvis skill → `py-clob-client` → CLOB Polymarket (mode live uniquement)

---

## ⚙️ Configuration avancée

### Changer la fenêtre du leaderboard
```bash
# Dans .env
COPY_LEADERBOARD_WINDOW=7d   # Traders performants sur 7 jours
```

| Valeur | Description |
|---|---|
| `1d` | Top traders des dernières 24h (volatil) |
| `7d` | Top traders de la semaine |
| `30d` | Top traders du mois (défaut, plus stable) |
| `all` | All-time leaderboard |

### Réduire la position max
```bash
COPY_MAX_POSITION=5    # 5 USDC max par trade au lieu de 10
```

### Accélérer les cycles (déconseillé < 5 min — rate limits API)
```bash
COPY_INTERVAL_MIN=10   # Cycle toutes les 10 minutes
```

---

## ⚠️ Avertissements

- Le copy trading **ne garantit pas de profit** — les performances passées des top traders ne préjugent pas des performances futures
- Le leaderboard Gamma a un **délai de mise à jour** — les trades copiés auront un léger décalage de prix par rapport aux originaux
- En mode live, chaque trade consomme du **gas POL** (~0.001 POL) en plus des USDC
- Ce projet est **expérimental** — commence toujours par le mode paper avant de passer en live
- Polymarket est soumis à des **restrictions géographiques**

---

## 🔗 Ressources

- [Polymarket](https://polymarket.com)
- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client)
- [Data API Polymarket](https://data-api.polymarket.com)
- [Gamma API Polymarket](https://gamma-api.polymarket.com)
- [README PolyJarvis](./README_polyjarvis.md)
