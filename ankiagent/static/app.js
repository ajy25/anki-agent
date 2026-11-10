// Anki Agent — minimal vanilla JS client.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  mode: "fast",
  model: "",               // selected model, captured separately for each request
  query: "",
  activeTags: new Set(),   // holds full hierarchical tag strings (exact-match)
  lastReq: 0,
  tutorMessages: [],       // current chat tab's [{role, content}] (Tutor or Organize)
  chatStore: { tutor: [], agent: [] },   // each chat tab keeps its own conversation
  tutorPending: false,
  tutorAbort: null,        // cancels a request when its conversation is discarded
  tutorLive: null,         // partial output; only completed answers enter history
  searchAbort: null,
  searchDebug: null,
  searchThinkingOpen: false,
  deck: "",                // selected deck/subdeck path ("" = whole index)
  aiAssist: false,         // Search mode: route through the AI planner
  deep: false,             // Search mode: route through Deep AI search
  deepIntent: "",          // the intent the current Deep run is screening
  deepRefinements: [],     // user narrowing details added during clarify
  lastIds: [],             // Anki note ids of the current result set (bulk Anki search)
};

const VALID_MODES = new Set(["fast", "tutor", "agent"]);
// Tutor (read-only, cited answers) and Organize (prepares deck changes)
// share the chat UI; each keeps a separate conversation.
const isChat = (mode) => mode === "tutor" || mode === "agent";
const CHAT_UI = {
  tutor: {
    title: "Tutor", endpoint: "/api/tutor",
    placeholder: "Message the tutor…  (Enter to send, Shift+Enter for newline)",
    emptyTitle: "Ask the tutor anything",
    emptySub: `The tutor searches your deck and grounds its answer in real cards,
          citing them inline — sources show on the right.
          Closing the launcher will start a fresh conversation.`,
  },
  agent: {
    title: "Organize", endpoint: "/api/agent",
    placeholder: "Describe what to change, e.g. “move all Boards and Beyond GI cards to my GI deck”  (Enter to send)",
    emptyTitle: "Organize your Anki deck",
    emptySub: `Move, suspend, or unsuspend cards, e.g.
          “put all Boards and Beyond GI cards in my GI deck” or “in my GI deck,
          suspend everything except GERD”. You'll see every card in a preview
          and confirm before anything changes, and you can undo.
          Study questions go to the Tutor tab.`,
  },
};

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
  if (isChat(state.mode)) state.tutorMessages = state.chatStore[state.mode];

  bindUi();
  applyModeUi();           // apply body class + active mode button
  updatePlaceholder();

  if (isChat(state.mode)) {
    renderTutor();
  }

  // Allow the launcher to reset the tutor conversation on window hide.
  window.resetTutor = () => resetTutor({ all: true });

  await Promise.all([loadStats(), loadTags(), loadDecks()]);
  if (qs.get("settings") === "tutor") openTutorSettings();
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
  // Both chat tabs use the chat layout (mode-tutor); mode-agent marks the organizer.
  document.body.classList.toggle("mode-tutor", isChat(state.mode));
  document.body.classList.toggle("mode-agent", state.mode === "agent");
}

function setModel(model) {
  const select = $("#model-select");
  const option = Array.from(select.options).find((item) => item.value === model);
  if (!option) return false;
  select.value = model;
  state.model = model;
  $("#desktop-model-name").textContent = option.textContent;
  try { localStorage.setItem("ankiagent.model", model); } catch { /* ignore */ }
  return true;
}

function bindModelPicker() {
  const modelSelect = $("#model-select");
  try {
    const saved = localStorage.getItem("ankiagent.model");
    if (Array.from(modelSelect.options).some((option) => option.value === saved)) {
      modelSelect.value = saved;
    }
  } catch { /* Model selection still works when storage is unavailable. */ }
  setModel(modelSelect.value);
  modelSelect.addEventListener("change", () => setModel(modelSelect.value));
  // The native macOS menu calls this same setter. Both controls keep the
  // current conversation and leave any request already in progress alone.
  window.setModel = setModel;
  const useNativeMenu = () => {
    document.body.classList.add("desktop-mode");
    const setup = $("#setup-settings");
    if (setup) setup.hidden = false;
  };
  window.addEventListener("pywebviewready", useNativeMenu);
  if (window.pywebview?.api) useNativeMenu();
}

function bindUi() {
  bindTutorSettings();
  bindImageViewer();
  bindModelPicker();
  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runCurrent();
  });

  const aiToggle = $("#ai-assist");
  const deepToggle = $("#deep-search");
  if (aiToggle) {
    aiToggle.addEventListener("change", () => {
      cancelSearchRequest();
      hideSearchThinking();
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
      cancelSearchRequest();
      hideSearchThinking();
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

  $("#move-to-deck")?.addEventListener("click", () => {
    if (!state.lastIds.length) return;
    openMoveDialog({ nids: state.lastIds, label: state.query });
  });

  $$(".mode-btn").forEach((btn) => {
    if (!btn.dataset.mode) return;   // skip nav links
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      if (btn.dataset.mode === state.mode) return;
      if (isChat(state.mode)) cancelTutorRequest();
      else { cancelSearchRequest(); hideSearchThinking(); }
      state.mode = btn.dataset.mode;
      if (isChat(state.mode)) state.tutorMessages = state.chatStore[state.mode];
      applyModeUi();
      if (isChat(state.mode)) {
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
      if (!isChat(state.mode) && (state.query || state.activeTags.size)) {
        runCurrent();
      }
    });
  });

  document.addEventListener("keydown", (e) => {
    // Let the modal handle Escape without hiding the desktop app or clearing
    // search. Other app shortcuts must not steal focus from the viewer.
    if ($("#image-viewer").open || $("#move-dialog")?.open || $("#tutor-settings-dialog")?.open) return;
    const active = document.activeElement;
    const inField = active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName);
    if (active?.tagName === "SELECT" && e.key === "Escape") return;
    if (e.key === "/" && !inField) {
      e.preventDefault();
      if (isChat(state.mode)) {
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
    if ($("#image-viewer").open || $("#move-dialog")?.open || $("#tutor-settings-dialog")?.open) return;
    if (isChat(state.mode)) {
      const ti = $("#tutor-input");
      if (ti) ti.focus();
      return;
    }
    const q = $("#q");
    if (q) { q.focus(); q.select(); }
  });
}

function bindImageViewer() {
  const viewer = $("#image-viewer");
  viewer.querySelector(".image-viewer-close").addEventListener("click", () => viewer.close());
  viewer.addEventListener("click", (event) => {
    if (event.target === viewer) viewer.close();
  });
  viewer.addEventListener("close", () => {
    document.body.classList.remove("image-viewer-open");
    viewer.querySelector("img").removeAttribute("src");
  });
}

function openCardImage(img) {
  if (!img.naturalWidth) return;
  const viewer = $("#image-viewer");
  const expanded = viewer.querySelector("img");
  expanded.src = img.currentSrc || img.src;
  expanded.alt = img.alt || "Card image";
  // A modal dialog keeps keyboard focus inside and returns it to this image
  // on close. The original card and its scroll position stay in place.
  img.focus({ preventScroll: true });
  viewer.showModal();
  document.body.classList.add("image-viewer-open");
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
    <span class="deck-count" title="Unique notes in this deck and its subdecks">${node.count.toLocaleString()}</span>
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
  if (!isChat(state.mode)) runCurrent();
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
  if (isChat(state.mode)) return;
  return runSearch();
}

async function runSearch() {
  const q = $("#q").value.trim();
  state.query = q;

  if (state.deep) return runDeepSearch(q);
  if (state.aiAssist) return runAssistedSearch(q);

  cancelSearchRequest();
  hideSearchThinking();
  hideAssistPlan();

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
    cancelSearchRequest();
    hideSearchThinking();
    renderEmpty("Describe what you want — e.g. \"high-yield cards on a topic\".");
    hideAssistPlan();
    return;
  }
  const {reqId, controller} = beginSearchThinking();
  renderLoading("AI is building your search…");
  const started = performance.now();
  try {
    const res = await fetch("/api/search_assist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({ q, deck: state.deck, model: state.model, stream: true }),
    }).then((response) => readSearchResponse(response, reqId));
    if (reqId !== state.lastReq) return;
    const elapsed = (performance.now() - started).toFixed(0);
    renderAssisted(res, elapsed);
  } catch (e) {
    if (reqId === state.lastReq && e.name !== "AbortError") {
      renderSearchThinking({...state.searchDebug, streaming: false, error: String(e)});
      renderError(e);
    }
  } finally {
    if (reqId === state.lastReq) state.searchAbort = null;
  }
}


