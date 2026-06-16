// Anki Agent — minimal vanilla JS client.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  mode: "fast",
  query: "",
  activeTags: new Set(),   // holds full hierarchical tag strings (exact-match)
  lastReq: 0,
  tutorMessages: [],       // [{role: "user"|"assistant", content: string}]
  tutorPending: false,
  deck: "",                // selected deck/subdeck path ("" = whole index)
  aiAssist: false,         // Search mode: route through the AI planner
  deep: false,             // Search mode: route through Deep AI search
  deepIntent: "",          // the intent the current Deep run is screening
  deepRefinements: [],     // user narrowing details added during clarify
  lastIds: [],             // Anki note ids of the current result set (Copy all IDs)
};

const VALID_MODES = new Set(["fast", "tutor"]);

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function boot() {
  // Respect ?mode=fast or ?mode=tutor on page load.
  const qs = new URLSearchParams(window.location.search || "");
  const initialMode = qs.get("mode");
  if (initialMode && VALID_MODES.has(initialMode)) {
    state.mode = initialMode;
  }

  bindUi();
  applyModeUi();           // apply body class + active mode button
  updatePlaceholder();

  if (state.mode === "tutor") {
    renderTutor();
  }

  // Allow the launcher to reset the tutor conversation on window hide.
  window.resetTutor = resetTutor;

  await Promise.all([loadStats(), loadTags(), loadDecks()]);
}

function applyModeUi() {
  // Mark the current mode button active; others inactive. Links (anchors) pass
  // through untouched — only <button data-mode> elements participate.
  $$(".mode-btn").forEach((b) => {
    if (!b.dataset.mode) return;
    const active = b.dataset.mode === state.mode;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.body.classList.toggle("mode-tutor", state.mode === "tutor");
}

function bindUi() {
  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runCurrent();
  });

  const aiToggle = $("#ai-assist");
  const deepToggle = $("#deep-search");
  if (aiToggle) {
    aiToggle.addEventListener("change", () => {
      state.aiAssist = aiToggle.checked;
      // AI assist and Deep are mutually exclusive search depths.
      if (state.aiAssist && deepToggle?.checked) {
        deepToggle.checked = false;
        state.deep = false;
        $("#deep-label")?.classList.remove("on");
      }
      $("#ai-assist-label")?.classList.toggle("on", state.aiAssist);
      updatePlaceholder();
      if (!state.aiAssist) hideAssistPlan();
      const q = $("#q");
      if (q) { q.focus(); q.select(); }
    });
  }
  if (deepToggle) {
    deepToggle.addEventListener("change", () => {
      state.deep = deepToggle.checked;
      if (state.deep && aiToggle?.checked) {
        aiToggle.checked = false;
        state.aiAssist = false;
        $("#ai-assist-label")?.classList.remove("on");
      }
      $("#deep-label")?.classList.toggle("on", state.deep);
      // Starting fresh: forget any prior Deep narrowing context.
      state.deepIntent = "";
      state.deepRefinements = [];
      updatePlaceholder();
      if (!state.deep) hideAssistPlan();
      const q = $("#q");
      if (q) { q.focus(); q.select(); }
    });
  }

  const copyAll = $("#copy-all-ids");
  if (copyAll) {
    copyAll.addEventListener("click", async () => {
      if (!state.lastIds.length) return;
      // `nid:` search lists every note at once in the Anki browser. Anki
      // accepts a comma-separated list of note ids: nid:1,2,3
      const blob = "nid:" + state.lastIds.join(",");
      try {
        await navigator.clipboard.writeText(blob);
        const orig = copyAll.textContent;
        copyAll.textContent = `Copied ${state.lastIds.length}`;
        copyAll.classList.add("copied");
        setTimeout(() => {
          copyAll.textContent = orig;
          copyAll.classList.remove("copied");
        }, 1400);
      } catch { /* ignore */ }
    });
  }

  $$(".mode-btn").forEach((btn) => {
    if (!btn.dataset.mode) return;   // skip nav links
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      if (btn.dataset.mode === state.mode) return;
      state.mode = btn.dataset.mode;
      applyModeUi();
      if (state.mode === "tutor") {
        renderTutor();
      } else {
        // Leaving tutor: wipe the results area back to Search defaults.
        $("#results").innerHTML = "";
        $("#result-count").textContent = "";
        $("#result-timing").textContent = "";
        setCopyAllIds([]);
        hideAssistPlan();
      }
      updatePlaceholder();
      // tutor drives itself via its own composer.
      if (state.mode !== "tutor" && (state.query || state.activeTags.size)) {
        runCurrent();
      }
    });
  });

  document.addEventListener("keydown", (e) => {
    const active = document.activeElement;
    const inField = active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA");
    if (e.key === "/" && !inField) {
      e.preventDefault();
      if (state.mode === "tutor") {
        const ti = $("#tutor-input");
        if (ti) ti.focus();
      } else {
        $("#q").focus();
        $("#q").select();
      }
    }
    if (e.key === "Escape") {
      if (window.pywebview?.api?.hide) {
        window.pywebview.api.hide();
      } else if (active === $("#q")) {
        $("#q").value = "";
      }
    }
  });

  window.addEventListener("focus", () => {
    if (state.mode === "tutor") {
      const ti = $("#tutor-input");
      if (ti) ti.focus();
      return;
    }
    const q = $("#q");
    if (q) { q.focus(); q.select(); }
  });
}

