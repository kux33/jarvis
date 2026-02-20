"""
Router Multi-LLM : Claude, Grok, OpenAI, Kimi
"""

import logging
from typing import Optional, AsyncGenerator
from abc import ABC, abstractmethod

logger = logging.getLogger("Jarvis.LLM")


# ──────────────────────────────────────────
# Interface de base
# ──────────────────────────────────────────

class BaseLLM(ABC):
    def __init__(self, settings):
        self.settings = settings

    @abstractmethod
    async def chat(self, messages: list, stream: bool = False) -> str:
        pass

    @abstractmethod
    def get_name(self) -> str:
        pass


# ──────────────────────────────────────────
# Claude (Anthropic)
# ──────────────────────────────────────────

class ClaudeLLM(BaseLLM):
    def __init__(self, settings):
        super().__init__(settings)
        try:
            import anthropic
            self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        except ImportError:
            raise ImportError("pip install anthropic")

    def get_name(self) -> str:
        return f"Claude ({self.settings.claude_model})"

    async def chat(self, messages: list, stream: bool = False) -> str:
        # Séparer le system prompt
        system = self.settings.system_prompt
        user_messages = [m for m in messages if m["role"] != "system"]

        response = await self.client.messages.create(
            model=self.settings.claude_model,
            max_tokens=self.settings.max_tokens,
            system=system,
            messages=user_messages,
        )
        return response.content[0].text


# ──────────────────────────────────────────
# OpenAI (GPT)
# ──────────────────────────────────────────

class OpenAILLM(BaseLLM):
    def __init__(self, settings):
        super().__init__(settings)
        try:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        except ImportError:
            raise ImportError("pip install openai")

    def get_name(self) -> str:
        return f"OpenAI ({self.settings.openai_model})"

    async def chat(self, messages: list, stream: bool = False) -> str:
        full_messages = [{"role": "system", "content": self.settings.system_prompt}] + messages
        response = await self.client.chat.completions.create(
            model=self.settings.openai_model,
            messages=full_messages,
            max_tokens=self.settings.max_tokens,
            temperature=self.settings.temperature,
        )
        return response.choices[0].message.content


# ──────────────────────────────────────────
# Grok (xAI)
# ──────────────────────────────────────────

class GrokLLM(BaseLLM):
    def __init__(self, settings):
        super().__init__(settings)
        try:
            from openai import AsyncOpenAI
            # Grok utilise l'API compatible OpenAI
            self.client = AsyncOpenAI(
                api_key=settings.grok_api_key,
                base_url="https://api.x.ai/v1"
            )
        except ImportError:
            raise ImportError("pip install openai")

    def get_name(self) -> str:
        return f"Grok ({self.settings.grok_model})"

    async def chat(self, messages: list, stream: bool = False) -> str:
        full_messages = [{"role": "system", "content": self.settings.system_prompt}] + messages
        response = await self.client.chat.completions.create(
            model=self.settings.grok_model,
            messages=full_messages,
            max_tokens=self.settings.max_tokens,
            temperature=self.settings.temperature,
        )
        return response.choices[0].message.content


# ──────────────────────────────────────────
# Kimi (Moonshot AI)
# ──────────────────────────────────────────

class KimiLLM(BaseLLM):
    def __init__(self, settings):
        super().__init__(settings)
        try:
            from openai import AsyncOpenAI
            # Kimi utilise aussi l'API compatible OpenAI
            self.client = AsyncOpenAI(
                api_key=settings.kimi_api_key,
                base_url="https://api.moonshot.cn/v1"
            )
        except ImportError:
            raise ImportError("pip install openai")

    def get_name(self) -> str:
        return f"Kimi ({self.settings.kimi_model})"

    async def chat(self, messages: list, stream: bool = False) -> str:
        full_messages = [{"role": "system", "content": self.settings.system_prompt}] + messages
        response = await self.client.chat.completions.create(
            model=self.settings.kimi_model,
            messages=full_messages,
            max_tokens=self.settings.max_tokens,
            temperature=self.settings.temperature,
        )
        return response.choices[0].message.content


# ──────────────────────────────────────────
# Factory / Router
# ──────────────────────────────────────────

class LLMRouter:
    PROVIDERS = {
        "claude": ClaudeLLM,
        "openai": OpenAILLM,
        "grok": GrokLLM,
        "kimi": KimiLLM,
    }

    def __init__(self, settings):
        self.settings = settings
        self._instances = {}

    def get(self, provider: Optional[str] = None) -> BaseLLM:
        """Retourne l'instance LLM pour le provider donné"""
        provider = provider or self.settings.active_llm
        provider = provider.lower()

        if provider not in self.PROVIDERS:
            raise ValueError(f"Provider inconnu: {provider}. Disponibles: {list(self.PROVIDERS.keys())}")

        if provider not in self._instances:
            logger.info(f"🔌 Initialisation LLM: {provider}")
            self._instances[provider] = self.PROVIDERS[provider](self.settings)

        return self._instances[provider]

    def switch(self, provider: str):
        """Change le LLM actif"""
        provider = provider.lower()
        if provider not in self.PROVIDERS:
            raise ValueError(f"Provider inconnu: {provider}")
        self.settings.active_llm = provider
        self.settings.save_state()
        logger.info(f"🔄 Changement LLM → {provider}")
        return self.get(provider)

    def available(self) -> list:
        return list(self.PROVIDERS.keys())
