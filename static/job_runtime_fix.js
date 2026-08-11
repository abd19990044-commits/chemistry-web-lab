(() => {
  "use strict";

  const TERMINAL = new Set(["status-complete", "status-error", "status-cancelled"]);

  function normalizeCard(card) {
    const badge = card.querySelector(".job-status");
    const timer = card.querySelector(".job-timer");
    if (!badge || !timer) return;
    if (TERMINAL.has(badge.className.split(/\s+/).find(c => c.startsWith("status-")) || "")) {
      const status = badge.textContent.trim();
      timer.dataset.finished = String(Date.now());
      timer.textContent = status === "Complete" ? "✓ Completed" : status;
      timer.title = "Terminal job state; this is not an elapsed-time counter.";
    }
  }

  function normalizeExplorer() {
    const result = document.getElementById("explorer-result");
    const actionRow = document.querySelector("#explorer-result .explorer-action-row");
    const propList = document.querySelector("#explorer-result .prop-list");
    if (!result || !actionRow || !propList) return;

    // Keep the main controls directly below the compound properties.
    if (actionRow.previousElementSibling !== propList) {
      propList.insertAdjacentElement("afterend", actionRow);
    }
    actionRow.style.display = "flex";
    actionRow.style.alignItems = "center";
    actionRow.style.gap = "0.55rem";
    actionRow.style.flexWrap = "wrap";
    actionRow.style.marginTop = "0.9rem";
    actionRow.style.marginBottom = "0.8rem";

    const box = document.getElementById("explorer-iupac-box");
    if (box) {
      box.style.background = "var(--ink-2)";
      box.style.border = "1px solid var(--border)";
      box.style.borderRadius = "var(--radius-s)";
      box.style.padding = "0.75rem 0.9rem";
      box.style.margin = "0 0 1rem";
      box.style.lineHeight = "1.5";
      const label = box.querySelector(".iupac-label");
      const value = box.querySelector(".iupac-value");
      if (label) { label.style.fontSize = "0.78rem"; label.style.color = "var(--text-muted)"; label.style.fontWeight = "600"; }
      if (value) { value.style.display = "block"; value.style.fontSize = "0.9rem"; value.style.overflowWrap = "anywhere"; value.style.marginBottom = "0.55rem"; }
    }
  }

  function scan() {
    document.querySelectorAll(".job-card").forEach(normalizeCard);
    normalizeExplorer();
  }

  scan();
  new MutationObserver(scan).observe(document.getElementById("jobs-list") || document.body, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["class"]
  });
})();
