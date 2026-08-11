(() => {
  "use strict";

  const USER_KEY = "chemlab_kaggle_username";
  const KEY_KEY = "chemlab_kaggle_key";

  const username = document.getElementById("kaggle-username");
  const key = document.getElementById("kaggle-key");
  const remember = document.getElementById("kaggle-remember");
  const form = document.getElementById("kaggle-login-form");
  if (!username || !key || !remember || !form) return;

  const row = document.createElement("div");
  row.className = "form-row kaggle-credential-tools";
  row.innerHTML = `
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
      <button type="button" id="kaggle-forget-saved" class="btn btn-ghost btn-small">Forget saved Kaggle credentials</button>
      <span id="kaggle-saved-status" class="field-hint" aria-live="polite"></span>
    </div>
    <span class="field-hint">When “Remember me” is enabled, the username and API key/token stay only in this browser's local storage. They are not uploaded as a saved file.</span>`;
  form.appendChild(row);

  const status = document.getElementById("kaggle-saved-status");
  const forget = document.getElementById("kaggle-forget-saved");
  function hasSavedCredentials() { return Boolean(localStorage.getItem(USER_KEY) && localStorage.getItem(KEY_KEY)); }
  function render() {
    const saved = hasSavedCredentials();
    remember.checked = saved || remember.checked;
    status.textContent = saved ? `Saved on this browser for ${localStorage.getItem(USER_KEY)}.` : "No Kaggle credentials are saved on this browser.";
    forget.disabled = !saved;
  }
  forget.addEventListener("click", () => {
    localStorage.removeItem(USER_KEY); localStorage.removeItem(KEY_KEY);
    username.value = ""; key.value = ""; remember.checked = false;
    document.getElementById("kaggle-signout-btn")?.click();
    render();
    const stack = document.getElementById("toast-stack");
    if (stack) { const toast = document.createElement("div"); toast.className = "toast"; toast.textContent = "Saved Kaggle credentials were removed from this browser."; stack.appendChild(toast); setTimeout(() => toast.remove(), 6000); }
  });
  form.addEventListener("submit", () => setTimeout(render, 0));
  render();

  // ─────────────────────────────────────────────────────────────
  // ORCA generated-input editor
  // ─────────────────────────────────────────────────────────────
  // app.js historically renders the generated file in <pre>. Convert that
  // exact element to a native textarea so the user can edit the real input.
  // This runs after app.js has loaded but before the user can generate a file.
  const outputWrap = document.getElementById("orca-output-wrap");
  let editor = document.getElementById("orca-output");

  if (outputWrap && editor && editor.tagName === "PRE") {
    const replacement = document.createElement("textarea");
    replacement.id = "orca-output";
    replacement.className = "code-block mono orca-input-editor";
    replacement.rows = 24;
    replacement.spellcheck = false;
    replacement.wrap = "off";
    replacement.setAttribute("aria-label", "Editable ORCA input file");
    replacement.setAttribute("autocomplete", "off");
    replacement.value = editor.textContent || "";
    editor.replaceWith(replacement);
    editor = replacement;
  }

  if (!outputWrap || !editor || editor.tagName !== "TEXTAREA") return;

  // Keep the generated text and the textarea's value synchronized. The main
  // generator currently assigns `textContent`; for a textarea that creates a
  // child text node but does not update `.value`, so both a MutationObserver
  // and a short polling fallback are used. The polling stops once the first
  // generated file has appeared.
  let lastObservedText = editor.value || "";
  let objectUrl = null;

  function syncFromDomText() {
    const domText = editor.textContent || "";
    if (domText && domText !== lastObservedText) {
      // Only overwrite an untouched/previously generated value. Never destroy
      // text while the researcher is actively editing the textarea.
      if (document.activeElement !== editor) editor.value = domText;
      lastObservedText = domText;
      syncDownload();
    }
  }

  function syncDownload() {
    const anchor = document.getElementById("orca-download-inp");
    if (!anchor) return;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = URL.createObjectURL(new Blob([editor.value], { type: "text/plain;charset=utf-8" }));
    anchor.href = objectUrl;
  }

  editor.addEventListener("input", () => {
    lastObservedText = editor.value;
    syncDownload();
  });

  const observer = new MutationObserver(syncFromDomText);
  observer.observe(editor, { childList: true, characterData: true, subtree: true });

  const poll = setInterval(() => {
    syncFromDomText();
    if (editor.value.trim()) clearInterval(poll);
  }, 100);
  setTimeout(() => clearInterval(poll), 30000);

  // app.js registered its Send-to-Kaggle handler first. This second handler
  // intentionally copies the CURRENT edited value after that handler runs.
  document.getElementById("orca-send-to-kaggle")?.addEventListener("click", () => {
    const target = document.getElementById("kaggle-inp-content");
    if (target) target.value = editor.value;
    const name = document.getElementById("kaggle-inp-name");
    if (name && !name.value.trim()) {
      const generated = document.getElementById("orca-download-inp")?.download;
      name.value = generated || "molecule.inp";
    }
  });

  syncDownload();
})();
