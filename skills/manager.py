"""
JARVIS Skill Manager
Chargement dynamique, activation/désactivation et routage des skills.
"""

import importlib
import importlib.util
import inspect
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from skills.base import BaseSkill, SkillContext

logger = logging.getLogger("Jarvis.SkillManager")

SKILLS_DIR = Path(__file__).parent / "builtin"
USER_SKILLS_DIR = Path(__file__).parent / "user"
STATE_FILE = Path("config/skills_state.json")


class SkillManager:
    """Gestionnaire de skills JARVIS"""

    def __init__(self, settings, agent=None):
        self.settings = settings
        self.agent = agent
        self._skills: dict[str, BaseSkill] = {}          # name -> instance
        self._command_map: dict[str, str] = {}            # command -> skill_name
        self._disabled: set[str] = set()                  # skills désactivées

        # Créer les dossiers si besoin
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        self._load_state()

    # ──────────────────────────────────────────
    # Chargement
    # ──────────────────────────────────────────

    async def load_all(self):
        """Charge toutes les skills disponibles"""
        count = 0
        for directory in (SKILLS_DIR, USER_SKILLS_DIR):
            for skill_file in sorted(directory.glob("skill_*.py")):
                if await self._load_file(skill_file):
                    count += 1
        logger.info(f"✅ {count} skill(s) chargée(s) | {len(self._command_map)} commande(s) disponibles")

    async def _load_file(self, path: Path) -> bool:
        """Charge une skill depuis un fichier .py"""
        try:
            module_name = f"skills.{path.stem}"
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # Trouver la classe skill dans le module
            skill_class = None
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BaseSkill) and obj is not BaseSkill and obj.SKILL_NAME:
                    skill_class = obj
                    break

            if not skill_class:
                logger.warning(f"Aucune skill valide dans {path.name}")
                return False

            return await self._register(skill_class, path)

        except Exception as e:
            logger.error(f"Erreur chargement {path.name}: {e}")
            return False

    async def _register(self, skill_class: type, path: Path) -> bool:
        """Instancie et enregistre une skill"""
        try:
            instance = skill_class(self.settings)
            name = instance.SKILL_NAME

            # Désactivée ?
            if name in self._disabled:
                logger.info(f"⏸ Skill désactivée: {name}")
                self._skills[name] = instance
                return True

            # Setup
            ok = await instance.setup()
            if not ok:
                logger.warning(f"⚠️ Setup échoué: {name}")
                return False

            # Enregistrement
            self._skills[name] = instance
            for cmd in instance.SKILL_COMMANDS:
                self._command_map[cmd.lower()] = name
                logger.debug(f"  /{cmd} → {name}")

            logger.info(f"📦 Skill chargée: {name} v{instance.SKILL_VERSION} ({len(instance.SKILL_COMMANDS)} cmd)")
            return True

        except Exception as e:
            logger.error(f"Erreur enregistrement skill: {e}")
            return False

    async def reload_skill(self, name: str) -> str:
        """Recharge une skill à chaud (sans redémarrer JARVIS)"""
        name = name.lower()
        if name not in self._skills:
            return f"❌ Skill inconnue: `{name}`"

        skill = self._skills[name]
        # Trouver le fichier source
        for directory in (SKILLS_DIR, USER_SKILLS_DIR):
            skill_file = directory / f"skill_{name}.py"
            if skill_file.exists():
                # Teardown
                await skill.teardown()
                # Supprimer de la map
                self._command_map = {k: v for k, v in self._command_map.items() if v != name}
                del self._skills[name]
                # Recharger
                if await self._load_file(skill_file):
                    return f"🔄 Skill `{name}` rechargée avec succès !"
                return f"❌ Erreur lors du rechargement de `{name}`"

        return f"❌ Fichier source introuvable pour `{name}`"

    # ──────────────────────────────────────────
    # Routage des commandes
    # ──────────────────────────────────────────

    def has_command(self, command: str) -> bool:
        """Vérifie si une commande est gérée par une skill"""
        return command.lower().lstrip("/") in self._command_map

    async def dispatch(self, command: str, args: str, context: SkillContext) -> Optional[str]:
        """Dispatche une commande vers la skill appropriée"""
        cmd = command.lower().lstrip("/")
        skill_name = self._command_map.get(cmd)

        if not skill_name:
            return None

        skill = self._skills.get(skill_name)
        if not skill or not skill.is_ready:
            return f"⚠️ Skill `{skill_name}` non disponible."

        if skill_name in self._disabled:
            return f"⏸ Skill `{skill_name}` est désactivée. (`/skill on {skill_name}` pour réactiver)"

        try:
            logger.info(f"🔧 Dispatch /{cmd} → {skill_name} | args: {args!r}")
            return await skill.handle(cmd, args, context)
        except Exception as e:
            logger.error(f"Erreur skill {skill_name}: {e}")
            return f"❌ Erreur dans la skill `{skill_name}`: {e}"

    # ──────────────────────────────────────────
    # Gestion (enable / disable / list)
    # ──────────────────────────────────────────

    async def enable(self, name: str) -> str:
        name = name.lower()
        if name not in self._skills:
            return f"❌ Skill inconnue: `{name}`"
        if name not in self._disabled:
            return f"ℹ️ Skill `{name}` déjà active."
        self._disabled.discard(name)
        # Réenregistrer les commandes
        skill = self._skills[name]
        await skill.setup()
        for cmd in skill.SKILL_COMMANDS:
            self._command_map[cmd.lower()] = name
        self._save_state()
        return f"✅ Skill `{name}` activée !"

    async def disable(self, name: str) -> str:
        name = name.lower()
        if name not in self._skills:
            return f"❌ Skill inconnue: `{name}`"
        if name in self._disabled:
            return f"ℹ️ Skill `{name}` déjà désactivée."
        self._disabled.add(name)
        await self._skills[name].teardown()
        self._command_map = {k: v for k, v in self._command_map.items() if v != name}
        self._save_state()
        return f"⏸ Skill `{name}` désactivée."

    def list_skills(self) -> str:
        if not self._skills:
            return "📭 Aucune skill installée.\n\nPlace tes fichiers `skill_xxx.py` dans `skills/user/`"

        lines = ["📦 **Skills JARVIS**\n━━━━━━━━━━━━━━━"]
        for name, skill in sorted(self._skills.items()):
            status = "⏸" if name in self._disabled else "✅"
            cmds = " ".join(f"`/{c}`" for c in skill.SKILL_COMMANDS)
            lines.append(f"{status} **{name}** v{skill.SKILL_VERSION} — {skill.SKILL_DESC}\n   {cmds}")

        lines.append(f"\n💡 `/skill help <nom>` pour l'aide d'une skill")
        lines.append(f"💡 `/skill reload <nom>` pour recharger à chaud")
        return "\n".join(lines)

    def skill_help(self, name: str) -> str:
        name = name.lower()
        if name not in self._skills:
            return f"❌ Skill inconnue: `{name}`"
        return self._skills[name].get_help()

    # ──────────────────────────────────────────
    # Persistance de l'état
    # ──────────────────────────────────────────

    def _save_state(self):
        STATE_FILE.parent.mkdir(exist_ok=True)
        try:
            data = {}
            if STATE_FILE.exists():
                with open(STATE_FILE) as f:
                    data = json.load(f)
            data["disabled_skills"] = list(self._disabled)
            with open(STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Erreur sauvegarde état skills: {e}")

    def _load_state(self):
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE) as f:
                    data = json.load(f)
                self._disabled = set(data.get("disabled_skills", []))
        except Exception as e:
            logger.error(f"Erreur chargement état skills: {e}")
