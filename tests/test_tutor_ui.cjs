// Run with: node --test tests/test_tutor_ui.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../ankiagent/static/app.js"), "utf8")
  .replace(/boot\(\);\s*$/, "");

function fixture({ render = false, storage = new Map() } = {}) {
  const nodes = new Map();
  const requests = [];
  function node(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {
      value: "", style: { setProperty(key, value) { this[key] = value; } }, dataset: {}, scrollHeight: 24, innerHTML: "old source", hidden: false,
      focus() {}, close() { this.open = false; }, showModal() { this.open = true; }, appendChild() {},
      listeners: {}, addEventListener(name, handler) { this.listeners[name] = handler; },
      insertAdjacentHTML(_where, html) { this.innerHTML += html; },
      querySelectorAll() { return []; }, querySelector() { return null; },
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute(name, value) { this[name] = String(value); },
    });
    return nodes.get(selector);
  }
  const context = vm.createContext({
    document: { querySelector: node }, console, AbortController,
    localStorage: {getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key)},
    TextDecoder, setTimeout, clearTimeout,
    performance, URLSearchParams,
    fetch: (url, options = {}) => new Promise((resolve, reject) => requests.push({
      url, payload: options.body ? JSON.parse(options.body) : null, signal: options.signal, reject,
      respond: (data, status = 200) => resolve({ ok: status < 400, status, json: async () => data }),
      respondStream: () => {
        let controller;
        const body = new ReadableStream({ start(c) { controller = c; } });
        options.signal.addEventListener("abort", () => {
          controller.error(Object.assign(new Error("Aborted"), {name: "AbortError"}));
        });
        resolve({headers: {get: () => "text/event-stream"}, body});
        return {
          raw: (bytes) => controller.enqueue(bytes),
          event: (event, data = {}) => controller.enqueue(new TextEncoder().encode(
            `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
          )),
          close: () => controller.close(),
        };
      },
    })),
  });
  // Classic browser scripts share global function bindings with window.
  // A separate mock window hides accidental function replacement at startup.
  context.window = context;
  context.location = {search: ""};
  vm.runInContext(source, context);
  if (!render) vm.runInContext("renderTutorMessages = () => {};", context);
  vm.runInContext('state.mode = "tutor";', context);
  const run = (code) => vm.runInContext(code, context);
  const state = () => JSON.parse(run("JSON.stringify(state)"));
  function send(text) {
    node("#tutor-input").value = text;
    return run("submitTutor()");
  }
  return { node, requests, run, state, send };
}

test("Tutor text size updates immediately, persists across renders, and rejects invalid values", () => {
  const f = fixture();
  assert.equal(f.run("readTutorTextScale()"), 1);
  assert.equal(f.run("setTutorTextScale('1.5')"), true);
  assert.equal(f.node("#tutor-shell").style["--tutor-text-scale"], "1.5");
  f.run("renderTutor()");
  assert.equal(f.node("#tutor-text-size").value, "1.5");
  assert.equal(f.run("setTutorTextScale('999')"), false);
  assert.equal(f.run("readTutorTextScale()"), 1.5);
  f.run('state.mode = "agent"');
  f.node("#tutor-shell").style["--tutor-text-scale"] = "1";
  f.run("applyTutorTextScale(1.5)");
  assert.equal(f.node("#tutor-shell").style["--tutor-text-scale"], "1");
});

test("Clear chat button clears only the current tab and cancels late replies", async () => {
  for (const mode of ["tutor", "agent"]) {
    const f = fixture({render: true});
    f.run(`state.mode = "${mode}"; state.chatStore = {tutor: [{role:"user", content:"Tutor"}], agent: [{role:"user", content:"Organize"}]}; state.tutorMessages = state.chatStore[state.mode]; renderTutor();`);
    assert.match(f.node("#results").innerHTML, /id="tutor-clear-chat">Clear chat/);
    const pending = f.send("New question");
    const request = f.requests.find(r => r.url === (mode === "tutor" ? "/api/tutor" : "/api/agent"));
    f.node("#tutor-clear-chat").listeners.click();
    assert(request.signal.aborted);
    request.respond({answer: "Stale reply", sources: []});
    await pending;
    assert.deepEqual(f.state().tutorMessages, []);
    assert.equal(f.state().chatStore[mode === "tutor" ? "agent" : "tutor"].length, 1);
    assert.equal(f.node("#tutor-input").value, "");
  }
});

test("Clear chat and automatic clearing work after browser startup wiring", async () => {
  for (const mode of ["tutor", "agent"]) {
    const storage = new Map();
    const f = fixture({render:true, storage});
    // Keep the real boot/render/reset wiring; stub unrelated page/data loading.
    f.run(`window.location.search = "?mode=${mode}";
      bindUi = applyModeUi = updatePlaceholder = () => {};
      loadStats = loadTags = loadDecks = async () => {};`);
    await f.run("boot()");
    f.run(`state.chatStore = {
      tutor: [{role:"user", content:"Tutor message"}],
      agent: [{role:"user", content:"Organize message"}]
    }; state.tutorMessages = state.chatStore[state.mode]; setClearChatOnClose(false);`);
    f.node("#tutor-input").value = "Draft message";
    const pending = f.send("Pending message");
    const request = f.requests.find(r => r.url === (mode === "tutor" ? "/api/tutor" : "/api/agent"));
    f.node("#tutor-clear-chat").listeners.click();
    assert(request.signal.aborted);
    request.respond({answer:"Late reply", sources:[]});
    await pending;
    assert.deepEqual(f.state().chatStore[mode], []);
    assert.equal(f.state().chatStore[mode === "tutor" ? "agent" : "tutor"].length, 1);
    assert.equal(f.node("#tutor-input").value, "");
    assert.match(f.node("#tutor-messages").innerHTML, /tutor-empty/);
    const reopened = fixture({storage});
    reopened.run("restoreChatHistory()");
    assert.deepEqual(reopened.state().chatStore[mode], []);
    f.run("setClearChatOnClose(true); window.handleChatWindowClose();");
    assert.deepEqual(f.state().chatStore, {tutor:[], agent:[]});
  }
});

test("Automatic clearing defaults on and clears both tabs when hiding", () => {
  const f = fixture();
  assert.equal(f.run("readClearChatOnClose()"), true);
  f.run('state.chatStore = {tutor: [{role:"user", content:"Tutor"}], agent: [{role:"user", content:"Organize"}]}; state.tutorMessages = state.chatStore.tutor; handleChatWindowClose();');
  assert.deepEqual(f.state().chatStore, {tutor: [], agent: []});
});

test("Settings toggle keeps chats across reopening without reviving Organize actions", () => {
  const storage = new Map();
  const f = fixture({render: true, storage});
  f.run('bindTutorSettings(); state.chatStore = {tutor: [{role:"user", content:"Tutor"}], agent: [{role:"assistant", content:"Organize", proposals:[{id:"old-action"}]}]}; state.tutorMessages = state.chatStore.tutor;');
  f.node("#clear-chat-on-close").listeners.change({target:{checked:false}});
  f.run("handleChatWindowClose()");
  assert.equal(f.state().tutorMessages.length, 1);
  const reopened = fixture({render:true, storage});
  reopened.run('restoreChatHistory(); state.tutorMessages = state.chatStore.tutor; renderTutorMessages();');
  assert.equal(reopened.run("readClearChatOnClose()"), false);
  assert.equal(reopened.state().tutorMessages[0].content, "Tutor");
  assert.equal(reopened.state().chatStore.agent[0].content, "Organize");
  assert.equal(reopened.state().chatStore.agent[0].proposals, undefined);
  reopened.run("resetTutor()");
  const afterClear = fixture({storage});
  afterClear.run("restoreChatHistory()");
  assert.deepEqual(afterClear.state().chatStore.tutor, []);
  assert.equal(afterClear.state().chatStore.agent.length, 1);
  reopened.run("setClearChatOnClose(true)");
  assert.equal(storage.has("ankiagent.chatHistory"), false);
  assert.equal(reopened.state().chatStore.agent.length, 1); // preference doesn't clear the visible session
  reopened.run("handleChatWindowClose()");
  assert.deepEqual(reopened.state().chatStore, {tutor: [], agent: []});
});

test("Default startup discards old saved history and corrupt history is ignored", () => {
  const storage = new Map([["ankiagent.chatHistory", '{"tutor":[{"role":"user","content":"Old"}]}']]);
  const f = fixture({storage});
  f.run("restoreChatHistory()");
  assert.equal(storage.has("ankiagent.chatHistory"), false);
  storage.set("ankiagent.clearChatOnClose", "false");
  storage.set("ankiagent.chatHistory", "broken JSON");
  f.run("restoreChatHistory()");
  assert.deepEqual(f.state().chatStore, {tutor: [], agent: []});
});

test("Tutor settings load, restore defaults in the editor, and save edited instructions", async () => {
  const f = fixture();
  f.run("bindTutorSettings()");
  const opening = f.run("openTutorSettings()");
  const defaults = {system_prompt: "Default prompt", first_turn_format: "Default first reply"};
  f.requests[0].respond({settings: {system_prompt: "Saved prompt", first_turn_format: ""}, defaults});
  await opening;
  assert.equal(f.node("#tutor-system-prompt").value, "Saved prompt");
  f.node("#tutor-prompt-reset").listeners.click();
  assert.equal(f.node("#tutor-system-prompt").value, "Default prompt");
  assert.equal(f.requests.length, 1); // Reset is a draft until Save.
  f.node("#tutor-system-prompt").value = "Explain with examples.";
  f.node("#tutor-first-turn").value = "";
  const saving = f.node("#tutor-settings-form").listeners.submit({preventDefault() {}});
  assert.deepEqual(f.requests[1].payload, {system_prompt: "Explain with examples.", first_turn_format: ""});
  f.requests[1].respond({settings: f.requests[1].payload, defaults});
  await saving;
  assert.match(f.node("#tutor-settings-status").textContent, /Saved/);
  assert.equal(f.node("#tutor-prompt-fields").disabled, false);
});

test("Failed Tutor settings saves retain edits and allow another attempt", async () => {
  const f = fixture();
  f.node("#tutor-settings-dialog").showModal();
  f.node("#tutor-system-prompt").value = "My unsaved instructions";
  const saving = f.run("saveTutorSettings({preventDefault() {}})");
  f.requests[0].respond({error: "Couldn't save settings."}, 500);
  await saving;
  assert.equal(f.node("#tutor-system-prompt").value, "My unsaved instructions");
  assert.equal(f.node("#tutor-prompt-fields").disabled, false);
  assert.match(f.node("#tutor-settings-status").textContent, /Couldn't save/);
});

test("A stale settings load cannot overwrite a reopened editor", async () => {
  const f = fixture();
  const old = f.run("openTutorSettings()");
  f.node("#tutor-settings-dialog").close();
  const current = f.run("openTutorSettings()");
  const defaults = {system_prompt: "Default", first_turn_format: ""};
  f.requests[1].respond({settings: {system_prompt: "Current", first_turn_format: ""}, defaults});
  await current;
  f.requests[0].respond({settings: {system_prompt: "Old", first_turn_format: ""}, defaults});
  await old;
  assert.equal(f.node("#tutor-system-prompt").value, "Current");
});

test("Organize checks on entry and shows setup instructions when Anki is unavailable", async () => {
  const f = fixture();
  f.run('state.mode = "agent"; renderTutor();');
  assert.equal(f.requests[0].url, "/api/anki/status");
  assert.match(f.node("#results").innerHTML, /Tools → Add-ons → Get Add-ons/);
  assert.match(f.node("#results").innerHTML, /2055492159/);
  assert.equal(f.node("#organize-check-again").disabled, true);
  f.requests[0].respond({error: "Couldn't reach Anki."}, 503);
  await new Promise(setImmediate);
  assert.equal(f.node("#organize-connection").dataset.state, "unavailable");
  assert.equal(f.node("#organize-connection-help").hidden, false);
  assert.equal(f.node("#organize-check-again").textContent, "Check again");
  assert.equal(f.node("#organize-check-again").disabled, false);
  const retry = f.run("checkOrganizeConnection()");
  f.requests[1].respond({connected: true});
  await retry;
  assert.equal(f.node("#organize-connection").dataset.state, "connected");
  assert.equal(f.node("#organize-connection-help").hidden, true);
  assert.equal(f.node("#organize-check-again").hidden, true);
});

test("Tutor does not check AnkiConnect or display Organize setup", () => {
  const f = fixture();
  f.run("renderTutor()");
  assert.equal(f.requests.length, 0);
  assert.doesNotMatch(f.node("#results").innerHTML, /organize-connection/);
});

test("Connection failures allow retry and insert error text without interpreting HTML", async () => {
  const f = fixture();
  f.run('state.mode = "agent"');
  const pending = f.run("checkOrganizeConnection()");
  f.requests[0].reject(new Error("<img src=x onerror=alert(1)>"));
  await pending;
  assert.equal(f.node("#organize-connection-status").textContent, "<img src=x onerror=alert(1)>");
  assert.equal(f.node("#organize-check-again").disabled, false);
  assert.equal(f.node("#organize-connection-help").hidden, false);
});

test("Stale connection checks cannot overwrite newer results or update Tutor", async () => {
  const f = fixture();
  f.run('state.mode = "agent"');
  const old = f.run("checkOrganizeConnection()");
  const latest = f.run("checkOrganizeConnection()");
  f.requests[1].respond({connected: true});
  await latest;
  f.requests[0].respond({error: "Old failure"}, 503);
  await old;
  assert.equal(f.node("#organize-connection").dataset.state, "connected");
  const leaving = f.run("checkOrganizeConnection()");
  f.run('state.mode = "tutor"');
  f.requests[2].respond({error: "Late failure"}, 503);
  await leaving;
  assert.notEqual(f.node("#organize-connection-status").textContent, "Late failure");
});

test("New chat aborts its request and ignores a reply that arrives afterwards", async () => {
  const f = fixture();
  const pending = f.send("Earlier card");
  f.run("resetTutor()");
  assert(f.requests[0].signal.aborted);
  f.requests[0].respond({ answer: "Earlier answer", sources: [{ guid: "old" }] });
  await pending;
  assert.deepEqual(f.state().tutorMessages, []);
  assert.equal(f.state().tutorPending, false);
});

test("A late reply cannot overwrite a new conversation or clear its pending request", async () => {
  const f = fixture();
  const old = f.send("Earlier card");
  f.run("resetTutor()");
  const current = f.send("Current card\n\nAnswer\n\nSketchy Bootcamp\n\n1");
  f.requests[0].respond({ answer: "Stale answer", sources: [{ guid: "stale" }] });
  await old;
  assert.equal(f.state().tutorMessages.length, 1);
  assert.equal(f.state().tutorPending, true);
  assert.equal(f.run("state.tutorAbort.signal.aborted"), false);
  assert.equal(f.requests[1].payload.messages[0].content, "Current card\n\nAnswer\n\nSketchy Bootcamp\n\n1");
  f.requests[1].respond({ answer: "Current explanation", sources: [{ guid: "current" }] });
  await current;
  assert.equal(f.state().tutorMessages[1].content, "Current explanation");
  assert.equal(f.state().tutorMessages[1].sources[0].guid, "current");
});

test("A late failure is ignored after reset or when leaving Tutor", async () => {
  for (const action of ['resetTutor()', 'cancelTutorRequest(); state.mode = "fast"']) {
    const f = fixture();
    const pending = f.send("Current card");
    f.run(action);
    f.requests[0].reject(new Error("Old network failure"));
    await pending;
    assert.equal(f.state().tutorMessages.some((m) => m.role === "assistant"), false);
    assert.equal(f.state().tutorPending, false);
  }
});

test("Errors and source-card HTML are not replayed to the model", async () => {
  const f = fixture();
  f.run(`state.tutorMessages = [
    {role: "user", content: "Original card"},
    {role: "assistant", content: "Explanation", sources: [{front_html: "private markup"}]},
    {role: "assistant", content: "Display error", error: true, sources: []}
  ];`);
  const pending = f.send("Follow-up");
  assert.deepEqual(f.requests[0].payload.messages, [
    { role: "user", content: "Original card" },
    { role: "assistant", content: "Explanation" },
    { role: "user", content: "Follow-up" },
  ]);
  f.requests[0].respond({ error: "Please try again", sources: [] });
  await pending;
  assert.equal(f.state().tutorMessages.at(-1).error, true);
});

test("A source-free latest answer hides older sources", () => {
  const f = fixture({ render: true });
  f.run(`state.tutorMessages = [
    {role: "assistant", content: "Earlier answer", sources: [{guid: "unrelated"}]},
    {role: "user", content: "New card"},
    {role: "assistant", content: "Please try again", sources: [], error: true}
  ]; renderTutorSources();`);
  assert.equal(f.node("#tutor-sources-rail").hidden, true);
  assert.equal(f.node("#tutor-sources-list").innerHTML, "");
});

test("Resetting a conversation clears the source rail even in the empty state", () => {
  const f = fixture({ render: true });
  f.run("resetTutor()");
  assert.equal(f.node("#tutor-sources-rail").hidden, true);
  assert.equal(f.node("#tutor-sources-list").innerHTML, "");
});

test("Old sources are hidden while a new answer is loading", () => {
  const f = fixture({ render: true });
  f.run(`state.tutorMessages = [
    {role: "assistant", content: "Old answer", sources: [{guid: "old"}]}
  ]; state.tutorPending = true; renderTutorSources();`);
  assert.equal(f.node("#tutor-sources-rail").hidden, true);
});

test("Thinking starts collapsed, escapes model text, and supports reopening", () => {
  const f = fixture();
  f.run(`globalThis.debug = {
    model: "qwen/qwen3.8-27b", total_ms: 2300, response_retries: 1,
    thinking: [{type: "reasoning", text: '<img src=x onerror="alert(1)">', attempt: 1},
      {type: "tool", text: 'search_deck(q="sucralfate") → 2 cards', attempt: 2}]
  };`);
  const html = f.run("renderTutorThinking(debug, false, 2)");
  assert.match(html, /<details class="tutor-thinking" data-message-index="2">/);
  assert.match(html, /Thinking/);
  assert.match(html, /2\.3s · 1 retry/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /Deck activity · Attempt 2/);
  assert.match(f.run("renderTutorThinking(debug, true, 2)"), /data-message-index="2" open/);
});

test("Unavailable reasoning is stated explicitly", () => {
  const f = fixture();
  const html = f.run('renderTutorThinking({model: "GPT-OSS", thinking: []})');
  assert.match(html, /did not return reasoning text/);
  assert.equal(f.run("renderTutorThinking(undefined)"), "");
});

test("Thinking is stored per reply and never replayed in follow-up requests", async () => {
  const f = fixture();
  const first = f.send("Card");
  const debug = {model: "GPT-OSS", thinking: [{type: "reasoning", text: "Reasoning only."}]};
  f.requests[0].respond({ answer: "Answer.", sources: [], debug });
  await first;
  assert.deepEqual(f.state().tutorMessages[1].debug, debug);
  const next = f.send("Follow-up");
  assert.deepEqual(f.requests[1].payload.messages, [
    {role: "user", content: "Card"}, {role: "assistant", content: "Answer."},
    {role: "user", content: "Follow-up"},
  ]);
  f.requests[1].respond({ error: "All attempts failed", debug: {thinking: [{type: "error", text: "Failed."}]} });
  await next;
  assert.equal(f.state().tutorMessages.at(-1).debug.thinking[0].type, "error");
});

test("Partial answer and reasoning arrive before completion without entering history", async () => {
  const f = fixture();
  const pending = f.send("Card");
  assert.equal(f.requests[0].payload.stream, true);
  const stream = f.requests[0].respondStream();
  stream.event("attempt", {attempt: 1, model: "GPT-OSS"});
  stream.event("reasoning_delta", {text: "Check ", iteration: 1, attempt: 1});
  stream.event("reasoning_delta", {text: "the card.", iteration: 1, attempt: 1});
  stream.event("answer_delta", {text: "First part", iteration: 1, attempt: 1});
  await new Promise(setImmediate);
  assert.equal(f.state().tutorPending, true);
  assert.equal(f.state().tutorLive.content, "First part");
  assert.equal(f.state().tutorLive.debug.thinking[0].text, "Check the card.");
  assert.equal(f.state().tutorMessages.length, 1);
  stream.event("done", {answer: "First part, completed.", sources: [], debug: {thinking: []}});
  await pending;
  assert.equal(f.state().tutorMessages[1].content, "First part, completed.");
  assert.equal(f.state().tutorLive, null);
  assert.equal(f.state().tutorPending, false);
});

test("SSE parser handles byte-split Unicode, CRLF, and heartbeat frames", async () => {
  const f = fixture();
  const pending = f.send("Card");
  const stream = f.requests[0].respondStream();
  const frames = ': keepalive\r\n\r\nevent: reasoning_delta\r\ndata: {"text":"β→","iteration":1}\r\n\r\n' +
    'event: done\r\ndata: {"answer":"Bicarbonate → protection β","sources":[]}\r\n\r\n';
  for (const byte of new TextEncoder().encode(frames)) stream.raw(new Uint8Array([byte]));
  await pending;
  assert.equal(f.state().tutorMessages[1].content, "Bicarbonate → protection β");
});

test("Retry clears incomplete answer while retaining earlier thinking", async () => {
  const f = fixture();
  const pending = f.send("Card");
  const stream = f.requests[0].respondStream();
  stream.event("reasoning_delta", {text: "Earlier reasoning.", iteration: 1, attempt: 1});
  stream.event("answer_delta", {text: "Failed partial"});
  stream.event("retry", {attempt: 1, step: {type: "retry", text: "Retrying…"}});
  stream.event("attempt", {attempt: 2, model: "Qwen"});
  stream.event("answer_delta", {text: "Replacement"});
  await new Promise(setImmediate);
  assert.equal(f.state().tutorLive.content, "Replacement");
  assert.equal(f.state().tutorLive.debug.response_retries, 1);
  assert.deepEqual(f.state().tutorLive.debug.thinking.map((step) => step.type), ["reasoning", "retry"]);
  stream.event("done", {answer: "Replacement", sources: []});
  await pending;
});

test("Interrupted stream becomes an error and never stores its partial answer", async () => {
  const f = fixture();
  const pending = f.send("Card");
  const stream = f.requests[0].respondStream();
  stream.event("answer_delta", {text: "Incomplete answer"});
  stream.close();
  await pending;
  assert.equal(f.state().tutorMessages[1].error, true);
  assert.doesNotMatch(f.state().tutorMessages[1].content, /Incomplete answer/);
  assert.equal(f.state().tutorLive, null);
});

test("Reset cancels an active stream and protects the next request", async () => {
  const f = fixture();
  const old = f.send("Old card");
  const stream = f.requests[0].respondStream();
  stream.event("answer_delta", {text: "Old partial"});
  await new Promise(setImmediate);
  f.run("resetTutor()");
  const current = f.send("New card");
  await old;
  assert.equal(f.state().tutorLive.content, "");
  assert.equal(f.state().tutorPending, true);
  assert.equal(f.state().tutorMessages[0].content, "New card");
  f.requests[1].respond({answer: "New answer", sources: []});
  await current;
  assert.equal(f.state().tutorMessages[1].content, "New answer");
});

test("Progress updates keep the disclosure stable and escape reasoning text", () => {
  const f = fixture();
  const answer = {textContent: ""};
  const meta = {textContent: ""};
  const body = {innerHTML: "", scrollTop: 12, scrollHeight: 100, clientHeight: 25};
  const children = {".tutor-stream-answer": answer, ".tutor-thinking-meta": meta, ".tutor-thinking-body": body};
  const list = f.node("#tutor-messages");
  const initialHtml = list.innerHTML;
  list.querySelector = () => ({querySelector: (selector) => children[selector]});
  f.run(`state.tutorLive = {content: "Partial <b>text</b>", thinkingOpen: true, debug: {
    streaming: true, thinking: [{type:"reasoning", text:"<script>unsafe</script>"}]
  }}; renderTutorProgress();`);
  assert.equal(answer.textContent, "Partial <b>text</b>");
  assert.match(body.innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(body.innerHTML, /<script>/);
  assert.equal(meta.textContent, "Generating…");
  assert.equal(body.scrollTop, 12);
  assert.equal(list.innerHTML, initialHtml);
});

test("AI assist streams reasoning and retains it with empty results", async () => {
  const f = fixture();
  const pending = f.run('state.mode = "fast"; runAssistedSearch("ulcer")');
  assert.equal(f.requests[0].payload.stream, true);
  const stream = f.requests[0].respondStream();
  stream.event("reasoning_delta", {text: "Search the topic.", phase: "plan", iteration: 1});
  await new Promise(setImmediate);
  assert.equal(f.state().searchDebug.thinking[0].text, "Search the topic.");
  stream.event("done", {results: [], plan: {}, debug: {thinking: [{type: "reasoning", text: "Search the topic."}]}});
  await pending;
  assert.equal(f.node("#search-thinking").hidden, false);
  assert.match(f.node("#search-thinking").innerHTML, /Search the topic/);
  assert.match(f.node("#results").innerHTML, /No cards matched/);
  assert.doesNotMatch(f.node("#search-thinking").innerHTML, /Generating…/);
});

test("Deep search keeps reasoning at the narrowing step", async () => {
  const f = fixture();
  const pending = f.run('state.mode = "fast"; runDeepSearch("ulcer")');
  f.requests[0].respond({status: "clarify", candidate_count: 121, message: "Narrow this search", facets: [],
    debug: {thinking: [{type: "reasoning", text: "Broad search rationale."}]}});
  await pending;
  assert.match(f.node("#search-thinking").innerHTML, /Broad search rationale/);
  assert.match(f.node("#results").innerHTML, /Screen all 121 anyway/);
});

test("Overlapping AI searches cancel old reasoning without replacing the new search", async () => {
  const f = fixture();
  const old = f.run('state.mode = "fast"; runAssistedSearch("old query")');
  const stream = f.requests[0].respondStream();
  stream.event("reasoning_delta", {text: "Old thought", phase: "plan", iteration: 1});
  await new Promise(setImmediate);
  const current = f.run('runAssistedSearch("new query")');
  await old;
  assert.equal(f.requests[0].signal.aborted, true);
  assert.equal(f.state().searchDebug.thinking.length, 0);
  f.requests[1].respond({results: [], plan: {}, debug: {thinking: [{type: "reasoning", text: "New thought"}]}});
  await current;
  assert.match(f.node("#search-thinking").innerHTML, /New thought/);
  assert.doesNotMatch(f.node("#search-thinking").innerHTML, /Old thought/);
});

test("Plain keyword search hides and invalidates earlier AI reasoning", async () => {
  const f = fixture();
  f.run('state.mode = "fast"; renderSearchThinking({thinking:[{type:"reasoning",text:"Old thought"}]})');
  f.node("#q").value = "ulcer";
  const pending = f.run("runSearch()");
  assert.equal(f.node("#search-thinking").hidden, true);
  assert.equal(f.state().searchDebug, null);
  f.requests[0].respond({results: [], mode: "fast"});
  await pending;
});

test("Concurrent screening reasoning stays grouped by batch", () => {
  const f = fixture();
  f.run(`renderSearchThinking({streaming:true, thinking:[]});
    applySearchEvent({event:"reasoning_delta",text:"Second ",phase:"screen",batch:2,iteration:1});
    applySearchEvent({event:"reasoning_delta",text:"First batch",phase:"screen",batch:1,iteration:1});
    applySearchEvent({event:"reasoning_delta",text:"batch",phase:"screen",batch:2,iteration:1});`);
  const steps = f.state().searchDebug.thinking;
  assert.equal(steps.length, 2);
  assert.equal(steps[0].text, "Second batch");
  assert.equal(steps[1].text, "First batch");
});

test("Search errors retain escaped reasoning and clear generating state", async () => {
  const f = fixture();
  const pending = f.run('state.mode = "fast"; runAssistedSearch("query")');
  const stream = f.requests[0].respondStream();
  stream.event("reasoning_delta", {text: "<script>sample</script>", phase: "plan", iteration: 1});
  await new Promise(setImmediate);
  stream.event("error", {error: "Search failed"});
  await pending;
  const html = f.node("#search-thinking").innerHTML;
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(html, /Generating…/);
  assert.match(f.node("#results").innerHTML, /Search failed/);
});

test("Failed screening batches show an incomplete-results notice", () => {
  const f = fixture();
  f.run(`renderSearchThinking({screen_errors:["Timeout"],thinking:[
    {type:"error", text:"This batch failed",phase:"screen",batch:2}
  ]});`);
  assert.match(f.node("#search-thinking").innerHTML, /results may be incomplete/);
  assert.match(f.node("#search-thinking").innerHTML, /Batch 2/);
});

test("Interrupted search stream retains reasoning without a generating indicator", async () => {
  const f = fixture();
  const pending = f.run('state.mode = "fast"; runAssistedSearch("query")');
  const stream = f.requests[0].respondStream();
  stream.event("reasoning_delta", {text: "Partial planning", phase: "plan", iteration: 1});
  stream.close();
  await pending;
  assert.match(f.node("#search-thinking").innerHTML, /Partial planning/);
  assert.doesNotMatch(f.node("#search-thinking").innerHTML, /Generating…/);
  assert.match(f.node("#results").innerHTML, /stream ended/);
});

function organizeFixture() {
  const f = fixture();
  f.node("#move-dialog").querySelector = (s) => f.node(`#move-dialog ${s}`);
  f.run("mountPreviewCards = () => {}; loadDecks = () => {}; ");
  return f;
}

const movePreview = (token) => ({preview_id: token, cards_to_move: 2, notes: 1,
  target: "GI", target_exists: true, from_decks: []});

test("Move confirmation sends the reviewed token and requires a new preview after failure", async () => {
  const f = organizeFixture();
  f.run('openMoveDialog({nids:[1]})');
  f.requests[0].respond({decks:["GI"]});
  f.node("#move-target").value = "GI";
  const preview = f.run("previewMove()");
  f.requests[1].respond(movePreview("reviewed"));
  await preview;
  const confirm = f.run("confirmMove()");
  assert.equal(f.requests[2].payload.preview_id, "reviewed");
  await f.run("confirmMove()"); // Double click cannot create another request.
  assert.equal(f.requests.length, 3);
  f.requests[2].respond({error:"Cards changed; preview again", preview_required:true}, 409);
  await confirm;
  assert.equal(f.run("moveUi.previewId"), null);
  assert.equal(f.node('#move-dialog [data-act="confirm"]').disabled, true);
  const again = f.run("previewMove()");
  f.requests[3].respond(movePreview("fresh"));
  await again;
  assert.equal(f.run("moveUi.previewId"), "fresh");
  assert.equal(f.node('#move-dialog [data-act="confirm"]').disabled, false);
});

test("Out-of-order move previews cannot restore an older confirmation token", async () => {
  const f = organizeFixture();
  f.run('openMoveDialog({nids:[1]})');
  f.requests[0].respond({decks:[]});
  f.node("#move-target").value = "GI";
  const old = f.run("previewMove()");
  const fresh = f.run("previewMove()");
  f.requests[2].respond(movePreview("fresh"));
  await fresh;
  f.requests[1].respond(movePreview("old"));
  await old;
  assert.equal(f.run("moveUi.previewId"), "fresh");
  f.node("#move-target").listeners.input();
  assert.equal(f.run("moveUi.previewId"), null);
  assert.equal(f.node('#move-dialog [data-act="confirm"]').disabled, true);
});

test("A preview from a closed move dialog cannot authorize another selection", async () => {
  const f = organizeFixture();
  f.run('openMoveDialog({nids:[1]})');
  f.requests[0].respond({decks:[]});
  f.node("#move-target").value = "GI";
  const old = f.run("previewMove()");
  f.node("#move-dialog").close();
  f.run('openMoveDialog({nids:[2]})');
  f.requests[2].respond({decks:[]});
  f.node("#move-target").value = "GI";
  f.requests[1].respond(movePreview("old"));
  await old;
  assert.equal(f.run("moveUi.previewId"), null);
});

test("Card confirmation sends its preview token and cannot be retried blindly", async () => {
  const f = organizeFixture();
  const opening = f.run('openCardActionDialog({action:"suspend",deck:"GI"})');
  f.requests[0].respond({preview_id:"card-token",cards_to_change:1,change_nids:[2]});
  await opening;
  const button = f.node('#move-dialog [data-act="confirm"]');
  assert.equal(button.disabled, false);
  const applying = button.listeners.click();
  assert.equal(f.requests[1].payload.preview_id, "card-token");
  await button.listeners.click();
  assert.equal(f.requests.length, 2);
  f.requests[1].respond({error:"Outcome uncertain; check Recent changes"}, 503);
  await applying;
  assert.equal(button.disabled, true);
  assert.equal(f.node('#move-dialog [data-act="preview"]').disabled, false);
  await button.listeners.click();
  assert.equal(f.requests.length, 2);
});

test("A late card preview cannot overwrite a newly opened dialog", async () => {
  const f = organizeFixture();
  const old = f.run('openCardActionDialog({action:"suspend",deck:"Old"})');
  f.node("#move-dialog").close();
  const fresh = f.run('openCardActionDialog({action:"suspend",deck:"New"})');
  f.requests[1].respond({preview_id:"new",cards_to_change:2,change_nids:[2]});
  await fresh;
  f.requests[0].respond({preview_id:"old",cards_to_change:99,change_nids:[1]});
  await old;
  assert.match(f.node("#move-preview").innerHTML, /<b>2<\/b>/);
  const applying = f.node('#move-dialog [data-act="confirm"]').listeners.click();
  assert.equal(f.requests[2].payload.preview_id, "new");
  assert.equal(f.requests[2].payload.deck, "New");
  f.requests[2].respond({changed:2,id:"saved"});
  await applying;
});

test("Uncertain history is visibly distinguished from confirmed changes", async () => {
  const f = organizeFixture();
  const loading = f.run("loadMoveHistory()");
  f.requests[0].respond({moves:[{id:"recover",status:"uncertain",cards:2,target:"GI",created:1}]});
  await loading;
  assert.match(f.node("#move-history-list").innerHTML, /Outcome unconfirmed/);
  assert.match(f.node("#move-history-list").innerHTML, /data-id="recover"/);
});
