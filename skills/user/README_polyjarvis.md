# 🎯 PolyJarvis — Skill Polymarket pour JARVIS

> Trade sur Polymarket directement depuis Telegram, tournant sur Raspberry Pi 5.  
> Basé sur la librairie officielle [`py-clob-client`](https://github.com/Polymarket/py-clob-client).

---

## 📦 Installation

### 1. Placer la skill
```bash
cp skill_polyjarvis.py jarvis/skills/user/
```

### 2. Installer les dépendances
```bash
pip install py-clob-client web3 aiohttp
```

### 3. Configurer le `.env`
```bash
# Wallet Polygon
POLY_PRIVATE_KEY=0x...    # Clé privée (ne jamais partager !)
POLY_FUNDER=0x...          # Adresse du wallet qui détient les fonds
POLY_SIG_TYPE=0            # 0=MetaMask/EOA | 1=Magic/email | 2=Browser proxy
```

> **Mode lecture seule** : sans `POLY_PRIVATE_KEY`, la skill fonctionne quand même pour parcourir les marchés, rechercher et scanner les hedges.

### 4. Lancer JARVIS
```bash
source venv/bin/activate
python main.py
```

Tu verras dans les logs :
```
📦 Skill chargée: polyjarvis v1.0.0 (10 cmd)
```

---

## 🚀 Première utilisation

Dans Telegram, dans cet ordre :

```
/skill list       → vérifier que polyjarvis est chargée
/polywallet       → vérifier tes soldes POL + USDC.e
/polyapprove      → approuver les contrats (une seule fois)
/polymarkets      → explorer les marchés
```

---

## 📱 Commandes

### 🔍 Explorer les marchés

| Commande | Description | Exemple |
|---|---|---|
| `/polymarkets` | Top 10 marchés par volume 24h | `/polymarkets` |
| `/polymarkets 20` | Top N marchés | `/polymarkets 20` |
| `/polysearch <mots>` | Recherche par mots-clés | `/polysearch bitcoin ETF` |
| `/polymarket <condID>` | Détail complet d'un marché | `/polymarket 0xabc…` |

**Exemple de sortie `/polymarkets` :**
```
📈 Marchés Polymarket — Top 10 par volume 24h
━━━━━━━━━━━━━━━
1. Will Bitcoin reach $100k in 2025?
   🟢 YES 0.67$ | 🔴 NO 0.33$
   📊 Vol24h: $1,234,567 | Liq: $456,789
   🔑 0xbd31dc8a…
```

---

### 💸 Trader

| Commande | Description | Exemple |
|---|---|---|
| `/polybuy <condID> YES <$>` | Acheter une position YES | `/polybuy 0xabc… YES 50` |
| `/polybuy <condID> NO <$>` | Acheter une position NO | `/polybuy 0xabc… NO 25` |

Les ordres sont passés en **Market Order FOK** (Fill or Kill) via le CLOB Polymarket.

**Exemple :**
```
/polybuy 0xbd31dc8a YES 50

🛒 Récapitulatif de l'ordre
━━━━━━━━━━━━━━━
🎯 Côté: YES
💵 Montant: $50.00 USDC
💹 Prix actuel: 0.6700$ (67.0%)
📦 Shares estimées: ~74.63

✅ Ordre exécuté !
📋 Order ID: 0x1234…
```

---

### 💼 Suivre ses positions

| Commande | Description |
|---|---|
| `/polypositions` | Positions ouvertes avec P&L en temps réel |

Les positions sont stockées localement dans `config/poly_positions.json`.

**Exemple de sortie :**
```
💼 Positions ouvertes PolyJarvis
━━━━━━━━━━━━━━━
📌 0xbd31dc8a… | YES
   💰 Investi: $50.00 | Entrée: 0.6700$
   💹 Actuel: 0.7200$ | Valeur: $53.73
   🟢 P&L: +$3.73 (+7.5%)

━━━━━━━━━━━━━━━
📊 Total investi: $50.00
📊 Valeur actuelle: $53.73
🟢 P&L total: +$3.73 (+7.5%)
```

---

### 🔎 Scanner les hedges

| Commande | Description |
|---|---|
| `/polyhedge` | Analyse les 50 top marchés et détecte les anomalies de prix |

Le scanner détecte les marchés où `prix YES + prix NO ≠ 1.00` de façon significative, ce qui peut représenter des opportunités d'arbitrage ou de hedge. Les résultats sont ensuite analysés par le LLM actif de JARVIS.

**Niveaux d'opportunité :**

| Niveau | Écart | Icône |
|---|---|---|
| T1 — Critique | ≥ 15% | 🔥 |
| T2 — Fort     | ≥ 8%  | ⭐ |
| T3 — Modéré   | ≥ 5%  | 💡 |
| T4 — Faible   | < 5%  | 📌 |

---

### 💰 Gestion du wallet

| Commande | Description |
|---|---|
| `/polywallet` | Soldes POL (gas) + USDC.e + état des approbations |
| `/polyapprove` | Approuver les 3 contrats Polymarket (one-time) |

**Contrats approuvés par `/polyapprove` :**
- `0x4bFb41d5…` — Exchange principal
- `0xC5d563A3…` — Neg-Risk Exchange
- `0xd91E80cF…` — Neg-Risk Adapter

> Coût : ~0.01 POL en gas. À faire **une seule fois** par wallet.

---

### 📋 Gestion des ordres

| Commande | Description | Exemple |
|---|---|---|
| `/polyorders` | Lister les ordres ouverts sur le CLOB | `/polyorders` |
| `/polycancel <orderID>` | Annuler un ordre | `/polycancel 0x1234…` |

---

## 🏗 Architecture

```
skill_polyjarvis.py
│
├── PolyJarvisSkill          ← Classe principale (BaseSkill)
│   ├── setup()              ← Init ClobClient + Web3
│   ├── handle()             ← Router des commandes
│   │
│   ├── _cmd_markets()       ← Gamma API /markets (volume sort)
│   ├── _cmd_search()        ← Gamma API /markets (query search)
│   ├── _cmd_market_detail() ← Gamma + CLOB orderbook
│   ├── _cmd_buy()           ← MarketOrderArgs FOK via ClobClient
│   ├── _cmd_positions()     ← JSON local + prix CLOB temps réel
│   ├── _cmd_hedge()         ← Analyse prix + LLM JARVIS
│   ├── _cmd_wallet()        ← Web3 balanceOf + allowance
│   ├── _cmd_approve()       ← Web3 approve + setApprovalForAll
│   ├── _cmd_orders()        ← ClobClient get_orders()
│   └── _cmd_cancel()        ← ClobClient cancel()
│
├── _get_token_price()       ← Extrait prix YES/NO depuis Gamma
├── _get_token_id()          ← Résout conditionID → tokenID
├── _record_position()       ← Sauvegarde JSON locale
└── _hedge_tier()            ← Classement T1→T4
```

**APIs utilisées :**
- `https://gamma-api.polymarket.com` — marchés, prix, métadonnées
- `https://clob.polymarket.com` — orderbook, passage d'ordres, trades
- `https://polygon-rpc.com` — soldes on-chain, approbations

---

## ⚙️ Variables d'environnement

| Variable | Requis | Description |
|---|---|---|
| `POLY_PRIVATE_KEY` | Trade uniquement | Clé privée du wallet Polygon |
| `POLY_FUNDER` | Trade uniquement | Adresse qui détient les fonds |
| `POLY_SIG_TYPE` | Non (défaut: `0`) | Type de signature (0/1/2) |

---

## ⚠️ Avertissements

- **Ne jamais partager** ta `POLY_PRIVATE_KEY` — elle donne accès total à ton wallet
- Les ordres FOK sont **irréversibles** une fois exécutés
- Le P&L affiché est **indicatif** — basé sur le midpoint CLOB, pas le prix de sortie réel
- Le hedge scanner détecte des anomalies de prix mais **ne garantit pas de profit**
- Polymarket est soumis à des **restrictions géographiques** (US notamment)
- Ce projet est **expérimental** — ne pas investir plus que ce qu'on peut perdre

---

## 🔗 Ressources

- [Polymarket](https://polymarket.com)
- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client)
- [Documentation CLOB](https://docs.polymarket.com/developers/CLOB/introduction)
- [Gamma API](https://docs.polymarket.com/developers/gamma-markets-api/overview)
- [Polygonscan](https://polygonscan.com) — explorer les transactions
