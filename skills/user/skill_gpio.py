"""
Skill JARVIS : Contrôle GPIO Raspberry Pi
⚠️ Nécessite: pip install RPi.GPIO
               et ENABLE_GPIO=true dans .env

Commandes: /gpio, /gpioread
"""

import logging
from skills.base import BaseSkill, SkillContext

logger = logging.getLogger("Jarvis.Skill.GPIO")


class GpioSkill(BaseSkill):
    SKILL_NAME = "gpio"
    SKILL_DESC = "Contrôle GPIO Raspberry Pi"
    SKILL_VERSION = "1.0.0"
    SKILL_AUTHOR = "JARVIS"
    SKILL_COMMANDS = {
        "gpio":     "Contrôler un pin GPIO (`/gpio 17 on` ou `/gpio 17 off`)",
        "gpioread": "Lire un pin GPIO (`/gpioread 18`)",
        "gpiolist": "Lister les pins configurés",
    }

    # Pins configurés : {numéro_pin: {"name": str, "mode": "OUT"|"IN", "state": bool}}
    PINS_CONFIG = {
        17: {"name": "LED rouge", "mode": "OUT", "state": False},
        18: {"name": "Capteur PIR", "mode": "IN",  "state": None},
        27: {"name": "Relais 1",   "mode": "OUT", "state": False},
    }

    def __init__(self, settings=None):
        super().__init__(settings)
        self._gpio = None
        self._pin_states = {}

    async def setup(self) -> bool:
        if not self.settings or not self.settings.enable_gpio:
            logger.info("GPIO désactivé (ENABLE_GPIO=false)")
            self._ready = False
            return False

        try:
            import RPi.GPIO as GPIO
            self._gpio = GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)

            for pin, cfg in self.PINS_CONFIG.items():
                if cfg["mode"] == "OUT":
                    GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
                else:
                    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
                self._pin_states[pin] = cfg.copy()

            logger.info(f"✅ GPIO initialisé ({len(self.PINS_CONFIG)} pins)")
            self._ready = True
            return True

        except ImportError:
            logger.warning("RPi.GPIO non installé. `pip install RPi.GPIO`")
            self._ready = False
            return False
        except Exception as e:
            logger.error(f"Erreur init GPIO: {e}")
            self._ready = False
            return False

    async def teardown(self):
        if self._gpio:
            self._gpio.cleanup()
        self._ready = False

    async def handle(self, command: str, args: str, context: SkillContext) -> str:
        if not self._gpio:
            return "❌ GPIO non disponible (vérifier ENABLE_GPIO=true et RPi.GPIO installé)"

        if command == "gpio":
            return self._set_pin(args)
        elif command == "gpioread":
            return self._read_pin(args.strip())
        elif command == "gpiolist":
            return self._list_pins()
        return ""

    def _set_pin(self, args: str) -> str:
        parts = args.strip().split()
        if len(parts) < 2:
            return "Usage: `/gpio <pin> on|off`\nEx: `/gpio 17 on`"

        try:
            pin = int(parts[0])
            action = parts[1].lower()
        except ValueError:
            return "❌ Numéro de pin invalide."

        if pin not in self._pin_states:
            return f"❌ Pin {pin} non configuré. Pins disponibles: {list(self._pin_states.keys())}"

        cfg = self._pin_states[pin]
        if cfg["mode"] != "OUT":
            return f"❌ Pin {pin} est en mode entrée (IN)."

        if action in ("on", "1", "high"):
            self._gpio.output(pin, self._gpio.HIGH)
            cfg["state"] = True
            return f"✅ Pin {pin} ({cfg['name']}) → **ON** 💡"
        elif action in ("off", "0", "low"):
            self._gpio.output(pin, self._gpio.LOW)
            cfg["state"] = False
            return f"✅ Pin {pin} ({cfg['name']}) → **OFF**"
        else:
            return "❌ Action invalide. Utilise `on` ou `off`."

    def _read_pin(self, pin_str: str) -> str:
        try:
            pin = int(pin_str)
        except ValueError:
            return "Usage: `/gpioread <pin>`"

        if pin not in self._pin_states:
            return f"❌ Pin {pin} non configuré."

        value = self._gpio.input(pin)
        name = self._pin_states[pin]["name"]
        state = "HIGH ✅" if value else "LOW ⬜"
        return f"📡 Pin {pin} ({name}): **{state}**"

    def _list_pins(self) -> str:
        lines = ["🔌 **Pins GPIO configurés**\n━━━━━━━━━━━━━━━"]
        for pin, cfg in self._pin_states.items():
            mode_icon = "📤" if cfg["mode"] == "OUT" else "📥"
            state = "ON" if cfg.get("state") else "OFF"
            lines.append(f"{mode_icon} Pin **{pin}** — {cfg['name']} ({cfg['mode']}) → {state}")
        return "\n".join(lines)