// Deep AI search: pool candidates, then screen each card. May come back with
// a `clarify` step asking the user to narrow a broad search before screening.
async function runDeepSearch(q, { confirm = false, viaRefine = false } = {}) {
  if (!q) {
    cancelSearchRequest();
    hideSearchThinking();
    renderEmpty("Describe what you want — Deep search reads and screens each card.");
    hideAssistPlan();
    return;
  }
  // A new intent (typed in the search bar) resets prior narrowing.
  if (!viaRefine && q !== state.deepIntent) {
    state.deepIntent = q;
    state.deepRefinements = [];
  }
  const {reqId, controller} = beginSearchThinking();
  renderLoading(
    confirm ? "Screening every candidate card…" : "Deep search: pooling candidates…"
  );
  const started = performance.now();
  try {
    const res = await fetch("/api/deep_search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({
        q: state.deepIntent,
        refinements: state.deepRefinements,
        confirm,
        deck: state.deck,
        model: state.model,
        stream: true,
      }),
    }).then((response) => readSearchResponse(response, reqId));
    if (reqId !== state.lastReq) return;
    const elapsed = (performance.now() - started).toFixed(0);
    if (res.status === "clarify") renderDeepClarify(res, elapsed);
    else renderDeepResults(res, elapsed);
  } catch (e) {
    if (reqId === state.lastReq && e.name !== "AbortError") {
      renderSearchThinking({...state.searchDebug, streaming: false, error: String(e)});
      renderError(e);
    }
  } finally {
    if (reqId === state.lastReq) state.searchAbort = null;
  }
}

function renderDeepClarify(res, elapsedMs) {
  renderSearchThinking(res.debug || {}, elapsedMs);
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
  renderSearchThinking(debug, elapsedMs);

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
  renderSearchThinking(debug, elapsedMs);
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

function cancelSearchRequest() {
  ++state.lastReq;
  state.searchAbort?.abort();
  state.searchAbort = null;
}

function hideSearchThinking() {
  state.searchDebug = null;
  state.searchThinkingOpen = false;
  const container = $("#search-thinking");
  if (container) { container.hidden = true; container.innerHTML = ""; }
}

function beginSearchThinking() {
  cancelSearchRequest();
  hideSearchThinking();
  hideAssistPlan();
  setCopyAllIds([]);
  $("#result-count").textContent = "";
  $("#result-timing").textContent = "";
  const controller = new AbortController();
  state.searchAbort = controller;
  renderSearchThinking({model: state.model, thinking: [], streaming: true});
  return {reqId: state.lastReq, controller};
}

function renderSearchThinking(debug, elapsedMs) {
  const container = $("#search-thinking");
  if (!container) return;
  const scroll = container.querySelector(".tutor-thinking-body")?.scrollTop || 0;
  const thinking = Array.isArray(debug.thinking) ? [...debug.thinking] : [];
  if (debug.error) thinking.push({type: "error", text: String(debug.error)});
  state.searchDebug = {...debug, thinking, total_ms: debug.total_ms || Number(elapsedMs) || 0};
  container.hidden = false;
  container.innerHTML = renderTutorThinking(state.searchDebug, state.searchThinkingOpen);
  const body = container.querySelector(".tutor-thinking-body");
  if (body) body.scrollTop = scroll;
  const panel = container.querySelector("details");
  panel?.addEventListener("toggle", () => {
    if (panel.isConnected) state.searchThinkingOpen = panel.open;
  });
  if (debug.screen_errors?.length) {
    container.insertAdjacentHTML("beforeend", '<div class="search-thinking-warning">Some screening batches failed. These results may be incomplete; expand Thinking for details.</div>');
  }
}

function applySearchEvent(event) {
  const debug = state.searchDebug;
  if (!debug) return;
  if (event.event === "reasoning_delta") {
    const previous = debug.thinking.find((step) => step.type === "reasoning" &&
      step.phase === event.phase && step.batch === event.batch && step.iteration === event.iteration);
    if (previous) previous.text += event.text || "";
    else debug.thinking.push({type: "reasoning", text: event.text || "",
      phase: event.phase, batch: event.batch, iteration: event.iteration});
  } else if (event.event === "tool") {
    debug.thinking.push(event.step);
  }
}

function renderSearchProgress() {
  const container = $("#search-thinking");
  const body = container?.querySelector(".tutor-thinking-body");
  if (!body || !state.searchDebug) return;
  const scroll = body.scrollTop;
  const follow = body.scrollHeight - scroll - body.clientHeight < 30;
  body.innerHTML = renderTutorThinkingBody(state.searchDebug);
  body.scrollTop = follow ? body.scrollHeight : scroll;
}

async function readSearchResponse(response, reqId) {
  let timer = null;
  try {
    const data = response.headers?.get("content-type")?.includes("text/event-stream")
      ? await readTutorStream(response, (event) => {
          if (reqId !== state.lastReq || state.mode !== "fast") return;
          applySearchEvent(event);
          if (timer === null) timer = setTimeout(() => {
            timer = null;
            if (reqId === state.lastReq && state.mode === "fast") renderSearchProgress();
          }, 50);
        })
      : await response.json();
    if (data.error) data.debug = {...(data.debug || state.searchDebug), streaming: false,
      error: data.debug?.error || data.error};
    return data;
  } finally {
    clearTimeout(timer);
  }
}

function setCopyAllIds(ids) {
  state.lastIds = (ids || []).filter((x) => x != null && x !== "");
  const btn = $("#copy-all-ids");
  if (!btn) return;
  btn.hidden = state.lastIds.length === 0;
  btn.textContent = state.lastIds.length
    ? `Copy Anki search (${state.lastIds.length})`
    : "Copy Anki search";
  const move = $("#move-to-deck");
  if (move) {
    move.hidden = state.lastIds.length === 0;
    move.textContent = state.lastIds.length ? `Move ${state.lastIds.length} to deck…` : "Move to deck…";
  }
}

// ---------------------------------------------------------------------------
// Move cards to a deck (AnkiConnect). Flow: pick destination -> Preview ->
// Confirm -> Undo available. Nothing is written before the Confirm click.
// ---------------------------------------------------------------------------

let moveDialogGeneration = 0;
const moveUi = { nids: [], label: "", previewed: "", previewId: null, request: 0, busy: false, lastMoveId: null };

async function postJson(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let data = {};
  try { data = await r.json(); } catch { /* non-JSON error page */ }
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}

function moveStatus(html, kind = "") {
  const el = $("#move-status");
  if (!el) return;
  el.className = `move-status ${kind}`;
  el.innerHTML = html;
}

function openMoveDialog({ nids, target = "", label = "" }) {
  const generation = ++moveDialogGeneration;
  moveUi.nids = [...nids];
  moveUi.label = label || "";
  moveUi.previewed = "";
  moveUi.previewId = null;
  moveUi.busy = false;
  moveUi.lastMoveId = null;
  const n = moveUi.nids.length;
  const dlg = $("#move-dialog");
  dlg.innerHTML = `
    <div class="move-form">
      <h2>Move ${n} note${n === 1 ? "" : "s"} to a deck</h2>
      <p class="move-sub">Every card of these notes leaves its current deck. Review history
        and scheduling move with them, and you can undo.</p>
      <label class="move-label" for="move-target">Destination deck</label>
      <input id="move-target" list="move-deck-list" autocomplete="off" spellcheck="false"
             placeholder="Pick a deck or type a new name, e.g. GI or Step 1::GI" />
      <datalist id="move-deck-list"></datalist>
      <div id="move-status" class="move-status" aria-live="polite"></div>
      <div id="move-preview" class="move-preview"></div>
      <div id="move-cards"></div>
      <div class="move-actions">
        <button type="button" class="move-secondary" data-act="close">Close</button>
        <button type="button" class="move-secondary" data-act="preview">Preview</button>
        <button type="button" class="move-primary" data-act="confirm" disabled>Move cards</button>
      </div>
      <details class="move-history">
        <summary>Recent changes</summary>
        <div id="move-history-list" class="move-history-list"></div>
      </details>
    </div>`;
  const input = $("#move-target");
  input.value = target;
  const confirmBtn = dlg.querySelector('[data-act="confirm"]');
  input.addEventListener("input", () => {
    // Any edit invalidates the preview; the user must preview the new target.
    moveUi.previewed = "";
    moveUi.previewId = null;
    ++moveUi.request;
    confirmBtn.disabled = true;
    $("#move-preview").innerHTML = "";
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); previewMove(); }
  });
  dlg.querySelector('[data-act="close"]').addEventListener("click", () => dlg.close());
  dlg.querySelector('[data-act="preview"]').addEventListener("click", previewMove);
  confirmBtn.addEventListener("click", confirmMove);
  dlg.querySelector(".move-history").addEventListener("toggle", (e) => {
    if (e.target.open) loadMoveHistory();
  });
  dlg.showModal();
  input.focus();
  mountPreviewCards($("#move-cards"), moveUi.nids, "Cards that will move");

  fetch("/api/anki/decks").then(async (r) => {
    const data = await r.json();
    if (generation !== moveDialogGeneration || !dlg.open) return;
    if (!r.ok) throw new Error(data.error || "Couldn't load your Anki decks.");
    $("#move-deck-list").innerHTML = (data.decks || [])
      .map((d) => `<option value="${escapeHtml(d)}"></option>`).join("");
  }).catch((e) => {
    if (generation === moveDialogGeneration && dlg.open) moveStatus(escapeHtml(e.message), "error");
  });

  if (target) previewMove();
}