async function loadStats() {
  try {
    const s = await fetch("/api/stats").then((r) => r.json());
    $("#stats").textContent = `${s.notes.toLocaleString()} notes · ${s.tags.toLocaleString()} tags`;
    const deckEl = $("#deck-name");
    if (deckEl) {
      const deck = (s.deck || "").trim();
      deckEl.textContent = deck || "Whole collection";
      deckEl.title = deck ? `Indexed deck: ${deck}` : "Indexed: whole collection";
      deckEl.hidden = false;
    }
  } catch (e) {
    console.error("stats failed", e);
  }
}

async function loadTags() {
  try {
    const items = await fetch("/api/sidebar_tags").then((r) => r.json());
    renderSidebarGroup("#sidebar-tags", items || []);
  } catch (e) {
    console.error("sidebar tags failed", e);
  }
}

function renderSidebarGroup(selector, items) {
  const list = $(selector);
  list.innerHTML = "";
  for (const t of items) {
    const row = document.createElement("div");
    row.className = "tag-row";
    row.title = t.tag;
    row.innerHTML = `<span class="tag-name">${escapeHtml(t.label)}</span>
                     <span class="tag-count">${t.count.toLocaleString()}</span>`;
    row.addEventListener("click", () => {
      toggleTagFilter(t.tag);
      runCurrent();
    });
    list.appendChild(row);
  }
}

// ---------------------------------------------------------------------------
// Deck / subdeck selector
// ---------------------------------------------------------------------------

async function loadDecks() {
  try {
    const tree = await fetch("/api/decks").then((r) => r.json());
    renderDeckTree(tree || []);
  } catch (e) {
    console.error("decks failed", e);
  }
}

function renderDeckTree(tree) {
  const section = $("#deck-section");
  const host = $("#deck-tree");
  if (!section || !host) return;
  // No deck metadata (e.g. a TSV export) -> hide the whole section.
  if (!tree.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  host.innerHTML = "";

  const all = document.createElement("div");
  all.className = "deck-row deck-all" + (state.deck ? "" : " active");
  all.dataset.deckAll = "1";
  all.innerHTML = `<span class="deck-caret deck-caret-empty"></span><span class="deck-name">All decks</span>`;
  all.addEventListener("click", () => selectDeck(""));
  host.appendChild(all);

  for (const node of tree) host.appendChild(buildDeckNode(node, 0));
}

function buildDeckNode(node, depth) {
  const wrap = document.createElement("div");
  wrap.className = "deck-node";

  const row = document.createElement("div");
  row.className = "deck-row" + (state.deck === node.path ? " active" : "");
  row.style.paddingLeft = `${8 + depth * 14}px`;
  row.title = node.path;
  row.dataset.deckPath = node.path;

  const hasKids = node.children && node.children.length;
  row.innerHTML = `
    <span class="deck-caret${hasKids ? "" : " deck-caret-empty"}">${hasKids ? "▸" : ""}</span>
    <span class="deck-name">${escapeHtml(node.label)}</span>
    <span class="deck-count">${node.count.toLocaleString()}</span>
  `;

  let childBox = null;
  if (hasKids) {
    childBox = document.createElement("div");
    childBox.className = "deck-children";
    childBox.hidden = true;
    for (const k of node.children) childBox.appendChild(buildDeckNode(k, depth + 1));
  }

  row.addEventListener("click", () => selectDeck(node.path));
  if (hasKids) {
    const caret = row.querySelector(".deck-caret");
    caret.addEventListener("click", (e) => {
      e.stopPropagation();           // expand/collapse without selecting
      const open = childBox.hidden;
      childBox.hidden = !open;
      caret.classList.toggle("open", open);
    });
  }

  wrap.appendChild(row);
  if (childBox) wrap.appendChild(childBox);
  return wrap;
}

function selectDeck(path) {
  state.deck = path || "";
  // Update highlight in place (re-rendering would lose expansion state).
  $$("#deck-tree .deck-row").forEach((r) => {
    const isActive = r.dataset.deckAll
      ? !state.deck
      : r.dataset.deckPath === state.deck;
    r.classList.toggle("active", isActive);
  });
  updateDeckPill();
  if (state.mode !== "tutor") runCurrent();
}

function updateDeckPill() {
  const el = $("#deck-pill");
  if (!el) return;
  if (!state.deck) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML =
    `<span class="deck-pill-label">Deck</span>` +
    `<span class="deck-pill-name">${escapeHtml(leafOf(state.deck))}</span>` +
    `<span class="x" title="Clear deck filter">×</span>`;
  el.title = state.deck;
  el.querySelector(".x").addEventListener("click", () => selectDeck(""));
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

function runCurrent() {
  // Tutor is conversational — it submits via its own composer, not the
  // (hidden) global search bar.
  if (state.mode === "tutor") return;
  return runSearch();
}

async function runSearch() {
  const q = $("#q").value.trim();
  state.query = q;

  if (state.deep) return runDeepSearch(q);
  if (state.aiAssist) return runAssistedSearch(q);

  if (!q && state.activeTags.size === 0 && !state.deck) {
    renderEmpty("Type a query, pick a deck, or click a tag.");
    return;
  }

  hideAssistPlan();
  renderLoading("Searching…");
  const reqId = ++state.lastReq;
  const started = performance.now();

  const params = new URLSearchParams({
    q,
    mode: state.mode,
    limit: "30",
  });
  if (state.activeTags.size) {
    params.set("tags", Array.from(state.activeTags).join(","));
  }
  if (state.deck) params.set("deck", state.deck);

  try {
    const res = await fetch(`/api/search?${params}`).then((r) => r.json());
    if (reqId !== state.lastReq) return;
    const elapsed = (performance.now() - started).toFixed(0);
    renderResults(res, elapsed);
  } catch (e) {
    if (reqId === state.lastReq) renderError(e);
  }
}

// AI-assisted search: a plain-English request -> tag/keyword plan, run
// server-side. Renders the chosen plan above the matching cards.
async function runAssistedSearch(q) {
  if (!q) {
    renderEmpty("Describe what you want — e.g. \"high-yield cards on a topic\".");
    hideAssistPlan();
    return;
  }
  renderLoading("AI is building your search…");
  const reqId = ++state.lastReq;
  const started = performance.now();
  try {
    const res = await fetch("/api/search_assist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q, deck: state.deck }),
    }).then((r) => r.json());
    if (reqId !== state.lastReq) return;
    const elapsed = (performance.now() - started).toFixed(0);
    renderAssisted(res, elapsed);
  } catch (e) {
    if (reqId === state.lastReq) renderError(e);
  }
}


// Deep AI search: pool candidates, then screen each card. May come back with
// a `clarify` step asking the user to narrow a broad search before screening.
async function runDeepSearch(q, { confirm = false, viaRefine = false } = {}) {
  if (!q) {
    renderEmpty("Describe what you want — Deep search reads and screens each card.");
    hideAssistPlan();
    return;
  }
  // A new intent (typed in the search bar) resets prior narrowing.
  if (!viaRefine && q !== state.deepIntent) {
    state.deepIntent = q;
    state.deepRefinements = [];
  }
  renderLoading(
    confirm ? "Screening every candidate card…" : "Deep search: pooling candidates…"
  );
  const reqId = ++state.lastReq;
  const started = performance.now();
  try {
    const res = await fetch("/api/deep_search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        q: state.deepIntent,
        refinements: state.deepRefinements,
        confirm,
        deck: state.deck,
      }),
    }).then((r) => r.json());
    if (reqId !== state.lastReq) return;
    const elapsed = (performance.now() - started).toFixed(0);
    if (res.status === "clarify") renderDeepClarify(res, elapsed);
    else renderDeepResults(res, elapsed);
  } catch (e) {
    if (reqId === state.lastReq) renderError(e);
  }
}

