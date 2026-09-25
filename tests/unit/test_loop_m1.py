import json

import pytest

from agentplat.messages import Role
from agentplat.orchestrator import AgentConfig, Orchestrator, RunStatus
from agentplat.providers.fake import ScriptedProvider, call, reply
from agentplat.tools.builtin.calculator import Calculator, safe_eval
from agentplat.tools.builtin.notes import InMemoryNotes, NotesRead, NotesWrite
from agentplat.tools.registry import ToolRegistry


@pytest.fixture
def registry() -> ToolRegistry:
    notes = InMemoryNotes()
    return ToolRegistry([Calculator(), NotesWrite(notes), NotesRead(notes)])


def agent(**kw: object) -> AgentConfig:
    base: dict[str, object] = {
        "name": "t",
        "system_prompt": "be helpful",
        "allowed_tools": ["calculator", "notes_write", "notes_read"],
    }
    base.update(kw)
    return AgentConfig(**base)  # type: ignore[arg-type]


async def test_direct_answer(registry: ToolRegistry) -> None:
    orch = Orchestrator(ScriptedProvider([reply("hi")]), registry)
    result = await orch.run(agent(), "hello")
    assert result.status is RunStatus.SUCCEEDED
    assert result.final_output == "hi"
    assert result.model_calls == 1


async def test_multi_step_tool_use(registry: ToolRegistry) -> None:
    provider = ScriptedProvider(
        [
            call("calculator", expression="0.17 * 2340 + 12"),
            call("notes_write", key="answer", value="409.8"),
            reply("The answer is 409.8"),
        ]
    )
    result = await Orchestrator(provider, registry).run(agent(), "what's 17% of 2340 + 12")
    assert result.status is RunStatus.SUCCEEDED
    assert result.tool_calls == ["calculator", "notes_write"]
    tool_msgs = [m for m in result.messages if m.role is Role.TOOL]
    assert json.loads(tool_msgs[0].content)["result"] == pytest.approx(409.8)
    # the model saw the observation before answering
    last_prompt = provider.calls[-1]
    assert any(m.role is Role.TOOL and m.name == "notes_write" for m in last_prompt)
    assert result.usage.prompt_tokens == 30


async def test_max_steps(registry: ToolRegistry) -> None:
    provider = ScriptedProvider([call("calculator", expression="1+1") for _ in range(5)])
    result = await Orchestrator(provider, registry).run(agent(max_steps=3), "loop")
    assert result.status is RunStatus.MAX_STEPS
    assert result.model_calls == 3


async def test_tool_not_allowed_is_observation_not_execution(registry: ToolRegistry) -> None:
    provider = ScriptedProvider([call("notes_write", key="k", value="v"), reply("ok")])
    result = await Orchestrator(provider, registry).run(agent(allowed_tools=["calculator"]), "x")
    tool_msg = next(m for m in result.messages if m.role is Role.TOOL)
    assert "not available" in tool_msg.content


async def test_invalid_args_reported_to_model(registry: ToolRegistry) -> None:
    provider = ScriptedProvider([call("calculator", expr="1+1"), reply("sorry")])
    result = await Orchestrator(provider, registry).run(agent(), "x")
    tool_msg = next(m for m in result.messages if m.role is Role.TOOL)
    assert "invalid arguments" in tool_msg.content


@pytest.mark.parametrize(
    ("expr", "value"), [("2+3*4", 14.0), ("(1+2)**2", 9.0), ("-5 // 2", -3.0), ("7 % 3", 1.0)]
)
def test_safe_eval(expr: str, value: float) -> None:
    assert safe_eval(expr) == value


@pytest.mark.parametrize("expr", ["__import__('os')", "2**1000", "1/0", "a+1", "[1,2]"])
def test_safe_eval_rejects(expr: str) -> None:
    from agentplat.tools.base import ToolError

    with pytest.raises(ToolError):
        safe_eval(expr)


def test_tool_schema_forbids_extra_fields() -> None:
    schema = Calculator().schema()
    assert schema.parameters["additionalProperties"] is False
    assert schema.parameters["required"] == ["expression"]
