from __future__ import annotations

import unittest
from unittest import mock
from typing import Optional

import spacemit_llm.llm_model as llm_module
from spacemit_llm.llm_model import (
    LLMModel,
    _calls_from_text,
    _validate_tool_arguments,
    mcp_tools_to_ollama,
)


TOOLS = [
    {
        "name": "set_office_light",
        "description": "设置办公室灯光",
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["on", "off"]},
            },
            "required": ["state"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_office_light",
        "description": "查询办公室灯光状态",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


def response(
    *,
    content: str = "",
    calls: Optional[list[tuple[str, object]]] = None,
):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = [
            {
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            for name, arguments in calls
        ]
    return {"message": message, "prompt_eval_count": 10, "eval_count": 5}


class ScriptedLLM(LLMModel):
    def __init__(self, responses):
        self._model_path = "test-native-model"
        self._stream = False
        self._think_disabled = True
        self.responses = list(responses)
        self.requests = []

    def _chat(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("测试未提供足够的 LLM 响应")
        return self.responses.pop(0)


class FakeHttpResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class LlamaServerAdapterTests(unittest.TestCase):
    @mock.patch("spacemit_llm.llm_model.httpx.post")
    def test_openai_tool_call_is_normalized(self, post):
        post.return_value = FakeHttpResponse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_123",
                                    "type": "function",
                                    "function": {
                                        "name": "set_office_light",
                                        "arguments": '{"state":"on"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 21, "completion_tokens": 8},
            }
        )
        with mock.patch.dict(
            "os.environ",
            {
                "LLM_BACKEND": "llama-server",
                "LLM_BASE_URL": "http://127.0.0.1:8080",
                "MCP_TOOL_LLM": "qwen-test",
            },
            clear=False,
        ):
            model = LLMModel(stream=False)
        result = model._chat(
            model=model._model_path,
            messages=[{"role": "user", "content": "打开灯"}],
            tools=mcp_tools_to_ollama(TOOLS),
            stream=False,
            options={"num_predict": 64, "temperature": 0},
            keep_alive="30m",
        )
        self.assertEqual(
            result["message"]["tool_calls"][0]["function"]["name"],
            "set_office_light",
        )
        self.assertEqual(result["prompt_eval_count"], 21)
        endpoint = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertEqual(endpoint, "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(payload["max_tokens"], 64)
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertNotIn("keep_alive", payload)

    def test_openai_messages_include_tool_call_id(self):
        converted = LLMModel._as_openai_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "get_office_light",
                                "arguments": {},
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_name": "get_office_light",
                    "tool_call_id": "call_abc",
                    "content": '{"value":"开"}',
                },
            ]
        )
        self.assertEqual(
            converted[0]["tool_calls"][0]["function"]["arguments"], "{}"
        )
        self.assertEqual(converted[1]["tool_call_id"], "call_abc")
        self.assertNotIn("tool_name", converted[1])