function renderDeepClarify(res, elapsedMs) {
  renderAssistPlan(res.plan || {});
  setCopyAllIds([]);
  $("#result-count").textContent = `${res.candidate_count} candidates`;
  $("#result-timing").textContent = `Deep · ${elapsedMs} ms`;

  const facets = (res.facets || [])
    .map(
      (f) =>
        `<button class="deep-facet" type="button" data-label="${escapeHtml(f.label)}" title="${escapeHtml(f.tag)}">${escapeHtml(f.label)} <span class="deep-facet-n">${f.count}</span></button>`
    )
    .join("");
  const applied = state.deepRefinements.length
    ? `<div class="deep-applied">Narrowing: ${state.deepRefinements
        .map((r) => `<span class="deep-applied-tag">${escapeHtml(r)}</span>`)
        .join("")}</div>`
    : "";

  const c = $("#results");
  c.innerHTML = `
    <div class="deep-clarify">
      <div class="deep-clarify-msg">${escapeHtml(res.message || "")}</div>
      ${facets ? `<div class="deep-facets">${facets}</div>` : ""}
      <form class="deep-refine" id="deep-refine-form" autocomplete="off">
        <input id="deep-refine-input" type="text"
               placeholder="Add a detail to narrow… (a subtopic, system, or what you want)" />
        <button type="submit">Refine</button>
      </form>
      ${applied}
      <button class="deep-screen-all" id="deep-screen-all" type="button">
        Screen all ${res.candidate_count} anyway
      </button>
    </div>
  `;

  const input = $("#deep-refine-input");
  c.querySelectorAll(".deep-facet").forEach((b) => {
    b.addEventListener("click", () => {
      const val = b.dataset.label || "";
      input.value = input.value.trim() ? `${input.value.trim()} ${val}` : val;
      input.focus();
    });
  });
  $("#deep-refine-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = input.value.trim();
    if (!v) return;
    state.deepRefinements.push(v);
    runDeepSearch(state.deepIntent, { viaRefine: true });
  });
  $("#deep-screen-all").addEventListener("click", () => {
    runDeepSearch(state.deepIntent, { confirm: true, viaRefine: true });
  });
  input.focus();
}

