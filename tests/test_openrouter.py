"""Provider isolation, onboarding, and real SDK wire compatibility without network."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace as NS
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
import httpx2
from openai import OpenAI
from flask import Flask
from werkzeug.test import Client
from werkzeug.wrappers import Response
from ankiagent import llm, llm_client
from ankiagent.desktop import DesktopManager, create_desktop_app, validate_key
from ankiagent.settings import SettingsStore


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.collection = self.root / 'collection.anki2'
        with sqlite3.connect(self.collection) as db:
            db.executescript("CREATE TABLE decks (id INTEGER, name TEXT); INSERT INTO decks VALUES(1,'Study');")
        env = patch.dict(os.environ, {'LLM_PROVIDER':'groq', 'LLM_MODEL':'',
            'GROQ_API_KEY':'', 'OPENROUTER_API_KEY':'', 'DECK_PATH':'', 'ANKIAGENT_DATA_DIR':str(self.root)})
        env.start(); self.addCleanup(env.stop)
        llm_client.reset_clients(); self.addCleanup(llm_client.reset_clients)
        self.store = SettingsStore(self.root / 'preferences')
        self.store.save({'deck_path':str(self.collection), 'deck_name':'Study'}, 'gsk_saved')
        backend = Flask('fixture'); backend.add_url_rule('/', view_func=lambda: 'Ready')
        self.manager = DesktopManager(self.store, Mock(return_value=backend))
        self.validator = Mock()
        app, _ = create_desktop_app(self.manager, key_validator=self.validator)
        self.client = Client(app, Response)

    def post(self, route, data):
        return self.client.post('/api/setup/' + route, json=data, base_url='http://127.0.0.1',
            headers={'X-AnkiAgent-Setup-Token':self.manager.token})

    def test_saving_both_keys_exposes_both_catalogs_and_keeps_saved_keys(self):
        with patch.object(self.manager, 'prepare'):
            r = self.post('save', {'path':str(self.collection), 'deck':'Study',
                'keys': {'groq':'gsk_updated', 'openrouter':'sk-or-saved'}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.validator.call_count, 2)
        self.assertEqual(self.store.get_key('groq'), 'gsk_updated')
        self.assertEqual(self.store.get_key('openrouter'), 'sk-or-saved')
        self.assertEqual(self.store.vault.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('sk-or-saved', self.store.path.read_text())
        self.manager._prepare()
        self.assertEqual(self.manager.status, 'ready')
        self.assertEqual(llm_client.available_models(), {**llm_client.GROQ_MODELS, **llm_client.OPENROUTER_MODELS})
        self.assertNotIn('google/gemini-2.5-flash-lite', llm_client.available_models())
        self.assertEqual(llm_client.get_provider('google/gemini-3.8-flash'), 'openrouter')
        self.assertEqual(llm_client.get_provider('openai/gpt-oss-120b'), 'groq')
        with patch.object(self.manager, 'prepare'):
            self.assertEqual(self.post('save', {'path':str(self.collection), 'deck':'Study',
                'keys': {'groq':'', 'openrouter':''}}).status_code, 200)
        self.assertEqual(self.store.get_key('groq'), 'gsk_updated')
        self.assertEqual(self.store.get_key('openrouter'), 'sk-or-saved')

    def test_either_provider_alone_limits_the_catalog_and_sets_default(self):
        for provider, key, catalog in [('groq','gsk_saved',llm_client.GROQ_MODELS),
                                      ('openrouter','sk-or-saved',llm_client.OPENROUTER_MODELS)]:
            with self.subTest(provider=provider):
                self.store.vault.path.unlink(missing_ok=True)
                self.store.vault.set(key, provider)
                os.environ['OPENROUTER_API_KEY'] = 'sk-or-stale'
                os.environ['GROQ_API_KEY'] = 'gsk_stale'
                self.manager._prepare()
                self.assertEqual(self.manager.status, 'ready')
                self.assertEqual(llm_client.available_models(), catalog)
                self.assertIn(llm_client.get_model(), catalog)
                other = 'groq' if provider == 'openrouter' else 'openrouter'
                self.assertEqual(os.environ[other.upper() + '_API_KEY'], '')
                missing = next(iter(llm_client.GROQ_MODELS if other == 'groq' else llm_client.OPENROUTER_MODELS))
                with self.assertRaises(ValueError): llm_client.get_model(missing)

    def test_no_key_or_invalid_key_cannot_partially_save_other_keys(self):
        self.store.vault.path.unlink()
        with patch.object(self.manager, 'prepare') as prepare:
            r = self.post('save', {'path':str(self.collection), 'keys':{}})
            self.assertEqual(r.status_code, 400)
            def validate(key, provider):
                if provider == 'openrouter': raise ValueError('Rejected')
            self.validator.side_effect = validate
            r = self.post('save', {'path':str(self.collection), 'keys':{'groq':'gsk_new','openrouter':'bad'}})
            self.assertEqual(r.status_code, 400)
            self.assertFalse(self.store.vault.path.exists())
            r = self.post('save', {'path':str(self.collection), 'keys':{'unknown':'key'}})
            self.assertEqual(r.status_code, 400)
            prepare.assert_not_called()

    def test_concurrent_requests_route_by_model_without_changing_default_provider(self):
        barrier = Barrier(2)
        groq, router = Mock(), Mock()
        sent = {}
        def complete(provider, **kwargs):
            sent[provider] = kwargs
            barrier.wait(timeout=5)
            return NS(choices=[NS(message=NS(content='{"keep":[]}'))])
        groq.chat.completions.create.side_effect = lambda **kw: complete('groq', **kw)
        router.chat.completions.create.side_effect = lambda **kw: complete('openrouter', **kw)
        with patch.dict(os.environ, {'ANKIAGENT_PROVIDERS':'groq,openrouter','LLM_PROVIDER':'groq'}), \
             patch.object(llm_client, '_GROQ_CLIENT', groq), patch.object(llm_client, '_OPENROUTER_CLIENT', router):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(llm._screen_batch, 'heart', [], [(0,{'front':'Heart'})], model=m)
                           for m in ('openai/gpt-oss-120b','openai/gpt-6-luna')]
                for future in futures:
                    self.assertIsNone(future.result()[1])
            self.assertEqual(os.environ['LLM_PROVIDER'], 'groq')
        self.assertIn('max_completion_tokens', sent['groq'])
        self.assertIn('max_tokens', sent['openrouter'])
        self.assertNotIn('temperature', sent['openrouter'])
        self.assertNotIn('include_reasoning', sent['openrouter'])
        self.assertEqual(sent['openrouter']['extra_body']['reasoning']['effort'], 'low')

    def test_search_planner_preserves_gemini_signatures_for_tool_results(self):
        signature = [{'index':0,'type':'reasoning.encrypted','data':'opaque-signature'}]
        calls = []
        def handler(request):
            calls.append(json.loads(request.content))
            message = {'role':'assistant','content':'{"keywords":"heart"}'}
            if len(calls) == 1:
                message.update(content='', reasoning_details=signature, tool_calls=[{
                    'id':'tag1','type':'function','function':{'name':'find_tags','arguments':'{"substring":"heart"}'},
                    'extra_content':{'google':{'thought_signature':'tool-signature'}}}])
            return httpx2.Response(200,json={'id':'test','object':'chat.completion','created':0,
                'model':'google/gemini-3.8-flash','choices':[{'index':0,'finish_reason':'tool_calls' if len(calls)==1 else 'stop','message':message}]})
        with patch.dict(os.environ, {'ANKIAGENT_PROVIDERS':'groq,openrouter'}):
            with OpenAI(api_key='sk-or-fixture',base_url=llm_client.OPENROUTER_BASE_URL,
                http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
                with patch.object(llm_client,'_OPENROUTER_CLIENT',client), patch.object(llm.search,'find_tags',return_value=[]):
                    plan = llm._run_planner('Find cards','heart',{'thinking':[]},model='google/gemini-3.8-flash')
        self.assertEqual(plan, {'keywords':'heart'})
        assistant = next(m for m in calls[1]['messages'] if m['role']=='assistant')
        self.assertEqual(assistant['reasoning_details'], signature)
        self.assertEqual(assistant['tool_calls'][0]['extra_content']['google']['thought_signature'], 'tool-signature')

    def test_initial_environment_keys_are_persisted_before_first_settings_save(self):
        self.store.path.unlink()
        self.store.vault.path.unlink()
        with patch.dict(os.environ, {'GROQ_API_KEY':'gsk_initial','OPENROUTER_API_KEY':'sk-or-initial'}):
            with patch.object(self.manager, 'prepare'):
                r = self.post('save', {'path':str(self.collection),'keys':{}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.get_key('groq'), 'gsk_initial')
        self.assertEqual(self.store.get_key('openrouter'), 'sk-or-initial')

    def test_key_status_never_returns_secrets(self):
        self.store.vault.set('sk-or-secret', 'openrouter')
        r = self.client.get('/api/setup/state', base_url='http://127.0.0.1',
            headers={'X-AnkiAgent-Setup-Token':self.manager.token})
        self.assertEqual(r.get_json()['saved_keys'], {'groq':True, 'openrouter':True})
        self.assertNotIn(b'sk-or-secret', r.data)
        self.assertNotIn(b'gsk_saved', r.data)

    def test_key_validation_uses_authenticated_endpoint_and_redacts_errors(self):
        fake = Mock(); fake.__enter__ = Mock(return_value=fake); fake.__exit__ = Mock(return_value=False)
        fake.get.return_value.json.return_value = {'data':{'limit':5}}
        with patch('httpx.Client', return_value=fake):
            validate_key('sk-or-fixture', 'openrouter')
        self.assertEqual(fake.get.call_args.args, ('https://openrouter.ai/api/v1/key',))
        self.assertEqual(fake.get.call_args.kwargs['headers']['Authorization'], 'Bearer sk-or-fixture')
        response = httpx.Response(401, request=httpx.Request('GET','https://openrouter.ai/api/v1/key'))
        fake.get.return_value.raise_for_status.side_effect = httpx.HTTPStatusError('sk-or-secret', request=response.request, response=response)
        with patch('httpx.Client', return_value=fake), self.assertRaisesRegex(ValueError, 'OpenRouter did not accept') as err:
            validate_key('sk-or-fixture', 'openrouter')
        self.assertNotIn('sk-or-secret', str(err.exception))
        with self.assertRaisesRegex(ValueError, 'OpenRouter API key'):
            validate_key('gsk_wrong_provider', 'openrouter')

    def test_real_sdk_uses_openrouter_key_url_and_supported_options(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx2.Response(200, json={'id':'test','object':'chat.completion','created':0,
                'model':'openai/gpt-6-luna','choices':[{'index':0,'finish_reason':'stop',
                'message':{'role':'assistant','content':'Answer'}}]})
        with patch.dict(os.environ, {'LLM_PROVIDER':'openrouter','OPENROUTER_API_KEY':'sk-or-fixture','GROQ_API_KEY':'gsk_other'}):
            real_client = OpenAI(api_key='sk-or-fixture', base_url=llm_client.OPENROUTER_BASE_URL,
                http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0)
            self.addCleanup(real_client.close)
            with patch('openai.OpenAI', return_value=real_client) as factory:
                client = llm_client.get_client()
            self.assertEqual(factory.call_args.kwargs['api_key'], 'sk-or-fixture')
            self.assertEqual(factory.call_args.kwargs['base_url'], llm_client.OPENROUTER_BASE_URL)
            llm._chat_completion(client, model='openai/gpt-6-luna', messages=[{'role':'user','content':'Hello'}],
                temperature=.2, max_completion_tokens=1500, **llm_client.reasoning_kwargs('openai/gpt-6-luna'))
        request = calls[0]; payload = json.loads(request.content)
        self.assertEqual(str(request.url), 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(request.headers['authorization'], 'Bearer sk-or-fixture')
        self.assertEqual(payload['max_tokens'], 1500)
        self.assertNotIn('temperature', payload)
        self.assertNotIn('max_completion_tokens', payload)
        self.assertEqual(payload['reasoning'], {'effort':'low'})

    def test_streamed_gemini_tool_signature_and_citations_survive_next_round(self):
        calls = []
        def handler(request):
            payload = json.loads(request.content); calls.append(payload)
            if len(calls) == 1:
                deltas = [
                    {'reasoning_details':[{'index':0,'type':'reasoning.encrypted','format':'google-gemini-v1','data':'opaque-'}]},
                    {'reasoning_details':[{'index':0,'type':'reasoning.encrypted','data':'signature'}],
                     'tool_calls':[{'index':0,'id':'search1','type':'function','function':{'name':'search_deck','arguments':'{"query":"heart"}'},'extra_content':{'google':{'thought_signature':'token'}}}]},
                ]; finish = 'tool_calls'
            else:
                deltas = [{'content':'Explanation [1].'}]; finish = 'stop'
            chunks = [{'id':'test','object':'chat.completion.chunk','created':0,'model':'google/gemini-3.8-flash',
                       'choices':[{'index':0,'delta':d,'finish_reason':None}]} for d in deltas]
            chunks.append({'choices':[{'index':0,'delta':{},'finish_reason':finish}]})
            body = ''.join('data: '+json.dumps(c)+'\n\n' for c in chunks) + 'data: [DONE]\n\n'
            return httpx2.Response(200, text=body, headers={'content-type':'text/event-stream'})
        events = []
        with patch.dict(os.environ, {'LLM_PROVIDER':'openrouter'}):
            with OpenAI(api_key='sk-or-fixture', base_url=llm_client.OPENROUTER_BASE_URL,
                http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
                with patch.object(llm, 'get_client', return_value=client), patch.object(llm.search, 'fast_search', return_value=[{'guid':'one','front':'Heart'}]):
                    answer, sources, debug = llm.tutor_chat([{'role':'user','content':'Explain heart'}], model='google/gemini-3.8-flash', on_event=events.append)
        self.assertEqual(answer, 'Explanation [1].')
        self.assertTrue(sources[0]['cited'])
        self.assertEqual(len(calls), 2)
        assistant = next(m for m in calls[1]['messages'] if m['role']=='assistant')
        self.assertEqual(assistant['reasoning_details'][0]['data'], 'opaque-signature')
        self.assertEqual(assistant['tool_calls'][0]['extra_content']['google']['thought_signature'], 'token')
        self.assertFalse(any('opaque' in str(e) for e in events))
        self.assertEqual(calls[0]['temperature'], .2)
        self.assertNotIn('error', debug)


if __name__ == '__main__':
    unittest.main()
