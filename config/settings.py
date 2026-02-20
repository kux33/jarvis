"""
Configuration centralisée de JARVIS
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path
import json


@dataclass
class Settings:
    # ──────────────────────────────
    # LLM Configuration
    # ──────────────────────────────
    active_llm: str = os.getenv("JARVIS_LLM", "claude")  # claude | grok | openai | kimi

    # Clés API
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    grok_api_key: str = os.getenv("GROK_API_KEY", "")
    kimi_api_key: str = os.getenv("KIMI_API_KEY", "")

    # Modèles par défaut
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-opus-4-6")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    grok_model: str = os.getenv("GROK_MODEL", "grok-3")
    kimi_model: str = os.getenv("KIMI_MODEL", "moonshot-v1-8k")

    # ──────────────────────────────
    # Telegram
    # ──────────────────────────────
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_allowed_users: list = field(default_factory=lambda: [
        int(uid) for uid in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",") if uid
    ])

    # ──────────────────────────────
    # Agent Behavior
    # ──────────────────────────────
    jarvis_name: str = "JARVIS"
    system_prompt: str = """Tu es JARVIS, un assistant IA avancé tournant sur Raspberry Pi 5.
Tu es intelligent, efficace et légèrement sarcastique comme le JARVIS de Iron Man.
Tu peux exécuter des tâches système, analyser des données, et interagir avec l'environnement.
Réponds de façon concise et pertinente. Tu parles en français par défaut."""
    
    max_tokens: int = 2048
    temperature: float = 0.7
    memory_max_messages: int = 20  # Historique de conversation

    # ──────────────────────────────
    # Raspberry Pi / Hardware
    # ──────────────────────────────
    enable_gpio: bool = os.getenv("ENABLE_GPIO", "false").lower() == "true"
    enable_camera: bool = os.getenv("ENABLE_CAMERA", "false").lower() == "true"
    enable_audio: bool = os.getenv("ENABLE_AUDIO", "false").lower() == "true"

    # ──────────────────────────────
    # Tools / Capabilities
    # ──────────────────────────────
    enable_web_search: bool = True
    enable_shell: bool = os.getenv("ENABLE_SHELL", "false").lower() == "true"  # ⚠️ Sécurité
    enable_file_ops: bool = True

    def validate(self):
        """Vérifie que la config est valide"""
        errors = []
        if not self.telegram_token:
            errors.append("TELEGRAM_BOT_TOKEN manquant")
        
        api_keys = {
            "claude": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "grok": self.grok_api_key,
            "kimi": self.kimi_api_key,
        }
        if not api_keys.get(self.active_llm):
            errors.append(f"Clé API manquante pour {self.active_llm.upper()}")
        
        if errors:
            raise ValueError("Erreurs de configuration:\n" + "\n".join(f"  - {e}" for e in errors))
        
        return True

    def save_state(self, path: str = "config/state.json"):
        """Sauvegarde l'état runtime"""
        state = {"active_llm": self.active_llm}
        Path(path).parent.mkdir(exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f)

    def load_state(self, path: str = "config/state.json"):
        """Charge l'état runtime"""
        if Path(path).exists():
            with open(path) as f:
                state = json.load(f)
                self.active_llm = state.get("active_llm", self.active_llm)