function renderDeepResults(res, elapsedMs) {
  const container = $("#results");
  const results = res.results || [];
  const debug = res.debug || {};

  if (res.status === "error" || debug.error) {
    hideAssistPlan();
    setCopyAllIds([]);
    $("#result-count").textContent = "";
    $("#result-timing").textContent = `Deep · ${elapsedMs} ms`;
    container.innerHTML =
      `<div class="error">Deep search failed: ${escapeHtml(String(debug.error || "error"))}</div>`;
    return;
  }

  renderAssistPlan(res.plan || {});
  setCopyAllIds(res.ids || results.map((n) => n.nid).filter(Boolean));
  const screened = debug.screened != null ? ` · screened ${debug.screened}` : "";
  $("#result-count").textContent =
    `${results.length} kept${res.candidate_count != null ? ` of ${res.candidate_count}` : ""}`;
  $("#result-timing").textContent = `Deep · ${elapsedMs} ms${screened}`;

  if (!results.length) {
    container.innerHTML =
      `<div class="empty">No cards passed screening. Try a broader intent or different wording.</div>`;
    return;
  }
  container.innerHTML = "";
  for (const note of results) {
    // Surface the screening rationale using the existing rationale styling.
    if (note.deep_why) note.llm_why = note.deep_why;
    container.appendChild(renderNote(note));
  }
}


function toggleTagFilter(fullTag) {
  if (state.activeTags.has(fullTag)) state.activeTags.delete(fullTag);
  else state.activeTags.add(fullTag);
  renderActiveTags();
  refreshSidebarActive();
}
function removeTagFilter(fullTag) {
  state.activeTags.delete(fullTag);
  renderActiveTags();
  refreshSidebarActive();
  runCurrent();
}
function renderActiveTags() {
  const box = $("#active-tags");
  box.innerHTML = "";
  for (const t of state.activeTags) {
    const el = document.createElement("span");
    el.className = "active-tag";
    el.innerHTML = `<span>${escapeHtml(leafOf(t))}</span><span class="x">×</span>`;
    el.title = t;
    el.addEventListener("click", () => removeTagFilter(t));
    box.appendChild(el);
  }
}
function refreshSidebarActive() {
  $$(".tag-row").forEach((row) => {
    row.classList.toggle("active", state.activeTags.has(row.title));
  });
}