// Every card in a pending change, as compact rows (front text + image
// thumbnails) that expand to the full card. Loads PREVIEW_PAGE at a time.
const PREVIEW_PAGE = 50;

function mountPreviewCards(container, nids, title) {
  const section = document.createElement("section");
  section.className = "preview-cards";
  section.innerHTML = `
    <h3 class="preview-cards-title">${escapeHtml(title)}
      <span class="muted">${nids.length} note${nids.length === 1 ? "" : "s"}</span></h3>
    <div class="preview-cards-list"></div>
    <p class="preview-cards-missing muted" hidden></p>
    <button type="button" class="move-secondary preview-cards-more" hidden></button>`;
  container.appendChild(section);
  const list = section.querySelector(".preview-cards-list");
  const more = section.querySelector(".preview-cards-more");
  const missingEl = section.querySelector(".preview-cards-missing");
  let offset = 0;
  let missing = 0;
  async function loadPage() {
    const page = nids.slice(offset, offset + PREVIEW_PAGE);
    offset += page.length;
    more.disabled = true;
    more.textContent = "Loading…";
    try {
      const r = await postJson("/api/notes", { nids: page });
      for (const note of r.notes || []) list.appendChild(renderPreviewCard(note));
      missing += (r.missing || []).length;
    } catch (e) {
      missingEl.hidden = false;
      missingEl.textContent = e.message;
    }
    const left = nids.length - offset;
    more.hidden = left <= 0;
    more.disabled = false;
    more.textContent = `Show ${Math.min(left, PREVIEW_PAGE)} more (${left} left)`;
    if (missing) {
      missingEl.hidden = false;
      missingEl.textContent = `${missing} note${missing === 1 ? " isn't" : "s aren't"} in this app's search index, so ${missing === 1 ? "it" : "they"} can't be shown. Restart the app to re-index.`;
    }
  }
  more.addEventListener("click", loadPage);
  if (nids.length) loadPage();
  else list.innerHTML = `<p class="muted">None.</p>`;
}

function renderPreviewCard(note) {
  const row = document.createElement("div");
  row.className = "preview-card";
  // Sanitized server HTML; a <template> is inert, so nothing loads here.
  const tpl = document.createElement("template");
  tpl.innerHTML = [note.front_html, note.back_html, ...(note.extras_html || [])].join("");
  const images = [...tpl.content.querySelectorAll("img")].slice(0, 6);
  const text = (note.front || "").trim();
  row.innerHTML = `
    <div class="preview-card-main">
      <div class="preview-card-text">${text ? escapeHtml(text.length > 220 ? text.slice(0, 219) + "…" : text) : '<span class="muted">(no text)</span>'}</div>
      <div class="preview-card-thumbs"></div>
    </div>
    <button type="button" class="move-undo preview-card-toggle" aria-expanded="false">Full card</button>
    <div class="preview-card-full" hidden></div>`;
  const thumbs = row.querySelector(".preview-card-thumbs");
  for (const source of images) {
    const img = document.createElement("img");
    img.src = source.getAttribute("src") || "";
    img.alt = source.getAttribute("alt") || "";
    img.loading = "lazy";
    img.className = "preview-thumb";
    img.title = "Click to expand";
    img.addEventListener("click", () => openCardImage(img));
    img.addEventListener("error", () => img.remove(), { once: true });
    thumbs.appendChild(img);
  }
  const toggle = row.querySelector(".preview-card-toggle");
  const full = row.querySelector(".preview-card-full");
  toggle.addEventListener("click", () => {
    if (!full.firstChild) full.appendChild(renderNote(note, { hideScore: true, inDialog: true }));
    full.hidden = !full.hidden;
    toggle.textContent = full.hidden ? "Full card" : "Hide card";
    toggle.setAttribute("aria-expanded", String(!full.hidden));
  });
  return row;
}

