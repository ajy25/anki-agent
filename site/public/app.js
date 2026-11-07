"use strict";

const cards = [
  {id: "1", front: "What does active recall mean?", back: "Trying to retrieve an answer before looking it up.", tag: "STUDY / LEARNING"},
  {id: "2", front: "How can a flashcard support active recall?", back: "Answer the question first, then reveal the back of the card to check it.", tag: "STUDY / FLASHCARDS"},
];

function selectMode(mode) {
  for (const name of ["search", "tutor"]) {
    const selected = name === mode;
    const tab = document.getElementById(`tab-${name}`);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    document.getElementById(`panel-${name}`).hidden = !selected;
  }
}

for (const name of ["search", "tutor"]) {
  const tab = document.getElementById(`tab-${name}`);
  tab.addEventListener("click", () => selectMode(name));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? "search" : event.key === "End" ? "tutor" : name === "search" ? "tutor" : "search";
    selectMode(next);
    document.getElementById(`tab-${next}`).focus();
  });
}

for (const button of document.querySelectorAll(".citation")) {
  button.addEventListener("click", () => {
    const source = document.getElementById("demo-source");
    const show = button.getAttribute("aria-expanded") !== "true";
    document.querySelectorAll(".citation").forEach((item) => item.setAttribute("aria-expanded", "false"));
    source.replaceChildren();
    source.hidden = !show;
    if (!show) return;
    const card = cards.find((item) => item.id === button.dataset.card);
    button.setAttribute("aria-expanded", "true");
    const label = document.createElement("strong");
    label.textContent = `SOURCE ${card.id} / ${card.front}`;
    const answer = document.createElement("p");
    answer.textContent = card.back;
    source.append(label, answer);
  });
}

function searchSamples(query) {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return cards.filter((card) => words.every((word) => `${card.front} ${card.back} ${card.tag}`.toLowerCase().includes(word)));
}

function renderSearch(query) {
  const results = searchSamples(query);
  const container = document.getElementById("sample-results");
  container.replaceChildren();
  document.getElementById("sample-count").textContent = `${results.length} sample ${results.length === 1 ? "card" : "cards"}`;
  for (const card of results) {
    const article = document.createElement("article");
    article.className = "sample-card";
    const tag = document.createElement("small");
    tag.textContent = card.tag;
    const front = document.createElement("h4");
    front.textContent = card.front;
    const back = document.createElement("p");
    back.textContent = card.back;
    article.append(tag, front, back);
    container.append(article);
  }
  if (!results.length) {
    const message = document.createElement("p");
    message.className = "sample-search-note";
    message.textContent = "No matches in these two sample cards. Try “flashcard” or “recall”.";
    container.append(message);
  }
}

document.getElementById("sample-search").addEventListener("submit", (event) => {
  event.preventDefault();
  renderSearch(document.getElementById("demo-query").value);
});
renderSearch("active recall");
