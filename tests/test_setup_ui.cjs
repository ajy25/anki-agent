const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const providers = {
  groq: {label: 'Groq', prefix: 'gsk_', keys_url: 'https://console.groq.com/keys'},
  openrouter: {label: 'OpenRouter', prefix: 'sk-or-', keys_url: 'https://openrouter.ai/settings/keys'},
};
async function fixture(savedKeys = {groq: true, openrouter: false}) {
  const elements = {}, requests = [];
  function element(id) {
    return elements[id] ||= {
      value: '', textContent: '', type: 'password', disabled: false, hidden: false,
      listeners: {}, children: [],
      addEventListener(event, handler) { this.listeners[event] = handler; },
      setAttribute(name, value) { this[name] = value; },
      appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; },
      focus() { this.focused = true; },
    };
  }
  const state = {status: 'ready', config: {provider: 'groq', deck_path: '/fixture/collection.anki2', deck_name: 'Study'},
    providers, saved_keys: savedKeys, profiles: []};
  let testReply = async () => ({ok: true});
  const context = vm.createContext({
    document: {getElementById: element, querySelector: () => ({content: 'token'}), createElement: () => element(Symbol())},
    window: {location: {pathname: '/setup', replace() {}}, addEventListener() {}},
    setTimeout() {},
    async fetch(url, options) {
      const body = options.body && JSON.parse(options.body);
      requests.push({url, body});
      const data = url.endsWith('/state') ? state : url.endsWith('/decks') ? {path: '/fixture/collection.anki2', decks: ['Study']} :
        url.endsWith('/test-key') ? await testReply() : {ok: true};
      return {ok: true, json: async () => data};
    },
  });
  vm.runInContext(fs.readFileSync('ankiagent/static/setup.js', 'utf8'), context);
  await new Promise(setImmediate);
  return {element, requests, setTestReply(fn) {testReply = fn;},
    submit() {return element('setup-form').listeners.submit({preventDefault() {}});}};
}

test('either saved key permits setup; no keys asks for one', async () => {
  for (const saved of [{groq:true,openrouter:false},{groq:false,openrouter:true},{groq:true,openrouter:true}]) {
    const f = await fixture(saved);
    await f.submit();
    assert(f.requests.some(r => r.url.endsWith('/save')));
  }
  const f = await fixture({groq:false,openrouter:false});
  await f.submit();
  assert.match(f.element('status').textContent, /Add at least one API key/);
  assert.equal(f.requests.some(r => r.url.endsWith('/save')), false);
});

test('new keys are tested separately and saved together without mixing them', async () => {
  const f = await fixture({groq:false,openrouter:false});
  f.element('groq-key').value = 'gsk-fixture';
  f.element('openrouter-key').value = 'sk-or-fixture';
  await f.element('openrouter-test').listeners.click();
  await f.element('groq-test').listeners.click();
  const tested = f.requests.filter(r => r.url.endsWith('/test-key')).map(r => r.body);
  assert.deepEqual(tested, [{key:'sk-or-fixture',provider:'openrouter'},{key:'gsk-fixture',provider:'groq'}]);
  assert.match(f.element('openrouter-status').textContent, /accepted by OpenRouter/);
  assert.match(f.element('groq-status').textContent, /accepted by Groq/);
  await f.submit();
  const saved = f.requests.find(r => r.url.endsWith('/save')).body;
  assert.deepEqual(saved.keys, {groq:'gsk-fixture',openrouter:'sk-or-fixture'});
  assert.equal(saved.path, '/fixture/collection.anki2');
  assert.equal(f.element('groq-key').value, '');
  assert.equal(f.element('openrouter-key').value, '');
});

test('editing a key while its test is pending clears stale success', async () => {
  const f = await fixture();
  let complete;
  f.setTestReply(() => new Promise(resolve => {complete = resolve;}));
  const pending = f.element('openrouter-test').listeners.click();
  f.element('openrouter-key').value = 'sk-or-new';
  f.element('openrouter-key').listeners.input();
  complete({ok:true});
  await pending;
  assert.equal(f.element('openrouter-status').textContent, '');
  assert.equal(f.element('openrouter-test').disabled, false);
});

test('saved keys stay hidden and can be retained without reentry', async () => {
  const f = await fixture({groq:true,openrouter:true});
  for (const p of ['groq','openrouter']) {
    assert.match(f.element(`${p}-key`).placeholder, /Key saved/);
    assert.equal(f.element(`${p}-key`).value, '');
    await f.element(`${p}-test`).listeners.click();
  }
  await f.submit();
  assert.deepEqual(f.requests.find(r => r.url.endsWith('/save')).body.keys, {groq:'',openrouter:''});
});