// Suspend / unsuspend proposal from Organize: fresh preview from Anki, then
// the user confirms. Undo and history are shared with moves.
async function openCardActionDialog(p, msg) {
  const generation = ++moveDialogGeneration;
  let previewId = null;
  let busy = false;
  const verb = p.action === "suspend" ? "Suspend" : "Unsuspend";
  const spec = { action: p.action, deck: p.deck || "", nids: p.nids ?? null, exclude_nids: p.exclude_nids || [] };
  const dlg = $("#move-dialog");
  dlg.innerHTML = `
    <div class="move-form">
      <h2>${verb} cards${p.deck ? ` in ${escapeHtml(p.deck)}` : ""}</h2>
      <p class="move-sub">${p.action === "suspend"
        ? "Suspended cards stay in your deck with their history but won't show up in reviews until you unsuspend them."
        : "Unsuspended cards return to your reviews with their history intact."} You can undo.</p>
      <div id="move-status" class="move-status" aria-live="polite"></div>
      <div id="move-preview" class="move-preview"></div>
      <div id="move-cards"></div>
      <div class="move-actions">
        <button type="button" class="move-secondary" data-act="close">Close</button>
        <button type="button" class="move-secondary" data-act="preview" disabled>Preview again</button>
        <button type="button" class="move-primary" data-act="confirm" disabled>${verb} cards</button>
      </div>
      <details class="move-history">
        <summary>Recent changes</summary>
        <div id="move-history-list" class="move-history-list"></div>
      </details>
    </div>`;
  const confirmBtn = dlg.querySelector('[data-act="confirm"]');
  const previewBtn = dlg.querySelector('[data-act="preview"]');
  const current = () => generation === moveDialogGeneration && dlg.open;
  previewBtn.addEventListener("click", () => {
    if (busy) return;
    dlg.close();
    openCardActionDialog(p, msg);
  });
  dlg.querySelector('[data-act="close"]').addEventListener("click", () => dlg.close());
  dlg.querySelector(".move-history").addEventListener("toggle", (e) => { if (e.target.open) loadMoveHistory(); });
  dlg.showModal();

  moveStatus("Checking with Anki…");
  try {
    const r = await postJson("/api/cards/preview", spec);
    if (!current()) return;
    previewId = r.preview_id;
    moveStatus("");
    $("#move-preview").innerHTML = `
      <p class="move-summary"><b>${r.cards_to_change}</b> card${r.cards_to_change === 1 ? "" : "s"} will be ${p.action}ed.</p>
      ${p.except_selection ? `<p class="move-note">Keeping <b>${r.kept}</b> card${r.kept === 1 ? "" : "s"} about “${escapeHtml(p.except_selection)}”.</p>` : ""}
      ${r.already ? `<p class="move-note">${r.already} card${r.already === 1 ? " is" : "s are"} already ${p.action}ed.</p>` : ""}`;
    const cards = $("#move-cards");
    if (p.except_selection) mountPreviewCards(cards, r.kept_nids || [], `Cards that stay ${p.action === "suspend" ? "active" : "suspended"} (“${p.except_selection}”)`);
    mountPreviewCards(cards, r.change_nids || [], `Cards that will be ${p.action}ed`);
    confirmBtn.disabled = !previewId || r.cards_to_change === 0;
    confirmBtn.textContent = r.cards_to_change ? `${verb} ${r.cards_to_change} cards` : "Nothing to change";
  } catch (e) {
    if (!current()) return;
    moveStatus(escapeHtml(e.message), "error");
  }
  previewBtn.disabled = false;

  confirmBtn.addEventListener("click", async () => {
    if (busy || !previewId || !current()) return;
    busy = true;
    const token = previewId;
    previewId = null;
    confirmBtn.disabled = true;
    previewBtn.disabled = true;
    moveStatus(`${verb === "Suspend" ? "Suspending" : "Unsuspending"} cards in Anki…`);
    try {
      const r = await postJson("/api/cards/apply", { ...spec, preview_id: token, label: msg?.content?.slice(0, 200) || "" });
      if (!current()) return;
      $("#move-preview").innerHTML = "";
      $("#move-cards").innerHTML = "";
      moveStatus(r.changed
        ? `${p.action === "suspend" ? "Suspended" : "Unsuspended"} <b>${r.changed}</b> card${r.changed === 1 ? "" : "s"}.
           <button type="button" class="move-undo" data-id="${escapeHtml(r.id)}">Undo</button>`
        : "Nothing needed changing.", "ok");
      $("#move-status .move-undo")?.addEventListener("click", (e) => undoMove(e.target.dataset.id));
      if ($("#move-dialog .move-history")?.open) loadMoveHistory();
    } catch (e) {
      if (!current()) return;
      moveStatus(escapeHtml(e.message), "error");
      if ($("#move-dialog .move-history")?.open) loadMoveHistory();
    } finally {
      busy = false;
      if (current()) previewBtn.disabled = false;
    }
  });
}

async function previewMove() {
  if (moveUi.busy) return;
  const generation = moveDialogGeneration;
  const request = ++moveUi.request;
  const dlg = $("#move-dialog");
  const target = $("#move-target").value.trim();
  const confirmBtn = $('#move-dialog [data-act="confirm"]');
  const current = () => generation === moveDialogGeneration && request === moveUi.request
    && dlg.open && $("#move-target").value.trim() === target;
  moveUi.previewed = "";
  moveUi.previewId = null;
  confirmBtn.disabled = true;
  if (!target) { moveStatus("Choose a destination deck first.", "error"); return; }
  moveStatus("Checking with Anki…");
  confirmBtn.disabled = true;
  try {
    const p = await postJson("/api/move/preview", { nids: moveUi.nids, target });
    if (!current()) return;
    moveStatus("");
    const from = (p.from_decks || []).slice(0, 6).map((d) =>
      `<li><span>${escapeHtml(d.deck)}</span><b>${d.cards}</b></li>`).join("");
    const more = (p.from_decks || []).length > 6
      ? `<li class="muted">…and ${p.from_decks.length - 6} more decks</li>` : "";
    const notes = [];
    if (!p.target_exists) notes.push(`<b>${escapeHtml(p.target)}</b> doesn't exist yet. It will be created.`);
    if (p.already_in_target) notes.push(`${p.already_in_target} card${p.already_in_target === 1 ? " is" : "s are"} already there and will be skipped.`);
    if (p.missing_notes) notes.push(`${p.missing_notes} note${p.missing_notes === 1 ? " wasn't" : "s weren't"} found in Anki (deleted since indexing?).`);
    if (p.outside_index_scope) notes.push(`This deck is outside <b>${escapeHtml(p.index_scope)}</b>, the deck this app indexes. After the app re-indexes, these cards won't show in search here. To keep them searchable, choose a parent deck or the whole collection in Setup.`);
    $("#move-preview").innerHTML = `
      <p class="move-summary"><b>${p.cards_to_move}</b> card${p.cards_to_move === 1 ? "" : "s"}
        from <b>${p.notes}</b> note${p.notes === 1 ? "" : "s"} will move to
        <b>${escapeHtml(p.target)}</b>.</p>
      ${from ? `<p class="move-from-label">Currently in:</p><ul class="move-from">${from}${more}</ul>` : ""}
      ${notes.map((t) => `<p class="move-note">${t}</p>`).join("")}`;
    moveUi.previewed = target;
    moveUi.previewId = p.preview_id;
    confirmBtn.disabled = !moveUi.previewId || p.cards_to_move === 0;
    confirmBtn.textContent = p.cards_to_move ? `Move ${p.cards_to_move} cards` : "Nothing to move";
  } catch (e) {
    if (current()) moveStatus(escapeHtml(e.message), "error");
  }
}

async function confirmMove() {
  if (moveUi.busy) return;
  const generation = moveDialogGeneration;
  const current = () => generation === moveDialogGeneration && $("#move-dialog").open;
  const target = $("#move-target").value.trim();
  const confirmBtn = $('#move-dialog [data-act="confirm"]');
  if (!current()) return;
  if (!target || target !== moveUi.previewed || !moveUi.previewId) { previewMove(); return; }
  const token = moveUi.previewId;
  moveUi.previewId = null;
  moveUi.previewed = "";
  moveUi.busy = true;
  confirmBtn.disabled = true;
  moveStatus("Moving cards in Anki…");
  try {
    const r = await postJson("/api/move", { nids: moveUi.nids, target, preview_id: token, label: moveUi.label });
    if (!current()) return;
    moveUi.lastMoveId = r.id;
    moveUi.previewed = "";
    $("#move-preview").innerHTML = "";
    $("#move-cards").innerHTML = "";
    confirmBtn.textContent = "Move cards";
    moveStatus(r.moved
      ? `Moved <b>${r.moved}</b> card${r.moved === 1 ? "" : "s"} to <b>${escapeHtml(r.target)}</b>.
         <button type="button" class="move-undo" data-id="${escapeHtml(r.id)}">Undo</button>`
      : "Those cards were already in that deck. Nothing changed.", "ok");
    $("#move-status .move-undo")?.addEventListener("click", (e) => undoMove(e.target.dataset.id));
    loadDecks();
    if ($("#move-dialog .move-history")?.open) loadMoveHistory();
  } catch (e) {
    if (!current()) return;
    moveStatus(escapeHtml(e.message), "error");
    if ($("#move-dialog .move-history")?.open) loadMoveHistory();
  } finally {
    if (generation === moveDialogGeneration) moveUi.busy = false;
  }
}