function updatePlaceholder() {
  const input = $("#q");
  if (state.deep) {
    input.placeholder = "Deep AI search…  describe exactly what you want screened";
  } else if (state.aiAssist) {
    input.placeholder = "Describe what you want…  e.g. high-yield cards on a topic";
  } else {
    input.placeholder = "Search your deck…  try: keywords  ·  tag:Topic keyword  ·  \"exact phrase\"";
  }
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

// Fast-search empty/loading/error states. The tutor manages its own
// in-conversation states, so these only ever target plain #results.
function renderEmpty(msg) {
  $("#results").innerHTML = `<div class="empty">${escapeHtml(msg)}</div>`;
  $("#result-count").textContent = "";
  $("#result-timing").textContent = "";
}

function renderLoading(msg = "Searching…") {
  $("#results").innerHTML =
    `<div class="loading"><span class="spinner"></span>${escapeHtml(msg)}</div>`;
}

function renderError(e) {
  $("#results").innerHTML =
    `<div class="error">Error: ${escapeHtml(String(e))}</div>`;
}

function renderResults(res, elapsedMs) {
  const container = $("#results");
  const results = res.results || [];
  $("#result-count").textContent =
    `${results.length} result${results.length === 1 ? "" : "s"}`;
  $("#result-timing").textContent = `${res.mode} · ${elapsedMs} ms`;
  setCopyAllIds(results.map((n) => n.nid).filter(Boolean));

  if (!results.length) {
    container.innerHTML = `<div class="empty">No notes matched.</div>`;
    return;
  }

  container.innerHTML = "";
  for (const note of results) container.appendChild(renderNote(note));
}

// AI-assisted result render: show the plan the model built, then the cards.
// `res.guids` is the full matching set for the Copy-all-IDs button.
function renderAssisted(res, elapsedMs) {
  const container = $("#results");
  const results = res.results || [];
  const plan = res.plan || {};
  const debug = res.debug || {};
  const probes = Array.isArray(debug.tag_lookups) ? debug.tag_lookups.length : 0;

  if (debug.error) {
    hideAssistPlan();
    setCopyAllIds([]);
    $("#result-count").textContent = "";
    $("#result-timing").textContent = `AI · ${elapsedMs} ms`;
    container.innerHTML =
      `<div class="error">AI search failed: ${escapeHtml(String(debug.error))}</div>`;
    return;
  }

  renderAssistPlan(plan);
  setCopyAllIds(res.ids || results.map((n) => n.nid).filter(Boolean));
  $("#result-count").textContent =
    `${results.length} result${results.length === 1 ? "" : "s"}`;
  $("#result-timing").textContent =
    `AI · ${elapsedMs} ms · ${probes} tag probe${probes === 1 ? "" : "s"}`;

  if (!results.length) {
    container.innerHTML =
      `<div class="empty">No cards matched that. Try rephrasing, or uncheck AI assist for a raw search.</div>`;
    return;
  }
  container.innerHTML = "";
  for (const note of results) container.appendChild(renderNote(note));
}

function setCopyAllIds(ids) {
  state.lastIds = (ids || []).filter((x) => x != null && x !== "");
  const btn = $("#copy-all-ids");
  if (!btn) return;
  btn.hidden = state.lastIds.length === 0;
  btn.textContent = state.lastIds.length
    ? `Copy all IDs (${state.lastIds.length})`
    : "Copy all IDs";
}

function hideAssistPlan() {
  const el = $("#assist-plan");
  if (el) { el.hidden = true; el.innerHTML = ""; }
}

function renderAssistPlan(plan) {
  const el = $("#assist-plan");
  if (!el) return;
  const chips = [];
  const add = (label, val, cls) => {
    if (!val) return;
    chips.push(
      `<span class="plan-chip ${cls}"><b>${escapeHtml(label)}</b> ${escapeHtml(val)}</span>`
    );
  };
  (plan.tag_filters || []).forEach((t) => add("tag:", t, "plan-tag"));
  if ((plan.any_tags || []).length) {
    add("any tag:", plan.any_tags.join("  or  "), "plan-tag plan-or");
  }
  if (plan.keywords) add("text:", plan.keywords, "plan-kw");
  if ((plan.any_keywords || []).length) {
    add("any text:", plan.any_keywords.join("  or  "), "plan-kw plan-or");
  }

  if (!chips.length && !plan.explanation) { hideAssistPlan(); return; }
  el.hidden = false;
  el.innerHTML = `
    <div class="assist-plan-row">
      <span class="assist-plan-label">AI search</span>
      ${chips.join("")}
    </div>
    ${plan.explanation
      ? `<div class="assist-plan-note">${escapeHtml(plan.explanation)}</div>`
      : ""}
  `;
}

// ---------------------------------------------------------------------------
// Tutor mode — multi-turn tutor that grounds answers in the deck via agentic
// card search (search_deck + read_note) and cites the cards it used.
// ---------------------------------------------------------------------------

function renderTutor() {
  const results = $("#results");
  results.innerHTML = `
    <div class="tutor-shell" id="tutor-shell">
      <div class="tutor-main">
        <div class="tutor-toolbar">
          <span class="tutor-title">Tutor</span>
          <button class="tutor-new" type="button" title="Start a new conversation">New chat</button>
        </div>
        <div class="tutor-messages" id="tutor-messages"></div>
        <form class="tutor-composer" id="tutor-composer" autocomplete="off">
          <textarea id="tutor-input" rows="1"
                    placeholder="Message the tutor…  (Enter to send, Shift+Enter for newline)"
                    spellcheck="true"></textarea>
          <button type="submit" class="tutor-send" title="Send">Send</button>
        </form>
      </div>
      <aside class="tutor-sources-rail" id="tutor-sources-rail" hidden>
        <div class="tutor-sources-head">Sources</div>
        <div class="tutor-sources-list" id="tutor-sources-list"></div>
      </aside>
    </div>
  `;
  $("#result-count").textContent = "";
  $("#result-timing").textContent = "";

  const input = $("#tutor-input");
  autoGrow(input);
  input.addEventListener("input", () => autoGrow(input));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submitTutor();
    }
  });
  $("#tutor-composer").addEventListener("submit", (e) => {
    e.preventDefault();
    submitTutor();
  });
  results.querySelector(".tutor-new").addEventListener("click", () => resetTutor());

  renderTutorMessages();
  input.focus();
}

function autoGrow(ta) {
  if (!ta) return;
  ta.style.height = "auto";
  const max = 180;
  ta.style.height = Math.min(ta.scrollHeight, max) + "px";
}

function resetTutor() {
  state.tutorMessages = [];
  state.tutorPending = false;
  if (state.mode === "tutor") {
    renderTutorMessages();
    const input = $("#tutor-input");
    if (input) {
      input.value = "";
      autoGrow(input);
      input.focus();
    }
  }
}

function renderTutorMessages() {
  const list = $("#tutor-messages");
  if (!list) return;
  if (!state.tutorMessages.length && !state.tutorPending) {
    list.innerHTML = `
      <div class="tutor-empty">
        <div class="tutor-empty-title">Ask the tutor anything</div>
        <div class="tutor-empty-sub">
          The tutor searches your deck and grounds its answer in real cards,
          citing them inline — sources show on the right.
          Closing the launcher will start a fresh conversation.
        </div>
      </div>
    `;
    return;
  }
  const parts = [];
  const lastIdx = state.tutorMessages.length - 1;
  for (let i = 0; i < state.tutorMessages.length; i++) {
    const m = state.tutorMessages[i];
    const isLatest = !state.tutorPending && i === lastIdx;
    parts.push(renderTutorBubble(m.role, m.content, isLatest, m.sources));
  }
  if (state.tutorPending) {
    parts.push(`
      <div class="tutor-msg tutor-msg-assistant tutor-msg-latest">
        <div class="tutor-bubble">
          <div class="tutor-typing"><span></span><span></span><span></span></div>
        </div>
      </div>
    `);
  }
  list.innerHTML = parts.join("");
  renderTutorSources();
  wireTutorCitations(list);
  if (state.tutorPending) {
    // While the answer is loading, keep the question + typing dots in view.
    list.scrollTop = list.scrollHeight;
  } else {
    // Pin the top of the latest message to the top of the viewport so the
    // reader starts at the beginning of a long answer instead of its end.
    const latest = list.querySelector(".tutor-msg-latest");
    if (latest) {
      const TOP_GAP = 16; // small breathing room above the message
      const delta =
        latest.getBoundingClientRect().top -
        list.getBoundingClientRect().top -
        TOP_GAP;
      list.scrollTop += delta;
    } else {
      list.scrollTop = list.scrollHeight;
    }
  }
}

