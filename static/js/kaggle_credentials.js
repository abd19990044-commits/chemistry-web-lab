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
  }

  // ─────────────────────────────────────────────────────────────
  // ORCA generated-input editor
  // ─────────────────────────────────────────────────────────────
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
  }

  // ─────────────────────────────────────────────────────────────
  // Chemical copy / Word export helpers
  // ─────────────────────────────────────────────────────────────
  // Word accepts SVG from the browser clipboard in modern Chromium/Edge. The
  // SVG below intentionally embeds the already rendered structure PNG. This
  // gives a reliable, self-contained SVG clipboard item without requiring
  // RDKit.js or a chemical editor plugin in the browser. The original MOL/RXN
  // download remains the editable chemical representation.
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
        const dataUrl = reader.result;
        const safeWidth = Math.max(1, Math.round(width || 560));
        const safeHeight = Math.max(1, Math.round(height || 420));
        const svg = `<?xml version="1.0" encoding="UTF-8"?>\n` +
          `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" ` +
          `width="${safeWidth}" height="${safeHeight}" viewBox="0 0 ${safeWidth} ${safeHeight}">` +
          `<image width="${safeWidth}" height="${safeHeight}" preserveAspectRatio="xMidYMid meet" href="${dataUrl}"/>` +
          `</svg>`;
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
      if (navigator.clipboard && window.ClipboardItem) {
        const item = new ClipboardItem({
          "image/svg+xml": svg,
          "image/png": png,
          "text/plain": new Blob([plainText || ""], { type: "text/plain" }),
        });
        await navigator.clipboard.write([item]);
      } else {
        throw new Error("This browser does not expose image clipboard access. Use Download instead.");
      }
      showCopyToast(`${label} copied — paste directly into Word.`);
    } catch (err) {
      showCopyToast(err.message || "Could not copy the structure.", true);
    }
  }

  async function copyText(text, label) {
    try {
      await navigator.clipboard.writeText(text || "");
      showCopyToast(`${label} copied.`);
    } catch (_err) {
      showCopyToast("Clipboard access was blocked by the browser. Select and copy the text manually.", true);
    }
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
    if (!parent || document.getElementById(id)) return;
    const button = document.createElement("button");
    button.type = "button";
    button.id = id;
    button.className = extraClass;
    button.textContent = text;
    button.addEventListener("click", handler);
    parent.appendChild(button);
  }

  function installExplorerActions() {
    const result = document.getElementById("explorer-result");
    const image = document.getElementById("explorer-image");
    if (!result || !image || result.classList.contains("hidden")) return;

    const visual = result.querySelector(".result-visual");
    const data = result.querySelector(".result-data");
    addButtonOnce(visual, "explorer-copy-word", "Copy for Word", () => {
      const title = document.getElementById("explorer-title-out")?.textContent || "Chemical structure";
      copyImageForWord(image, title, "Structure");
    }, "btn btn-primary btn-small");

    addButtonOnce(data, "explorer-iupac-toggle", "Show IUPAC name", async () => {
      const query = document.getElementById("explorer-query")?.value.trim();
      if (!query) return;
      const button = document.getElementById("explorer-iupac-toggle");
      button.disabled = true;
      button.textContent = "Looking up IUPAC name…";
      try {
        const response = await fetch("/api/compound", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query }),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "IUPAC lookup failed.");
        let box = document.getElementById("explorer-iupac-box");
        if (!box) {
          box = document.createElement("div");
          box.id = "explorer-iupac-box";
          box.className = "iupac-box";
          data.parentNode;
          const copy = document.createElement("button");
          copy.type = "button";
          copy.id = "explorer-copy-iupac";
          copy.className = "btn btn-ghost btn-small";
          copy.textContent = "Copy IUPAC name";
          copy.addEventListener("click", () => copyText(box.dataset.value || "", "IUPAC name"));
          box.appendChild(document.createElement("strong"));
          box.appendChild(document.createElement("span"));
          box.appendChild(copy);
          data.parentNode;
          data.closest(".result-data")?.appendChild(box);
        }
        const strong = box.querySelector("strong");
        const span = box.querySelector("span");
        const name = data.iupac_name || "IUPAC name is not available for this PubChem record.";
        strong.textContent = "IUPAC name";
        span.textContent = name;
        box.dataset.value = data.iupac_name || "";
        box.classList.add("is-visible");
        button.textContent = "Hide IUPAC name";
        button.onclick = () => { box.classList.toggle("is-visible"); button.textContent = box.classList.contains("is-visible") ? "Hide IUPAC name" : "Show IUPAC name"; };
      } catch (err) {
        showCopyToast(err.message, true);
        button.textContent = "Show IUPAC name";
      } finally {
        button.disabled = false;
      }
    });
  }

  function installReactionActions() {
    const result = document.getElementById("reaction-result");
    const image = document.getElementById("reaction-image");
    if (!result || !image || result.classList.contains("hidden")) return;
    const actions = result.querySelector(".reaction-actions") || result;
    addButtonOnce(actions, "reaction-copy-word", "Copy for Word", () => {
      const equation = document.getElementById("reaction-equation")?.textContent || "Chemical reaction";
      copyImageForWord(image, equation, "Reaction");
    }, "btn btn-primary btn-small");
  }

  function installChemicalActions() {
    installExplorerActions();
    installReactionActions();
  }

  // app.js changes the result sections from hidden -> visible after every API
  // response. Observe only those two nodes so the buttons survive repeated
  // searches/reactions without adding duplicate controls.
  ["explorer-result", "reaction-result"].forEach(id => {
    const node = document.getElementById(id);
    if (node) new MutationObserver(installChemicalActions).observe(node, { attributes: true, attributeFilter: ["class"] });
  });
  installChemicalActions();
})();
