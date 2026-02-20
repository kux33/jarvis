# 🤖 JARVIS — Agent IA Multi-LLM

> *"Just A Rather Very Intelligent System"*  
> Compatible **Raspberry Pi 5** | Piloté via **Telegram** | Multi-LLM

---

## ✨ Fonctionnalités

- 🧠 **4 LLMs supportés** : Claude, OpenAI GPT, Grok (xAI), Kimi (Moonshot)
- 🔄 **Changement de LLM à la volée** via Telegram
- 💬 **Mémoire de conversation** avec fenêtre glissante
- 📱 **Pilotage complet via Telegram**
- 🖥 **Monitoring système** (CPU, RAM, température Raspberry Pi)
- 🔒 **Whitelist utilisateurs** Telegram
- 🚀 **Service systemd** pour démarrage automatique
- ⚡ **Optimisé ARM64** pour Raspberry Pi 5

---

## 🚀 Installation (Raspberry Pi 5)

```bash
# 1. Cloner/copier le projet
cd /home/pi
git clone <votre-repo> jarvis
cd jarvis

# 2. Lancer l'installation
chmod +x install.sh
./install.sh

# 3. Configurer
cp .env.example .env
nano .env  # Remplir les clés API et token Telegram

# 4. Lancer
source venv/bin/activate
python main.py
```

---

## ⚙️ Configuration (.env)

| Variable | Description |
|---|---|
| `JARVIS_LLM` | LLM par défaut : `claude` / `openai` / `grok` / `kimi` |
| `ANTHROPIC_API_KEY` | Clé API Anthropic (Claude) |
| `OPENAI_API_KEY` | Clé API OpenAI |
| `GROK_API_KEY` | Clé API xAI (Grok) |
| `KIMI_API_KEY` | Clé API Moonshot (Kimi) |
| `TELEGRAM_BOT_TOKEN` | Token obtenu via @BotFather |
| `TELEGRAM_ALLOWED_USERS` | IDs Telegram autorisés (vide = tous) |
| `ENABLE_SHELL` | Permet les commandes shell (`false` par défaut) |
| `ENABLE_GPIO` | Active le GPIO Raspberry Pi |

---

## 📱 Commandes Telegram

| Commande | Description |
|---|---|
| `/start` | Démarrer JARVIS |
| `/help` | Afficher l'aide |
| `/status` | Statut de l'agent |
| `/sysinfo` | CPU, RAM, température |
| `/llm` | Voir le LLM actif |
| `/llm claude` | Passer sur Claude |
| `/llm openai` | Passer sur OpenAI |
| `/llm grok` | Passer sur Grok |
| `/llm kimi` | Passer sur Kimi |
| `/memory` | Voir l'historique |
| `/clear` | Effacer la mémoire |
| `/shell <cmd>` | Shell (si activé) |
| *texte libre* | Conversation avec JARVIS |

---

## 🏗 Architecture

```
jarvis/
├── main.py              # Point d'entrée
├── config/
│   └── settings.py      # Configuration centralisée
├── core/
│   └── agent.py         # Logique principale + mémoire
├── llm/
│   └── router.py        # Router multi-LLM (Claude/OpenAI/Grok/Kimi)
├── telegram/
│   └── bot.py           # Interface Telegram
├── tools/               # Outils extensibles
├── requirements.txt
├── .env.example
├── install.sh           # Script d'installation Pi
└── jarvis.service       # Service systemd
```

---

## 🔧 Service systemd (démarrage auto)

```bash
# Installer le service
sudo cp jarvis.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable jarvis
sudo systemctl start jarvis

# Voir les logs
journalctl -u jarvis -f
```

---

## 🛡 Sécurité

- La whitelist Telegram (`TELEGRAM_ALLOWED_USERS`) est **recommandée**
- Le mode shell (`ENABLE_SHELL`) est **désactivé par défaut**
- Les clés API sont chargées via `.env` (jamais dans le code)

---

## 📡 Obtenir les clés API

- **Claude** : [console.anthropic.com](https://console.anthropic.com)
- **OpenAI** : [platform.openai.com](https://platform.openai.com)
- **Grok** : [console.x.ai](https://console.x.ai)
- **Kimi** : [platform.moonshot.cn](https://platform.moonshot.cn)
- **Telegram Bot** : Écrire à [@BotFather](https://t.me/BotFather)
- **Votre ID Telegram** : Écrire à [@userinfobot](https://t.me/userinfobot)