function renderTutorBubble(role, content, isLatest = false, sources) {
  let body;
  if (role === "assistant") {
    // Protect [N] refs as placeholders BEFORE markdown parsing, render, then
    // swap them for pills. Doing it after marked let adjacent refs like
    // [1][2] be parsed as reference-links and silently collapse to empty
    // pills. Only the latest answer's pills are interactive (they map to the
    // cards in the rail, which always reflects the latest answer).
    body = renderTutorMarkdown(content, sources);
    body = restoreCitations(body, { interactive: isLatest });
  } else {
    body = `<p>${escapeHtml(content).replace(/\n/g, "<br>")}</p>`;
  }
  const cls = role === "assistant" ? "tutor-msg-assistant" : "tutor-msg-user";
  const latestCls = isLatest ? " tutor-msg-latest" : "";
  return `
    <div class="tutor-msg ${cls}${latestCls}">
      <div class="tutor-bubble">${body}</div>
    </div>
  `;
}

// Citation placeholders use private-use-area sentinels so `marked` passes
// them through untouched (it won't treat them as Markdown the way it does
// raw [N] brackets). <ref> per ref.
function protectCitations(md, sources) {
  const validRefs = new Set((sources || []).map((s) => String(s.ref)));
  if (!validRefs.size) return md;
  return md.replace(/\[(\d+)\]/g, (m, n) =>
    validRefs.has(n) ? `${n}` : m
  );
}

// Swap citation placeholders in rendered HTML for pills. A run of adjacent
// refs is deduped and the pills are kept together so [1][1][2] renders as two
// tidy pills, not a wall of blue boxes. Interactive pills (latest answer
// only) carry a data-ref so a click can flash the matching source card.
function restoreCitations(html, { interactive } = {}) {
  return html.replace(/(?:\d+)+/g, (run) => {
    const seen = new Set();
    const pills = [];
    for (const match of run.matchAll(/(\d+)/g)) {
      const n = match[1];
      if (seen.has(n)) continue;
      seen.add(n);
      pills.push(
        interactive
          ? `<a href="#tutor-ref-${n}" class="cite" data-ref="${n}">${n}</a>`
          : `<span class="cite cite-inert">${n}</span>`
      );
    }
    return `<span class="cite-group">${pills.join("")}</span>`;
  });
}

// Wire citation-pill clicks (latest answer only) to scroll/flash the matching
// card in the sources rail.
function wireTutorCitations(scope) {
  scope.querySelectorAll(".cite[data-ref]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const card = $(`#tutor-sources-list [data-source-ref="${a.dataset.ref}"]`);
      if (!card) return;
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.remove("flash");
      void card.offsetWidth;   // restart animation
      card.classList.add("flash");
    });
  });
}

// The sources rail shows the cards behind the most recent answer. Hidden
// (and the shell collapses to a single column) until an answer has sources.
function renderTutorSources() {
  const shell = $("#tutor-shell");
  const rail = $("#tutor-sources-rail");
  const listEl = $("#tutor-sources-list");
  if (!shell || !rail || !listEl) return;

  let latest = null;
  for (let i = state.tutorMessages.length - 1; i >= 0; i--) {
    const m = state.tutorMessages[i];
    if (m.role === "assistant" && m.sources && m.sources.length) {
      latest = m;
      break;
    }
  }

  if (!latest) {
    shell.classList.remove("has-sources");
    rail.hidden = true;
    listEl.innerHTML = "";
    return;
  }

  shell.classList.add("has-sources");
  rail.hidden = false;
  listEl.innerHTML = "";
  for (const note of latest.sources) {
    const card = renderNote(note, { refLabel: note.ref, hideScore: true });
    card.dataset.sourceRef = note.ref;
    if (!note.cited) card.classList.add("source-uncited");
    listEl.appendChild(card);
  }
}

// Full Markdown → HTML via `marked` (vendored). GFM + soft line breaks.
// Falls back to the minimal inline parser if marked didn't load.
function renderTutorMarkdown(md, sources) {
  const protectedMd = protectCitations(md, sources);
  if (typeof marked !== "undefined" && marked.parse) {
    try {
      return marked.parse(protectedMd, { gfm: true, breaks: true });
    } catch (e) {
      console.warn("marked failed, falling back:", e);
    }
  }
  // Fallback parser does its own [N] -> pill pass on the raw markdown;
  // restoreCitations then no-ops since there are no placeholders.
  return markdownishToHtml(md, sources);
}

