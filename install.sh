#!/bin/bash
# ╔══════════════════════════════════════════╗
# ║     Script d'installation JARVIS         ║
# ║     Raspberry Pi 5 - Ubuntu/Raspberry OS ║
# ╚══════════════════════════════════════════╝

set -e

JARVIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$JARVIS_DIR/venv"

echo "🤖 Installation de JARVIS..."
echo "📁 Dossier: $JARVIS_DIR"

# ──────────────────────────────
# Vérification Python 3.11+
# ──────────────────────────────
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

echo "🐍 Python détecté: $PYTHON_VERSION"

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]); then
    echo "❌ Python 3.11+ requis. Actuel: $PYTHON_VERSION"
    echo "   sudo apt install python3.11 python3.11-venv"
    exit 1
fi

# ──────────────────────────────
# Dépendances système
# ──────────────────────────────
echo "📦 Installation des dépendances système..."
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip libopenblas-dev

# ──────────────────────────────
# Environnement virtuel
# ──────────────────────────────
echo "🔧 Création de l'environnement virtuel..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

# ──────────────────────────────
# Installation des packages
# ──────────────────────────────
echo "📚 Installation des packages Python..."
pip install --upgrade pip -q
pip install -r "$JARVIS_DIR/requirements.txt" -q

echo "✅ Packages installés !"

# ──────────────────────────────
# Configuration .env
# ──────────────────────────────
if [ ! -f "$JARVIS_DIR/.env" ]; then
    cp "$JARVIS_DIR/.env.example" "$JARVIS_DIR/.env"
    echo ""
    echo "⚠️  CONFIGURATION REQUISE:"
    echo "   Édite le fichier: $JARVIS_DIR/.env"
    echo "   et renseigne tes clés API et token Telegram"
    echo ""
fi

# ──────────────────────────────
# Création des dossiers
# ──────────────────────────────
mkdir -p "$JARVIS_DIR/logs"
mkdir -p "$JARVIS_DIR/config"

# ──────────────────────────────
# Service systemd (optionnel)
# ──────────────────────────────
read -p "🚀 Installer comme service systemd (démarrage auto) ? [y/N] " install_service
if [[ "$install_service" =~ ^[Yy]$ ]]; then
    # Adapter le chemin dans le service
    sed "s|/home/pi/jarvis|$JARVIS_DIR|g" "$JARVIS_DIR/jarvis.service" | \
    sed "s|User=pi|User=$(whoami)|g" > /tmp/jarvis.service
    
    sudo cp /tmp/jarvis.service /etc/systemd/system/jarvis.service
    sudo systemctl daemon-reload
    sudo systemctl enable jarvis
    echo "✅ Service installé ! Démarre avec: sudo systemctl start jarvis"
fi

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     ✅ JARVIS installé avec succès !     ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "1. Configure ton .env avec tes clés API"
echo "2. Lance JARVIS: source venv/bin/activate && python main.py"
echo ""