async function undoMove(id) {
  moveStatus("Putting cards back…");
  try {
    const r = await postJson(`/api/moves/${encodeURIComponent(id)}/undo`, {});
    const skipped = r.skipped
      ? ` ${r.skipped} card${r.skipped === 1 ? " was" : "s were"} left alone because ${r.skipped === 1 ? "it was" : "they were"} moved again since.` : "";
    moveStatus(`Undone: <b>${r.restored}</b> card${r.restored === 1 ? "" : "s"} put back.${skipped}`, "ok");
    loadDecks();
    if ($("#move-dialog .move-history")?.open) loadMoveHistory();
  } catch (e) {
    moveStatus(escapeHtml(e.message), "error");
  }
}

async function loadMoveHistory() {
  const list = $("#move-history-list");
  if (!list) return;
  list.textContent = "Loading…";
  try {
    const { moves } = await fetch("/api/moves").then((r) => r.json());
    if (!moves?.length) { list.innerHTML = `<p class="muted">No changes yet.</p>`; return; }
    list.innerHTML = moves.map((m) => {
      const when = new Date((m.created || 0) * 1000).toLocaleString();
      const from = (m.from_decks || []).join(", ");
      const action = m.status === "undone"
        ? `<span class="muted">Undone</span>`
        : `<button type="button" class="move-undo" data-id="${escapeHtml(m.id)}">Undo</button>`;
      return `<div class="move-history-row">
        <div>${m.action === "suspend" || m.action === "unsuspend"
            ? `<b>${m.cards}</b> cards ${m.action}ed${m.target ? ` in <b>${escapeHtml(m.target)}</b>` : ""}`
            : `<b>${m.cards}</b> cards → <b>${escapeHtml(m.target)}</b>`}
          <div class="muted" title="${escapeHtml(from)}">${escapeHtml(when)}${m.label ? ` · “${escapeHtml(m.label)}”` : ""}</div></div>
        ${m.status === "pending" || m.status === "uncertain" ? '<span class="muted">Outcome unconfirmed — recovery available</span>' : ""}
        ${action}</div>`;
    }).join("");
    list.querySelectorAll(".move-undo").forEach((b) =>
      b.addEventListener("click", () => undoMove(b.dataset.id)));
  } catch {
    list.textContent = "Couldn't load move history.";
  }
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
          <span class="tutor-title">${CHAT_UI[state.mode].title}</span>
          ${state.mode === "tutor" ? `<div class="tutor-controls">
            <label for="tutor-text-size">Text size</label>
            <select id="tutor-text-size">
              <option value="0.75">Small</option><option value="1">Default</option>
              <option value="1.25">Large</option><option value="1.5">Extra large</option>
            </select>
            <button type="button" id="tutor-open-settings">Settings</button>
          </div>` : ""}
        </div>
        ${state.mode === "agent" ? `
        <section class="organize-connection" id="organize-connection" aria-label="Anki connection">
          <p id="organize-connection-status" role="status" aria-live="polite">Checking Anki connection…</p>
          <div id="organize-connection-help" hidden>
            <p>Organize needs Anki running with the AnkiConnect add-on to move, suspend, or unsuspend cards.</p>
            <ol>
              <li>In Anki, open <b>Tools → Add-ons → Get Add-ons…</b></li>
              <li>Paste <code>2055492159</code> and install <a href="https://ankiweb.net/shared/info/2055492159" target="_blank" rel="noopener noreferrer">AnkiConnect</a>.</li>
              <li>Restart Anki and open the profile you want to organize. Leave Anki open, then check again.</li>
            </ol>
            <p>Already installed? Open Anki, or update AnkiConnect under Tools → Add-ons if needed. Search and Tutor work without it.</p>
          </div>
          <button type="button" id="organize-check-again" disabled>Checking…</button>
        </section>` : ""}
        <div class="tutor-messages" id="tutor-messages"></div>
        <form class="tutor-composer" id="tutor-composer" autocomplete="off">
          <div class="tutor-composer-box">
            <textarea id="tutor-input" rows="1"
                      placeholder="${escapeHtml(CHAT_UI[state.mode].placeholder)}"
                      spellcheck="true"></textarea>
            <button type="button" class="tutor-send" id="tutor-send"></button>
          </div>
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
  input.addEventListener("input", () => { autoGrow(input); updateComposerButton(); });
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
  // One button, two jobs (as in ChatGPT / Claude): Send when idle, Stop while
  // the reply is generating.
  $("#tutor-send").addEventListener("click", () => {
    if (state.tutorPending) stopTutor();
    else submitTutor();
  });

  renderTutorMessages();
  if (state.mode === "tutor") {
    applyTutorTextScale(readTutorTextScale());
    $("#tutor-text-size").addEventListener("change", (event) => setTutorTextScale(event.target.value));
    $("#tutor-open-settings").addEventListener("click", openTutorSettings);
  }
  if (state.mode === "agent") {
    $("#organize-check-again").addEventListener("click", checkOrganizeConnection);
    checkOrganizeConnection();
  }
  input.focus();
}

const TUTOR_TEXT_SCALES = [0.75, 1, 1.25, 1.5];

function readTutorTextScale() {
  try {
    const value = Number(localStorage.getItem("ankiagent.tutorTextScale"));
    return TUTOR_TEXT_SCALES.includes(value) ? value : 1;
  } catch { return 1; }
}

function applyTutorTextScale(value) {
  if (state.mode === "tutor") {
    $("#tutor-shell")?.style.setProperty("--tutor-text-scale", String(value));
    const picker = $("#tutor-text-size");
    if (picker) picker.value = String(value);
  }
  const settingsPicker = $("#tutor-settings-size");
  if (settingsPicker) settingsPicker.value = String(value);
}

function setTutorTextScale(raw) {
  const value = Number(raw);
  if (!TUTOR_TEXT_SCALES.includes(value)) return false;
  try { localStorage.setItem("ankiagent.tutorTextScale", String(value)); } catch { /* Still resize this view. */ }
  applyTutorTextScale(value);
  return true;
}

let tutorSettingsRequest = 0;
let tutorPromptDefaults = null;

function bindTutorSettings() {
  window.openTutorSettings = openTutorSettings;
  $("#tutor-settings-close").addEventListener("click", () => $("#tutor-settings-dialog").close());
  $("#tutor-settings-size").addEventListener("change", (event) => setTutorTextScale(event.target.value));
  $("#tutor-prompt-reset").addEventListener("click", () => {
    if (!tutorPromptDefaults) return;
    $("#tutor-system-prompt").value = tutorPromptDefaults.system_prompt;
    $("#tutor-first-turn").value = tutorPromptDefaults.first_turn_format;
    $("#tutor-settings-status").textContent = "Defaults restored in the editor. Click Save instructions to apply.";
  });
  $("#tutor-settings-form").addEventListener("submit", saveTutorSettings);
}

