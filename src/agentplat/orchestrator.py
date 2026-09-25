"""The agent loop.

model proposes -> runtime executes -> observation fed back -> repeat
until the model answers, or the step budget runs out.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from agentplat.messages import Message, Role, ToolCall, Usage
from agentplat.providers.base import ModelProvider
from agentplat.tools.base import ToolContext, ToolError
from agentplat.tools.registry import ToolRegistry, UnknownToolError


@dataclass(slots=True)
class AgentConfig:
    name: str
    system_prompt: str
    allowed_tools: list[str]
    max_steps: int = 8
    model: str | None = None


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MAX_STEPS = "max_steps_exceeded"


@dataclass(slots=True)
class RunResult:
    run_id: str
    status: RunStatus
    final_output: str | None
    messages: list[Message]
    usage: Usage = field(default_factory=Usage)
    model_calls: int = 0
    tool_calls: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(self, provider: ModelProvider, registry: ToolRegistry) -> None:
        self.provider = provider
        self.registry = registry

    async def run(
        self, agent: AgentConfig, user_input: str, *, user_id: str = "user", tenant_id: str = "t"
    ) -> RunResult:
        run_id = uuid.uuid4().hex
        messages = [Message(Role.SYSTEM, agent.system_prompt), Message(Role.USER, user_input)]
        result = RunResult(run_id, RunStatus.MAX_STEPS, None, messages)
        schemas = self.registry.schemas(agent.allowed_tools)

        for _ in range(agent.max_steps):
            response = await self.provider.complete(messages, schemas, model=agent.model)
            result.usage += response.usage
            result.model_calls += 1
            if response.is_final:
                messages.append(Message(Role.ASSISTANT, response.content or ""))
                result.status = RunStatus.SUCCEEDED
                result.final_output = response.content or ""
                return result

            messages.append(
                Message(Role.ASSISTANT, response.content or "", tool_calls=response.tool_calls)
            )
            for call in response.tool_calls:
                result.tool_calls.append(call.name)
                observation = await self._execute(agent, call, run_id, user_id, tenant_id)
                messages.append(
                    Message(Role.TOOL, observation, tool_call_id=call.id, name=call.name)
                )
        return result

    async def _execute(
        self, agent: AgentConfig, call: ToolCall, run_id: str, user_id: str, tenant_id: str
    ) -> str:
        if call.name not in agent.allowed_tools:
            return _obs({"error": f"tool {call.name!r} is not available to this agent"})
        try:
            tool = self.registry.get(call.name)
            args = tool.parse_args(call.arguments)
            ctx = ToolContext(run_id, user_id, tenant_id, idempotency_key=f"{run_id}:{call.id}")
            result = await tool.run(args, ctx)
        except UnknownToolError:
            return _obs({"error": f"unknown tool {call.name!r}"})
        except ValidationError as exc:
            return _obs({"error": "invalid arguments", "details": exc.errors(include_url=False)})
        except ToolError as exc:
            return _obs({"error": str(exc)})
        return _obs(result.output)


def _obs(payload: Any) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload, default=str)
