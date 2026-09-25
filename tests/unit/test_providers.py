"""Contract tests: adapters speak each provider's wire format, verified with
recorded-shape fixtures and httpx.MockTransport. No network, no keys."""

import json
from typing import Any

import httpx
import pytest

from agentplat.messages import Message, Role, ToolCall, ToolSchema
from agentplat.providers.anthropic import AnthropicProvider
from agentplat.providers.base import ProviderError
from agentplat.providers.gemini import GeminiProvider, to_gemini_schema
from agentplat.providers.openai import OpenAIProvider
from agentplat.providers.retry import RetryingProvider
from agentplat.providers.rules import RuleBasedProvider, _extract_expression
from agentplat.tools.builtin.calculator import Calculator

CALC = Calculator().schema()
CONVO = [
    Message(Role.SYSTEM, "sys"),
    Message(Role.USER, "17% of 2340?"),
    Message(Role.ASSISTANT, "", tool_calls=[ToolCall("c1", "calculator", {"expression": "1+1"})]),
    Message(Role.TOOL, '{"result": 2}', tool_call_id="c1", name="calculator"),
]


def mock_client(
    response: dict[str, Any], seen: list[httpx.Request], status: int = 200
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=response)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_openai_request_and_parallel_tool_calls() -> None:
    seen: list[httpx.Request] = []
    resp = {
        "model": "m",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "a",
                            "function": {"name": "calculator", "arguments": '{"expression":"2*2"}'},
                        },
                        {"id": "b", "function": {"name": "calculator", "arguments": "not json"}},
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 7},
    }
    p = OpenAIProvider("sk-test", "m", client=mock_client(resp, seen))
    out = await p.complete(CONVO, [CALC])
    body = json.loads(seen[0].content)
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"expression": "1+1"}'
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": '{"result": 2}'}
    assert body["tools"][0]["function"]["name"] == "calculator"
    assert [c.id for c in out.tool_calls] == ["a", "b"]
    assert out.tool_calls[1].arguments == {"__unparseable_arguments__": "not json"}
    assert out.usage.prompt_tokens == 50


async def test_anthropic_merges_tool_results_and_parses_tool_use() -> None:
    seen: list[httpx.Request] = []
    resp = {
        "model": "m",
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "Let me calculate."},
            {"type": "tool_use", "id": "tu1", "name": "calculator", "input": {"expression": "3"}},
        ],
        "usage": {"input_tokens": 20, "output_tokens": 4},
    }
    convo = [*CONVO, Message(Role.TOOL, "x", tool_call_id="c2", name="calculator")]
    p = AnthropicProvider("k", "m", client=mock_client(resp, seen))
    out = await p.complete(convo, [CALC])
    body = json.loads(seen[0].content)
    assert body["system"] == "sys"
    assert seen[0].headers["x-api-key"] == "k"
    last = body["messages"][-1]
    assert last["role"] == "user" and len(last["content"]) == 2  # merged tool results
    assert body["tools"][0]["input_schema"]["required"] == ["expression"]
    assert out.content == "Let me calculate."
    assert out.tool_calls == [ToolCall("tu1", "calculator", {"expression": "3"})]


async def test_gemini_request_shape_and_schema_cleanup() -> None:
    seen: list[httpx.Request] = []
    resp = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [{"functionCall": {"name": "calculator", "args": {"expression": "5"}}}]
                },
            }
        ],
        "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 2},
    }
    p = GeminiProvider("g", "gm", client=mock_client(resp, seen))
    out = await p.complete(CONVO, [CALC])
    body = json.loads(seen[0].content)
    assert seen[0].url.path.endswith("/models/gm:generateContent")
    assert seen[0].headers["x-goog-api-key"] == "g"
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    assert body["contents"][1]["role"] == "model"
    assert body["contents"][2]["parts"][0]["functionResponse"]["response"] == {
        "result": {"result": 2}
    }
    params = body["tools"][0]["functionDeclarations"][0]["parameters"]
    assert "additionalProperties" not in params
    assert out.tool_calls[0].name == "calculator"


def test_gemini_schema_strips_nested() -> None:
    s = {"type": "object", "title": "X", "properties": {"a": {"type": "string", "title": "A"}}}
    assert to_gemini_schema(s) == {"type": "object", "properties": {"a": {"type": "string"}}}


@pytest.mark.parametrize(
    ("status", "transient"), [(429, True), (503, True), (400, False), (401, False)]
)
async def test_http_errors_classified(status: int, transient: bool) -> None:
    p = OpenAIProvider("secret-key", "m", client=mock_client({"error": "x"}, [], status=status))
    with pytest.raises(ProviderError) as ei:
        await p.complete(CONVO, [])
    assert ei.value.transient is transient
    assert "secret-key" not in str(ei.value)


async def test_retrying_provider_retries_transient_only() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    class Flaky:
        name = "flaky"

        def __init__(self, errors: list[ProviderError]) -> None:
            self.errors = errors

        async def complete(self, *a: Any, **k: Any) -> Any:
            attempts.append(1)
            if self.errors:
                raise self.errors.pop(0)
            return await RuleBasedProvider().complete([Message(Role.USER, "hi")], [])

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    ok = RetryingProvider(Flaky([ProviderError("x", transient=True)] * 2), sleep=fake_sleep)
    await ok.complete([], [])
    assert len(attempts) == 3 and len(sleeps) == 2

    attempts.clear()
    bad = RetryingProvider(Flaky([ProviderError("bad", transient=False)]), sleep=fake_sleep)
    with pytest.raises(ProviderError):
        await bad.complete([], [])
    assert len(attempts) == 1


@pytest.mark.parametrize(
    ("text", "expr"),
    [
        ("What's 17% of 2,340 plus 12?", "(17/100*2340) + 12"),
        ("compute 3 * (4 + 5)", "3 * (4 + 5)"),
        ("hello there", None),
    ],
)
def test_rule_provider_expression_extraction(text: str, expr: str | None) -> None:
    assert _extract_expression(text) == expr


async def test_rule_provider_only_uses_offered_tools() -> None:
    p = RuleBasedProvider()
    tools: list[ToolSchema] = []
    out = await p.complete([Message(Role.USER, "what is 2+2")], tools)
    assert out.is_final
