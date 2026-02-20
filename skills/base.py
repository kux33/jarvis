"""
Classe de base pour toutes les Skills JARVIS.

Pour créer une skill, hérite de BaseSkill et implémente:
  - SKILL_NAME     : identifiant unique (ex: "meteo")
  - SKILL_DESC     : description courte
  - SKILL_COMMANDS : dict {commande: description}
  - setup()        : initialisation optionnelle
  - handle(cmd, args, context) -> str : traitement de la commande
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SkillContext:
    """Contexte passé à chaque skill lors de l'exécution"""
    user_id: int = 0
    username: str = ""
    settings: object = None          # Settings JARVIS
    agent: object = None             # Référence à l'agent (pour mémoire, LLM, etc.)
    extra: dict = field(default_factory=dict)


class BaseSkill(ABC):
    """Classe de base pour toutes les skills JARVIS"""

    # ── À définir dans chaque skill ──────────────────────
    SKILL_NAME: str = ""             # ex: "meteo"
    SKILL_DESC: str = ""             # ex: "Météo en temps réel"
    SKILL_VERSION: str = "1.0.0"
    SKILL_AUTHOR: str = "JARVIS"
    SKILL_COMMANDS: dict = {}        # ex: {"meteo": "Météo d'une ville", "previsions": "Prévisions 5j"}
    SKILL_ENABLED: bool = True
    # ─────────────────────────────────────────────────────

    def __init__(self, settings=None):
        self.settings = settings
        self._ready = False

    async def setup(self) -> bool:
        """
        Initialisation de la skill (appelée au chargement).
        Override pour ajouter ta propre logique d'init.
        Retourne True si la skill est prête, False sinon.
        """
        self._ready = True
        return True

    async def teardown(self):
        """Nettoyage à la désactivation de la skill"""
        self._ready = False

    @abstractmethod
    async def handle(self, command: str, args: str, context: SkillContext) -> str:
        """
        Point d'entrée principal de la skill.
        
        Args:
            command: La commande déclenchée (ex: "meteo")
            args: Arguments de la commande (ex: "Paris")
            context: Contexte d'exécution (user, settings, agent...)
        
        Returns:
            str: Réponse à envoyer à l'utilisateur
        """
        pass

    def get_help(self) -> str:
        """Génère le texte d'aide de la skill"""
        lines = [f"📦 **{self.SKILL_NAME.upper()}** — {self.SKILL_DESC}\n"]
        for cmd, desc in self.SKILL_COMMANDS.items():
            lines.append(f"`/{cmd}` — {desc}")
        return "\n".join(lines)

    @property
    def is_ready(self) -> bool:
        return self._ready

    def __repr__(self):
        return f"<Skill:{self.SKILL_NAME} v{self.SKILL_VERSION}>"
