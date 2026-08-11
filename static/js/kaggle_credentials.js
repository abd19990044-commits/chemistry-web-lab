(() => {
  "use strict";

  const USER_KEY = "chemlab_kaggle_username";
  const KEY_KEY = "chemlab_kaggle_key";
  const username = document.getElementById("kaggle-username");
  const key = document.getElementById("kaggle-key");
  const remember = document.getElementById("kaggle-remember");
  const form = document.getElementById("kaggle-login-form");

  if (username && key && remember && form) {
    const row = document.createElement("div");
    row.className = "form-row kaggle-credential-tools";
    row.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;"><button type="button" id="kaggle-forget-saved" class="btn btn-ghost btn-small">Forget saved Kaggle credentials</button><span id="kaggle-saved-status" class="field-hint" aria-live="polite"></span></div><span class="field-hint">When “Remember me” is enabled, the username and API key/token stay only in this browser's local storage. They are not uploaded as a saved file.</span>`;
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
    });
    form.addEventListener("submit", () => setTimeout(render, 0));
    render();
  }

  // Editable ORCA input + download synchronization.
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
  if (outputWrap && editor && editor.tagName === "TEXTAREA") {
    let lastObservedText = editor.value || "";
    let objectUrl = null;
    function syncDownload() {
      const anchor = document.getElementById("orca-download-inp");
      if (!anchor) return;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = URL.createObjectURL(new Blob([editor.value], { type: "text/plain;charset=utf-8" }));
      anchor.href = objectUrl;
    }
    function syncFromDomText() {
      const domText = editor.textContent || "";
      if (domText && domText !== lastObservedText) {
        if (document.activeElement !== editor) editor.value = domText;
        lastObservedText = domText;
        syncDownload();
      }
    }
    editor.addEventListener("input", () => { lastObservedText = editor.value; syncDownload(); });
    new MutationObserver(syncFromDomText).observe(editor, { childList: true, characterData: true, subtree: true });
    const poll = setInterval(() => { syncFromDomText(); if (editor.value.trim()) clearInterval(poll); }, 100);
    setTimeout(() => clearInterval(poll), 30000);
    document.getElementById("orca-send-to-kaggle")?.addEventListener("click", () => {
      const target = document.getElementById("kaggle-inp-content");
      if (target) target.value = editor.value;
      const name = document.getElementById("kaggle-inp-name");
      if (name && !name.value.trim()) name.value = document.getElementById("orca-download-inp")?.download || "molecule.inp";
    });
    syncDownload();
  }

  // ─────────────────────────────────────────────────────────────
  // Chemical copy / IUPAC controls
  // ─────────────────────────────────────────────────────────────
  function imageElementToPngBlob(img) {
    return new Promise((resolve, reject) => {
      if (!img || !img.src) return reject(new Error("No structure image is available."));
      fetch(img.src).then(r => r.blob()).then(resolve).catch(reject);
    });
  }

  function pngBlobToSvgBlob(pngBlob, width, height) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const safeWidth = Math.max(1, Math.round(width || 560));
        const safeHeight = Math.max(1, Math.round(height || 420));
        const svg = `<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${safeWidth}" height="${safeHeight}" viewBox="0 0 ${safeWidth} ${safeHeight}"><image width="${safeWidth}" height="${safeHeight}" preserveAspectRatio="xMidYMid meet" href="${reader.result}"/></svg>`;
        resolve(new Blob([svg], { type: "image/svg+xml" }));
      };
      reader.onerror = reject;
      reader.readAsDataURL(pngBlob);
    });
  }

  async function copyImageForWord(img, plainText, label) {
    try {
      const png = await imageElementToPngBlob(img);
      const svg = await pngBlobToSvgBlob(png, img.naturalWidth || 560, img.naturalHeight || 420);
      if (!navigator.clipboard || !window.ClipboardItem) throw new Error("This browser does not expose image clipboard access. Use Download instead.");
      await navigator.clipboard.write([new ClipboardItem({
        "image/svg+xml": svg,
        "image/png": png,
        "text/plain": new Blob([plainText || ""], { type: "text/plain" }),
      })]);
      showCopyToast(`${label} copied — paste directly into Word.`);
    } catch (err) { showCopyToast(err.message || "Could not copy the structure.", true); }
  }

  async function copyText(text, label) {
    try { await navigator.clipboard.writeText(text || ""); showCopyToast(`${label} copied.`); }
    catch (_err) { showCopyToast("Clipboard access was blocked by the browser. Select and copy the text manually.", true); }
  }

  function showCopyToast(message, error = false) {
    const stack = document.getElementById("toast-stack");
    if (!stack) return;
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.setAttribute("role", error ? "alert" : "status");
    toast.textContent = message;
    stack.appendChild(toast);
    setTimeout(() => toast.remove(), 5000);
  }

  function addButtonOnce(parent, id, text, handler, extraClass = "btn btn-ghost btn-small") {
    if (!parent || document.getElementById(id)) return null;
    const button = document.createElement("button");
    button.type = "button";
    button.id = id;
    button.className = extraClass;
    button.textContent = text;
    button.addEventListener("click", handler);
    parent.appendChild(button);
    return button;
  }

  function installExplorerActions() {
    const result = document.getElementById("explorer-result");
    const image = document.getElementById("explorer-image");
    if (!result || !image || result.classList.contains("hidden")) return;
    const visual = result.querySelector(".result-visual");
    const dataPanel = result.querySelector(".result-data");
    const sendToOrca = document.getElementById("explorer-send-to-orca");

    addButtonOnce(visual, "explorer-copy-word", "Copy for Word", () => {
      copyImageForWord(image, document.getElementById("explorer-title-out")?.textContent || "Chemical structure", "Structure");
    }, "btn btn-primary btn-small");

    if (!document.getElementById("explorer-iupac-toggle") && dataPanel) {
      const button = document.createElement("button");
      button.type = "button";
      button.id = "explorer-iupac-toggle";
      button.className = "btn btn-ghost btn-small explorer-iupac-button";
      button.textContent = "Show IUPAC name";

      const box = document.createElement("div");
      box.id = "explorer-iupac-box";
      box.className = "iupac-box hidden";
      box.innerHTML = `<div class="iupac-label">IUPAC name</div><div class="iupac-value"></div><button type="button" id="explorer-copy-iupac" class="btn btn-ghost btn-small">Copy IUPAC name</button>`;
      const valueEl = box.querySelector(".iupac-value");
      box.querySelector("#explorer-copy-iupac").addEventListener("click", () => copyText(valueEl.textContent || "", "IUPAC name"));

      button.addEventListener("click", async () => {
        if (!box.classList.contains("hidden")) {
          box.classList.add("hidden");
          button.textContent = "Show IUPAC name";
          return;
        }
        const query = document.getElementById("explorer-query")?.value.trim();
        if (!query) return;
        button.disabled = true;
        button.textContent = "Looking up IUPAC name…";
        try {
          const response = await fetch("/api/compound", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query }),
          });
          const payload = await response.json();
          if (!response.ok || !payload.ok) throw new Error(payload.error || "IUPAC lookup failed.");
          if (!payload.iupac_name) throw new Error("An IUPAC name is not available for this PubChem record.");
          valueEl.textContent = payload.iupac_name;
          box.classList.remove("hidden");
          button.textContent = "Hide IUPAC name";
        } catch (err) {
          showCopyToast(err.message || "IUPAC lookup failed.", true);
          button.textContent = "Show IUPAC name";
        } finally { button.disabled = false; }
      });

      // Keep this control directly with the primary result actions.
      const controls = document.createElement("div");
      controls.className = "explorer-action-row";
      if (sendToOrca && sendToOrca.parentNode) {
        sendToOrca.parentNode.insertBefore(controls, sendToOrca);
        controls.appendChild(sendToOrca);
      } else {
        dataPanel.appendChild(controls);
      }
      controls.appendChild(button);
      controls.insertAdjacentElement("afterend", box);
    }
  }

  function installReactionActions() {
    const result = document.getElementById("reaction-result");
    const image = document.getElementById("reaction-image");
    if (!result || !image || result.classList.contains("hidden")) return;
    const actions = result.querySelector(".reaction-actions") || result;
    addButtonOnce(actions, "reaction-copy-word", "Copy for Word", () => {
      copyImageForWord(image, document.getElementById("reaction-equation")?.textContent || "Chemical reaction", "Reaction");
    }, "btn btn-primary btn-small");
  }

  function installChemicalActions() { installExplorerActions(); installReactionActions(); }
  ["explorer-result", "reaction-result"].forEach(id => {
    const node = document.getElementById(id);
    if (node) new MutationObserver(installChemicalActions).observe(node, { attributes: true, attributeFilter: ["class"] });
  });
  installChemicalActions();
})();