async function submitTutor() {
  if (state.tutorPending) return;
  const input = $("#tutor-input");
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;
  state.tutorMessages.push({ role: "user", content: text });
  input.value = "";
  autoGrow(input);
  state.tutorPending = true;
  renderTutorMessages();

  const reqId = ++state.lastReq;
  try {
    const res = await fetch("/api/tutor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: state.tutorMessages, deck: state.deck }),
    });
    const data = await res.json();
    if (reqId !== state.lastReq || state.mode !== "tutor") return;
    state.tutorPending = false;
    if (data.error) {
      state.tutorMessages.push({
        role: "assistant",
        content: `_Error: ${data.error}_`,
        sources: [],
      });
    } else {
      state.tutorMessages.push({
        role: "assistant",
        content: data.answer || "(no response)",
        sources: data.sources || [],
      });
    }
    renderTutorMessages();
    input.focus();
  } catch (e) {
    if (reqId !== state.lastReq) return;
    state.tutorPending = false;
    state.tutorMessages.push({
      role: "assistant",
      content: `_Network error: ${String(e)}_`,
    });
    renderTutorMessages();
  }
}

// Minimal Markdown → HTML for tutor answers. Supports inline code, bold/italic,
// bulleted lists, paragraph breaks, and the [n] citation markers.
function markdownishToHtml(md, sources) {
  const validRefs = new Set((sources || []).map((s) => s.ref));
  // Escape first, then unescape the citation markers we emit ourselves.
  let s = escapeHtml(md);
  // Inline code
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  // Bold + italic
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");

  // Citation pills (do this AFTER escape so the raw [1] survived)
  s = s.replace(/\[(\d+)\]/g, (m, n) => {
    if (!validRefs.has(n)) return m;
    return `<a href="#ref-${n}" class="cite" data-ref="${n}">${n}</a>`;
  });

  // Paragraphs, bullets, tables
  const blocks = s.split(/\n{2,}/);
  const htmlBlocks = blocks.map((blk) => {
    const lines = blk.split("\n");
    if (isTableBlock(lines)) return renderTableBlock(lines);
    const bulletLines = lines.filter((l) => /^\s*[-*]\s+/.test(l));
    if (bulletLines.length === lines.length && lines.length > 0) {
      const items = lines
        .map((l) => l.replace(/^\s*[-*]\s+/, ""))
        .map((l) => `<li>${l}</li>`)
        .join("");
      return `<ul>${items}</ul>`;
    }
    return `<p>${lines.join("<br>")}</p>`;
  });
  return htmlBlocks.join("");
}

function isTableBlock(lines) {
  const clean = lines.map((l) => l.trim()).filter(Boolean);
  if (clean.length < 2) return false;
  if (!clean.every((l) => l.includes("|"))) return false;
  return /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(clean[1]);
}

