const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="setup-token"]').content;
let selectedPath = "", hasKey = false, loadingDeck = 0, waitingForDeck = false, polling = null;

async function api(path, body) {
  const response = await fetch(`/api/setup/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {"Content-Type": "application/json", "X-AnkiAgent-Setup-Token": token},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Please try again.");
  return data;
}
function status(text, error = false) {
  $("status").hidden = !text;
  $("status").textContent = text;
  $("status").className = error ? "error" : "";
}
function fieldStatus(id, text, error = false) {
  $(id).textContent = text; $(id).className = `field-status${error ? " error" : ""}`;
}
function option(select, value, label) {
  const node = document.createElement("option"); node.value = value; node.textContent = label; select.appendChild(node);
}
async function chooseCollection(path, savedDeck = "") {
  const request = ++loadingDeck;
  selectedPath = path;
  $("collection-path").value = path;
  $("deck").disabled = true;
  $("deck").replaceChildren(); option($("deck"), "", "Loading decks…");
  fieldStatus("deck-status", "Reading the deck list…");
  try {
    const data = await api("decks", {path});
    if (request !== loadingDeck) return;
    selectedPath = data.path;
    $("deck").replaceChildren(); option($("deck"), "", "All decks in this profile");
    data.decks.forEach((name) => option($("deck"), name, name.replaceAll("::", " › ")));
    $("deck").value = data.decks.includes(savedDeck) ? savedDeck : "";
    $("deck").disabled = false;
    $("profile-help").textContent = "Collection found. Your decks are ready to choose.";
    fieldStatus("deck-status", `${data.decks.length} decks available. Your Anki collection will not be changed.`);
  } catch (error) {
    if (request !== loadingDeck) return;
    $("deck").replaceChildren(); option($("deck"), "", "Choose another collection");
    fieldStatus("deck-status", error.message, true);
  }
}
function fillProfiles(data) {
  const profiles = data.profiles || [];
  $("profile").replaceChildren(); option($("profile"), "", profiles.length ? "Choose a profile" : "No Anki profiles found");
  profiles.forEach((profile) => option($("profile"), profile.path, profile.name));
  const saved = data.config.deck_path;
  if (saved && !profiles.some((profile) => profile.path === saved)) option($("profile"), saved, "Saved collection");
  const path = saved || (profiles.length === 1 ? profiles[0].path : "");
  if (path) { $("profile").value = path; chooseCollection(path, data.config.deck_name); }
  else $("profile-help").textContent = "Open Anki and sync your cards, then click Refresh. Or browse to your collection.anki2 file.";
}
async function checkState(initial = false) {
  try {
    const data = await api("state");
    hasKey = data.has_key;
    if (initial) {
      fillProfiles(data);
      $("enable-hotkey").checked = data.config.enable_hotkey === true;
      if (hasKey) $("api-key").placeholder = "A key is configured. Leave blank to keep it.";
    }
    if (data.status === "indexing") {
      $("setup-fields").disabled = true;
      status("Preparing your deck… Large decks may take a moment. You can leave this window open.");
      waitingForDeck = true;
      polling = setTimeout(() => checkState(), 500);
    } else if (data.status === "ready") {
      if (waitingForDeck || window.location.pathname !== "/setup") window.location.replace("/?mode=tutor");
      else { $("back").hidden = false; $("setup-fields").disabled = false; }
    } else {
      waitingForDeck = false;
      $("setup-fields").disabled = false;
      if (data.message) status(data.message, true);
    }
  } catch (error) { $("setup-fields").disabled = false; status(error.message, true); }
}
$("show-key").addEventListener("click", () => {
  const showing = $("api-key").type === "password";
  $("api-key").type = showing ? "text" : "password";
  $("show-key").textContent = showing ? "Hide" : "Show";
  $("show-key").setAttribute("aria-label", showing ? "Hide API key" : "Show API key");
});
$("test-key").addEventListener("click", async () => {
  $("test-key").disabled = true; fieldStatus("key-status", "Checking your key with Groq…");
  try { await api("test-key", {key: $("api-key").value.trim()}); fieldStatus("key-status", "Connected. Your key was accepted by Groq."); }
  catch (error) { fieldStatus("key-status", error.message, true); }
  finally { $("test-key").disabled = false; }
});
$("profile").addEventListener("change", () => {
  if ($("profile").value) chooseCollection($("profile").value);
  else {
    ++loadingDeck; selectedPath = ""; $("deck").disabled = true;
    $("deck").replaceChildren(); option($("deck"), "", "Choose an Anki profile first");
  }
});
$("use-path").addEventListener("click", () => chooseCollection($("collection-path").value.trim()));
$("refresh-profiles").addEventListener("click", () => checkState(true));
$("browse").addEventListener("click", async () => {
  const path = await window.pywebview.api.choose_collection();
  if (path) { option($("profile"), path, "Selected collection"); $("profile").value = path; chooseCollection(path); }
});
const nativeReady = () => { if (window.pywebview?.api) $("browse").hidden = false; };
window.addEventListener("pywebviewready", nativeReady); nativeReady();
$("setup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedPath || $("deck").disabled) { status("Choose a readable Anki collection first.", true); return; }
  if (!hasKey && !$("api-key").value.trim()) { status("Add your Groq API key first.", true); $("api-key").focus(); return; }
  $("setup-fields").disabled = true; status("Saving your settings…");
  try {
    await api("save", {path: selectedPath, deck: $("deck").value, key: $("api-key").value.trim(), enable_hotkey: $("enable-hotkey").checked});
    $("api-key").value = ""; waitingForDeck = true; checkState();
  } catch (error) { $("setup-fields").disabled = false; status(error.message, true); }
});
checkState(true);