async function openTutorSettings() {
  const dialog = $("#tutor-settings-dialog");
  if (dialog.open) return;
  dialog.showModal();
  applyTutorTextScale(readTutorTextScale());
  const request = ++tutorSettingsRequest;
  tutorPromptDefaults = null;
  $("#tutor-prompt-fields").disabled = true;
  $("#tutor-system-prompt").value = "";
  $("#tutor-first-turn").value = "";
  $("#tutor-settings-status").textContent = "Loading Tutor instructions…";
  try {
    const response = await fetch("/api/tutor/settings", {cache: "no-store"});
    const data = await response.json();
    if (request !== tutorSettingsRequest || !dialog.open) return;
    if (!response.ok) throw new Error(data.error || "Couldn't load Tutor settings. Close and reopen to try again.");
    tutorPromptDefaults = data.defaults;
    $("#tutor-system-prompt").value = data.settings.system_prompt;
    $("#tutor-first-turn").value = data.settings.first_turn_format;
    $("#tutor-prompt-fields").disabled = false;
    $("#tutor-settings-status").textContent = "";
  } catch (error) {
    if (request === tutorSettingsRequest && dialog.open) $("#tutor-settings-status").textContent = error.message;
  }
}

async function saveTutorSettings(event) {
  event.preventDefault();
  const request = ++tutorSettingsRequest;
  const dialog = $("#tutor-settings-dialog");
  $("#tutor-prompt-fields").disabled = true;
  $("#tutor-settings-status").textContent = "Saving instructions…";
  try {
    const response = await fetch("/api/tutor/settings", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({system_prompt: $("#tutor-system-prompt").value,
        first_turn_format: $("#tutor-first-turn").value}),
    });
    const data = await response.json();
    if (request !== tutorSettingsRequest || !dialog.open) return;
    if (!response.ok) throw new Error(data.error || "Couldn't save Tutor settings. Please try again.");
    $("#tutor-settings-status").textContent = "Saved. Your next Tutor message will use these instructions.";
  } catch (error) {
    if (request === tutorSettingsRequest && dialog.open) $("#tutor-settings-status").textContent = error.message;
  } finally {
    if (request === tutorSettingsRequest && dialog.open) $("#tutor-prompt-fields").disabled = false;
  }
}

let organizeConnectionCheck = 0;

async function checkOrganizeConnection() {
  if (state.mode !== "agent") return;
  const banner = $("#organize-connection");
  if (!banner) return;
  const status = $("#organize-connection-status");
  const help = $("#organize-connection-help");
  const button = $("#organize-check-again");
  const request = ++organizeConnectionCheck;
  const current = () => request === organizeConnectionCheck && state.mode === "agent"
    && $("#organize-connection") === banner;
  banner.dataset.state = "checking";
  status.textContent = "Checking Anki connection…";
  button.hidden = false;
  button.disabled = true;
  button.textContent = "Checking…";
  try {
    const response = await fetch("/api/anki/status", {cache: "no-store"});
    const data = await response.json();
    if (!current()) return;
    if (!response.ok || data.connected !== true) {
      throw new Error(data.error || "Couldn't check the Anki connection. Make sure Anki is open, then try again.");
    }
    banner.dataset.state = "connected";
    status.textContent = "Anki is connected. Organize is ready.";
    help.hidden = true;
    button.hidden = true;
  } catch (error) {
    if (!current()) return;
    banner.dataset.state = "unavailable";
    status.textContent = error.message || "Couldn't check the Anki connection. Please try again.";
    help.hidden = false;
  } finally {
    if (current()) {
      button.disabled = false;
      button.textContent = "Check again";
    }
  }
}

function autoGrow(ta) {
  if (!ta) return;
  ta.style.height = "auto";
  const max = 180;
  ta.style.height = Math.min(ta.scrollHeight, max) + "px";
}

const SEND_ICON = `<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const STOP_ICON = `<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2.5" fill="currentColor"/></svg>`;

function updateComposerButton() {
  const btn = $("#tutor-send");
  if (!btn) return;
  const stopping = state.tutorPending;
  const mode = stopping ? "stop" : "send";
  if (btn.dataset.mode !== mode) {
    btn.dataset.mode = mode;
    btn.innerHTML = stopping ? STOP_ICON : SEND_ICON;
    btn.classList.toggle("is-stop", stopping);
    btn.title = stopping ? "Stop generating" : "Send message";
    btn.setAttribute("aria-label", btn.title);
  }
  btn.disabled = !stopping && !$("#tutor-input")?.value.trim();
}

// Stop button: abort the in-flight reply but keep whatever streamed so far,
// the way ChatGPT and Claude do. Organize only prepares proposals (nothing
// changes in Anki until the user confirms), so stopping mid-reply is safe.
function stopTutor() {
  if (!state.tutorPending) return;
  const live = state.tutorLive;
  cancelTutorRequest();
  const debug = live?.debug;
  if (debug) {
    debug.streaming = false;
    if (!debug.total_ms && live.startedAt) debug.total_ms = Date.now() - live.startedAt;
  }
  const partial = (live?.content || "").trim();
  state.tutorMessages.push({
    role: "assistant",
    content: partial,
    sources: [],
    debug,
    thinkingOpen: live?.thinkingOpen,
    stopped: true,
    // An empty stopped turn is display-only; a partial answer stays in the
    // conversation so a follow-up like "continue" has context.
    error: !partial,
  });
  renderTutorMessages();
  $("#tutor-input")?.focus();
}

function cancelTutorRequest() {
  ++state.lastReq;
  state.tutorAbort?.abort();
  state.tutorAbort = null;
  state.tutorPending = false;
  state.tutorLive = null;
}

