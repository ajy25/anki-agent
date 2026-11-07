"""Preserve pasted cards at the API/LLM boundary and retrieve before answering."""

from copy import deepcopy
import importlib
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import ingest
with patch.object(ingest, "ensure_built", return_value=None):
    app_module = importlib.import_module("ankiagent.app")

from ankiagent import llm


CARD = """What is the long-term treatment for severe hypercalcemia (> 14 mg/dL or symptomatic)?

Bisphosphonates (e.g., zoledronic acid, pamidronate)

Symptoms include weakness, GI distress, and neuropsychiatric symptoms

Sketchy Sketchy 2 Pixorize Bootcamp"""

CURRENT_CARD = """Absorptive hypercalciuria, renal hypercalciuria, and resorptive hypercalciuria present with normocalcemia

Increases the risk for calcium kidney stones

Sketchy Bootcamp


1"""


class TutorInputTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.calls = []
        tool = SimpleNamespace(id="search-1", function=SimpleNamespace(
            name="search_deck", arguments=json.dumps({"query": "hypercalcemia bisphosphonates"}),
        ))

        def create(**kwargs):
            self.calls.append(deepcopy(kwargs))
            searching = len(self.calls) % 2 == 1
            message = SimpleNamespace(
                content="" if searching else "Card explanation [1].",
                tool_calls=[tool] if searching else [],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        for patcher in (
            patch.dict(os.environ, {"LLM_PROVIDER": "groq", "LLM_MODEL": "openai/gpt-oss-120b"}),
            patch.object(llm, "get_client", return_value=fake),
            patch.object(llm.time, "sleep"),
            patch.object(llm.search, "fast_search", return_value=[{
                "guid": "card-guid", "front": CARD, "back": "", "tags": [],
            }]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_full_multiline_card_is_the_user_message_for_both_models(self):
        for model in ("openai/gpt-oss-120b", "qwen/qwen3.8-27b"):
            with self.subTest(model=model):
                self.calls.clear()
                result = self.client.post("/api/tutor", json={
                    "messages": [{"role": "user", "content": CARD}], "model": model,
                })
                self.assertEqual(result.status_code, 200)
                first, second = self.calls
                self.assertEqual(first["messages"][-1], {"role": "user", "content": CARD})
                self.assertIn(llm.TUTOR_FIRST_TURN_FORMAT, first["messages"][0]["content"])
                self.assertEqual(first["tool_choice"], {
                    "type": "function", "function": {"name": "search_deck"},
                })
                self.assertEqual(second["tool_choice"], "auto")
                self.assertEqual(first["model"], model)
                self.assertEqual(result.get_json()["answer"], "Card explanation [1].")
                self.assertTrue(result.get_json()["sources"][0]["cited"])

    def test_followup_keeps_raw_history_and_drops_first_reply_format(self):
        history = [
            {"role": "user", "content": CARD},
            {"role": "assistant", "content": "Card explanation [1]."},
            {"role": "user", "content": "Why does it take time to work?"},
        ]
        result = self.client.post("/api/tutor", json={"messages": history})
        self.assertEqual(result.status_code, 200)
        first = self.calls[0]
        self.assertEqual(first["messages"][1:], history)
        self.assertNotIn(llm.TUTOR_FIRST_TURN_FORMAT, first["messages"][0]["content"])
        self.assertEqual(first["tool_choice"]["function"]["name"], "search_deck")

    def test_empty_user_message_is_rejected_without_calling_the_model(self):
        result = self.client.post("/api/tutor", json={
            "messages": [{"role": "user", "content": " \n\t "}],
        })
        self.assertEqual(result.status_code, 400)
        self.assertFalse(self.calls)

    def test_empty_current_turn_does_not_replay_a_previous_question(self):
        result = self.client.post("/api/tutor", json={"messages": [
            {"role": "user", "content": CARD},
            {"role": "user", "content": "  "},
        ]})
        self.assertEqual(result.status_code, 400)
        self.assertFalse(self.calls)

    def test_false_empty_reply_retries_same_card_and_discards_unrelated_sources(self):
        missing = "It looks like your message came through empty — no card or question attached."
        tool = SimpleNamespace(id="search", function=SimpleNamespace(
            name="search_deck", arguments='{"query":"test"}',
        ))
        outputs = iter([
            SimpleNamespace(content="", tool_calls=[tool]),
            SimpleNamespace(content=missing, tool_calls=[]),
            SimpleNamespace(content="", tool_calls=[tool]),
            SimpleNamespace(content="Current card explanation [1].", tool_calls=[]),
        ])

        def create(**kwargs):
            self.calls.append(deepcopy(kwargs))
            return SimpleNamespace(choices=[SimpleNamespace(message=next(outputs))])

        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(llm, "get_client", return_value=fake), patch.object(
            llm.search, "fast_search", side_effect=[
                [{"guid": "unrelated", "front": "Unrelated source"}],
                [{"guid": "correct", "front": CURRENT_CARD}],
            ],
        ):
            result = self.client.post("/api/tutor", json={
                "messages": [{"role": "user", "content": CURRENT_CARD}],
                "model": "qwen/qwen3.8-27b",
            })
        self.assertEqual(result.status_code, 200)
        body = result.get_json()
        self.assertEqual(body["answer"], "Current card explanation [1].")
        self.assertEqual([s["guid"] for s in body["sources"]], ["correct"])
        self.assertEqual(body["debug"]["response_retries"], 1)
        for request in self.calls:
            users = [m["content"] for m in request["messages"] if m["role"] == "user"]
            self.assertEqual(users, [CURRENT_CARD])
            self.assertEqual(request["model"], "qwen/qwen3.8-27b")

    def test_persistent_false_empty_claim_is_an_error_with_no_sources(self):
        def failed(**kwargs):
            return "Your message is empty.", [{"guid": "irrelevant"}], {}
        with patch.object(llm.AgentSession, "run", side_effect=failed) as run:
            with self.assertLogs(app_module.app.logger, level="WARNING"):
                result = self.client.post("/api/tutor", json={
                    "messages": [{"role": "user", "content": CURRENT_CARD}],
                })
        self.assertEqual(run.call_count, 4)
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("answer", result.get_json())
        self.assertEqual(result.get_json()["sources"], [])
        self.assertEqual(result.get_json()["debug"]["error"], "false_empty_input")

    def test_missing_input_detection_does_not_reject_medical_explanations(self):
        for answer in (
            "Your question is about empty sella syndrome.",
            "The bladder is empty after urination [1].",
            "The deck does not cover this card, but the supplied text describes hypercalciuria.",
            "This card is incomplete on the underlying mechanism; here is how it works [1].",
        ):
            with self.subTest(answer=answer):
                self.assertFalse(llm._claims_missing_input(answer))

    def test_three_retries_can_recover_for_both_models(self):
        history = [
            {"role": "user", "content": CARD},
            {"role": "assistant", "content": "Earlier explanation."},
            {"role": "user", "content": "Why does it take time?"},
        ]
        for model in ("openai/gpt-oss-120b", "qwen/qwen3.8-27b"):
            with self.subTest(model=model):
                create = Mock(side_effect=[
                    TimeoutError("temporary outage"),
                    SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(content="", tool_calls=[]),
                        finish_reason="length",
                    )]),
                    SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(content="Your message is empty.", tool_calls=[]),
                    )]),
                    SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(content="Recovered explanation.", tool_calls=[]),
                        finish_reason="stop",
                    )]),
                ])
                fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                with patch.object(llm, "get_client", return_value=fake), patch.object(
                    llm.time, "sleep",
                ) as sleep, self.assertLogs("ankiagent.llm", level="WARNING"):
                    result = self.client.post("/api/tutor", json={
                        "messages": history, "model": model, "deck": "Test deck",
                    })
                self.assertEqual(result.status_code, 200)
                body = result.get_json()
                self.assertEqual(body["answer"], "Recovered explanation.")
                self.assertEqual(body["debug"]["response_retries"], 3)
                self.assertNotIn("error", body["debug"])
                failures = body["debug"]["retry_failures"]
                self.assertEqual([f["error"] for f in failures], [
                    "TimeoutError:temporary outage", "empty_response", "false_empty_input",
                ])
                self.assertEqual(failures[1]["finish_reason"], "length")
                self.assertEqual(create.call_count, 4)
                self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0), call(2.0)])
                for request in create.call_args_list:
                    self.assertEqual(request.kwargs["model"], model)
                    self.assertEqual(request.kwargs["messages"][1:], history)
                    self.assertEqual(request.kwargs["tool_choice"]["function"]["name"], "search_deck")

    def test_persistent_provider_or_empty_failure_stops_after_four_attempts(self):
        for failure in (TimeoutError("outage"), "", "   "):
            with self.subTest(failure=failure):
                if isinstance(failure, Exception):
                    create = Mock(side_effect=failure)
                else:
                    create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(content=failure, tool_calls=[]),
                        finish_reason="length",
                    )]))
                fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                with patch.object(llm, "get_client", return_value=fake), self.assertLogs(level="WARNING"):
                    result = self.client.post("/api/tutor", json={
                        "messages": [{"role": "user", "content": CURRENT_CARD}],
                    })
                self.assertEqual(create.call_count, 4)
                self.assertEqual(result.status_code, 502)
                body = result.get_json()
                self.assertNotIn("answer", body)
                self.assertEqual(body["sources"], [])
                self.assertEqual(body["debug"]["response_retries"], 3)
                self.assertEqual(len(body["debug"]["retry_failures"]), 4)

    def test_forced_finalization_failure_retries_with_fresh_sources(self):
        tool = SimpleNamespace(id="search", function=SimpleNamespace(
            name="search_deck", arguments='{"query":"test"}',
        ))
        searching = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=[tool]),
        )])
        create = Mock(side_effect=[
            searching, TimeoutError("finalization failed"), searching,
            SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="Recovered [1].", tool_calls=[]),
            )]),
        ])
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(llm, "get_client", return_value=fake), patch.object(
            llm.search, "fast_search", side_effect=[
                [{"guid": "failed-source", "front": "Old evidence"}],
                [{"guid": "correct", "front": CURRENT_CARD}],
            ],
        ) as search, self.assertLogs("ankiagent.llm", level="WARNING"):
            answer, sources, debug = llm.tutor_chat(
                [{"role": "user", "content": CURRENT_CARD}],
                deck="Test deck", max_iterations=1,
            )
        self.assertEqual(answer, "Recovered [1].")
        self.assertEqual([s["guid"] for s in sources], ["correct"])
        self.assertEqual(debug["response_retries"], 1)
        self.assertEqual(debug["retry_failures"][0]["error"], "TimeoutError:finalization failed")
        self.assertEqual([c.kwargs["deck"] for c in search.call_args_list], ["Test deck", "Test deck"])

    def test_success_does_not_retry_or_wait(self):
        with patch.object(llm.time, "sleep") as sleep:
            result = self.client.post("/api/tutor", json={
                "messages": [{"role": "user", "content": CARD}],
            })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.calls), 2)  # One search and one final answer.
        self.assertEqual(result.get_json()["debug"]["response_retries"], 0)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
