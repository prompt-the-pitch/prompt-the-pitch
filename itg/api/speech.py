"""Spoken commentary with Azure AI Speech neural voices, one per language.

The Foundry resource is a multi-service Azure AI resource, so the same Microsoft Entra
identity that calls the model also calls text to speech: no key, no extra resource.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import httpx

from itg.agents.llm import LLM

VOICES = {"en": ("en-GB", "en-GB-RyanNeural"), "fr": ("fr-FR", "fr-FR-HenriNeural"),
          "es": ("es-ES", "es-ES-AlvaroNeural"), "pt": ("pt-PT", "pt-PT-DuarteNeural")}
SCOPE = "https://cognitiveservices.azure.com/.default"
MAX_CHARS = 400
CACHE_SIZE = 400


class Speech:
    def __init__(self, llm: LLM):
        self.credential = llm.credential
        self.url = None
        if llm.online and llm.endpoint:
            account = urlparse(llm.endpoint).hostname.split(".")[0]
            self.url = f"https://{account}.cognitiveservices.azure.com/tts/cognitiveservices/v1"
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._token: tuple[str, float] | None = None

    @property
    def available(self) -> bool:
        return self.url is not None and self.credential is not None

    async def _bearer(self) -> str:
        if self._token is None or self._token[1] - time.time() < 120:
            token = await self.credential.get_token(SCOPE)
            self._token = (token.token, token.expires_on)
        return self._token[0]

    @staticmethod
    def ssml(text: str, language: str, excited: bool) -> str:
        locale, voice = VOICES.get(language, VOICES["en"])
        rate, pitch = ("+12%", "+6%") if excited else ("+0%", "+0%")
        return (f"<speak version='1.0' xml:lang='{locale}'><voice name='{voice}'>"
                f"<prosody rate='{rate}' pitch='{pitch}'>{escape(text[:MAX_CHARS])}</prosody></voice></speak>")

    async def synthesize(self, text: str, language: str, excited: bool = False) -> bytes:
        ssml = self.ssml(text, language, excited)
        key = hashlib.sha256(ssml.encode()).hexdigest()
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(self.url, content=ssml.encode(), headers={
                "Authorization": f"Bearer {await self._bearer()}",
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
                "User-Agent": "prompt-the-pitch",
            })
        response.raise_for_status()
        self._cache[key] = response.content
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return response.content
