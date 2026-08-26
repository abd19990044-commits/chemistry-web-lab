(() => {
  "use strict";

  const TERMINAL = new Set(["status-complete", "status-error", "status-cancelled"]);

  function normalizeCard(card) {
    if (!card) return;
    const badge = card.querySelector(".job-status");
    const timer = card.querySelector(".job-timer");
    if (!badge || !timer) return;
    const statusClass = badge.className.split(/\s+/).find(c => c.startsWith("status-")) || "";
    if (statusClass === "status-complete") {
      timer.remove();
      return;
    }
    if (TERMINAL.has(statusClass)) {
      const status = badge.textContent.trim();
      const target = status;
      if (timer.textContent !== target) {
        timer.textContent = target;
      }
      if (timer.title !== "Terminal job state; this is not an elapsed-time counter.") {
        timer.title = "Terminal job state; this is not an elapsed-time counter.";
      }
    }
  }

  function normalizeExplorer() {
    const result = document.getElementById("explorer-result");
    const actionRow = document.querySelector("#explorer-result .explorer-action-row");
    const propList = document.querySelector("#explorer-result .prop-list");
    if (!result || !actionRow || !propList) return;
    if (actionRow.previousElementSibling !== propList) {
      propList.insertAdjacentElement("afterend", actionRow);
    }
  }

  function scan() {
    document.querySelectorAll(".job-card").forEach(normalizeCard);
    normalizeExplorer();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scan, { once: true });
  } else {
    scan();
  }
  window.addEventListener("chemlab-jobs-rendered", scan);
})();
