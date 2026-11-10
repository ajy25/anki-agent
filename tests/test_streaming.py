"""Streaming deltas, tool calls, retry boundaries, and client cancellation."""
import importlib
import json
import os
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import ingest
with patch.object(ingest, "ensure_built", return_value=None):
    app_module = importlib.import_module("ankiagent.app")
from ankiagent import llm


def chunk(text=None, reasoning=None, tools=None, finish=None):
    return NS(choices=[NS(delta=NS(content=text, reasoning=reasoning, tool_calls=tools), finish_reason=finish)])


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
    def __iter__(self):
        return iter(self.chunks)
    def close(self):
        self.closed = True


def client_for(create):
    return NS(chat=NS(completions=NS(create=create)))


class StreamingTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.dict(os.environ, {"LLM_PROVIDER": "groq", "LLM_MODEL": "openai/gpt-oss-120b"}),
            patch.object(llm.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_tokens_emit_before_completion_for_both_models(self):
        for model in ("openai/gpt-oss-120b", "qwen/qwen3.8-27b"):
            events = []
            def tokens():
                yield chunk(reasoning="Look ")
                self.assertEqual(events[-1]["text"], "Look ")
                yield chunk(reasoning="at the card.")
                yield chunk(text="Hello ")
                self.assertEqual(events[-1]["event"], "answer_delta")
                yield chunk(text="world.", finish="stop")
            stream = Stream(tokens())
            create = Mock(return_value=stream)
            with patch.object(llm, "get_client", return_value=client_for(create)):
                answer, _, debug = llm.tutor_chat(
                    [{"role": "user", "content": "Explain this card"}], model=model, on_event=events.append,
                )
            self.assertEqual(answer, "Hello world.")
            self.assertEqual(debug["thinking"][0]["text"], "Look at the card.")
            self.assertTrue(stream.closed)
            self.assertTrue(create.call_args.kwargs["stream"])
            self.assertEqual([e["text"] for e in events if e["event"] == "answer_delta"], ["Hello ", "world."])

    def test_fragmented_tool_arguments_execute_only_after_complete_call(self):
        first = Stream([
            chunk(text="Let me look."),
            chunk(tools=[NS(index=0, id="call-1", function=NS(name="search_deck", arguments='{"query":'))]),
            chunk(tools=[NS(index=0, id=None, function=NS(name=None, arguments='"sucralfate"}'))], finish="tool_calls"),
        ])
        second = Stream([chunk(text="Explanation [1].", finish="stop")])
        create = Mock(side_effect=[first, second])
        events = []
        with patch.object(llm, "get_client", return_value=client_for(create)), patch.object(
            llm.search, "fast_search", return_value=[{"guid": "one", "front": "Sucralfate"}],
        ) as search:
            answer, sources, debug = llm.tutor_chat(
                [{"role": "user", "content": "Explain sucralfate"}], on_event=events.append,
            )
        self.assertEqual(answer, "Explanation [1].")
        self.assertEqual(search.call_count, 1)
        self.assertEqual(search.call_args.args[0], "sucralfate")
        self.assertEqual([s["guid"] for s in sources], ["one"])
        self.assertTrue(any(e["event"] == "answer_reset" for e in events))
        self.assertTrue(any(e["event"] == "tool" for e in events))
        history = create.call_args.kwargs["messages"]
        self.assertEqual(next(m for m in history if m["role"] == "tool")["tool_call_id"], "call-1")

    def test_interrupted_stream_retries_instead_of_accepting_partial_answer(self):
        first = Stream([chunk(text="Incomplete")])
        second = Stream([chunk(text="Complete answer.", finish="stop")])
        create = Mock(side_effect=[first, second])
        events = []
        with patch.object(llm, "get_client", return_value=client_for(create)), self.assertLogs("ankiagent.llm", level="WARNING"):
            answer, _, debug = llm.tutor_chat(
                [{"role": "user", "content": "Explain this"}], on_event=events.append,
            )
        self.assertEqual(answer, "Complete answer.")
        self.assertEqual(debug["response_retries"], 1)
        self.assertEqual([e["attempt"] for e in events if e["event"] == "attempt"], [1, 2])
        self.assertTrue(any(e["event"] == "retry" for e in events))
        self.assertTrue(first.closed)

    def test_named_tool_rejection_clears_partial_stream_before_recovery(self):
        for model in ("openai/gpt-oss-120b", "qwen/qwen3.8-27b"):
            with self.subTest(model=model):
                def rejected():
                    yield chunk(reasoning="This is a greeting.")
                    yield chunk(text="Discard this partial reply")
                    raise RuntimeError("Tool choice is required, but model did not call a tool")
                first = Stream(rejected())
                second = Stream([chunk(text="How can I help?", finish="stop")])
                create = Mock(side_effect=[first, second])
                events = []
                with patch.object(llm, "get_client", return_value=client_for(create)):
                    answer, sources, debug = llm.tutor_chat(
                        [{"role": "user", "content": "hi"}], model=model, on_event=events.append,
                    )
                visible_answer = ""
                for event in events:
                    if event["event"] == "answer_reset":
                        visible_answer = ""
                    elif event["event"] == "answer_delta":
                        visible_answer += event["text"]
                self.assertEqual(answer, "How can I help?")
                self.assertEqual(visible_answer, answer)
                self.assertEqual(sources, [])
                self.assertTrue(first.closed and second.closed)
                self.assertEqual(create.call_count, 2)
                self.assertEqual(create.call_args.kwargs["tool_choice"], "auto")
                self.assertTrue(debug["tool_choice_fallback"])
                self.assertEqual(debug["response_retries"], 0)
                self.assertNotIn("error", debug)

    def test_cancelled_stream_closes_provider_without_retry(self):
        cancelled = threading.Event()
        stream = Stream([chunk(text="First"), chunk(text="Second", finish="stop")])
        create = Mock(return_value=stream)
        def emit(event):
            if event["event"] == "answer_delta":
                cancelled.set()
        with patch.object(llm, "get_client", return_value=client_for(create)):
            with self.assertRaises(llm.TutorCancelled):
                llm.tutor_chat([{ "role": "user", "content": "Explain this"}], on_event=emit, cancelled=cancelled)
        self.assertTrue(stream.closed)
        self.assertEqual(create.call_count, 1)

    def test_four_failed_streams_stop_and_preserve_failed_reasoning(self):
        streams = [Stream([chunk(reasoning="Partial reasoning.")]) for _ in range(4)]
        create = Mock(side_effect=streams)
        with patch.object(llm, "get_client", return_value=client_for(create)), self.assertLogs("ankiagent.llm", level="WARNING"):
            answer, sources, debug = llm.tutor_chat(
                [{"role": "user", "content": "Explain this"}], on_event=lambda event: None,
            )
        self.assertEqual(answer, "")
        self.assertEqual(sources, [])
        self.assertEqual(create.call_count, 4)
        self.assertEqual(debug["response_retries"], 3)
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertEqual(len([s for s in debug["thinking"] if s["type"] == "reasoning"]), 4)

    def test_parallel_tool_fragments_keep_distinct_arguments(self):
        stream = Stream([
            NS(choices=[]),
            chunk(tools=[
                NS(index=1, id="b", function=NS(name="read_note", arguments='{"guid":')),
                NS(index=0, id="a", function=NS(name="search_deck", arguments='{"query":')),
            ]),
            chunk(tools=[
                NS(index=0, id=None, function=NS(name=None, arguments='"ulcer"}')),
                NS(index=1, id=None, function=NS(name=None, arguments='"note-one"}')),
            ], finish="tool_calls"),
        ])
        response = llm._chat_completion(client_for(Mock(return_value=stream)), emit=lambda event: None)
        tools = response.choices[0].message.tool_calls
        self.assertEqual([tool.id for tool in tools], ["a", "b"])
        self.assertEqual([json.loads(tool.function.arguments) for tool in tools], [
            {"query": "ulcer"}, {"guid": "note-one"},
        ])

    def test_http_stream_presents_source_cards_from_worker_thread(self):
        with patch.object(llm, "tutor_chat", return_value=("Answer [1].", [{
            "guid": "one", "front_html": "<b>Card</b>", "ref": "1", "cited": True,
        }], {})):
            response = app_module.app.test_client().post("/api/tutor", json={
                "messages": [{"role": "user", "content": "Explain this"}], "stream": True,
            })
            body = response.get_data(as_text=True)
        self.assertIn("event: done", body)
        self.assertIn('"front_html": "<b>Card</b>"', body)

    def test_http_delivers_first_token_before_model_finishes_and_final_sources(self):
        release = threading.Event()
        finished = threading.Event()
        def tokens():
            yield chunk(text="First ")
            if not release.wait(3):
                raise RuntimeError("Test did not release the stream")
            yield chunk(text="answer.", finish="stop")
            finished.set()
        create = Mock(return_value=Stream(tokens()))
        with patch.object(llm, "get_client", return_value=client_for(create)):
            response = app_module.app.test_client().post("/api/tutor", json={
                "messages": [{"role": "user", "content": "Explain this"}], "stream": True,
            }, buffered=False)
            self.assertEqual(response.mimetype, "text/event-stream")
            frames = iter(response.response)
            seen = [next(frames).decode()]
            while not any("event: answer_delta" in f for f in seen):
                seen.append(next(frames).decode())
            self.assertFalse(finished.is_set())
            release.set()
            seen.extend(frame.decode() for frame in frames)
            response.close()
        self.assertIn('"answer": "First answer."', "".join(seen))
        self.assertIn("event: done", "".join(seen))

    def test_http_disconnect_signals_worker_cancellation(self):
        stopped = threading.Event()
        def tutor(messages, *, cancelled, **kwargs):
            cancelled.wait(2)
            if cancelled.is_set():
                stopped.set()
                raise llm.TutorCancelled()
            raise RuntimeError("Client did not cancel")
        with patch.object(llm, "tutor_chat", side_effect=tutor):
            response = app_module.app.test_client().post("/api/tutor", json={
                "messages": [{"role": "user", "content": "Explain this"}], "stream": True,
            }, buffered=False)
            response.close()
            self.assertTrue(stopped.wait(1))

    def test_stream_request_validation_and_terminal_errors(self):
        client = app_module.app.test_client()
        self.assertEqual(client.post("/api/tutor", json={"stream": True, "messages": []}).status_code, 400)
        with patch.object(llm, "tutor_chat", return_value=("", [], {"error": "empty_response"})):
            response = client.post("/api/tutor", json={
                "messages": [{"role": "user", "content": "Explain this"}], "stream": True,
            })
            body = response.get_data(as_text=True)
        self.assertIn("event: error", body)
        self.assertNotIn("event: done", body)


if __name__ == "__main__":
    unittest.main()
