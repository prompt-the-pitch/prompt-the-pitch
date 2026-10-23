"""Connection to Microsoft Foundry through Microsoft Agent Framework.

Agents run on the Foundry model deployment with Microsoft Entra ID authentication
(`az login` locally, managed identity in Azure). Without a configured endpoint, or
with ITG_OFFLINE=1, the system runs offline on deterministic fallbacks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from agent_framework import Agent
from dotenv import load_dotenv
from pydantic import BaseModel

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
FOUNDRY_SCOPE = "https://ai.azure.com/.default"
RATE_LIMIT_BACKOFF_S = (3, 6, 12)  # Waits before retrying a call refused with HTTP 429.


def _rate_limited(exc: BaseException) -> bool:
    return "429" in str(exc) or "rate_limit" in str(exc).lower()


class LLM:
    def __init__(self, client: Any | None, timeout_s: float = 45.0, reasoning_effort: str = "minimal",
                 max_parallel: int = 16, credential: Any | None = None, endpoint: str | None = None):
        self.client = client
        self.credential = credential  # Shared with other Azure AI services (speech).
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.reasoning_effort = reasoning_effort
        self.max_parallel = max_parallel
        self._slots: asyncio.Semaphore | None = None

    @property
    def slots(self) -> asyncio.Semaphore:
        """Caps concurrent Foundry calls so bursts (many viewers x languages) stay under rate limits."""
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.max_parallel)
        return self._slots

    @property
    def online(self) -> bool:
        return self.client is not None

    def agent(self, name: str, instructions: str, tools: Sequence[Callable[..., Any]] | None = None) -> Agent:
        # A fresh Agent per run keeps concurrent runs isolated; the client is shared.
        return Agent(client=self.client, name=name, instructions=instructions, tools=tools)

    async def _run(self, agent: Agent, prompt: str, options: dict[str, Any]) -> Any:
        """One agent run, within the parallel-call cap, waiting and retrying when rate limited."""
        for wait in (0, *RATE_LIMIT_BACKOFF_S):
            if wait:
                await asyncio.sleep(wait)
            try:
                async with self.slots:
                    return await asyncio.wait_for(agent.run(prompt, options=options), timeout=self.timeout_s)
            except Exception as exc:
                if not _rate_limited(exc) or wait == RATE_LIMIT_BACKOFF_S[-1]:
                    raise

    async def structured(self, name: str, instructions: str, payload: dict[str, Any], output: type[T]) -> T:
        """Run one agent turn and return its structured output."""
        agent = self.agent(name, instructions)
        prompt = json.dumps(payload, ensure_ascii=False, default=str)
        response = await self._run(agent, prompt, {"response_format": output,
                                                   "reasoning": {"effort": self.reasoning_effort}})
        if response.value is None:
            raise ValueError(f"{name} returned no structured output")
        return response.value

    async def chat(self, name: str, instructions: str, question: str,
                   tools: Sequence[Callable[..., Any]]) -> str:
        agent = self.agent(name, instructions, tools)
        response = await self._run(agent, question, {"reasoning": {"effort": "low"}})
        return response.text


@asynccontextmanager
async def open_llm(offline: bool | None = None) -> AsyncIterator[LLM]:
    """Yield an LLM bound to the Foundry project, or an offline one."""
    load_dotenv(".env")
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    model = os.environ.get("FOUNDRY_MODEL_DEPLOYMENT")
    if offline is None:
        offline = os.environ.get("ITG_OFFLINE") == "1"
    if offline or not endpoint or not model:
        yield LLM(None)
        return

    from agent_framework.foundry import FoundryChatClient
    from azure.identity.aio import DefaultAzureCredential

    async with DefaultAzureCredential(exclude_interactive_browser_credential=True) as credential:
        try:
            # Resolve the credential chain once at start-up so the first live moment is not slowed down.
            await asyncio.wait_for(credential.get_token(FOUNDRY_SCOPE), timeout=30)
        except Exception as exc:
            log.warning("could not get a Foundry token at start-up: %r", exc)
        client = FoundryChatClient(project_endpoint=endpoint, model=model, credential=credential)
        yield LLM(client, credential=credential, endpoint=endpoint)
