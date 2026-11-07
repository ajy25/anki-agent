"""Model routing regressions; no live deck access or inference calls."""

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


GPT = "openai/gpt-oss-120b"
QWEN = "qwen/qwen3.8-27b"


def response(content="Answer", tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls or [])
    )])


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "LLM_PROVIDER": "groq", "LLM_MODEL": GPT, "LLM_REASONING_EFFORT": "low",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app_module.app.test_client()
        self.create = Mock(return_value=response())
        self.fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        ))
        client_patch = patch.object(llm, "get_client", return_value=self.fake_client)
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def test_picker_and_default(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn(f'value="{GPT}" selected', page)
        self.assertIn(f'value="{QWEN}"', page)
        self.assertEqual(llm_client.get_model(), GPT)
        self.assertEqual(llm_client.get_model(QWEN), QWEN)
        self.assertEqual(llm_client.reasoning_kwargs(QWEN), {"reasoning_effort": "low"})

    def test_api_routes_selected_model_and_legacy_default(self):
        cases = [
            ("/api/tutor", "tutor_chat", {"messages": [{"role": "user", "content": "heart"}]}, ("Answer", [], {})),
            ("/api/search_assist", "assisted_search", {"q": "heart"}, ([], {}, {})),
            ("/api/deep_search", "deep_search", {"q": "heart"}, {"status": "results", "results": []}),
        ]
        for route, function, body, result in cases:
            for model in (GPT, QWEN, None):
                with self.subTest(route=route, model=model), patch.object(llm, function, return_value=result) as call:
                    payload = {**body, **({"model": model} if model else {})}
                    self.assertEqual(self.client.post(route, json=payload).status_code, 200)
                    self.assertEqual(call.call_args.kwargs["model"], model or GPT)

    def test_invalid_models_rejected_before_inference(self):
        for route in ("/api/tutor", "/api/search_assist", "/api/deep_search"):
            for model in ("unknown", "", [], {}, 123):
                with self.subTest(route=route, model=model):
                    res = self.client.post(route, json={"model": model, "q": "heart"})
                    self.assertEqual(res.status_code, 400)
                    self.assertIn("Unsupported model", res.get_json()["error"])
        self.create.assert_not_called()

    def test_tutor_requests_keep_their_model(self):
        # Interleaved sessions must not mutate a shared/global model setting.
        first = llm.AgentSession("heart", model=QWEN)
        second = llm.AgentSession("heart", model=GPT)
        for session in (second, first, second):
            _, _, debug = session.run()
            self.assertEqual(self.create.call_args.kwargs["model"], session.model)
            self.assertEqual(debug["model"], session.model)
        self.assertEqual(llm_client.get_model(), GPT)

    def test_tutor_tool_loop_and_forced_finalization(self):
        tool = SimpleNamespace(id="call-1", function=SimpleNamespace(
            name="search_deck", arguments='{"query":"heart"}'
        ))
        for model in (GPT, QWEN):
            self.create.reset_mock()
            self.create.side_effect = [response(tool_calls=[tool]), response("Final")]
            with patch.object(llm.search, "fast_search", return_value=[]):
                answer, _, debug = llm.tutor_chat(
                    [{"role": "user", "content": "heart"}], model=model, max_iterations=1
                )
            self.assertEqual(answer, "Final")
            self.assertTrue(debug["forced_finalize"])
            self.assertEqual([call.kwargs["model"] for call in self.create.call_args_list], [model, model])
            self.assertEqual(self.create.call_args.kwargs["reasoning_effort"], "low")

    def test_assisted_search_uses_selected_model(self):
        self.create.return_value = response('{"query":"heart"}')
        with patch.object(llm, "_run_assist_plan", return_value=[]):
            _, _, debug = llm.assisted_search("heart", model=QWEN)
        self.assertEqual(debug["model"], QWEN)
        self.assertEqual(self.create.call_args.kwargs["model"], QWEN)

    def test_deep_search_threads_use_selected_model(self):
        pool = [{"guid": str(i), "front": "heart"} for i in range(25)]
        self.create.return_value = response('{"keep":[{"i":0,"why":"matches"}]}')
        with patch.object(llm, "_run_assist_plan", return_value=pool):
            out = llm.deep_search("heart", model=QWEN)
        self.assertEqual(out["debug"]["model"], QWEN)
        self.assertEqual(out["debug"]["batches"], 3)
        # One scout call and three parallel screening calls.
        self.assertEqual(self.create.call_count, 4)
        self.assertEqual({call.kwargs["model"] for call in self.create.call_args_list}, {QWEN})


if __name__ == "__main__":
    unittest.main()
