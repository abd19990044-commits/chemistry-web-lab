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
    row.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;"><button type="button" id="kaggle-forget-saved" class="btn btn-ghost btn-small">Forget saved Kaggle credentials</button><span id="kaggle-saved-status" class="field-hint" aria-live="polite"></span></div><span class="field-hint">When “Remember me” is enabled, credentials are encrypted and stored in your account's secure server vault (AES-256-GCM AEAD). The raw API token is never kept in browser local storage.</span>`;
    form.appendChild(row);

    const status = document.getElementById("kaggle-saved-status");
    const forget = document.getElementById("kaggle-forget-saved");

    async function checkVaultStatus() {
      try {
        const resp = await fetch("/api/kaggle/credentials", { method: "GET" });
        if (resp.ok) {
          const data = await resp.json();
          if (data && data.ok && data.configured) {
            return {
              saved: true,
              user: data.username || localStorage.getItem(USER_KEY) || "account",
              tokenMasked: data.token_masked || "********",
            };
          }
        }
      } catch (_) {}
      return { saved: false, user: "", tokenMasked: "" };
    }

    async function render() {
      const { saved, user, tokenMasked } = await checkVaultStatus();
      if (saved) {
        try { localStorage.removeItem(KEY_KEY); } catch (_) {}
        status.textContent = `Encrypted in secure account vault for ${user} (${tokenMasked}).`;
        forget.disabled = false;
        if (username && !username.value && user) {
          username.value = user;
        }
      } else {
        status.textContent = "No Kaggle credentials are saved in account vault.";
        forget.disabled = true;
      }
    }

    forget.addEventListener("click", async () => {
      forget.disabled = true;
      try {
        await fetch("/api/kaggle/credentials", { method: "DELETE" });
      } catch (_) {}
      localStorage.removeItem(USER_KEY);
      localStorage.removeItem(KEY_KEY);
      if (username) username.value = "";
      if (key) key.value = "";
      if (remember) remember.checked = false;
      document.getElementById("kaggle-signout-btn")?.click();
      await render();
    });

    form.addEventListener("submit", () => setTimeout(render, 500));
    render();
  }

  // ORCA generated input: style only the ORCA output container/editor.
  // Do not override #orca or #orca-view display; those are workspace panels.
  const orcaStyle = document.createElement("style");
  orcaStyle.textContent = `
    #orca-output-wrap {
      width: 100% !important;
      min-width: 0 !important;
      max-width: none !important;
      margin: 0 !important;
    }
    #orca-output-wrap h4,
    #orca-output-wrap .orca-output-actions {
      width: 100% !important;
    }
    #orca-output-wrap #orca-output,
    #orca-output.orca-input-editor {
      display: block !important;
      width: 100% !important;
      min-width: 0 !important;
      max-width: 100% !important;
      height: clamp(420px, 68vh, 900px) !important;
      min-height: 420px !important;
      margin: 0 !important;
      box-sizing: border-box !important;
      overflow: auto !important;
      white-space: pre !important;
      word-break: normal !important;
      overflow-wrap: normal !important;
      resize: vertical !important;
      background: #16273C !important;
      color: #ffffff !important;
      caret-color: #ffffff !important;
      -webkit-text-fill-color: #ffffff !important;
      opacity: 1 !important;
    }
    #orca-output-wrap #orca-output::selection {
      background: #2BD9A8 !important;
      color: #0A1420 !important;
    }
    @media (max-width: 720px) {
      #orca-output-wrap #orca-output,
      #orca-output.orca-input-editor {
        height: clamp(320px, 60vh, 650px) !important;
        min-height: 320px !important;
      }
    }
  `;
  document.head.appendChild(orcaStyle);

  const outputWrap = document.getElementById("orca-output-wrap");
  if (outputWrap) {
    outputWrap.style.width = "100%";
    outputWrap.style.minWidth = "0";
    outputWrap.style.maxWidth = "none";
  }

  let editor = document.getElementById("orca-output");
  if (outputWrap && editor && editor.tagName === "PRE") {
    const replacement = document.createElement("textarea");
    replacement.id = "orca-output";
    replacement.className = "code-block mono orca-input-editor";
    replacement.rows = 28;
    replacement.spellcheck = false;
    replacement.wrap = "off";
    replacement.setAttribute("aria-label", "Editable ORCA input file");
    replacement.setAttribute("autocomplete", "off");
    replacement.value = editor.textContent || "";
    editor.replaceWith(replacement);
    editor = replacement;
  }
  if (outputWrap && editor && editor.tagName === "TEXTAREA") {
    Object.assign(editor.style, {
      display: "block",
      width: "100%",
      minWidth: "0",
      maxWidth: "100%",
      height: "clamp(420px, 68vh, 900px)",
      minHeight: "420px",
      resize: "vertical",
      boxSizing: "border-box",
      whiteSpace: "pre",
      overflow: "auto",
      lineHeight: "1.55",
      background: "#16273C",
      color: "#ffffff",
      caretColor: "#ffffff",
      WebkitTextFillColor: "#ffffff",
      opacity: "1"
    });
    let lastObservedText = editor.value || "", objectUrl = null;
    function syncDownload() { const anchor = document.getElementById("orca-download-inp"); if (!anchor) return; if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = URL.createObjectURL(new Blob([editor.value], { type: "text/plain;charset=utf-8" })); anchor.href = objectUrl; }
    function syncFromDomText() { const domText = editor.textContent || ""; if (domText && domText !== lastObservedText) { if (document.activeElement !== editor) editor.value = domText; lastObservedText = domText; syncDownload(); } }
    editor.addEventListener("input", () => { lastObservedText = editor.value; syncDownload(); });
    new MutationObserver(syncFromDomText).observe(editor, { childList: true, characterData: true, subtree: true });
    const poll = setInterval(() => { syncFromDomText(); if (editor.value.trim()) clearInterval(poll); }, 100); setTimeout(() => clearInterval(poll), 30000);
    document.getElementById("orca-send-to-kaggle")?.addEventListener("click", () => { const target = document.getElementById("kaggle-inp-content"); if (target) target.value = editor.value; const name = document.getElementById("kaggle-inp-name"); if (name && !name.value.trim()) name.value = document.getElementById("orca-download-inp")?.download || "molecule.inp"; }); syncDownload();
  }

  function imageElementToPngBlob(img) { return new Promise((resolve, reject) => { if (!img || !img.src) return reject(new Error("No structure image is available.")); fetch(img.src).then(r => r.blob()).then(resolve).catch(reject); }); }
  function pngBlobToSvgBlob(pngBlob, width, height) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => { const safeWidth = Math.max(1, Math.round(width || 560)), safeHeight = Math.max(1, Math.round(height || 420)); const svg = `<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${safeWidth}" height="${safeHeight}" viewBox="0 0 ${safeWidth} ${safeHeight}"><image width="${safeWidth}" height="${safeHeight}" preserveAspectRatio="xMidYMid meet" href="${reader.result}"/></svg>`; resolve(new Blob([svg], { type: "image/svg+xml" })); }; reader.onerror = reject; reader.readAsDataURL(pngBlob); }); }
  function showCopyToast(message, error = false) { const stack = document.getElementById("toast-stack"); if (!stack) return; const toast = document.createElement("div"); toast.className = "toast"; toast.setAttribute("role", error ? "alert" : "status"); toast.textContent = message; stack.appendChild(toast); setTimeout(() => toast.remove(), 5000); }
  async function copyImageForWord(img, plainText, label) { try { const png = await imageElementToPngBlob(img), svg = await pngBlobToSvgBlob(png, img.naturalWidth || 560, img.naturalHeight || 420); if (!navigator.clipboard || !window.ClipboardItem) throw new Error("This browser does not expose image clipboard access. Use Download instead."); await navigator.clipboard.write([new ClipboardItem({ "image/svg+xml": svg, "image/png": png, "text/plain": new Blob([plainText || ""], { type: "text/plain" }) })]); showCopyToast(`${label} copied - paste directly into Word.`); } catch (err) { showCopyToast(err.message || "Could not copy the structure.", true); } }
  async function copyText(text, label) { try { await navigator.clipboard.writeText(text || ""); showCopyToast(`${label} copied.`); } catch (_err) { showCopyToast("Clipboard access was blocked by the browser. Select and copy the text manually.", true); } }
  function installExplorerActions() { const result = document.getElementById("explorer-result"), image = document.getElementById("explorer-image"); if (!result || !image || result.classList.contains("hidden")) return; const visual = result.querySelector(".result-visual"); addButtonOnce(visual, "explorer-copy-word", "Copy for Word", () => copyImageForWord(image, document.getElementById("explorer-title-out")?.textContent || "Chemical structure", "Structure"), "btn btn-primary btn-small"); }
  function installReactionActions() { const result = document.getElementById("reaction-result"), image = document.getElementById("reaction-image"); if (!result || !image || result.classList.contains("hidden")) return; const actions = result.querySelector(".reaction-actions") || result; addButtonOnce(actions, "reaction-copy-word", "Copy for Word", () => copyImageForWord(image, document.getElementById("reaction-equation")?.textContent || "Chemical reaction", "Reaction"), "btn btn-primary btn-small"); }
  const STOP_TERMINAL = new Set(["complete", "completed", "error", "failed", "cancelled", "canceled"]);
  function jobIdFromCard(card) { return card?.dataset.jobId || card?.getAttribute("data-job-id") || card?.querySelector("[data-job-id]")?.dataset.jobId || null; }
  async function stopRequest(url, body) { const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }), payload = await response.json().catch(() => ({})); if (!response.ok || !payload.ok) throw new Error(payload.error || `Request failed (HTTP ${response.status}).`); return payload; }
  function activeCard(card) { const status = (card.querySelector(".job-status")?.textContent || "").trim().toLowerCase(); return !STOP_TERMINAL.has(status); }
  function installJobStopControls() { const jobsList = document.getElementById("jobs-list"); if (!jobsList) return; let toolbar = document.getElementById("jobs-stop-toolbar"); if (!toolbar) { toolbar = document.createElement("div"); toolbar.id = "jobs-stop-toolbar"; Object.assign(toolbar.style, { display: "flex", justifyContent: "flex-end", margin: "0 0 0.75rem", gap: "0.5rem" }); const allButton = document.createElement("button"); allButton.type = "button"; allButton.id = "stop-all-active-jobs"; allButton.className = "btn btn-ghost btn-small"; allButton.textContent = "Stop all active jobs"; allButton.addEventListener("click", async () => { const count = [...jobsList.querySelectorAll(".job-card")].filter(activeCard).length; if (!count) return showCopyToast("There are no active jobs to stop."); if (!window.confirm(`Stop all ${count} active job${count === 1 ? "" : "s"}? Running Kaggle kernels will be terminated and no continuation will be started.`)) return; allButton.disabled = true; const oldText = allButton.textContent; allButton.textContent = "Stopping…"; try { const result = await stopRequest("/api/orca/stop-all"); showCopyToast(result.message || "Stop request sent for all active jobs."); window.dispatchEvent(new CustomEvent("chemlab-jobs-refresh")); } catch (err) { showCopyToast(err.message || "Could not stop active jobs.", true); } finally { allButton.disabled = false; allButton.textContent = oldText; } }); jobsList.parentNode?.insertBefore(toolbar, jobsList); toolbar.appendChild(allButton); } jobsList.querySelectorAll(".job-card").forEach(card => { const id = jobIdFromCard(card); if (!id || !activeCard(card) || card.querySelector(".stop-job-btn")) return; const actions = card.querySelector(".job-actions") || card.querySelector(".job-card-actions") || card; const button = document.createElement("button"); button.type = "button"; button.className = "btn btn-ghost btn-small stop-job-btn"; button.textContent = "Stop job"; button.addEventListener("click", async () => { if (!window.confirm("Stop this job? Its running Kaggle kernel will be terminated and future continuation windows will not start.")) return; button.disabled = true; const oldText = button.textContent; button.textContent = "Stopping…"; try { let result; try { result = await stopRequest(`/api/v1/jobs/${encodeURIComponent(id)}/cancel`); } catch { result = await stopRequest("/api/orca/cancel", { job_id: id }); } showCopyToast(result.message || "Job stop requested."); card.querySelector(".job-status")?.classList.add("status-cancelled"); const badge = card.querySelector(".job-status"); if (badge) badge.textContent = "Cancelled"; button.remove(); window.dispatchEvent(new CustomEvent("chemlab-jobs-refresh")); } catch (err) { showCopyToast(err.message || "Could not stop this job.", true); button.disabled = false; button.textContent = oldText; } }); actions.appendChild(button); }); }
  function installChemicalActions() { installExplorerActions(); installReactionActions(); }
  ["explorer-result", "reaction-result"].forEach(id => { const node = document.getElementById(id); if (node) new MutationObserver(installChemicalActions).observe(node, { attributes: true, attributeFilter: ["class"] }); });
  const jobsNode = document.getElementById("jobs-list"); if (jobsNode) new MutationObserver(installJobStopControls).observe(jobsNode, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "data-job-id"] });
  window.addEventListener("chemlab-jobs-rendered", installJobStopControls); installChemicalActions(); installJobStopControls();
})();