function renderTableBlock(lines) {
  const clean = lines.map((l) => l.trim()).filter(Boolean);
  const splitRow = (l) => {
    const cells = l.split("|");
    if (cells.length && cells[0].trim() === "") cells.shift();
    if (cells.length && cells[cells.length - 1].trim() === "") cells.pop();
    return cells.map((c) => c.trim());
  };
  const header = splitRow(clean[0]);
  const bodyRows = clean.slice(2).map(splitRow);
  const thead = `<thead><tr>${header.map((c) => `<th>${c}</th>`).join("")}</tr></thead>`;
  const tbody = `<tbody>${bodyRows
    .map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`)
    .join("")}</tbody>`;
  return `<div class="md-table-wrap"><table class="md-table">${thead}${tbody}</table></div>`;
}

function renderNote(note, opts = {}) {
  const el = document.createElement("article");
  el.className = "note";
  if (opts.refLabel) el.classList.add("note-with-ref");

  const tagChips = renderTags(note.tags || []);
  const score = opts.hideScore
    ? ""
    : (note.llm_score != null
        ? `llm ${Number(note.llm_score).toFixed(1)}`
        : (note.score ? `bm25 ${note.score.toFixed(2)}` : ""));

  const why = note.llm_why
    ? `<div class="llm-why" title="LLM rationale">${escapeHtml(note.llm_why)}</div>`
    : "";
  const snippetHtml = renderSnippets(note);

  const extrasBlock = note.extras_html && note.extras_html.length
    ? `<details class="note-extras">
         <summary>Extras (${note.extras_html.length})</summary>
         ${note.extras_html.map((h) => `<div class="extra-item">${h}</div>`).join("")}
       </details>`
    : "";

  const refBadge = opts.refLabel
    ? `<span class="ref-badge" title="Reference ${escapeHtml(opts.refLabel)}">${escapeHtml(opts.refLabel)}</span>`
    : "";

  // Deck breadcrumb (subdeck structure), clickable to scope the search to it.
  const deckLine = note.deck
    ? `<div class="note-deck" data-deck="${escapeHtml(note.deck)}" title="Filter to deck: ${escapeHtml(note.deck)}">
         ${escapeHtml(note.deck.split("::").join(" › "))}
       </div>`
    : "";

  // Anki's numeric note id (searchable as nid:<id>). Fall back to the guid
  // for TSV-sourced decks, which carry no note id.
  const noteId = note.nid ? String(note.nid) : (note.guid || "");

  el.innerHTML = `
    <header class="note-header">
      <div class="guid-wrap">
        ${refBadge}
        <span class="guid-label">Note ID</span>
        <span class="guid">${escapeHtml(noteId)}</span>
        <button class="copy-btn" type="button" data-copy="${escapeHtml(noteId)}">Copy</button>
      </div>
      <span class="score">${escapeHtml(score)}</span>
    </header>
    ${deckLine}
    <div class="note-tags">${tagChips}</div>
    ${why}
    ${snippetHtml}
    <div class="note-front">${note.front_html || escapeHtml(note.front || "")}</div>
    ${note.back_html ? `<div class="note-back">${note.back_html}</div>` : ""}
    ${extrasBlock}
  `;

  // Copy button
  const copyBtn = el.querySelector(".copy-btn");
  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(copyBtn.dataset.copy);
      copyBtn.classList.add("copied");
      copyBtn.textContent = "Copied";
      setTimeout(() => {
        copyBtn.classList.remove("copied");
        copyBtn.textContent = "Copy";
      }, 1200);
    } catch {
      /* ignore */
    }
  });

  // Tag chip clicks -> add exact-match filter
  el.querySelectorAll(".tag-chip[data-tag]").forEach((chip) => {
    chip.addEventListener("click", () => {
      toggleTagFilter(chip.dataset.tag);
      runCurrent();
    });
  });

  // Deck breadcrumb click -> scope the search to that deck.
  const deckEl = el.querySelector(".note-deck[data-deck]");
  if (deckEl) {
    deckEl.addEventListener("click", () => selectDeck(deckEl.dataset.deck));
  }

  // "+N more" expansion
  const more = el.querySelector(".tag-more");
  if (more) {
    more.addEventListener("click", () => {
      el.querySelectorAll(".tag-chip.tag-hidden").forEach((c) =>
        c.classList.remove("tag-hidden")
      );
      more.remove();
    });
  }

  // Collapse long content behind a clickable ellipsis fold.
  installEllipsisFold(el.querySelector(".note-front"));
  installEllipsisFold(el.querySelector(".note-back"));
  el.querySelectorAll(".note-extras .extra-item").forEach(installEllipsisFold);

  return el;
}

// If the element's rendered height exceeds COLLAPSE_PX, wrap it in a
// collapsible container with a fading ellipsis gradient and an "expand"
// affordance. Using rAF so measurements happen after layout.
const COLLAPSE_PX = 240;
function installEllipsisFold(el) {
  if (!el) return;
  requestAnimationFrame(() => {
    if (el.scrollHeight <= COLLAPSE_PX + 24) return;
    el.classList.add("collapsible", "collapsed");
    el.style.setProperty("--collapse-px", COLLAPSE_PX + "px");
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "fold-toggle";
    toggle.textContent = "Show more …";
    toggle.addEventListener("click", () => {
      const nowCollapsed = el.classList.toggle("collapsed");
      toggle.textContent = nowCollapsed ? "Show more …" : "Show less";
    });
    el.after(toggle);
  });
}

function renderSnippets(note) {
  // The server already inserts <mark> in snippets. Still run through a
  // minimal allowlist so we don't inject raw HTML.
  const pieces = [];
  if (note.snippet_front) pieces.push(sanitizeSnippet(note.snippet_front));
  if (note.snippet_back) pieces.push(sanitizeSnippet(note.snippet_back));
  if (!pieces.length) return "";
  return `<div class="snippet">${pieces.join(" · ")}</div>`;
}

// Allow only <mark> inside snippets.
function sanitizeSnippet(s) {
  return escapeHtml(s)
    .replaceAll("&lt;mark&gt;", "<mark>")
    .replaceAll("&lt;/mark&gt;", "</mark>");
}

function renderTags(tags) {
  if (!tags.length) return `<span class="tag-chip" style="opacity:0.6">no tags</span>`;

  // Show tags in their natural order, deduped by full tag string. We keep the
  // full tag for exact-match filtering and show the leaf segment as the label.
  const ordered = [];
  const seen = new Set();
  for (const t of tags) {
    if (seen.has(t)) continue;
    seen.add(t);
    ordered.push(t);
  }

  const MAX_VISIBLE = 8;
  const visible = ordered.slice(0, MAX_VISIBLE);
  const hidden = ordered.slice(MAX_VISIBLE);

  function chipFor(fullTag, extra = "") {
    const label = leafOf(fullTag);
    return `<span class="tag-chip${extra}"
                  data-tag="${escapeHtml(fullTag)}"
                  title="${escapeHtml(fullTag)}">${escapeHtml(label)}</span>`;
  }

  const chips = visible.map((t) => chipFor(t));
  for (const t of hidden) chips.push(chipFor(t, " tag-hidden"));
  if (hidden.length) {
    chips.push(`<span class="tag-more">+${hidden.length} more</span>`);
  }
  return chips.join("");
}

function leafOf(tag) {
  const idx = tag.lastIndexOf("::");
  return idx >= 0 ? tag.slice(idx + 2) : tag;
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Hide the "+N more" chips that are initially hidden via display:none but
// still counted. Handled inline above.

boot();
