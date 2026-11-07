"""Reasoning and actual search activity across planning and screening."""
from copy import deepcopy
import importlib
import json
import os
import re
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import ingest
with patch.object(ingest, "ensure_built", return_value=None):
    app_module = importlib.import_module("ankiagent.app")
from ankiagent import llm


def response(content="", reasoning=None, tools=()):
    return NS(choices=[NS(message=NS(content=content, reasoning=reasoning, tool_calls=tools))])


def client(create):
    return NS(chat=NS(completions=NS(create=create)))


class SearchThinkingTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"LLM_PROVIDER": "groq", "LLM_MODEL": "openai/gpt-oss-120b"})
        env.start()
        self.addCleanup(env.stop)

    def test_assist_captures_reasoning_and_searches_without_replaying_thoughts(self):
        tools = [NS(id="tags", function=NS(name="find_tags", arguments='{"substring":"GI"}')),
            NS(id="probe", function=NS(name="search_deck", arguments='{"query":"ulcer"}'))]
        for model in ("openai/gpt-oss-120b", "qwen/qwen3.8-27b"):
            with self.subTest(model=model):
                outputs = iter([response(reasoning="Discover tags and probe the topic.", tools=tools),
                    response('{"keywords":"ulcer","explanation":"Match ulcer cards."}', "Use the verified topic.")])
                requests = []
                def create(**kwargs):
                    requests.append(deepcopy(kwargs))
                    return next(outputs)
                with patch.object(llm, "get_client", return_value=client(create)), patch.object(
                    llm.search, "find_tags", return_value=[("GI", 2)],
                ), patch.object(llm.search, "fast_search", return_value=[]), patch.object(
                    llm, "_run_assist_plan", return_value=[],
                ):
                    result = app_module.app.test_client().post("/api/search_assist", json={"q": "ulcer", "model": model})
                self.assertEqual(result.status_code, 200)
                data = result.get_json()
                self.assertEqual(data["plan"]["keywords"], "ulcer")
                self.assertEqual([s["type"] for s in data["debug"]["thinking"]], ["reasoning", "tool", "tool", "reasoning", "tool"])
                self.assertIn("GI", data["debug"]["thinking"][1]["text"])
                self.assertIn("0 matching cards", data["debug"]["thinking"][2]["text"])
                for request in requests:
                    self.assertEqual(request["model"], model)
                    self.assertNotIn("Discover tags", json.dumps(request["messages"]))
                    self.assertNotIn("reasoning", request["messages"][0])
                self.assertIn("total_ms", data["debug"])

    def test_deep_keeps_scout_and_distinct_screening_batches(self):
        pool = [{"guid": str(i), "front": f"Card {i}"} for i in range(25)]
        def create(**kwargs):
            if kwargs["messages"][0]["content"] != llm.SCREEN_SYSTEM:
                return response('{"keywords":"ulcer"}', "Scout reasoning.")
            idx = int(re.search(r"\[(\d+)\]", kwargs["messages"][1]["content"])[1])
            return response(json.dumps({"keep": [{"i": idx, "why": "Matches request"}]}), f"Screening reasoning for {idx}.")
        with patch.object(llm, "get_client", return_value=client(create)), patch.object(llm, "_run_assist_plan", return_value=pool):
            result = llm.deep_search("ulcer")
        self.assertEqual([card["guid"] for card in result["results"]], ["0", "12", "24"])
        thoughts = [s for s in result["debug"]["thinking"] if s["type"] == "reasoning"]
        self.assertEqual(thoughts[0]["phase"], "plan")
        self.assertEqual([s["batch"] for s in thoughts[1:]], [1, 2, 3])
        self.assertTrue(all(card["deep_why"] == "Matches request" for card in result["results"]))

    def test_clarification_retains_planning_reasoning_without_screening(self):
        create = Mock(return_value=response('{"keywords":"ulcer"}', "Broad topic reasoning."))
        with patch.object(llm, "get_client", return_value=client(create)), patch.object(
            llm, "_run_assist_plan", return_value=[{"guid": str(i)} for i in range(121)],
        ):
            result = llm.deep_search("ulcer")
        self.assertEqual(result["status"], "clarify")
        self.assertEqual(create.call_count, 1)
        self.assertEqual(result["debug"]["thinking"][0]["text"], "Broad topic reasoning.")
        self.assertIn("Waiting for you", result["debug"]["thinking"][-1]["text"])

    def test_failed_screening_is_visible_alongside_successful_batches(self):
        pool = [{"guid": str(i)} for i in range(13)]
        def create(**kwargs):
            if kwargs["messages"][0]["content"] != llm.SCREEN_SYSTEM:
                return response('{"keywords":"ulcer"}')
            if "[12]" in kwargs["messages"][1]["content"]:
                raise TimeoutError("batch unavailable")
            return response('{"keep":[{"i":0,"why":"Matches"}]}', "This card matches.")
        with patch.object(llm, "get_client", return_value=client(create)), patch.object(llm, "_run_assist_plan", return_value=pool):
            result = llm.deep_search("ulcer")
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["debug"]["screen_errors"])
        failure = next(s for s in result["debug"]["thinking"] if s["type"] == "error")
        self.assertEqual(failure["batch"], 2)

    def test_search_routes_deliver_reasoning_before_planner_completes(self):
        for route in ("/api/search_assist", "/api/deep_search"):
            with self.subTest(route=route):
                release = threading.Event()
                def chunks():
                    yield NS(choices=[NS(delta=NS(content=None, reasoning="Live reasoning.", tool_calls=None), finish_reason=None)])
                    if not release.wait(3):
                        raise RuntimeError("Test did not release stream")
                    yield NS(choices=[NS(delta=NS(content='{"keywords":"ulcer"}', reasoning=None, tool_calls=None), finish_reason="stop")])
                class Stream:
                    def __iter__(self): return chunks()
                    def close(self): pass
                create = Mock(return_value=Stream())
                with patch.object(llm, "get_client", return_value=client(create)), patch.object(llm, "_run_assist_plan", return_value=[]):
                    result = app_module.app.test_client().post(route, json={"q": "ulcer", "stream": True}, buffered=False)
                    frames = iter(result.response)
                    seen = []
                    while not any("reasoning_delta" in frame for frame in seen):
                        seen.append(next(frames).decode())
                    self.assertFalse(release.is_set())
                    release.set()
                    seen.extend(frame.decode() for frame in frames)
                    result.close()
                self.assertIn("event: done", "".join(seen))
                self.assertNotIn("event: answer_delta", "".join(seen))
                self.assertTrue(create.call_args.kwargs["stream"])


if __name__ == "__main__":
    unittest.main()