class ToolAgentTests(unittest.TestCase):
    def setUp(self):
        self.old_skip = llm_module.SKIP_SUMMARIZE
        llm_module.SKIP_SUMMARIZE = True

    def tearDown(self):
        llm_module.SKIP_SUMMARIZE = self.old_skip

    def run_agent(self, prompt, responses, executor=None, max_rounds=3):
        executed = []

        def default_executor(name, arguments):
            executed.append((name, arguments))
            return "ok"

        model = ScriptedLLM(responses)
        result = model.run_tool_agent(
            prompt,
            TOOLS,
            executor or default_executor,
            max_rounds=max_rounds,
        )
        return result, executed, model

    def test_llm_call_executes_without_description_keyword(self):
        result, executed, _ = self.run_agent(
            "有点暗，帮我处理一下",
            [response(calls=[("set_office_light", {"state": "on"})])],
        )
        self.assertEqual(executed, [("set_office_light", {"state": "on"})])
        self.assertTrue(result["calls"][0]["ok"])

    def test_host_does_not_infer_call_from_user_text(self):
        result, executed, _ = self.run_agent(
            "设置办公室灯光",
            [response(content="请问还需要什么？")],
        )
        self.assertEqual(executed, [])
        self.assertEqual(result["calls"], [])

    def test_text_protocol_requires_exact_tool_name(self):
        allowed = {"get_office_light"}
        exact = '<tool_call>{"name":"get_office_light","arguments":{}}</tool_call>'
        prefix = '<tool_call>{"name":"get_office","arguments":{}}</tool_call>'
        typo = '<tool_call>{"name":"get_office_ligth","arguments":{}}</tool_call>'
        self.assertEqual(
            _calls_from_text(exact, allowed),
            [("get_office_light", {})],
        )
        self.assertEqual(_calls_from_text(prefix, allowed), [])
        self.assertEqual(_calls_from_text(typo, allowed), [])

    def test_multiple_llm_calls_execute_without_host_expansion(self):
        result, executed, _ = self.run_agent(
            "处理两件事",
            [
                response(
                    calls=[
                        ("set_office_light", {"state": "off"}),
                        ("get_office_light", {}),
                    ]
                )
            ],
        )
        self.assertEqual(
            executed,
            [
                ("set_office_light", {"state": "off"}),
                ("get_office_light", {}),
            ],
        )
        self.assertEqual(len(result["calls"]), 2)

    def test_schema_validation_rejects_missing_wrong_and_extra_args(self):
        schema = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        self.assertIsNone(
            _validate_tool_arguments(
                "set_office_light", {"state": "on"}, schema
            )
        )
        self.assertIn(
            "required",
            _validate_tool_arguments("set_office_light", {}, schema),
        )
        self.assertIn(
            "not one of",
            _validate_tool_arguments(
                "set_office_light", {"state": "dim"}, schema
            ),
        )
        self.assertIn(
            "Additional properties",
            _validate_tool_arguments(
                "get_office_light", {"room": "office"}, schema
            ),
        )
        self.assertIn(
            "未知工具",
            _validate_tool_arguments("get_office_ligth", {}, schema),
        )

    def test_ollama_schema_keeps_validation_constraints(self):
        tools = mcp_tools_to_ollama(
            [
                {
                    "name": "set_level",
                    "description": "设置亮度",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 100,
                            }
                        },
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                }
            ]
        )
        parameters = tools[0]["function"]["parameters"]
        self.assertEqual(parameters["required"], ["value"])
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["properties"]["value"]["minimum"], 0)
        self.assertEqual(parameters["properties"]["value"]["maximum"], 100)

    def test_invalid_arguments_never_reach_executor(self):
        result, executed, model = self.run_agent(
            "打开灯",
            [
                response(calls=[("set_office_light", {})]),
                response(content="参数不完整"),
            ],
        )
        self.assertEqual(executed, [])
        self.assertFalse(result["calls"][0]["ok"])
        tool_messages = [
            msg
            for request in model.requests[1:]
            for msg in request["messages"]
            if msg.get("role") == "tool"
        ]
        self.assertTrue(any("校验失败" in msg["content"] for msg in tool_messages))

    def test_unknown_native_tool_never_reaches_executor(self):
        result, executed, _ = self.run_agent(
            "执行未知工具",
            [
                response(calls=[("get_office_ligth", {})]),
                response(content="工具不存在"),
            ],
        )
        self.assertEqual(executed, [])
        self.assertIn("未知工具", result["calls"][0]["error"])

    def test_executor_failure_is_returned_to_model(self):
        seen = []

        def failing(name, arguments):
            seen.append((name, arguments))
            return "ERROR: MCP timeout"

        result, _, model = self.run_agent(
            "查一下",
            [
                response(calls=[("get_office_light", {})]),
                response(content="设备暂时不可用"),
            ],
            executor=failing,
        )
        self.assertEqual(seen, [("get_office_light", {})])
        self.assertFalse(result["calls"][0]["ok"])
        self.assertEqual(result["content"], "设备暂时不可用")
        self.assertEqual(len(model.requests), 2)

    def test_max_rounds_stops_retry_loop(self):
        result, executed, _ = self.run_agent(
            "打开灯",
            [response(calls=[("set_office_light", {})])],
            max_rounds=1,
        )
        self.assertEqual(executed, [])
        self.assertEqual(result["rounds"], 1)
        self.assertFalse(result["calls"][0]["ok"])


if __name__ == "__main__":
    unittest.main()
