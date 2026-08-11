(() => {
  "use strict";

  // The original timer measures browser submission -> now. That is useful for
  // active jobs, but misleading after a browser has been closed for hours.
  // The backend is authoritative for terminal state; once the card says
  // Complete/Error/Cancelled, replace the live elapsed timer with a terminal
  // label instead of displaying an inflated wall-clock duration.
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

  function scan() {
    document.querySelectorAll(".job-card").forEach(normalizeCard);
  }

  scan();
  new MutationObserver(scan).observe(document.getElementById("jobs-list") || document.body, {
    childList: true,
    subtree: true,
    characterData: true,
  });
})();
