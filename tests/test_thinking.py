"""Reasoning is display data, separated from answers and model history."""

from copy import deepcopy
import importlib
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import ingest
with patch.object(ingest, "ensure_built", return_value=None):
    app_module = importlib.import_module("ankiagent.app")

from ankiagent import llm, llm_client


def response(content="", reasoning=None, tools=()):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content, reasoning=reasoning, tool_calls=tools),
        finish_reason="tool_calls" if tools else "stop",
    )])


class ThinkingTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.dict(os.environ, {"LLM_PROVIDER": "groq", "LLM_MODEL": "openai/gpt-oss-120b"}),
            patch.object(llm.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_model_specific_output_options(self):
        self.assertEqual(llm_client.reasoning_output_kwargs("openai/gpt-oss-120b"), {"include_reasoning": True})
        self.assertEqual(llm_client.reasoning_output_kwargs("qwen/qwen3.8-27b"), {"reasoning_format": "parsed"})
        self.assertEqual(llm_client.reasoning_output_kwargs("custom"), {})
        with patch.dict(os.environ, {"LLM_PROVIDER": "openai"}):
            self.assertEqual(llm_client.reasoning_output_kwargs("openai/gpt-oss-120b"), {})

    def test_api_returns_reasoning_and_tools_in_order_without_replaying_reasoning(self):
        tool = SimpleNamespace(id="search", function=SimpleNamespace(
            name="search_deck", arguments='{"query":"sucralfate"}',
        ))
        for model in ("openai/gpt-oss-120b", "qwen/qwen3.8-27b"):
            with self.subTest(model=model):
                outputs = iter([
                    response(reasoning="I should search for the mechanism.", tools=[tool]),
                    response("Answer [1].", reasoning="The search supports this answer."),
                ])
                requests = []
                def create(**kwargs):
                    requests.append(deepcopy(kwargs))
                    return next(outputs)
                fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                with patch.object(llm, "get_client", return_value=fake), patch.object(
                    llm.search, "fast_search", return_value=[{"guid": "one", "front": "Sucralfate"}],
                ):
                    result = app_module.app.test_client().post("/api/tutor", json={
                        "model": model, "messages": [{"role": "user", "content": "Explain sucralfate"}],
                    })
                self.assertEqual(result.status_code, 200)
                body = result.get_json()
                self.assertEqual(body["answer"], "Answer [1].")
                steps = body["debug"]["thinking"]
                self.assertEqual([s["type"] for s in steps], ["reasoning", "tool", "reasoning"])
                self.assertEqual([s["attempt"] for s in steps], [1, 1, 1])
                self.assertIn("sucralfate", steps[1]["text"])
                for request in requests:
                    for message in request["messages"]:
                        self.assertNotIn("reasoning", message)
                        self.assertNotIn("I should search", message.get("content", ""))
                    for key, value in llm_client.reasoning_output_kwargs(model).items():
                        self.assertEqual(request[key], value)

    def test_forced_finalization_captures_reasoning_separately(self):
        create = Mock(return_value=response("Final answer.", "Final reasoning."))
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(llm, "get_client", return_value=fake):
            answer, _, debug = llm.tutor_chat(
                [{"role": "user", "content": "Explain sucralfate"}], max_iterations=0,
            )
        self.assertEqual(answer, "Final answer.")
        self.assertTrue(debug["forced_finalize"])
        self.assertEqual(debug["thinking"][0]["text"], "Final reasoning.")
        self.assertTrue(create.call_args.kwargs["include_reasoning"])

    def test_retry_activity_retains_reasoning_from_each_attempt(self):
        create = Mock(side_effect=[
            response(reasoning="First attempt reasoning."),
            response("Recovered answer.", reasoning="Second attempt reasoning."),
        ])
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(llm, "get_client", return_value=fake), self.assertLogs("ankiagent.llm", level="WARNING"):
            answer, _, debug = llm.tutor_chat([{ "role": "user", "content": "Explain sucralfate"}])
        self.assertEqual(answer, "Recovered answer.")
        self.assertEqual([s["type"] for s in debug["thinking"]], ["reasoning", "retry", "reasoning"])
        self.assertEqual([s["attempt"] for s in debug["thinking"]], [1, 1, 2])

    def test_missing_reasoning_does_not_invent_text(self):
        create = Mock(return_value=response("Answer."))
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(llm, "get_client", return_value=fake):
            answer, _, debug = llm.tutor_chat([{ "role": "user", "content": "Explain sucralfate"}])
        self.assertEqual(answer, "Answer.")
        self.assertEqual(debug["thinking"], [])


if __name__ == "__main__":
    unittest.main()
