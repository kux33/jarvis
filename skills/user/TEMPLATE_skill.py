"""
╔══════════════════════════════════════════════╗
║     Template de Skill JARVIS                 ║
║     Copie ce fichier et renomme-le           ║
║     skill_<tonnom>.py dans skills/user/      ║
╚══════════════════════════════════════════════╝

INSTRUCTIONS:
1. Copie ce fichier dans skills/user/skill_MASKI LL.py
2. Remplis les champs SKILL_*
3. Ajoute tes commandes dans SKILL_COMMANDS
4. Implémente la méthode handle()
5. Redémarre JARVIS ou utilise /skill reload <nom>

Sans redémarrage: place le fichier dans skills/user/
JARVIS détecte automatiquement les nouvelles skills.
"""

from skills.base import BaseSkill, SkillContext


class MaSkillTemplate(BaseSkill):
    # ─── Obligatoire ───────────────────────────────
    SKILL_NAME = "meskill"           # Identifiant unique, minuscule
    SKILL_DESC = "Description courte de ma skill"
    # ───────────────────────────────────────────────

    SKILL_VERSION = "1.0.0"
    SKILL_AUTHOR = "Ton Nom"

    SKILL_COMMANDS = {
        # Format: "commande": "Description affichée dans /help"
        "macommande":  "Fait quelque chose (`/macommande argument`)",
        "autrecmd":    "Autre action",
    }

    def __init__(self, settings=None):
        super().__init__(settings)
        # Initialise ici tes variables d'instance

    async def setup(self) -> bool:
        """
        Appelé au chargement de la skill.
        Retourne True si tout va bien, False pour désactiver la skill.
        """
        # Ex: vérifier une clé API, une dépendance, etc.
        # api_key = self.settings.extra.get("MA_CLE_API")
        # if not api_key:
        #     return False  # La skill ne se charge pas

        self._ready = True
        return True

    async def handle(self, command: str, args: str, context: SkillContext) -> str:
        """
        Point d'entrée. Appelé quand l'utilisateur tape /macommande.

        Args:
            command (str): La commande sans le slash, ex: "macommande"
            args (str):    Le reste du message, ex: "mon argument"
            context:       Contexte (user_id, settings, agent...)

        Returns:
            str: Texte à envoyer dans Telegram (Markdown OK)
        """

        if command == "macommande":
            return await self._handle_macommande(args, context)

        elif command == "autrecmd":
            return "🎉 Autre commande exécutée !"

        return "❓ Commande inconnue."

    # ─── Méthodes privées ──────────────────────────

    async def _handle_macommande(self, args: str, context: SkillContext) -> str:
        if not args:
            return "Usage: `/macommande <argument>`"

        # Accès à l'agent JARVIS (LLM, mémoire...)
        # llm = context.agent.llm_router.get()
        # reponse = await llm.chat([{"role": "user", "content": args}])

        # Accès aux settings
        # api_key = self.settings.anthropic_api_key

        return f"✅ Tu as tapé: `{args}`\nUtilisateur: {context.username}"