// Clears the conversation in the current chat tab. The launcher calls this on
// window hide, which starts both chat tabs fresh.
function resetTutor({ all = false } = {}) {
  cancelTutorRequest();
  $("#image-viewer").close();
  if (all) state.chatStore = { tutor: [], agent: [] };
  if (isChat(state.mode)) {
    state.chatStore[state.mode] = [];
    state.tutorMessages = state.chatStore[state.mode];
  }
  if (isChat(state.mode)) {
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
  const previousScroll = list.scrollTop;
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  const thinkingScroll = new Map(Array.from(list.querySelectorAll(".tutor-thinking-body"),
    (body) => [body.parentElement.dataset.messageIndex, body.scrollTop]));
  renderTutorSources();
  updateComposerButton();
  if (!state.tutorMessages.length && !state.tutorPending) {
    list.innerHTML = `
      <div class="tutor-empty">
        <div class="tutor-empty-title">${CHAT_UI[state.mode]?.emptyTitle || ""}</div>
        <div class="tutor-empty-sub">
          ${CHAT_UI[state.mode]?.emptySub || ""}
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
    parts.push(renderTutorBubble(m.role, m.content, isLatest, m.sources, m.debug, m.thinkingOpen, i, false, m.proposals, m.stopped));
  }
  if (state.tutorPending) {
    const live = state.tutorLive;
    parts.push(renderTutorBubble("assistant", live?.content || "", true, [],
      live?.debug, live?.thinkingOpen, state.tutorMessages.length, true));
  }
  list.innerHTML = parts.join("");
  wireTutorCitations(list);
  list.querySelectorAll(".move-proposal-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const msg = state.tutorMessages[Number(btn.dataset.messageIndex)];
      const p = msg?.proposals?.[Number(btn.dataset.proposal)];
      if (!p) return;
      // Label the history entry with the student's request (the turn before).
      const request = state.tutorMessages[Number(btn.dataset.messageIndex) - 1];
      if (p.action === "undo") runUndoProposal(p);
      else if (p.action === "suspend" || p.action === "unsuspend") openCardActionDialog(p, request);
      else openMoveDialog({ nids: p.nids, target: p.target, label: p.selection });
    });
  });
  list.querySelectorAll(".tutor-thinking").forEach((panel) => {
    const index = Number(panel.dataset.messageIndex);
    const message = state.tutorMessages[index] || state.tutorLive;
    const body = panel.querySelector(".tutor-thinking-body");
    if (body) body.scrollTop = thinkingScroll.get(panel.dataset.messageIndex) || 0;
    panel.addEventListener("toggle", () => {
      if (panel.isConnected && message) message.thinkingOpen = panel.open;
    });
  });
  if (state.tutorPending) {
    // While the answer is loading, keep the question + typing dots in view.
    list.scrollTop = nearBottom && !state.tutorLive?.thinkingOpen ? list.scrollHeight : previousScroll;
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

function renderTutorBubble(role, content, isLatest = false, sources, debug, thinkingOpen = false, messageIndex = 0, streaming = false, proposals = [], stopped = false) {
  let body;
  if (streaming) {
    // Incomplete Markdown and citations become interactive only on completion.
    body = `<div class="tutor-stream-answer">${escapeHtml(content)}</div>
      <div class="tutor-typing" aria-label="Generating answer"><span></span><span></span><span></span></div>`;
  } else if (role === "assistant") {
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
    <div class="tutor-msg ${cls}${latestCls}"${streaming ? " data-tutor-live" : ""}>
      <div class="tutor-bubble">${role === "assistant" ? renderTutorThinking(debug, thinkingOpen, messageIndex) : ""}${body}${renderMoveProposals(proposals, messageIndex)}${stopped ? '<div class="tutor-stopped">Stopped</div>' : ""}</div>
    </div>
  `;
}

// A card move the agent prepared. The button opens the same preview/confirm
// dialog as "Move to deck…"; nothing moves until the user confirms there.
function describeChange(c) {
  const cards = `<b>${Number(c.cards) || 0}</b> card${c.cards === 1 ? "" : "s"}`;
  const what = c.action === "suspend" || c.action === "unsuspend"
    ? `${cards} ${c.action}ed${c.target ? ` in <b>${escapeHtml(c.target)}</b>` : ""}`
    : `${cards} moved to <b>${escapeHtml(c.target || "")}</b>`;
  return `${what}${c.label ? `<div class="move-proposal-sel">“${escapeHtml(c.label)}”</div>` : ""}`;
}

function renderUndoProposal(p, i, messageIndex) {
  const items = (p.changes || []).map((c) => `<li>${describeChange(c)}</li>`).join("");
  return `
    <div class="move-proposal move-proposal-undo">
      <div class="move-proposal-text">
        Undo ${p.changes.length} change${p.changes.length === 1 ? "" : "s"}:
        <ul class="move-proposal-changes">${items}</ul>
        ${p.result ? `<div class="move-proposal-result">${p.result}</div>` : ""}
      </div>
      <button type="button" class="move-primary move-proposal-btn"
              data-message-index="${messageIndex}" data-proposal="${i}"
              ${p.done ? "disabled" : ""}>${p.done ? "Undone" : "Review &amp; undo…"}</button>
    </div>`;
}

// Undo the changes an Organize proposal names, newest first, after the
// student confirms. Uses the same undo endpoint as the move history.
async function runUndoProposal(p) {
  if (p.done || p.running) return;
  const list = (p.changes || []).map((c) => "• " + (c.action === "move" || !c.action
    ? `${c.cards} cards moved to ${c.target}` : `${c.cards} cards ${c.action}ed${c.target ? ` in ${c.target}` : ""}`)).join("\n");
  if (!window.confirm(`Undo these changes in Anki?\n\n${list}`)) return;
  p.running = true;
  p.result = "Undoing…";
  renderTutorMessages();
  const lines = [];
  let failed = false;
  for (const c of p.changes || []) {
    try {
      const r = await postJson(`/api/moves/${encodeURIComponent(c.id)}/undo`, {});
      const skipped = r.skipped ? ` (${r.skipped} left alone: changed again since)` : "";
      lines.push(`Undone: ${r.restored} card${r.restored === 1 ? "" : "s"} restored${skipped}.`);
    } catch (e) {
      failed = true;
      lines.push(`Couldn't undo one change: ${escapeHtml(e.message)}`);
    }
  }
  p.running = false;
  p.done = !failed;
  p.result = lines.join("<br>");
  loadDecks();
  renderTutorMessages();
}

function renderMoveProposals(proposals, messageIndex) {
  if (!proposals?.length) return "";
  return proposals.map((p, i) => p.action === "undo" ? renderUndoProposal(p, i, messageIndex)
    : (p.action === "suspend" || p.action === "unsuspend") ? `
    <div class="move-proposal">
      <div class="move-proposal-text">
        ${p.action === "suspend" ? "Suspend" : "Unsuspend"} <b>${p.count}</b> card${p.count === 1 ? "" : "s"}${p.deck ? ` in <b>${escapeHtml(p.deck)}</b>` : ""}
        ${p.except_selection ? `<div class="move-proposal-sel">Keeping ${p.kept} card${p.kept === 1 ? "" : "s"} about “${escapeHtml(p.except_selection)}”</div>` : ""}
        ${p.selection ? `<div class="move-proposal-sel">${escapeHtml(p.selection)}</div>` : ""}
      </div>
      <button type="button" class="move-primary move-proposal-btn"
              data-message-index="${messageIndex}" data-proposal="${i}">Review &amp; ${p.action}…</button>
    </div>` : `
    <div class="move-proposal">
      <div class="move-proposal-text">
        <b>${p.count}</b> note${p.count === 1 ? "" : "s"} → <b>${escapeHtml(p.target)}</b>${p.target_exists ? "" : ' <span class="move-proposal-new">new deck</span>'}
        <div class="move-proposal-sel">${escapeHtml(p.selection)}</div>
      </div>
      <button type="button" class="move-primary move-proposal-btn"
              data-message-index="${messageIndex}" data-proposal="${i}">Review &amp; move…</button>
    </div>`).join("");
}

function renderTutorThinking(debug, open = false, messageIndex = 0) {
  if (!debug) return "";
  return `<details class="tutor-thinking" data-message-index="${messageIndex}"${open ? " open" : ""}>
    <summary><span>Thinking</span><span class="tutor-thinking-meta">${tutorThinkingMeta(debug)}</span></summary>
    <div class="tutor-thinking-body">${renderTutorThinkingBody(debug)}</div>
  </details>`;
}

function tutorThinkingMeta(debug) {
  const elapsed = Number(debug.total_ms);
  const duration = Number.isFinite(elapsed) && elapsed > 0 ? `${(elapsed / 1000).toFixed(1)}s` : "";
  const retries = Number(debug.response_retries) || 0;
  return [debug.streaming ? "Generating…" : duration, retries ? `${retries} ${retries === 1 ? "retry" : "retries"}` : ""].filter(Boolean).join(" · ");
}

function renderTutorThinkingBody(debug) {
  const steps = Array.isArray(debug.thinking) ? debug.thinking : [];
  const hasReasoning = steps.some((step) => step.type === "reasoning");
  const retries = Number(debug.response_retries) || 0;
  const labels = { reasoning: "Reasoning", tool: "Deck activity", retry: "Retry", error: "Failed attempt" };
  const items = steps.map((step) => {
    const kind = Object.hasOwn(labels, step.type) ? step.type : "tool";
    const attempt = retries ? ` · Attempt ${Number(step.attempt) || 1}` : "";
    const phase = {plan: "Search planning", screen: "Screening"}[step.phase] || "";
    const batch = step.batch ? ` · Batch ${Number(step.batch)}` : "";
    return `<li class="tutor-thinking-step tutor-thinking-${kind}">
      <div class="tutor-thinking-label">${labels[kind]}${attempt}${phase ? ` · ${phase}` : ""}${batch}</div>
      <div class="tutor-thinking-text">${escapeHtml(String(step.text || ""))}</div>
    </li>`;
  }).join("");
  return `
      <div class="tutor-thinking-caption">${escapeHtml(String(debug.model || "Model"))} · Reasoning and activity</div>
      ${hasReasoning ? "" : `<div class="tutor-thinking-unavailable">${debug.streaming ? "No reasoning text received yet." : "The model did not return reasoning text for this reply."}</div>`}
      ${items ? `<ol class="tutor-thinking-steps">${items}</ol>` : ""}
  `;
}

function renderTutorProgress() {
  const live = state.tutorLive;
  const list = $("#tutor-messages");
  const bubble = list?.querySelector("[data-tutor-live] .tutor-bubble");
  if (!live || !bubble) return;
  const follow = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  // Keep the disclosure and its focus stable while updating only its contents.
  bubble.querySelector(".tutor-stream-answer").textContent = live.content;
  bubble.querySelector(".tutor-thinking-meta").textContent = tutorThinkingMeta(live.debug);
  const body = bubble.querySelector(".tutor-thinking-body");
  const scroll = body.scrollTop;
  const followThought = body.scrollHeight - body.scrollTop - body.clientHeight < 30;
  body.innerHTML = renderTutorThinkingBody(live.debug);
  body.scrollTop = followThought ? body.scrollHeight : scroll;
  if (follow && !live.thinkingOpen) list.scrollTop = list.scrollHeight;
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
  for (let i = state.tutorMessages.length - 1; !state.tutorPending && i >= 0; i--) {
    const m = state.tutorMessages[i];
    if (m.role === "assistant") {
      latest = m;
      break;
    }
  }

  if (!latest?.sources?.length) {
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

async function readTutorStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const lines = frame.split("\n");
        const payload = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
        if (!payload) continue; // Heartbeat comment.
        const name = lines.find((line) => line.startsWith("event:"))?.slice(6).trim() || "message";
        const event = { ...JSON.parse(payload), event: name };
        if (name === "done" || name === "error") return event;
        onEvent(event);
      }
      if (done) throw new Error("The response stream ended before the answer finished. Please try again.");
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function applyTutorEvent(event) {
  const live = state.tutorLive;
  if (!live) return;
  const debug = live.debug;
  const attempt = Number(event.attempt) || 1;
  if (event.event === "attempt") {
    live.content = "";
    debug.model = event.model;
    debug.response_retries = attempt - 1;
  } else if (event.event === "round" || event.event === "answer_reset") {
    live.content = "";
  } else if (event.event === "answer_delta") {
    live.content += event.text || "";
  } else if (event.event === "reasoning_delta") {
    const previous = debug.thinking.at(-1);
    if (previous?.type === "reasoning" && previous.attempt === attempt && previous.iteration === event.iteration) {
      previous.text += event.text || "";
    } else {
      debug.thinking.push({type: "reasoning", text: event.text || "", iteration: event.iteration, attempt});
    }
  } else if (event.event === "tool" || event.event === "retry") {
    debug.thinking.push({...event.step, attempt});
    if (event.event === "retry") live.content = "";
  }
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
  state.tutorLive = {content: "", thinkingOpen: false, startedAt: Date.now(), debug: {
    model: state.model, streaming: true, thinking: [], response_retries: 0,
  }};
  renderTutorMessages();

  const reqId = ++state.lastReq;
  const chatMode = state.mode;              // Tutor or Organize
  const controller = new AbortController();
  state.tutorAbort = controller;
  let renderTimer = null;
  const renderProgress = (event) => {
    if (reqId !== state.lastReq || state.mode !== chatMode) return;
    applyTutorEvent(event);
    if (renderTimer === null) renderTimer = setTimeout(() => {
      renderTimer = null;
      if (reqId === state.lastReq && state.mode === chatMode) renderTutorProgress();
    }, 50);
  };
  // Only conversation text belongs in the next model request. UI errors and
  // source-card HTML are display state, not earlier tutor answers.
  const messages = state.tutorMessages
    .filter((m) => !m.error)
    .map(({ role, content }) => ({ role, content }));
  try {
    const res = await fetch(CHAT_UI[chatMode].endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({ messages, deck: state.deck, model: state.model, stream: true }),
    });
    const data = res.headers?.get("content-type")?.includes("text/event-stream")
      ? await readTutorStream(res, renderProgress) : await res.json();
    if (reqId !== state.lastReq || state.mode !== chatMode) return;
    clearTimeout(renderTimer);
    state.tutorPending = false;
    const thinkingOpen = state.tutorLive?.thinkingOpen;
    const liveDebug = state.tutorLive?.debug;
    if (liveDebug) liveDebug.streaming = false;
    state.tutorLive = null;
    if (data.error) {
      state.tutorMessages.push({
        role: "assistant",
        content: `_Error: ${data.error}_`,
        sources: [],
        error: true,
        debug: data.debug || liveDebug,
        thinkingOpen,
      });
    } else {
      state.tutorMessages.push({
        role: "assistant",
        content: data.answer || "(no response)",
        sources: data.sources || [],
        proposals: data.proposals || [],
        debug: data.debug,
        thinkingOpen,
      });
    }
    renderTutorMessages();
    input.focus();
  } catch (e) {
    if (reqId !== state.lastReq || state.mode !== chatMode || e.name === "AbortError") return;
    state.tutorPending = false;
    const debug = state.tutorLive?.debug;
    if (debug) debug.streaming = false;
    state.tutorLive = null;
    state.tutorMessages.push({
      role: "assistant",
      content: `_Network error: ${String(e)}_`,
      sources: [],
      error: true,
      debug,
    });
    renderTutorMessages();
  } finally {
    clearTimeout(renderTimer);
    if (reqId === state.lastReq) state.tutorAbort = null;
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
  const ankiSearch = note.nid ? `nid:${noteId}` : noteId;

  el.innerHTML = `
    <header class="note-header">
      <div class="guid-wrap">
        ${refBadge}
        <span class="guid-label">Note ID</span>
        <span class="guid">${escapeHtml(noteId)}</span>
        <button class="copy-btn" type="button" data-copy="${escapeHtml(ankiSearch)}"
                title="${note.nid ? "Copy a ready-to-paste Anki search" : "Copy note GUID"}">Copy</button>
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

  el.querySelectorAll(".note-front img, .note-back img, .note-extras img").forEach((img) => {
    img.classList.add("note-image-expandable");
    img.tabIndex = 0;
    img.setAttribute("role", "button");
    img.setAttribute("aria-label", img.alt ? `Expand image: ${img.alt}` : "Expand card image");
    img.setAttribute("aria-haspopup", "dialog");
    img.title = img.title ? `${img.title} — Click to expand` : "Click to expand";
    img.addEventListener("click", (event) => {
      // Some cards wrap images in links; expanding should keep the card open.
      event.preventDefault();
      event.stopPropagation();
      openCardImage(img);
    });
    img.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        event.stopPropagation();
        openCardImage(img);
      }
    });
    const showUnavailable = () => {
      const message = document.createElement("span");
      message.className = "note-image-unavailable";
      message.textContent = img.alt ? `Image unavailable: ${img.alt}` : "Image unavailable";
      img.replaceWith(message);
    };
    img.addEventListener("error", showUnavailable, { once: true });
    if (!img.getAttribute("src") || (img.complete && !img.naturalWidth)) showUnavailable();
  });

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

  // Tag chip clicks -> add exact-match filter (not inside a preview dialog,
  // where it would silently change the search behind it).
  if (!opts.inDialog) el.querySelectorAll(".tag-chip[data-tag]").forEach((chip) => {
    chip.addEventListener("click", () => {
      toggleTagFilter(chip.dataset.tag);
      runCurrent();
    });
  });

  // Deck breadcrumb click -> scope the search to that deck.
  const deckEl = el.querySelector(".note-deck[data-deck]");
  if (deckEl && !opts.inDialog) {
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
  let installed = false;
  const measure = () => requestAnimationFrame(() => {
    if (installed || el.scrollHeight <= COLLAPSE_PX + 24) return;
    installed = true;
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
  measure();
  el.querySelectorAll("img").forEach((img) => {
    img.addEventListener("load", measure, { once: true });
  });
  const details = el.closest("details");
  if (details) details.addEventListener("toggle", measure);
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
