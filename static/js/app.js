(() => {
  "use strict";

  // Helper for escaping HTML
  function escapeHtml(str) {
    if (str == null) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // Helper to extract session CSRF token from meta tag or cookie
  function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    const match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  // Intercept window.fetch to automatically include CSRF token for mutating requests
  const _nativeFetch = window.fetch;
  window.fetch = function(resource, init) {
    let opts = init ? { ...init } : {};
    let method = "GET";
    if (opts.method) {
      method = opts.method.toUpperCase();
    } else if (typeof Request !== "undefined" && resource instanceof Request) {
      method = resource.method.toUpperCase();
    }
    if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      const csrf = getCsrfToken();
      if (csrf) {
        if (typeof Request !== "undefined" && resource instanceof Request) {
          try {
            resource.headers.set("X-CSRF-Token", csrf);
          } catch (_) {}
        }
        if (!opts.headers) {
          opts.headers = { "X-CSRF-Token": csrf };
        } else if (typeof Headers !== "undefined" && opts.headers instanceof Headers) {
          if (!opts.headers.has("X-CSRF-Token")) {
            opts.headers.set("X-CSRF-Token", csrf);
          }
        } else if (Array.isArray(opts.headers)) {
          if (!opts.headers.some(([k]) => k.toLowerCase() === "x-csrf-token")) {
            opts.headers.push(["X-CSRF-Token", csrf]);
          }
        } else {
          if (!opts.headers["X-CSRF-Token"] && !opts.headers["x-csrf-token"]) {
            opts.headers["X-CSRF-Token"] = csrf;
          }
        }
      }
    }
    return _nativeFetch.call(this, resource, opts);
  };

  // Helper to determine if a job or calculation output represents a Geometry Optimization (OPT)
  function isOptimizationJob(job) {
    if (!job) return false;
    const type = String(
      job.stageType ||
      job.calc_type ||
      (job.stageConfig && (job.stageConfig.calc_type || job.stageConfig.calculation_type)) ||
      (job.stage2Config && (job.stage2Config.calc_type || job.stage2Config.calculation_type)) ||
      (job.result && (job.result.calc_type || job.result.calculation_type)) ||
      job.calculation_type ||
      ""
    ).toLowerCase();

    // Coordinate sources are geometry optimizations ONLY (Opt / OptTS / the
    // combined Opt+Freq forms), identified from structured calculation
    // metadata - never from a job's name or comment text (an SP whose comment
    // says "opt" is still an SP). Scan/NEB are reaction paths, not converged
    // minima; SP/Freq/NumFreq/TD-DFT have no optimized geometry to import.
    // Convergence itself is enforced server-side by /api/kaggle/
    // extract-opt-coords via the ORCA classifier.
    return /geometry optimization|\bopt\b|optts|opt_ts|opt\+freq/.test(type)
      && !/scan|neb|relax/.test(type);

    const name = String(job.name || "").toLowerCase();
    if (
      name.includes("opt") ||
      name.includes("relax") ||
      name.includes("geom") ||
      name.includes("ts") ||
      name.includes("scan")
    ) {
      return true;
    }

    const inputContent = String(job.input_content || (job.stageConfig && job.stageConfig.input_text) || "").toLowerCase();
    if (
      inputContent.includes("! opt") ||
      inputContent.includes("!opt") ||
      inputContent.includes("! tightopt") ||
      inputContent.includes("!verytightopt") ||
      inputContent.includes("! looseopt") ||
      inputContent.includes("! optts") ||
      inputContent.includes("!optts")
    ) {
      return true;
    }

    if (job.optimizedCoords && job.optimizedCoords.trim()) {
      return true;
    }

    return false;
  }

  // Ephemeral Session Management (30-min auto-cleanup) with safe fallback
  let lastIsolatedUser = null;
  let currentSessionId = "";
  try {
    currentSessionId = sessionStorage.getItem("orca_weblab_sess_id");
    if (!currentSessionId) {
      currentSessionId = "sess_" + Date.now() + "_" + Math.random().toString(36).substring(2, 9);
      sessionStorage.setItem("orca_weblab_sess_id", currentSessionId);
    }
  } catch (e) {
    currentSessionId = "sess_" + Date.now() + "_" + Math.random().toString(36).substring(2, 9);
  }

  // Periodic heartbeat every 5 minutes
  setInterval(() => {
    try {
      fetch("/api/session/heartbeat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: currentSessionId })
      }).catch(() => {});
    } catch (e) {}
  }, 300000);

  // Send leave beacon on tab close / unload
  window.addEventListener("pagehide", () => {
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/session/leave", JSON.stringify({ session_id: currentSessionId }));
      }
    } catch (e) {}
  });

  let CFG = {};
  try {
    const cfgEl = document.getElementById("orca-config");
    if (cfgEl && cfgEl.textContent) {
      CFG = JSON.parse(cfgEl.textContent);
    }
  } catch (e) {
    console.error("Error parsing orca-config JSON:", e);
  }

  const LS_KEYS = {
    kaggleUsername: "chemlab_kaggle_username",
    kaggleKey: "chemlab_kaggle_key",
    orcaSourceKind: "chemlab_orca_source_kind",
    orcaDataset: "chemlab_orca_dataset",
    orcaLink: "chemlab_orca_link",
    jobs: "chemlab_jobs",
    removedJobIds: "chemlab_removed_job_ids",
  };

  let sessionKaggleKey = "";

  // ───────────────────────────────────────────────
  // View routing: home -> draw / orca / quantum / legal
  // ───────────────────────────────────────────────
  const views = document.querySelectorAll(".view");
  const backHomeBtn = document.getElementById("back-home-btn");
  const brandHomeBtn = document.getElementById("brand-home-btn");

  function showView(name, targetTab) {
    if (!name) return;
    const targetSection = document.getElementById(`${name}-view`);
    if (!targetSection) {
      console.warn(`Target view #${name}-view not found.`);
      return;
    }
    document.querySelectorAll(".view").forEach(v => v.classList.remove("is-active"));
    targetSection.classList.add("is-active");

    if (backHomeBtn) backHomeBtn.classList.toggle("hidden", name === "home");
    if (targetTab) {
      switchToTab(targetTab);
    }
    if (name === "quantum" && typeof populateEngineJobPickers === "function") {
      try { populateEngineJobPickers(); } catch (err) { console.error("Error populating quantum pickers:", err); }
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // Expose globally for inline buttons & testing
  window.showChemistryView = showView;


  // Deep-link support: /lab, /calculations and /analysis are real Flask
  // routes that render this page with the requested studio preselected.
  // Level-1 navigation only - the workspace tabs inside each studio (level 2)
  // keep their own behaviour untouched.
  const initialView = (document.body && document.body.dataset)
    ? document.body.dataset.initialView : '';
  if (['draw', 'reactions', 'orca', 'quantum'].indexOf(initialView) !== -1) {
    try { showView(initialView); } catch (err) { console.warn('initial view activation failed:', err); }
  }


  // Robust global event delegation for navigation & modals
  document.addEventListener("click", (e) => {
    // 1. Choice cards on landing view
    const card = e.target.closest(".choice-card");
    if (card) {
      e.preventDefault();
      const openTarget = card.dataset.open || card.getAttribute("data-open");
      const tabTarget = card.dataset.tab || card.getAttribute("data-tab");
      if (openTarget) showView(openTarget, tabTarget);
      return;
    }

    // 2. Citation modal trigger buttons
    const citeBtn = e.target.closest("#hero-cite-btn, #footer-cite-btn, [data-open-citation]");
    if (citeBtn) {
      e.preventDefault();
      const modal = document.getElementById("citation-modal");
      if (modal) show(modal);
      return;
    }

    // 2b. License modal trigger buttons
    const licBtn = e.target.closest("#footer-license-btn, [data-open-license]");
    if (licBtn) {
      e.preventDefault();
      const modal = document.getElementById("license-modal");
      if (modal) show(modal);
      return;
    }

    // 3. View navigation links (data-view-link)
    const viewLink = e.target.closest("[data-view-link]");
    if (viewLink) {
      e.preventDefault();
      showView(viewLink.dataset.viewLink);
      return;
    }
  });

  if (backHomeBtn) backHomeBtn.addEventListener("click", () => showView("home"));
  if (brandHomeBtn) {
    brandHomeBtn.addEventListener("click", () => showView("home"));
    brandHomeBtn.addEventListener("keydown", (e) => { if (e.key === "Enter") showView("home"); });
  }

  // ───────────────────────────────────────────────
  // Academic Citation Modal & Clipboard Copy
  // ───────────────────────────────────────────────
  function initCitationModal() {
    const citationModal = document.getElementById("citation-modal");
    const closeBtn = document.getElementById("citation-modal-close");
    const okBtn = document.getElementById("citation-modal-ok");

    if (!citationModal) return;

    const closeCitationModal = () => hide(citationModal);

    if (closeBtn) closeBtn.addEventListener("click", closeCitationModal);
    if (okBtn) okBtn.addEventListener("click", closeCitationModal);
    citationModal.addEventListener("click", (e) => {
      if (e.target === citationModal) closeCitationModal();
    });

    const fallbackCopy = (text, label) => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
        showToast(`${label} copied to clipboard!`);
      } catch (e) {
        showToast(`Please copy manually.`, "error");
      }
      document.body.removeChild(ta);
    };

    const copyText = (elementId, label) => {
      const el = document.getElementById(elementId);
      if (!el) return;
      const text = el.textContent || el.innerText;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
          showToast(`${label} copied to clipboard!`);
        }).catch(() => {
          fallbackCopy(text, label);
        });
      } else {
        fallbackCopy(text, label);
      }
    };

    const copyBibtexBtn = document.getElementById("copy-bibtex-btn");
    const copyAcsBtn = document.getElementById("copy-acs-btn");
    const copyApaBtn = document.getElementById("copy-apa-btn");

    if (copyBibtexBtn) copyBibtexBtn.addEventListener("click", () => copyText("citation-bibtex", "BibTeX entry"));
    if (copyAcsBtn) copyAcsBtn.addEventListener("click", () => copyText("citation-acs", "ACS citation"));
    if (copyApaBtn) copyApaBtn.addEventListener("click", () => copyText("citation-apa", "APA citation"));
  }
  initCitationModal();

  // ───────────────────────────────────────────────
  // Workspace sub-tabs (Molecule Explorer/Reaction, Generator/Kaggle/Jobs)
  // ───────────────────────────────────────────────
  document.querySelectorAll(".workspace-nav").forEach(nav => {
    const tabs = nav.querySelectorAll(".ws-tab");
    const panelsRoot = nav.parentElement;
    tabs.forEach(tab => {
      tab.addEventListener("click", () => {
        tabs.forEach(t => t.classList.toggle("is-active", t === tab));
        panelsRoot.querySelectorAll(".ws-panel").forEach(p => {
          p.classList.toggle("is-active", p.id === tab.dataset.target);
        });
      });
    });
  });

  function switchToTab(target) {
    if (!target) return;
    const tab = document.querySelector(`.ws-tab[data-target="${target}"]`);
    if (tab) tab.click();
  }


  // ───────────────────────────────────────────────
  // Helpers
  // ───────────────────────────────────────────────
  function b64ToDataUrl(b64, mime) { return `data:${mime};base64,${b64}`; }
  function setDownload(anchorEl, b64, mime, filename) {
    anchorEl.href = b64ToDataUrl(b64, mime);
    anchorEl.download = filename;
  }
  async function postJSON(url, body) {
    const resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const contentType = resp.headers.get("content-type") || "";
    let data = null;
    if (contentType.includes("application/json")) data = await resp.json().catch(() => null);
    else { const text = await resp.text().catch(() => ""); const detail = text.replace(/<[^>]+>/g," ").replace(/\s+/g," ").trim().slice(0,240); throw new Error(`Server returned HTTP ${resp.status}${detail ? `: ${detail}` : "."}`); }
    if (!data) throw new Error(`Server returned HTTP ${resp.status} with an invalid JSON response.`);
    if (!resp.ok || !data.ok) {
      let errMsg = "";
      if (data.error) {
        if (typeof data.error === "string") errMsg = data.error;
        else if (typeof data.error === "object") errMsg = data.error.message || data.error.detail || data.error.code || JSON.stringify(data.error);
      } else if (data.detail) {
        if (typeof data.detail === "string") errMsg = data.detail;
        else if (typeof data.detail === "object") errMsg = data.detail.message || JSON.stringify(data.detail);
      }
      if (!errMsg) errMsg = `Request failed (HTTP ${resp.status}).`;
      throw new Error(errMsg);
    }
    return data;
  }
  function showError(el, message) {
    el.textContent = typeof message === "object" && message ? (message.message || JSON.stringify(message)) : String(message);
    el.classList.remove("hidden");
  }
  function hide(el) { el.classList.add("hidden"); }
  function show(el) { el.classList.remove("hidden"); }

  function escapeHtml(str) {
    if (str == null) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function sanitizeToastHtml(input) {
    if (input == null) return "";
    const str = String(input);
    try {
      const parser = new DOMParser();
      const doc = parser.parseFromString(str, "text/html");
      const allowedTags = new Set(["STRONG", "B", "EM", "I", "SPAN", "BR", "CODE", "P", "DIV", "SMALL"]);
      const allElements = Array.from(doc.body.querySelectorAll("*"));
      allElements.forEach(el => {
        if (!allowedTags.has(el.tagName)) {
          const textNode = doc.createTextNode(el.textContent || "");
          el.parentNode ? el.parentNode.replaceChild(textNode, el) : el.remove();
          return;
        }
        for (let i = el.attributes.length - 1; i >= 0; i--) {
          const attr = el.attributes[i];
          const name = attr.name.toLowerCase();
          const val = (attr.value || "").trim().toLowerCase();
          if (
            name.startsWith("on") ||
            name === "href" ||
            name === "src" ||
            name === "action" ||
            name === "formaction" ||
            name === "xlink:href" ||
            val.includes("javascript:") ||
            val.includes("data:") ||
            val.includes("vbscript:")
          ) {
            el.removeAttribute(attr.name);
          }
        }
      });
      return doc.body.innerHTML;
    } catch (e) {
      return escapeHtml(str);
    }
  }

  function showToast(html, timeoutMs = 9000) {
    const stack = document.getElementById("toast-stack");
    if (!stack) return;
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.innerHTML = sanitizeToastHtml(html);
    stack.appendChild(toast);
    setTimeout(() => toast.remove(), timeoutMs);
  }

  // ───────────────────────────────────────────────
  // Google Sign-In (identity display only)
  // ───────────────────────────────────────────────
  const userChip = document.getElementById("user-chip");
  const googleSigninArea = document.getElementById("google-signin-area");

  window.handleGoogleCredential = async (response) => {
    try {
      const data = await postJSON("/api/auth/google", { credential: response.credential });
      renderUser(data.user);
    } catch (err) {
      showToast(`<strong>Sign-in failed</strong><br>${escapeHtml(err.message)}`);
    }
  };

  function renderUser(user) {
    if (!user) { hide(userChip); return; }
    document.getElementById("user-avatar").src = user.picture || "";
    document.getElementById("user-name").textContent = user.name || user.email || "Signed in";
    show(userChip);
    if (googleSigninArea) hide(googleSigninArea);
  }

  document.getElementById("sign-out-btn")?.addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    hide(userChip);
    if (googleSigninArea) show(googleSigninArea);
  });

  fetch("/api/auth/me").then(r => r.json()).then(d => { if (d.ok && d.user) renderUser(d.user); }).catch(() => {});

  // ───────────────────────────────────────────────
  // Molecule Explorer
  // ───────────────────────────────────────────────
  const explorerForm = document.getElementById("explorer-form");
  const explorerError = document.getElementById("explorer-error");
  const explorerLoading = document.getElementById("explorer-loading");
  const explorerResult = document.getElementById("explorer-result");
  let lastCompound = null; // shared with ORCA wizard

  explorerForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const query = document.getElementById("explorer-query").value.trim();
    if (!query) return;

    hide(explorerError); hide(explorerResult); show(explorerLoading);
    try {
      const data = await postJSON("/api/compound", { query });
      lastCompound = { query, smiles: data.smiles, name: data.filename };

      document.getElementById("explorer-image").src = b64ToDataUrl(data.image_png_base64, "image/png");
      setDownload(document.getElementById("explorer-download-mol"), data.mol_file_base64, "chemical/x-mdl-molfile", `${data.filename}.mol`);
      setDownload(document.getElementById("explorer-download-svg"), data.image_svg_base64, "image/svg+xml", `${data.filename}.svg`);
      document.getElementById("explorer-title-out").textContent = data.title || query;

      document.getElementById("explorer-formula").textContent = data.formula || "-";
      document.getElementById("explorer-weight").textContent = data.weight ? `${data.weight} g/mol` : "-";
      document.getElementById("explorer-smiles").textContent = data.smiles;

      const iupacValEl = document.getElementById("explorer-iupac-val");
      const iupacCopyBtn = document.getElementById("explorer-copy-iupac");
      const iupacName = (data.iupac_name || "").trim();

      if (iupacName) {
        if (iupacValEl) {
          iupacValEl.textContent = iupacName;
          iupacValEl.classList.remove("iupac-unavailable");
        }
        if (iupacCopyBtn) {
          iupacCopyBtn.classList.remove("hidden");
        }
      } else {
        if (iupacValEl) {
          iupacValEl.textContent = "Unavailable for this PubChem record";
          iupacValEl.classList.add("iupac-unavailable");
        }
        if (iupacCopyBtn) {
          iupacCopyBtn.classList.add("hidden");
        }
      }

      const solWrap = document.getElementById("explorer-solubility-wrap");
      const solList = document.getElementById("explorer-solubility");
      solList.innerHTML = "";
      if (data.solubility && data.solubility.length) {
        data.solubility.forEach(s => {
          const li = document.createElement("li");
          li.textContent = s;
          solList.appendChild(li);
        });
        show(solWrap);
      } else hide(solWrap);

      const wikiWrap = document.getElementById("explorer-wiki-wrap");
      if (data.wikipedia_summary) {
        document.getElementById("explorer-wiki").textContent = data.wikipedia_summary;
        show(wikiWrap);
      } else hide(wikiWrap);

      show(explorerResult);
    } catch (err) {
      showError(explorerError, err.message);
    } finally {
      hide(explorerLoading);
    }
  });

  document.getElementById("explorer-copy-iupac")?.addEventListener("click", async () => {
    const iupacText = document.getElementById("explorer-iupac-val")?.textContent || "";
    if (!iupacText || iupacText === "-" || iupacText === "Unavailable for this PubChem record") return;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(iupacText);
      } else {
        const ta = document.createElement("textarea");
        ta.value = iupacText;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      showCopyToast ? showCopyToast("IUPAC name copied.") : showToast("IUPAC name copied.");
    } catch (_err) {
      showCopyToast ? showCopyToast("Clipboard access blocked. Select text manually.", true) : showToast("Could not copy IUPAC name.");
    }
  });

  async function copyElementForWord(imgEl, titleText, subtitleText, plainTextFallback) {
    if (!imgEl || !imgEl.src) {
      showToast("No visual drawing is available to copy.");
      return;
    }
    try {
      const res = await fetch(imgEl.src);
      const blob = await res.blob();
      
      const width = imgEl.naturalWidth || imgEl.clientWidth || 560;
      const height = imgEl.naturalHeight || imgEl.clientHeight || 420;

      const htmlPayload = `<div style="font-family: Calibri, Arial, sans-serif; text-align: center; margin: 10px 0;">` +
        `<img src="${imgEl.src}" width="${width}" height="${height}" alt="${titleText || ''}" style="max-width: 100%; height: auto;" />` +
        (titleText ? `<div style="font-weight: bold; font-size: 11pt; margin-top: 4px;">${titleText}</div>` : "") +
        (subtitleText ? `<div style="font-size: 9.5pt; color: #555555; margin-top: 2px;">${subtitleText}</div>` : "") +
        `</div>`;

      const plainText = plainTextFallback || (titleText ? `${titleText}\n${subtitleText || ''}` : "");

      if (navigator.clipboard && window.ClipboardItem) {
        let pngBlob = blob;
        if (blob.type !== "image/png") {
          pngBlob = await new Promise((resolve) => {
            const canvas = document.createElement("canvas");
            canvas.width = Math.max(1, width);
            canvas.height = Math.max(1, height);
            const ctx = canvas.getContext("2d");
            const tempImg = new Image();
            tempImg.crossOrigin = "anonymous";
            tempImg.onload = () => {
              ctx.drawImage(tempImg, 0, 0, canvas.width, canvas.height);
              canvas.toBlob((b) => resolve(b || blob), "image/png");
            };
            tempImg.onerror = () => resolve(blob);
            tempImg.src = imgEl.src;
          });
        }

        const clipData = {
          "text/html": new Blob([htmlPayload], { type: "text/html" }),
          "text/plain": new Blob([plainText], { type: "text/plain" }),
        };
        if (pngBlob && pngBlob.type === "image/png") {
          clipData["image/png"] = pngBlob;
        }

        await navigator.clipboard.write([new ClipboardItem(clipData)]);
        showToast("Structure copied for Microsoft Word (paste directly into Word).");
      } else if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(plainText);
        showToast("Structure text copied to clipboard.");
      }
    } catch (err) {
      console.warn("Word copy clipboard error:", err);
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(plainTextFallback || titleText || "");
          showToast("Copied text representation to clipboard.");
        }
      } catch (_e) {
        showToast("Clipboard access was denied by browser.");
      }
    }
  }

  document.getElementById("explorer-copy-word")?.addEventListener("click", () => {
    const img = document.getElementById("explorer-image");
    const title = document.getElementById("explorer-title-out")?.textContent || "Chemical Structure";
    const formula = document.getElementById("explorer-formula")?.textContent || "";
    const smiles = document.getElementById("explorer-smiles")?.textContent || "";
    const iupac = document.getElementById("explorer-iupac-val")?.textContent || "";
    const subtitle = [formula ? `Formula: ${formula}` : "", iupac && iupac !== "-" && !iupac.includes("Unavailable") ? `IUPAC: ${iupac}` : "", smiles ? `SMILES: ${smiles}` : ""].filter(Boolean).join(" | ");
    const plain = `${title} (${formula})\nSMILES: ${smiles}` + (iupac ? `\nIUPAC: ${iupac}` : "");
    copyElementForWord(img, title, subtitle, plain);
  });

  document.getElementById("explorer-query")?.addEventListener("input", () => {
    const iupacValEl = document.getElementById("explorer-iupac-val");
    if (iupacValEl) {
      iupacValEl.textContent = "-";
      iupacValEl.classList.remove("iupac-unavailable");
    }
    const iupacCopyBtn = document.getElementById("explorer-copy-iupac");
    if (iupacCopyBtn) {
      iupacCopyBtn.classList.add("hidden");
    }
  });

  document.getElementById("explorer-send-to-orca").addEventListener("click", () => {
    if (!lastCompound) return;
    showView("orca");
    switchToTab("orca");
    openWizard({ prefillQuery: lastCompound.query });
  });

  // ───────────────────────────────────────────────
  // Molecule Explorer Background Switcher
  // ───────────────────────────────────────────────
  const explorerImgStage = document.getElementById("explorer-img-stage");
  document.querySelectorAll(".btn-canvas-theme").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".btn-canvas-theme").forEach(b => b.classList.toggle("active", b === btn));
      const bg = btn.dataset.bg || "#ffffff";
      if (explorerImgStage) {
        explorerImgStage.style.background = bg === "transparent" ? "repeating-conic-gradient(#222 0% 25%, #181818 0% 50%) 50% / 20px 20px" : bg;
      }
    });
  });

  // ───────────────────────────────────────────────
  // Reaction Drawing & Responsive Viewport
  // ───────────────────────────────────────────────
  let rxnZoomLevel = 1.0;
  const rxnImage = document.getElementById("reaction-image");
  const rxnContainer = document.getElementById("reaction-canvas-container");
  const rxnScrollStage = document.getElementById("reaction-scroll-stage");

  // Reaction Background Switcher
  document.querySelectorAll(".btn-rxn-theme").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".btn-rxn-theme").forEach(b => b.classList.toggle("active", b === btn));
      const bg = btn.dataset.bg || "#ffffff";
      if (rxnContainer) {
        rxnContainer.style.background = bg === "transparent" ? "repeating-conic-gradient(#222 0% 25%, #181818 0% 50%) 50% / 20px 20px" : bg;
      }
    });
  });

  // Reaction Zoom Controls
  const rxnZoomInBtn = document.getElementById("reaction-zoom-in");
  const rxnZoomOutBtn = document.getElementById("reaction-zoom-out");
  const rxnZoomFitBtn = document.getElementById("reaction-zoom-fit");
  const rxnZoom100Btn = document.getElementById("reaction-zoom-100");

  function applyRxnZoom() {
    if (!rxnImage) return;
    const isFit = (rxnZoomLevel === 1.0 && !rxnImage.classList.contains("scale-100"));
    if (isFit) {
      rxnImage.style.transform = "";
      rxnImage.style.maxWidth = "100%";
      rxnImage.style.width = "auto";
    } else {
      rxnImage.style.maxWidth = "none";
      const baseWidth = rxnImage.naturalWidth || 1000;
      rxnImage.style.transform = "";
      rxnImage.style.width = `${Math.round(baseWidth * rxnZoomLevel)}px`;
    }
  }

  if (rxnZoomInBtn) {
    rxnZoomInBtn.addEventListener("click", () => {
      rxnZoomLevel = Math.min(3.0, rxnZoomLevel + 0.25);
      applyRxnZoom();
    });
  }
  if (rxnZoomOutBtn) {
    rxnZoomOutBtn.addEventListener("click", () => {
      rxnZoomLevel = Math.max(0.4, rxnZoomLevel - 0.25);
      applyRxnZoom();
    });
  }
  if (rxnZoomFitBtn) {
    rxnZoomFitBtn.addEventListener("click", () => {
      rxnZoomLevel = 1.0;
      if (rxnImage) {
        rxnImage.classList.remove("scale-100");
        rxnImage.style.maxWidth = "100%";
        rxnImage.style.width = "auto";
      }
      applyRxnZoom();
      if (rxnZoomFitBtn) rxnZoomFitBtn.classList.add("active");
      if (rxnZoom100Btn) rxnZoom100Btn.classList.remove("active");
    });
  }
  if (rxnZoom100Btn) {
    rxnZoom100Btn.addEventListener("click", () => {
      rxnZoomLevel = 1.0;
      if (rxnImage) {
        rxnImage.classList.add("scale-100");
        rxnImage.style.maxWidth = "none";
      }
      applyRxnZoom();
      if (rxnZoom100Btn) rxnZoom100Btn.classList.add("active");
      if (rxnZoomFitBtn) rxnZoomFitBtn.classList.remove("active");
    });
  }

  // ───────────────────────────────────────────────
  // Reaction Drawing
  // ───────────────────────────────────────────────
  const reactionForm = document.getElementById("reaction-form");
  const reactionError = document.getElementById("reaction-error");
  const reactionLoading = document.getElementById("reaction-loading");
  const reactionResult = document.getElementById("reaction-result");
  const reactionArrowTop = document.getElementById("reaction-arrow-top");
  const reactionArrowBottom = document.getElementById("reaction-arrow-bottom");

  reactionForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const reactants = document.getElementById("reaction-reactants").value.trim();
    const products = document.getElementById("reaction-products").value.trim();
    if (!reactants || !products) return;

    hide(reactionError); hide(reactionResult); show(reactionLoading);
    try {
      const data = await postJSON("/api/reaction", {
        reactants, products,
        small_as_formula: document.getElementById("reaction-small-as-formula").checked,
        arrow_top: reactionArrowTop?.value.trim() || "",
        arrow_bottom: reactionArrowBottom?.value.trim() || "",
      });
      const rxnImg = document.getElementById("reaction-image");
      rxnImg.onerror = () => {
        if (data.image_png_base64 && rxnImg.src !== b64ToDataUrl(data.image_png_base64, "image/png")) {
          rxnImg.src = b64ToDataUrl(data.image_png_base64, "image/png");
        }
      };
      if (data.image_svg_base64) {
        rxnImg.src = b64ToDataUrl(data.image_svg_base64, "image/svg+xml");
      } else if (data.image_png_base64) {
        rxnImg.src = b64ToDataUrl(data.image_png_base64, "image/png");
      }
      setDownload(document.getElementById("reaction-download-rxn"), data.rxn_file_base64, "chemical/x-mdl-rxnfile", "reaction.rxn");
      setDownload(document.getElementById("reaction-download-svg"), data.image_svg_base64, "image/svg+xml", "reaction.svg");
      document.getElementById("reaction-smiles").textContent = data.reaction_smiles;
      document.getElementById("reaction-equation").textContent = data.equation || "";

      // Whether the equation balances. Shown, never enforced - a mechanism step
      // or a fragment is legitimately unbalanced, and the failure this guards
      // against is not noticing.
      const balanceEl = document.getElementById("reaction-balance");
      const balance = data.balance || {};
      balanceEl.className = "reaction-badge " + (balance.balanced ? "badge-ok" : "badge-warn");
      balanceEl.textContent = (balance.balanced ? "✓ " : "⚠ ") + (balance.message || "");
      balanceEl.classList.toggle("hidden", !balance.message);

      // The file check. This reports a STRUCTURAL validation of the MDL RXN
      // block - ChemDraw is not installed on the server and cannot be, so the
      // honest claim is "this is a well-formed file in the format ChemDraw
      // imports", which is exactly what was verified.
      const fileEl = document.getElementById("reaction-filecheck");
      const report = data.file_report || {};
      if (report.valid) {
        fileEl.className = "reaction-badge badge-ok";
        fileEl.textContent = `✓ Valid ${report.format} file (${report.reactants} reactant `
          + `component${report.reactants === 1 ? "" : "s"}, ${report.products} product `
          + `component${report.products === 1 ? "" : "s"}) - opens in `
          + `${(report.opens_in || []).join(", ")}.`;
      } else {
        fileEl.className = "reaction-badge badge-warn";
        fileEl.textContent = "⚠ The reaction file did not pass its format check: "
          + ((report.problems || []).join("; ") || "unknown problem")
          + ". Please report this - the drawing above is still correct.";
      }
      fileEl.classList.remove("hidden");

      const notesEl = document.getElementById("reaction-notes");
      notesEl.innerHTML = "";
      (data.notes || []).forEach(text => {
        const li = document.createElement("li");
        li.textContent = text;
        notesEl.appendChild(li);
      });
      notesEl.classList.toggle("hidden", !(data.notes || []).length);

      show(reactionResult);
    } catch (err) {
      showError(reactionError, err.message);
    } finally {
      hide(reactionLoading);
    }
  });

  // Changing how small molecules are shown redraws immediately, but only once
  // there is something on screen - toggling it before the first draw would fire
  // a request with empty boxes.
  document.getElementById("reaction-small-as-formula").addEventListener("change", () => {
    if (!reactionResult.classList.contains("hidden")) {
      document.getElementById("reaction-form").requestSubmit();
    }
  });

  document.getElementById("reaction-example-btn").addEventListener("click", () => {
    document.getElementById("reaction-reactants").value = "2 benzene + 15 O2";
    document.getElementById("reaction-products").value = "12 CO2 + 6 H2O";
    document.getElementById("reaction-help").open = false;
    document.getElementById("reaction-form").requestSubmit();
  });

  document.getElementById("reaction-calc-thermo-btn")?.addEventListener("click", () => {
    const reactants = document.getElementById("reaction-reactants")?.value.trim() || "";
    const products = document.getElementById("reaction-products")?.value.trim() || "";
    if (!reactants || !products) return;
    const equation = `${reactants} -> ${products}`;

    if (window.showChemistryView) {
      window.showChemistryView("quantum", "thermo");
    }
    const thermoEquationInput = document.getElementById("thermo-equation");
    const thermoLiveEquation = document.getElementById("thermo-live-equation");
    if (thermoEquationInput) {
      thermoEquationInput.value = equation;
      thermoEquationInput.dispatchEvent(new Event("change", { bubbles: true }));
    }
    if (thermoLiveEquation) {
      thermoLiveEquation.textContent = `${reactants} ➔ ${products}`;
    }
    if (window.populateThermoCardsFromEquation) {
      window.populateThermoCardsFromEquation(equation);
    }
  });

  document.getElementById("topbar-download-runner-btn")?.addEventListener("click", () => {
    document.getElementById("download-agent-modal")?.classList.remove("hidden");
  });

  document.querySelectorAll("#download-agent-modal [data-os-target]").forEach(tabBtn => {
    tabBtn.addEventListener("click", () => {
      const targetId = tabBtn.getAttribute("data-os-target");
      document.querySelectorAll("#download-agent-modal [data-os-target]").forEach(b => b.classList.remove("is-active"));
      tabBtn.classList.add("is-active");
      document.querySelectorAll("#download-agent-modal .os-panel").forEach(panel => {
        if (panel.id === targetId) {
          panel.classList.remove("hidden");
          panel.classList.add("is-active");
        } else {
          panel.classList.add("hidden");
          panel.classList.remove("is-active");
        }
      });
    });
  });

  document.getElementById("reaction-copy-word")?.addEventListener("click", () => {
    const img = document.getElementById("reaction-image");
    const eq = document.getElementById("reaction-equation")?.textContent || "Chemical Reaction Scheme";
    const smiles = document.getElementById("reaction-smiles")?.textContent || "";
    const plain = `${eq}` + (smiles ? `\nReaction SMILES: ${smiles}` : "");
    copyElementForWord(img, eq, smiles ? `Reaction SMILES: ${smiles}` : "", plain);
  });

  // ───────────────────────────────────────────────
  // ORCA Wizard ("options window") state machine
  // ───────────────────────────────────────────────
  const modal = document.getElementById("orca-modal");
  const modalBody = document.getElementById("orca-modal-body");
  const modalTitle = document.getElementById("orca-modal-title");
  const modalBadge = document.getElementById("orca-modal-step-badge");
  const coordsStatus = document.getElementById("orca-coords-status");

  let wizard = {};
  let stepStack = [];

  function openWizard(opts = {}) {
    wizard = {};
    stepStack = [];
    if (coordsStatus) {
      coordsStatus.textContent = "No structure selected yet.";
      coordsStatus.classList.remove("is-set");
    }
    show(modal);
    if (opts.isReactionWorkflow) {
      wizard.isReactionWorkflow = true;
      wizard.reactionEquation = opts.equation || "";
      wizard.reactionReactants = opts.reactants || "";
      wizard.reactionProducts = opts.products || "";
      wizard.name = "reaction_" + Math.random().toString(36).substring(2, 8);
      if (coordsStatus) {
        coordsStatus.textContent = `Reaction: ${wizard.reactionEquation} (All species 3D ready)`;
        coordsStatus.classList.add("is-set");
      }
      renderStep("calc", opts);
    } else if (opts.step === "builder3d" && opts.coords) {
      wizard.coords = opts.coords;
      wizard.name = opts.name || "optimized_molecule";
      wizard.formula = opts.formula || "";
      setCoordsStatus(`${wizard.name} (from calculation output)`);
      renderStep("builder3d", opts);
    } else {
      renderStep("coords", opts);
    }
  }
  window.openWizard = openWizard;
  window.openOrcaWizardForReaction = function(opts) {
    openWizard({ isReactionWorkflow: true, ...opts });
  };
  window.openOrcaWizardWithCoords = function(coords, name, meta = {}) {
    openWizard({ step: "builder3d", coords: coords, name: name, ...meta });
  };
  function closeWizard() {
    const win = modal.querySelector(".modal-window");
    if (win) win.classList.remove("is-builder-mode");
    hide(modal);
  }

  document.getElementById("orca-open-wizard").addEventListener("click", () => openWizard());
  document.getElementById("orca-modal-close").addEventListener("click", closeWizard);
  modal.addEventListener("click", (e) => { if (e.target === modal) closeWizard(); });

  function setModalChrome(stepNumber, title) {
    modalBadge.textContent = `Step ${stepNumber}`;
    modalTitle.textContent = title;
  }

  function optionGrid(options, cols, onPick, selectedValue) {
    const grid = document.createElement("div");
    grid.className = "option-grid";
    grid.style.setProperty("--cols", cols);
    const entries = Array.isArray(options) ? options.map(v => [v, v]) : Object.entries(options);
    entries.forEach(([value, label]) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "option-btn" + (value === selectedValue ? " is-selected" : "");
      btn.textContent = label;
      btn.addEventListener("click", () => onPick(value));
      grid.appendChild(btn);
    });
    return grid;
  }

  function backButton(onBack) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-ghost";
    btn.textContent = "◄ Back";
    btn.addEventListener("click", onBack);
    return btn;
  }

  function goBack() {
    stepStack.pop();
    const prev = stepStack.pop();
    if (prev) renderStep(prev.name, prev.opts);
    else closeWizard();
  }

  function renderStep(name, opts = {}) {
    stepStack.push({ name, opts });
    const win = modal.querySelector(".modal-window");
    if (win) {
      if (name === "builder3d") win.classList.add("is-builder-mode");
      else win.classList.remove("is-builder-mode");
    }
    modalBody.innerHTML = "";
    if (wizard && wizard.isReactionWorkflow) {
      const rxnNotice = document.createElement("div");
      rxnNotice.className = "alert alert-info reaction-wizard-banner";
      rxnNotice.style.cssText = "margin-bottom:1.25rem; border-left:4px solid #0ea5e9; background:rgba(14,165,233,0.12); padding:0.85rem 1rem; border-radius:8px; color:#f8fafc;";
      rxnNotice.innerHTML = `
        <div style="display:flex; align-items:center; gap:0.5rem; font-weight:bold; color:#38bdf8; margin-bottom:0.35rem;">
          <span>⚡</span>
          <span style="font-size:1rem;">Unified Reaction Quantum Calculation</span>
          <span class="badge badge-success text-xs" style="margin-left:auto; background:#10b981; color:#fff; padding:2px 8px; border-radius:4px;">Applied to All Species</span>
        </div>
        <div style="font-size:0.9rem; color:#e2e8f0; line-height:1.4;">
          <strong>Chemical Equation:</strong> <code style="color:#38bdf8; font-size:0.95rem; font-family:var(--font-mono);">${escapeHtml(wizard.reactionEquation || "")}</code>
        </div>
        <div style="font-size:0.84rem; color:#94a3b8; margin-top:0.4rem; line-height:1.4;">
          📌 <strong>Important Notice:</strong> The calculation method, functional, basis set, and quantum options chosen in this wizard will be applied uniformly across <strong>all reactants and products</strong> in the reaction equation.
        </div>
      `;
      modalBody.appendChild(rxnNotice);
    }
    STEP_RENDERERS[name](opts);
  }

  // ---- Step: coordinates (compound identification) ----
  function stepCoords(opts) {
    setModalChrome(1, "Select the molecular structure");
    const tabs = document.createElement("div");
    tabs.className = "modal-tabs";
    const tabSearch = document.createElement("button");
    tabSearch.className = "modal-tab is-active"; tabSearch.textContent = "🔍 Search by name";
    const tabUpload = document.createElement("button");
    tabUpload.className = "modal-tab"; tabUpload.textContent = "📁 Upload file (xyz/sdf/mol)";
    const tabManual = document.createElement("button");
    tabManual.className = "modal-tab"; tabManual.textContent = "✍️ Manual entry";
    const tabOutputs = document.createElement("button");
    tabOutputs.className = "modal-tab"; tabOutputs.textContent = "📊 From Calculation Outputs";
    tabs.append(tabSearch, tabUpload, tabManual, tabOutputs);
    modalBody.appendChild(tabs);

    const searchPane = document.createElement("div");
    const nameField = document.createElement("div");
    nameField.className = "modal-field";
    nameField.innerHTML = `<label>Compound name</label>`;
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.placeholder = "e.g. Aspirin";
    nameInput.value = opts.prefillQuery || "";
    nameField.appendChild(nameInput);
    const fetchBtn = document.createElement("button");
    fetchBtn.type = "button"; fetchBtn.className = "btn btn-primary";
    fetchBtn.textContent = "Fetch coordinates from PubChem";
    const searchStatus = document.createElement("p");
    searchStatus.className = "modal-hint";
    searchPane.append(nameField, fetchBtn, searchStatus);

    nameInput.addEventListener("input", () => {
      if (coordsStatus) {
        coordsStatus.textContent = "No structure selected yet.";
        coordsStatus.classList.remove("is-set");
      }
      searchStatus.textContent = "";
    });

    fetchBtn.addEventListener("click", async () => {
      const q = nameInput.value.trim();
      if (!q) return;
      fetchBtn.disabled = true;
      searchStatus.textContent = "Looking it up…";
      if (coordsStatus) {
        coordsStatus.textContent = "Looking it up…";
        coordsStatus.classList.remove("is-set");
      }
      try {
        const data = await postJSON("/api/orca/coords", { query: q });
        wizard.coords = data.coords;
        wizard.name = data.name;
        wizard.formula = data.formula;
        setCoordsStatus(`${data.name} (${data.formula || "-"})`);
        renderStep("builder3d", { coords: data.coords, name: data.name, formula: data.formula });
      } catch (err) {
        searchStatus.textContent = err.message;
        if (coordsStatus) {
          coordsStatus.textContent = "No structure selected yet.";
          coordsStatus.classList.remove("is-set");
        }
      } finally {
        fetchBtn.disabled = false;
      }
    });

    const uploadPane = document.createElement("div");
    uploadPane.classList.add("hidden");
    const uploadField = document.createElement("div");
    uploadField.className = "modal-field";
    uploadField.innerHTML = `<label>Choose a .xyz / .sdf / .mol file</label>`;
    const fileInput = document.createElement("input");
    fileInput.type = "file"; fileInput.accept = ".xyz,.sdf,.mol";
    uploadField.appendChild(fileInput);
    const uploadStatus = document.createElement("p");
    uploadStatus.className = "modal-hint";
    uploadPane.append(uploadField, uploadStatus);

    fileInput.addEventListener("change", async () => {
      const file = fileInput.files[0];
      if (!file) return;
      uploadStatus.textContent = "Parsing…";
      const form = new FormData();
      form.append("file", file);
      try {
        const resp = await fetch("/api/orca/coords/file", { method: "POST", body: form });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || "Failed to parse the file.");
        wizard.coords = data.coords;
        wizard.name = data.name;
        setCoordsStatus(`${data.name} (uploaded file)`);
        renderStep("builder3d", { coords: data.coords, name: data.name });
      } catch (err) {
        uploadStatus.textContent = err.message;
      }
    });

    const manualPane = document.createElement("div");
    manualPane.classList.add("hidden");
    const manualField = document.createElement("div");
    manualField.className = "modal-field";
    manualField.innerHTML = `<label>XYZ coordinates (element symbol then x y z, one atom per line)</label>`;
    const manualTextarea = document.createElement("textarea");
    manualTextarea.placeholder = "C 0.000 0.000 0.000\nH 0.629 0.629 0.629\n...";
    manualField.appendChild(manualTextarea);
    const manualNameField = document.createElement("div");
    manualNameField.className = "modal-field";
    manualNameField.innerHTML = `<label>Molecule name (used for the output filename)</label>`;
    const manualNameInput = document.createElement("input");
    manualNameInput.type = "text"; manualNameInput.placeholder = "molecule";
    manualNameField.appendChild(manualNameInput);
    const manualBtn = document.createElement("button");
    manualBtn.type = "button"; manualBtn.className = "btn btn-primary";
    manualBtn.textContent = "Continue to 3D Builder";
    manualPane.append(manualField, manualNameField, manualBtn);

    manualBtn.addEventListener("click", () => {
      const coords = manualTextarea.value.trim();
      if (!coords) return;
      wizard.coords = coords;
      wizard.name = manualNameInput.value.trim() || "molecule";
      setCoordsStatus(`${wizard.name} (manual entry)`);
      renderStep("builder3d", { coords: coords, name: wizard.name });
    });

    // ---- Calculation Outputs Selection Pane (Lazy Loaded on Click) ----
    const outputsPane = document.createElement("div");
    outputsPane.className = "wizard-outputs-pane hidden";
    let outputsLoaded = false;

    function populateOutputsPane() {
      if (outputsLoaded) return;
      outputsLoaded = true;

      // Collect actual calculation outputs from active session & saved jobs (Filtered strictly for Geometry Optimization: OPT)
      let availableOutputs = [];
      if (window.currentEngineData) {
        const d = window.currentEngineData;
        const job = d.latest_job || (d.jobs && d.jobs[0]) || {};
        if (isOptimizationJob(job) || isOptimizationJob(d) || (d.jobs && d.jobs.length > 1) || job.opt_energies) {
          let cleanCoords = "";
          if (job.xyz) {
            const rawLines = job.xyz.trim().split("\n");
            cleanCoords = (rawLines.length > 2 && /^\d+$/.test(rawLines[0].trim())) ? rawLines.slice(2).join("\n") : rawLines.join("\n");
          } else if (job.elements && job.coords && job.elements.length === job.coords.length) {
            cleanCoords = job.elements.map((elem, idx) => {
              const [x, y, z] = job.coords[idx];
              return `${elem.padEnd(3)} ${x.toFixed(6).padStart(12)} ${y.toFixed(6).padStart(12)} ${z.toFixed(6).padStart(12)}`;
            }).join("\n");
          }
          if (cleanCoords) {
            availableOutputs.push({
              id: "active_session",
              name: d.name || job.name || "ORCA Output (OPT)",
              formula: job.chemical_formula || job.formula || "-",
              atomsCount: (job.elements && job.elements.length) || (job.coords && job.coords.length) || "-",
              calcType: job.calculation_type || "Geometry Optimization (OPT)",
              method: job.functional || job.method || "DFT",
              basis: job.basis_set || "",
              coords: cleanCoords,
              badge: "Active in Analyzer (OPT)"
            });
          }
        }
      }

      try {
        const savedJobs = JSON.parse(localStorage.getItem(LS_KEYS.jobs) || "[]");
        // Only genuinely COMPLETED optimizations are offered as coordinate
        // sources: failed, aborted, unconverged and still-running jobs are
        // hidden (requirement: import shows only valid completed OPT results).
        const optJobs = savedJobs.filter(
          (sj) => isOptimizationJob(sj) && (sj.status === "complete" || sj.optimizedCoords)
        );
        optJobs.forEach((sj, idx) => {
          let cCoords = "";
          if (sj.result && sj.result.xyz) {
            const rawLines = sj.result.xyz.trim().split("\n");
            cCoords = (rawLines.length > 2 && /^\d+$/.test(rawLines[0].trim())) ? rawLines.slice(2).join("\n") : rawLines.join("\n");
          }
          if (cCoords) {
            availableOutputs.push({
              id: `job_${sj.id || sj.jobId || idx}`,
              jobId: sj.jobId,
              jobRef: sj,
              name: sj.name || `Job #${sj.id || idx}`,
              formula: (sj.result && sj.result.chemical_formula) || "-",
              atomsCount: (sj.result && sj.result.atom_count) || "-",
              calcType: sj.calc_type || "Geometry Optimization (OPT)",
              method: sj.method || "",
              basis: sj.basis || "",
              coords: cCoords,
              badge: "Completed Output (OPT)"
            });
          } else if (sj.status === "complete" || sj.jobId) {
            availableOutputs.push({
              id: `kaggle_${sj.jobId || idx}`,
              jobId: sj.jobId,
              jobRef: sj,
              name: sj.name || `Kaggle Job ${sj.jobId ? sj.jobId.slice(0, 8) : idx}`,
              formula: sj.chemical_formula || "-",
              atomsCount: "-",
              calcType: sj.calc_type || "Geometry Optimization (OPT)",
              method: sj.method || "ORCA",
              basis: sj.basis || "",
              coords: "",
              fetchRequired: true,
              badge: "Kaggle Cloud Job (OPT)"
            });
          }
        });
      } catch (e) {}

      let outputsListHTML = "";
      if (availableOutputs.length > 0) {
        outputsListHTML = `
          <div style="font-size:0.84rem; color:var(--text-muted); margin-bottom:0.6rem;">
            Select an optimized molecular geometry from your calculation outputs (OPT only):
          </div>
          <div class="outputs-sources-grid" style="display:flex; flex-direction:column; gap:0.6rem;">
            ${availableOutputs.map(out => `
              <div class="output-source-card" data-output-id="${escapeHtml(out.id)}">
                <div class="output-source-header">
                  <span class="source-badge">${escapeHtml(out.badge)}</span>
                  <strong>${escapeHtml(out.name)}</strong>
                </div>
                <div class="output-source-meta">
                  <span>Formula: <strong>${escapeHtml(out.formula)}</strong></span>
                  <span>Atoms: <strong>${escapeHtml(out.atomsCount)}</strong></span>
                  <span>Type: <strong>${escapeHtml(out.calcType)}</strong></span>
                  ${out.method ? `<span>Method: <strong>${escapeHtml(out.method)} ${escapeHtml(out.basis)}</strong></span>` : ""}
                </div>
                <button type="button" class="btn btn-primary btn-small btn-select-output" data-output-id="${escapeHtml(out.id)}" style="margin-top:6px; width:100%;">
                  ${out.fetchRequired ? '📥 Fetch & Load Geometry from Kaggle' : '🚀 Load this Output Geometry into 3D Builder'}
                </button>
              </div>
            `).join("")}
          </div>
        `;
      } else {
        outputsListHTML = `
          <div style="background:rgba(245, 158, 11, 0.08); border:1px solid rgba(245, 158, 11, 0.25); border-radius:var(--radius-s); padding:0.85rem 1rem; color:var(--text-muted); font-size:0.84rem; line-height:1.45;">
            <div style="display:flex; align-items:center; gap:6px; color:#f59e0b; font-weight:700; margin-bottom:4px;">
              <span>ℹ️</span> <span>No Geometry Optimization (OPT) Outputs Found</span>
            </div>
            No completed geometry optimization (OPT) calculations were found in your saved jobs or current session.
            <ul style="margin:0.4rem 0 0 1.2rem; padding:0; font-size:0.78rem;">
              <li>You can <strong>upload an ORCA output file (.out / .log / .txt)</strong> containing optimized coordinates directly below.</li>
              <li>Or launch an <strong>OPT calculation</strong> via the Kaggle Launcher and import its optimized geometry once completed.</li>
            </ul>
          </div>
        `;
      }

      outputsPane.innerHTML = `
        ${outputsListHTML}
        <div style="margin-top:0.85rem; border-top:1px dashed var(--border); padding-top:0.75rem;">
          <label style="font-size:0.82rem; font-weight:600; color:var(--text); margin-bottom:4px; display:block;">📁 Or choose / drop your ORCA output file (.out / .log / .txt):</label>
          <input type="file" id="wizard-output-file-input" accept=".out,.log,.txt,.molden" style="font-size:0.8rem;">
          <p id="wizard-output-file-status" class="modal-hint"></p>
        </div>
      `;

      outputsPane.querySelectorAll(".btn-select-output").forEach(btn => {
        btn.addEventListener("click", async () => {
          const outId = btn.getAttribute("data-output-id");
          const found = availableOutputs.find(o => o.id === outId);
          if (!found) return;

          if (found.fetchRequired && found.jobId) {
            const originalText = btn.innerHTML;
            btn.disabled = true;
            btn.textContent = "Fetching coordinates from Kaggle…";
            try {
              const res = await postJSON("/api/kaggle/extract-opt-coords", {
                ...credsFor(found.jobRef || found.jobId),
                job_id: found.jobId,
              });
              if (!res.coords) throw new Error("No 3D coordinates were found in this job's output.");
              found.coords = res.coords;
              found.formula = res.chemical_formula || found.formula;
              wizard.coords = res.coords;
              wizard.name = `${found.name}_opt`;
              wizard.formula = found.formula;
              setCoordsStatus(`${wizard.name} (${wizard.formula || "Kaggle Output"})`);
              renderStep("builder3d", { coords: res.coords, name: wizard.name, formula: wizard.formula });
              showToast(`Loaded optimized geometry from Kaggle job <strong>${found.name}</strong>.`);
            } catch (err) {
              showToast(`Failed to extract coordinates from Kaggle: ${err.message}`);
              btn.disabled = false;
              btn.innerHTML = originalText;
            }
            return;
          }

          wizard.coords = found.coords;
          wizard.name = `${found.name}_opt`;
          wizard.formula = found.formula;
          setCoordsStatus(`${wizard.name} (${wizard.formula || "output"})`);
          renderStep("builder3d", { coords: found.coords, name: wizard.name, formula: wizard.formula });
        });
      });

      const outFileInput = outputsPane.querySelector("#wizard-output-file-input");
      const outFileStatus = outputsPane.querySelector("#wizard-output-file-status");
      if (outFileInput) {
        outFileInput.addEventListener("change", async () => {
          const file = outFileInput.files[0];
          if (!file) return;
          outFileStatus.textContent = "Parsing ORCA output file…";
          const form = new FormData();
          form.append("file", file);
          try {
            const resp = await fetch("/api/orca/engine/parse", { method: "POST", body: form });
            const res = await resp.json();
            if (!resp.ok || !res.ok) throw new Error(res.error || "Failed to parse output file.");
            const job = res.latest_job || (res.jobs && res.jobs[0]) || (res.data && (res.data.latest_job || (res.data.jobs && res.data.jobs[0])));
            if (!job) throw new Error("No calculation data found in file.");
            let cleanCoords = "";
            if (job.xyz) {
              const rawLines = job.xyz.trim().split("\n");
              cleanCoords = (rawLines.length > 2 && /^\d+$/.test(rawLines[0].trim())) ? rawLines.slice(2).join("\n") : rawLines.join("\n");
            } else if (job.elements && job.coords && job.elements.length === job.coords.length) {
              cleanCoords = job.elements.map((elem, idx) => {
                const [x, y, z] = job.coords[idx];
                return `${elem.padEnd(3)} ${x.toFixed(6).padStart(12)} ${y.toFixed(6).padStart(12)} ${z.toFixed(6).padStart(12)}`;
              }).join("\n");
            }
            if (!cleanCoords) throw new Error("No 3D Cartesian coordinates found in output file.");
            const baseName = file.name.replace(/\.[^/.]+$/, "");
            wizard.coords = cleanCoords;
            wizard.name = baseName;
            wizard.formula = job.chemical_formula || job.formula || "";
            setCoordsStatus(`${wizard.name} (Output File)`);
            renderStep("builder3d", { coords: cleanCoords, name: wizard.name, formula: wizard.formula });
          } catch (err) {
            outFileStatus.textContent = err.message || "Failed to parse output file.";
          }
        });
      }
    }

    modalBody.append(searchPane, uploadPane, manualPane, outputsPane);

    function activateTab(tab, pane) {
      [tabSearch, tabUpload, tabManual, tabOutputs].forEach(t => t.classList.remove("is-active"));
      [searchPane, uploadPane, manualPane, outputsPane].forEach(p => p.classList.add("hidden"));
      tab.classList.add("is-active");
      if (tab === tabOutputs) {
        populateOutputsPane();
      }
      pane.classList.remove("hidden");
    }
    tabSearch.addEventListener("click", () => activateTab(tabSearch, searchPane));
    tabUpload.addEventListener("click", () => activateTab(tabUpload, uploadPane));
    tabManual.addEventListener("click", () => activateTab(tabManual, manualPane));
    tabOutputs.addEventListener("click", () => activateTab(tabOutputs, outputsPane));
  }

  // ---- Step: 3D Molecular Builder & Multi-Molecule Staging ----
  function stepBuilder3D(opts = {}) {
    setModalChrome(2, "3D Molecular Builder & Multi-Molecule Staging");
    
    // Parse input coordinates into atom objects
    const rawCoords = opts.coords || wizard.coords || "";
    let initialAtoms = [];
    const lines = rawCoords.trim().split("\n");
    let lineIdx = 0;
    if (lines.length > 2 && /^\d+$/.test(lines[0].trim())) {
      lineIdx = 2;
    }
    for (let i = lineIdx; i < lines.length; i++) {
      const parts = lines[i].trim().split(/\s+/);
      if (parts.length >= 4) {
        const elem = parts[0];
        const x = parseFloat(parts[1]);
        const y = parseFloat(parts[2]);
        const z = parseFloat(parts[3]);
        if (!isNaN(x) && !isNaN(y) && !isNaN(z)) {
          initialAtoms.push({
            id: initialAtoms.length + 1,
            elem: elem.charAt(0).toUpperCase() + elem.slice(1).toLowerCase(),
            x: x, y: y, z: z,
            molId: 1,
            molName: opts.name || wizard.name || "Compound 1"
          });
        }
      }
    }

    const vdwMap = {
      H: 1.20, He: 1.40, Li: 1.82, Be: 1.53, B: 1.92, C: 1.70,
      N: 1.55, O: 1.52, F: 1.47, Ne: 1.54, Na: 2.27, Mg: 1.73,
      Al: 1.84, Si: 2.10, P: 1.80, S: 1.80, Cl: 1.75, Ar: 1.88,
      K: 2.75, Ca: 2.31, Br: 1.85, I: 1.98
    };

    let bState = {
      atoms: JSON.parse(JSON.stringify(initialAtoms)),
      fragments: [{
        id: 1,
        name: opts.name || wizard.name || "Compound 1",
        color: "#2dd4bf",
        initialSnapshot: JSON.parse(JSON.stringify(initialAtoms))
      }],
      bonds: [],
      selectedAtomId: null,
      bondFirstAtomId: null,
      measurePicks: [],
      selectedFragId: 1,
      activeTool: "select",
      renderStyle: "stick_ball",
      showLabels: true,
      viewer: null,
      history: [],
      redoStack: []
    };

    function saveHistory() {
      bState.history.push({
        atoms: JSON.parse(JSON.stringify(bState.atoms)),
        fragments: JSON.parse(JSON.stringify(bState.fragments)),
        bonds: JSON.parse(JSON.stringify(bState.bonds))
      });
      bState.redoStack = [];
      if (bState.history.length > 25) bState.history.shift();
    }

    function computeFormula(atoms) {
      if (!atoms || atoms.length === 0) return "-";
      const counts = {};
      atoms.forEach(a => { counts[a.elem] = (counts[a.elem] || 0) + 1; });
      let order = [];
      if (counts["C"]) order.push("C");
      if (counts["H"]) order.push("H");
      Object.keys(counts).sort().forEach(e => {
        if (e !== "C" && e !== "H") order.push(e);
      });
      return order.map(e => (counts[e] > 1 ? `${e}${counts[e]}` : e)).join("");
    }

    function formatXYZ(atoms) {
      return atoms.map(a => `${a.elem.padEnd(3)} ${a.x.toFixed(6).padStart(12)} ${a.y.toFixed(6).padStart(12)} ${a.z.toFixed(6).padStart(12)}`).join("\n");
    }

    const container = document.createElement("div");
    container.className = "builder3d-container";

    // Top action bar
    const topBar = document.createElement("div");
    topBar.className = "builder3d-topbar";

    const toolGroup = document.createElement("div");
    toolGroup.className = "builder3d-toolgroup";

    const tools = [
      { id: "select", icon: "🔍", label: "Select / Inspect" },
      { id: "moveAtom", icon: "📍", label: "Move Single Atom" },
      { id: "move", icon: "🖐️", label: "Move Fragment (Others Fixed)" },
      { id: "bond", icon: "🔗", label: "Draw / Connect Bond" },
      { id: "delete", icon: "🗑️", label: "Delete Atom" },
      { id: "measure", icon: "📏", label: "Measure (Dist/Angle)" }
    ];

    const toolBtns = {};
    tools.forEach(t => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `builder3d-toolbtn ${t.id === bState.activeTool ? "is-active" : ""}`;
      btn.innerHTML = `<span>${t.icon}</span> <span>${t.label}</span>`;
      btn.addEventListener("click", () => {
        bState.activeTool = t.id;
        bState.bondFirstAtomId = null;
        bState.measurePicks = [];
        Object.values(toolBtns).forEach(b => b.classList.remove("is-active"));
        btn.classList.add("is-active");
        updateHint();
        updateSidebar();
        renderScene();
      });
      toolBtns[t.id] = btn;
      toolGroup.appendChild(btn);
    });

    // Add extra molecule search box & Outputs Import Button
    const addMolBox = document.createElement("div");
    addMolBox.className = "builder3d-add-mol-box";
    const addMolInput = document.createElement("input");
    addMolInput.type = "text";
    addMolInput.className = "builder3d-add-mol-input";
    addMolInput.placeholder = "Import compound (e.g. H2O, NH3, Cl-)";
    addMolInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        addMolBtn.click();
      }
    });

    const addMolBtn = document.createElement("button");
    addMolBtn.type = "button";
    addMolBtn.className = "btn btn-primary btn-small";
    addMolBtn.innerHTML = "<span>➕ Add</span>";

    const addAtomBtn = document.createElement("button");
    addAtomBtn.type = "button";
    addAtomBtn.className = "btn btn-ghost btn-small";
    addAtomBtn.innerHTML = "<span>⚛️ Add Atom</span>";
    addAtomBtn.title = "Add single atom (C, H, O, N, S, P, halogens, metals)";
    addAtomBtn.addEventListener("click", () => {
      const modalAtom = document.createElement("div");
      modalAtom.className = "modal-overlay";
      const commonElements = ["H", "C", "N", "O", "F", "P", "S", "Cl", "Br", "I", "B", "Si", "Na", "Mg", "Al", "K", "Ca", "Fe", "Cu", "Zn", "Pd", "Pt", "Au"];
      modalAtom.innerHTML = `
        <div class="modal-window" style="max-width:440px;">
          <div class="modal-header">
            <h3>⚛️ Add Single Atom to 3D Scene</h3>
            <button type="button" class="btn-close-atom-modal" style="background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:1.2rem;">✕</button>
          </div>
          <div class="modal-body" style="padding:1rem; display:flex; flex-direction:column; gap:0.8rem;">
            <label style="font-size:0.82rem; font-weight:600; color:var(--text);">Select Element:</label>
            <div style="display:grid; grid-template-columns:repeat(6, 1fr); gap:6px;">
              ${commonElements.map(el => `
                <button type="button" class="btn btn-ghost btn-small btn-quick-elem" data-elem="${el}" style="font-weight:700; font-family:var(--font-mono); font-size:0.9rem; padding:6px 0;">
                  ${el}
                </button>
              `).join("")}
            </div>
            <div style="display:flex; gap:6px; align-items:center; margin-top:4px;">
              <input type="text" id="builder-custom-elem-in" class="builder3d-add-mol-input" placeholder="Custom symbol (e.g. Ru, Ir)" style="max-width:160px; text-transform:capitalize;">
              <button type="button" id="builder-add-custom-elem-btn" class="btn btn-primary btn-small">Add Custom</button>
            </div>
          </div>
        </div>
      `;
      document.body.appendChild(modalAtom);
      modalAtom.querySelector(".btn-close-atom-modal").addEventListener("click", () => modalAtom.remove());
      
      const insertAtom = (sym) => {
        if (!sym) return;
        const cleanSym = sym.trim().charAt(0).toUpperCase() + sym.trim().slice(1).toLowerCase();
        saveHistory();
        let targetX = 0, targetY = 0, targetZ = 0;
        if (bState.atoms.length > 0) {
          const maxX = Math.max(...bState.atoms.map(a => a.x));
          targetX = maxX + 1.8;
          targetY = 0.0;
          targetZ = 0.0;
        }
        const newAtomId = bState.atoms.length + 1;
        bState.atoms.push({
          id: newAtomId,
          elem: cleanSym,
          x: targetX,
          y: targetY,
          z: targetZ,
          molId: bState.selectedFragId || 1,
          molName: (bState.fragments.find(f => f.id === bState.selectedFragId) || {}).name || "Compound 1"
        });
        bState.selectedAtomId = newAtomId;
        showToast(`Added atom ${cleanSym}${newAtomId} to 3D scene.`);
        updateSidebar();
        renderScene(true);
        modalAtom.remove();
      };

      modalAtom.querySelectorAll(".btn-quick-elem").forEach(btn => {
        btn.addEventListener("click", () => insertAtom(btn.getAttribute("data-elem")));
      });
      const customIn = modalAtom.querySelector("#builder-custom-elem-in");
      const customBtn = modalAtom.querySelector("#builder-add-custom-elem-btn");
      if (customBtn && customIn) {
        customBtn.addEventListener("click", () => insertAtom(customIn.value));
        customIn.addEventListener("keydown", (e) => { if (e.key === "Enter") insertAtom(customIn.value); });
      }
    });

    const addFromOutBtn = document.createElement("button");
    addFromOutBtn.type = "button";
    addFromOutBtn.className = "btn btn-ghost btn-small";
    addFromOutBtn.innerHTML = "<span>📥 Outputs</span>";
    addFromOutBtn.title = "Import fragment from calculation outputs";

    addMolBox.append(addMolInput, addMolBtn, addAtomBtn, addFromOutBtn);
    topBar.append(toolGroup, addMolBox);

    // Main layout
    const mainLayout = document.createElement("div");
    mainLayout.className = "builder3d-main-layout";

    const viewportWrap = document.createElement("div");
    viewportWrap.className = "builder3d-viewport-wrap";

    const canvasEl = document.createElement("div");
    canvasEl.className = "builder3d-canvas-el";
    canvasEl.id = "builder3d-viewport";

    const overlay = document.createElement("div");
    overlay.className = "builder3d-viewport-overlay";
    const hintBadge = document.createElement("div");
    hintBadge.className = "builder3d-hint-badge";
    hintBadge.id = "builder3d-hint-badge";
    hintBadge.innerHTML = "<span>💡</span> <span>Select mode: click any atom to inspect coordinates.</span>";

    const styleBar = document.createElement("div");
    styleBar.style.cssText = "display:flex; gap:4px; pointer-events:auto; margin-top:4px;";
    
    const styleOptions = [
      { id: "stick_ball", label: "Ball & Stick" },
      { id: "stick", label: "Sticks" },
      { id: "sphere", label: "Spacefill" },
      { id: "wire", label: "Wireframe" }
    ];
    styleOptions.forEach(s => {
      const sbtn = document.createElement("button");
      sbtn.type = "button";
      sbtn.className = "builder3d-nudge-btn";
      sbtn.style.fontSize = "0.72rem";
      sbtn.textContent = s.label;
      sbtn.addEventListener("click", (e) => {
        e.stopPropagation();
        bState.renderStyle = s.id;
        renderScene();
      });
      styleBar.appendChild(sbtn);
    });

    const labelToggleBtn = document.createElement("button");
    labelToggleBtn.type = "button";
    labelToggleBtn.className = "builder3d-nudge-btn";
    labelToggleBtn.style.fontSize = "0.72rem";
    labelToggleBtn.textContent = "🏷️ Labels: ON";
    labelToggleBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      bState.showLabels = !bState.showLabels;
      labelToggleBtn.textContent = bState.showLabels ? "🏷️ Labels: ON" : "🏷️ Labels: OFF";
      renderScene();
    });

    const recenterBtn = document.createElement("button");
    recenterBtn.type = "button";
    recenterBtn.className = "builder3d-nudge-btn";
    recenterBtn.style.fontSize = "0.72rem";
    recenterBtn.innerHTML = "<span>🎯 Center</span>";
    recenterBtn.title = "Recenter camera and fit structure into viewport";
    recenterBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (bState.viewer) {
        bState.viewer.zoomTo();
        bState.viewer.render();
      }
    });

    styleBar.append(labelToggleBtn, recenterBtn);

    overlay.append(hintBadge, styleBar);
    viewportWrap.append(canvasEl, overlay);

    // Sidebar
    const sidebar = document.createElement("div");
    sidebar.className = "builder3d-sidebar";

    const fragCard = document.createElement("div");
    fragCard.innerHTML = `<div class="builder3d-card-title"><span>Molecules / Fragments</span> <span id="builder3d-frag-count" class="mono">1</span></div>`;
    const fragList = document.createElement("div");
    fragList.id = "builder3d-frag-list";
    fragCard.appendChild(fragList);

    const transformCard = document.createElement("div");
    transformCard.id = "builder3d-transform-card";
    transformCard.innerHTML = `
      <div class="builder3d-card-title">
        <span>Spatial Manipulation</span>
        <span class="mono small" style="font-size:0.65rem; color:var(--teal);">Rigid / Local</span>
      </div>

      <!-- FRAGMENT MOVEMENT SUBSECTION -->
      <div id="builder3d-frag-move-section">
        <div style="font-size:0.75rem; color:var(--text-muted); margin-bottom:0.25rem; font-weight:600;">Active Fragment to Move:</div>
        <select id="builder3d-active-frag-select" class="input-tiny" style="margin-bottom:0.4rem; font-weight:600;"></select>
        <div class="builder-locked-notice" id="builder-frag-lock-notice">
          🔒 Active: <strong id="builder-active-frag-name">Compound 1</strong> (Other molecules remain fixed).
        </div>
        <div style="font-size:0.72rem; color:var(--text-muted); margin-bottom:0.25rem;">Translate Fragment:</div>
        <div class="builder3d-nudge-row">
          <span class="mono" style="color:#ef4444; font-weight:700;">X</span>
          <button type="button" class="builder3d-nudge-btn" data-axis="x" data-val="-0.5">◀ -0.5 Å</button>
          <button type="button" class="builder3d-nudge-btn" data-axis="x" data-val="0.5">+0.5 Å ▶</button>
        </div>
        <div class="builder3d-nudge-row">
          <span class="mono" style="color:#22c55e; font-weight:700;">Y</span>
          <button type="button" class="builder3d-nudge-btn" data-axis="y" data-val="-0.5">▼ -0.5 Å</button>
          <button type="button" class="builder3d-nudge-btn" data-axis="y" data-val="0.5">+0.5 Å ▲</button>
        </div>
        <div class="builder3d-nudge-row">
          <span class="mono" style="color:#3b82f6; font-weight:700;">Z</span>
          <button type="button" class="builder3d-nudge-btn" data-axis="z" data-val="-0.5">🔍 -0.5 Å</button>
          <button type="button" class="builder3d-nudge-btn" data-axis="z" data-val="0.5">+0.5 Å 🔎</button>
        </div>
        <div style="font-size:0.72rem; color:var(--text-muted); margin:0.4rem 0 0.25rem;">Rotate Active Fragment:</div>
        <div class="builder3d-nudge-row">
          <span class="mono" style="color:#f59e0b; font-weight:700;">⟳</span>
          <button type="button" class="builder3d-nudge-btn" data-rot="x" data-val="-15">X -15°</button>
          <button type="button" class="builder3d-nudge-btn" data-rot="x" data-val="15">X +15°</button>
        </div>
        <div class="builder3d-nudge-row">
          <span class="mono" style="color:#f59e0b; font-weight:700;">⟳</span>
          <button type="button" class="builder3d-nudge-btn" data-rot="y" data-val="-15">Y -15°</button>
          <button type="button" class="builder3d-nudge-btn" data-rot="y" data-val="15">Y +15°</button>
        </div>
        <div class="builder3d-nudge-row">
          <span class="mono" style="color:#f59e0b; font-weight:700;">⟳</span>
          <button type="button" class="builder3d-nudge-btn" data-rot="z" data-val="-15">Z -15°</button>
          <button type="button" class="builder3d-nudge-btn" data-rot="z" data-val="15">Z +15°</button>
        </div>
        <div style="margin-top:0.35rem;">
          <button type="button" id="builder3d-reset-frag-btn" class="builder3d-nudge-btn" style="width:100%; font-size:0.72rem;">🔄 Reset Fragment Position</button>
        </div>
      </div>

      <!-- SINGLE ATOM MOVEMENT SUBSECTION -->
      <div id="builder3d-atom-move-section" class="builder3d-atom-move-panel" style="margin-top:0.6rem;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
          <strong style="color:var(--teal); font-size:0.78rem;">📍 Move Single Atom</strong>
          <span id="builder-atom-target-badge" class="mono small" style="color:var(--text); font-weight:700;">None</span>
        </div>
        <div style="font-size:0.72rem; color:var(--text-muted); margin-bottom:0.35rem;">Displace selected atom independently:</div>
        <div class="builder3d-nudge-row">
          <span class="mono" style="color:#ef4444; font-weight:700;">X</span>
          <button type="button" class="builder3d-nudge-btn" data-atom-axis="x" data-val="-0.2">-0.2 Å</button>
          <button type="button" class="builder3d-nudge-btn" data-atom-axis="x" data-val="0.2">+0.2 Å</button>
        </div>
        <div class="builder3d-nudge-row">
          <span class="mono" style="color:#22c55e; font-weight:700;">Y</span>
          <button type="button" class="builder3d-nudge-btn" data-atom-axis="y" data-val="-0.2">-0.2 Å</button>
          <button type="button" class="builder3d-nudge-btn" data-atom-axis="y" data-val="0.2">+0.2 Å</button>
        </div>
        <div class="builder3d-nudge-row">
          <span class="mono" style="color:#3b82f6; font-weight:700;">Z</span>
          <button type="button" class="builder3d-nudge-btn" data-atom-axis="z" data-val="-0.2">-0.2 Å</button>
          <button type="button" class="builder3d-nudge-btn" data-atom-axis="z" data-val="0.2">+0.2 Å</button>
        </div>
        <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:4px; margin-top:0.35rem;">
          <div><label style="font-size:0.65rem; color:var(--text-muted); font-family:var(--font-mono);">X (Å)</label><input type="number" id="builder-atom-in-x" step="0.05" class="input-tiny mono"></div>
          <div><label style="font-size:0.65rem; color:var(--text-muted); font-family:var(--font-mono);">Y (Å)</label><input type="number" id="builder-atom-in-y" step="0.05" class="input-tiny mono"></div>
          <div><label style="font-size:0.65rem; color:var(--text-muted); font-family:var(--font-mono);">Z (Å)</label><input type="number" id="builder-atom-in-z" step="0.05" class="input-tiny mono"></div>
        </div>
        <button type="button" id="builder-pull-atom-btn" class="builder3d-nudge-btn" style="width:100%; margin-top:0.45rem; font-size:0.72rem; color:var(--teal);">↔️ Pull Atom Outward (+1.0 Å)</button>
      </div>
    `;

    const infoCard = document.createElement("div");
    infoCard.id = "builder3d-info-card";
    infoCard.innerHTML = `
      <div class="builder3d-card-title"><span>Atom / Measurement Details</span></div>
      <div id="builder3d-atom-detail" style="font-size:0.8rem; color:var(--text-muted); font-family:var(--font-mono); line-height:1.4;">
        No atom selected. Click any atom in 3D scene.
      </div>
    `;

    const toolActionsCard = document.createElement("div");
    toolActionsCard.style.cssText = "display:flex; flex-direction:column; gap:0.4rem; margin-top:auto;";
    const cleanBtn = document.createElement("button");
    cleanBtn.type = "button";
    cleanBtn.className = "btn btn-outline btn-small";
    cleanBtn.innerHTML = "<span>🧹 Clean Geometry (UFF)</span>";
    cleanBtn.title = "Preliminary force-field relaxation (not quantum-chemical optimization)";
    
    const uffHint = document.createElement("div");
    uffHint.style.cssText = "font-size:0.7rem; color:var(--text-faint); line-height:1.2; text-align:center;";
    uffHint.textContent = "Preliminary force-field relaxation (not final QM opt)";

    const undoRedoRow = document.createElement("div");
    undoRedoRow.style.cssText = "display:grid; grid-template-columns:1fr 1fr 1fr; gap:0.35rem;";
    
    const undoBtn = document.createElement("button");
    undoBtn.type = "button";
    undoBtn.className = "btn btn-ghost btn-small";
    undoBtn.innerHTML = "<span>↩️ Undo</span>";

    const redoBtn = document.createElement("button");
    redoBtn.type = "button";
    redoBtn.className = "btn btn-ghost btn-small";
    redoBtn.innerHTML = "<span>↪️ Redo</span>";

    const clearAllBtn = document.createElement("button");
    clearAllBtn.type = "button";
    clearAllBtn.className = "btn btn-ghost btn-small";
    clearAllBtn.style.color = "var(--red)";
    clearAllBtn.innerHTML = "<span>🗑️ Clear</span>";
    clearAllBtn.title = "Clear all atoms from 3D scene";
    clearAllBtn.addEventListener("click", () => {
      if (bState.atoms.length === 0) return;
      if (confirm("Are you sure you want to clear all atoms from the 3D scene?")) {
        saveHistory();
        bState.atoms = [];
        bState.fragments = [{ id: 1, name: "Compound 1", color: "#2dd4bf", initialSnapshot: [] }];
        bState.bonds = [];
        bState.selectedAtomId = null;
        bState.bondFirstAtomId = null;
        bState.measurePicks = [];
        bState.selectedFragId = 1;
        updateSidebar();
        renderScene(true);
        showToast("Cleared 3D builder scene.");
      }
    });

    undoRedoRow.append(undoBtn, redoBtn, clearAllBtn);
    toolActionsCard.append(cleanBtn, uffHint, undoRedoRow);
    sidebar.append(fragCard, transformCard, infoCard, toolActionsCard);
    mainLayout.append(viewportWrap, sidebar);

    const summaryBar = document.createElement("div");
    summaryBar.className = "builder3d-summary-bar";
    summaryBar.innerHTML = `
      <div><strong>Formula:</strong> <span id="builder3d-formula-val" class="mono" style="color:var(--teal); font-weight:700;">${computeFormula(bState.atoms)}</span></div>
      <div><strong>Total Atoms:</strong> <span id="builder3d-atom-count" class="mono">${bState.atoms.length}</span></div>
      <div><strong>Fragments:</strong> <span id="builder3d-frag-summary" class="mono">${bState.fragments.length}</span></div>
    `;

    const actions = document.createElement("div");
    actions.className = "modal-actions";
    const backBtn = backButton(goBack);
    const proceedBtn = document.createElement("button");
    proceedBtn.type = "button";
    proceedBtn.className = "btn btn-primary";
    proceedBtn.innerHTML = "<strong>Confirm 3D Structure &amp; Proceed to Calculation Settings →</strong>";

    actions.append(backBtn, proceedBtn);
    container.append(topBar, mainLayout, summaryBar, actions);
    modalBody.appendChild(container);

    function updateSummary() {
      const formulaEl = document.getElementById("builder3d-formula-val");
      const atomCountEl = document.getElementById("builder3d-atom-count");
      const fragSummaryEl = document.getElementById("builder3d-frag-summary");
      if (formulaEl) formulaEl.textContent = computeFormula(bState.atoms);
      if (atomCountEl) atomCountEl.textContent = bState.atoms.length;
      if (fragSummaryEl) fragSummaryEl.textContent = bState.fragments.length;
    }

    function updateSidebar() {
      const fList = document.getElementById("builder3d-frag-list");
      const fCount = document.getElementById("builder3d-frag-count");
      const fragSelect = document.getElementById("builder3d-active-frag-select");
      const fragNameNotice = document.getElementById("builder-active-frag-name");

      if (fCount) fCount.textContent = bState.fragments.length;

      if (fragSelect) {
        fragSelect.innerHTML = "";
        bState.fragments.forEach(f => {
          const opt = document.createElement("option");
          opt.value = f.id;
          const count = bState.atoms.filter(a => a.molId === f.id).length;
          opt.textContent = `${f.name} (${count} atoms)${f.id === bState.selectedFragId ? " - [ACTIVE]" : " - [LOCKED 🔒]"}`;
          if (f.id === bState.selectedFragId) opt.selected = true;
          fragSelect.appendChild(opt);
        });
      }

      const curFrag = bState.fragments.find(f => f.id === bState.selectedFragId) || bState.fragments[0];
      if (fragNameNotice && curFrag) {
        fragNameNotice.textContent = curFrag.name;
      }

      if (fList) {
        fList.innerHTML = "";
        bState.fragments.forEach(f => {
          const fItem = document.createElement("div");
          fItem.className = `builder3d-fragment-item ${f.id === bState.selectedFragId ? "is-selected" : ""}`;
          const nAtomsInFrag = bState.atoms.filter(a => a.molId === f.id).length;
          fItem.innerHTML = `
            <div style="display:flex; align-items:center; gap:6px;">
              <span style="display:inline-block; width:10px; height:10px; border-radius:50%; background:${f.color};"></span>
              <strong>${f.name}</strong>
            </div>
            <span class="mono small" style="color:var(--text-muted);">${nAtomsInFrag} atoms</span>
          `;
          fItem.addEventListener("click", () => {
            bState.selectedFragId = f.id;
            updateSidebar();
            renderScene();
          });
          fList.appendChild(fItem);
        });
      }

      // Update Single Atom inputs & badges
      const targetBadge = document.getElementById("builder-atom-target-badge");
      const inX = document.getElementById("builder-atom-in-x");
      const inY = document.getElementById("builder-atom-in-y");
      const inZ = document.getElementById("builder-atom-in-z");

      if (bState.selectedAtomId) {
        const a = bState.atoms.find(at => at.id === bState.selectedAtomId);
        if (a) {
          if (targetBadge) targetBadge.textContent = `${a.elem}${a.id}`;
          if (inX && document.activeElement !== inX) inX.value = a.x.toFixed(4);
          if (inY && document.activeElement !== inY) inY.value = a.y.toFixed(4);
          if (inZ && document.activeElement !== inZ) inZ.value = a.z.toFixed(4);
        }
      } else {
        if (targetBadge) targetBadge.textContent = "None";
        if (inX) inX.value = "";
        if (inY) inY.value = "";
        if (inZ) inZ.value = "";
      }

      const atomDetail = document.getElementById("builder3d-atom-detail");
      if (atomDetail) {
        if (bState.activeTool === "measure" && bState.measurePicks.length > 0) {
          const p = bState.measurePicks;
          if (p.length === 1) {
            atomDetail.innerHTML = `<div>Selected atom: <strong>${p[0].elem}${p[0].id}</strong></div><div style="color:var(--teal);">Click 2nd atom to measure distance.</div>`;
          } else if (p.length === 2) {
            const dx = p[1].x - p[0].x, dy = p[1].y - p[0].y, dz = p[1].z - p[0].z;
            const dist = Math.sqrt(dx*dx + dy*dy + dz*dz);
            atomDetail.innerHTML = `
              <div><strong>Distance:</strong> <span style="color:var(--teal); font-weight:700;">${dist.toFixed(4)} Å</span></div>
              <div style="color:var(--text-muted); font-size:0.75rem;">Between ${p[0].elem}${p[0].id} and ${p[1].elem}${p[1].id}</div>
              <div style="color:var(--text-faint); margin-top:2px;">Click 3rd atom for angle.</div>
            `;
          } else if (p.length === 3) {
            const u = { x: p[0].x - p[1].x, y: p[0].y - p[1].y, z: p[0].z - p[1].z };
            const v = { x: p[2].x - p[1].x, y: p[2].y - p[1].y, z: p[2].z - p[1].z };
            const dot = u.x*v.x + u.y*v.y + u.z*v.z;
            const lu = Math.sqrt(u.x*u.x + u.y*u.y + u.z*u.z);
            const lv = Math.sqrt(v.x*v.x + v.y*v.y + v.z*v.z);
            const cosVal = Math.max(-1, Math.min(1, dot / (lu * lv)));
            const angle = (Math.acos(cosVal) * 180) / Math.PI;
            atomDetail.innerHTML = `
              <div><strong>Angle:</strong> <span style="color:var(--teal); font-weight:700;">${angle.toFixed(2)}°</span></div>
              <div style="color:var(--text-muted); font-size:0.75rem;">${p[0].elem}${p[0].id} - ${p[1].elem}${p[1].id} - ${p[2].elem}${p[2].id}</div>
              <div style="color:var(--text-faint); margin-top:2px;">Click 4th atom for dihedral.</div>
            `;
          } else if (p.length === 4) {
            const b1 = { x: p[1].x - p[0].x, y: p[1].y - p[0].y, z: p[1].z - p[0].z };
            const b2 = { x: p[2].x - p[1].x, y: p[2].y - p[1].y, z: p[2].z - p[1].z };
            const b3 = { x: p[3].x - p[2].x, y: p[3].y - p[2].y, z: p[3].z - p[2].z };
            const n1 = { x: b1.y*b2.z - b1.z*b2.y, y: b1.z*b2.x - b1.x*b2.z, z: b1.x*b2.y - b1.y*b2.x };
            const n2 = { x: b2.y*b3.z - b2.z*b3.y, y: b2.z*b3.x - b2.x*b3.z, z: b2.x*b3.y - b2.y*b3.x };
            const lb2 = Math.sqrt(b2.x*b2.x + b2.y*b2.y + b2.z*b2.z);
            const ub2 = { x: b2.x/lb2, y: b2.y/lb2, z: b2.z/lb2 };
            const m1 = { x: n1.y*ub2.z - n1.z*ub2.y, y: n1.z*ub2.x - n1.x*ub2.z, z: n1.x*ub2.y - n1.y*ub2.x };
            const x = n1.x*n2.x + n1.y*n2.y + n1.z*n2.z;
            const y = m1.x*n2.x + m1.y*n2.y + m1.z*n2.z;
            const tau = (Math.atan2(y, x) * 180) / Math.PI;
            atomDetail.innerHTML = `
              <div><strong>Dihedral:</strong> <span style="color:var(--teal); font-weight:700;">${tau.toFixed(2)}°</span></div>
              <div style="color:var(--text-muted); font-size:0.75rem;">${p[0].elem}${p[0].id}-${p[1].elem}${p[1].id}-${p[2].elem}${p[2].id}-${p[3].elem}${p[3].id}</div>
            `;
          }
        } else if (bState.selectedAtomId) {
          const a = bState.atoms.find(at => at.id === bState.selectedAtomId);
          if (a) {
            const frag = bState.fragments.find(f => f.id === a.molId) || { name: "Compound" };
            atomDetail.innerHTML = `
              <div><strong>Atom:</strong> ${a.elem}${a.id} (${frag.name})</div>
              <div><strong>X:</strong> ${a.x.toFixed(4)} Å</div>
              <div><strong>Y:</strong> ${a.y.toFixed(4)} Å</div>
              <div><strong>Z:</strong> ${a.z.toFixed(4)} Å</div>
            `;
          } else {
            atomDetail.textContent = "No atom selected. Click any atom in 3D scene.";
          }
        } else {
          atomDetail.textContent = "No atom selected. Click any atom in 3D scene.";
        }
      }
    }

    function updateHint(customText) {
      const hintEl = document.getElementById("builder3d-hint-badge");
      if (!hintEl) return;
      if (customText) {
        hintEl.innerHTML = `<span>💡</span> <span>${customText}</span>`;
        return;
      }
      if (bState.activeTool === "select") {
        hintEl.innerHTML = "<span>💡</span> <span>Select / Direct Move: click an atom to inspect, or click and drag it directly in 3D.</span>";
      } else if (bState.activeTool === "moveAtom") {
        hintEl.innerHTML = "<span>📍</span> <span style='color:#f59e0b;'>Move Single Atom: click and drag atom with mouse. (Shift: axis snap, Alt / Shift+Wheel: depth).</span>";
      } else if (bState.activeTool === "move") {
        hintEl.innerHTML = "<span>🖐️</span> <span style='color:#38bdf8;'>Move Fragment: click and drag any atom to translate active fragment rigidly (others stay fixed).</span>";
      } else if (bState.activeTool === "delete") {
        hintEl.innerHTML = "<span>🗑️</span> <span style='color:#ef4444;'>Delete mode: click any atom to remove it from structure.</span>";
      } else if (bState.activeTool === "bond") {
        hintEl.innerHTML = "<span>🔗</span> <span style='color:#eab308;'>Draw / Connect Bond: click first atom, then second atom to connect with UFF placement.</span>";
      } else if (bState.activeTool === "measure") {
        hintEl.innerHTML = "<span>📏</span> <span style='color:#a855f7;'>Measure mode: click 2 atoms for distance, 3 for angle, 4 for dihedral.</span>";
      }
    }

    // =========================================================================
    // Mathematical 3D-to-Screen Projection & Direct Manipulation Helpers
    // =========================================================================
    function screenDeltaToWorld(dxPix, dyPix, refPos, viewer, modifierKeys = {}) {
      if (!viewer) return { dx: 0, dy: 0, dz: 0, pixPerAng: 35.0 };
      const view = viewer.getView(); // [cx, cy, cz, dist, qx, qy, qz, qw]
      const qx = view[4] || 0, qy = view[5] || 0, qz = view[6] || 0, qw = (view[7] !== undefined ? view[7] : 1);

      // Camera rotation matrix
      const r00 = 1 - 2 * (qy * qy + qz * qz);
      const r01 = 2 * (qx * qy - qz * qw);
      const r02 = 2 * (qx * qz + qy * qw);

      const r10 = 2 * (qx * qy + qz * qw);
      const r11 = 1 - 2 * (qx * qx + qz * qz);
      const r12 = 2 * (qy * qz - qx * qw);

      const r20 = 2 * (qx * qz - qy * qw);
      const r21 = 2 * (qy * qz + qx * qw);
      const r22 = 1 - 2 * (qx * qx + qy * qy);

      // Measure pixels per Angstrom at refPos
      const s0 = viewer.modelToScreen(refPos);
      const testStep = 0.5;
      const pRight = {
        x: refPos.x + r00 * testStep,
        y: refPos.y + r01 * testStep,
        z: refPos.z + r02 * testStep
      };
      const sRight = viewer.modelToScreen(pRight);
      const pixPerAng = (s0 && sRight) ? (Math.hypot(sRight.x - s0.x, sRight.y - s0.y) / testStep || 35.0) : 35.0;

      let effectiveDxPix = dxPix;
      let effectiveDyPix = dyPix;

      // Shift key: constrain to dominant screen axis
      if (modifierKeys.shiftKey) {
        if (Math.abs(dxPix) > Math.abs(dyPix)) {
          effectiveDyPix = 0;
        } else {
          effectiveDxPix = 0;
        }
      }

      const angX = effectiveDxPix / pixPerAng;
      const angY = -effectiveDyPix / pixPerAng; // Invert because screen Y is downwards

      let wDx = r00 * angX + r10 * angY;
      let wDy = r01 * angX + r11 * angY;
      let wDz = r02 * angX + r12 * angY;

      // Ctrl key: constrain to closest world Cartesian axis (X, Y, or Z)
      if (modifierKeys.ctrlKey) {
        const absX = Math.abs(wDx), absY = Math.abs(wDy), absZ = Math.abs(wDz);
        if (absX >= absY && absX >= absZ) {
          wDy = 0; wDz = 0;
        } else if (absY >= absX && absY >= absZ) {
          wDx = 0; wDz = 0;
        } else {
          wDx = 0; wDy = 0;
        }
      }

      // Alt key: depth displacement along camera normal vector
      if (modifierKeys.altKey) {
        const depthAng = -effectiveDyPix / pixPerAng;
        wDx = r20 * depthAng;
        wDy = r21 * depthAng;
        wDz = r22 * depthAng;
      }

      return { dx: wDx, dy: wDy, dz: wDz, pixPerAng, r20, r21, r22 };
    }

    function findAtomUnderCursor(screenX, screenY, maxDistPix = 24) {
      if (!bState.viewer || bState.atoms.length === 0) return null;
      let bestAtom = null;
      let bestScreenDist = maxDistPix;
      let bestDepth = -Infinity;

      const view = bState.viewer.getView();
      const qx = view[4] || 0, qy = view[5] || 0, qz = view[6] || 0, qw = (view[7] !== undefined ? view[7] : 1);
      const r20 = 2 * (qx * qz - qy * qw);
      const r21 = 2 * (qy * qz + qx * qw);
      const r22 = 1 - 2 * (qx * qx + qy * qy);

      for (const atom of bState.atoms) {
        const s = bState.viewer.modelToScreen({ x: atom.x, y: atom.y, z: atom.z });
        if (!s || isNaN(s.x) || isNaN(s.y)) continue;
        const dPix = Math.hypot(s.x - screenX, s.y - screenY);
        if (dPix <= bestScreenDist) {
          // Camera depth calculation (higher depth = closer to camera in 3Dmol)
          const camDepth = -(r20 * atom.x + r21 * atom.y + r22 * atom.z);
          if (dPix < bestScreenDist - 3 || camDepth > bestDepth) {
            bestScreenDist = dPix;
            bestDepth = camDepth;
            bestAtom = atom;
          }
        }
      }
      return bestAtom;
    }

    function updateLiveGeometryInfo(atomId) {
      const a = bState.atoms.find(at => at.id === atomId);
      if (!a) return;
      const inX = document.getElementById("builder-atom-in-x");
      const inY = document.getElementById("builder-atom-in-y");
      const inZ = document.getElementById("builder-atom-in-z");
      if (inX && document.activeElement !== inX) inX.value = a.x.toFixed(4);
      if (inY && document.activeElement !== inY) inY.value = a.y.toFixed(4);
      if (inZ && document.activeElement !== inZ) inZ.value = a.z.toFixed(4);

      // Find neighbor distances
      const neighborDistances = [];
      bState.atoms.forEach(other => {
        if (other.id !== a.id) {
          const isBonded = bState.bonds.some(b => (b.a1 === a.id && b.a2 === other.id) || (b.a1 === other.id && b.a2 === a.id));
          const d = Math.hypot(other.x - a.x, other.y - a.y, other.z - a.z);
          if (isBonded || d < 2.8) {
            neighborDistances.push({ elem: other.elem, id: other.id, dist: d, isBonded });
          }
        }
      });

      const atomDetail = document.getElementById("builder3d-atom-detail");
      if (atomDetail) {
        let neighborHtml = "";
        if (neighborDistances.length > 0) {
          neighborHtml = `<div style="margin-top:4px; font-size:0.75rem; color:var(--teal);"><strong>Neighbors:</strong> ${neighborDistances.slice(0, 3).map(n => `${n.elem}${n.id}: ${n.dist.toFixed(3)} Å${n.isBonded ? ' (bonded)' : ''}`).join(', ')}</div>`;
        }
        atomDetail.innerHTML = `
          <div><strong>Atom:</strong> ${a.elem}${a.id} (${bState.fragments.find(f => f.id === a.molId)?.name || 'Compound'})</div>
          <div><strong>X:</strong> ${a.x.toFixed(4)} Å &nbsp; <strong>Y:</strong> ${a.y.toFixed(4)} Å &nbsp; <strong>Z:</strong> ${a.z.toFixed(4)} Å</div>
          ${neighborHtml}
        `;
      }
    }

    async function formIntelligentBond(atom1Id, atom2Id) {
      const atom1 = bState.atoms.find(a => a.id === atom1Id);
      const atom2 = bState.atoms.find(a => a.id === atom2Id);
      if (!atom1 || !atom2 || atom1.id === atom2.id) return;

      // 1. Check if bond already exists
      const existingBond = bState.bonds.find(b => (b.a1 === atom1.id && b.a2 === atom2.id) || (b.a1 === atom2.id && b.a2 === atom1.id));
      if (existingBond) {
        showToast(`Bond already exists between ${atom1.elem}${atom1.id} and ${atom2.elem}${atom2.id}.`, "warning");
        bState.bondFirstAtomId = null;
        renderScene();
        return;
      }

      // 2. Valence limits check
      const maxValenceMap = { H: 1, He: 0, Li: 1, Be: 2, B: 4, C: 4, N: 4, O: 2, F: 1, Ne: 0, Na: 1, Mg: 2, Al: 3, Si: 4, P: 5, S: 6, Cl: 1, Br: 1, I: 1 };
      const count1 = bState.bonds.filter(b => b.a1 === atom1.id || b.a2 === atom1.id).length;
      const count2 = bState.bonds.filter(b => b.a1 === atom2.id || b.a2 === atom2.id).length;
      const max1 = maxValenceMap[atom1.elem] || 6;
      const max2 = maxValenceMap[atom2.elem] || 6;

      if (count1 >= max1) {
        showToast(`Cannot connect: ${atom1.elem}${atom1.id} has already reached its typical maximum valence of ${max1}.`, "warning");
        bState.bondFirstAtomId = null;
        renderScene();
        return;
      }
      if (count2 >= max2) {
        showToast(`Cannot connect: ${atom2.elem}${atom2.id} has already reached its typical maximum valence of ${max2}.`, "warning");
        bState.bondFirstAtomId = null;
        renderScene();
        return;
      }

      saveHistory();

      // 3. Check inter-fragment placement
      const isInterFragment = (atom1.molId !== atom2.molId);
      const dx = atom2.x - atom1.x;
      const dy = atom2.y - atom1.y;
      const dz = atom2.z - atom1.z;
      const currentDist = Math.sqrt(dx * dx + dy * dy + dz * dz);

      const covRadii = { H: 0.31, C: 0.76, N: 0.71, O: 0.66, F: 0.57, P: 1.07, S: 1.05, Cl: 1.02, Br: 1.20, I: 1.39, B: 0.84, Si: 1.11 };
      const r1 = covRadii[atom1.elem] || 0.77;
      const r2 = covRadii[atom2.elem] || 0.77;
      const targetBondLength = Math.max(0.9, (r1 + r2) * 1.02);

      if (isInterFragment && currentDist > targetBondLength * 1.25) {
        // Bring Fragment 2 rigidly towards Atom 1
        const shift = (targetBondLength - currentDist) / currentDist;
        const tx = dx * shift;
        const ty = dy * shift;
        const tz = dz * shift;

        const frag2Id = atom2.molId;
        bState.atoms.forEach(a => {
          if (a.molId === frag2Id) {
            a.x += tx;
            a.y += ty;
            a.z += tz;
          }
        });
      }

      // 4. Create the bond
      bState.bonds.push({ a1: atom1.id, a2: atom2.id, order: 1 });
      bState.bondFirstAtomId = null;

      showToast(`Formed bond between ${atom1.elem}${atom1.id} and ${atom2.elem}${atom2.id}. Running local UFF relaxation…`);
      renderScene(false);

      // 5. Run preliminary local UFF relaxation
      try {
        const currentXYZ = formatXYZ(bState.atoms);
        const res = await postJSON("/api/orca/builder/clean", { coords: currentXYZ });
        if (res && res.ok && res.coords) {
          const optLines = res.coords.trim().split("\n");
          optLines.forEach((line, idx) => {
            const p = line.trim().split(/\s+/);
            if (p.length >= 4 && bState.atoms[idx]) {
              bState.atoms[idx].x = parseFloat(p[1]);
              bState.atoms[idx].y = parseFloat(p[2]);
              bState.atoms[idx].z = parseFloat(p[3]);
            }
          });
          showToast("✔ Bond created and geometry relaxed with UFF. (Preliminary FF relaxation).");
        } else {
          showToast(`Bond created with initial placement. UFF local relaxation note: ${res?.error || "Kept placed geometry."}`, "info");
        }
      } catch (err) {
        showToast("Bond created with initial placement. (UFF optimization skipped).", "info");
      }
      updateSidebar();
      renderScene(false);
    }

    function handleAtomClick(atom3d) {
      if (!atom3d) return;
      let targetAtom = null;
      let minDist = Infinity;
      for (const a of bState.atoms) {
        const d = Math.hypot(a.x - (atom3d.x || 0), a.y - (atom3d.y || 0), a.z - (atom3d.z || 0));
        if (d < minDist) {
          minDist = d;
          targetAtom = a;
        }
      }
      if (!targetAtom || minDist > 0.6) {
        const clickedIdx = atom3d.index !== undefined ? atom3d.index : (atom3d.serial !== undefined ? atom3d.serial - 1 : 0);
        targetAtom = bState.atoms[clickedIdx];
      }
      if (!targetAtom) return;

      const clickedIdx = bState.atoms.findIndex(a => a.id === targetAtom.id);
      bState.selectedAtomId = targetAtom.id;
      bState.selectedFragId = targetAtom.molId;

      if (bState.activeTool === "select" || bState.activeTool === "moveAtom" || bState.activeTool === "move") {
        updateSidebar();
        renderScene();
      } else if (bState.activeTool === "delete") {
        saveHistory();
        bState.atoms.splice(clickedIdx, 1);
        bState.bonds = bState.bonds.filter(b => b.a1 !== targetAtom.id && b.a2 !== targetAtom.id);
        bState.atoms.forEach((a, idx) => { a.id = idx + 1; });
        showToast(`Deleted atom ${targetAtom.elem}${targetAtom.id}.`);
        bState.selectedAtomId = null;
        updateSidebar();
        renderScene(true);
      } else if (bState.activeTool === "bond") {
        if (!bState.bondFirstAtomId) {
          bState.bondFirstAtomId = targetAtom.id;
          updateHint(`First atom selected: ${targetAtom.elem}${targetAtom.id}. Now click second atom to create bond.`);
          renderScene();
        } else if (bState.bondFirstAtomId === targetAtom.id) {
          bState.bondFirstAtomId = null;
          updateHint();
          renderScene();
        } else {
          formIntelligentBond(bState.bondFirstAtomId, targetAtom.id);
        }
      } else if (bState.activeTool === "measure") {
        if (bState.measurePicks.length >= 4) bState.measurePicks = [];
        bState.measurePicks.push(targetAtom);
        updateSidebar();
        renderScene();
      }
    }

    function renderSceneFast() {
      if (!bState.viewer || bState.atoms.length === 0) return;
      const v = bState.viewer;
      v.clear();
      const xyzStr = formatXYZ(bState.atoms);
      const fullXyz = `${bState.atoms.length}\nORCA 3D Builder Model\n${xyzStr}`;
      v.addModel(fullXyz, "xyz");
      if (bState.renderStyle === "stick_ball") {
        v.setStyle({}, { stick: { radius: 0.14 }, sphere: { scale: 0.28 } });
      } else if (bState.renderStyle === "stick") {
        v.setStyle({}, { stick: { radius: 0.2 } });
      } else if (bState.renderStyle === "sphere") {
        v.setStyle({}, { sphere: { scale: 0.8 } });
      } else if (bState.renderStyle === "wire") {
        v.setStyle({}, { line: { linewidth: 2 } });
      }

      if (bState.selectedAtomId) {
        const aIdx = bState.atoms.findIndex(a => a.id === bState.selectedAtomId);
        if (aIdx >= 0) {
          v.setStyle({ index: aIdx }, { sphere: { scale: 0.48, color: "#f59e0b" }, stick: { radius: 0.25, color: "#f59e0b" } });
        }
      }
      v.render();
    }

    function renderScene(zoomToFit = false) {
      if (!window.$3Dmol) return;
      const vEl = document.getElementById("builder3d-viewport");
      if (!vEl) return;
      
      if (!bState.viewer) {
        bState.viewer = $3Dmol.createViewer(vEl, {
          defaultcolors: $3Dmol.elementColors.rasmol,
          backgroundColor: "#090e17"
        });
      }
      
      const v = bState.viewer;
      v.clear();
      
      if (bState.atoms.length === 0) {
        v.render();
        updateSummary();
        return;
      }
      
      const xyzStr = formatXYZ(bState.atoms);
      const fullXyz = `${bState.atoms.length}\nORCA 3D Builder Model\n${xyzStr}`;
      const m = v.addModel(fullXyz, "xyz");
      
      if (bState.renderStyle === "stick_ball") {
        v.setStyle({}, { stick: { radius: 0.14 }, sphere: { scale: 0.28 } });
      } else if (bState.renderStyle === "stick") {
        v.setStyle({}, { stick: { radius: 0.2 } });
      } else if (bState.renderStyle === "sphere") {
        v.setStyle({}, { sphere: { scale: 0.8 } });
      } else if (bState.renderStyle === "wire") {
        v.setStyle({}, { line: { linewidth: 2 } });
      }
      
      if (bState.bondFirstAtomId) {
        const aIdx = bState.atoms.findIndex(a => a.id === bState.bondFirstAtomId);
        if (aIdx >= 0) {
          v.setStyle({ index: aIdx }, { sphere: { scale: 0.48, color: "#eab308" }, stick: { radius: 0.24, color: "#eab308" } });
        }
      } else if (bState.measurePicks.length > 0) {
        bState.measurePicks.forEach(pa => {
          const aIdx = bState.atoms.findIndex(a => a.id === pa.id);
          if (aIdx >= 0) {
            v.setStyle({ index: aIdx }, { sphere: { scale: 0.48, color: "#a855f7" }, stick: { radius: 0.24, color: "#a855f7" } });
          }
        });
      } else if (bState.selectedAtomId) {
        const aIdx = bState.atoms.findIndex(a => a.id === bState.selectedAtomId);
        if (aIdx >= 0) {
          v.setStyle({ index: aIdx }, { sphere: { scale: 0.48, color: "#f59e0b" }, stick: { radius: 0.25, color: "#f59e0b" } });
        }
      }
      
      m.setClickable({}, true, function(atom3d) {
        handleAtomClick(atom3d);
      });
      
      if (bState.showLabels) {
        bState.atoms.forEach((a, idx) => {
          v.addLabel(`${a.elem}${idx + 1}`, {
            position: { x: a.x, y: a.y, z: a.z },
            backgroundColor: "rgba(15,23,42,0.75)",
            fontColor: "#ffffff",
            fontSize: 10,
            showBackground: true
          });
        });
      }
      
      if (zoomToFit) {
        v.zoomTo();
      }
      v.render();
      updateSummary();
    }

    // =========================================================================
    // Direct Manipulation Mouse Event Handlers (Avogadro-Style)
    // =========================================================================
    let dragState = null;
    let preDragSnapshot = null;

    canvasEl.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return; // Only left-click initiates dragging
      const rect = canvasEl.getBoundingClientRect();
      const screenX = e.clientX - rect.left;
      const screenY = e.clientY - rect.top;

      const pickedAtom = findAtomUnderCursor(screenX, screenY, 24);
      if (!pickedAtom) {
        // Click on empty space: allow default 3Dmol camera rotation
        return;
      }

      // If active tool is delete:
      if (bState.activeTool === "delete") {
        e.stopPropagation();
        e.preventDefault();
        saveHistory();
        const clickedIdx = bState.atoms.findIndex(a => a.id === pickedAtom.id);
        if (clickedIdx >= 0) {
          bState.atoms.splice(clickedIdx, 1);
          bState.bonds = bState.bonds.filter(b => b.a1 !== pickedAtom.id && b.a2 !== pickedAtom.id);
          bState.atoms.forEach((a, idx) => { a.id = idx + 1; });
          showToast(`Deleted atom ${pickedAtom.elem}${pickedAtom.id}.`);
          bState.selectedAtomId = null;
          updateSidebar();
          renderScene(true);
        }
        return;
      }

      // If active tool is measure:
      if (bState.activeTool === "measure") {
        e.stopPropagation();
        e.preventDefault();
        if (bState.measurePicks.length >= 4) bState.measurePicks = [];
        bState.measurePicks.push(pickedAtom);
        bState.selectedAtomId = pickedAtom.id;
        updateSidebar();
        renderScene();
        return;
      }

      // If active tool is bond:
      if (bState.activeTool === "bond") {
        e.stopPropagation();
        e.preventDefault();
        if (!bState.bondFirstAtomId) {
          bState.bondFirstAtomId = pickedAtom.id;
          bState.selectedAtomId = pickedAtom.id;
          updateHint(`First atom selected: ${pickedAtom.elem}${pickedAtom.id}. Click second atom to form bond.`);
          renderScene();
        } else if (bState.bondFirstAtomId === pickedAtom.id) {
          bState.bondFirstAtomId = null;
          updateHint();
          renderScene();
        } else {
          formIntelligentBond(bState.bondFirstAtomId, pickedAtom.id);
        }
        return;
      }

      // Direct Manipulation Dragging (select, moveAtom, move):
      e.stopPropagation();
      e.preventDefault();
      bState.selectedAtomId = pickedAtom.id;
      bState.selectedFragId = pickedAtom.molId;

      const isFragMove = (bState.activeTool === "move");
      preDragSnapshot = {
        atoms: JSON.parse(JSON.stringify(bState.atoms)),
        fragments: JSON.parse(JSON.stringify(bState.fragments)),
        bonds: JSON.parse(JSON.stringify(bState.bonds))
      };

      const startPositions = new Map();
      bState.atoms.forEach(a => {
        startPositions.set(a.id, { x: a.x, y: a.y, z: a.z });
      });

      dragState = {
        isDragging: true,
        hasMoved: false,
        dragType: isFragMove ? "fragment" : "atom",
        atomId: pickedAtom.id,
        fragId: pickedAtom.molId,
        startScreenX: screenX,
        startScreenY: screenY,
        refPos: { x: pickedAtom.x, y: pickedAtom.y, z: pickedAtom.z },
        startPositions: startPositions
      };

      canvasEl.style.cursor = "grabbing";
      updateSidebar();
      renderScene(false);
    });

    window.addEventListener("mousemove", (e) => {
      const rect = canvasEl.getBoundingClientRect();
      const screenX = e.clientX - rect.left;
      const screenY = e.clientY - rect.top;

      if (dragState && dragState.isDragging) {
        dragState.hasMoved = true;
        const dxPix = screenX - dragState.startScreenX;
        const dyPix = screenY - dragState.startScreenY;

        const delta = screenDeltaToWorld(dxPix, dyPix, dragState.refPos, bState.viewer, e);

        if (dragState.dragType === "atom") {
          const a = bState.atoms.find(at => at.id === dragState.atomId);
          const s0 = dragState.startPositions.get(dragState.atomId);
          if (a && s0) {
            a.x = s0.x + delta.dx;
            a.y = s0.y + delta.dy;
            a.z = s0.z + delta.dz;
          }
        } else if (dragState.dragType === "fragment") {
          bState.atoms.forEach(a => {
            if (a.molId === dragState.fragId) {
              const s0 = dragState.startPositions.get(a.id);
              if (s0) {
                a.x = s0.x + delta.dx;
                a.y = s0.y + delta.dy;
                a.z = s0.z + delta.dz;
              }
            }
          });
        }

        // Live smooth 60 FPS update
        renderSceneFast();
        updateLiveGeometryInfo(dragState.atomId);
        return;
      }

      // Hover feedback when mouse is moving over viewport without dragging
      if (screenX >= 0 && screenX <= rect.width && screenY >= 0 && screenY <= rect.height) {
        const hoverAtom = findAtomUnderCursor(screenX, screenY, 20);
        if (hoverAtom) {
          if (bState.activeTool === "bond" || bState.activeTool === "measure") {
            canvasEl.style.cursor = "crosshair";
          } else if (bState.activeTool === "delete") {
            canvasEl.style.cursor = "not-allowed";
          } else {
            canvasEl.style.cursor = "grab";
          }
        } else {
          canvasEl.style.cursor = "default";
        }
      }
    });

    window.addEventListener("mouseup", (e) => {
      if (dragState && dragState.isDragging) {
        if (dragState.hasMoved && preDragSnapshot) {
          // Validate numerical coordinates
          const hasInvalid = bState.atoms.some(a => isNaN(a.x) || isNaN(a.y) || isNaN(a.z) || !isFinite(a.x) || !isFinite(a.y) || !isFinite(a.z));
          if (hasInvalid) {
            bState.atoms = preDragSnapshot.atoms;
            showToast("Invalid numerical coordinates detected. Restored previous position.", "warning");
          } else {
            bState.history.push(preDragSnapshot);
            bState.redoStack = [];
            if (bState.history.length > 30) bState.history.shift();
          }
        }
        dragState = null;
        canvasEl.style.cursor = "default";
        updateSidebar();
        renderScene(false);
      }
    });

    // Shift/Alt + MouseWheel depth adjustment
    canvasEl.addEventListener("wheel", (e) => {
      if ((e.shiftKey || e.altKey) && bState.selectedAtomId) {
        e.preventDefault();
        e.stopPropagation();
        const a = bState.atoms.find(at => at.id === bState.selectedAtomId);
        if (!a || !bState.viewer) return;
        saveHistory();

        const view = bState.viewer.getView();
        const qx = view[4] || 0, qy = view[5] || 0, qz = view[6] || 0, qw = (view[7] !== undefined ? view[7] : 1);
        const r20 = 2 * (qx * qz - qy * qw);
        const r21 = 2 * (qy * qz + qx * qw);
        const r22 = 1 - 2 * (qx * qx + qy * qy);

        const step = (e.deltaY < 0 ? 0.25 : -0.25);
        if (bState.activeTool === "move") {
          // Move fragment depth
          bState.atoms.forEach(at => {
            if (at.molId === a.molId) {
              at.x += r20 * step;
              at.y += r21 * step;
              at.z += r22 * step;
            }
          });
        } else {
          // Move single atom depth
          a.x += r20 * step;
          a.y += r21 * step;
          a.z += r22 * step;
        }
        updateSidebar();
        renderScene(false);
        showToast(`Adjusted depth (${step > 0 ? '+0.25' : '-0.25'} Å along camera view).`);
      }
    }, { passive: false });

    // Expose for verification and testing
    window._builder3DState = bState;
    window._builder3DTools = {
      screenDeltaToWorld,
      findAtomUnderCursor,
      formIntelligentBond
    };

    // Fragment selector change handler
    const fragSelectEl = document.getElementById("builder3d-active-frag-select");
    if (fragSelectEl) {
      fragSelectEl.addEventListener("change", () => {
        bState.selectedFragId = parseInt(fragSelectEl.value, 10) || 1;
        updateSidebar();
        renderScene();
      });
    }

    // Helper to add extra molecule into scene with collision avoidance
    function importExtraMoleculeToScene(coords, molName) {
      const newLines = coords.trim().split("\n");
      let newAtoms = [];
      let nIdx = 0;
      if (newLines.length > 2 && /^\d+$/.test(newLines[0].trim())) nIdx = 2;
      for (let i = nIdx; i < newLines.length; i++) {
        const parts = newLines[i].trim().split(/\s+/);
        if (parts.length >= 4) {
          const elem = parts[0];
          const x = parseFloat(parts[1]);
          const y = parseFloat(parts[2]);
          const z = parseFloat(parts[3]);
          if (!isNaN(x) && !isNaN(y) && !isNaN(z)) {
            newAtoms.push({
              elem: elem.charAt(0).toUpperCase() + elem.slice(1).toLowerCase(),
              x: x, y: y, z: z
            });
          }
        }
      }
      if (newAtoms.length === 0) throw new Error("No atom coordinates found in structure.");

      let offsetX = 5.0;
      if (bState.atoms.length > 0) {
        const currentMaxX = Math.max(...bState.atoms.map(a => a.x));
        const newMinX = Math.min(...newAtoms.map(a => a.x));
        offsetX = (currentMaxX - newMinX) + 5.0;

        for (let iter = 0; iter < 20; iter++) {
          let collision = false;
          for (const a1 of bState.atoms) {
            for (const a2 of newAtoms) {
              const dx = (a2.x + offsetX) - a1.x;
              const dy = a2.y - a1.y;
              const dz = a2.z - a1.z;
              const dist = Math.sqrt(dx*dx + dy*dy + dz*dz);
              const r1 = vdwMap[a1.elem] || 1.70;
              const r2 = vdwMap[a2.elem] || 1.70;
              const safeThresh = Math.max(2.8, (r1 + r2) * 0.75);
              if (dist < safeThresh) {
                collision = true;
                offsetX += 1.0;
                break;
              }
            }
            if (collision) break;
          }
          if (!collision) break;
        }
      }

      const newMolId = bState.fragments.length + 1;
      const colors = ["#2dd4bf", "#f59e0b", "#a855f7", "#ec4899", "#3b82f6", "#10b981", "#ef4444"];
      const fragColor = colors[(newMolId - 1) % colors.length];
      const fragName = molName || `Compound ${newMolId}`;

      const shiftedSnapshot = newAtoms.map((na, idx) => ({
        id: bState.atoms.length + idx + 1,
        elem: na.elem,
        x: na.x + offsetX,
        y: na.y,
        z: na.z,
        molId: newMolId,
        molName: fragName
      }));

      bState.fragments.push({
        id: newMolId,
        name: fragName,
        color: fragColor,
        initialSnapshot: JSON.parse(JSON.stringify(shiftedSnapshot))
      });
      bState.selectedFragId = newMolId;

      shiftedSnapshot.forEach(atom => bState.atoms.push(atom));

      showToast(`Imported ${fragName} with safe clearance (+${offsetX.toFixed(1)} Å).`);
      updateSidebar();
      renderScene(true);
    }

    // Add extra molecule from PubChem
    addMolBtn.addEventListener("click", async () => {
      const q = addMolInput.value.trim();
      if (!q) return;
      addMolBtn.disabled = true;
      addMolBtn.textContent = "Fetching…";
      try {
        saveHistory();
        const data = await postJSON("/api/orca/coords", { query: q });
        if (!data || !data.coords) throw new Error("Could not resolve coordinates for " + q);
        importExtraMoleculeToScene(data.coords, data.name || q);
        addMolInput.value = "";
      } catch (err) {
        showToast(err.message || "Failed to import compound.", "error");
      } finally {
        addMolBtn.disabled = false;
        addMolBtn.innerHTML = "<span>➕ Add</span>";
      }
    });

    // Add extra molecule from Calculation Outputs / Files
    addFromOutBtn.addEventListener("click", () => {
      const modalOut = document.createElement("div");
      modalOut.className = "modal-overlay";

      let sessionCards = [];
      if (window.currentEngineData) {
        const d = window.currentEngineData;
        const job = d.latest_job || (d.jobs && d.jobs[0]) || {};
        if (isOptimizationJob(job) || isOptimizationJob(d) || (d.jobs && d.jobs.length > 1) || job.opt_energies) {
          let cleanCoords = "";
          if (job.xyz) {
            const rawLines = job.xyz.trim().split("\n");
            cleanCoords = (rawLines.length > 2 && /^\d+$/.test(rawLines[0].trim())) ? rawLines.slice(2).join("\n") : rawLines.join("\n");
          } else if (job.elements && job.coords && job.elements.length === job.coords.length) {
            cleanCoords = job.elements.map((elem, idx) => {
              const [x, y, z] = job.coords[idx];
              return `${elem.padEnd(3)} ${x.toFixed(6).padStart(12)} ${y.toFixed(6).padStart(12)} ${z.toFixed(6).padStart(12)}`;
            }).join("\n");
          }
          if (cleanCoords) {
            sessionCards.push({
              id: "active_session",
              name: d.name || job.name || "ORCA Output (OPT)",
              formula: job.chemical_formula || job.formula || "-",
              atomsCount: (job.elements && job.elements.length) || (job.coords && job.coords.length) || "-",
              coords: cleanCoords,
              badge: "Active in Analyzer (OPT)"
            });
          }
        }
      }

      try {
        const savedJobs = JSON.parse(localStorage.getItem(LS_KEYS.jobs) || "[]");
        const optJobs = savedJobs.filter(isOptimizationJob);
        optJobs.forEach((sj, idx) => {
          let cCoords = "";
          if (sj.result && sj.result.xyz) {
            const rawLines = sj.result.xyz.trim().split("\n");
            cCoords = (rawLines.length > 2 && /^\d+$/.test(rawLines[0].trim())) ? rawLines.slice(2).join("\n") : rawLines.join("\n");
          }
          if (cCoords) {
            sessionCards.push({
              id: `job_${sj.id || sj.jobId || idx}`,
              jobId: sj.jobId,
              jobRef: sj,
              name: sj.name || `Job #${sj.id || idx}`,
              formula: (sj.result && sj.result.chemical_formula) || "-",
              atomsCount: (sj.result && sj.result.atom_count) || "-",
              coords: cCoords,
              badge: "Completed Output (OPT)"
            });
          } else if (sj.status === "complete" || sj.jobId) {
            sessionCards.push({
              id: `kaggle_${sj.jobId || idx}`,
              jobId: sj.jobId,
              jobRef: sj,
              name: sj.name || `Kaggle Job ${sj.jobId ? sj.jobId.slice(0, 8) : idx}`,
              formula: sj.chemical_formula || "-",
              atomsCount: "-",
              coords: "",
              fetchRequired: true,
              badge: "Kaggle Cloud Job (OPT)"
            });
          }
        });
      } catch (e) {}

      let outputsListHTML = "";
      if (sessionCards.length > 0) {
        outputsListHTML = `
          <div style="display:flex; flex-direction:column; gap:0.5rem; max-height:240px; overflow-y:auto; padding-right:4px;">
            ${sessionCards.map(out => `
              <div class="output-source-card" style="padding:0.6rem 0.8rem;">
                <div class="output-source-header" style="margin-bottom:2px;">
                  <span class="source-badge">${out.badge}</span>
                  <strong style="font-size:0.85rem;">${out.name}</strong>
                </div>
                <div class="output-source-meta" style="font-size:0.75rem; margin-bottom:4px;">
                  <span>Formula: <strong>${out.formula}</strong></span>
                  ${out.atomsCount !== "-" ? `<span>Atoms: <strong>${out.atomsCount}</strong></span>` : ""}
                </div>
                <button type="button" class="btn btn-primary btn-small btn-import-out-item" data-id="${out.id}" style="width:100%; font-size:0.78rem; padding:4px 8px;">
                  ${out.fetchRequired ? '📥 Fetch & Import from Kaggle' : '➕ Import this Molecule into 3D Scene'}
                </button>
              </div>
            `).join("")}
          </div>
        `;
      } else {
        outputsListHTML = `<div style="color:var(--text-muted); font-size:0.84rem; margin-bottom:0.4rem;">No active calculations or saved outputs in the current session. You can upload an output file below:</div>`;
      }

      modalOut.innerHTML = `
        <div class="modal-window" style="max-width:520px;">
          <div class="modal-header">
            <h3>📥 Import Molecule from Calculation Outputs</h3>
            <button type="button" class="btn-close-outputs-modal" style="background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:1.2rem;">✕</button>
          </div>
          <div class="modal-body" style="padding:1rem; display:flex; flex-direction:column; gap:0.6rem;">
            ${outputsListHTML}
            <div style="border-top:1px dashed var(--border); padding-top:0.6rem;">
              <label style="font-size:0.8rem; font-weight:600; color:var(--text); margin-bottom:4px; display:block;">📁 Upload another ORCA Output (.out / .log / .xyz):</label>
              <input type="file" class="modal-extra-file-input" accept=".out,.log,.txt,.xyz,.sdf,.mol" style="font-size:0.8rem;">
              <p class="modal-extra-file-status modal-hint"></p>
            </div>
          </div>
        </div>
      `;
      document.body.appendChild(modalOut);

      modalOut.querySelector(".btn-close-outputs-modal").addEventListener("click", () => modalOut.remove());

      modalOut.querySelectorAll(".btn-import-out-item").forEach(btn => {
        btn.addEventListener("click", async () => {
          const outId = btn.getAttribute("data-id");
          const found = sessionCards.find(o => o.id === outId);
          if (!found) return;

          if (found.fetchRequired && found.jobId) {
            const originalText = btn.innerHTML;
            btn.disabled = true;
            btn.textContent = "Fetching coordinates from Kaggle…";
            try {
              const res = await postJSON("/api/kaggle/extract-opt-coords", {
                ...credsFor(found.jobRef || found.jobId),
                job_id: found.jobId,
              });
              if (!res.coords) throw new Error("No 3D coordinates found in this job's output.");
              saveHistory();
              importExtraMoleculeToScene(res.coords, found.name);
              modalOut.remove();
            } catch (err) {
              showToast(`Failed to extract coordinates from Kaggle: ${err.message}`);
              btn.disabled = false;
              btn.innerHTML = originalText;
            }
            return;
          }

          if (found.coords) {
            saveHistory();
            importExtraMoleculeToScene(found.coords, found.name);
            modalOut.remove();
          }
        });
      });

      const extraFileInput = modalOut.querySelector(".modal-extra-file-input");
      const extraFileStatus = modalOut.querySelector(".modal-extra-file-status");
      if (extraFileInput) {
        extraFileInput.addEventListener("change", async () => {
          const file = extraFileInput.files[0];
          if (!file) return;
          extraFileStatus.textContent = "Parsing file…";
          const form = new FormData();
          form.append("file", file);
          try {
            const isOut = file.name.endsWith(".out") || file.name.endsWith(".log") || file.name.endsWith(".txt");
            const url = isOut ? "/api/orca/engine/parse" : "/api/orca/coords/file";
            const resp = await fetch(url, { method: "POST", body: form });
            const res = await resp.json();
            if (!resp.ok || !res.ok) throw new Error(res.error || "Failed to parse file.");
            let cleanCoords = "";
            const job = res.latest_job || (res.jobs && res.jobs[0]) || (res.data && (res.data.latest_job || (res.data.jobs && res.data.jobs[0])));
            if (job) {
              if (job.xyz) {
                const rawLines = job.xyz.trim().split("\n");
                cleanCoords = (rawLines.length > 2 && /^\d+$/.test(rawLines[0].trim())) ? rawLines.slice(2).join("\n") : rawLines.join("\n");
              } else if (job.elements && job.coords) {
                cleanCoords = job.elements.map((elem, idx) => {
                  const [x, y, z] = job.coords[idx];
                  return `${elem.padEnd(3)} ${x.toFixed(6).padStart(12)} ${y.toFixed(6).padStart(12)} ${z.toFixed(6).padStart(12)}`;
                }).join("\n");
              }
            } else if (res.coords) {
              cleanCoords = res.coords;
            }
            if (!cleanCoords) throw new Error("No 3D coordinates found in file.");
            saveHistory();
            const baseName = file.name.replace(/\.[^/.]+$/, "");
            importExtraMoleculeToScene(cleanCoords, baseName);
            modalOut.remove();
          } catch (err) {
            extraFileStatus.textContent = err.message || "Failed to parse file.";
          }
        });
      }
    });

    // Rigid-body translations for active fragment only (others stay fixed)
    transformCard.querySelectorAll("[data-axis]").forEach(btn => {
      btn.addEventListener("click", () => {
        const axis = btn.getAttribute("data-axis");
        const val = parseFloat(btn.getAttribute("data-val"));
        saveHistory();
        bState.atoms.forEach(a => {
          if (a.molId === bState.selectedFragId) {
            if (axis === "x") a.x += val;
            if (axis === "y") a.y += val;
            if (axis === "z") a.z += val;
          }
        });
        updateSidebar();
        renderScene();
      });
    });

    // Rigid-body rotations around active fragment centroid (others stay fixed)
    transformCard.querySelectorAll("[data-rot]").forEach(btn => {
      btn.addEventListener("click", () => {
        const axis = btn.getAttribute("data-rot");
        const deg = parseFloat(btn.getAttribute("data-val"));
        const rad = (deg * Math.PI) / 180;
        saveHistory();
        
        const fragAtoms = bState.atoms.filter(a => a.molId === bState.selectedFragId);
        if (fragAtoms.length === 0) return;
        const cx = fragAtoms.reduce((s, a) => s + a.x, 0) / fragAtoms.length;
        const cy = fragAtoms.reduce((s, a) => s + a.y, 0) / fragAtoms.length;
        const cz = fragAtoms.reduce((s, a) => s + a.z, 0) / fragAtoms.length;

        fragAtoms.forEach(a => {
          let dx = a.x - cx;
          let dy = a.y - cy;
          let dz = a.z - cz;
          if (axis === "x") {
            const ny = dy * Math.cos(rad) - dz * Math.sin(rad);
            const nz = dy * Math.sin(rad) + dz * Math.cos(rad);
            a.y = cy + ny;
            a.z = cz + nz;
          } else if (axis === "y") {
            const nx = dx * Math.cos(rad) + dz * Math.sin(rad);
            const nz = -dx * Math.sin(rad) + dz * Math.cos(rad);
            a.x = cx + nx;
            a.z = cz + nz;
          } else if (axis === "z") {
            const nx = dx * Math.cos(rad) - dy * Math.sin(rad);
            const ny = dx * Math.sin(rad) + dy * Math.cos(rad);
            a.x = cx + nx;
            a.y = cy + ny;
          }
        });
        updateSidebar();
        renderScene();
      });
    });

    // Single Atom Translation Nudge Buttons
    transformCard.querySelectorAll("[data-atom-axis]").forEach(btn => {
      btn.addEventListener("click", () => {
        if (!bState.selectedAtomId) {
          showToast("Click an atom in the 3D scene to select it first.", "warning");
          return;
        }
        const targetA = bState.atoms.find(a => a.id === bState.selectedAtomId);
        if (!targetA) return;
        const axis = btn.getAttribute("data-atom-axis");
        const val = parseFloat(btn.getAttribute("data-val"));
        saveHistory();
        if (axis === "x") targetA.x += val;
        if (axis === "y") targetA.y += val;
        if (axis === "z") targetA.z += val;
        updateSidebar();
        renderScene();
      });
    });

    // Single Atom Direct Coordinate Input Listeners
    ["x", "y", "z"].forEach(axis => {
      const inEl = document.getElementById(`builder-atom-in-${axis}`);
      if (inEl) {
        inEl.addEventListener("input", () => {
          if (!bState.selectedAtomId) return;
          const targetA = bState.atoms.find(a => a.id === bState.selectedAtomId);
          if (!targetA) return;
          const val = parseFloat(inEl.value);
          if (!isNaN(val)) {
            targetA[axis] = val;
            renderScene();
            updateSummary();
          }
        });
      }
    });

    // Single Atom Outward Radial Pull
    const pullAtomBtn = document.getElementById("builder-pull-atom-btn");
    if (pullAtomBtn) {
      pullAtomBtn.addEventListener("click", () => {
        if (!bState.selectedAtomId) {
          showToast("Click an atom in the 3D scene to select it first.", "warning");
          return;
        }
        const targetA = bState.atoms.find(a => a.id === bState.selectedAtomId);
        if (!targetA) return;
        saveHistory();
        const parentFragAtoms = bState.atoms.filter(a => a.molId === targetA.molId);
        let cx = 0, cy = 0, cz = 0;
        if (parentFragAtoms.length > 1) {
          cx = parentFragAtoms.reduce((s, a) => s + a.x, 0) / parentFragAtoms.length;
          cy = parentFragAtoms.reduce((s, a) => s + a.y, 0) / parentFragAtoms.length;
          cz = parentFragAtoms.reduce((s, a) => s + a.z, 0) / parentFragAtoms.length;
        }
        let vx = targetA.x - cx;
        let vy = targetA.y - cy;
        let vz = targetA.z - cz;
        let norm = Math.sqrt(vx*vx + vy*vy + vz*vz);
        if (norm < 1e-4) {
          vx = 1.0; vy = 0.0; vz = 0.0; norm = 1.0;
        }
        targetA.x += (vx / norm) * 1.0;
        targetA.y += (vy / norm) * 1.0;
        targetA.z += (vz / norm) * 1.0;
        showToast(`Displaced ${targetA.elem}${targetA.id} outward by +1.0 Å.`);
        updateSidebar();
        renderScene();
      });
    }

    // Reset fragment position & rotation
    const resetFragBtn = document.getElementById("builder3d-reset-frag-btn");
    if (resetFragBtn) {
      resetFragBtn.addEventListener("click", () => {
        const currentFrag = bState.fragments.find(f => f.id === bState.selectedFragId);
        if (!currentFrag || !currentFrag.initialSnapshot) {
          showToast("No initial position snapshot available for this fragment.");
          return;
        }
        saveHistory();
        currentFrag.initialSnapshot.forEach(snapAtom => {
          const match = bState.atoms.find(a => a.id === snapAtom.id);
          if (match) {
            match.x = snapAtom.x;
            match.y = snapAtom.y;
            match.z = snapAtom.z;
          }
        });
        showToast(`Reset ${currentFrag.name} to its initial position.`);
        updateSidebar();
        renderScene();
      });
    }

    // Clean Geometry (UFF Force Field)
    cleanBtn.addEventListener("click", async () => {
      if (bState.atoms.length === 0) return;
      cleanBtn.disabled = true;
      cleanBtn.textContent = "Relaxing geometry…";
      try {
        saveHistory();
        const currentXYZ = formatXYZ(bState.atoms);
        const res = await postJSON("/api/orca/builder/clean", { coords: currentXYZ });
        if (res && res.ok && res.coords) {
          const optLines = res.coords.trim().split("\n");
          optLines.forEach((line, idx) => {
            const p = line.trim().split(/\s+/);
            if (p.length >= 4 && bState.atoms[idx]) {
              bState.atoms[idx].x = parseFloat(p[1]);
              bState.atoms[idx].y = parseFloat(p[2]);
              bState.atoms[idx].z = parseFloat(p[3]);
            }
          });
          showToast("Geometry relaxed with Universal Force Field (UFF).");
          renderScene(true);
        } else if (res && !res.ok) {
          showToast(res.error || "UFF relaxation could not converge. Original geometry preserved.", "warning");
        }
      } catch (err) {
        showToast(err.message || "Geometry relaxation request failed. Original geometry preserved.", "warning");
      } finally {
        cleanBtn.disabled = false;
        cleanBtn.innerHTML = "<span>🧹 Clean Geometry (UFF)</span>";
      }
    });

    // Undo action
    undoBtn.addEventListener("click", () => {
      if (bState.history.length === 0) {
        showToast("Nothing to undo.");
        return;
      }
      bState.redoStack.push({
        atoms: JSON.parse(JSON.stringify(bState.atoms)),
        fragments: JSON.parse(JSON.stringify(bState.fragments)),
        bonds: JSON.parse(JSON.stringify(bState.bonds))
      });
      const prev = bState.history.pop();
      bState.atoms = prev.atoms;
      bState.fragments = prev.fragments;
      bState.bonds = prev.bonds || [];
      bState.selectedAtomId = null;
      bState.bondFirstAtomId = null;
      bState.measurePicks = [];
      showToast("Undid last action.");
      updateSidebar();
      renderScene(true);
    });

    // Redo action
    redoBtn.addEventListener("click", () => {
      if (bState.redoStack.length === 0) {
        showToast("Nothing to redo.");
        return;
      }
      bState.history.push({
        atoms: JSON.parse(JSON.stringify(bState.atoms)),
        fragments: JSON.parse(JSON.stringify(bState.fragments)),
        bonds: JSON.parse(JSON.stringify(bState.bonds))
      });
      const next = bState.redoStack.pop();
      bState.atoms = next.atoms;
      bState.fragments = next.fragments;
      bState.bonds = next.bonds || [];
      bState.selectedAtomId = null;
      bState.bondFirstAtomId = null;
      bState.measurePicks = [];
      showToast("Redid action.");
      updateSidebar();
      renderScene(true);
    });

    // Proceed to calculation setup
    proceedBtn.addEventListener("click", () => {
      if (bState.atoms.length === 0) {
        showToast("Cannot proceed with an empty structure.", "error");
        return;
      }
      const finalXYZ = formatXYZ(bState.atoms);
      wizard.coords = finalXYZ;
      wizard.name = bState.fragments.map(f => f.name).join("_") || "molecule";
      wizard.formula = computeFormula(bState.atoms);
      setCoordsStatus(`${wizard.name} (${wizard.formula}, ${bState.atoms.length} atoms)`);
      renderStep("calc");
    });

    // Initial render
    setTimeout(() => {
      updateSidebar();
      renderScene(true);
    }, 50);
  }

  function setCoordsStatus(text) {
    coordsStatus.textContent = `✔ Selected structure: ${text}`;
    coordsStatus.classList.add("is-set");
  }

  function stepCalc() {
    setModalChrome(2, "Calculation type");

    const wfSwitch = document.createElement("div");
    wfSwitch.className = "wizard-workflow-switch";
    wfSwitch.innerHTML = `
      <button type="button" class="workflow-option-btn ${!wizard.isDualWorkflow ? 'is-active' : ''}" id="btn-wf-single">
        <span>🔹 Single Calculation</span>
      </button>
      <button type="button" class="workflow-option-btn ${wizard.isDualWorkflow ? 'is-active' : ''}" id="btn-wf-dual">
        <span>⚡ Chained Multi-Stage Workflow (2 to 5 Stages)</span>
      </button>
    `;
    modalBody.appendChild(wfSwitch);

    const btnSingle = wfSwitch.querySelector("#btn-wf-single");
    const btnDual = wfSwitch.querySelector("#btn-wf-dual");
    btnSingle.addEventListener("click", () => {
      wizard.isDualWorkflow = false;
      wizard.isWorkflow = false;
      wizard.workflowStageCount = 1;
      renderStep("calc");
    });
    btnDual.addEventListener("click", () => {
      wizard.isDualWorkflow = true;
      wizard.isWorkflow = true;
      wizard.workflowStageCount = wizard.workflowStageCount || 2;
      renderStep("calc");
    });

    if (wizard.isDualWorkflow) {
      const totalStages = wizard.workflowStageCount || 2;
      const stageCountCard = document.createElement("div");
      stageCountCard.className = "stage-header-card";
      stageCountCard.style.cssText = "display:flex; flex-direction:column; align-items:flex-start; gap:0.6rem;";
      
      stageCountCard.innerHTML = `
        <div style="display:flex; justify-content:space-between; width:100%; align-items:center;">
          <div>
            <strong style="color:var(--text); font-size:0.88rem;">Chained Multi-Stage Workflow</strong>
            <div style="font-size:0.75rem; color:var(--text-muted); margin-top:2px;">
              Each consecutive calculation automatically receives the converged equilibrium coordinates (.xyz) from the previous stage.
            </div>
          </div>
          <span class="stage-pill">${totalStages} Stages</span>
        </div>
        <div style="display:flex; gap:8px; align-items:center; width:100%; flex-wrap:wrap;">
          <span style="font-size:0.78rem; font-weight:600; color:var(--text-muted);">Number of Consecutive Stages:</span>
          <div style="display:flex; gap:4px;">
            ${[2, 3, 4, 5].map(cnt => `
              <button type="button" class="btn btn-small ${cnt === totalStages ? 'btn-primary' : 'btn-ghost'} btn-stage-cnt-select" data-cnt="${cnt}" style="padding:2px 10px; font-size:0.8rem;">
                ${cnt} Stages
              </button>
            `).join("")}
          </div>
        </div>
        <div style="font-size:0.75rem; color:var(--teal); font-weight:600; font-family:var(--font-mono); margin-top:2px;">
          Stage 1 of ${totalStages}: Geometry Optimization / Initial Exploration
        </div>
      `;
      modalBody.appendChild(stageCountCard);
      stageCountCard.querySelectorAll(".btn-stage-cnt-select").forEach(b => {
        b.addEventListener("click", () => {
          wizard.workflowStageCount = parseInt(b.getAttribute("data-cnt"), 10) || 2;
          renderStep("calc");
        });
      });
    }

    modalBody.appendChild(optionGrid(CFG.calc_types, 1, (val) => {
      wizard.calc_type = val;
      if (val === "custom") renderStep("customLine");
      else renderStep("family");
    }, wizard.calc_type));
    modalBody.appendChild(backButton(goBack));
  }

  function stepCustomLine() {
    setModalChrome(2, "Custom input block");
    const field = document.createElement("div");
    field.className = "modal-field";
    field.innerHTML = `<label>The first line must start with "!". You can add extra lines/blocks too, e.g. %geom ... end or %neb ... end - cores/RAM and the coordinate block are still appended automatically after this.</label>`;
    const textarea = document.createElement("textarea");
    textarea.value = wizard.custom_line || "";
    textarea.placeholder = "! B3LYP def2-TZVP Opt\n\n%geom\n  MaxIter 200\nend";
    textarea.rows = 8;
    field.appendChild(textarea);
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    actions.append(backButton(goBack));
    const nextBtn = document.createElement("button");
    nextBtn.type = "button"; nextBtn.className = "btn btn-primary";
    nextBtn.textContent = "Next ►";
    nextBtn.addEventListener("click", () => {
      const text = textarea.value.trim();
      if (!text.startsWith("!")) { textarea.focus(); return; }
      wizard.custom_line = text;
      renderStep("charge");
    });
    actions.append(nextBtn);
    modalBody.append(field, actions);
  }

  const THEORY_FAMILIES = {
    f_comp: "Composite methods (r2SCAN-3C...)",
    f_dft: "DFT (B3LYP, wB97M-V...)",
    f_mp2: "MP2 and variants",
    f_ccsd: "CCSD / highly correlated",
    f_hf: "Hartree-Fock (HF)",
  };

  function stepFamily() {
    setModalChrome(3, "Theory family");
    modalBody.appendChild(optionGrid(THEORY_FAMILIES, 1, (val) => {
      wizard.family = val;
      if (val === "f_comp") renderStep("method", { list: CFG.composite_methods, cols: 2 });
      else if (val === "f_dft") renderStep("method", { list: CFG.dft_functionals, cols: 2 });
      else if (val === "f_mp2") renderStep("method", { list: CFG.mp2_variants, cols: 1 });
      else if (val === "f_ccsd") renderStep("method", { list: CFG.ccsd_variants, cols: 1 });
      else if (val === "f_hf") { wizard.theory = "HF"; renderStep("dispersion"); }
    }, wizard.family));
    modalBody.appendChild(backButton(goBack));
  }

  function stepMethod(opts) {
    setModalChrome(4, "Choose the method");
    modalBody.appendChild(optionGrid(opts.list, opts.cols, (val) => {
      wizard.theory = val;
      renderStep("scf");
    }, wizard.theory));
    modalBody.appendChild(backButton(goBack));
  }

  function stepSCF() {
    setModalChrome(5, "SCF Convergence (ORCA 6.1)");
    const scfOpts = CFG.scf_options || {
      none: "Default SCF",
      tightscf: "TightSCF (Recommended for Freq/Opt)",
      verytightscf: "VeryTightSCF (High Precision)",
      normalscf: "NormalSCF",
      loosescf: "LooseSCF (Quick Preliminary)",
      slowconv: "SlowConv (Hard to Converge)",
    };
    modalBody.appendChild(optionGrid(scfOpts, 1, (val) => {
      wizard.scf_conv = val;
      const isDLPNO = (wizard.theory || "").includes("DLPNO");
      const methodLower = (wizard.theory || "").toLowerCase();
      const hasBuiltinDisp = methodLower.endsWith("-v") || methodLower.includes("-d4") || methodLower.includes("-d3") || methodLower.includes("-3c");
      if (wizard.family === "f_dft" && !isDLPNO && !hasBuiltinDisp) {
        renderStep("dispersion");
      } else if (isDLPNO || wizard.family === "f_comp" || methodLower.includes("-3c")) {
        wizard.ri_type = "none"; wizard.disp = "none"; wizard.basis = "";
        if (wizard.family === "f_comp" || methodLower.includes("-3c")) renderStep("solvation");
        else renderStep("basisFamily");
      } else {
        wizard.disp = "none";
        renderStep("ri");
      }
    }, wizard.scf_conv || "none"));
    modalBody.appendChild(backButton(goBack));
  }

  function stepDispersion() {
    setModalChrome(6, "Dispersion model");
    modalBody.appendChild(optionGrid(CFG.dispersion_models, 2, (val) => {
      wizard.disp = val;
      renderStep("ri");
    }, wizard.disp));
    modalBody.appendChild(backButton(goBack));
  }

  function stepRI() {
    setModalChrome(7, "Acceleration (RI)");
    modalBody.appendChild(optionGrid(CFG.ri_options, 1, (val) => {
      wizard.ri_type = val;
      renderStep("basisFamily");
    }, wizard.ri_type));
    modalBody.appendChild(backButton(goBack));
  }

  const BASIS_FAMILIES = {
    def2: "Ahlrichs (def2 - Standard)",
    dunning: "Dunning (cc-pV - Correlated)",
    pople: "Pople (6-31G / 6-311G)",
    pcseg: "Jensen (pcseg - Polar. Cons.)",
    x2c: "Relativistic (x2c / ZORA / SARC)",
    property: "Properties & NMR (IGLO / pcJ / EPR)",
  };

  function stepBasisFamily() {
    setModalChrome(8, "Basis set family");
    modalBody.appendChild(optionGrid(BASIS_FAMILIES, 1, (val) => {
      renderStep("basis", { family: val });
    }));
    modalBody.appendChild(backButton(goBack));
  }

  function stepBasis(opts) {
    setModalChrome(9, "Basis set");
    modalBody.appendChild(optionGrid(CFG.basis_map[opts.family], 2, (val) => {
      wizard.basis = val;
      renderStep("solvation");
    }, wizard.basis));
    modalBody.appendChild(backButton(goBack));
  }

  function stepSolvation() {
    setModalChrome(10, "Solvation model");
    modalBody.appendChild(optionGrid(CFG.solvation_models, 1, (val) => {
      wizard.solv_model = val;
      if (val === "none") renderStep("x2c");
      else renderStep("solvent");
    }, wizard.solv_model));
    modalBody.appendChild(backButton(goBack));
  }

  function stepSolvent() {
    setModalChrome(11, "Choose the solvent");
    modalBody.appendChild(optionGrid(CFG.solvents, 2, (val) => {
      wizard.solvent = val;
      renderStep("x2c");
    }, wizard.solvent));
    modalBody.appendChild(backButton(goBack));
  }

  function stepX2C() {
    setModalChrome(12, "Enable X2C (scalar relativistic)?");
    modalBody.appendChild(optionGrid({ yes: "Yes", no: "No" }, 2, (val) => {
      wizard.x2c = val === "yes";
      renderStep("charge");
    }));
    modalBody.appendChild(backButton(goBack));
  }

  function numberField(labelText, placeholder, value) {
    const field = document.createElement("div");
    field.className = "modal-field";
    const label = document.createElement("label");
    label.textContent = labelText;
    const input = document.createElement("input");
    input.type = "number"; input.placeholder = placeholder; if (value !== undefined) input.value = value;
    field.append(label, input);
    return { field, input };
  }

  function numericStep({ stepNumber, title, label, placeholder, min, max, value, key, errorText, nextStep }) {
    setModalChrome(stepNumber, title);
    const { field, input } = numberField(label, placeholder, wizard[key] ?? value);
    const err = document.createElement("p");
    err.className = "modal-hint";
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    actions.append(backButton(goBack));
    const nextBtn = document.createElement("button");
    nextBtn.type = "button"; nextBtn.className = "btn btn-primary"; nextBtn.textContent = "Next ►";
    nextBtn.addEventListener("click", () => {
      const n = Number(input.value);
      if (!Number.isFinite(n) || n < min || n > max) {
        err.textContent = errorText;
        return;
      }
      wizard[key] = n;
      renderStep(nextStep);
    });
    actions.append(nextBtn);
    modalBody.append(field, err, actions);
  }

  function stepCharge() {
    numericStep({
      stepNumber: 13, title: "Molecular charge", label: "Charge", placeholder: "0",
      min: -10, max: 10, value: 0, key: "charge",
      errorText: "Enter a whole number between -10 and 10.", nextStep: "multiplicity",
    });
  }

  function stepMultiplicity() {
    numericStep({
      stepNumber: 14, title: "Spin multiplicity", label: "Multiplicity", placeholder: "1",
      min: 1, max: 20, value: 1, key: "mult",
      errorText: "Enter a whole number between 1 and 20.",
      nextStep: wizard.calc_type === "tddft" ? "nroots" : "cores",
    });
  }

  function stepNroots() {
    numericStep({
      stepNumber: 15, title: "Number of excited states (nroots)", label: "nroots", placeholder: "10",
      min: 1, max: 200, value: 10, key: "nroots",
      errorText: "Enter a whole number between 1 and 200.", nextStep: "cores",
    });
  }

  function stepCores() {
    numericStep({
      stepNumber: 16, title: "Processor cores", label: "Cores", placeholder: "4",
      min: 1, max: 128, value: 4, key: "cores",
      errorText: "Enter a whole number between 1 and 128.", nextStep: "ram",
    });
  }

  // Kaggle CPU environments have ~31 GB of physical RAM and 20 GB of local scratch disk (/tmp).
  const KAGGLE_TOTAL_RAM_MB = 31744; // 31 GB total
  const RAM_WARNING_THRESHOLD_MB = Math.round(KAGGLE_TOTAL_RAM_MB * 0.70); // 70% = ~22,220 MB
  const HARD_LIMIT_RAM_MB = 30720; // 30 GB hard machine ceiling
  const KAGGLE_MAX_DISK_MB = 20480; // 20 GB scratch disk ceiling on Kaggle
  const DEFAULT_MAX_DISK_MB = 20000; // 20,000 MB safe default within Kaggle limit

  function renderSingleOutput(data) {
    const wrap = document.getElementById("orca-output-wrap");
    const titleEl = document.getElementById("orca-output-title");
    const tabsWrap = document.getElementById("orca-dual-tabs-wrap");
    const singleKaggleBtn = document.getElementById("orca-send-to-kaggle");
    const dualKaggleBtn = document.getElementById("orca-submit-dual-queue");

    if (titleEl) titleEl.textContent = "Generated input file";
    if (tabsWrap) tabsWrap.classList.add("hidden");
    if (dualKaggleBtn) dualKaggleBtn.classList.add("hidden");
    if (singleKaggleBtn) singleKaggleBtn.classList.remove("hidden");

    document.getElementById("orca-output").textContent = data.input_text;
    setDownload(document.getElementById("orca-download-inp"), data.file_base64, "text/plain", data.filename);
    lastOrcaFile = { filename: data.filename, content: data.input_text };
    show(wrap);
    wrap.scrollIntoView({ behavior: "smooth" });
  }

  let currentWorkflowData = null;
  let currentDualWorkflowData = null;

  function renderWorkflowOutputs(wfData) {
    currentWorkflowData = wfData;
    currentDualWorkflowData = wfData;
    const wrap = document.getElementById("orca-output-wrap");
    const titleEl = document.getElementById("orca-output-title");
    const tabsWrap = document.getElementById("orca-dual-tabs-wrap");
    const outputPre = document.getElementById("orca-output");
    const downloadA = document.getElementById("orca-download-inp");
    const singleKaggleBtn = document.getElementById("orca-send-to-kaggle");
    const dualKaggleBtn = document.getElementById("orca-submit-dual-queue");

    const stages = wfData.stages || [
      { num: 1, type: "opt", name: `${wfData.workflowName}_stage1_opt`, config: wfData.wizardSnapshot?.stage1 || {}, data: wfData.stage1 },
      { num: 2, type: wfData.wizardSnapshot?.stage2?.calc_type || "sp", name: `${wfData.workflowName}_stage2`, config: wfData.wizardSnapshot?.stage2 || {}, data: wfData.stage2 }
    ];

    if (titleEl) titleEl.textContent = `⚡ ${stages.length}-Stage Chained Workflow: ${wfData.workflowName}`;
    if (tabsWrap) {
      tabsWrap.classList.remove("hidden");
      tabsWrap.innerHTML = "";
      stages.forEach((st, idx) => {
        const tBtn = document.createElement("button");
        tBtn.type = "button";
        tBtn.className = `dual-output-tab ${idx === 0 ? "is-active" : ""}`;
        tBtn.id = `btn-tab-stage${st.num}`;
        tBtn.textContent = `Stage ${st.num}: ${(st.type || "calc").toUpperCase()}`;
        tBtn.addEventListener("click", () => {
          tabsWrap.querySelectorAll(".dual-output-tab").forEach(b => b.classList.remove("is-active"));
          tBtn.classList.add("is-active");
          if (st.data) {
            outputPre.textContent = st.data.input_text || "";
            setDownload(downloadA, st.data.file_base64, "text/plain", st.data.filename);
          }
        });
        tabsWrap.appendChild(tBtn);
      });
    }

    if (dualKaggleBtn) {
      dualKaggleBtn.classList.remove("hidden");
      dualKaggleBtn.textContent = `🚀 Submit ${stages.length}-Stage Workflow to Kaggle Queue (Auto-Chained)`;
      dualKaggleBtn.onclick = () => submitWorkflowToQueue(wfData);
    }
    if (singleKaggleBtn) singleKaggleBtn.classList.add("hidden");

    if (stages[0] && stages[0].data) {
      outputPre.textContent = stages[0].data.input_text || "";
      setDownload(downloadA, stages[0].data.file_base64, "text/plain", stages[0].data.filename);
    }

    show(wrap);
    wrap.scrollIntoView({ behavior: "smooth" });
  }

  function renderDualWorkflowOutputs(dw) {
    renderWorkflowOutputs(dw);
  }

  function submitWorkflowToQueue(wfData) {
    const jobs = loadJobs();
    const stages = wfData.stages || [
      { num: 1, type: "opt", name: `${wfData.workflowName}_Stage1_Opt`, config: wfData.wizardSnapshot?.stage1 || {}, data: wfData.stage1 },
      { num: 2, type: wfData.wizardSnapshot?.stage2?.calc_type || "sp", name: `${wfData.workflowName}_Stage2_${(wfData.wizardSnapshot?.stage2?.calc_type || 'SP').toUpperCase()}`, config: wfData.wizardSnapshot?.stage2 || {}, data: wfData.stage2 }
    ];

    if (jobs.length + stages.length > 20) {
      showToast(`Adding ${stages.length} workflow jobs would exceed the maximum limit of 20 jobs in your workspace. Please remove completed jobs first.`, "warning");
      return;
    }
    const workflowId = "chain_" + Date.now() + "_" + Math.random().toString(36).substr(2, 6);
    rememberOrcaSource();
    const orcaSourceKind = localStorage.getItem(LS_KEYS.orcaSourceKind) || (document.getElementById("orca-source-link")?.checked ? "link" : "dataset");
    const orcaDataset = localStorage.getItem(LS_KEYS.orcaDataset) || (document.getElementById("kaggle-dataset") ? document.getElementById("kaggle-dataset").value.trim() : "");
    const orcaLink = localStorage.getItem(LS_KEYS.orcaLink) || (document.getElementById("kaggle-orca-link") ? document.getElementById("kaggle-orca-link").value.trim() : "");

    stages.forEach((st, idx) => {
      const isFirst = idx === 0;
      const job = {
        name: st.name || `${wfData.workflowName}_Stage${st.num}_${(st.type || 'calc').toUpperCase()}`,
        jobId: `job_${Date.now() + idx}_${st.num}`,
        workflowId: workflowId,
        stage: st.num,
        stageType: st.type || "opt",
        stageConfig: st.config,
        stage2Config: st.config, // backwards compat
        dependsOnWorkflow: isFirst ? null : workflowId,
        input_filename: (st.data && st.data.filename) || `${wfData.workflowName}_stage${st.num}_${st.type}.inp`,
        input_content: (st.data && st.data.input_text) || "",
        kaggleUsername: (currentKaggle && currentKaggle.username) || localStorage.getItem(LS_KEYS.kaggleUsername) || "",
        orcaSourceKind: orcaSourceKind,
        orcaDataset: orcaDataset,
        orcaLink: orcaLink,
        status: isFirst ? "queued" : "waiting_dependency",
        submittedAt: isFirst ? (Date.now() + idx) : null,
        startedAt: null,
        finishedAt: null,
      };
      addJob(job);
    });

    showToast(`⚡ Added ${stages.length}-Stage Chained Workflow for <strong>${wfData.workflowName}</strong> to Kaggle Queue.`);
    switchToTab("jobs");
    processKaggleQueue();
  }

  function submitDualWorkflowToQueue(dw) {
    submitWorkflowToQueue(dw);
  }

  function stepRam() {
    setModalChrome(17, wizard.isDualWorkflow ? "Stage 1: Memory, Disk & Execution Options" : "Memory, Disk & Execution Options");
    const { field: ramField, input: ramInput } = numberField("RAM per core (%maxcore in MB)", "6000", wizard.ram ?? 6000);
    const { field: diskField, input: diskInput } = numberField("Max Scratch Disk (%maxdisk in MB, Kaggle max 20 GB)", "20000", wizard.maxdisk ?? DEFAULT_MAX_DISK_MB);

    const diskHint = document.createElement("p");
    diskHint.className = "modal-hint";
    const updateDiskHint = () => {
      const diskVal = Number(diskInput.value);
      if (!Number.isFinite(diskVal) || diskVal <= 0) {
        diskHint.textContent = "ℹ Kaggle provides ~20 GB scratch disk. Default is 20,000 MB.";
        diskHint.style.color = "var(--text-muted)";
        return;
      }
      const diskGb = (diskVal / 1024).toFixed(1);
      if (diskVal <= KAGGLE_MAX_DISK_MB) {
        diskHint.textContent = `✔ ${diskVal} MB (~${diskGb} GB): Safe scratch disk allocation within Kaggle's 20 GB limit.`;
        diskHint.style.color = "var(--teal)";
      } else {
        diskHint.textContent = `⚠ ${diskVal} MB (~${diskGb} GB) exceeds Kaggle's 20 GB scratch disk limit! ORCA may crash if temporary files exceed 20 GB.`;
        diskHint.style.color = "var(--amber)";
      }
    };
    diskInput.addEventListener("input", updateDiskHint);
    updateDiskHint();

    const extrasBox = document.createElement("div");
    extrasBox.className = "modal-field";
    extrasBox.style.marginTop = "0.75rem";
    extrasBox.style.padding = "0.75rem";
    extrasBox.style.background = "var(--surface-sunken, rgba(255,255,255,0.03))";
    extrasBox.style.borderRadius = "var(--radius-sm, 6px)";

    const isFreq = String(wizard.calc_type || "").toLowerCase().includes("freq");
    let html = `<label style="display:flex; align-items:center; gap:0.5rem; cursor:pointer; font-weight:600;">
      <input type="checkbox" id="wizard-largeprint" ${wizard.largeprint ? "checked" : ""}>
      Enable LargePrint (Detailed Orbitals & Population Output)
    </label>`;

    if (isFreq) {
      html += `<div style="display:grid; grid-template-columns:1fr 1fr; gap:0.5rem; margin-top:0.75rem;">
        <div>
          <label style="font-size:0.8rem; color:var(--text-muted);">IR/Thermochemistry Temp (K)</label>
          <input type="number" id="wizard-temp" value="${wizard.temp ?? 298.15}" step="0.1" style="width:100%;">
        </div>
        <div>
          <label style="font-size:0.8rem; color:var(--text-muted);">Pressure (atm)</label>
          <input type="number" id="wizard-pressure" value="${wizard.pressure ?? 1.0}" step="0.1" style="width:100%;">
        </div>
      </div>`;
    }
    extrasBox.innerHTML = html;

    const budget = document.createElement("p");
    budget.className = "modal-hint";
    const updateBudget = () => {
      const perCore = Number(ramInput.value);
      const cores = Number(wizard.cores) || 1;
      if (!Number.isFinite(perCore) || perCore <= 0) { budget.textContent = ""; return; }
      const total = perCore * cores;
      const totalGb = (total / 1024).toFixed(1);

      if (total <= RAM_WARNING_THRESHOLD_MB) {
        budget.textContent = `${perCore} MB × ${cores} core(s) = ${total} MB (~${totalGb} GB). Safe allocation within 70% of Kaggle's 31 GB RAM.`;
        budget.style.color = "var(--teal)";
      } else if (total <= HARD_LIMIT_RAM_MB) {
        budget.textContent = `⚠ High memory usage: ${perCore} MB × ${cores} core(s) = ${total} MB (~${totalGb} GB), exceeding 70% of Kaggle's 31 GB RAM. It will run as requested without reduction.`;
        budget.style.color = "var(--amber)";
      } else {
        const perCoreFits = Math.floor(HARD_LIMIT_RAM_MB / cores);
        budget.textContent = `⚠ ${perCore} MB × ${cores} core(s) = ${total} MB (~${totalGb} GB) exceeds the 30 GB maximum Kaggle limit. It will be capped at ${perCoreFits} MB per core at runtime.`;
        budget.style.color = "#FF8B8B";
      }
    };
    ramInput.addEventListener("input", updateBudget);
    updateBudget();
    const err = document.createElement("p");
    err.className = "modal-hint";
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    actions.append(backButton(goBack));
    const genBtn = document.createElement("button");
    genBtn.type = "button"; genBtn.className = "btn btn-primary";
    const totalStages = wizard.workflowStageCount || 2;
    genBtn.textContent = wizard.isDualWorkflow ? `➡️ Next: Configure Stage 2 (of ${totalStages}) ►` : "🧪 Generate file";

    genBtn.addEventListener("click", async () => {
      const n = Number(ramInput.value);
      if (!Number.isFinite(n) || n < 100 || n > 64000) { err.textContent = "Enter a RAM value between 100 and 64000."; return; }
      wizard.ram = n;

      const diskVal = Number(diskInput.value);
      wizard.maxdisk = (Number.isFinite(diskVal) && diskVal > 0) ? diskVal : DEFAULT_MAX_DISK_MB;

      const lpBox = extrasBox.querySelector("#wizard-largeprint");
      wizard.largeprint = lpBox ? lpBox.checked : false;

      if (isFreq) {
        const tBox = extrasBox.querySelector("#wizard-temp");
        const pBox = extrasBox.querySelector("#wizard-pressure");
        if (tBox && Number.isFinite(Number(tBox.value))) wizard.temp = Number(tBox.value);
        if (pBox && Number.isFinite(Number(pBox.value))) wizard.pressure = Number(pBox.value);
      }

      if (wizard.isDualWorkflow) {
        // Save Stage 1 configuration
        const s1Config = {
          name: `${wizard.name}_stage1_opt`,
          coords: wizard.coords,
          formula: wizard.formula,
          calc_type: wizard.calc_type || "opt",
          custom_line: wizard.custom_line,
          family: wizard.family,
          theory: wizard.theory,
          disp: wizard.disp || "none",
          ri_type: wizard.ri_type || "none",
          scf_conv: wizard.scf_conv || "none",
          basis_family: wizard.basis_family,
          basis: wizard.basis || "",
          aux_basis: wizard.aux_basis,
          solv_model: wizard.solv_model || "none",
          solvent: wizard.solvent || "",
          x2c: wizard.x2c || false,
          charge: wizard.charge || 0,
          mult: wizard.mult || 1,
          nroots: wizard.nroots || 10,
          cores: wizard.cores || 4,
          ram: wizard.ram,
          maxdisk: wizard.maxdisk,
          largeprint: wizard.largeprint || false,
          temp: wizard.temp,
          pressure: wizard.pressure,
          stage: 1
        };
        wizard.stages = wizard.stages || [];
        wizard.stages[0] = s1Config;
        wizard.stage1 = s1Config;
        renderStep("stage2Calc", { stageNum: 2 });
        return;
      }

      if (wizard && wizard.isReactionWorkflow) {
        genBtn.disabled = true;
        err.textContent = "Setting up unified 3D reaction inputs for all species...";
        try {
          const method = wizard.theory || wizard.method || "B3LYP";
          const basis = wizard.basis || "def2-SVP";
          const dispersion = wizard.disp || wizard.dispersion || "D3BJ";
          let solventModel = "none";
          let solvent = "Water";
          if (wizard.solv_model && wizard.solv_model !== "none") {
            solventModel = wizard.solv_model;
            solvent = wizard.solvent || "Water";
          }
          const calcType = wizard.calc_type || "opt";
          const stagesPreset = calcType === "opt_freq" ? "opt_freq" : (calcType === "freq" ? "freq" : (calcType === "sp" ? "sp" : "opt"));

          const res = await postJSON("/api/v1/reactions/unified-setup", {
            equation: wizard.reactionEquation,
            stages_preset: stagesPreset,
            method: method,
            basis_set: basis,
            dispersion: dispersion,
            solv_model: solventModel,
            solvent: solvent,
            target_host: "server_local",
            max_concurrency: 1
          });

          const rxn = res.reaction || {};
          const rxnId = rxn.reaction_id || res.reaction_id;
          const totalStagesCount = res.stages_count || (rxn.species || []).reduce((acc, s) => acc + (s.stages || []).length, 0) || 1;

          closeWizard();
          showToast(`⚡ Unified reaction workflow initialized with ${totalStagesCount} stages across all species!`);

          if (window.ReactionUnifiedClient && typeof window.ReactionUnifiedClient.startExecution === "function") {
            window.ReactionUnifiedClient.startExecution(rxnId);
          }
        } catch (e) {
          err.textContent = typeof e === "object" && e.message ? e.message : String(e);
        } finally {
          genBtn.disabled = false;
        }
        return;
      }

      genBtn.disabled = true;
      err.textContent = "Generating…";
      try {
        const data = await postJSON("/api/orca/generate", { ...wizard, name: wizard.name });
        renderSingleOutput(data);
        closeWizard();
      } catch (e) {
        err.textContent = e.message;
      } finally {
        genBtn.disabled = false;
      }
    });
    actions.append(genBtn);
    modalBody.append(ramField, budget, diskField, diskHint, extrasBox, err, actions);
  }

  // ─── Multi-Stage Workflow Steps (Stages 2 to 5) ───
  const STAGE2_CALC_TYPES = {
    sp: "Single Point Energy (High-Accuracy Evaluation)",
    opt: "Geometry Optimization (High-Level / Refinement)",
    opt_freq: "Optimization + Frequencies & Thermochemistry",
    numfreq: "Numerical Frequencies & Thermochemistry",
    tddft: "TD-DFT Excited States (UV-Vis Absorption Spectrum)",
    nmr: "NMR Chemical Shifts & Shieldings",
    custom: "Custom Keyword Line"
  };

  let currentConfiguringStageNum = 2;

  function stepStage2Calc(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    currentConfiguringStageNum = stageNum;
    const totalStages = wizard.workflowStageCount || 2;
    setModalChrome(18, `Stage ${stageNum}: Calculation Type`);

    const headerCard = document.createElement("div");
    headerCard.className = "stage-header-card";
    headerCard.innerHTML = `
      <div>
        <strong style="color:var(--text); font-size:0.88rem;">Stage ${stageNum} of ${totalStages}: Configuration</strong>
        <div style="font-size:0.75rem; color:var(--text-muted); margin-top:2px;">
          This calculation will automatically run on the converged equilibrium geometry (.xyz) from Stage ${stageNum - 1}.
        </div>
      </div>
      <span class="stage-pill stage-${stageNum}">Stage ${stageNum}</span>
    `;
    modalBody.appendChild(headerCard);

    modalBody.appendChild(optionGrid(STAGE2_CALC_TYPES, 1, (val) => {
      wizard.stage2_calc_type = val;
      if (val === "custom") renderStep("stage2CustomLine", { stageNum });
      else renderStep("stage2Family", { stageNum });
    }, wizard.stage2_calc_type || (stageNum === 2 ? "sp" : "tddft")));
    modalBody.appendChild(backButton(goBack));
  }

  function stepStage2CustomLine(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    setModalChrome(19, `Stage ${stageNum}: Custom input block`);
    const field = document.createElement("div");
    field.className = "modal-field";
    field.innerHTML = `<label>Enter Stage ${stageNum} keywords (e.g. ! DLPNO-CCSD(T) def2-TZVP def2-TZVP/C TightSCF):</label>`;
    const textarea = document.createElement("textarea");
    textarea.value = wizard.stage2_custom_line || "! DLPNO-CCSD(T) def2-TZVP TightSCF";
    textarea.rows = 8;
    field.appendChild(textarea);
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    actions.append(backButton(goBack));
    const nextBtn = document.createElement("button");
    nextBtn.type = "button"; nextBtn.className = "btn btn-primary"; nextBtn.textContent = "Next ►";
    nextBtn.addEventListener("click", () => {
      const text = textarea.value.trim();
      if (!text.startsWith("!")) { textarea.focus(); return; }
      wizard.stage2_custom_line = text;
      renderStep("stage2Cores", { stageNum });
    });
    actions.append(nextBtn);
    modalBody.append(field, actions);
  }

  function stepStage2Family(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    setModalChrome(19, `Stage ${stageNum}: Theory Family`);
    const headerCard = document.createElement("div");
    headerCard.className = "stage-header-card";
    headerCard.innerHTML = `
      <div><strong style="color:var(--text); font-size:0.84rem;">Stage ${stageNum} Theory Family (Method / Functional)</strong></div>
      <span class="stage-pill stage-${stageNum}">Stage ${stageNum}</span>
    `;
    modalBody.appendChild(headerCard);

    modalBody.appendChild(optionGrid(THEORY_FAMILIES, 1, (val) => {
      wizard.stage2_family = val;
      if (val === "f_comp") renderStep("stage2Method", { list: CFG.composite_methods, cols: 2, stageNum });
      else if (val === "f_dft") renderStep("stage2Method", { list: CFG.dft_functionals, cols: 2, stageNum });
      else if (val === "f_mp2") renderStep("stage2Method", { list: CFG.mp2_variants, cols: 1, stageNum });
      else if (val === "f_ccsd") renderStep("stage2Method", { list: CFG.ccsd_variants, cols: 1, stageNum });
      else if (val === "f_hf") { wizard.stage2_theory = "HF"; renderStep("stage2BasisFamily", { stageNum }); }
    }, wizard.stage2_family || "f_dft"));
    modalBody.appendChild(backButton(goBack));
  }

  function stepStage2Method(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    setModalChrome(20, `Stage ${stageNum}: Choose Method / Functional`);
    modalBody.appendChild(optionGrid(opts.list, opts.cols, (val) => {
      wizard.stage2_theory = val;
      renderStep("stage2SCF", { stageNum });
    }, wizard.stage2_theory));
    modalBody.appendChild(backButton(goBack));
  }

  function stepStage2SCF(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    setModalChrome(21, `Stage ${stageNum}: SCF Convergence`);
    const scfOpts = CFG.scf_options || {
      tightscf: "TightSCF (High Precision / Recommended)",
      verytightscf: "VeryTightSCF",
      normalscf: "NormalSCF",
    };
    modalBody.appendChild(optionGrid(scfOpts, 1, (val) => {
      wizard.stage2_scf_conv = val;
      const isDLPNO = (wizard.stage2_theory || "").includes("DLPNO");
      const methodLower = (wizard.stage2_theory || "").toLowerCase();
      const hasBuiltinDisp = methodLower.endsWith("-v") || methodLower.includes("-d4") || methodLower.includes("-d3") || methodLower.includes("-3c");
      if (wizard.stage2_family === "f_dft" && !isDLPNO && !hasBuiltinDisp) {
        renderStep("stage2Dispersion", { stageNum });
      } else if (isDLPNO || wizard.stage2_family === "f_comp" || methodLower.includes("-3c")) {
        wizard.stage2_ri_type = "none"; wizard.stage2_disp = "none"; wizard.stage2_basis = "";
        if (wizard.stage2_family === "f_comp" || methodLower.includes("-3c")) renderStep("stage2Solvation", { stageNum });
        else renderStep("stage2BasisFamily", { stageNum });
      } else {
        wizard.stage2_disp = "none";
        renderStep("stage2RI", { stageNum });
      }
    }, wizard.stage2_scf_conv || "tightscf"));
    modalBody.appendChild(backButton(goBack));
  }

  function stepStage2Dispersion(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    setModalChrome(22, `Stage ${stageNum}: Dispersion Model`);
    modalBody.appendChild(optionGrid(CFG.dispersion_models, 2, (val) => {
      wizard.stage2_disp = val;
      renderStep("stage2RI", { stageNum });
    }, wizard.stage2_disp));
    modalBody.appendChild(backButton(goBack));
  }

  function stepStage2RI(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    setModalChrome(23, `Stage ${stageNum}: Acceleration (RI)`);
    modalBody.appendChild(optionGrid(CFG.ri_options, 1, (val) => {
      wizard.stage2_ri_type = val;
      renderStep("stage2BasisFamily", { stageNum });
    }, wizard.stage2_ri_type));
    modalBody.appendChild(backButton(goBack));
  }

  function stepStage2BasisFamily(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    setModalChrome(24, `Stage ${stageNum}: Basis Set Family`);
    modalBody.appendChild(optionGrid(BASIS_FAMILIES, 1, (val) => {
      renderStep("stage2Basis", { family: val, stageNum });
    }));
    modalBody.appendChild(backButton(goBack));
  }

  function stepStage2Basis(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    setModalChrome(25, `Stage ${stageNum}: Basis Set`);
    modalBody.appendChild(optionGrid(CFG.basis_map[opts.family], 2, (val) => {
      wizard.stage2_basis = val;
      renderStep("stage2Solvation", { stageNum });
    }, wizard.stage2_basis));
    modalBody.appendChild(backButton(goBack));
  }

  function stepStage2Solvation(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    setModalChrome(26, `Stage ${stageNum}: Solvation Model`);
    modalBody.appendChild(optionGrid(CFG.solvation_models, 1, (val) => {
      wizard.stage2_solv_model = val;
      if (val === "none") renderStep("stage2Cores", { stageNum });
      else renderStep("stage2Solvent", { stageNum });
    }, wizard.stage2_solv_model || "none"));
    modalBody.appendChild(backButton(goBack));
  }

  function stepStage2Solvent(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    setModalChrome(27, `Stage ${stageNum}: Solvent`);
    modalBody.appendChild(optionGrid(CFG.solvents, 2, (val) => {
      wizard.stage2_solvent = val;
      renderStep("stage2Cores", { stageNum });
    }, wizard.stage2_solvent));
    modalBody.appendChild(backButton(goBack));
  }

  function stepStage2Cores(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    numericStep({
      stepNumber: 28, title: `Stage ${stageNum}: Processor Cores`, label: `Cores for Stage ${stageNum}`, placeholder: "4",
      min: 1, max: 128, value: wizard.cores || 4, key: "stage2_cores",
      errorText: "Enter a whole number between 1 and 128.", nextStep: "stage2Ram",
      stepOpts: { stageNum }
    });
  }

  function stepStage2Ram(opts) {
    const stageNum = (opts && opts.stageNum) || currentConfiguringStageNum || 2;
    const totalStages = wizard.workflowStageCount || 2;
    const isLastStage = stageNum >= totalStages;

    setModalChrome(29, isLastStage ? `Stage ${stageNum}: Memory & Generate Workflow` : `Stage ${stageNum}: Memory & Next Stage`);
    const { field: ramField, input: ramInput } = numberField(`RAM per core for Stage ${stageNum} (MB)`, "6000", wizard.stage2_ram ?? 6000);
    const { field: diskField, input: diskInput } = numberField("Max Scratch Disk (%maxdisk in MB)", "20000", wizard.stage2_maxdisk ?? DEFAULT_MAX_DISK_MB);

    const err = document.createElement("p");
    err.className = "modal-hint";
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    actions.append(backButton(goBack));
    const genBtn = document.createElement("button");
    genBtn.type = "button"; genBtn.className = "btn btn-primary";
    genBtn.textContent = isLastStage ? `⚡ Generate All ${totalStages} Workflow Input Files` : `➡️ Next: Configure Stage ${stageNum + 1} (of ${totalStages}) ►`;

    genBtn.addEventListener("click", async () => {
      const n = Number(ramInput.value);
      if (!Number.isFinite(n) || n < 100 || n > 64000) { err.textContent = "Enter a RAM value between 100 and 64000."; return; }
      wizard.stage2_ram = n;
      wizard.stage2_maxdisk = Number(diskInput.value) || DEFAULT_MAX_DISK_MB;

      const stageCfg = {
        name: `${wizard.name}_stage${stageNum}_${wizard.stage2_calc_type || 'calc'}`,
        coords: wizard.coords,
        formula: wizard.formula,
        calc_type: wizard.stage2_calc_type || (stageNum === 2 ? "sp" : "tddft"),
        custom_line: wizard.stage2_custom_line,
        family: wizard.stage2_family,
        theory: wizard.stage2_theory,
        disp: wizard.stage2_disp || "none",
        ri_type: wizard.stage2_ri_type || "none",
        scf_conv: wizard.stage2_scf_conv || "tightscf",
        basis_family: wizard.stage2_basis_family,
        basis: wizard.stage2_basis || "",
        aux_basis: wizard.stage2_aux_basis,
        solv_model: wizard.stage2_solv_model || "none",
        solvent: wizard.stage2_solvent || "",
        x2c: wizard.stage2_x2c || false,
        charge: wizard.charge || 0,
        mult: wizard.mult || 1,
        nroots: wizard.nroots || 10,
        cores: wizard.stage2_cores || wizard.cores || 4,
        ram: wizard.stage2_ram,
        maxdisk: wizard.stage2_maxdisk,
        largeprint: wizard.largeprint || false,
        stage: stageNum
      };

      wizard.stages = wizard.stages || [];
      wizard.stages[stageNum - 1] = stageCfg;
      if (stageNum === 2) wizard.stage2 = stageCfg;

      if (!isLastStage) {
        // Reset intermediate choices for next stage
        wizard.stage2_calc_type = null;
        wizard.stage2_theory = null;
        wizard.stage2_basis = "";
        wizard.stage2_custom_line = "";
        renderStep("stage2Calc", { stageNum: stageNum + 1 });
        return;
      }

      if (wizard && wizard.isReactionWorkflow) {
        genBtn.disabled = true;
        err.textContent = "Setting up unified 3D reaction inputs for all species...";
        try {
          const stageCfg = wizard.stages[0] || {};
          const method = stageCfg.theory || stageCfg.method || wizard.theory || wizard.method || "B3LYP";
          const basis = stageCfg.basis || wizard.basis || "def2-SVP";
          const dispersion = stageCfg.disp || stageCfg.dispersion || wizard.disp || wizard.dispersion || "D3BJ";
          let solventModel = "none";
          let solvent = "Water";
          if (stageCfg.solv_model && stageCfg.solv_model !== "none") {
            solventModel = stageCfg.solv_model;
            solvent = stageCfg.solvent || "Water";
          } else if (wizard.solv_model && wizard.solv_model !== "none") {
            solventModel = wizard.solv_model;
            solvent = wizard.solvent || "Water";
          }
          const stagesPreset = wizard.stages.length > 1 ? "opt_freq" : (stageCfg.calc_type || "opt_freq");

          const res = await postJSON("/api/v1/reactions/unified-setup", {
            equation: wizard.reactionEquation,
            stages_preset: stagesPreset,
            method: method,
            basis_set: basis,
            dispersion: dispersion,
            solv_model: solventModel,
            solvent: solvent,
            target_host: "server_local",
            max_concurrency: 1
          });

          const rxn = res.reaction || {};
          const rxnId = rxn.reaction_id || res.reaction_id;
          const totalStagesCount = res.stages_count || (rxn.species || []).reduce((acc, s) => acc + (s.stages || []).length, 0) || totalStages;

          closeWizard();
          showToast(`⚡ Unified reaction workflow initialized with ${totalStagesCount} stages across all species!`);

          if (window.ReactionUnifiedClient && typeof window.ReactionUnifiedClient.startExecution === "function") {
            window.ReactionUnifiedClient.startExecution(rxnId);
          }
        } catch (e) {
          err.textContent = typeof e === "object" && e.message ? e.message : String(e);
        } finally {
          genBtn.disabled = false;
        }
        return;
      }

      genBtn.disabled = true;
      err.textContent = `Generating all ${totalStages} workflow files…`;
      try {
        const generatedStages = [];
        for (let i = 0; i < totalStages; i++) {
          const cfg = wizard.stages[i] || wizard.stages[0];
          const stType = cfg.calc_type || "calc";
          const stName = `${wizard.name}_stage${i + 1}_${stType}`;
          const resp = await postJSON("/api/orca/generate", { ...cfg, name: stName });
          generatedStages.push({
            num: i + 1,
            type: stType,
            name: stName,
            config: cfg,
            data: resp
          });
        }

        const workflowOutputs = {
          workflowName: wizard.name,
          stages: generatedStages,
          stage1: generatedStages[0]?.data,
          stage2: generatedStages[1]?.data,
          wizardSnapshot: JSON.parse(JSON.stringify(wizard))
        };

        renderWorkflowOutputs(workflowOutputs);
        closeWizard();
      } catch (e) {
        err.textContent = e.message;
      } finally {
        genBtn.disabled = false;
      }
    });

    actions.append(genBtn);
    modalBody.append(ramField, diskField, err, actions);
  }

  const STEP_RENDERERS = {
    coords: stepCoords, builder3d: stepBuilder3D, calc: stepCalc, customLine: stepCustomLine, family: stepFamily,
    method: stepMethod, scf: stepSCF, dispersion: stepDispersion, ri: stepRI, basisFamily: stepBasisFamily,
    basis: stepBasis, solvation: stepSolvation, solvent: stepSolvent, x2c: stepX2C,
    charge: stepCharge, multiplicity: stepMultiplicity, nroots: stepNroots, cores: stepCores, ram: stepRam,
    stage2Calc: stepStage2Calc, stage2CustomLine: stepStage2CustomLine, stage2Family: stepStage2Family,
    stage2Method: stepStage2Method, stage2SCF: stepStage2SCF, stage2Dispersion: stepStage2Dispersion,
    stage2RI: stepStage2RI, stage2BasisFamily: stepStage2BasisFamily, stage2Basis: stepStage2Basis,
    stage2Solvation: stepStage2Solvation, stage2Solvent: stepStage2Solvent, stage2Cores: stepStage2Cores,
    stage2Ram: stepStage2Ram,
  };


  // ───────────────────────────────────────────────
  // Kaggle Launcher
  // ───────────────────────────────────────────────
  let lastOrcaFile = null;

  document.getElementById("orca-send-to-kaggle").addEventListener("click", () => {
    if (!lastOrcaFile) return;
    switchToTab("kaggle");
    document.getElementById("inp-source-text").checked = true;
    document.getElementById("kaggle-inp-text-row").classList.remove("hidden");
    document.getElementById("kaggle-inp-upload-row").classList.add("hidden");
    document.getElementById("kaggle-inp-name").value = lastOrcaFile.filename;
    document.getElementById("kaggle-inp-content").value = lastOrcaFile.content;
  });

  // Signing in with a Kaggle username + key/token is required before the
  // launcher or the Jobs tab can be used. Besides authenticating, sign-in
  // asks Kaggle for every job this site has ever submitted under that
  // account, so a person who cleared their browser data (or is on a new
  // device) can sign back in and pick up exactly where they left off,
  // instead of losing the list.
  const kaggleLoginForm = document.getElementById("kaggle-login-form");
  const kaggleLoginError = document.getElementById("kaggle-login-error");
  const kaggleLoginLoading = document.getElementById("kaggle-login-loading");
  const kaggleSignedInAs = document.getElementById("kaggle-signed-in-as");
  const kaggleUsernameInput = document.getElementById("kaggle-username");
  const kaggleKeyInput = document.getElementById("kaggle-key");
  const kaggleRememberBox = document.getElementById("kaggle-remember");
  const kaggleForm = document.getElementById("kaggle-form");
  const kaggleError = document.getElementById("kaggle-error");
  const kaggleLoading = document.getElementById("kaggle-loading");
  const kaggleResult = document.getElementById("kaggle-result");
  const jobsSigninRequired = document.getElementById("jobs-signin-required");
  const jobsSignedInArea = document.getElementById("jobs-signed-in-area");

  let currentKaggle = null; // { username, key } once signed in, else null

  function setSignedIn(username, key) {
    currentKaggle = { username, key };
    hide(kaggleLoginForm);
    kaggleSignedInAs.textContent = `Signed in as ${username}`;
    show(kaggleSignedInAs);
    show(kaggleForm);
    jobsSigninRequired.classList.add("hidden");
    jobsSignedInArea.classList.remove("hidden");
  }
  window.setKaggleSignedIn = setSignedIn;

  function setSignedOut() {
    currentKaggle = null;
    show(kaggleLoginForm);
    hide(kaggleSignedInAs);
    hide(kaggleForm);
    hide(kaggleResult);
    jobsSigninRequired.classList.remove("hidden");
    jobsSignedInArea.classList.add("hidden");
  }

  // Jobs the person has explicitly deleted stay hidden even after a fresh
  // sign-in re-fetches this account's full kernel list from Kaggle (see
  // removeJob below for why this is needed in addition to the real
  // Kaggle-side delete).
  function loadRemovedIds() {
    try { return new Set(JSON.parse(localStorage.getItem(LS_KEYS.removedJobIds) || "[]")); }
    catch { return new Set(); }
  }
  function saveRemovedIds(idSet) {
    localStorage.setItem(LS_KEYS.removedJobIds, JSON.stringify([...idSet]));
  }
  // A defensive fallback for jobs saved before per-job chainIds tracking
  // existed: also treat a kernel as removed if it's a restart continuation
  // (the "<base>-r<N>" suffix build_job_dir/KAGGLE_RUNNER_BODY use) of a
  // job id that was explicitly removed.
  function isRemovedId(jobId, removedIds) {
    if (removedIds.has(jobId)) return true;
    for (const rid of removedIds) {
      if (jobId.startsWith(`${rid}-r`)) return true;
    }
    return false;
  }

  // Merges jobs Kaggle reports for this account into the locally cached
  // list, without clobbering jobs already tracked locally (which carry
  // live status/timing info this merge can't reconstruct).
  function mergeRemoteJobs(username, key, remoteJobs) {
    const jobs = loadJobs();
    const removedIds = loadRemovedIds();
    let added = 0;
    remoteJobs.forEach(rj => {
      if (isRemovedId(rj.job_id, removedIds)) return;
      const remoteIds = new Set((rj.chain_ids && rj.chain_ids.length) ? rj.chain_ids : [rj.job_id]);
      // Same job, already tracked here? Then only catch it up: the chain may
      // have auto-restarted into a newer kernel while this browser was closed,
      // and the local entry would otherwise keep polling a finished window.
      const local = jobs.find(j => remoteIds.has(j.jobId) ||
        (j.chainIds || []).some(id => remoteIds.has(id)));
      if (local) {
        if (local.jobId !== rj.job_id || local.kaggleUsername !== rj.owner) {
          const known = new Set((local.chain || []).map(w => w.id));
          local.jobId = rj.job_id;
          local.kaggleUrl = rj.kaggle_url || local.kaggleUrl;
          local.kaggleUsername = rj.owner || local.kaggleUsername || username;
          local.chainIds = [...remoteIds];
          local.rootId = local.rootId || [...remoteIds][0];
          local.chain = [...(local.chain || []),
                         ...(known.has(rj.job_id)
                             ? [] : [{ id: rj.job_id, url: rj.kaggle_url }])];
          local.restarts = rj.restarts || local.restarts || 0;
          local.status = "unknown";     // the new window's state, until the next poll
          local.finishedAt = null;
          added++;
        }
        return;
      }
      const submittedAt = rj.last_run ? Date.parse(rj.last_run) || Date.now() : Date.now();
      jobs.push({
        name: rj.title || rj.job_id,
        jobId: rj.job_id,
        // The server groups an auto-restarted job's kernels back into one
        // entry, so deleting it here removes every continuation too.
        chainIds: (rj.chain_ids && rj.chain_ids.length) ? rj.chain_ids : [rj.job_id],
        rootId: (rj.chain_ids && rj.chain_ids.length) ? rj.chain_ids[0] : rj.job_id,
        chain: [{ id: rj.job_id, url: rj.kaggle_url }],
        restarts: rj.restarts || 0,
        kaggleUrl: rj.kaggle_url,
        kaggleUsername: rj.owner || username,
        status: "unknown",
        submittedAt,
      });
      added++;
    });
    if (added > 0) saveJobs(jobs);
    renderJobs();
  }

  async function syncKaggleJobs(username, key, opts = {}) {
    try { const data = await postJSON("/api/kaggle/sync", {kaggle_username: username, kaggle_key: key}); mergeRemoteJobs(username,key,data.jobs||[]); pollAllActiveJobs(); return true; }
    catch (err) { if (!(opts && opts.silent)) showToast(`<strong>Kaggle job sync failed</strong><br>${err.message}`); else console.warn("Kaggle job sync failed:",err.message); return false; }
  }

  async function attemptLogin(username, key, opts) {
    const silent=opts&&opts.silent; if(!silent){hide(kaggleLoginError);show(kaggleLoginLoading);}
    try { const data=await postJSON("/api/kaggle/login",{kaggle_username:username,kaggle_key:key}); const u=data.username||username; setSignedIn(u,key); sessionKaggleKey=key; syncKaggleJobs(u,key,{silent:true}); return true; }
    catch(err){ if(!silent) showError(kaggleLoginError,err.message); return false; }
    finally{ if(!silent) hide(kaggleLoginLoading); }
  }

  // If credentials were remembered from a previous visit, check server vault; migrate & purge legacy keys.
  (async function autoSignIn() {
    const legacyKey = localStorage.getItem("chemlab_kaggle_key");
    const savedUser = localStorage.getItem(LS_KEYS.kaggleUsername);
    if (legacyKey) {
      if (savedUser) {
        let attempts = parseInt(sessionStorage.getItem("chemlab_migration_attempts") || "0", 10);
        if (attempts < 3) {
          try {
            sessionStorage.setItem("chemlab_migration_attempts", String(attempts + 1));
            const migResp = await postJSON("/api/kaggle/credentials", { kaggle_username: savedUser, kaggle_key: legacyKey });
            if (migResp && migResp.ok && migResp.saved_to_vault) {
              // Delete ONLY after confirmed successful vault persistence
              localStorage.removeItem("chemlab_kaggle_key");
              sessionStorage.removeItem("chemlab_migration_attempts");
            }
          } catch (migErr) {
            console.warn("Kaggle legacy credential migration to vault failed; retaining temporarily for retry.");
          }
        }
      } else {
        localStorage.removeItem("chemlab_kaggle_key");
      }
    }

    try {
      const resp = await fetch("/api/kaggle/credentials", { method: "GET" });
      if (resp.ok) {
        const data = await resp.json();
        if (data && data.ok && data.configured) {
          const u = data.username || savedUser || "";
          if (kaggleUsernameInput) kaggleUsernameInput.value = u;
          if (kaggleKeyInput) kaggleKeyInput.value = "";
          setSignedIn(u, "");
          sessionKaggleKey = "";
          syncKaggleJobs(u, "", { silent: true });
          return;
        }
      }
    } catch (_) {}

    if (savedUser && kaggleUsernameInput) {
      kaggleUsernameInput.value = savedUser;
    }
  })();

  kaggleLoginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = kaggleUsernameInput.value.trim();
    const key = kaggleKeyInput.value.trim();
    if (!username || !key) {
      showError(kaggleLoginError, "Please enter your Kaggle username and API key/token.");
      return;
    }
    const ok = await attemptLogin(username, key);
    if (ok) {
      sessionKaggleKey = key;
      if (kaggleRememberBox.checked) {
        localStorage.setItem(LS_KEYS.kaggleUsername, username);
        try {
          await postJSON("/api/kaggle/credentials", { kaggle_username: username, kaggle_key: key });
        } catch (err) {
          console.warn("Could not save to encrypted vault:", err.message);
        }
      } else {
        localStorage.removeItem(LS_KEYS.kaggleUsername);
        try {
          await fetch("/api/kaggle/credentials", { method: "DELETE" });
        } catch (_) {}
      }
      kaggleKeyInput.value = "";
    }
    localStorage.removeItem("chemlab_kaggle_key");
  });

  document.getElementById("kaggle-signout-btn").addEventListener("click", async () => {
    localStorage.removeItem(LS_KEYS.kaggleUsername);
    localStorage.removeItem("chemlab_kaggle_key");
    sessionKaggleKey = "";
    if (kaggleKeyInput) kaggleKeyInput.value = "";
    try {
      await fetch("/api/kaggle/credentials", { method: "DELETE" });
    } catch (_) {}
    // Any credential a previous version left inside the job list goes too, so
    // "Sign out removes it" is true of every copy.
    const jobs = loadJobs();
    jobs.forEach(j => delete j.kaggleKey);
    saveJobs(jobs);
    setSignedOut();
  });

  // ORCA source toggle: Kaggle Dataset <-> Google Drive / direct link
  const datasetRow = document.getElementById("kaggle-dataset-row");
  const orcaLinkRow = document.getElementById("kaggle-orca-link-row");
  const datasetInput = document.getElementById("kaggle-dataset");
  const orcaLinkInput = document.getElementById("kaggle-orca-link");
  const orcaSourceDatasetRadio = document.getElementById("orca-source-dataset");
  const orcaSourceLinkRadio = document.getElementById("orca-source-link");

  function normalizeKaggleDatasetInput(val) {
    if (!val) return "";
    return val.split(",").map(s => {
      let raw = s.trim().replace(/^["']|["']$/g, '');
      if (!raw) return "";
      const m = raw.match(/^(?:https?:\/\/)?(?:[a-zA-Z0-9_-]+\.)?kaggle\.com\/(?:datasets\/)?([A-Za-z0-9._-]+)\/([A-Za-z0-9._-]+)(?:[/?#].*)?$/i);
      if (m) {
        return `${m[1]}/${m[2]}`;
      }
      return raw;
    }).filter(Boolean).join(", ");
  }

  function getSelectedOrcaSourceKind() {
    if (orcaSourceLinkRadio?.checked) return "google_drive";
    return "kaggle_dataset";
  }

  function applyOrcaSourceToggle() {
    const kind = getSelectedOrcaSourceKind();
    if (datasetRow) datasetRow.classList.toggle("hidden", kind !== "kaggle_dataset");
    if (orcaLinkRow) orcaLinkRow.classList.toggle("hidden", kind !== "google_drive");
  }

  function rememberOrcaSource() {
    const kind = getSelectedOrcaSourceKind();
    localStorage.setItem(LS_KEYS.orcaSourceKind, kind);
    if (datasetInput) {
      const normalized = normalizeKaggleDatasetInput(datasetInput.value);
      if (datasetInput.value.trim() && normalized) {
        datasetInput.value = normalized;
      }
      localStorage.setItem(LS_KEYS.orcaDataset, normalized || datasetInput.value.trim());
    }
    if (orcaLinkInput) localStorage.setItem(LS_KEYS.orcaLink, orcaLinkInput.value.trim());
  }

  (function restoreOrcaSource() {
    const savedKind = localStorage.getItem(LS_KEYS.orcaSourceKind);
    const savedDataset = localStorage.getItem(LS_KEYS.orcaDataset);
    const savedLink = localStorage.getItem(LS_KEYS.orcaLink);

    if (savedDataset && datasetInput) datasetInput.value = normalizeKaggleDatasetInput(savedDataset) || savedDataset;
    if (savedLink && orcaLinkInput) orcaLinkInput.value = savedLink;

    if (savedKind === "google_drive" || savedKind === "link") {
      if (orcaSourceLinkRadio) orcaSourceLinkRadio.checked = true;
    } else {
      if (orcaSourceDatasetRadio) orcaSourceDatasetRadio.checked = true;
    }

    applyOrcaSourceToggle();
  })();

  if (datasetInput) {
    datasetInput.addEventListener("blur", () => {
      const normalized = normalizeKaggleDatasetInput(datasetInput.value);
      if (normalized) datasetInput.value = normalized;
      rememberOrcaSource();
    });
    datasetInput.addEventListener("change", rememberOrcaSource);
  }
  if (orcaLinkInput) {
    orcaLinkInput.addEventListener("change", rememberOrcaSource);
  }

  document.querySelectorAll('input[name="orca-source"]').forEach(radio => {
    radio.addEventListener("change", () => {
      applyOrcaSourceToggle();
      rememberOrcaSource();
    });
  });

  const forgetSourceBtn = document.getElementById("kaggle-forget-source");
  if (forgetSourceBtn) {
    forgetSourceBtn.addEventListener("click", () => {
      [LS_KEYS.orcaSourceKind, LS_KEYS.orcaDataset, LS_KEYS.orcaLink]
        .forEach(k => localStorage.removeItem(k));
      if (datasetInput) datasetInput.value = "";
      if (orcaLinkInput) orcaLinkInput.value = "";
      if (orcaSourceDatasetRadio) orcaSourceDatasetRadio.checked = true;
      applyOrcaSourceToggle();
      showToast("The saved ORCA source was removed from this browser.");
    });
  }

  // Input-file source toggle: pasted/generated text <-> uploaded .inp file.
  const inpTextRow = document.getElementById("kaggle-inp-text-row");
  const inpUploadRow = document.getElementById("kaggle-inp-upload-row");
  document.querySelectorAll('input[name="inp-source"]').forEach(radio => {
    radio.addEventListener("change", () => {
      const useUpload = document.getElementById("inp-source-upload").checked;
      inpTextRow.classList.toggle("hidden", useUpload);
      inpUploadRow.classList.toggle("hidden", !useUpload);
    });
  });

  function readFileAsText(file) {
    return new Promise((resolve, reject) => {
      if (!file) return resolve("");
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result || "");
      reader.onerror = () => reject(reader.error);
      reader.readAsText(file);
    });
  }

  // Double-submission guard (Phase 13): a second submit while one is already
  // in flight is ignored, and the submit button is disabled for the duration.
  // The backend Idempotency-Key + orchestrator idempotency store remain the
  // final authority; this only prevents the obvious double click.
  let kaggleSubmitInFlight = false;
  const kaggleSubmitBtn = kaggleForm.querySelector('button[type="submit"]');
  kaggleForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (kaggleSubmitInFlight) return;
    kaggleSubmitInFlight = true;
    if (kaggleSubmitBtn) kaggleSubmitBtn.disabled = true;
    hide(kaggleError); hide(kaggleResult); show(kaggleLoading);

    if (!currentKaggle) {
      showError(kaggleError, "Please sign in with your Kaggle account first.");
      hide(kaggleLoading);
      return;
    }
    const username = currentKaggle.username;
    const key = currentKaggle.key;
    // The "Job name" field wins when filled in; otherwise the input file's
    // own name (without extension) is used as the job's label, both here
    // and as the title on Kaggle. addJob() below reconciles this with the
    // server's resolved data.job_title once submission succeeds.
    const inpFileForLabel = document.getElementById("kaggle-inp-file").files[0];
    const customJobName = document.getElementById("kaggle-job-name").value.trim();
    const rawInputName = document.getElementById("kaggle-inp-name").value.trim() ||
      (inpFileForLabel && inpFileForLabel.name) || "molecule.inp";
    const jobLabel = customJobName || rawInputName.replace(/\.[^./]+$/, "");

    const useOrcaLink = document.getElementById("orca-source-link").checked;
    const useInpUpload = document.getElementById("inp-source-upload").checked;

    if (useOrcaLink) {
      const link = document.getElementById("kaggle-orca-link").value.trim();
      if (!link) { showError(kaggleError, "Please provide the ORCA archive link, or switch back to Kaggle Dataset."); hide(kaggleLoading); return; }
    } else {
      const ds = document.getElementById("kaggle-dataset").value.trim();
      if (!ds) { showError(kaggleError, "Please provide your ORCA Dataset id, or switch to a Drive/direct link."); hide(kaggleLoading); return; }
    }

    const uploadedInpFiles = document.getElementById("kaggle-inp-file").files;
    if (useInpUpload && uploadedInpFiles.length === 0) {
      showError(kaggleError, "Please choose a .inp file to upload, or switch back to pasted content.");
      hide(kaggleLoading);
      return;
    }
    if (!useInpUpload && !document.getElementById("kaggle-inp-content").value.trim()) {
      showError(kaggleError, "Please paste or generate .inp content, or switch to uploading a ready file.");
      hide(kaggleLoading);
      return;
    }

    // Persist chosen ORCA source options
    rememberOrcaSource();

    // Check active jobs capacity before dispatching
    const currentJobs = loadJobs();
    const activeJobs = currentJobs.filter(j => ["running", "restarting", "submitting"].includes(j.status));
    
    // If active jobs already at or above max (5), queue the job safely
    if (activeJobs.length >= MAX_ACTIVE_KAGGLE_JOBS) {
      try {
        const inpText = useInpUpload
          ? (await readFileAsText(uploadedInpFiles[0]))
          : document.getElementById("kaggle-inp-content").value;
        const inpName = useInpUpload
          ? uploadedInpFiles[0].name
          : (document.getElementById("kaggle-inp-name").value.trim() || "molecule.inp");
        const orcaKind = getSelectedOrcaSourceKind();
        let queuedDataset = "";
        let queuedLink = "";
        if (orcaKind === "google_drive") {
          queuedLink = document.getElementById("kaggle-orca-link") ? document.getElementById("kaggle-orca-link").value.trim() : "";
        } else {
          let rawDs = document.getElementById("kaggle-dataset") ? document.getElementById("kaggle-dataset").value.trim() : "";
          queuedDataset = normalizeKaggleDatasetInput(rawDs) || rawDs;
        }

        const queuedJob = {
          name: jobLabel,
          jobId: `job_${Date.now()}_${Math.random().toString(36).substr(2, 6)}`,
          input_filename: inpName,
          input_content: inpText,
          kaggleUsername: username,
          orcaSourceKind: orcaKind,
          orcaDataset: queuedDataset,
          orcaLink: queuedLink,
          status: "queued",
          submittedAt: Date.now(),
        };
        addJob(queuedJob);

        // Clear the submitted form fields
        const inpContent = document.getElementById("kaggle-inp-content");
        const inpNameEl = document.getElementById("kaggle-inp-name");
        const jobNameInput = document.getElementById("kaggle-job-name");
        const auxFiles = document.getElementById("kaggle-aux-files");
        const fileInput = document.getElementById("kaggle-inp-file");
        if (inpContent) inpContent.value = "";
        if (inpNameEl) inpNameEl.value = "";
        if (jobNameInput) jobNameInput.value = "";
        if (auxFiles) auxFiles.value = "";
        if (fileInput) fileInput.value = "";

        showToast(`⏳ 5 active jobs limit reached on Kaggle. Job <strong>${queuedJob.name}</strong> added to the queue (#${loadJobs().filter(j => j.status === "queued").length}) and will launch automatically when a slot frees up.`);
        switchToTab("jobs");
      } catch (readErr) {
        showError(kaggleError, `Could not read input file: ${readErr.message}`);
      } finally {
        hide(kaggleLoading);
      }
      return;
    }

    const orcaKind = getSelectedOrcaSourceKind();
    let datasetSourcesVal = "";
    let orcaLinkVal = "";

    if (orcaKind === "google_drive") {
      orcaLinkVal = document.getElementById("kaggle-orca-link") ? document.getElementById("kaggle-orca-link").value.trim() : "";
      if (!orcaLinkVal) {
        showError(kaggleError, "Please provide a valid Google Drive or direct download URL for the ORCA package.");
        hide(kaggleLoading);
        return;
      }
    } else {
      let rawDs = document.getElementById("kaggle-dataset") ? document.getElementById("kaggle-dataset").value.trim() : "";
      datasetSourcesVal = normalizeKaggleDatasetInput(rawDs) || rawDs;
      if (!datasetSourcesVal) {
        showError(kaggleError, "Please enter your Kaggle ORCA Dataset identifier (e.g. username/dataset or jon534/orca6).");
        hide(kaggleLoading);
        return;
      }
    }

    const form = new FormData();
    form.append("kaggle_username", username);
    form.append("kaggle_key", key);
    form.append("dataset_sources", datasetSourcesVal);
    form.append("orca_link", orcaLinkVal);
    form.append("job_name", customJobName);

    if (useInpUpload) {
      form.append("input_file", uploadedInpFiles[0]);
      form.append("input_filename", uploadedInpFiles[0].name);
    } else {
      form.append("input_filename", document.getElementById("kaggle-inp-name").value.trim());
      form.append("input_content", document.getElementById("kaggle-inp-content").value);
    }
    for (const f of document.getElementById("kaggle-aux-files").files) {
      form.append("aux_files", f);
    }

    try {
      const resp = await fetch("/api/kaggle/submit", {
        method: "POST",
        headers: {
          "Idempotency-Key": "sub_" + Date.now() + "_" + Math.random().toString(36).substr(2, 8)
        },
        body: form
      });
      let data = null;
      const contentType = resp.headers.get("content-type") || "";
      if (contentType.includes("application/json")) {
        data = await resp.json().catch(() => null);
      } else {
        const text = await resp.text().catch(() => "");
        const detail = text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 240);
        throw new Error(`Server returned HTTP ${resp.status}${detail ? `: ${detail}` : "."}`);
      }
      if (!data) throw new Error(`Server returned HTTP ${resp.status} with an invalid JSON response.`);
      if (!resp.ok || !data.ok) throw new Error(data.error || `Submission failed (HTTP ${resp.status}).`);
      rememberOrcaSource();
      const safeMsg = escapeHtml(data.message || "");
      const safeUrl = escapeHtml(data.kaggle_url || "");
      const encodedUrl = encodeURI(data.kaggle_url || "");
      kaggleResult.innerHTML = `${safeMsg}<br><a href="${encodedUrl}" target="_blank" rel="noopener">${safeUrl}</a>`;
      show(kaggleResult);
      const safeTitle = escapeHtml(data.job_title || jobLabel || "Job");
      showToast(`✅ Job <strong>${safeTitle}</strong> submitted to Kaggle successfully.`);

      // Clear the submitted form fields so stale input does not linger
      const inpContent = document.getElementById("kaggle-inp-content");
      const inpName = document.getElementById("kaggle-inp-name");
      const jobNameInput = document.getElementById("kaggle-job-name");
      const auxFiles = document.getElementById("kaggle-aux-files");
      const fileInput = document.getElementById("kaggle-inp-file");
      if (inpContent) inpContent.value = "";
      if (inpName) inpName.value = "";
      if (jobNameInput) jobNameInput.value = "";
      if (auxFiles) auxFiles.value = "";
      if (fileInput) fileInput.value = "";

      addJob({
        name: data.job_title || jobLabel,
        jobId: data.job_id,
        rootId: data.job_id,
        chainIds: [data.job_id],
        chain: [{ id: data.job_id, url: data.kaggle_url }],
        kaggleUrl: data.kaggle_url,
        kaggleUsername: data.kaggle_owner || username,
        status: "running",
        submittedAt: Date.now(),
        startedAt: Date.now(),
      });
      switchToTab("jobs");
      window.dispatchEvent(new CustomEvent("chemlab-jobs-rendered"));
    } catch (err) {
      showError(kaggleError, err.message);
    } finally {
      kaggleSubmitInFlight = false;
      if (kaggleSubmitBtn) kaggleSubmitBtn.disabled = false;
      hide(kaggleLoading);
    }
  });

  // ───────────────────────────────────────────────
  // Jobs tracker (localStorage + polling)
  // ───────────────────────────────────────────────
  const jobsListEl = document.getElementById("jobs-list");
  const jobsEmptyEl = document.getElementById("jobs-empty");

  function credsFor(job) {
    const j = (typeof job === "object" && job !== null) ? job : ((typeof job === "string" && job) ? (loadJobs().find(x => x.jobId === job || x.id === job) || {}) : {});
    const user = j.kaggleUsername || (currentKaggle && currentKaggle.username) || localStorage.getItem(LS_KEYS.kaggleUsername) || (kaggleUsernameInput ? kaggleUsernameInput.value.trim() : "") || "";
    const key = j.kaggleKey || (currentKaggle && currentKaggle.key) || sessionKaggleKey || (kaggleKeyInput ? kaggleKeyInput.value.trim() : "") || "";
    return {
      kaggle_username: user,
      kaggle_key: key,
    };
  }


  const MAX_ACTIVE_KAGGLE_JOBS = 5;
  const MAX_TOTAL_JOBS = 20;

  function loadJobs() {
    try {
      const jobs = JSON.parse(localStorage.getItem(LS_KEYS.jobs) || "[]");
      // Scrub any key persisted by an earlier version of this page.
      let dirty = false;
      jobs.forEach(j => { if (j.kaggleKey !== undefined) { delete j.kaggleKey; dirty = true; } });
      if (dirty) localStorage.setItem(LS_KEYS.jobs, JSON.stringify(jobs));
      return jobs;
    } catch { return []; }
  }
  function saveJobs(jobs) { localStorage.setItem(LS_KEYS.jobs, JSON.stringify(jobs)); }

  function addJob(job) {
    const jobs = loadJobs();
    if (jobs.length >= MAX_TOTAL_JOBS) {
      showToast(`Maximum limit of ${MAX_TOTAL_JOBS} jobs in workspace reached. Please delete old or completed jobs before submitting new ones.`, "warning");
      return;
    }
    jobs.unshift(job);
    saveJobs(jobs);
    renderJobs();
    processKaggleQueue();
  }

  function updateJob(jobId, patch) {
    const jobs = loadJobs();
    const idx = jobs.findIndex(j => j.jobId === jobId);
    if (idx === -1) return;
    jobs[idx] = { ...jobs[idx], ...patch };
    saveJobs(jobs);
    renderJobs();
  }

  async function removeJob(jobId) {
    const jobs = loadJobs();
    const job = jobs.find(j => j.jobId === jobId);
    if (!job) return;
    const confirmed = confirm(
      `Delete "${job.name}" from Kaggle and from this list?\n\nThis permanently deletes its notebook from your Kaggle account and can't be undone.`
    );
    if (!confirmed) return;

    // Persist the removal locally first (belt-and-suspenders): even if a
    // Kaggle-side delete call below hits a transient error, the job still
    // stays hidden here instead of silently reappearing the next time this
    // account signs in (see mergeRemoteJobs).
    const idsToDelete = (job.chainIds && job.chainIds.length) ? job.chainIds : [job.jobId];
    const removedIds = loadRemovedIds();
    idsToDelete.forEach(id => removedIds.add(id));
    saveRemovedIds(removedIds);
    saveJobs(jobs.filter(j => j.jobId !== jobId));
    renderJobs();
    processKaggleQueue();

    const failedIds = [];
    for (const id of idsToDelete) {
      try {
        await postJSON("/api/kaggle/delete", { ...credsFor(job), job_id: id });
      } catch (err) {
        failedIds.push(id);
      }
    }
    if (failedIds.length) {
      const what = failedIds.length > 1 ? "some of its Kaggle notebooks" : "its Kaggle notebook";
      const them = failedIds.length > 1 ? "them" : "it";
      showToast(
        `<strong>"${job.name}"</strong> was removed from this list, but ${what} ` +
        `(${failedIds.join(", ")}) could not be deleted from Kaggle. You may want to remove ${them} directly on kaggle.com.`
      );
    }
  }

  const STATUS_LABELS = {
    queued: "Queued",
    waiting_dependency: "Waiting for Stage 1",
    submitting: "Submitting…",
    running: "Running",
    restarting: "Restarting",
    complete: "Complete",
    error: "Error",
    cancelled: "Cancelled",
    unknown: "Unknown",
  };
  const TERMINAL_STATUSES = ["complete", "error", "cancelled"];

  function formatElapsed(ms) {
    const totalSec = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(totalSec / 3600).toString().padStart(2, "0");
    const m = Math.floor((totalSec % 3600) / 60).toString().padStart(2, "0");
    const s = (totalSec % 60).toString().padStart(2, "0");
    return `${h}:${m}:${s}`;
  }

  function renderJobs() {
    const jobs = loadJobs();
    jobsListEl.innerHTML = "";
    jobsEmptyEl.classList.toggle("hidden", jobs.length > 0);

    // Update queue governor statistics banner
    const activeJobs = jobs.filter(j => ["running", "restarting", "submitting"].includes(j.status));
    const queuedJobs = jobs.filter(j => j.status === "queued");
    const waitingDepJobs = jobs.filter(j => j.status === "waiting_dependency");
    const completedJobs = jobs.filter(j => j.status === "complete");

    const statActive = document.getElementById("stat-active-slots");
    const statQueued = document.getElementById("stat-queued-count");
    const statDep = document.getElementById("stat-dep-count");
    const statCompleted = document.getElementById("stat-completed-count");
    if (statActive) statActive.textContent = activeJobs.length;
    if (statQueued) statQueued.textContent = queuedJobs.length;
    if (statDep) statDep.textContent = waitingDepJobs.length;
    if (statCompleted) statCompleted.textContent = completedJobs.length;

    jobs.forEach(job => {
      const li = document.createElement("li");
      li.className = "job-card";

      const info = document.createElement("div");
      info.className = "job-info";
      const nameRow = document.createElement("div");
      nameRow.className = "job-name";
      const nameSpan = document.createElement("span");
      nameSpan.textContent = job.name;
      nameRow.appendChild(nameSpan);

      // Workflow Stage Badges
      if (job.stage) {
        const sBadge = document.createElement("span");
        sBadge.className = `job-stage-badge stage-badge-${job.stage}`;
        sBadge.textContent = job.stage === 1 ? `⚡ Stage 1 (${(job.stageType || "Opt").toUpperCase()})` : `🔗 Stage ${job.stage} (${(job.stageType || "SP").toUpperCase()})`;
        nameRow.appendChild(sBadge);
      }

      const statusBadge = document.createElement("span");
      statusBadge.className = `job-status status-${job.status}`;
      if (job.status === "queued") {
        const qPos = queuedJobs.findIndex(x => x.jobId === job.jobId) + 1;
        statusBadge.textContent = `⏳ Queued (#${qPos || 1})`;
      } else if (job.status === "waiting_dependency") {
        const parentNum = (job.stage || 2) - 1;
        statusBadge.textContent = `🔗 Waiting for Stage ${parentNum}`;
      } else {
        statusBadge.textContent = STATUS_LABELS[job.status] || job.status;
      }
      nameRow.appendChild(statusBadge);

      if (job.status !== "complete") {
        const timerBadge = document.createElement("span");
        timerBadge.className = "job-timer";
        timerBadge.dataset.status = job.status;
        if (job.submittedAt) timerBadge.dataset.submitted = String(job.submittedAt);
        if (job.finishedAt) timerBadge.dataset.finished = String(job.finishedAt);

        if (job.status === "waiting_dependency") {
          timerBadge.textContent = "⏱ Waiting";
        } else if (job.status === "queued" && !job.submittedAt) {
          timerBadge.textContent = "⏱ Queued";
        } else if (["error", "cancelled"].includes(job.status)) {
          timerBadge.textContent = STATUS_LABELS[job.status] || job.status;
          timerBadge.title = "Terminal job state; this is not an elapsed-time counter.";
        } else {
          const start = Number(job.submittedAt || job.startedAt || Date.now());
          const end = job.finishedAt ? Number(job.finishedAt) : Date.now();
          const elapsed = Math.max(0, end - start);
          timerBadge.textContent = `⏱ ${formatElapsed(elapsed)}`;
        }
        nameRow.appendChild(timerBadge);
      }

      if (job.status === "complete") {
        const downloadFullBtn = document.createElement("button");
        downloadFullBtn.type = "button";
        downloadFullBtn.className = "btn btn-ghost btn-small";
        downloadFullBtn.innerHTML = "📦 Full ZIP";
        downloadFullBtn.title = "Download full verified scientific result archive";
        downloadFullBtn.addEventListener("click", () => downloadJobResults(job, downloadFullBtn, "full"));
        nameRow.appendChild(downloadFullBtn);

        const analyzeBtn = document.createElement("button");
        analyzeBtn.type = "button";
        analyzeBtn.className = "btn btn-outline btn-small";
        analyzeBtn.style.marginLeft = "6px";
        analyzeBtn.innerHTML = "🔬 3D & Spectra";
        analyzeBtn.title = "View 3D molecule and spectra directly in browser without downloading";
        analyzeBtn.addEventListener("click", () => analyzeJobInEngine(job, analyzeBtn));
        nameRow.appendChild(analyzeBtn);

        if (job.kaggleUrl) {
          const kaggleLink = document.createElement("a");
          kaggleLink.href = job.kaggleUrl;
          kaggleLink.target = "_blank";
          kaggleLink.rel = "noopener noreferrer";
          kaggleLink.className = "btn btn-ghost btn-small";
          kaggleLink.style.marginLeft = "6px";
          kaggleLink.innerHTML = "🔗 Kaggle";
          kaggleLink.title = "Open notebook directly on Kaggle";
          nameRow.appendChild(kaggleLink);
        }
      }

      const meta = document.createElement("div");
      meta.className = "job-meta";
      const restartNote = job.restarts
        ? ` - continued ${job.restarts} time${job.restarts > 1 ? "s" : ""}`
        : "";
      meta.textContent = `${job.jobId} - submitted ${new Date(job.submittedAt).toLocaleString()}${restartNote}`;

      info.append(nameRow, meta);

      // Every notebook this job has run in, oldest first.
      const chain = (job.chain && job.chain.length) ? job.chain : null;
      if (chain && chain.length > 1) {
        const chainRow = document.createElement("div");
        chainRow.className = "job-chain";
        const label = document.createElement("span");
        label.textContent = "Notebooks: ";
        chainRow.appendChild(label);
        chain.forEach((win, i) => {
          if (i) chainRow.appendChild(document.createTextNode(" · "));
          const a = document.createElement("a");
          a.href = win.url;
          a.target = "_blank";
          a.rel = "noopener";
          a.title = win.id;
          a.textContent = i === chain.length - 1 ? `${i + 1} (current)` : String(i + 1);
          chainRow.appendChild(a);
        });
        info.appendChild(chainRow);
      }

      if (job.warning) {
        const warn = document.createElement("div");
        warn.className = "job-warning";
        warn.textContent = `⚠ ${job.warning}`;
        info.appendChild(warn);
      }

      const actions = document.createElement("div");
      actions.className = "job-actions";
      if (job.kaggleUrl) {
        const kaggleLink = document.createElement("a");
        kaggleLink.className = "btn btn-ghost btn-small";
        kaggleLink.href = job.kaggleUrl;
        kaggleLink.target = "_blank";
        kaggleLink.rel = "noopener";
        kaggleLink.textContent = "View on Kaggle";
        actions.append(kaggleLink);
      }
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "job-remove-btn";
      removeBtn.textContent = "Delete";
      removeBtn.title = "Permanently delete this job's notebook from Kaggle";
      removeBtn.addEventListener("click", () => removeJob(job.jobId));
      actions.append(removeBtn);

      li.append(info, actions);
      jobsListEl.appendChild(li);
    });
    window.dispatchEvent(new CustomEvent("chemlab-jobs-rendered"));
  }

  // Helper to extract clean XYZ coordinates verbatim from an ORCA .inp file content
  function extractCoordsFromInp(inpContent) {
    if (!inpContent) return "";
    const match = inpContent.match(/\*\s*xyz\s+[-+]?\d+\s+[-+]?\d+\s*\n([\s\S]*?)\n\s*\*/i);
    if (match && match[1]) {
      const lines = match[1].trim().split("\n");
      const atomLines = lines.map(l => l.trim()).filter(l => {
        const parts = l.split(/\s+/);
        return parts.length >= 4 && /^[A-Za-z]{1,2}:?$/.test(parts[0]);
      });
      if (atomLines.length > 0) {
        return atomLines.join("\n");
      }
    }
    return "";
  }

  // Finds the coordinates for subsequent stages:
  // 1. Prioritizes the latest completed ancestor stage that performed Geometry Optimization (OPT).
  // 2. If NO OPT stage exists in the workflow (e.g. all SP or Freq), takes the coordinates from the .inp file of the immediately preceding step.
  async function getWorkflowOptimizedCoords(targetJob, allJobs) {
    if (!targetJob || !targetJob.dependsOnWorkflow) return null;
    const workflowId = targetJob.dependsOnWorkflow;
    const currentStageNum = targetJob.stage || 2;
    
    // Find all completed predecessor stages in this workflow, sorted newest first
    const predecessors = (allJobs || loadJobs())
      .filter(x => x.workflowId === workflowId && (x.stage || 1) < currentStageNum && x.status === "complete")
      .sort((a, b) => (b.stage || 1) - (a.stage || 1));

    if (!predecessors.length) return null;

    // 1. Primary: Prioritize the most recent Geometry Optimization (OPT) predecessor
    const optPredecessors = predecessors.filter(isOptimizationJob);
    if (optPredecessors.length > 0) {
      for (const cand of optPredecessors) {
        // a. Cached coordinates from previous extraction
        if (cand.optimizedCoords && cand.optimizedCoords.trim()) {
          return { coords: cand.optimizedCoords.trim(), sourceStage: cand.stage, sourceName: cand.name };
        }
        // b. Fetch from /api/kaggle/extract-opt-coords
        try {
          const resp = await fetch("/api/kaggle/extract-opt-coords", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ...credsFor(cand), job_id: cand.jobId })
          });
          const data = await resp.json().catch(() => null);
          if (data && data.ok && data.coords && data.coords.trim()) {
            const coords = data.coords.trim();
            updateJob(cand.jobId, { optimizedCoords: coords });
            cand.optimizedCoords = coords;
            return { coords, sourceStage: cand.stage, sourceName: cand.name };
          }
        } catch (err) {
          console.warn(`Could not extract coords from OPT Stage ${cand.stage}:`, err.message);
        }
      }
    }

    // 2. Secondary fallback (when NO OPT is available): Take coordinates from the .inp of the immediately preceding step
    const immediatePredecessor = predecessors.find(x => x.stage === currentStageNum - 1) || predecessors[0];
    if (immediatePredecessor) {
      // a. Extract from the .inp file text directly
      const inpCoords = extractCoordsFromInp(immediatePredecessor.input_content);
      if (inpCoords && inpCoords.trim()) {
        return { coords: inpCoords.trim(), sourceStage: immediatePredecessor.stage, sourceName: immediatePredecessor.name };
      }

      // b. Extract from stageConfig / stage2Config
      const cfgCoords = (immediatePredecessor.stageConfig && immediatePredecessor.stageConfig.coords) ||
                        (immediatePredecessor.stage2Config && immediatePredecessor.stage2Config.coords);
      if (cfgCoords && cfgCoords.trim()) {
        return { coords: cfgCoords.trim(), sourceStage: immediatePredecessor.stage, sourceName: immediatePredecessor.name };
      }

      // c. Fetch from /api/kaggle/extract-opt-coords on the immediate predecessor (reads .inp from archive)
      try {
        const resp = await fetch("/api/kaggle/extract-opt-coords", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...credsFor(immediatePredecessor), job_id: immediatePredecessor.jobId })
        });
        const data = await resp.json().catch(() => null);
        if (data && data.ok && data.coords && data.coords.trim()) {
          const coords = data.coords.trim();
          return { coords, sourceStage: immediatePredecessor.stage, sourceName: immediatePredecessor.name };
        }
      } catch (err) {
        console.warn(`Could not extract coords from Stage ${immediatePredecessor.stage}:`, err.message);
      }
    }

    // 3. Tertiary fallback: Check all remaining predecessor configs
    for (const cand of predecessors) {
      const cfgCoords = (cand.stageConfig && cand.stageConfig.coords) || (cand.stage2Config && cand.stage2Config.coords);
      if (cfgCoords && cfgCoords.trim()) {
        return { coords: cfgCoords.trim(), sourceStage: cand.stage, sourceName: cand.name };
      }
    }

    return null;
  }

  let isProcessingQueue = false;

  async function processKaggleQueue() {
    if (isProcessingQueue) return;
    isProcessingQueue = true;
    try {
      const jobs = loadJobs();
      let updated = false;

      // 1. Advance dependent workflow jobs whose parent stage finished
      const waitingDepJobs = jobs.filter(j => j.status === "waiting_dependency");
      for (const j of waitingDepJobs) {
        if (j.dependsOnWorkflow) {
          const parentStageNum = (j.stage || 2) - 1;
          const parentJob = jobs.find(x => x.workflowId === j.dependsOnWorkflow && x.stage === parentStageNum);
          if (parentJob && parentJob.status === "complete") {
            try {
              const optResult = await getWorkflowOptimizedCoords(j, jobs);
              if (optResult && optResult.coords) {
                const stageCfg = j.stageConfig || j.stage2Config || {};
                stageCfg.coords = optResult.coords;
                const genResp = await postJSON("/api/orca/generate", { ...stageCfg, name: stageCfg.name || `${j.name}` });
                if (genResp && genResp.input_text) {
                  j.status = "queued";
                  j.readyAt = Date.now();
                  j.input_content = genResp.input_text;
                  j.stageConfig = stageCfg;
                  j.stage2Config = stageCfg;
                  j.warning = null;
                  updateJob(j.jobId, {
                    status: "queued",
                    readyAt: Date.now(),
                    input_content: genResp.input_text,
                    stageConfig: stageCfg,
                    stage2Config: stageCfg,
                    warning: null,
                  });
                  updated = true;
                  showToast(`🔗 <strong>Stage ${parentStageNum} (${parentJob.name})</strong> finished! Geometry from Stage ${optResult.sourceStage} (${optResult.sourceName}) applied and <strong>Stage ${j.stage} (${j.name})</strong> promoted to active queue.`);
                } else {
                  j.status = "error";
                  j.finishedAt = Date.now();
                  j.warning = `Stage ${parentStageNum} finished, but the server could not generate Stage ${j.stage}'s input file. The chain has been stopped - submit Stage ${j.stage} manually.`;
                  updateJob(j.jobId, {
                    status: "error",
                    finishedAt: Date.now(),
                    warning: j.warning,
                  });
                  updated = true;
                }
              } else {
                j.status = "error";
                j.finishedAt = Date.now();
                j.warning = `Could not extract optimized coordinates from earlier stages for Stage ${j.stage} (${j.name}). The chain has been stopped - previous results are untouched; submit Stage ${j.stage} manually.`;
                updateJob(j.jobId, {
                  status: "error",
                  finishedAt: Date.now(),
                  warning: j.warning,
                });
                updated = true;
                showToast(`⚠️ <strong>Workflow stopped:</strong> no optimized coordinates were found for Stage ${j.stage}.`);
              }
            } catch (e) {
              console.warn(`Could not extract coords for dependent Stage ${j.stage} job:`, e.message);
              j.status = "error";
              j.finishedAt = Date.now();
              j.warning = `Failed to extract optimized coordinates for Stage ${j.stage} (${j.name}): ${e.message}.`;
              updateJob(j.jobId, {
                status: "error",
                warning: j.warning,
                finishedAt: Date.now(),
              });
              updated = true;
            }
          } else if (parentJob && ["error", "cancelled"].includes(parentJob.status)) {
            if (parentJob.status === "restarting") continue;
            j.status = "cancelled";
            j.warning = `Parent Stage ${parentStageNum} calculation (${parentJob.name}) stopped or errored.`;
            updateJob(j.jobId, {
              status: "cancelled",
              warning: j.warning,
            });
            updated = true;
          }
        }
      }

      // 2. Dispatch runnable queued jobs up to MAX_ACTIVE_KAGGLE_JOBS (5)
      let currentActive = jobs.filter(j => ["running", "restarting", "submitting"].includes(j.status)).length;
      if (currentActive < MAX_ACTIVE_KAGGLE_JOBS) {
        const runnable = jobs
          .filter(j => j.status === "queued" && !(j.jobId && j.jobId.startsWith("chem-tools-")))
          .sort((a, b) => {
            const aIsChained = (a.stage && a.stage > 1) ? 1 : 0;
            const bIsChained = (b.stage && b.stage > 1) ? 1 : 0;
            if (aIsChained !== bIsChained) return bIsChained - aIsChained;
            return (a.readyAt || a.submittedAt || 0) - (b.readyAt || b.submittedAt || 0);
          });
        for (const nextJob of runnable) {
          if (currentActive >= MAX_ACTIVE_KAGGLE_JOBS) break;

          const creds = credsFor(nextJob);
          if (!creds.kaggle_username || !creds.kaggle_key) break;
          const orcaSourceKind = nextJob.orcaSourceKind || localStorage.getItem(LS_KEYS.orcaSourceKind) || getSelectedOrcaSourceKind();
          const orcaDataset = nextJob.orcaDataset || localStorage.getItem(LS_KEYS.orcaDataset) || (document.getElementById("kaggle-dataset") ? document.getElementById("kaggle-dataset").value.trim() : "") || "";
          const orcaLink = nextJob.orcaLink || localStorage.getItem(LS_KEYS.orcaLink) || (document.getElementById("kaggle-orca-link") ? document.getElementById("kaggle-orca-link").value.trim() : "") || "";

          if (orcaSourceKind === "google_drive" && !orcaLink) break;
          if (orcaSourceKind === "kaggle_dataset" && !orcaDataset && !orcaLink) break;

          nextJob.status = "submitting";
          updated = true;
          updateJob(nextJob.jobId, { status: "submitting" });
          renderJobs();

          try {
            const form = new FormData();
            form.append("kaggle_username", creds.kaggle_username);
            form.append("kaggle_key", creds.kaggle_key);
            if (orcaSourceKind === "google_drive" || orcaLink) {
              form.append("orca_link", orcaLink);
            } else {
              form.append("dataset_sources", normalizeKaggleDatasetInput(orcaDataset) || orcaDataset);
            }
            form.append("job_name", nextJob.name);
            form.append("input_filename", nextJob.input_filename || `${nextJob.name}.inp`);
            form.append("input_content", nextJob.input_content || "");

            const resp = await fetch("/api/kaggle/submit", {
              method: "POST",
              headers: {
                "Idempotency-Key": "q_" + (nextJob.jobId || "job") + "_" + Date.now() + "_" + Math.random().toString(36).substr(2, 6)
              },
              body: form
            });
            let data = null;
            const contentType = resp.headers.get("content-type") || "";
            if (contentType.includes("application/json")) {
              data = await resp.json().catch(() => null);
            } else {
              const text = await resp.text().catch(() => "");
              const detail = text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 240);
              throw new Error(`Server returned HTTP ${resp.status}${detail ? `: ${detail}` : "."}`);
            }
            if (!data) throw new Error(`Server returned HTTP ${resp.status} with an invalid JSON response.`);
            if (!resp.ok || !data.ok) throw new Error(data.error || `Submission failed (HTTP ${resp.status}).`);

            if (nextJob.jobId && nextJob.jobId.startsWith("chem-tools-")) {
              nextJob.status = "running";
              nextJob.startedAt = nextJob.startedAt || Date.now();
              currentActive++;
              updateJob(nextJob.jobId, { status: "running", startedAt: nextJob.startedAt });
              continue;
            }

            const oldJobId = nextJob.jobId;
            nextJob.jobId = data.job_id;
            nextJob.rootId = data.job_id;
            nextJob.chainIds = [data.job_id];
            nextJob.chain = [{ id: data.job_id, url: data.kaggle_url }];
            nextJob.kaggleUrl = data.kaggle_url;
            nextJob.kaggleUsername = data.kaggle_owner || creds.kaggle_username;
            nextJob.status = "running";
            nextJob.startedAt = Date.now();
            nextJob.submittedAt = Date.now();
            nextJob.finishedAt = null;
            currentActive++;
            
            const currentStoredJobs = loadJobs();
            const storedIdx = currentStoredJobs.findIndex(x => x.jobId === oldJobId);
            if (storedIdx !== -1) {
              currentStoredJobs[storedIdx] = { ...currentStoredJobs[storedIdx], ...nextJob };
              saveJobs(currentStoredJobs);
            } else {
              updateJob(data.job_id, nextJob);
            }
            showToast(`🚀 Launched job <strong>${nextJob.name}</strong> on Kaggle (${currentActive}/5 active slots).`);
          } catch (err) {
            nextJob.status = "error";
            nextJob.warning = err.message;
            updateJob(nextJob.jobId, { status: "error", warning: err.message });
          }
        }
      }

      renderJobs();
    } finally {
      isProcessingQueue = false;
    }
  }
  window.renderJobs = renderJobs;
  window.processKaggleQueue = processKaggleQueue;

  // Ticks every running timer once a second, independent of the 45s status poll.
  setInterval(() => {
    document.querySelectorAll(".job-timer").forEach(el => {
      const status = el.dataset.status;
      if (status === "complete") {
        el.remove();
        return;
      }
      if (["error", "cancelled"].includes(status)) {
        const text = STATUS_LABELS[status] || status;
        if (el.textContent !== text) el.textContent = text;
        return;
      }
      if (status === "waiting_dependency") {
        if (el.textContent !== "⏱ Waiting") el.textContent = "⏱ Waiting";
        return;
      }
      if (status === "queued" && !el.dataset.submitted) {
        if (el.textContent !== "⏱ Queued") el.textContent = "⏱ Queued";
        return;
      }
      if (!el.dataset.submitted) return;
      const submitted = Number(el.dataset.submitted);
      const finished = el.dataset.finished ? Number(el.dataset.finished) : Date.now();
      const newText = `⏱ ${formatElapsed(Math.max(0, finished - submitted))}`;
      if (el.textContent !== newText) el.textContent = newText;
    });
  }, 1000);

  // Background queue governor tick every 8 seconds
  setInterval(processKaggleQueue, 8000);

  // Directly advances a chained workflow when one of its stages completes.
  // Called straight from pollJob() on "complete" so the Stage N -> Stage N+1
  // transition does not depend on (or get silently dropped by) the
  // processKaggleQueue() re-entry guard. Every write goes through updateJob()
  // so it always patches the freshest localStorage state, and the completed
  // parent stage is never re-submitted: it is already terminal ("complete")
  // and the dispatcher only ever touches "queued" jobs without a real
  // chem-tools- id.
  async function advanceWorkflowChain(parentJob) {
    try {
      if (!parentJob || !parentJob.workflowId || !parentJob.stage) return false;
      const jobs = loadJobs();
      const dependents = jobs.filter(j =>
        j.status === "waiting_dependency" &&
        j.dependsOnWorkflow === parentJob.workflowId &&
        (j.stage || 2) - 1 === parentJob.stage
      );
      if (!dependents.length) return false;

      for (const j of dependents) {
        // Never resurrect a stage that already failed terminally: its error
        // state and user-facing explanation must survive later polls of the
        // completed parent stage.
        if (TERMINAL_STATUSES.includes(j.status)) continue;
        try {
          // 1. Pull the geometry from the latest Opt stage in the workflow chain
          const optResult = await getWorkflowOptimizedCoords(j, jobs);
          if (!optResult || !optResult.coords) {
            throw new Error(`no optimized coordinates were found in previous stages for Stage ${j.stage}`);
          }
          // 2. Rebuild the next stage's input around that geometry.
          const stageCfg = j.stageConfig || j.stage2Config || {};
          stageCfg.coords = optResult.coords;
          const genResp = await postJSON("/api/orca/generate", { ...stageCfg, name: stageCfg.name || `${j.name}` });
          if (!genResp || !genResp.input_text) throw new Error("the server could not generate the next stage's input file");
          // 3. Promote to the active queue; the dispatcher submits it.
          j.status = "queued";
          j.readyAt = Date.now();
          j.submittedAt = null;
          j.startedAt = null;
          j.finishedAt = null;
          j.input_content = genResp.input_text;
          j.stageConfig = stageCfg;
          j.stage2Config = stageCfg;
          j.warning = null;
          updateJob(j.jobId, {
            status: "queued",
            readyAt: Date.now(),
            submittedAt: null,
            startedAt: null,
            finishedAt: null,
            input_content: genResp.input_text,
            stageConfig: stageCfg,
            stage2Config: stageCfg,
            warning: null,
          });
          showToast(`🔗 <strong>Stage ${parentJob.stage} (${parentJob.name})</strong> finished! Geometry from Stage ${optResult.sourceStage} (${optResult.sourceName}) applied and <strong>Stage ${j.stage} (${j.name})</strong> promoted to the active queue.`);
        } catch (e) {
          console.warn(`Chain transition failed for Stage ${j.stage}:`, e.message);
          j.status = "error";
          j.finishedAt = Date.now();
          j.warning = `Could not continue the workflow after Stage ${parentJob.stage} (${parentJob.name}): ${e.message}. The chain has been stopped - Stage ${parentJob.stage}'s completed results are untouched. Fix the issue and submit Stage ${j.stage} manually.`;
          updateJob(j.jobId, {
            status: "error",
            finishedAt: Date.now(),
            warning: j.warning,
          });
          showToast(`⚠️ <strong>Workflow stopped:</strong> ${e.message} - Stage ${j.stage} was not submitted.`);
        }
      }
      // Dispatch whatever the promotion produced right away.
      await processKaggleQueue();
      return true;
    } catch (err) {
      console.warn("advanceWorkflowChain failed:", err.message);
      return false;
    }
  }

  async function pollJob(job) {
    // Temp-id jobs the dispatcher has not submitted yet have nothing to poll
    // on Kaggle. Jobs that already hold a real chem-tools- kernel id are
    // ALWAYS polled, whatever the local status says: Kaggle itself keeps a
    // freshly pushed kernel QUEUED until a worker slot frees up, and a server
    // "queued" answer used to overwrite the local state and then strand the
    // job forever (the early-return below skipped it) - the "stuck in Queued
    // while it actually runs on Kaggle" bug.
    if (!job.jobId) return;
    const hasRealKaggleId = job.jobId.startsWith("chem-tools-");
    if (!hasRealKaggleId && ["queued", "waiting_dependency", "submitting"].includes(job.status)) return;
    try {
      const data = await postJSON("/api/kaggle/status", {
        ...credsFor(job), job_id: job.jobId,
      });

      if (data.status === "restarting" && data.next_job_id) {
        const chainIds = (job.chainIds && job.chainIds.length) ? job.chainIds : [job.jobId];
        const chain = (job.chain && job.chain.length)
          ? job.chain : [{ id: job.jobId, url: job.kaggleUrl }];
        const nextUrl = data.next_kaggle_url || job.kaggleUrl;
        if (chainIds.includes(data.next_job_id)) return;
        updateJob(job.jobId, {
          jobId: data.next_job_id,
          rootId: job.rootId || job.jobId,
          status: "running",
          chainIds: [...chainIds, data.next_job_id],
          chain: [...chain, { id: data.next_job_id, url: nextUrl }],
          kaggleUrl: nextUrl,
          restarts: (job.restarts || 0) + 1,
          warning: null,
        });
        return;
      }
      if (data.status === "complete" && job.status !== "complete") {
        updateJob(job.jobId, { status: "complete", finishedAt: Date.now(), warning: data.warning || null, notifiedComplete: true });
        if (!job.notifiedComplete) {
          if (data.warning) {
            showToast(`<strong>${job.name}</strong> stopped early - only partial results are available. Open the Jobs tab for details.`);
          } else {
            showToast(`<strong>${job.name}</strong> finished. Open the Jobs tab to download the results.`);
          }
        }
        // Polling stops here: this job is terminal, and pollAllActiveJobs()
        // only ever selects non-terminal jobs. If this job is a stage of a
        // chained workflow, drive the Stage N -> N+1 transition immediately
        // instead of waiting for the next governor tick.
        await advanceWorkflowChain(job);
        await processKaggleQueue();
        return;
      }
      if (data.status && data.status !== "unknown") {
        const patch = { status: data.status };
        if (TERMINAL_STATUSES.includes(data.status)) {
          patch.finishedAt = job.finishedAt || Date.now();
          processKaggleQueue();
        }
        if (data.warning) patch.warning = data.warning;
        else if (data.status === "error" && data.note) patch.warning = data.note;
        updateJob(job.jobId, patch);
      }
    } catch (err) {
      console.warn(`Status check failed for ${job.jobId}:`, err.message);
      // Transient network/API failure must not demote a running job back to
      // "queued" (which would re-submit it); it stays as-is and the next
      // poll retry is the only effect.
    }
  }

  async function downloadJobResults(job, buttonEl, mode = "full") {
    const originalLabel = buttonEl.textContent;
    buttonEl.disabled = true;
    buttonEl.textContent = "Fetching…";
    try {
      const creds = credsFor(job);
      if (!creds.kaggle_username || !creds.kaggle_key) {
        throw new Error("Kaggle credentials missing. Please make sure you are signed in.");
      }

      const fileName = `${job.jobId}_results.zip`;

      const downloadUrl = `/api/kaggle/download?job_id=${encodeURIComponent(job.jobId)}&kaggle_username=${encodeURIComponent(creds.kaggle_username)}&kaggle_key=${encodeURIComponent(creds.kaggle_key)}&mode=${encodeURIComponent(mode)}&t=${Date.now()}`;

      // Native Direct HTTP Download:
      // Uses a standard HTTP attachment endpoint with real Content-Disposition and Content-Length.
      // Fully compatible with Internet Download Manager (IDM), Free Download Manager,
      // browser built-in downloaders, and all download accelerators without RAM or blob limits.
      const a = document.createElement("a");
      a.style.display = "none";
      a.href = downloadUrl;
      a.download = fileName;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => a.remove(), 2000);

      showToast(`💾 Starting download for <strong>${fileName}</strong>…`);
    } catch (err) {
      showToast(`<strong>Download failed:</strong> ${err.message}`);
    } finally {
      buttonEl.disabled = false;
      buttonEl.textContent = originalLabel;
    }
  }

  function pollAllActiveJobs() {
    const jobs = loadJobs();
    const active = jobs.filter(j => !TERMINAL_STATUSES.includes(j.status));
    active.forEach(pollJob);
    processKaggleQueue();
  }

  document.getElementById("jobs-refresh-btn").addEventListener("click", pollAllActiveJobs);

  // Browsers heavily throttle (or fully suspend) setInterval timers in
  // backgrounded/minimized tabs - exactly the situation for most of an
  // 11-12 hour job, since nobody keeps a tab in the foreground that long.
  // Without this, a job that actually finished hours ago can keep showing
  // its last-known "Running" badge until the throttled interval next
  // happens to fire. Re-checking the moment the tab is looked at again
  // fixes that immediately instead of waiting on the timer.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") pollAllActiveJobs();
  });
  window.addEventListener("focus", pollAllActiveJobs);

  renderJobs();
  pollAllActiveJobs();
  setInterval(pollAllActiveJobs, 45000);

  /* CHEMLAB KAGGLE BROWSER UPGRADE 2026-08-10 */
  (function installKaggleBrowserTools() {
    const loginForm = document.getElementById('kaggle-login-form');
    const userInput = document.getElementById('kaggle-username');
    const keyInput = document.getElementById('kaggle-key');
    if (!loginForm || !userInput || !keyInput) return;

    const tools = document.createElement('div');
    tools.className = 'kaggle-browser-tools';
    tools.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px;';
    const fileLabel = document.createElement('label');
    fileLabel.className = 'btn btn-ghost btn-small';
    fileLabel.textContent = '📄 Load kaggle.json';
    const fileInput = document.createElement('input');
    fileInput.type = 'file'; fileInput.accept = '.json,application/json'; fileInput.style.display = 'none';
    fileLabel.appendChild(fileInput);
    const downloadBtn = document.createElement('button');
    downloadBtn.type = 'button'; downloadBtn.className = 'btn btn-ghost btn-small'; downloadBtn.textContent = '⬇ Save Kaggle credentials';
    const settingsLink = document.createElement('a');
    settingsLink.className = 'btn btn-ghost btn-small'; settingsLink.href = 'https://www.kaggle.com/settings/api';
    settingsLink.target = '_blank'; settingsLink.rel = 'noopener noreferrer'; settingsLink.textContent = 'Kaggle API settings';
    const help = document.createElement('span');
    help.className = 'field-hint'; help.textContent = 'The JSON is read locally in your browser; it is not uploaded as a file.';
    tools.append(fileLabel, downloadBtn, settingsLink, help);
    keyInput.closest('.form-row')?.appendChild(tools);

    fileInput.addEventListener('change', async () => {
      const file = fileInput.files && fileInput.files[0]; if (!file) return;
      try {
        const obj = JSON.parse(await file.text());
        const username = String(obj.username || '').trim();
        const key = String(obj.key || obj.token || '').trim();
        if (!username || !key) throw new Error('The selected JSON does not contain username and key/token.');
        userInput.value = username; keyInput.value = key;
        showToast('kaggle.json loaded locally. Press “Sign in with Kaggle” to verify it.');
      } catch (err) { showToast(`<strong>Could not read kaggle.json:</strong> ${err.message}`); }
      finally { fileInput.value = ''; }
    });

    downloadBtn.addEventListener('click', () => {
      const username = userInput.value.trim(), key = keyInput.value.trim();
      if (!username || !key) { showToast('Enter your Kaggle username and API key/token first.'); return; }
      const isLegacy = /^[0-9a-f]{32}$/.test(key);
      const payload = isLegacy ? JSON.stringify({username, key}, null, 2) + '\n' : key + '\n';
      const blob = new Blob([payload], {type:'text/plain'});
      const url = URL.createObjectURL(blob); const a = document.createElement('a');
      a.href = url; a.download = isLegacy ? 'kaggle.json' : 'access_token'; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });

    function isolateLocalJobsTo(username) {
      if (!username || username.toLowerCase() === (lastIsolatedUser || "").toLowerCase()) return;
      lastIsolatedUser = username;
      try { saveJobs(loadJobs().filter(j => !j.kaggleUsername || j.kaggleUsername.toLowerCase() === username.toLowerCase())); renderJobs(); } catch (_) {}
    }
    // Guarded for non-DOM environments (static analysis, server-side imports):
    // MutationObserver only exists in browsers, and the static test loads this
    // file under plain Node. Behaviour in the browser is unchanged.
    if (typeof MutationObserver === "function" && kaggleSignedInAs) {
      const observer = new MutationObserver(() => { if (currentKaggle?.username) isolateLocalJobsTo(currentKaggle.username); });
      observer.observe(kaggleSignedInAs, {childList:true, characterData:true, subtree:true});
    }

    const jobsToolbar = document.querySelector('.jobs-toolbar');
    if (jobsToolbar && !document.getElementById('jobs-account-refresh-btn')) {
      const b = document.createElement('button'); b.type = 'button'; b.id = 'jobs-account-refresh-btn'; b.className = 'btn btn-ghost btn-small'; b.textContent = '↻ Sync account jobs';
      b.addEventListener('click', async () => {
        if (!currentKaggle) { showToast('Sign in to Kaggle first.'); return; }
        b.disabled = true; const old = b.textContent; b.textContent = 'Syncing…';
        try {
          const data = await postJSON('/api/kaggle/login', {kaggle_username: currentKaggle.username, kaggle_key: currentKaggle.key});
          isolateLocalJobsTo(currentKaggle.username); mergeRemoteJobs(currentKaggle.username, currentKaggle.key, data.jobs || []); pollAllActiveJobs();
          showToast(`Kaggle account synchronized: ${(data.jobs || []).length} Chemistry Lab job(s) found.`);
        } catch (err) { showToast(`<strong>Account sync failed:</strong> ${err.message}`); }
        finally { b.disabled = false; b.textContent = old; }
      });
      jobsToolbar.insertBefore(b, jobsToolbar.firstChild);
    }
    if (jobsToolbar && !document.getElementById('jobs-running-count')) {
      const count = document.createElement('span'); count.id = 'jobs-running-count'; count.className = 'field-hint'; count.style.marginLeft = 'auto'; jobsToolbar.appendChild(count);
      const updateCount = () => { count.textContent = `Active jobs: ${loadJobs().filter(j => ['queued','running','restarting'].includes(j.status)).length}`; };
      updateCount(); setInterval(updateCount, 2000); document.addEventListener('visibilitychange', updateCount);
    }

    const linkInput = document.getElementById('kaggle-orca-link');
    if (linkInput) {
      const wrap = linkInput.parentElement, tools2 = document.createElement('div'); tools2.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;';
      const open = document.createElement('a'); open.className = 'btn btn-ghost btn-small'; open.target = '_blank'; open.rel = 'noopener noreferrer'; open.textContent = '↗ Open link in browser';
      const download = document.createElement('a'); download.className = 'btn btn-outline btn-small'; download.target = '_blank'; download.rel = 'noopener noreferrer'; download.textContent = '⬇ Download ORCA in browser';
      const status = document.createElement('span'); status.className = 'field-hint';
      function direct(raw) {
        const value = (raw || '').trim(); if (!value) return '';
        try { const u = new URL(value); if (u.hostname === 'drive.google.com' || u.hostname === 'docs.google.com') { const m = u.pathname.match(/\/file\/d\/([^/]+)/); const id = (m && m[1]) || u.searchParams.get('id'); if (id) return `https://drive.google.com/uc?export=download&id=${encodeURIComponent(id)}`; } } catch (_) {}
        return value;
      }
      function refresh() { const raw = linkInput.value.trim(), url = direct(raw); open.href = raw || '#'; download.href = url || '#'; open.style.pointerEvents = raw ? '' : 'none'; download.style.pointerEvents = url ? '' : 'none'; status.textContent = raw ? 'Ready - this link can also be opened/downloaded in the browser.' : ''; }
      linkInput.addEventListener('input', refresh); linkInput.addEventListener('change', refresh); tools2.append(open, download, status); wrap.appendChild(tools2); refresh();
    }
  })();

  // ───────────────────────────────────────────────
  // Quantum Chemistry Engine & 3D Viewer Frontend
  // ───────────────────────────────────────────────
  let currentEngineData = null;
  let viewer3D = null;
  let isSpinning = false;
  let showLabels = false;
  let isLabelBold = true;
  let showDipole = false;
  let currentViewportBg = "#0B0F19";
  let currentSpectrumTheme = "dark";
  let currentConvolutedSpectrum = [];

  function populateEngineJobPickers() {
    const picker = document.getElementById("engine-job-picker");
    if (!picker) return;
    const jobs = loadJobs();
    picker.innerHTML = '<option value="">-- Select from your completed Kaggle calculations --</option>';
    let count = 0;
    jobs.forEach(j => {
      const opt = document.createElement("option");
      opt.value = j.jobId || j.id;
      opt.textContent = `${j.name || j.jobId} [${j.status || 'unknown'}] - ${j.createdAt ? new Date(j.createdAt).toLocaleDateString() : ''}`;
      if (j.status === "complete") {
        opt.textContent = `✓ ${opt.textContent}`;
      }
      picker.appendChild(opt);
      count++;
    });
  }

  async function analyzeJobInEngine(job, buttonEl) {
    const originalLabel = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
      buttonEl.disabled = true;
      buttonEl.textContent = "Analyzing…";
    }
    try {
      showView("quantum", "engine");
      const loadingEl = document.getElementById("engine-loading");
      const errorEl = document.getElementById("engine-error");
      const resultEl = document.getElementById("engine-result");
      if (loadingEl) loadingEl.classList.remove("hidden");
      if (errorEl) errorEl.classList.add("hidden");
      if (resultEl) resultEl.classList.add("hidden");

      const jobObj = (typeof job === "object" && job !== null) ? job : (loadJobs().find(x => x.jobId === job || x.id === job) || { jobId: job, name: job });
      const jobId = jobObj.jobId || jobObj.id || (typeof job === "string" ? job : "");
      const creds = credsFor(jobObj);

      if (!creds.kaggle_username || !creds.kaggle_key) {
        throw new Error("Please sign in with your Kaggle username and API key on the Jobs tab first.");
      }

      const data = await postJSON("/api/orca/engine/analyze-job", {
        ...creds,
        job_id: jobId,
      });

      renderEngineResults(data);
      showToast(`Calculation <strong>${data.name || jobId}</strong> loaded into Quantum Engine.`);
    } catch (err) {
      const errorEl = document.getElementById("engine-error");
      if (errorEl) showError(errorEl, err.message);
      showToast(`<strong>Analysis failed:</strong> ${err.message}`, "error");
    } finally {
      const loadingEl = document.getElementById("engine-loading");
      if (loadingEl) loadingEl.classList.add("hidden");
      if (buttonEl) {
        buttonEl.disabled = false;
        buttonEl.textContent = originalLabel;
      }
    }
  }

  function parseMultiWfnCrgText(text) {
    if (!text || typeof text !== "string") return [];
    const lines = text.trim().split(/\r?\n/);
    const charges = [];
    for (const line of lines) {
      const l = line.trim();
      if (!l || l.startsWith("#") || l.startsWith("//") || /^atom/i.test(l) || /^sum/i.test(l) || /^charge/i.test(l) || /^total/i.test(l) || /^---/.test(l)) {
        continue;
      }
      const tokens = l.split(/\s+/);
      if (tokens.length === 1) {
        const val = parseFloat(tokens[0]);
        if (!isNaN(val)) charges.push(val);
      } else if (tokens.length >= 2) {
        const val = parseFloat(tokens[tokens.length - 1]);
        if (!isNaN(val)) charges.push(val);
      }
    }
    return charges;
  }

  function updateLabelModeDropdown(hasHirshfeld) {
    const select = document.getElementById("engine-3d-label-mode");
    if (!select) return;
    const currentVal = select.value;

    // Remove any existing Hirshfeld options first
    Array.from(select.options).forEach(opt => {
      if (opt.value === "hirshfeld" || opt.value === "both_hirshfeld") {
        opt.remove();
      }
    });

    if (hasHirshfeld) {
      const opt1 = document.createElement("option");
      opt1.value = "hirshfeld";
      opt1.textContent = "⚡ Hirshfeld Charges (.crg / MultiWfn)";
      select.appendChild(opt1);

      const opt2 = document.createElement("option");
      opt2.value = "both_hirshfeld";
      opt2.textContent = "🏷+⚡ Atom & Hirshfeld";
      select.appendChild(opt2);
    }

    if (Array.from(select.options).some(o => o.value === currentVal)) {
      select.value = currentVal;
    } else {
      select.value = hasHirshfeld ? "hirshfeld" : "off";
    }
  }

  function applyViewer3DStyle() {
    if (!viewer3D || !currentEngineData) return;
    const styleSelect = document.getElementById("engine-3d-style");
    const styleVal = styleSelect ? styleSelect.value : "ballAndStick";
    viewer3D.removeAllLabels();
    viewer3D.removeAllSurfaces();
    viewer3D.removeAllShapes();
    viewer3D.setStyle({}, {});

    const job = currentEngineData.latest_job || (currentEngineData.jobs && currentEngineData.jobs[currentEngineData.jobs.length - 1]) || (currentEngineData.jobs && currentEngineData.jobs[0]) || currentEngineData || {};
    const elements = job.elements || [];
    const coords = job.coords || [];

    // Base Rendering Style
    if (styleVal === "ballAndStick") {
      viewer3D.setStyle({}, { stick: { radius: 0.14 }, sphere: { scale: 0.28 } });
    } else if (styleVal === "stick") {
      viewer3D.setStyle({}, { stick: { radius: 0.22 } });
    } else if (styleVal === "sphere") {
      viewer3D.setStyle({}, { sphere: { scale: 0.85 } });
    } else if (styleVal === "wireframe") {
      viewer3D.setStyle({}, { line: { linewidth: 2 } });
    }

    // Atom Labels, Direct Font Colors, Badge Styles & Bold Typography
    const labelModeSelect = document.getElementById("engine-3d-label-mode");
    const labelColorSelect = document.getElementById("engine-3d-label-color");
    const labelBadgeSelect = document.getElementById("engine-3d-label-badge");
    const currentMode = labelModeSelect ? labelModeSelect.value : "off";
    const fontColor = labelColorSelect ? labelColorSelect.value : "#FFFFFF";
    const badgeStyle = labelBadgeSelect ? labelBadgeSelect.value : "dark";

    let showBackground = true;
    let backgroundColor = "#0B0F19";
    let backgroundOpacity = 0.92;
    let borderColor = "#64748B";
    let borderThickness = 1.0;
    let fontSize = 15;

    if (badgeStyle === "dark") {
      showBackground = true;
      backgroundColor = "#0B0F19";
      backgroundOpacity = 0.92;
      borderColor = (fontColor === "#000000" || fontColor === "#0b0f19") ? "#64748B" : fontColor;
      borderThickness = 1.0;
    } else if (badgeStyle === "light") {
      showBackground = true;
      backgroundColor = "#FFFFFF";
      backgroundOpacity = 0.95;
      borderColor = (fontColor === "#FFFFFF" || fontColor.toLowerCase() === "#ffffff") ? "#64748B" : fontColor;
      borderThickness = 1.0;
    } else if (badgeStyle === "transparent") {
      showBackground = false;
      backgroundColor = "transparent";
      backgroundOpacity = 0.0;
      borderThickness = 0;
    }

    // Determine target charge array - strict without misleading fallback
    let charges = [];

    if (currentMode === "mulliken" || currentMode === "both_mulliken") {
      charges = (job.mulliken_charges && job.mulliken_charges.length) ? job.mulliken_charges : (job.charges || []);
      if (!charges.length && currentMode === "mulliken") {
        showToast("No Mulliken charges found in this calculation output.", "warning");
      }
    } else if (currentMode === "hirshfeld" || currentMode === "both_hirshfeld") {
      charges = (job.hirshfeld_charges && job.hirshfeld_charges.length) ? job.hirshfeld_charges : ((job.crg_charges && job.crg_charges.length) ? job.crg_charges : []);
      if (!charges.length && currentMode === "hirshfeld") {
        showToast("No Hirshfeld charges found. Upload a MultiWfn .crg file.", "warning");
      }
    }

    if (currentMode !== "off" && elements.length) {
      elements.forEach((el, idx) => {
        const c = coords[idx];
        if (c) {
          let text = `${el}${idx + 1}`;
          const q = (Array.isArray(charges) && typeof charges[idx] === "number") ? charges[idx] : null;

          if ((currentMode === "mulliken" || currentMode === "hirshfeld") && q !== null) {
            text = `${q >= 0 ? '+' : ''}${q.toFixed(3)}`;
          } else if ((currentMode === "both_mulliken" || currentMode === "both_hirshfeld") && q !== null) {
            text = `${el}${idx + 1} (${q >= 0 ? '+' : ''}${q.toFixed(3)})`;
          } else {
            text = `${el}${idx + 1}`;
          }

          viewer3D.addLabel(text, {
            position: { x: c[0], y: c[1], z: c[2] },
            fontSize: fontSize,
            fontColor: fontColor,
            bold: isLabelBold,
            font: "Arial, sans-serif",
            showBackground: showBackground,
            backgroundColor: backgroundColor,
            backgroundOpacity: backgroundOpacity,
            borderColor: borderColor,
            borderThickness: borderThickness,
            alignment: "center",
            inFront: true,
          });
        }
      });
    }




    // 3D Dipole Vector
    if (showDipole && job.dipole_moment_debye && coords.length) {
      // Centroid
      let cx = 0, cy = 0, cz = 0;
      coords.forEach(c => { cx += c[0]; cy += c[1]; cz += c[2]; });
      cx /= coords.length; cy /= coords.length; cz /= coords.length;

      const vec = job.dipole_moment_vector || [0, 1, 0];
      const scale = 0.8;
      const endX = cx + (vec[0] || 0) * scale;
      const endY = cy + (vec[1] || 0) * scale;
      const endZ = cz + (vec[2] || 0) * scale;

      viewer3D.addArrow({
        start: { x: cx, y: cy, z: cz },
        end: { x: endX, y: endY, z: endZ },
        color: "#FFD600",
        radius: 0.12,
      });
      viewer3D.addLabel(`μ = ${job.dipole_moment_debye.toFixed(2)} D`, {
        position: { x: endX, y: endY, z: endZ },
        fontSize: 12,
        fontColor: "#FFD600",
        font: "Arial, sans-serif",
        showBackground: true,
        backgroundColor: "rgba(0,0,0,0.8)",
        backgroundOpacity: 0.8,
        inFront: true,
      });
    }

    viewer3D.render();
  }

  function init3DViewer(xyzString, atomCount) {
    const viewport = document.getElementById("engine-3d-viewport");
    if (!viewport) return;
    viewport.innerHTML = "";

    const bgSelect = document.getElementById("engine-3d-bg");
    currentViewportBg = bgSelect ? bgSelect.value : "#0B0F19";
    viewport.style.backgroundColor = currentViewportBg === "transparent" ? "transparent" : currentViewportBg;

    if (window.$3Dmol && xyzString && xyzString.trim().length > 0) {
      try {
        viewer3D = $3Dmol.createViewer(viewport, {
          backgroundColor: currentViewportBg === "transparent" ? 0x000000 : currentViewportBg,
          defaultcolors: $3Dmol.elementColors.rasmol,
        });
        if (currentViewportBg === "transparent") {
          viewer3D.setBackgroundColor(0x000000, 0); // alpha 0
        } else {
          viewer3D.setBackgroundColor(currentViewportBg, 1);
        }
        viewer3D.addModel(xyzString, "xyz");
        applyViewer3DStyle();
        viewer3D.zoomTo();
        viewer3D.render();
      } catch (err) {
        console.warn("3Dmol initialization failed:", err);
        viewport.innerHTML = `<div style="padding: 2rem; color: var(--text-muted); text-align: center;">XYZ Structure Loaded (${atomCount} atoms)<br><pre class="mono" style="max-height: 300px; overflow: auto; text-align: left; margin-top: 1rem; font-size: 0.8rem;">${xyzString.slice(0, 1000)}</pre></div>`;
      }
    } else {
      viewport.innerHTML = `<div style="padding: 2rem; color: var(--text-muted); text-align: center;">Structure Coordinates Available (${atomCount} atoms)<br><pre class="mono" style="max-height: 300px; overflow: auto; text-align: left; margin-top: 1rem; font-size: 0.8rem;">${(xyzString || '').slice(0, 1000)}</pre></div>`;
    }
  }


  function renderEngineResults(data) {
    if (!data) return;
    currentEngineData = data;
    window.currentEngineData = data;

    const resultEl = document.getElementById("engine-result");
    if (resultEl) resultEl.classList.remove("hidden");

    const job = data.latest_job || (data.jobs && data.jobs[0]) || {};
    const name = data.name || job.name || "ORCA Calculation";
    
    // Header & Badges
    const nameEl = document.getElementById("engine-mol-name");
    if (nameEl) nameEl.textContent = name;

    const formulaBadge = document.getElementById("engine-formula-badge");
    if (formulaBadge) {
      formulaBadge.textContent = job.chemical_formula || job.formula || "Structure";
    }

    const versionBadge = document.getElementById("engine-version-badge");
    if (versionBadge) {
      let orcaVer = job.orca_version || data.orca_version || (data.latest_job && data.latest_job.orca_version);
      if (!orcaVer || orcaVer === "Unknown") {
        const raw = data.raw_text || data.content || "";
        const vMatch = raw.match(/Program\s+Version\s+([\d.]+)/i) || raw.match(/ORCA[^\d\n]*([\d.]+)/i);
        if (vMatch) orcaVer = vMatch[1];
      }
      versionBadge.textContent = orcaVer && orcaVer !== "Unknown" ? (orcaVer.toLowerCase().startsWith("orca") ? orcaVer : `ORCA ${orcaVer}`) : "ORCA";
    }

    const methodBadge = document.getElementById("engine-method-badge");
    if (methodBadge) {
      methodBadge.textContent = job.functional || job.method || "DFT";
    }

    const basisBadge = document.getElementById("engine-basis-badge");
    if (basisBadge) {
      basisBadge.textContent = job.basis_set || "Def2-SVP";
    }

    const solvBadge = document.getElementById("engine-solv-badge");
    if (solvBadge) {
      solvBadge.textContent = job.solvation_model ? `${job.solvation_model} (${job.solvent || ''})` : (job.solvent || "Gas Phase");
    }

    const statusBadge = document.getElementById("engine-status-badge");
    if (statusBadge) {
      const isOk = job.termination_status === "NORMAL" || job.converged !== false;
      statusBadge.textContent = isOk ? "Normal Termination" : (job.termination_status || "Calculation Finished");
      statusBadge.className = `prop-badge ${isOk ? 'badge-success' : 'badge-neutral'}`;
    }

    // Thermochemistry metrics
    const setMetric = (id, val, unit = "Eh") => {
      const el = document.getElementById(id);
      if (!el) return;
      if (typeof val === "number") {
        el.textContent = `${val.toFixed(6)} ${unit}`;
      } else if (val) {
        el.textContent = `${val} ${unit}`;
      } else {
        el.textContent = "-";
      }
    };

    const getVal = (...keys) => {
      for (const k of keys) {
        if (job[k] !== undefined && job[k] !== null && job[k] !== "-") return job[k];
      }
      // If current job doesn't have it (e.g. Opt step in Opt+Freq), check companion jobs in the calculation
      if (window.currentEngineData) {
        const compJobs = window.currentEngineData.jobs || (window.currentEngineData.molecule && window.currentEngineData.molecule.jobs) || [];
        for (const companion of compJobs) {
          for (const k of keys) {
            if (companion[k] !== undefined && companion[k] !== null && companion[k] !== "-") return companion[k];
          }
        }
      }
      return undefined;
    };

    setMetric("engine-val-eel", getVal("e_elec_eh", "total_energy_eh", "electronic_energy_hartree", "scf_energy_hartree"), "Eh");
    setMetric("engine-val-gibbs", getVal("gibbs_free_energy_eh", "gibbs_free_energy_hartree", "final_gibbs_hartree"), "Eh");
    setMetric("engine-val-enthalpy", getVal("total_enthalpy_eh", "enthalpy_hartree", "final_enthalpy_hartree"), "Eh");
    setMetric("engine-val-zpe", getVal("zpe_eh", "zero_point_energy_hartree", "zpe_hartree", "electronic_zpe_eh"), "Eh");

    const dipoleEl = document.getElementById("engine-val-dipole");
    if (dipoleEl) {
      if (typeof job.dipole_moment_debye === "number") {
        dipoleEl.textContent = `${job.dipole_moment_debye.toFixed(3)} Debye`;
      } else {
        dipoleEl.textContent = "-";
      }
    }

    const spinEl = document.getElementById("engine-val-spin");
    if (spinEl) {
      if (typeof job.multiplicity === "number") {
        spinEl.textContent = `2S+1 = ${job.multiplicity} (${(job.multiplicity === 1 ? 'Singlet' : job.multiplicity === 2 ? 'Doublet' : job.multiplicity === 3 ? 'Triplet' : 'Open Shell')})`;
      } else {
        spinEl.textContent = "-";
      }
    }

    // 3D Geometry Viewer
    let xyz = job.xyz || job.xyz_structure || job.last_geometry_xyz || job.input_xyz || "";
    const elements = job.elements || [];
    const coords = job.coords || [];
    let atomCount = elements.length || coords.length || 0;

    if (!xyz && elements.length > 0 && coords.length === elements.length) {
      const lines = [`${elements.length}`, `${name} 3D Geometry`];
      for (let i = 0; i < elements.length; i++) {
        const el = elements[i];
        const c = coords[i];
        if (Array.isArray(c) && c.length >= 3) {
          lines.push(`${el.padEnd(3)} ${Number(c[0]).toFixed(6).padStart(12)} ${Number(c[1]).toFixed(6).padStart(12)} ${Number(c[2]).toFixed(6).padStart(12)}`);
        }
      }
      if (lines.length > 2) {
        xyz = lines.join("\n");
      }
    }

    if (!atomCount && xyz && xyz.trim()) {
      const firstLine = xyz.trim().split("\n")[0].trim();
      const parsedCount = parseInt(firstLine, 10);
      if (!isNaN(parsedCount)) atomCount = parsedCount;
    }

    const atomCountEl = document.getElementById("engine-3d-atomcount");
    if (atomCountEl) atomCountEl.textContent = `${atomCount} atoms`;
    init3DViewer(xyz, atomCount);

    // Helper to extract clean Cartesian lines (Element X Y Z)
    function extractCleanCartesian(xyzStr, elemList, coordList) {
      if (elemList && elemList.length > 0 && coordList && coordList.length === elemList.length) {
        const lines = [];
        for (let i = 0; i < elemList.length; i++) {
          const el = elemList[i];
          const c = coordList[i];
          if (Array.isArray(c) && c.length >= 3) {
            lines.push(`${el.padEnd(2)}   ${Number(c[0]).toFixed(6).padStart(12)}   ${Number(c[1]).toFixed(6).padStart(12)}   ${Number(c[2]).toFixed(6).padStart(12)}`);
          }
        }
        if (lines.length > 0) return lines.join("\n");
      }
      if (xyzStr && xyzStr.trim()) {
        const rawLines = xyzStr.trim().split("\n");
        if (rawLines.length > 2 && /^\s*\d+\s*$/.test(rawLines[0].trim())) {
          return rawLines.slice(2).map(l => l.trim()).filter(Boolean).join("\n");
        }
        return rawLines.map(l => l.trim()).filter(Boolean).join("\n");
      }
      return "";
    }

    const cleanCartesian = extractCleanCartesian(xyz, elements, coords);
    const rawText = (data.raw_text || data.content || "").toLowerCase();
    const calcTypeStr = String(job.calculation_type || job.job_type || job.task || job.calc_type || "").toLowerCase();
    const isOpt = (
      rawText.includes("optimization run done") ||
      rawText.includes("the optimization has converged") ||
      rawText.includes("stationary point") ||
      rawText.includes("geometry optimization") ||
      /\b(opt|optts|cgo|neb|scan)\b/i.test(calcTypeStr) ||
      /!\s*.*\b(opt|optts)\b/i.test(data.raw_text || "")
    );

    // Geometry Optimization Callout Banner
    const optBanner = document.getElementById("engine-opt-banner");
    if (optBanner) {
      if (isOpt && cleanCartesian) {
        optBanner.classList.remove("hidden");
      } else {
        optBanner.classList.add("hidden");
      }
    }

    // Launch New Calculation workflow from current 3D geometry
    const launchNewCalc = () => {
      if (!cleanCartesian) {
        showToast("⚠️ No 3D coordinates available in this calculation to feed into the builder.", "error");
        return;
      }
      const baseName = (data.name || job.name || "molecule").replace(/\.[^/.]+$/, "").replace(/_job\d+/i, "");
      const cleanName = isOpt ? `${baseName}_opt` : `${baseName}_geom`;
      const formula = job.chemical_formula || job.formula || "";

      // 1. Switch to ORCA Input Generator tab
      if (typeof window.showChemistryView === "function") {
        window.showChemistryView("orca", "generator");
      }

      // 2. Pre-fill coords status in generator if present
      const manualStatus = document.getElementById("orca-coords-status");
      if (manualStatus) manualStatus.textContent = `Structure loaded: ${cleanName || 'Custom coordinates'}`;

      // 3. Open Wizard directly into 3D Molecular Builder
      openWizard({
        step: "builder3d",
        coords: cleanCartesian,
        name: cleanName,
        formula: formula
      });

      showToast(`🚀 ${isOpt ? "Optimized geometry" : "3D coordinates"} loaded into 3D Builder for new calculation.`);
    };

    const newCalcBtn = document.getElementById("engine-3d-new-calc-btn");
    if (newCalcBtn) {
      if (cleanCartesian) {
        newCalcBtn.classList.remove("hidden");
        newCalcBtn.onclick = launchNewCalc;
      } else {
        newCalcBtn.classList.add("hidden");
      }
    }

    const optLaunchBtn = document.getElementById("engine-opt-launch-btn");
    if (optLaunchBtn) {
      optLaunchBtn.onclick = launchNewCalc;
    }

    // Download XYZ button href
    const xyzBtn = document.getElementById("engine-download-xyz-btn");
    if (xyzBtn && xyz) {
      const blob = new Blob([xyz], { type: "chemical/x-xyz;charset=utf-8" });
      xyzBtn.href = URL.createObjectURL(blob);
      xyzBtn.download = `${name.replace(/\.[^/.]+$/, "")}_optimized.xyz`;
    }

    const hasHirshfeld = (job.hirshfeld_charges && job.hirshfeld_charges.length > 0) || (job.crg_charges && job.crg_charges.length > 0);
    updateLabelModeDropdown(hasHirshfeld);

    // Export JSON Button
    const exportJsonBtn = document.getElementById("engine-btn-export-json");
    if (exportJsonBtn) {
      exportJsonBtn.onclick = () => {
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${name.replace(/\.[^/.]+$/, "")}_quantum_analysis.json`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      };
    }

    // Frequencies & TS Section
    const freqTbody = document.getElementById("engine-freq-tbody");
    const tsBadge = document.getElementById("engine-ts-badge");
    const freqs = job.vibrational_frequencies_cm || job.vibrational_frequencies || job.frequencies || [];
    if (freqTbody) {
      freqTbody.innerHTML = "";
      if (freqs.length > 0) {
        const imagCount = freqs.filter(f => (typeof f === 'number' ? f : f.frequency_cm) < 0).length;
        if (tsBadge) {
          if (imagCount === 0) {
            tsBadge.textContent = "Ground State (0 Im Freq)";
            tsBadge.className = "prop-badge badge-success";

          } else if (imagCount === 1) {
            tsBadge.textContent = "Transition State (1 Im Freq)";
            tsBadge.className = "prop-badge badge-warning";
          } else {
            tsBadge.textContent = `Higher-Order Saddle (${imagCount} Im Freq)`;
            tsBadge.className = "prop-badge badge-error";
          }
        }

        freqs.forEach((f, idx) => {
          const val = typeof f === "number" ? f : f.frequency_cm;
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td style="font-family:var(--font-mono); font-size:0.8rem;">#${idx + 1}</td>
            <td style="font-family:var(--font-mono); font-size:0.8rem; ${val < 0 ? 'color:var(--accent-danger); font-weight:bold;' : ''}">
              ${val.toFixed(2)} cm⁻¹
            </td>
            <td style="font-size:0.75rem; color:var(--text-muted);">${val < 0 ? 'Imaginary (TS vector)' : 'Real Vibration'}</td>
          `;
          freqTbody.appendChild(tr);
        });
      } else {
        if (tsBadge) tsBadge.textContent = "No Vibrational Frequencies in Output";
        freqTbody.innerHTML = `<tr><td colspan="3" style="text-align:center; color:var(--text-muted); padding:1rem;">Single point or optimization without frequency calculation.</td></tr>`;
      }
    }

    // Orbitals & Frontier Energy Levels
    const homo = job.homo_ev !== undefined ? job.homo_ev : (job.homo_energy_ev !== undefined ? job.homo_energy_ev : null);
    const lumo = job.lumo_ev !== undefined ? job.lumo_ev : (job.lumo_energy_ev !== undefined ? job.lumo_energy_ev : null);
    const gap = job.homo_lumo_gap_ev !== undefined ? job.homo_lumo_gap_ev : (homo !== null && lumo !== null ? Math.abs(lumo - homo) : null);

    const homoEl = document.getElementById("engine-val-homo");
    const lumoEl = document.getElementById("engine-val-lumo");
    const gapEl = document.getElementById("engine-val-gap");

    if (homoEl) homoEl.textContent = homo !== null ? `${homo.toFixed(3)} eV` : " - eV";
    if (lumoEl) lumoEl.textContent = lumo !== null ? `${lumo.toFixed(3)} eV` : " - eV";
    if (gapEl) gapEl.textContent = gap !== null ? `ΔEgap: ${gap.toFixed(3)} eV` : "ΔEgap: - eV";

    // Conceptual DFT (CDFT) Reactivity Descriptors
    const ip = job.ionization_potential_ev !== undefined ? job.ionization_potential_ev : (homo !== null ? -homo : null);
    const ea = job.electron_affinity_ev !== undefined ? job.electron_affinity_ev : (lumo !== null ? -lumo : null);
    const hardness = job.chemical_hardness_ev !== undefined ? job.chemical_hardness_ev : (ip !== null && ea !== null ? (ip - ea) / 2 : null);
    const softness = job.chemical_softness_ev !== undefined ? job.chemical_softness_ev : (hardness !== null && hardness > 0 ? 1 / (2 * hardness) : null);
    const electronegativity = job.electronegativity_ev !== undefined ? job.electronegativity_ev : (ip !== null && ea !== null ? (ip + ea) / 2 : null);
    const potential = job.chemical_potential_ev !== undefined ? job.chemical_potential_ev : (electronegativity !== null ? -electronegativity : null);
    const electrophilicity = job.electrophilicity_index_ev !== undefined ? job.electrophilicity_index_ev : (potential !== null && hardness !== null && hardness > 0 ? (potential * potential) / (2 * hardness) : null);

    const setCdft = (id, val, unit = "eV") => {
      const el = document.getElementById(id);
      if (el) el.textContent = val !== null ? `${val.toFixed(3)} ${unit}` : "-";
    };
    setCdft("cdft-hardness", hardness, "eV");
    setCdft("cdft-softness", softness, "eV⁻¹");
    setCdft("cdft-electronegativity", electronegativity, "eV");
    setCdft("cdft-potential", potential, "eV");
    setCdft("cdft-electrophilicity", electrophilicity, "eV");
    setCdft("cdft-ip", ip, "eV");

    // UV-Vis / TD-DFT Multi-Spectrum Analysis Studio
    const uvvisSec = document.getElementById("engine-uvvis-section");
    const transitions = job.transitions || job.excited_states || [];
    if (uvvisSec) {
      if (transitions.length > 0) {
        uvvisSec.classList.remove("hidden");
        addTheoreticalCalculationToStudio(name, transitions, job.method || "TD-DFT", job.basis_set || "");
        drawUVVisMultiSpectrum();
      } else {
        uvvisSec.classList.add("hidden");
      }
    }

    // Vibrational IR / FTIR Multi-Spectrum Studio
    const irSec = document.getElementById("engine-ir-section");
    const irModesTbody = document.getElementById("engine-ir-modes-tbody");
    const irModes = job.ir_spectrum || [];
    const vibFreqs = job.vibrational_frequencies_cm || job.vibrational_frequencies || job.frequencies || [];
    
    if (irSec) {
      if (irModes.length > 0 || vibFreqs.length > 0) {
        irSec.classList.remove("hidden");
        
        // Populate theoretical IR table
        if (irModesTbody) {
          irModesTbody.innerHTML = "";
          const list = irModes.length > 0 ? irModes : vibFreqs.map((f, idx) => ({ mode: idx + 1, frequency_cm: typeof f === 'number' ? f : (f.frequency_cm ?? f.wavenumber_cm ?? f.freq ?? 0), intensity_km_mol: (typeof f === 'object' && (f.intensity_km_mol ?? f.intensity)) != null ? (f.intensity_km_mol ?? f.intensity) : 10.0 }));
          list.forEach((m, idx) => {
            const freq = typeof m === 'number' ? m : (m.frequency_cm != null ? m.frequency_cm : (m.wavenumber_cm != null ? m.wavenumber_cm : (m.freq != null ? m.freq : 0)));
            const t2 = (typeof m === 'object' && (m.intensity_km_mol != null || m.intensity != null)) ? (m.intensity_km_mol ?? m.intensity) : 10.0;
            const tr = document.createElement("tr");
            tr.innerHTML = `
              <td style="font-family:var(--font-mono); font-size:0.8rem;">#${m.mode || (idx + 1)}</td>
              <td style="font-family:var(--font-mono); font-size:0.8rem; ${freq < 0 ? 'color:var(--accent-danger); font-weight:bold;' : ''}">
                ${freq.toFixed(2)} cm⁻¹
              </td>
              <td style="font-family:var(--font-mono); font-size:0.8rem; color:var(--primary); font-weight:600;">
                ${t2.toFixed(3)}
              </td>
              <td style="font-size:0.75rem; color:var(--text-muted);">${freq < 0 ? 'Imaginary (TS)' : 'Harmonic IR Mode'}</td>
            `;
            irModesTbody.appendChild(tr);
          });
        }

        addTheoreticalIRCalculationToStudio(name, irModes.length > 0 ? irModes : vibFreqs, job.method || "ORCA DFT", job.basis_set || "");
        drawIRMultiSpectrum();
      } else {
        irSec.classList.add("hidden");
      }
    }

    // Calculated NMR Spectrum Analysis Studio (1H & 13C)
    const nmrSec = document.getElementById("engine-nmr-section");
    const nmrData = job.nmr || null;
    if (nmrSec) {
      if (nmrData && (nmrData.has_h1 || nmrData.has_c13 || (nmrData.atoms && nmrData.atoms.length > 0))) {
        nmrSec.classList.remove("hidden");
        loadNMRDataIntoStudio(nmrData, name, job);
      } else {
        nmrSec.classList.add("hidden");
      }
    }
  }
  window.renderEngineResults = renderEngineResults;


  // =========================================================================

  // Interactive Multi-Spectrum Analysis Studio & Experimental Overlay Layer
  // =========================================================================
  const userAssignedSeriesColors = new Map();
  const EXP_PALETTE = ["#06b6d4", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6", "#3b82f6", "#14b8a6", "#f97316", "#a855f7", "#0ea5e9", "#84cc16", "#e11d48"];
  const THEO_PALETTE = ["#818cf8", "#a855f7", "#f59e0b", "#ec4899", "#38bdf8", "#fb7185"];

  let loadedExperimentalFiles = [];
  let loadedExperimentalSpectra = [];
  let loadedTheoreticalSpectra = [];
  let showTheoreticalSticks = true;
  let showExperimentalPeaks = true;
  let showUVVisLegend = true;
  let spectrumNormalizeMode = "none";
  let spectrumViewportMode = "full";
  let uvvisManualRangeEnabled = false;
  let uvvisManualMinX = null;
  let uvvisManualMaxX = null;
  let uvvisManualMinY = null;
  let uvvisManualMaxY = null;
  let activeUVVisCandidateYHeaders = [];
  let activeExpFileObj = null;

  window._spectroscopyStudio = {
    get loadedExperimentalFiles() { return loadedExperimentalFiles; },
    get loadedExperimentalSpectra() { return loadedExperimentalSpectra; },
    get loadedExperimentalIRFiles() { return loadedExperimentalIRFiles; },
    get loadedExperimentalIRSpectra() { return loadedExperimentalIRSpectra; },
    get loadedTheoreticalIRSpectra() { return loadedTheoreticalIRSpectra; },
    setTheoreticalIRSpectra(list) { loadedTheoreticalIRSpectra = list; },
    setExperimentalIRSpectra(list) { loadedExperimentalIRSpectra = list; },
    setCurrentEngineData(data) { currentEngineData = data; window.currentEngineData = data; },
    get userAssignedSeriesColors() { return userAssignedSeriesColors; },
    get uvvisManualRangeEnabled() { return uvvisManualRangeEnabled; },
    get irManualRangeEnabled() { return irManualRangeEnabled; }
  };

  function getOrAssignSeriesColor(fileName, seriesLabel, defaultIdx = 0, isIR = false) {
    const key = `${fileName}::${seriesLabel}`;
    if (userAssignedSeriesColors.has(key)) {
      return userAssignedSeriesColors.get(key);
    }
    const palette = isIR ? EXP_IR_PALETTE : EXP_PALETTE;
    const col = palette[defaultIdx % palette.length];
    userAssignedSeriesColors.set(key, col);
    return col;
  }

  function syncUVVisFlattenedSpectra() {
    const active = [];
    loadedExperimentalFiles.forEach(file => {
      file.series_list.forEach(series => {
        if (file.selected_y_cols.includes(series.label || series.column_absorbance)) {
          active.push(series);
        }
      });
    });
    loadedExperimentalSpectra = active;
    updateSpectrumLayersTray();
    if (active.length > 0) {
      populateExperimentalPointsTable(active[0]);
    } else {
      clearExperimentalPointsTable();
    }
  }

  function addTheoreticalCalculationToStudio(name, transitions, method = "ORCA TD-DFT", basis = "", extra = {}) {
    if (!transitions || !transitions.length) return;
    const id = `theo_${Date.now()}_${Math.random().toString(36).substr(2, 5)}`;
    const color = THEO_PALETTE[loadedTheoreticalSpectra.length % THEO_PALETTE.length];
    
    // Check if calculation with same name already exists
    const existingIdx = loadedTheoreticalSpectra.findIndex(t => t.name === name);
    const item = {
      id,
      name: name || "Theoretical TD-DFT",
      method,
      basis,
      transitions,
      color,
      visible: true,
      imported: false,
      ...extra,
    };
    if (existingIdx >= 0) {
      loadedTheoreticalSpectra[existingIdx] = item;
    } else {
      loadedTheoreticalSpectra.push(item);
    }
    updateSpectrumLayersTray();
  }

  function addExperimentalSpectrumToStudio(expData) {
    if (!expData || !expData.points || !expData.points.length) return;
    const id = `exp_${Date.now()}_${Math.random().toString(36).substr(2, 5)}`;
    const fileName = expData.file_name || "experimental_spectrum.txt";
    const seriesLabel = expData.column_absorbance || "Absorbance";
    const color = getOrAssignSeriesColor(fileName, seriesLabel, loadedExperimentalSpectra.length, false);
    
    const item = {
      id,
      file_name: fileName,
      sheet_name: expData.sheet_name,
      label: seriesLabel,
      column_wavelength: expData.column_wavelength || "Wavelength",
      column_absorbance: seriesLabel,
      units_wavelength: expData.units_wavelength || "nm",
      units_y: expData.units_y || "AU",
      y_quantity: expData.y_quantity || "absorbance",
      raw_hash: expData.raw_hash || "hash",
      points_count: expData.points_count || expData.points.length,
      raw_data: expData.points.map(p => ({ wavelength_nm: p.wavelength_nm, absorbance: p.absorbance })),
      peaks: expData.detected_peaks || [],
      color,
      color_manually_set: userAssignedSeriesColors.has(`${fileName}::${seriesLabel}`),
      visible: true,
    };
    
    // Check if file already tracked in loadedExperimentalFiles
    let fileRec = loadedExperimentalFiles.find(f => f.file_name === fileName && f.sheet_name === expData.sheet_name);
    if (!fileRec) {
      fileRec = {
        file_id: `file_${Date.now()}_${Math.random().toString(36).substr(2, 5)}`,
        file_name: fileName,
        format: /\.(xlsx|xlsm|xltx)$/i.test(fileName) ? "excel" : "csv",
        sheet_name: expData.sheet_name,
        wavelength_col: expData.column_wavelength || "Wavelength",
        available_y_cols: [seriesLabel],
        selected_y_cols: [seriesLabel],
        series_list: [item]
      };
      loadedExperimentalFiles.push(fileRec);
    } else {
      if (!fileRec.available_y_cols.includes(seriesLabel)) {
        fileRec.available_y_cols.push(seriesLabel);
      }
      if (!fileRec.selected_y_cols.includes(seriesLabel)) {
        fileRec.selected_y_cols.push(seriesLabel);
      }
      const existingSeriesIdx = fileRec.series_list.findIndex(s => s.label === seriesLabel);
      if (existingSeriesIdx >= 0) {
        fileRec.series_list[existingSeriesIdx] = item;
      } else {
        fileRec.series_list.push(item);
      }
    }

    syncUVVisFlattenedSpectra();
    drawUVVisMultiSpectrum();
  }

  function updateSpectrumLayersTray() {
    const tray = document.getElementById("spectrum-layers-tray");
    const listEl = document.getElementById("spectrum-layers-list");
    const countEl = document.getElementById("spectrum-layers-count");
    if (!listEl) return;
    
    const totalCount = loadedTheoreticalSpectra.length + loadedExperimentalSpectra.length;
    if (countEl) countEl.textContent = totalCount;
    listEl.innerHTML = "";

    // Render Theoretical Layers
    loadedTheoreticalSpectra.forEach((theo, idx) => {
      const card = document.createElement("div");
      card.className = `spectrum-layer-card is-theo ${theo.visible ? '' : 'is-muted'}`;
      card.innerHTML = `
        <div class="layer-info-left">
          <input type="checkbox" class="layer-toggle-cb" data-type="theo" data-index="${idx}" ${theo.visible ? 'checked' : ''}>
          <input type="color" class="layer-color-picker" data-type="theo" data-index="${idx}" value="${theo.color}" title="Change curve color">
          <div class="layer-name-wrap">
            <span class="layer-name-title" title="${theo.name}">${theo.name}</span>
            <span class="layer-meta-sub">${theo.transitions.length} transitions • ${theo.method || 'TD-DFT'}</span>
          </div>
        </div>
        <div class="layer-info-actions">
          <button type="button" class="btn-layer-del" data-xy-uv-theo="${idx}" title="Export XY (Origin-friendly text)">&#8681;</button>
          <button type="button" class="btn-layer-del" data-rename-uv-theo="${idx}" title="Rename spectrum (display name only)">&#9998;</button>
        </div>
        <span class="layer-badge badge-theo">THEORY</span>
      `;
      listEl.appendChild(card);
    });

    // Render Experimental Layers
    loadedExperimentalSpectra.forEach((exp, idx) => {
      const card = document.createElement("div");
      card.className = `spectrum-layer-card is-exp ${exp.visible ? '' : 'is-muted'}`;
      card.innerHTML = `
        <div class="layer-info-left">
          <input type="checkbox" class="layer-toggle-cb" data-type="exp" data-index="${idx}" ${exp.visible ? 'checked' : ''}>
          <input type="color" class="layer-color-picker" data-type="exp" data-index="${idx}" value="${exp.color}" title="Change curve color">
          <div class="layer-name-wrap">
            <span class="layer-name-title" title="${exp.file_name}">${exp.file_name}${exp.sheet_name ? ` (${exp.sheet_name})` : ''}</span>
            <span class="layer-meta-sub">${exp.points_count} pts • hash: ${exp.raw_hash.slice(0, 8)}...</span>
          </div>
        </div>
        <div style="display: flex; align-items: center; gap: 0.4rem;">
          <div class="layer-info-actions">
            <button type="button" class="layer-del-btn" data-xy-uv-exp="${idx}" title="Export XY (Origin-friendly text)">&#8681;</button>
            <button type="button" class="layer-del-btn" data-rename-uv-exp="${idx}" title="Rename spectrum (display name only)">&#9998;</button>
          </div>
          <span class="layer-badge badge-exp">EXP</span>
          <button type="button" class="layer-del-btn" data-del-exp="${idx}" title="Remove experimental dataset">✕</button>
        </div>
      `;
      listEl.appendChild(card);
    });

    // Attach checkbox events
    listEl.querySelectorAll(".layer-toggle-cb").forEach(cb => {
      cb.addEventListener("change", (e) => {
        const type = cb.dataset.type;
        const idx = parseInt(cb.dataset.index, 10);
        if (type === "theo" && loadedTheoreticalSpectra[idx]) {
          loadedTheoreticalSpectra[idx].visible = cb.checked;
        } else if (type === "exp" && loadedExperimentalSpectra[idx]) {
          loadedExperimentalSpectra[idx].visible = cb.checked;
        }
        drawUVVisMultiSpectrum();
      });
    });

    // Attach color picker events
    listEl.querySelectorAll(".layer-color-picker").forEach(cp => {
      const handleColor = (e) => {
        const type = cp.dataset.type;
        const idx = parseInt(cp.dataset.index, 10);
        if (type === "theo" && loadedTheoreticalSpectra[idx]) {
          loadedTheoreticalSpectra[idx].color = e.target.value;
        } else if (type === "exp" && loadedExperimentalSpectra[idx]) {
          loadedExperimentalSpectra[idx].color = e.target.value;
        }
        drawUVVisMultiSpectrum();
      };
      cp.addEventListener("input", handleColor);
      cp.addEventListener("change", handleColor);
    });

    // Attach delete events
    listEl.querySelectorAll("[data-del-exp]").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.delExp, 10);
        if (loadedExperimentalSpectra[idx]) {
          loadedExperimentalSpectra.splice(idx, 1);
          updateSpectrumLayersTray();
          drawUVVisMultiSpectrum();
          if (!loadedExperimentalSpectra.length) {
            clearExperimentalPointsTable();
          }
        }
      });
    });
    listEl.querySelectorAll("[data-xy-uv-theo]").forEach(btn => {
      btn.addEventListener("click", () => exportUVXYSingle("theo", parseInt(btn.dataset.xyUvTheo, 10)));
    });
    listEl.querySelectorAll("[data-xy-uv-exp]").forEach(btn => {
      btn.addEventListener("click", () => exportUVXYSingle("exp", parseInt(btn.dataset.xyUvExp, 10)));
    });
    listEl.querySelectorAll("[data-rename-uv-theo]").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.renameUvTheo, 10);
        const entry = loadedTheoreticalSpectra[idx];
        if (entry) {
          const next = (window.prompt("Rename spectrum (display name only - source file and data are untouched):", entry.name) || "").trim();
          if (next) {
            entry.name = next;
            updateSpectrumLayersTray();
            drawUVVisMultiSpectrum();
          }
        }
      });
    });
    listEl.querySelectorAll("[data-rename-uv-exp]").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.renameUvExp, 10);
        const entry = loadedExperimentalSpectra[idx];
        if (entry) {
          const next = (window.prompt("Rename spectrum (display name only - source file is untouched):", entry.label || entry.file_name) || "").trim();
          if (next) {
            entry.label = next;
            updateSpectrumLayersTray();
            drawUVVisMultiSpectrum();
          }
        }
      });
    });

  }

  function populateExperimentalPointsTable(exp) {
    const tbody = document.getElementById("engine-exp-tbody");
    if (!tbody || !exp || !exp.raw_data) return;
    tbody.innerHTML = "";
    const peakWls = new Set((exp.peaks || []).map(pk => Math.round(pk.wavelength_nm * 10) / 10));
    
    // Sample or show first 300 points
    const pts = exp.raw_data.slice(0, 400);
    pts.forEach((p, idx) => {
      const isPeak = peakWls.has(Math.round(p.wavelength_nm * 10) / 10);
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td class="mono">${p.wavelength_nm.toFixed(2)}</td>
        <td class="mono ${isPeak ? 'text-teal' : ''}">${p.absorbance.toFixed(5)}</td>
        <td>${isPeak ? '<span class="prop-badge badge-success">Peak Max</span>' : '<span class="field-hint">Measured</span>'}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  function clearExperimentalPointsTable() {
    const tbody = document.getElementById("engine-exp-tbody");
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No experimental spectrum loaded. Click "+ Upload Experimental" above to add reference data.</td></tr>`;
    }
  }

  function computeUvvisConvolution(transitions, sigmaNm, shiftNm, startNm = 180, endNm = 800, stepNm = 1) {
    if (!transitions || !transitions.length || sigmaNm <= 0) return [];
    const valid = [];
    transitions.forEach(t => {
      let nm = t.wavelength_nm;
      if (!nm && t.energy_cm > 0) nm = 1e7 / t.energy_cm;
      const fosc = t.oscillator_strength != null ? t.oscillator_strength : (t.fosc || 0);
      if (nm && nm > 0 && fosc >= 0) {
        valid.push({ nm: nm + shiftNm, fosc });
      }
    });
    if (!valid.length) return [];

    const curve = [];
    const normFactor = 1.0 / (sigmaNm * Math.sqrt(2.0 * Math.PI));
    const twoSigmaSq = 2.0 * sigmaNm * sigmaNm;

    for (let currentNm = startNm; currentNm <= endNm; currentNm += stepNm) {
      let intensity = 0.0;
      for (let i = 0; i < valid.length; i++) {
        const diff = currentNm - valid[i].nm;
        if (Math.abs(diff) <= 5.0 * sigmaNm) {
          intensity += valid[i].fosc * Math.exp(-(diff * diff) / twoSigmaSq);
        }
      }
      curve.push({
        wavelength_nm: Math.round(currentNm * 100) / 100,
        intensity: Math.round(intensity * normFactor * 1000.0 * 1e6) / 1e6
      });
    }
    return curve;
  }

  function interpolatePoint(points, targetX, xKey, yKey) {
    if (!points || points.length === 0) return null;
    if (points.length === 1) return points[0][yKey];

    const firstX = points[0][xKey];
    const lastX = points[points.length - 1][xKey];
    const isAsc = firstX < lastX;
    const minX = isAsc ? firstX : lastX;
    const maxX = isAsc ? lastX : firstX;

    if (targetX < minX || targetX > maxX) return null;

    for (let i = 0; i < points.length - 1; i++) {
      const p1 = points[i];
      const p2 = points[i + 1];
      const x1 = p1[xKey];
      const x2 = p2[xKey];
      if ((x1 <= targetX && targetX <= x2) || (x2 <= targetX && targetX <= x1)) {
        if (Math.abs(x2 - x1) < 1e-9) return p1[yKey];
        const t = (targetX - x1) / (x2 - x1);
        return p1[yKey] + t * (p2[yKey] - p1[yKey]);
      }
    }
    return null;
  }

  function getUVVisBounds(activeTheos, activeExps) {
    let minX = 180;
    let maxX = 800;

    if (spectrumViewportMode === "custom") {
      const minInp = parseFloat(document.getElementById("spectrum-min-wl")?.value || "200");
      const maxInp = parseFloat(document.getElementById("spectrum-max-wl")?.value || "700");
      minX = isNaN(minInp) ? 200 : minInp;
      maxX = isNaN(maxInp) ? 700 : maxInp;
      if (minX >= maxX) minX = 200, maxX = 700;
    } else if (spectrumViewportMode === "uvvis_standard") {
      minX = 200;
      maxX = 700;
    } else if (spectrumViewportMode === "common" && (activeExps || []).length > 0 && (activeTheos || []).length > 0) {
      const expMin = Math.max(...activeExps.map(e => Math.min(...e.raw_data.map(p => p.wavelength_nm))));
      const expMax = Math.min(...activeExps.map(e => Math.max(...e.raw_data.map(p => p.wavelength_nm))));
      minX = Math.max(180, Math.floor(expMin));
      maxX = Math.min(800, Math.ceil(expMax));
      if (minX >= maxX) { minX = 200; maxX = 700; }
    } else {
      const allMins = [
        ...(activeExps || []).map(e => Math.min(...e.raw_data.map(p => p.wavelength_nm))),
        180
      ];
      const allMaxs = [
        ...(activeExps || []).map(e => Math.max(...e.raw_data.map(p => p.wavelength_nm))),
        800
      ];
      minX = Math.max(100, Math.floor(Math.min(...allMins)));
      maxX = Math.min(1000, Math.ceil(Math.max(...allMaxs)));
    }

    if (uvvisManualRangeEnabled) {
      if (uvvisManualMinX !== null && !isNaN(uvvisManualMinX)) minX = uvvisManualMinX;
      if (uvvisManualMaxX !== null && !isNaN(uvvisManualMaxX)) maxX = uvvisManualMaxX;
      if (minX >= maxX) maxX = minX + 50;
    }
    return { minX, maxX };
  }

  function getUVVisPadding() {
    return { top: showUVVisLegend ? 58 : 35, right: 65, bottom: 48, left: 65 };
  }

  function getIRBounds(activeTheos, activeExps) {
    let minWn = 400;
    let maxWn = 4000;

    if (irViewportMode === "fingerprint") {
      minWn = 400; maxWn = 1500;
    } else if (irViewportMode === "functional") {
      minWn = 1500; maxWn = 4000;
    } else if (irViewportMode === "custom") {
      const minInp = parseFloat(document.getElementById("ir-min-wn")?.value || "400");
      const maxInp = parseFloat(document.getElementById("ir-max-wn")?.value || "4000");
      minWn = isNaN(minInp) ? 400 : minInp;
      maxWn = isNaN(maxInp) ? 4000 : maxInp;
      if (minWn >= maxWn) maxWn = minWn + 100;
    } else if (irViewportMode === "full" && (activeExps || []).length > 0) {
      const allMins = activeExps.map(e => Math.min(...e.raw_data.map(p => p.wavenumber_cm)));
      const allMaxs = activeExps.map(e => Math.max(...e.raw_data.map(p => p.wavenumber_cm)));
      minWn = Math.max(200, Math.floor(Math.min(...allMins, 400)));
      maxWn = Math.min(6000, Math.ceil(Math.max(...allMaxs, 4000)));
    }

    if (irManualRangeEnabled) {
      if (irManualMinX !== null && !isNaN(irManualMinX)) minWn = irManualMinX;
      if (irManualMaxX !== null && !isNaN(irManualMaxX)) maxWn = irManualMaxX;
      if (minWn >= maxWn) maxWn = minWn + 100;
    }
    return { minWn, maxWn };
  }

  function getIRPadding() {
    return { top: showIRLegend ? 58 : 35, right: 65, bottom: 48, left: 65 };
  }

  function renderSpectrumToCanvas(canvas, scale = 1, isExport = false) {
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const width = canvas.width / scale;
    const height = canvas.height / scale;

    ctx.save();
    ctx.scale(scale, scale);
    ctx.clearRect(0, 0, width, height);

    const isLight = currentSpectrumTheme === "light";
    const canvasWrap = document.getElementById("uvvis-canvas-wrap");
    if (canvasWrap && !isExport) {
      canvasWrap.classList.toggle("light-theme", isLight);
    }

    const sigma = parseFloat(document.getElementById("engine-sigma-slider")?.value || "20");
    const shift = parseFloat(document.getElementById("engine-shift-slider")?.value || "0");

    // Gather active theoretical and experimental series
    const activeTheos = loadedTheoreticalSpectra.filter(t => t.visible && uvViewAllows("theo"));
    const activeExps = loadedExperimentalSpectra.filter(e => e.visible && uvViewAllows("exp"));

    if (activeTheos.length === 0 && activeExps.length === 0) {
      ctx.fillStyle = isLight ? "#475569" : "#91A5BE";
      ctx.font = "14px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No active spectra selected for overlay. Enable datasets in the tray above.", width / 2, height / 2);
      ctx.restore();
      return;
    }

    // 1. Determine X-Axis Bounds (Viewport & Manual Range Cropping)
    const { minX, maxX } = getUVVisBounds(activeTheos, activeExps);

    // 2. Convolute active theoretical spectra strictly without touching experimental data
    const theoCurves = activeTheos.map(theo => {
      const curve = computeUvvisConvolution(theo.transitions, sigma, shift, minX, maxX, 1);
      let maxI = 0;
      curve.forEach(pt => { if (pt.intensity > maxI) maxI = pt.intensity; });
      return { theo, curve, maxIntensity: maxI || 1.0 };
    });

    // 3. Evaluate Experimental maximums
    const expCurves = activeExps.map(exp => {
      let maxA = 0;
      exp.raw_data.forEach(pt => { if (pt.absorbance > maxA) maxA = pt.absorbance; });
      return { exp, maxAbsorbance: maxA || 1.0 };
    });

    // 4. Calculate Max Y for Theo and Exp
    let minY = 0.0;
    let maxY = 1.0;
    let maxTheoY = Math.max(...theoCurves.map(tc => tc.maxIntensity), 0.001) * 1.15;
    let maxExpY = Math.max(...expCurves.map(ec => ec.maxAbsorbance), 0.001) * 1.15;

    if (spectrumNormalizeMode === "all") {
      maxTheoY = 1.15;
      maxExpY = 1.15;
    } else if (spectrumNormalizeMode === "theoretical_only") {
      maxTheoY = 1.15;
    } else if (spectrumNormalizeMode === "experimental_only") {
      maxExpY = 1.15;
    }

    if (uvvisManualRangeEnabled) {
      if (uvvisManualMinY !== null && !isNaN(uvvisManualMinY)) {
        minY = uvvisManualMinY;
      }
      if (uvvisManualMaxY !== null && !isNaN(uvvisManualMaxY) && uvvisManualMaxY > minY) {
        maxTheoY = uvvisManualMaxY;
        maxExpY = uvvisManualMaxY;
      }
    }

    // Geometrically stable padding - invariant whether 0, 1, or 10 files loaded
    const padding = { top: showUVVisLegend ? 58 : 35, right: 65, bottom: 48, left: 65 };
    const plotW = width - padding.left - padding.right;
    const plotH = height - padding.top - padding.bottom;

    const mapX = (x) => padding.left + ((x - minX) / (maxX - minX)) * plotW;
    const mapYTheo = (y) => padding.top + plotH - ((y - minY) / (maxTheoY - minY)) * plotH;
    const mapYExp = (y) => padding.top + plotH - ((y - minY) / (maxExpY - minY)) * plotH;

    // Canvas Background
    if (isLight) {
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, width, height);
    } else if (isExport) {
      ctx.fillStyle = "#060B12";
      ctx.fillRect(0, 0, width, height);
    }

    // Grid Lines (Subtle)
    ctx.strokeStyle = isLight ? "rgba(0, 0, 0, 0.06)" : "rgba(210, 227, 245, 0.06)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let yStep = 1; yStep <= 4; yStep++) {
      const yTheoVal = minY + ((maxTheoY - minY) / 4) * yStep;
      const yPos = mapYTheo(yTheoVal);
      ctx.moveTo(padding.left, yPos);
      ctx.lineTo(width - padding.right, yPos);
    }
    ctx.stroke();

    // 5. Draw Coordinate Axes Box & Ticks
    const axisColor = isLight ? "#000000" : "#CBD5E1";
    ctx.strokeStyle = axisColor;
    ctx.lineWidth = 1.5;
    ctx.strokeRect(padding.left, padding.top, plotW, plotH);

    // Major Axis Ticks
    const xSpan = maxX - minX;
    const xStep = xSpan > 600 ? 100 : (xSpan > 250 ? 50 : 25);

    ctx.strokeStyle = axisColor;
    ctx.lineWidth = 1.2;
    ctx.beginPath();

    // X Ticks on Bottom and Top
    for (let xVal = Math.ceil(minX / xStep) * xStep; xVal <= maxX; xVal += xStep) {
      const xPos = mapX(xVal);
      if (xPos >= padding.left && xPos <= width - padding.right) {
        ctx.moveTo(xPos, padding.top + plotH);
        ctx.lineTo(xPos, padding.top + plotH + 5);
        ctx.moveTo(xPos, padding.top);
        ctx.lineTo(xPos, padding.top - 4);
      }
    }

    // Y Ticks on Left and Right
    for (let yStep = 0; yStep <= 4; yStep++) {
      const yTheoVal = minY + ((maxTheoY - minY) / 4) * yStep;
      const yPos = mapYTheo(yTheoVal);
      ctx.moveTo(padding.left - 5, yPos);
      ctx.lineTo(padding.left, yPos);
      ctx.moveTo(width - padding.right, yPos);
      ctx.lineTo(width - padding.right + 5, yPos);
    }
    ctx.stroke();

    // X-Axis Wavelength Numbers
    ctx.fillStyle = axisColor;
    ctx.font = "11px 'JetBrains Mono', monospace";
    ctx.textAlign = "center";
    for (let xVal = Math.ceil(minX / xStep) * xStep; xVal <= maxX; xVal += xStep) {
      const xPos = mapX(xVal);
      if (xPos >= padding.left && xPos <= width - padding.right) {
        ctx.fillText(`${xVal}`, xPos, height - padding.bottom + 18);
      }
    }

    // Left Y-Axis Numbers (Theoretical / Intensity)
    ctx.textAlign = "right";
    ctx.fillStyle = isLight ? "#4338ca" : "#818cf8";
    for (let yStep = 0; yStep <= 4; yStep++) {
      const yVal = minY + ((maxTheoY - minY) / 4) * yStep;
      const displayVal = spectrumNormalizeMode in { "all": 1, "theoretical_only": 1 } ? (yVal / maxTheoY * 1.15).toFixed(2) : yVal.toFixed(1);
      ctx.fillText(displayVal, padding.left - 8, mapYTheo(yVal) + 4);
    }

    // Right Y-Axis Numbers (Experimental / Absorbance)
    if (activeExps.length > 0) {
      ctx.textAlign = "left";
      ctx.fillStyle = isLight ? "#059669" : "#2BD9A8";
      for (let yStep = 0; yStep <= 4; yStep++) {
        const yVal = minY + ((maxExpY - minY) / 4) * yStep;
        const displayVal = spectrumNormalizeMode in { "all": 1, "experimental_only": 1 } ? (yVal / maxExpY * 1.15).toFixed(2) : yVal.toFixed(2);
        ctx.fillText(displayVal, width - padding.right + 8, mapYExp(yVal) + 4);
      }
    }

    // Axis Titles
    ctx.fillStyle = axisColor;
    ctx.font = "bold 12px Inter, -apple-system, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Wavelength λ (nm)", padding.left + plotW / 2, height - 12);

    ctx.save();
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "center";
    ctx.fillStyle = isLight ? "#4338ca" : "#818cf8";
    ctx.fillText(uvCustomTitles.y || (spectrumNormalizeMode in { "all": 1, "theoretical_only": 1 } ? "Theoretical Intensity (Norm)" : "Molar Extinction ε (L·mol⁻¹·cm⁻¹)"), -(padding.top + plotH / 2), 16);
    if (activeExps.length > 0) {
      ctx.fillStyle = isLight ? "#059669" : "#2BD9A8";
      ctx.fillText(spectrumNormalizeMode in { "all": 1, "experimental_only": 1 } ? "Exp Absorbance (Norm)" : "Experimental Absorbance (AU)", -(padding.top + plotH / 2), width - 12);
    }
    ctx.restore();
    ctx.font = "bold 11px Inter, sans-serif";
    ctx.fillStyle = isLight ? "#0f172a" : "#CBD5E1";
    ctx.textAlign = "center";
    ctx.fillText(uvCustomTitles.x || "Wavelength (nm)", padding.left + plotW / 2, padding.top + plotH + 18);
    ctx.font = "bold 13px Inter, sans-serif";
    ctx.fillStyle = isLight ? "#0f172a" : "#e2e8f0";
    ctx.fillText(uvCustomTitles.title || "UV-Vis Spectrum", width / 2, 14);

    // =========================================================================
    // STRICT VIEWPORT CLIPPING: Ensure all data curves remain inside the plot box
    // =========================================================================
    ctx.save();
    ctx.beginPath();
    ctx.rect(padding.left, padding.top, plotW, plotH);
    ctx.clip();

    // 6. Draw Theoretical Stick Transitions (if enabled)
    if (showTheoreticalSticks) {
      theoCurves.forEach(({ theo }) => {
        let maxFosc = 0;
        theo.transitions.forEach(t => { if (t.oscillator_strength > maxFosc) maxFosc = t.oscillator_strength; });
        if (maxFosc === 0) maxFosc = 1.0;

        ctx.lineWidth = 1.5;
        ctx.strokeStyle = isLight ? "rgba(99, 102, 241, 0.45)" : "rgba(129, 140, 248, 0.45)";
        theo.transitions.forEach(t => {
          const rawWl = t.wavelength_nm || (1e7 / t.energy_cm);
          const effWl = rawWl + shift;
          if (effWl >= minX && effWl <= maxX) {
            const xPos = mapX(effWl);
            const barHeight = (t.oscillator_strength / maxFosc) * (plotH * 0.75);
            const yPos = padding.top + plotH - barHeight;

            ctx.beginPath();
            ctx.moveTo(xPos, mapYTheo(0));
            ctx.lineTo(xPos, yPos);
            ctx.stroke();

            // Top stick marker
            ctx.fillStyle = theo.color || (isLight ? "#4f46e5" : "#818cf8");
            ctx.beginPath();
            ctx.arc(xPos, yPos, 2.5, 0, 2 * Math.PI);
            ctx.fill();
          }
        });
      });
    }

    // 7. Draw Theoretical Gaussian Convoluted Curves
    theoCurves.forEach(({ theo, curve, maxIntensity }) => {
      if (!curve.length) return;
      const normDiv = (spectrumNormalizeMode in { "all": 1, "theoretical_only": 1 }) ? maxIntensity : 1.0;

      // Area fill gradient
      const grad = ctx.createLinearGradient(0, padding.top, 0, padding.top + plotH);
      grad.addColorStop(0, `${theo.color}33`);
      grad.addColorStop(1, `${theo.color}00`);

      ctx.beginPath();
      ctx.moveTo(mapX(curve[0].wavelength_nm), mapYTheo(curve[0].intensity / normDiv));
      curve.forEach(pt => {
        ctx.lineTo(mapX(pt.wavelength_nm), mapYTheo(pt.intensity / normDiv));
      });
      ctx.lineTo(mapX(curve[curve.length - 1].wavelength_nm), mapYTheo(0));
      ctx.lineTo(mapX(curve[0].wavelength_nm), mapYTheo(0));
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      // Line outline
      ctx.beginPath();
      ctx.moveTo(mapX(curve[0].wavelength_nm), mapYTheo(curve[0].intensity / normDiv));
      curve.forEach(pt => {
        ctx.lineTo(mapX(pt.wavelength_nm), mapYTheo(pt.intensity / normDiv));
      });
      ctx.strokeStyle = theo.color;
      ctx.lineWidth = 2.0;
      ctx.stroke();
    });

    // 8. Draw Experimental Curves (IMMUTABLE RAW MEASUREMENTS)
    expCurves.forEach(({ exp, maxAbsorbance }) => {
      if (!exp.raw_data || !exp.raw_data.length) return;
      const normDiv = (spectrumNormalizeMode in { "all": 1, "experimental_only": 1 }) ? maxAbsorbance : 1.0;

      const pts = exp.raw_data;
      if (!pts.length) return;

      // Connect raw experimental points
      ctx.beginPath();
      let started = false;
      pts.forEach(pt => {
        if (typeof pt.wavelength_nm === 'number' && !isNaN(pt.wavelength_nm) && typeof pt.absorbance === 'number' && !isNaN(pt.absorbance)) {
          const xPos = mapX(pt.wavelength_nm);
          const yPos = mapYExp(pt.absorbance / normDiv);
          if (!started) {
            ctx.moveTo(xPos, yPos);
            started = true;
          } else {
            ctx.lineTo(xPos, yPos);
          }
        }
      });
      ctx.strokeStyle = exp.color;
      ctx.lineWidth = 2.4;
      ctx.stroke();

      // Point dots for discrete measurements (if <= 120 points)
      if (pts.length <= 120) {
        ctx.fillStyle = exp.color;
        pts.forEach(pt => {
          if (typeof pt.wavelength_nm === 'number' && typeof pt.absorbance === 'number') {
            ctx.beginPath();
            ctx.arc(mapX(pt.wavelength_nm), mapYExp(pt.absorbance / normDiv), 2.2, 0, 2 * Math.PI);
            ctx.fill();
          }
        });
      }

      // Experimental Peak Markers (if enabled)
      if (showExperimentalPeaks && exp.peaks && exp.peaks.length > 0) {
        exp.peaks.forEach(pk => {
          if (typeof pk.wavelength_nm === 'number' && typeof pk.absorbance === 'number') {
            const xPos = mapX(pk.wavelength_nm);
            const yPos = mapYExp(pk.absorbance / normDiv);

            // Draw diamond peak marker
            const size = 5;
            ctx.fillStyle = exp.color;
            ctx.strokeStyle = isLight ? "#ffffff" : "#060B12";
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(xPos, yPos - size);
            ctx.lineTo(xPos + size, yPos);
            ctx.lineTo(xPos, yPos + size);
            ctx.lineTo(xPos - size, yPos);
            ctx.closePath();
            ctx.fill();
            ctx.stroke();

            // Label
            ctx.font = "10px 'JetBrains Mono', monospace";
            ctx.textAlign = "center";
            ctx.fillStyle = axisColor;
            ctx.fillText(`${pk.wavelength_nm.toFixed(0)} nm`, xPos, yPos - 8);
          }
        });
      }
    });

    // Remove viewport clipping before drawing legend
    ctx.restore();

    // 9. Dedicated Top-Margin Legend (Guaranteed zero overlap with plot area, matching swatch and text colors)
    if (showUVVisLegend) {
      let curX = padding.left + 5;
      let curY = 20;
      ctx.font = "bold 11px Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
      ctx.textAlign = "left";

      const legendItems = [
        ...theoCurves.map(tc => ({ name: `[THEORY] ${tc.theo.name}`, color: tc.theo.color })),
        ...expCurves.map(ec => ({ name: `[EXP] ${ec.exp.file_name ? ec.exp.file_name + ': ' : ''}${ec.exp.label || ec.exp.column_absorbance}`, color: ec.exp.color }))
      ];

      legendItems.forEach(item => {
        const labelText = item.name.length > 28 ? item.name.slice(0, 26) + "…" : item.name;
        const textWidth = ctx.measureText(labelText).width;
        if (curX + textWidth + 28 > width - padding.right && curY === 20) {
          curX = padding.left + 5;
          curY = 38;
        }
        ctx.fillStyle = item.color;
        ctx.fillRect(curX, curY - 8, 12, 5);
        ctx.fillStyle = item.color;
        ctx.fillText(labelText, curX + 16, curY);
        curX += textWidth + 26;
      });
    }

    ctx.restore();
  }

  function drawUVVisMultiSpectrum() {
    const canvas = document.getElementById("engine-uvvis-canvas");
    if (!canvas) return;
    renderSpectrumToCanvas(canvas, 1, false);
  }


  function drawUVVisSpectrum(spectrum, transitions) {
    // Backward compatibility wrapper for single spectrum call
    if (transitions && transitions.length > 0) {
      const molName = currentEngineData?.name || "TD-DFT Calculation";
      addTheoreticalCalculationToStudio(molName, transitions);
    }
    drawUVVisMultiSpectrum();
  }

  let recalcTimeout = null;
  function triggerRecalculateSpectrum() {
    const sigma = parseFloat(document.getElementById("engine-sigma-slider")?.value || "20");
    const shift = parseFloat(document.getElementById("engine-shift-slider")?.value || "0");
    const sigmaLbl = document.getElementById("engine-sigma-label");
    const shiftLbl = document.getElementById("engine-shift-label");
    
    // Approximate eV width: delta E ≈ (1239.84 / lambda^2) * delta lambda ≈ 0.30 eV at 300 nm
    const approxEv = (sigma * 0.015).toFixed(2);
    if (sigmaLbl) sigmaLbl.textContent = `${sigma.toFixed(1)} nm (~${approxEv} eV)`;
    if (shiftLbl) shiftLbl.textContent = `${shift >= 0 ? '+' : ''}${shift.toFixed(1)} nm`;

    // Strict Invariance Guarantee:
    // Only theoretical models are refreshed. Experimental datasets remain untouched.
    drawUVVisMultiSpectrum();

    // Async sync with backend convolution endpoint
    const job = currentEngineData?.latest_job || {};
    const transitions = job.transitions || [];
    if (!transitions.length) return;

    clearTimeout(recalcTimeout);
    recalcTimeout = setTimeout(async () => {
      try {
        const res = await postJSON("/api/orca/engine/convolute", {
          transitions,
          tddft_cm: transitions.map(t => t.energy_cm),
          tddft_fosc: transitions.map(t => t.oscillator_strength),
          sigma_nm: sigma,
          wavelength_shift_nm: shift,
          shift_nm: shift,
        });
        if (res.ok && res.spectrum) {
          currentConvolutedSpectrum = res.spectrum;
        }
      } catch (e) {
        console.warn("Spectrum server convolution:", e);
      }
    }, 150);
  }

  // =========================================================================
  // Vibrational IR / FTIR Multi-Spectrum Analysis Studio & Experimental Overlay
  // =========================================================================
  const EXP_IR_PALETTE = ["#06b6d4", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6", "#3b82f6", "#14b8a6", "#f97316", "#a855f7", "#0ea5e9", "#84cc16", "#e11d48"];
  const THEO_IR_PALETTE = ["#FF5252", "#FF7043", "#FFA726", "#AB47BC", "#42A5F5", "#26A69A"];

  let loadedExperimentalIRFiles = [];
  let loadedExperimentalIRSpectra = [];
  let loadedTheoreticalIRSpectra = [];
  let irViewMode = "all";
  let uvViewMode = "all";
  let irCustomTitles = { title: "", x: "", y: "" };
  let uvCustomTitles = { title: "", x: "", y: "" };
  function irViewAllows(kind) {
    if (irViewMode === "theoretical_only") return kind === "theo";
    if (irViewMode === "experimental_only") return kind === "exp";
    return true;
  }
  function uvViewAllows(kind) {
    if (uvViewMode === "theoretical_only") return kind === "theo";
    if (uvViewMode === "experimental_only") return kind === "exp";
    return true;
  }
  function sanitizeFileStem(name) {
    return (String(name || "spectrum").split(/[\\/]/).pop() || "spectrum")
      .replace(/[\\/:*?"<>|]+/g, "_").replace(/\s+/g, "_")
      .replace(/^\.+/, "").slice(0, 80) || "spectrum";
  }
  function downloadTextFile(filename, text) {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function buildXYText(columns, metaLines) {
    const parts = [];
    (metaLines || []).forEach(m => parts.push("# " + m));
    const fmt = (v) => Number(v).toFixed(6);
    if (columns.length === 1) {
      const c = columns[0];
      parts.push(c.xHeader + "\t" + c.yHeader);
      const n = Math.min(c.xs.length, c.ys.length);
      for (let i = 0; i < n; i++) parts.push(fmt(c.xs[i]) + "\t" + fmt(c.ys[i]));
      return parts.join("\n") + "\n";
    }
    const x0 = columns[0];
    const sameGrid = columns.every(c => c.xs.length === x0.xs.length
      && c.xs.every((x, i) => Math.abs(Number(x) - Number(x0.xs[i])) < 1e-9));
    if (sameGrid) {
      parts.push([x0.xHeader].concat(columns.map(c => c.yHeader)).join("\t"));
      const n = x0.xs.length;
      for (let i = 0; i < n; i++) {
        parts.push([fmt(x0.xs[i])].concat(columns.map(c => fmt(c.ys[i]))).join("\t"));
      }
      return parts.join("\n") + "\n";
    }
    const headers = [];
    columns.forEach(c => { headers.push(c.xHeader, c.yHeader); });
    parts.push(headers.join("\t"));
    const n = Math.max(...columns.map(c => Math.min(c.xs.length, c.ys.length)));
    for (let i = 0; i < n; i++) {
      const row = [];
      columns.forEach(c => {
        row.push(i < c.xs.length ? fmt(c.xs[i]) : "");
        row.push(i < c.ys.length ? fmt(c.ys[i]) : "");
      });
      parts.push(row.join("\t"));
    }
    return parts.join("\n") + "\n";
  }
  let showTheoreticalIRSticks = true;
  let showIRLegend = true;
  let currentIRTheme = "dark";
  let irYAxisMode = "transmittance";
  let irViewportMode = "standard";
  let irManualRangeEnabled = false;
  let irManualMinX = null;
  let irManualMaxX = null;
  let irManualMinY = null;
  let irManualMaxY = null;
  let activeIRCandidateYHeaders = [];
  let activeExpIRFileObj = null;
  let cachedExcelIRSheets = [];

  function checkIRYQuantityCompatibility() {
    const activeExps = loadedExperimentalIRSpectra.filter(e => e.visible && irViewAllows("exp"));
    const quantities = new Set(activeExps.map(e => e.y_quantity || "transmittance"));
    const mismatchBanner = document.getElementById("ir-unit-mismatch-banner");
    if (quantities.size > 1 && mismatchBanner) {
      mismatchBanner.classList.remove("hidden");
      mismatchBanner.style.display = "block";
    } else if (mismatchBanner) {
      mismatchBanner.classList.add("hidden");
      mismatchBanner.style.display = "none";
    }
  }

  function syncIRFlattenedSpectra() {
    const active = [];
    loadedExperimentalIRFiles.forEach(file => {
      file.series_list.forEach(series => {
        if (file.selected_y_cols.includes(series.label || series.column_signal || series.column_absorbance)) {
          active.push(series);
        }
      });
    });
    loadedExperimentalIRSpectra = active;
    updateIRSpectrumLayersTray();
    checkIRYQuantityCompatibility();
    if (active.length > 0) {
      populateExperimentalIRPointsTable(active[0]);
    } else {
      clearExperimentalIRPointsTable();
    }
  }

  function addTheoreticalIRCalculationToStudio(name, irModes, method = "ORCA DFT", basis = "", extra = {}) {
    if (!irModes || !irModes.length) return;
    const id = `theo_ir_${Date.now()}_${Math.random().toString(36).substr(2, 5)}`;
    const color = THEO_IR_PALETTE[loadedTheoreticalIRSpectra.length % THEO_IR_PALETTE.length];

    const normalizedModes = irModes.map((m, idx) => {
      if (typeof m === "number") {
        return { mode: idx + 1, frequency_cm: m, intensity_km_mol: 10.0 };
      }
      return {
        mode: m.mode || (idx + 1),
        frequency_cm: m.frequency_cm || m.wavenumber_cm || m.freq || 0,
        intensity_km_mol: (m.intensity_km_mol != null) ? m.intensity_km_mol : ((m.intensity != null) ? m.intensity : 10.0),
      };
    }).filter(m => m.frequency_cm > 0);

    if (!normalizedModes.length) return;

    const existingIdx = loadedTheoreticalIRSpectra.findIndex(t => t.name === name);
    const item = {
      id,
      name: name || "Theoretical IR",
      method,
      basis,
      modes: normalizedModes,
      color,
      visible: true,
      imported: false,
      imaginary_count: 0,
      ...extra,
    };
    if (existingIdx >= 0) {
      loadedTheoreticalIRSpectra[existingIdx] = item;
    } else {
      loadedTheoreticalIRSpectra.push(item);
    }
    updateIRSpectrumLayersTray();
  }

  function addExperimentalIRSpectrumToStudio(expData) {
    if (!expData || !expData.points || !expData.points.length) return;
    const id = `exp_ir_${Date.now()}_${Math.random().toString(36).substr(2, 5)}`;
    const fileName = expData.file_name || "experimental_ir.txt";
    const seriesLabel = expData.column_absorbance || expData.column_transmittance || expData.column_signal || "Signal";
    const color = getOrAssignSeriesColor(fileName, seriesLabel, loadedExperimentalIRSpectra.length, true);

    const yVals = expData.points.map(p => p.absorbance != null ? p.absorbance : (p.transmittance != null ? p.transmittance : p.intensity));
    const maxVal = Math.max(...yVals);
    const isTransmittance = maxVal > 10.0 || (expData.units_y && expData.units_y.includes("%")) || /trans/i.test(seriesLabel);

    const raw_data = expData.points.map(p => {
      const wn = p.wavelength_nm != null ? p.wavelength_nm : (p.wavenumber_cm != null ? p.wavenumber_cm : (p.x != null ? p.x : 0));
      const val = p.absorbance != null ? p.absorbance : (p.transmittance != null ? p.transmittance : (p.y != null ? p.y : 0));
      let tPct, abs;
      if (isTransmittance) {
        tPct = val;
        abs = tPct > 0 ? -Math.log10(Math.max(0.0001, tPct / 100)) : 3.0;
      } else {
        abs = val;
        tPct = 100.0 * Math.pow(10, -Math.max(0, abs));
      }
      return {
        wavenumber_cm: wn,
        absorbance: abs,
        transmittance_pct: tPct
      };
    }).filter(p => p.wavenumber_cm > 0).sort((a, b) => a.wavenumber_cm - b.wavenumber_cm);

    const item = {
      id,
      file_name: fileName,
      sheet_name: expData.sheet_name,
      label: seriesLabel,
      column_wavenumber: expData.column_wavelength || expData.column_wavenumber || "Wavenumber",
      column_signal: seriesLabel,
      column_absorbance: seriesLabel,
      y_quantity: isTransmittance ? "transmittance" : "absorbance",
      raw_hash: expData.raw_hash || "hash",
      points_count: raw_data.length,
      raw_data,
      color,
      color_manually_set: userAssignedSeriesColors.has(`${fileName}::${seriesLabel}`),
      visible: true,
    };

    let fileRec = loadedExperimentalIRFiles.find(f => f.file_name === fileName && f.sheet_name === expData.sheet_name);
    if (!fileRec) {
      fileRec = {
        file_id: `ir_file_${Date.now()}_${Math.random().toString(36).substr(2, 5)}`,
        file_name: fileName,
        format: /\.(xlsx|xlsm|xltx)$/i.test(fileName) ? "excel" : "csv",
        sheet_name: expData.sheet_name,
        wavenumber_col: expData.column_wavelength || expData.column_wavenumber || "Wavenumber",
        available_y_cols: [seriesLabel],
        selected_y_cols: [seriesLabel],
        series_list: [item]
      };
      loadedExperimentalIRFiles.push(fileRec);
    } else {
      if (!fileRec.available_y_cols.includes(seriesLabel)) {
        fileRec.available_y_cols.push(seriesLabel);
      }
      if (!fileRec.selected_y_cols.includes(seriesLabel)) {
        fileRec.selected_y_cols.push(seriesLabel);
      }
      const existingSeriesIdx = fileRec.series_list.findIndex(s => s.label === seriesLabel);
      if (existingSeriesIdx >= 0) {
        fileRec.series_list[existingSeriesIdx] = item;
      } else {
        fileRec.series_list.push(item);
      }
    }

    syncIRFlattenedSpectra();
    drawIRMultiSpectrum();
  }

  function updateIRSpectrumLayersTray() {
    const trayEl = document.getElementById("ir-layers-tray");
    const countEl = document.getElementById("ir-layers-count");
    const listEl = document.getElementById("ir-layers-list");
    if (!trayEl || !listEl) return;

    const totalCount = loadedTheoreticalIRSpectra.length + loadedExperimentalIRSpectra.length;
    if (countEl) countEl.textContent = totalCount;

    listEl.innerHTML = "";
    if (totalCount === 0) {
      listEl.innerHTML = `<span class="field-hint" style="padding:0.5rem;">No active IR layers. Upload an experimental FTIR file or analyze an ORCA output.</span>`;
      return;
    }

    loadedTheoreticalIRSpectra.forEach((theo, idx) => {
      const card = document.createElement("div");
      card.className = `spectrum-layer-card theo-layer ${theo.visible ? 'active' : 'disabled'}`;
      const badge = theo.imported
        ? '<span class="layer-type-tag tag-imported" title="Imported ORCA FREQ output">Imported</span>'
        : '<span class="layer-type-tag">Current</span>';
      const actions = `
            <button type="button" class="btn-layer-del" data-xy-ir-theo="${idx}" title="Export XY (Origin-friendly text)">&#8681;</button>` +
        (theo.imported ? `
            <button type="button" class="btn-layer-del" data-rename-ir-theo="${idx}" title="Rename spectrum (display name only)">&#9998;</button>
            <button type="button" class="btn-layer-del" data-del-ir-theo="${idx}" title="Remove imported spectrum">&#10005;</button>` : "");
      const imagNote = (theo.imaginary_count > 0)
        ? `<span style="color:var(--accent-danger);">&#9888; ${theo.imaginary_count} imaginary</span>` : "";
      card.innerHTML = `
        <div class="layer-header-row">
          <label class="layer-title-label">
            <input type="checkbox" class="layer-toggle-cb" data-ir-type="theo" data-ir-index="${idx}" ${theo.visible ? 'checked' : ''}>
            <input type="color" class="layer-color-picker" data-ir-type="theo" data-ir-index="${idx}" value="${theo.color}" title="Change IR curve color">
            <strong>${escapeHtml(theo.name)}</strong>
          </label>
          <div class="layer-actions">
            ${badge}
            ${actions}
          </div>
        </div>
        <div class="layer-meta-row">
          <span>${escapeHtml(theo.method || "ORCA DFT")}</span>
          <span>${theo.modes.length} Modes</span>
          ${imagNote}
        </div>
      `;
      listEl.appendChild(card);
    });

    loadedExperimentalIRSpectra.forEach((exp, idx) => {
      const card = document.createElement("div");
      card.className = `spectrum-layer-card exp-layer ${exp.visible ? 'active' : 'disabled'}`;
      card.innerHTML = `
        <div class="layer-header-row">
          <label class="layer-title-label">
            <input type="checkbox" class="layer-toggle-cb" data-ir-type="exp" data-ir-index="${idx}" ${exp.visible ? 'checked' : ''}>
            <input type="color" class="layer-color-picker" data-ir-type="exp" data-ir-index="${idx}" value="${exp.color}" title="Change FTIR curve color">
            <strong>${exp.file_name}${exp.label ? ' : ' + exp.label : ''}</strong>
          </label>
          <div class="layer-actions">
            <span class="layer-type-tag tag-exp">FTIR Ref</span>
            <button type="button" class="btn-layer-del" data-xy-ir-exp="${idx}" title="Export XY (Origin-friendly text)">&#8681;</button>
            <button type="button" class="btn-layer-del" data-rename-ir-exp="${idx}" title="Rename spectrum (display name only)">&#9998;</button>
            <button type="button" class="btn-layer-del" data-del-ir-exp="${idx}" title="Remove dataset">✕</button>
          </div>
        </div>
        <div class="layer-meta-row">
          <span>${exp.points_count} Points</span>
          ${exp.sheet_name ? `<span>Sheet: ${exp.sheet_name}</span>` : ''}
        </div>
      `;
      listEl.appendChild(card);
    });

    listEl.querySelectorAll(".layer-toggle-cb").forEach(cb => {
      cb.addEventListener("change", (e) => {
        const type = cb.dataset.irType;
        const idx = parseInt(cb.dataset.irIndex, 10);
        if (type === "theo" && loadedTheoreticalIRSpectra[idx]) {
          loadedTheoreticalIRSpectra[idx].visible = cb.checked;
        } else if (type === "exp" && loadedExperimentalIRSpectra[idx]) {
          loadedExperimentalIRSpectra[idx].visible = cb.checked;
        }
        checkIRYQuantityCompatibility();
        drawIRMultiSpectrum();
      });
    });

    listEl.querySelectorAll(".layer-color-picker").forEach(cp => {
      const handleColor = (e) => {
        const type = cp.dataset.irType;
        const idx = parseInt(cp.dataset.irIndex, 10);
        if (type === "theo" && loadedTheoreticalIRSpectra[idx]) {
          loadedTheoreticalIRSpectra[idx].color = e.target.value;
        } else if (type === "exp" && loadedExperimentalIRSpectra[idx]) {
          const exp = loadedExperimentalIRSpectra[idx];
          exp.color = e.target.value;
          exp.color_manually_set = true;
          userAssignedSeriesColors.set(`${exp.file_name}::${exp.label}`, e.target.value);
          // Sync with multi-file container color pickers
          document.querySelectorAll(`.ir-series-color-input[data-series-label="${exp.label}"]`).forEach(inp => {
            inp.value = e.target.value;
          });
        }
        drawIRMultiSpectrum();
      };
      cp.addEventListener("input", handleColor);
      cp.addEventListener("change", handleColor);
    });

    listEl.querySelectorAll("[data-del-ir-exp]").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.delIrExp, 10);
        if (loadedExperimentalIRSpectra[idx]) {
          loadedExperimentalIRSpectra.splice(idx, 1);
          updateIRSpectrumLayersTray();
          drawIRMultiSpectrum();
          if (!loadedExperimentalIRSpectra.length) {
            clearExperimentalIRPointsTable();
          }
        }
      });
    });

    listEl.querySelectorAll("[data-xy-ir-theo]").forEach(btn => {
      btn.addEventListener("click", () => exportIRXYSingle("theo", parseInt(btn.dataset.xyIrTheo, 10)));
    });
    listEl.querySelectorAll("[data-xy-ir-exp]").forEach(btn => {
      btn.addEventListener("click", () => exportIRXYSingle("exp", parseInt(btn.dataset.xyIrExp, 10)));
    });
    listEl.querySelectorAll("[data-rename-ir-exp]").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.renameIrExp, 10);
        const entry = loadedExperimentalIRSpectra[idx];
        if (entry) {
          const next = (window.prompt("Rename spectrum (display name only - source file is untouched):", entry.label || entry.file_name) || "").trim();
          if (next) {
            entry.label = next;
            updateIRSpectrumLayersTray();
            drawIRMultiSpectrum();
          }
        }
      });
    });

    listEl.querySelectorAll("[data-del-ir-theo]").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.delIrTheo, 10);
        if (loadedTheoreticalIRSpectra[idx] && loadedTheoreticalIRSpectra[idx].imported) {
          loadedTheoreticalIRSpectra.splice(idx, 1);
          updateIRSpectrumLayersTray();
          drawIRMultiSpectrum();
        }
      });
    });

    listEl.querySelectorAll("[data-rename-ir-theo]").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.renameIrTheo, 10);
        const entry = loadedTheoreticalIRSpectra[idx];
        if (entry) {
          const next = (window.prompt("Rename spectrum (display name only - source file and data are untouched):", entry.name) || "").trim();
          if (next) {
            entry.name = next;
            updateIRSpectrumLayersTray();
            drawIRMultiSpectrum();
          }
        }
      });
    });
  }

  function populateExperimentalIRPointsTable(exp) {
    const tbody = document.getElementById("engine-ir-exp-tbody");
    if (!tbody || !exp || !exp.raw_data) return;
    tbody.innerHTML = "";
    const pts = exp.raw_data.slice(0, 400);
    pts.forEach((p, idx) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td style="font-family:var(--font-mono); font-size:0.8rem;">#${idx + 1}</td>
        <td style="font-family:var(--font-mono); font-size:0.8rem;">${p.wavenumber_cm.toFixed(1)} cm⁻¹</td>
        <td style="font-family:var(--font-mono); font-size:0.8rem; color:var(--accent);">
          ${irYAxisMode === 'transmittance' ? `${p.transmittance_pct.toFixed(2)} %T` : `${p.absorbance.toFixed(4)} AU`}
        </td>
        <td><span class="field-hint">Measured</span></td>
      `;
      tbody.appendChild(tr);
    });
  }

  function clearExperimentalIRPointsTable() {
    const tbody = document.getElementById("engine-ir-exp-tbody");
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No experimental IR spectrum loaded. Click "+ Upload Experimental IR" above to add reference data.</td></tr>`;
    }
  }

  function computeIRConvolution(modes, scalingFactor = 1.0, fwhmCm = 15.0, shiftCm = 0.0, startCm = 400, endCm = 4000, stepCm = 2, normMax = null) {
    if (!modes || !modes.length || fwhmCm <= 0) return [];
    const valid = [];
    modes.forEach(m => {
      const freq = typeof m === "number" ? m : (m.frequency_cm || m.freq || 0);
      const intensity = (typeof m === "object" && m.intensity_km_mol != null) ? m.intensity_km_mol : 10.0;
      if (freq > 0) {
        valid.push({ wn: (freq * scalingFactor) + shiftCm, intensity: Math.max(0, intensity) });
      }
    });
    if (!valid.length) return [];

    const gammaHalf = fwhmCm / 2.0;
    const gammaHalfSq = gammaHalf * gammaHalf;
    const points = [];
    let maxA = 0;

    for (let currentWn = startCm; currentWn <= endCm; currentWn += stepCm) {
      let absVal = 0.0;
      for (let i = 0; i < valid.length; i++) {
        const diff = currentWn - valid[i].wn;
        if (Math.abs(diff) <= 15.0 * fwhmCm) {
          absVal += (valid[i].intensity / Math.PI) * (gammaHalf / (diff * diff + gammaHalfSq));
        }
      }
      if (absVal > maxA) maxA = absVal;
      points.push({ wn: currentWn, abs: absVal });
    }

    return points.map(pt => {
      const denom = (normMax != null && normMax > 0) ? normMax : maxA;
      const aNorm = denom > 0 ? (pt.abs / denom) : 0;
      const tPct = 100.0 * Math.pow(10, -aNorm);
      return {
        wavenumber_cm: Math.round(pt.wn * 10) / 10,
        absorbance: pt.abs,
        absorbance_norm: aNorm,
        transmittance_pct: tPct
      };
    });
  }

  function getIRSharedNormMax(scaleFactor, fwhm, shift, minWn, maxWn) {
    if ((document.getElementById("ir-normalization-mode")?.value || "per_spectrum") !== "shared") return null;
    let globalMaxAbs = 0;
    loadedTheoreticalIRSpectra.filter(t => t.visible && irViewAllows("theo")).forEach(theo => {
      const c = computeIRConvolution(theo.modes, scaleFactor, fwhm, shift, minWn, maxWn, 2);
      c.forEach(p2 => { if (p2.absorbance > globalMaxAbs) globalMaxAbs = p2.absorbance; });
    });
    return globalMaxAbs > 0 ? globalMaxAbs : null;
  }

  function irDisplayedHeaders() {
    if (irYAxisMode === "theory_intensity") {
      return { yHeader: "Relative_Intensity", yUnit: "km/mol" };
    }
    if (irYAxisMode === "absorbance") {
      return { yHeader: "Absorbance", yUnit: "AU" };
    }
    return { yHeader: "Relative_Transmittance_pct", yUnit: "%" };
  }

  function renderIRSpectrumToCanvas(canvas, scale = 1, isExport = false) {
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const width = canvas.width / scale;
    const height = canvas.height / scale;

    ctx.save();
    ctx.scale(scale, scale);
    ctx.clearRect(0, 0, width, height);

    const isLight = currentIRTheme === "light";
    const canvasWrap = document.getElementById("ir-canvas-wrap");
    if (canvasWrap && !isExport) {
      canvasWrap.classList.toggle("light-theme", isLight);
    }

    const scaleFactor = parseFloat(document.getElementById("engine-ir-scale-slider")?.value || "1.0");
    const fwhm = parseFloat(document.getElementById("engine-ir-fwhm-slider")?.value || "15.0");
    const shift = parseFloat(document.getElementById("engine-ir-shift-slider")?.value || "0");

    const activeTheos = loadedTheoreticalIRSpectra.filter(t => t.visible && irViewAllows("theo"));
    const activeExps = loadedExperimentalIRSpectra.filter(e => e.visible && irViewAllows("exp"));

    if (activeTheos.length === 0 && activeExps.length === 0) {
      ctx.fillStyle = isLight ? "#475569" : "#91A5BE";
      ctx.font = "14px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No active IR spectra selected. Enable datasets in the tray above.", width / 2, height / 2);
      ctx.restore();
      return;
    }

    const { minWn, maxWn } = getIRBounds(activeTheos, activeExps);

    const sharedNormMax = getIRSharedNormMax(scaleFactor, fwhm, shift, minWn, maxWn);
    const theoCurves = activeTheos.map(theo => ({
      theo,
      curve: computeIRConvolution(theo.modes, scaleFactor, fwhm, shift, minWn, maxWn, 2, sharedNormMax),
    }));

    const isIntensity = (irYAxisMode === "theory_intensity");
    const isTrans = (irYAxisMode === "transmittance");
    const isAbs = (irYAxisMode === "absorbance");
    const isTheoOnly = (activeTheos.length > 0 && activeExps.length === 0);
    if (irViewMode === "experimental_only" && activeExps.length === 0) {
      ctx.font = "bold 13px Inter, sans-serif";
      ctx.fillStyle = axisColor;
      ctx.textAlign = "center";
      ctx.fillText("No experimental spectrum loaded. Import one to compare.", width / 2, height / 2);
    }

    const padding = getIRPadding();
    const plotW = width - padding.left - padding.right;
    const plotH = height - padding.top - padding.bottom;

    const mapX = (wn) => padding.left + ((maxWn - wn) / (maxWn - minWn)) * plotW;

    let minY = 0.0;
    let maxAbs = 1.0;
    const theoMaxAbs = Math.max(...theoCurves.flatMap(tc => tc.curve.map(p => p.absorbance)), 0.1);
    const expMaxAbs = Math.max(...activeExps.flatMap(e => e.raw_data.map(p => p.absorbance)), 0.1);
    maxAbs = Math.max(theoMaxAbs, expMaxAbs) * 1.15;

    let maxIntensity = 100.0;
    const allTheoModes = activeTheos.flatMap(t => (t.modes || (t.theo && t.theo.modes) || []));
    const maxTheoModeInt = allTheoModes.length > 0 ? Math.max(...allTheoModes.map(m => m.intensity_km_mol || 0), 10.0) : 100.0;
    maxIntensity = Math.ceil((maxTheoModeInt * 1.15) / 10) * 10 || 100.0;

    let maxY = isIntensity ? maxIntensity : (isTrans ? 100.0 : maxAbs);

    if (irManualRangeEnabled) {
      if (irManualMinY !== null && !isNaN(irManualMinY)) {
        minY = irManualMinY;
      }
      if (irManualMaxY !== null && !isNaN(irManualMaxY) && irManualMaxY > minY) {
        maxY = irManualMaxY;
        maxAbs = irManualMaxY;
        maxIntensity = irManualMaxY;
      }
    }

    const mapY = (val) => {
      return padding.top + plotH - ((val - minY) / (maxY - minY)) * plotH;
    };

    if (isLight) {
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, width, height);
    } else if (isExport) {
      ctx.fillStyle = "#060B12";
      ctx.fillRect(0, 0, width, height);
    }

    ctx.strokeStyle = isLight ? "rgba(0, 0, 0, 0.06)" : "rgba(210, 227, 245, 0.06)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let yStep = 1; yStep <= 4; yStep++) {
      const yVal = minY + ((maxY - minY) / 4) * yStep;
      const yPos = mapY(yVal);
      ctx.moveTo(padding.left, yPos);
      ctx.lineTo(width - padding.right, yPos);
    }
    ctx.stroke();

    const axisColor = isLight ? "#000000" : "#CBD5E1";
    ctx.strokeStyle = axisColor;
    ctx.lineWidth = 1.5;
    ctx.strokeRect(padding.left, padding.top, plotW, plotH);

    const wnSpan = maxWn - minWn;
    const wnStep = wnSpan > 2000 ? 500 : (wnSpan > 800 ? 200 : 100);

    ctx.strokeStyle = axisColor;
    ctx.lineWidth = 1.2;
    ctx.beginPath();

    for (let wnVal = Math.floor(maxWn / wnStep) * wnStep; wnVal >= minWn; wnVal -= wnStep) {
      const xPos = mapX(wnVal);
      if (xPos >= padding.left && xPos <= width - padding.right) {
        ctx.moveTo(xPos, padding.top + plotH);
        ctx.lineTo(xPos, padding.top + plotH + 5);
        ctx.moveTo(xPos, padding.top);
        ctx.lineTo(xPos, padding.top - 4);
      }
    }

    for (let yStep = 0; yStep <= 4; yStep++) {
      const yVal = minY + ((maxY - minY) / 4) * yStep;
      const yPos = mapY(yVal);
      ctx.moveTo(padding.left - 5, yPos);
      ctx.lineTo(padding.left, yPos);
      ctx.moveTo(width - padding.right, yPos);
      ctx.lineTo(width - padding.right + 5, yPos);
    }
    ctx.stroke();

    ctx.fillStyle = axisColor;
    ctx.font = "bold 11px Inter, sans-serif";
    ctx.textAlign = "center";

    for (let wnVal = Math.floor(maxWn / wnStep) * wnStep; wnVal >= minWn; wnVal -= wnStep) {
      const xPos = mapX(wnVal);
      if (xPos >= padding.left + 15 && xPos <= width - padding.right - 15) {
        ctx.fillText(wnVal.toString(), xPos, padding.top + plotH + 18);
      }
    }

    const irXTitle = irCustomTitles.x || "Wavenumber (cm⁻¹)";
    const irGraphTitle = irCustomTitles.title || "Simulated IR Spectrum";

    ctx.font = "bold 12px Inter, sans-serif";
    ctx.fillText(irXTitle, padding.left + plotW / 2, padding.top + plotH + 36);
    ctx.font = "bold 13px Inter, sans-serif";
    ctx.fillStyle = axisColor;
    ctx.textAlign = "center";
    ctx.fillText(irGraphTitle, padding.left + plotW / 2, 16);

    ctx.font = "10px Inter, sans-serif";
    ctx.textAlign = "right";
    for (let yStep = 0; yStep <= 4; yStep++) {
      const yVal = minY + ((maxY - minY) / 4) * yStep;
      let label = "";
      if (isIntensity) {
        label = yVal >= 10 ? Math.round(yVal).toString() : yVal.toFixed(1);
      } else if (isTrans) {
        label = `${Math.round(yVal)}%`;
      } else {
        label = yVal.toFixed(2);
      }
      const yPos = mapY(yVal);
      ctx.fillText(label, padding.left - 8, yPos + 4);
    }

    let yAxisTitle = "Transmittance (%T)";
    if (isIntensity) {
      yAxisTitle = "Theoretical IR Intensity (km/mol)";
    } else if (isTrans) {
      yAxisTitle = isTheoOnly ? "Theoretical IR Transmittance (Pseudo-%T)" : "Transmittance (%T)";
    } else if (isAbs) {
      yAxisTitle = isTheoOnly ? "Theoretical IR Absorbance (AU)" : "Absorbance (AU)";
    }
    if (irCustomTitles.y) yAxisTitle = irCustomTitles.y;

    ctx.save();
    ctx.translate(16, padding.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "center";
    ctx.font = "bold 12px Inter, sans-serif";
    ctx.fillText(yAxisTitle, 0, 0);
    ctx.restore();

    // =========================================================================
    // STRICT VIEWPORT CLIPPING: Ensure all data curves remain inside the plot box
    // =========================================================================
    ctx.save();
    ctx.beginPath();
    ctx.rect(padding.left, padding.top, plotW, plotH);
    ctx.clip();

    if (showTheoreticalIRSticks) {
      theoCurves.forEach(({ theo }) => {
        ctx.strokeStyle = theo.color;
        ctx.lineWidth = 1.8;
        theo.modes.forEach(m => {
          const scaledWn = (m.frequency_cm * scaleFactor) + shift;
          if (scaledWn >= minWn && scaledWn <= maxWn) {
            const xPos = mapX(scaledWn);
            let stickY, baselineY;
            if (isIntensity) {
              stickY = mapY(m.intensity_km_mol || 0);
              baselineY = mapY(0);
            } else if (isTrans) {
              stickY = mapY(35);
              baselineY = mapY(100);
            } else {
              stickY = mapY(maxAbs * 0.7);
              baselineY = mapY(0);
            }
            ctx.beginPath();
            ctx.moveTo(xPos, baselineY);
            ctx.lineTo(xPos, stickY);
            ctx.stroke();

            ctx.fillStyle = theo.color;
            ctx.beginPath();
            ctx.arc(xPos, stickY, 3, 0, Math.PI * 2);
            ctx.fill();
          }
        });
      });
    }

    theoCurves.forEach(({ theo, curve }) => {
      if (!curve.length) return;
      ctx.strokeStyle = theo.color;
      ctx.lineWidth = 2.4;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();

      let started = false;
      curve.forEach(pt => {
        if (pt.wavenumber_cm >= minWn && pt.wavenumber_cm <= maxWn) {
          const x = mapX(pt.wavenumber_cm);
          let yVal;
          if (isIntensity) {
            yVal = mapY(pt.absorbance_norm * maxTheoModeInt);
          } else if (isTrans) {
            yVal = mapY(pt.transmittance_pct);
          } else {
            yVal = mapY(pt.absorbance);
          }
          if (!started) {
            ctx.moveTo(x, yVal);
            started = true;
          } else {
            ctx.lineTo(x, yVal);
          }
        }
      });
      ctx.stroke();
    });

    activeExps.forEach(exp => {
      if (!exp.raw_data.length) return;
      ctx.strokeStyle = exp.color;
      ctx.lineWidth = 2.0;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();

      let started = false;
      exp.raw_data.forEach(pt => {
        if (typeof pt.wavenumber_cm === 'number' && !isNaN(pt.wavenumber_cm)) {
          const x = mapX(pt.wavenumber_cm);
          let yVal;
          if (isIntensity) {
            yVal = mapY((pt.absorbance / (maxAbs || 1)) * maxTheoModeInt);
          } else if (isTrans) {
            yVal = mapY(pt.transmittance_pct !== undefined ? pt.transmittance_pct : (100 * Math.pow(10, -pt.absorbance)));
          } else {
            yVal = mapY(pt.absorbance !== undefined ? pt.absorbance : (pt.transmittance_pct ? -Math.log10(Math.max(0.0001, pt.transmittance_pct / 100)) : 0));
          }
          if (!started) {
            ctx.moveTo(x, yVal);
            started = true;
          } else {
            ctx.lineTo(x, yVal);
          }
        }
      });
      ctx.stroke();
    });

    // Remove viewport clipping before drawing legend
    ctx.restore();

    // 9. Dedicated Top-Margin Legend (Guaranteed zero overlap with plot area, matching swatch and text colors)
    if (showIRLegend) {
      let curX = padding.left + 5;
      let curY = 20;
      ctx.font = "bold 11px Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
      ctx.textAlign = "left";

      const legendItems = [
        ...theoCurves.map(tc => ({ name: `[THEORY IR] ${tc.theo.name}`, color: tc.theo.color })),
        ...activeExps.map(ec => ({ name: `[EXP FTIR] ${ec.file_name ? ec.file_name + ': ' : ''}${ec.label || ec.column_signal || ec.column_absorbance}`, color: ec.color }))
      ];

      legendItems.forEach(item => {
        const labelText = item.name.length > 28 ? item.name.slice(0, 26) + "…" : item.name;
        const textWidth = ctx.measureText(labelText).width;
        if (curX + textWidth + 28 > width - padding.right && curY === 20) {
          curX = padding.left + 5;
          curY = 38;
        }
        ctx.fillStyle = item.color;
        ctx.fillRect(curX, curY - 8, 12, 5);
        ctx.fillStyle = item.color;
        ctx.fillText(labelText, curX + 16, curY);
        curX += textWidth + 26;
      });
    }

    ctx.restore();
  }

  function drawIRMultiSpectrum() {
    const canvas = document.getElementById("engine-ir-canvas");
    if (!canvas) return;
    renderIRSpectrumToCanvas(canvas, 1, false);
  }

  let recalcIRTimeout = null;
  function triggerRecalculateIRSpectrum() {
    const scale = parseFloat(document.getElementById("engine-ir-scale-slider")?.value || "1.0");
    const fwhm = parseFloat(document.getElementById("engine-ir-fwhm-slider")?.value || "15");
    const shift = parseFloat(document.getElementById("engine-ir-shift-slider")?.value || "0");

    const scaleLbl = document.getElementById("engine-ir-scale-label");
    const fwhmLbl = document.getElementById("engine-ir-fwhm-label");
    const shiftLbl = document.getElementById("engine-ir-shift-label");

    if (scaleLbl) scaleLbl.textContent = scale.toFixed(3);
    if (fwhmLbl) fwhmLbl.textContent = `${fwhm.toFixed(1)} cm⁻¹`;
    if (shiftLbl) shiftLbl.textContent = `${shift >= 0 ? '+' : ''}${shift.toFixed(1)} cm⁻¹`;

    drawIRMultiSpectrum();
  }

  function initQuantumEngine() {
    const form = document.getElementById("engine-form");
    const fileInput = document.getElementById("engine-file-input");
    const dropzone = document.getElementById("engine-dropzone");
    const chosenLabel = document.getElementById("engine-file-chosen-label");
    const pasteText = document.getElementById("engine-paste-text");
    const loadingEl = document.getElementById("engine-loading");
    const errorEl = document.getElementById("engine-error");
    const loadJobBtn = document.getElementById("engine-load-job-btn");

    // Load from completed Kaggle jobs
    if (loadJobBtn) {
      loadJobBtn.addEventListener("click", () => {
        const picker = document.getElementById("engine-job-picker");
        const jobId = picker ? picker.value : "";
        if (!jobId) {
          showToast("Please select a job from the dropdown first.");
          return;
        }
        analyzeJobInEngine(jobId, loadJobBtn);
      });
    }

    if (fileInput) {
      fileInput.addEventListener("change", () => {
        if (fileInput.files && fileInput.files.length > 0) {
          const names = Array.from(fileInput.files).map(f => f.name).join(", ");
          if (chosenLabel) {
            chosenLabel.textContent = fileInput.files.length === 1 ? `Selected: ${fileInput.files[0].name}` : `Selected ${fileInput.files.length} files: ${names}`;
            chosenLabel.classList.remove("hidden");
          }
          if (form) form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", { cancelable: true }));
        }
      });
    }

    const browseBtn = document.getElementById("engine-browse-btn");
    if (browseBtn && fileInput) {
      browseBtn.addEventListener("click", (e) => {
        e.preventDefault();
        fileInput.click();
      });
    }

    if (dropzone) {
      dropzone.addEventListener("click", (e) => {
        if (e.target !== browseBtn && fileInput) fileInput.click();
      });
      ["dragenter", "dragover"].forEach(evt => {
        dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("drag-over"); });
      });
      ["dragleave", "drop"].forEach(evt => {
        dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("drag-over"); });
      });
      dropzone.addEventListener("drop", (e) => {
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
          fileInput.files = e.dataTransfer.files;
          const names = Array.from(fileInput.files).map(f => f.name).join(", ");
          if (chosenLabel) {
            chosenLabel.textContent = fileInput.files.length === 1 ? `Selected: ${fileInput.files[0].name}` : `Selected ${fileInput.files.length} files: ${names}`;
            chosenLabel.classList.remove("hidden");
          }
          if (form) form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", { cancelable: true }));
        }
      });
    }



    // ─────────────────────────────────────────────────────────────
    // Client-Side Web Worker Archive & File Processing Pipeline
    // ─────────────────────────────────────────────────────────────
    let extractedPipelineFiles = [];
    let activePipelineFilter = "ALL";
    let pipelineSearchQuery = "";
    let archiveWorkerInstance = null;

    const pipelinePanel = document.getElementById("engine-pipeline-panel");
    const pipelineArchiveName = document.getElementById("pipeline-archive-name");
    const pipelineCloseBtn = document.getElementById("pipeline-close-btn");
    const pipelineProgressBox = document.getElementById("pipeline-progress-box");
    const pipelineProgressFill = document.getElementById("pipeline-progress-fill");
    const pipelineProgressLabel = document.getElementById("pipeline-progress-label");
    const pipelineProgressPct = document.getElementById("pipeline-progress-pct");
    const pipelineTbody = document.getElementById("pipeline-files-tbody");
    const pipelineSearchInput = document.getElementById("pipeline-search-input");
    const pipelineFilterBtns = document.querySelectorAll(".pipeline-filter-btn");

    function renderPipelineTable() {
      if (!pipelineTbody) return;
      pipelineTbody.innerHTML = "";

      const query = (pipelineSearchQuery || "").toLowerCase();
      const filtered = extractedPipelineFiles.filter(item => {
        if (activePipelineFilter !== "ALL" && item.tier !== activePipelineFilter) return false;
        if (query && !item.name.toLowerCase().includes(query) && !item.sha256.toLowerCase().includes(query)) return false;
        return true;
      });

      if (filtered.length === 0) {
        pipelineTbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding:1.5rem; color:var(--text-muted);">No files match the selected filter.</td></tr>`;
        return;
      }

      filtered.forEach((file, idx) => {
        const tr = document.createElement("tr");
        const sizeKb = (file.size / 1024).toFixed(1);
        const hashShort = file.sha256 ? file.sha256.substring(0, 12) + "…" : "-";
        const tierClass = `badge-${file.tier.toLowerCase()}`;

        let actionBtnHtml = "";
        if (file.tier === "ANALYZE" || file.tier === "UNKNOWN" || file.tier === "KEEP") {
          actionBtnHtml = `<button type="button" class="btn btn-secondary btn-small pipeline-load-file-btn" data-idx="${idx}" style="padding:0.2rem 0.5rem; font-size:0.75rem;">🔬 Load &amp; Analyze</button>`;
        } else if (file.tier === "DUPLICATE") {
          actionBtnHtml = `<span style="font-size:0.75rem; color:var(--text-faint);">Duplicate of ${escapeHtml(file.duplicateOf || "first")}</span>`;
        } else {
          actionBtnHtml = `<span style="font-size:0.75rem; color:var(--text-faint);">Excluded</span>`;
        }

        tr.innerHTML = `
          <td style="font-family:var(--font-mono); font-size:0.78rem; word-break:break-all; max-width:260px;">
            <strong>${escapeHtml(file.name)}</strong>
          </td>
          <td style="color:var(--text-muted); font-size:0.78rem; white-space:nowrap;">${sizeKb} KB</td>
          <td class="hash-preview" title="${escapeHtml(file.sha256 || '')}">${hashShort}</td>
          <td>
            <span class="tier-badge ${tierClass}" title="${escapeHtml(file.reason || '')}">
              ${file.tier}
            </span>
            <div style="font-size:0.72rem; color:var(--text-muted); margin-top:2px;">${escapeHtml(file.reason || '')}</div>
          </td>
          <td style="text-align:right; white-space:nowrap;">${actionBtnHtml}</td>
        `;
        pipelineTbody.appendChild(tr);
      });

      // Bind row actions
      pipelineTbody.querySelectorAll(".pipeline-load-file-btn").forEach(btn => {
        btn.addEventListener("click", () => {
          const idx = parseInt(btn.getAttribute("data-idx"), 10);
          const file = filtered[idx];
          if (file) loadExtractedFileIntoEngine(file);
        });
      });
    }

    async function loadExtractedFileIntoEngine(file) {
      if (!file) return;
      if (loadingEl) loadingEl.classList.remove("hidden");
      if (errorEl) errorEl.classList.add("hidden");
      try {
        let content = file.content;
        if (!content && file.raw) {
          content = await file.raw.text();
        }
        if (!content) throw new Error(`Could not read text content from file "${file.name}".`);

        const data = await postJSON("/api/orca/engine/parse", {
          content: content,
          name: file.name
        });
        if (!data.ok) throw new Error(data.error || "Failed to parse calculation.");
        renderEngineResults(data);
        showToast(`Loaded calculation for <strong>${escapeHtml(file.name)}</strong>.`);
      } catch (err) {
        if (errorEl) showError(errorEl, err.message);
      } finally {
        if (loadingEl) loadingEl.classList.add("hidden");
      }
    }

    if (pipelineCloseBtn) {
      pipelineCloseBtn.addEventListener("click", () => {
        if (pipelinePanel) pipelinePanel.classList.add("hidden");
      });
    }

    if (pipelineFilterBtns) {
      pipelineFilterBtns.forEach(btn => {
        btn.addEventListener("click", () => {
          pipelineFilterBtns.forEach(b => b.classList.remove("active"));
          btn.classList.add("active");
          activePipelineFilter = btn.getAttribute("data-filter") || "ALL";
          renderPipelineTable();
        });
      });
    }

    if (pipelineSearchInput) {
      pipelineSearchInput.addEventListener("input", (e) => {
        pipelineSearchQuery = e.target.value;
        renderPipelineTable();
      });
    }

    async function processFilesWithWorker(files) {
      if (!files || files.length === 0) return false;

      const isSingleArchive = files.length === 1 && /\.(zip|tar|gz|tgz|tar\.gz|tar\.bz2|tar\.xz)$/i.test(files[0].name);
      // Single archives and single calculation files route directly to server endpoint for rock-solid extraction
      const isBatch = files.length > 1;

      if (!isBatch) {
        // Direct single calculation or single archive file submit
        return false;
      }

      if (pipelinePanel) pipelinePanel.classList.remove("hidden");
      if (pipelineProgressBox) pipelineProgressBox.classList.remove("hidden");
      if (pipelineProgressFill) pipelineProgressFill.style.width = "5%";
      if (pipelineProgressPct) pipelineProgressPct.textContent = "5%";
      if (pipelineProgressLabel) pipelineProgressLabel.textContent = "Initializing worker thread…";
      if (pipelineArchiveName) pipelineArchiveName.textContent = isSingleArchive ? `Archive: ${files[0].name}` : `Batch Upload (${files.length} files)`;

      if (archiveWorkerInstance) {
        archiveWorkerInstance.terminate();
      }
      archiveWorkerInstance = new Worker("/static/js/workers/archive-worker.js");

      archiveWorkerInstance.onmessage = function(e) {
        const msg = e.data || {};
        if (msg.type === "PROGRESS") {
          const pct = msg.percent || 0;
          if (pipelineProgressFill) pipelineProgressFill.style.width = `${pct}%`;
          if (pipelineProgressPct) pipelineProgressPct.textContent = `${pct}%`;
          if (pipelineProgressLabel) pipelineProgressLabel.textContent = `Processing (${msg.current}/${msg.total}): ${msg.filename}`;
        } else if (msg.type === "COMPLETE") {
          if (pipelineProgressBox) pipelineProgressBox.classList.add("hidden");
          extractedPipelineFiles = msg.entries || [];

          // Update metrics
          const summary = msg.summary || {};
          const setMetric = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val || 0;
          };
          setMetric("metric-total-files", summary.total);
          setMetric("metric-analyze-files", summary.analyze);
          setMetric("metric-keep-files", summary.keep);
          setMetric("metric-unknown-files", summary.unknown);
          setMetric("metric-duplicate-files", summary.duplicate);
          setMetric("metric-ignore-files", summary.ignore);

          renderPipelineTable();

          // Populate the Archive Dropdown
          const archiveBar = document.getElementById("engine-archive-bar");
          const archiveSelect = document.getElementById("engine-archive-select");
          const analyzeFiles = extractedPipelineFiles.filter(f => f.tier === "ANALYZE" || f.tier === "UNKNOWN");
          if (archiveSelect && analyzeFiles.length > 0) {
            archiveSelect.innerHTML = "";
            analyzeFiles.forEach(f => {
              const opt = document.createElement("option");
              opt.value = f.name;
              opt.textContent = `${f.name} (${(f.size / 1024).toFixed(1)} KB) - ${f.tier}`;
              archiveSelect.appendChild(opt);
            });
            if (archiveBar) archiveBar.classList.remove("hidden");

            if (!archiveSelect.dataset.listenerAttached) {
              archiveSelect.dataset.listenerAttached = "true";
              archiveSelect.addEventListener("change", (e) => {
                const selectedFileName = e.target.value;
                const targetFile = extractedPipelineFiles.find(f => f.name === selectedFileName);
                if (targetFile) {
                  loadExtractedFileIntoEngine(targetFile);
                }
              });
            }
          }


          // Auto-load primary analysis file if available
          const primary = extractedPipelineFiles.find(f => f.tier === "ANALYZE");
          if (primary) {
            loadExtractedFileIntoEngine(primary);
          } else if (extractedPipelineFiles.length > 0) {
            showToast(`Archive unpacked: ${extractedPipelineFiles.length} file(s) classified.`);
          }
        } else if (msg.type === "ERROR") {
          if (pipelineProgressBox) pipelineProgressBox.classList.add("hidden");
          if (errorEl) showError(errorEl, `Worker processing error: ${msg.error}`);
        }
      };

      archiveWorkerInstance.onerror = function(err) {
        if (pipelineProgressBox) pipelineProgressBox.classList.add("hidden");
        if (errorEl) showError(errorEl, `Web Worker error: ${err.message || "Failed to process files in background worker."}`);
      };

      if (isSingleArchive) {
        const file = files[0];
        const buffer = await file.arrayBuffer();
        archiveWorkerInstance.postMessage({
          action: "PROCESS_ARCHIVE",
          filename: file.name,
          buffer: buffer
        }, [buffer]);
      } else {
        const rawFiles = [];
        for (let i = 0; i < files.length; i++) {
          const file = files[i];
          const buf = await file.arrayBuffer();
          rawFiles.push({ name: file.name, size: file.size, buffer: buf });
        }
        const transferBuffers = rawFiles.map(r => r.buffer);
        archiveWorkerInstance.postMessage({
          action: "PROCESS_PLAIN_FILES",
          rawFiles: rawFiles
        }, transferBuffers);
      }

      return true;
    }

    if (form) {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const hasFile = fileInput && fileInput.files && fileInput.files.length > 0;
        const textContent = pasteText ? pasteText.value.trim() : "";

        if (hasFile) {
          const handledByWorker = await processFilesWithWorker(fileInput.files);
          if (handledByWorker) return;
        }

        if (loadingEl) loadingEl.classList.remove("hidden");
        if (errorEl) errorEl.classList.add("hidden");

        if (!hasFile && !textContent) {
          if (errorEl) showError(errorEl, "Please choose an ORCA output file or paste calculation text.");
          if (loadingEl) loadingEl.classList.add("hidden");
          return;
        }

        try {
          let data;
          if (hasFile) {
            const formData = new FormData();
            formData.append("file", fileInput.files[0]);
            const resp = await fetch("/api/orca/engine/parse", { method: "POST", body: formData });
            data = await resp.json();
            if (!resp.ok || !data.ok) throw new Error(data.error || "Failed to parse ORCA output file.");
          } else {
            data = await postJSON("/api/orca/engine/parse", { content: textContent, name: "pasted_calculation" });
          }

          if (data.is_archive && data.archive_entries && data.archive_entries.length > 0) {
            const archiveBar = document.getElementById("engine-archive-bar");
            const archiveSelect = document.getElementById("engine-archive-select");
            const switchBtn = document.getElementById("engine-archive-switch-btn");
            if (archiveBar && archiveSelect) {
              archiveSelect.innerHTML = "";
              data.archive_entries.forEach(entry => {
                const opt = document.createElement("option");
                opt.value = entry.filename || entry.basename;
                opt.textContent = `${entry.basename || entry.filename} (${entry.jobs_count || 1} job(s))`;
                archiveSelect.appendChild(opt);
              });
              archiveSelect.value = data.selected_file || data.archive_entries[0].filename || data.archive_entries[0].basename;
              archiveBar.classList.remove("hidden");

              const switchCalculation = () => {
                const selectedVal = archiveSelect.value;
                const found = data.archive_entries.find(e => (e.filename === selectedVal || e.basename === selectedVal));
                if (found) {
                  const viewData = {
                    ok: true,
                    is_archive: true,
                    archive_filename: data.archive_filename,
                    archive_entries: data.archive_entries,
                    selected_file: found.filename || found.basename,
                    raw_text: found.raw_text,
                    name: found.molecule_name || found.basename,
                    molecule: found.molecule,
                    jobs_count: found.jobs_count,
                    jobs: (found.molecule && found.molecule.jobs) || (found.jobs) || [found.latest_job],
                    latest_job: found.latest_job,
                  };
                  renderEngineResults(viewData);
                  showToast(`Loaded calculation: <strong>${escapeHtml(found.basename || found.filename)}</strong>`);
                }
              };

              archiveSelect.onchange = switchCalculation;
              if (switchBtn) switchBtn.onclick = switchCalculation;
            }
          } else {
            const archiveBar = document.getElementById("engine-archive-bar");
            if (archiveBar) archiveBar.classList.add("hidden");
          }

          renderEngineResults(data);
          showToast(`ORCA output parsed successfully (${data.jobs_count || 1} job block(s) detected).`);
        } catch (err) {
          if (errorEl) showError(errorEl, err.message);
        } finally {
          if (loadingEl) loadingEl.classList.add("hidden");
        }
      });
    }

    // 3D viewer controls
    const styleSelect = document.getElementById("engine-3d-style");
    if (styleSelect) styleSelect.addEventListener("change", applyViewer3DStyle);

    const labelModeSelect = document.getElementById("engine-3d-label-mode");
    if (labelModeSelect) labelModeSelect.addEventListener("change", applyViewer3DStyle);

    const labelColorSelect = document.getElementById("engine-3d-label-color");
    if (labelColorSelect) labelColorSelect.addEventListener("change", applyViewer3DStyle);

    const labelBadgeSelect = document.getElementById("engine-3d-label-badge");
    if (labelBadgeSelect) labelBadgeSelect.addEventListener("change", applyViewer3DStyle);

    const labelBoldBtn = document.getElementById("engine-3d-label-bold");
    if (labelBoldBtn) {
      labelBoldBtn.addEventListener("click", () => {
        isLabelBold = !isLabelBold;
        labelBoldBtn.classList.toggle("btn-primary", isLabelBold);
        labelBoldBtn.classList.toggle("btn-ghost", !isLabelBold);
        applyViewer3DStyle();
      });
    }

    const crgBtn = document.getElementById("engine-3d-load-crg-btn");
    const crgInput = document.getElementById("engine-3d-crg-input");
    if (crgBtn && crgInput) {
      crgBtn.addEventListener("click", () => {
        crgInput.click();
      });
      crgInput.addEventListener("change", () => {
        if (!crgInput.files || !crgInput.files.length) return;
        const file = crgInput.files[0];
        const reader = new FileReader();
        reader.onload = (e) => {
          const text = e.target.result;
          const parsed = parseMultiWfnCrgText(text);
          if (!parsed.length) {
            showToast("Failed to parse charges from .crg file. Please check file format.", "error");
            return;
          }
          const job = currentEngineData ? (currentEngineData.latest_job || (currentEngineData.jobs && currentEngineData.jobs[currentEngineData.jobs.length - 1]) || (currentEngineData.jobs && currentEngineData.jobs[0]) || currentEngineData) : null;
          if (job) {
            job.hirshfeld_charges = parsed;
            job.crg_charges = parsed;
            if (!job.atomic_charges) job.atomic_charges = {};
            job.atomic_charges["hirshfeld"] = parsed;
            job.atomic_charges["crg"] = parsed;
            updateLabelModeDropdown(true);
            const labelModeSelect = document.getElementById("engine-3d-label-mode");
            if (labelModeSelect) labelModeSelect.value = "hirshfeld";
            applyViewer3DStyle();
            showToast(`Loaded <strong>${parsed.length}</strong> Hirshfeld charges from MultiWfn .crg file.`);
          } else {
            showToast("No active molecule loaded in 3D viewer.", "warning");
          }

        };
        reader.readAsText(file);
        crgInput.value = "";
      });
    }

    const bgSelect = document.getElementById("engine-3d-bg");


    if (bgSelect) {
      bgSelect.addEventListener("change", () => {
        currentViewportBg = bgSelect.value;
        const viewport = document.getElementById("engine-3d-viewport");
        if (viewport) {
          viewport.style.backgroundColor = currentViewportBg === "transparent" ? "transparent" : currentViewportBg;
          const canvas = viewport.querySelector("canvas");
          if (canvas) canvas.style.backgroundColor = currentViewportBg === "transparent" ? "transparent" : currentViewportBg;
        }
        if (viewer3D) {
          if (currentViewportBg === "transparent") {
            viewer3D.setBackgroundColor(0x000000, 0);
          } else {
            viewer3D.setBackgroundColor(currentViewportBg, 1);
          }
          applyViewer3DStyle();
          viewer3D.render();
        }
      });
    }


    const spinBtn = document.getElementById("engine-3d-spin");
    if (spinBtn) {
      spinBtn.addEventListener("click", () => {
        if (!viewer3D) return;
        isSpinning = !isSpinning;
        viewer3D.spin(isSpinning);
        spinBtn.classList.toggle("btn-primary", isSpinning);
      });
    }



    const dipoleBtn = document.getElementById("engine-3d-dipole");
    if (dipoleBtn) {
      dipoleBtn.addEventListener("click", () => {
        showDipole = !showDipole;
        applyViewer3DStyle();
        dipoleBtn.classList.toggle("btn-primary", showDipole);
      });
    }

    const resetBtn = document.getElementById("engine-3d-reset");
    if (resetBtn) {
      resetBtn.addEventListener("click", () => {
        if (!viewer3D) return;
        viewer3D.zoomTo();
        viewer3D.render();
      });
    }

    // UV-Vis Theme Toggle (White / Dark)
    const uvvisThemeToggle = document.getElementById("engine-uvvis-theme-toggle");
    if (uvvisThemeToggle) {
      uvvisThemeToggle.addEventListener("click", () => {
        currentSpectrumTheme = currentSpectrumTheme === "dark" ? "light" : "dark";
        uvvisThemeToggle.textContent = currentSpectrumTheme === "dark" ? "☀️ White Theme" : "🌙 Dark Theme";
        drawUVVisMultiSpectrum();
      });
    }

    // Experimental Spectrum File Upload Button & Input
    const addExpBtn = document.getElementById("engine-add-exp-btn");
    const expFileInput = document.getElementById("engine-exp-file-input");

    if (addExpBtn && expFileInput) {
      addExpBtn.addEventListener("click", () => {
        expFileInput.click();
      });
    }

    function renderUVVisFilesContainer() {
      const container = document.getElementById("exp-y-cols-container");
      if (!container) return;
      if (loadedExperimentalFiles.length === 0) {
        container.innerHTML = `<span style="font-size:0.8rem; color:var(--text-muted);">Upload Excel/CSV to choose multiple Y series.</span>`;
        return;
      }

      container.innerHTML = "";
      loadedExperimentalFiles.forEach((file) => {
        const fileBox = document.createElement("div");
        fileBox.className = "exp-file-box";
        fileBox.style.cssText = "width:100%; border:1px solid var(--border); border-radius:var(--radius-s); padding:0.4rem 0.6rem; margin-bottom:0.4rem; background:var(--surface);";

        // File Header
        const header = document.createElement("div");
        header.style.cssText = "display:flex; justify-content:space-between; align-items:center; margin-bottom:0.3rem;";
        header.innerHTML = `
          <div style="font-size:0.82rem; font-weight:600; display:flex; align-items:center; gap:0.4rem;">
            <span>📄 ${escapeHtml(file.file_name)}${file.sheet_name ? ` (${escapeHtml(file.sheet_name)})` : ''}</span>
            <span style="font-size:0.75rem; color:var(--text-muted); font-weight:normal;">(${file.available_y_cols.length} series)</span>
          </div>
          <button type="button" class="btn btn-ghost btn-small" data-del-uv-file="${file.file_id}" style="padding:0 0.35rem; font-size:0.75rem; color:var(--error);" title="Remove file">✕</button>
        `;
        fileBox.appendChild(header);

        // Series Pills Grid
        const pillsWrap = document.createElement("div");
        pillsWrap.style.cssText = "display:flex; flex-wrap:wrap; gap:0.4rem;";

        file.series_list.forEach((series) => {
          const isSelected = file.selected_y_cols.includes(series.label || series.column_absorbance);
          const pill = document.createElement("label");
          pill.style.cssText = "display:inline-flex; align-items:center; gap:5px; font-size:0.8rem; background:var(--surface-sunken); padding:2px 6px; border-radius:4px; border:1px solid var(--border); cursor:pointer;";

          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.className = "uvvis-y-col-checkbox";
          cb.dataset.fileId = file.file_id;
          cb.dataset.seriesLabel = series.label;
          cb.checked = isSelected;
          cb.addEventListener("change", () => {
            if (cb.checked) {
              if (!file.selected_y_cols.includes(series.label)) file.selected_y_cols.push(series.label);
              series.visible = true;
            } else {
              file.selected_y_cols = file.selected_y_cols.filter(y => y !== series.label);
              series.visible = false;
            }
            syncUVVisFlattenedSpectra();
            drawUVVisMultiSpectrum();
          });

          const colorInp = document.createElement("input");
          colorInp.type = "color";
          colorInp.className = "uvvis-series-color-input";
          colorInp.value = series.color;
          colorInp.title = "Change series color";
          colorInp.style.cssText = "width:18px; height:18px; border:none; padding:0; border-radius:3px; cursor:pointer; background:none;";
          colorInp.addEventListener("input", (e) => {
            series.color = e.target.value;
            series.color_manually_set = true;
            userAssignedSeriesColors.set(`${file.file_name}::${series.label}`, e.target.value);
            updateSpectrumLayersTray();
            drawUVVisMultiSpectrum();
          });

          const nameSpan = document.createElement("span");
          nameSpan.textContent = series.label;

          pill.appendChild(cb);
          pill.appendChild(colorInp);
          pill.appendChild(nameSpan);
          pillsWrap.appendChild(pill);
        });

        fileBox.appendChild(pillsWrap);
        container.appendChild(fileBox);
      });

      // Delete file buttons
      container.querySelectorAll("[data-del-uv-file]").forEach(btn => {
        btn.addEventListener("click", () => {
          const fileId = btn.dataset.delUvFile;
          loadedExperimentalFiles = loadedExperimentalFiles.filter(f => f.file_id !== fileId);
          syncUVVisFlattenedSpectra();
          renderUVVisFilesContainer();
          drawUVVisMultiSpectrum();
        });
      });
    }

    if (expFileInput) {
      expFileInput.addEventListener("change", async () => {
        if (!expFileInput.files || !expFileInput.files.length) return;
        const files = Array.from(expFileInput.files);

        for (const file of files) {
          activeExpFileObj = file;
          const formData = new FormData();
          formData.append("file", file);
          formData.append("file_name", file.name);

          try {
            showToast(`Parsing experimental spectrum <strong>${file.name}</strong>…`);
            const resp = await fetch("/api/orca/engine/experimental-spectrum/parse", {
              method: "POST",
              body: formData,
            });
            const data = await resp.json();
            if (!resp.ok || !data.ok) {
              throw new Error(data.error || "Failed to parse experimental file.");
            }

            const spectraToAdd = data.spectra || (data.spectrum ? [data.spectrum] : []);
            spectraToAdd.forEach(s => addExperimentalSpectrumToStudio(s));
            renderUVVisFilesContainer();
            showToast(`Added ${spectraToAdd.length} experimental series from <strong>${file.name}</strong>.`);

            if (/\.(xlsx|xlsm|xltx)$/i.test(file.name)) {
              inspectAndPopulateExcelControls(file);
            }
          } catch (err) {
            showToast(`Experimental upload error: ${err.message}`);
          }
        }
        expFileInput.value = "";
      });
    }

    let cachedExcelSheets = [];

    function populateExcelColumnSelectors(headers) {
      const wlSelect = document.getElementById("exp-wl-col-select");
      if (wlSelect) {
        wlSelect.innerHTML = '<option value="auto">Auto-detect Wavelength</option>';
        (headers || []).forEach(h => {
          const opt = document.createElement("option");
          opt.value = h;
          opt.textContent = h;
          wlSelect.appendChild(opt);
        });
      }
    }

    async function inspectAndPopulateExcelControls(file) {
      try {
        const formData = new FormData();
        formData.append("file", file);
        const resp = await fetch("/api/orca/engine/experimental-spectrum/inspect", {
          method: "POST",
          body: formData,
        });
        const data = await resp.json();
        if (data.ok && data.sheets && data.sheets.length > 0) {
          cachedExcelSheets = data.sheets;
          const sheetGroup = document.getElementById("exp-sheet-group");
          const sheetSelect = document.getElementById("exp-sheet-select");

          if (sheetGroup) sheetGroup.classList.remove("hidden");
          if (sheetSelect) {
            sheetSelect.innerHTML = "";
            data.sheets.forEach(s => {
              const opt = document.createElement("option");
              opt.value = s.name;
              opt.textContent = `${s.name} (${s.headers.slice(0, 3).join(", ") || 'No headers'})`;
              sheetSelect.appendChild(opt);
            });
          }

          populateExcelColumnSelectors(data.sheets[0]?.headers || []);
        }
      } catch (e) {
        console.warn("Excel inspection:", e);
      }
    }

    // Sheet / Column Override Listeners
    const expSheetSelect = document.getElementById("exp-sheet-select");
    const expWlSelect = document.getElementById("exp-wl-col-select");

    if (expSheetSelect) {
      expSheetSelect.addEventListener("change", async () => {
        if (!activeExpFileObj) return;
        const selectedSheetName = expSheetSelect.value;
        const targetSheet = cachedExcelSheets.find(s => s.name === selectedSheetName);
        if (targetSheet && targetSheet.headers) {
          populateExcelColumnSelectors(targetSheet.headers);
        }
        // Reparse for the new sheet
        const formData = new FormData();
        formData.append("file", activeExpFileObj);
        formData.append("file_name", activeExpFileObj.name);
        formData.append("sheet_name", selectedSheetName);
        try {
          const resp = await fetch("/api/orca/engine/experimental-spectrum/parse", { method: "POST", body: formData });
          const data = await resp.json();
          if (data.ok && (data.spectra || data.spectrum)) {
            const list = data.spectra || [data.spectrum];
            list.forEach(s => addExperimentalSpectrumToStudio(s));
            renderUVVisFilesContainer();
            drawUVVisMultiSpectrum();
          }
        } catch (e) {
          console.warn("Sheet switch parse error:", e);
        }
      });
    }

    const expSelectAllY = document.getElementById("exp-select-all-y");
    const expDeselectAllY = document.getElementById("exp-deselect-all-y");
    if (expSelectAllY) {
      expSelectAllY.addEventListener("click", (e) => {
        e.preventDefault();
        loadedExperimentalFiles.forEach(file => {
          file.selected_y_cols = [...file.available_y_cols];
          file.series_list.forEach(s => s.visible = true);
        });
        syncUVVisFlattenedSpectra();
        renderUVVisFilesContainer();
        drawUVVisMultiSpectrum();
      });
    }
    if (expDeselectAllY) {
      expDeselectAllY.addEventListener("click", (e) => {
        e.preventDefault();
        loadedExperimentalFiles.forEach(file => {
          file.selected_y_cols = [];
          file.series_list.forEach(s => s.visible = false);
        });
        syncUVVisFlattenedSpectra();
        renderUVVisFilesContainer();
        drawUVVisMultiSpectrum();
      });
    }

    // Global Display Controls
    const normSelect = document.getElementById("spectrum-norm-mode");
    if (normSelect) {
      normSelect.addEventListener("change", (e) => {
        spectrumNormalizeMode = e.target.value;
        drawUVVisMultiSpectrum();
      });
    }

    const viewportSelect = document.getElementById("spectrum-viewport-mode");
    const customRangeWrap = document.getElementById("spectrum-custom-range-wrap");
    const minWlInp = document.getElementById("spectrum-min-wl");
    const maxWlInp = document.getElementById("spectrum-max-wl");

    if (viewportSelect) {
      viewportSelect.addEventListener("change", (e) => {
        spectrumViewportMode = e.target.value;
        if (customRangeWrap) {
          customRangeWrap.style.display = spectrumViewportMode === "custom" ? "flex" : "none";
          customRangeWrap.classList.toggle("hidden", spectrumViewportMode !== "custom");
        }
        drawUVVisMultiSpectrum();
      });
    }

    if (minWlInp) minWlInp.addEventListener("input", drawUVVisMultiSpectrum);
    if (maxWlInp) maxWlInp.addEventListener("input", drawUVVisMultiSpectrum);

    // Manual Axis Range Controls (UV-Vis)
    const uvManualToggle = document.getElementById("spectrum-manual-range-toggle");
    const uvManualInputs = document.getElementById("spectrum-manual-axis-inputs");
    const uvApplyAxisBtn = document.getElementById("spectrum-apply-axis-btn");
    const uvAutoAxisBtn = document.getElementById("spectrum-auto-axis-btn");
    const uvMinXInp = document.getElementById("spectrum-manual-min-x");
    const uvMaxXInp = document.getElementById("spectrum-manual-max-x");
    const uvMinYInp = document.getElementById("spectrum-manual-min-y");
    const uvMaxYInp = document.getElementById("spectrum-manual-max-y");

    if (uvManualToggle) {
      uvManualToggle.addEventListener("change", (e) => {
        uvvisManualRangeEnabled = e.target.checked;
        if (uvManualInputs) uvManualInputs.style.display = uvvisManualRangeEnabled ? "flex" : "none";
        drawUVVisMultiSpectrum();
      });
    }
    if (uvApplyAxisBtn) {
      uvApplyAxisBtn.addEventListener("click", () => {
        const minX = parseFloat(uvMinXInp?.value);
        const maxX = parseFloat(uvMaxXInp?.value);
        const minY = parseFloat(uvMinYInp?.value);
        const maxY = parseFloat(uvMaxYInp?.value);
        if (!isNaN(minX)) uvvisManualMinX = minX;
        if (!isNaN(maxX)) uvvisManualMaxX = maxX;
        if (!isNaN(minY)) uvvisManualMinY = minY;
        if (!isNaN(maxY)) uvvisManualMaxY = maxY;
        uvvisManualRangeEnabled = true;
        if (uvManualToggle) uvManualToggle.checked = true;
        if (uvManualInputs) uvManualInputs.style.display = "flex";
        drawUVVisMultiSpectrum();
        showToast("Applied manual axis limits to UV-Vis chart.");
      });
    }
    if (uvAutoAxisBtn) {
      uvAutoAxisBtn.addEventListener("click", () => {
        uvvisManualRangeEnabled = false;
        uvvisManualMinX = null;
        uvvisManualMaxX = null;
        uvvisManualMinY = null;
        uvvisManualMaxY = null;
        if (uvManualToggle) uvManualToggle.checked = false;
        if (uvManualInputs) uvManualInputs.style.display = "none";
        if (uvMinXInp) uvMinXInp.value = "";
        if (uvMaxXInp) uvMaxXInp.value = "";
        if (uvMinYInp) uvMinYInp.value = "";
        if (uvMaxYInp) uvMaxYInp.value = "";
        drawUVVisMultiSpectrum();
        showToast("Reset UV-Vis axes to Auto Range.");
      });
    }


    // Quick Layer Toggles
    const uvvisLegendToggleBtn = document.getElementById("engine-uvvis-legend-toggle");
    const uvvisLegendCb = document.getElementById("uvvis-toggle-legend");
    const setUVVisLegend = (show) => {
      showUVVisLegend = show;
      if (uvvisLegendCb) uvvisLegendCb.checked = show;
      if (uvvisLegendToggleBtn) uvvisLegendToggleBtn.textContent = show ? "🏷️ Hide Legend" : "🏷️ Show Legend";
      drawUVVisMultiSpectrum();
    };
    if (uvvisLegendToggleBtn) {
      uvvisLegendToggleBtn.addEventListener("click", () => setUVVisLegend(!showUVVisLegend));
    }
    if (uvvisLegendCb) {
      uvvisLegendCb.addEventListener("change", (e) => setUVVisLegend(e.target.checked));
    }

    const toggleAllExp = document.getElementById("toggle-all-exp");
    if (toggleAllExp) {
      toggleAllExp.addEventListener("change", (e) => {
        loadedExperimentalSpectra.forEach(exp => exp.visible = e.target.checked);
        updateSpectrumLayersTray();
        drawUVVisMultiSpectrum();
      });
    }

    const toggleAllTheo = document.getElementById("toggle-all-theo");
    if (toggleAllTheo) {
      toggleAllTheo.addEventListener("change", (e) => {
        loadedTheoreticalSpectra.forEach(theo => theo.visible = e.target.checked);
        updateSpectrumLayersTray();
        drawUVVisMultiSpectrum();
      });
    }

    const toggleSticks = document.getElementById("toggle-trans-sticks");
    if (toggleSticks) {
      toggleSticks.addEventListener("change", (e) => {
        showTheoreticalSticks = e.target.checked;
        drawUVVisMultiSpectrum();
      });
    }

    const togglePeaks = document.getElementById("toggle-exp-peaks");
    if (togglePeaks) {
      togglePeaks.addEventListener("change", (e) => {
        showExperimentalPeaks = e.target.checked;
        drawUVVisMultiSpectrum();
      });
    }

    // Interactive Hover Tooltip for Canvas
    const uvvisCanvas = document.getElementById("engine-uvvis-canvas");
    const uvvisTooltip = document.getElementById("uvvis-tooltip");

    if (uvvisCanvas && uvvisTooltip) {
      uvvisCanvas.addEventListener("mousemove", (e) => {
        const activeTheos = loadedTheoreticalSpectra.filter(t => t.visible && uvViewAllows("theo"));
        const activeExps = loadedExperimentalSpectra.filter(e => e.visible && uvViewAllows("exp"));
        if (!activeTheos.length && !activeExps.length) {
          uvvisTooltip.classList.add("hidden");
          return;
        }

        const rect = uvvisCanvas.getBoundingClientRect();
        const canvasRectW = rect.width || uvvisCanvas.clientWidth || uvvisCanvas.width || 1000;
        const canvasRectH = rect.height || uvvisCanvas.clientHeight || uvvisCanvas.height || 380;
        const mouseX = (e.clientX != null && rect.left != null) ? (e.clientX - rect.left) : (e.offsetX || 300);
        const mouseY = (e.clientY != null && rect.top != null) ? (e.clientY - rect.top) : (e.offsetY || 150);
        const scaleX = uvvisCanvas.width / canvasRectW;
        const scaleY = uvvisCanvas.height / canvasRectH;
        const canvasX = mouseX * scaleX;
        const canvasY = mouseY * scaleY;

        const padding = getUVVisPadding();
        const plotW = uvvisCanvas.width - padding.left - padding.right;
        const plotH = uvvisCanvas.height - padding.top - padding.bottom;

        if (canvasX < padding.left || canvasX > uvvisCanvas.width - padding.right || canvasY < padding.top || canvasY > uvvisCanvas.height - padding.bottom) {
          uvvisTooltip.classList.add("hidden");
          return;
        }

        // Determine exact wavelength at mouse position
        const { minX, maxX } = getUVVisBounds(activeTheos, activeExps);
        const wlHover = minX + ((canvasX - padding.left) / plotW) * (maxX - minX);

        let tooltipHtml = `<div style="font-weight:700; border-bottom:1px solid rgba(255,255,255,0.2); margin-bottom:4px; padding-bottom:2px;">Wavelength (λ): ${wlHover.toFixed(2)} nm</div>`;
        let hasReadings = false;

        const sigma = parseFloat(document.getElementById("engine-sigma-slider")?.value || "20");
        const shift = parseFloat(document.getElementById("engine-shift-slider")?.value || "0");

        // Precise theoretical calculation and interpolation
        activeTheos.forEach(theo => {
          const curve = computeUvvisConvolution(theo.transitions, sigma, shift, minX, maxX, 1);
          const intensity = interpolatePoint(curve, wlHover, "wavelength_nm", "intensity");
          if (intensity !== null && !isNaN(intensity)) {
            hasReadings = true;
            let valStr = "";
            if (spectrumNormalizeMode in { "all": 1, "theoretical_only": 1 }) {
              const maxI = Math.max(...curve.map(p => p.intensity), 1.0);
              valStr = `Norm = ${(intensity / maxI).toFixed(3)}`;
            } else {
              valStr = `ε = ${intensity.toFixed(2)} L·mol⁻¹·cm⁻¹`;
            }
            tooltipHtml += `<div style="display:flex; align-items:center; gap:6px; margin:2px 0;"><span style="color:${theo.color}; font-size:1.1em;">●</span> <span>${escapeHtml(theo.name)}:</span> <strong>${valStr}</strong></div>`;
          }
        });

        // Precise experimental interpolation
        activeExps.forEach(exp => {
          const abs = interpolatePoint(exp.raw_data, wlHover, "wavelength_nm", "absorbance");
          if (abs !== null && !isNaN(abs)) {
            hasReadings = true;
            let valStr = "";
            if (spectrumNormalizeMode in { "all": 1, "experimental_only": 1 }) {
              const maxA = Math.max(...exp.raw_data.map(p => p.absorbance), 1.0);
              valStr = `Norm = ${(abs / maxA).toFixed(3)}`;
            } else {
              valStr = `Abs = ${abs.toFixed(4)} AU`;
            }
            tooltipHtml += `<div style="display:flex; align-items:center; gap:6px; margin:2px 0;"><span style="color:${exp.color}; font-size:1.1em;">■</span> <span>${escapeHtml(exp.label || exp.file_name)}:</span> <strong>${valStr}</strong></div>`;
          }
        });

        if (hasReadings) {
          uvvisTooltip.innerHTML = tooltipHtml;
          const tooltipWidth = 240;
          const posX = (mouseX + 15 + tooltipWidth > rect.width) ? (mouseX - tooltipWidth - 10) : (mouseX + 15);
          const posY = Math.max(10, Math.min(mouseY - 20, rect.height - 80));
          uvvisTooltip.style.left = `${posX}px`;
          uvvisTooltip.style.top = `${posY}px`;
          uvvisTooltip.classList.remove("hidden");
        } else {
          uvvisTooltip.classList.add("hidden");
        }
      });

      uvvisCanvas.addEventListener("mouseleave", () => {
        uvvisTooltip.classList.add("hidden");
      });
    }

    // UV-Vis Sliders (Gaussian Broadening & Theoretical Shift)
    const sigmaSlider = document.getElementById("engine-sigma-slider");
    const shiftSlider = document.getElementById("engine-shift-slider");
    if (sigmaSlider) sigmaSlider.addEventListener("input", triggerRecalculateSpectrum);
    if (shiftSlider) shiftSlider.addEventListener("input", triggerRecalculateSpectrum);

    // Multi-Spectrum Combined CSV Export
    const exportCsvBtn = document.getElementById("engine-uvvis-export-csv");
    if (exportCsvBtn) {
      exportCsvBtn.addEventListener("click", () => {
        const activeTheos = loadedTheoreticalSpectra.filter(t => t.visible && uvViewAllows("theo"));
        const activeExps = loadedExperimentalSpectra.filter(e => e.visible && uvViewAllows("exp"));

        if (!activeTheos.length && !activeExps.length) {
          showToast("No active spectrum layers to export.");
          return;
        }

        const sigma = parseFloat(document.getElementById("engine-sigma-slider")?.value || "20");
        const shift = parseFloat(document.getElementById("engine-shift-slider")?.value || "0");

        // Unified CSV Header
        let csv = "Wavelength_nm";
        activeTheos.forEach(t => {
          csv += `,"Theory_${t.name.replace(/,/g, '_')}_Intensity"`;
        });
        activeExps.forEach(e => {
          csv += `,"Exp_${e.file_name.replace(/,/g, '_')}_Absorbance_AU"`;
        });
        csv += "\n";

        // Grid 180 to 800 nm step 1
        for (let wl = 180; wl <= 800; wl += 1) {
          let row = `${wl}`;
          activeTheos.forEach(t => {
            const curve = computeUvvisConvolution(t.transitions, sigma, shift, wl, wl, 1);
            row += `,${curve[0] ? curve[0].intensity : 0}`;
          });
          activeExps.forEach(e => {
            const pt = e.raw_data.find(p => Math.abs(p.wavelength_nm - wl) < 0.5);
            row += `,${pt ? pt.absorbance : ''}`;
          });
          csv += `${row}\n`;
        }

        const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `multi_spectrum_analysis_${Date.now()}.csv`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        showToast("Exported multi-spectrum dataset to CSV.");
      });
    }

    // 📸 High-Resolution Multi-Spectrum Snapshot Download (PNG / Publication Quality)
    const dlUvvisPngBtn = document.getElementById("engine-uvvis-download-png");
    if (dlUvvisPngBtn) {
      dlUvvisPngBtn.addEventListener("click", () => {
        const activeTheos = loadedTheoreticalSpectra.filter(t => t.visible && uvViewAllows("theo"));
        const activeExps = loadedExperimentalSpectra.filter(e => e.visible && uvViewAllows("exp"));

        if (!activeTheos.length && !activeExps.length) {
          showToast("No spectrum data available to export.");
          return;
        }

        const scale = 2.5; // High-resolution scale
        const offCanvas = document.createElement("canvas");
        offCanvas.width = 1000 * scale;
        offCanvas.height = 380 * scale;

        renderSpectrumToCanvas(offCanvas, scale, true);

        offCanvas.toBlob((blob) => {
          if (!blob) return;
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          const name = currentEngineData?.name || "spectrum";
          a.download = `${name.replace(/\.[^/.]+$/, "")}_publication_uvvis.png`;
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          showToast("High-resolution publication spectrum PNG saved.");
        }, "image/png");
      });
    }

    // =========================================================================
    // IR Spectrum Studio Event Listeners
    // =========================================================================
    const irExpBtn = document.getElementById("engine-ir-add-exp-btn");
    const irExpFileInput = document.getElementById("engine-ir-exp-file-input");
    const irThemeToggle = document.getElementById("engine-ir-theme-toggle");
    const irDownloadPngBtn = document.getElementById("engine-ir-download-png");
    const irExportCsvBtn = document.getElementById("engine-ir-export-csv");
    const irScaleSlider = document.getElementById("engine-ir-scale-slider");
    const irFwhmSlider = document.getElementById("engine-ir-fwhm-slider");
    const irShiftSlider = document.getElementById("engine-ir-shift-slider");
    const irYModeSelect = document.getElementById("ir-y-mode");
    const irViewportSelect = document.getElementById("ir-viewport-mode");
    const irCustomRangeWrap = document.getElementById("ir-custom-range-wrap");
    const irMinWnInp = document.getElementById("ir-min-wn");
    const irMaxWnInp = document.getElementById("ir-max-wn");
    const irToggleAllExp = document.getElementById("ir-toggle-all-exp");
    const irToggleAllTheo = document.getElementById("ir-toggle-all-theo");
    const irToggleSticks = document.getElementById("ir-toggle-sticks");
    const irCanvas = document.getElementById("engine-ir-canvas");
    const irTooltip = document.getElementById("ir-tooltip");

    let activeIRExpFileObj = null;
    const irExpSheetGroup = document.getElementById("ir-exp-sheet-group");
    const irExpSheetSelect = document.getElementById("ir-exp-sheet-select");
    const irExpWnSelect = document.getElementById("ir-exp-wn-col-select");

    function renderIRFilesContainer() {
      const container = document.getElementById("ir-exp-y-cols-container");
      if (!container) return;
      if (loadedExperimentalIRFiles.length === 0) {
        container.innerHTML = `<span style="font-size:0.8rem; color:var(--text-muted);">Upload Excel/CSV to choose multiple Y series.</span>`;
        return;
      }

      container.innerHTML = "";
      loadedExperimentalIRFiles.forEach((file) => {
        const fileBox = document.createElement("div");
        fileBox.className = "exp-file-box";
        fileBox.style.cssText = "width:100%; border:1px solid var(--border); border-radius:var(--radius-s); padding:0.4rem 0.6rem; margin-bottom:0.4rem; background:var(--surface);";

        // File Header
        const header = document.createElement("div");
        header.style.cssText = "display:flex; justify-content:space-between; align-items:center; margin-bottom:0.3rem;";
        header.innerHTML = `
          <div style="font-size:0.82rem; font-weight:600; display:flex; align-items:center; gap:0.4rem;">
            <span>📄 ${escapeHtml(file.file_name)}${file.sheet_name ? ` (${escapeHtml(file.sheet_name)})` : ''}</span>
            <span style="font-size:0.75rem; color:var(--text-muted); font-weight:normal;">(${file.available_y_cols.length} series)</span>
          </div>
          <button type="button" class="btn btn-ghost btn-small" data-del-ir-file="${file.file_id}" style="padding:0 0.35rem; font-size:0.75rem; color:var(--error);" title="Remove file">✕</button>
        `;
        fileBox.appendChild(header);

        // Series Pills Grid
        const pillsWrap = document.createElement("div");
        pillsWrap.style.cssText = "display:flex; flex-wrap:wrap; gap:0.4rem;";

        file.series_list.forEach((series) => {
          const isSelected = file.selected_y_cols.includes(series.label || series.column_signal || series.column_absorbance);
          const pill = document.createElement("label");
          pill.style.cssText = "display:inline-flex; align-items:center; gap:5px; font-size:0.8rem; background:var(--surface-sunken); padding:2px 6px; border-radius:4px; border:1px solid var(--border); cursor:pointer;";

          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.className = "ir-y-col-checkbox";
          cb.dataset.fileId = file.file_id;
          cb.dataset.seriesLabel = series.label;
          cb.checked = isSelected;
          cb.addEventListener("change", () => {
            if (cb.checked) {
              if (!file.selected_y_cols.includes(series.label)) file.selected_y_cols.push(series.label);
              series.visible = true;
            } else {
              file.selected_y_cols = file.selected_y_cols.filter(y => y !== series.label);
              series.visible = false;
            }
            syncIRFlattenedSpectra();
            drawIRMultiSpectrum();
          });

          const colorInp = document.createElement("input");
          colorInp.type = "color";
          colorInp.className = "ir-series-color-input";
          colorInp.dataset.seriesLabel = series.label;
          colorInp.value = series.color;
          colorInp.title = "Change FTIR series color";
          colorInp.style.cssText = "width:18px; height:18px; border:none; padding:0; border-radius:3px; cursor:pointer; background:none;";
          colorInp.addEventListener("input", (e) => {
            series.color = e.target.value;
            series.color_manually_set = true;
            userAssignedSeriesColors.set(`${file.file_name}::${series.label}`, e.target.value);
            updateIRSpectrumLayersTray();
            drawIRMultiSpectrum();
          });

          const nameSpan = document.createElement("span");
          nameSpan.textContent = series.label;

          pill.appendChild(cb);
          pill.appendChild(colorInp);
          pill.appendChild(nameSpan);
          pillsWrap.appendChild(pill);
        });

        fileBox.appendChild(pillsWrap);
        container.appendChild(fileBox);
      });

      // Delete file buttons
      container.querySelectorAll("[data-del-ir-file]").forEach(btn => {
        btn.addEventListener("click", () => {
          const fileId = btn.dataset.delIrFile;
          loadedExperimentalIRFiles = loadedExperimentalIRFiles.filter(f => f.file_id !== fileId);
          syncIRFlattenedSpectra();
          renderIRFilesContainer();
          drawIRMultiSpectrum();
        });
      });
    }

    if (irExpSheetSelect) {
      irExpSheetSelect.addEventListener("change", async () => {
        if (!activeIRExpFileObj) return;
        const selectedSheetName = irExpSheetSelect.value;
        const formData = new FormData();
        formData.append("file", activeIRExpFileObj);
        formData.append("file_name", activeIRExpFileObj.name);
        formData.append("sheet_name", selectedSheetName);
        try {
          const resp = await fetch("/api/orca/engine/ir-spectrum/parse-experimental", { method: "POST", body: formData });
          const data = await resp.json();
          if (data.success && (data.spectra || data.spectrum)) {
            const list = data.spectra || [data.spectrum];
            list.forEach(s => addExperimentalIRSpectrumToStudio(s));
            renderIRFilesContainer();
            drawIRMultiSpectrum();
          }
        } catch (e) {
          console.warn("IR Sheet switch error:", e);
        }
      });
    }

    const irExpSelectAllY = document.getElementById("ir-exp-select-all-y");
    const irExpDeselectAllY = document.getElementById("ir-exp-deselect-all-y");
    if (irExpSelectAllY) {
      irExpSelectAllY.addEventListener("click", (e) => {
        e.preventDefault();
        loadedExperimentalIRFiles.forEach(file => {
          file.selected_y_cols = [...file.available_y_cols];
          file.series_list.forEach(s => s.visible = true);
        });
        syncIRFlattenedSpectra();
        renderIRFilesContainer();
        drawIRMultiSpectrum();
      });
    }
    if (irExpDeselectAllY) {
      irExpDeselectAllY.addEventListener("click", (e) => {
        e.preventDefault();
        loadedExperimentalIRFiles.forEach(file => {
          file.selected_y_cols = [];
          file.series_list.forEach(s => s.visible = false);
        });
        syncIRFlattenedSpectra();
        renderIRFilesContainer();
        drawIRMultiSpectrum();
      });
    }

    if (irExpBtn && irExpFileInput) {
      irExpBtn.addEventListener("click", () => irExpFileInput.click());
      irExpFileInput.addEventListener("change", async (e) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;

        for (const file of files) {
          const isExcel = /\.(xlsx|xlsm|xltx)$/i.test(file.name);
          activeIRExpFileObj = file;

          const formData = new FormData();
          formData.append("file", file);
          formData.append("file_name", file.name);

          try {
            const res = await fetch("/api/orca/engine/ir-spectrum/parse-experimental", {
              method: "POST",
              body: formData,
            });
            const data = await res.json();
            if (data.success && (data.spectra || data.spectrum)) {
              const spectraToAdd = data.spectra || [data.spectrum];
              spectraToAdd.forEach(s => addExperimentalIRSpectrumToStudio(s));
              renderIRFilesContainer();
              drawIRMultiSpectrum();
              showToast(`Loaded ${spectraToAdd.length} experimental IR series from <strong>${escapeHtml(file.name)}</strong>.`);
            } else {
              showToast(`Error parsing ${file.name}: ${data.error || 'Invalid IR spectrum format'}`, "error");
            }

            // Inspect Excel sheets if workbook
            if (isExcel) {
              try {
                const inspResp = await fetch("/api/orca/engine/experimental-spectrum/inspect", {
                  method: "POST",
                  body: formData,
                });
                const inspData = await inspResp.json();
                if (inspData.ok && inspData.sheets && inspData.sheets.length > 0) {
                  if (irExpSheetGroup) irExpSheetGroup.classList.remove("hidden");
                  if (irExpSheetSelect) {
                    irExpSheetSelect.innerHTML = "";
                    inspData.sheets.forEach(s => {
                      const opt = document.createElement("option");
                      opt.value = s.name;
                      opt.textContent = `${s.name} (${s.headers.slice(0, 3).join(", ") || 'No headers'})`;
                      irExpSheetSelect.appendChild(opt);
                    });
                  }
                  if (irExpWnSelect) {
                    irExpWnSelect.innerHTML = '<option value="auto">Auto Detect Wavenumber Column</option>';
                    (inspData.sheets[0]?.headers || []).forEach((h) => {
                      const opt = document.createElement("option");
                      opt.value = h;
                      opt.textContent = h;
                      irExpWnSelect.appendChild(opt);
                    });
                  }
                }
              } catch (inspErr) {
                console.warn("IR Excel inspection:", inspErr);
              }
            }
          } catch (err) {
            console.error("IR experimental upload error:", err);
            showToast(`Network error uploading ${file.name}.`, "error");
          }
        }
        irExpFileInput.value = "";
      });
    }


    if (irThemeToggle) {
      irThemeToggle.addEventListener("click", () => {
        currentIRTheme = currentIRTheme === "dark" ? "light" : "dark";
        irThemeToggle.textContent = currentIRTheme === "dark" ? "☀️ White Theme" : "🌙 Dark Theme";
        drawIRMultiSpectrum();
      });
    }

    if (irScaleSlider) irScaleSlider.addEventListener("input", triggerRecalculateIRSpectrum);
    if (irFwhmSlider) irFwhmSlider.addEventListener("input", triggerRecalculateIRSpectrum);
    if (irShiftSlider) irShiftSlider.addEventListener("input", triggerRecalculateIRSpectrum);

    if (irYModeSelect) {
      irYModeSelect.addEventListener("change", (e) => {
        irYAxisMode = e.target.value;
        drawIRMultiSpectrum();
      });
    }

    function exportIRXYSingle(kind, idx) {
      const scaleFactor = parseFloat(irScaleSlider?.value || "1.0");
      const fwhm = parseFloat(irFwhmSlider?.value || "15.0");
      const shift = parseFloat(irShiftSlider?.value || "0");
      const headers = irDisplayedHeaders();
      if (kind === "theo") {
        const theo = loadedTheoreticalIRSpectra[idx];
        if (!theo) return;
        const sharedNormMax = getIRSharedNormMax(scaleFactor, fwhm, shift, 400, 4000);
        const curve = computeIRConvolution(theo.modes, scaleFactor, fwhm, shift, 400, 4000, 2, sharedNormMax);
        // canonical FTIR presentation order: 4000 -> 400 (same coordinate pairs,
        // ordered to match the plotted X axis; the renderer's mapX already draws
        // 4000 on the left). No recalculation.
        const ordered = curve.slice().reverse();
        const xs = ordered.map(p => p.wavenumber_cm);
        let ys;
        if (irYAxisMode === "absorbance") ys = ordered.map(p => p.absorbance);
        else if (irYAxisMode === "theory_intensity") {
          const maxModeInt = theo.modes.length ? Math.max(...theo.modes.map(m => m.intensity_km_mol || 0), 10) : 100;
          ys = ordered.map(p => p.absorbance_norm * maxModeInt);
        } else ys = ordered.map(p => p.transmittance_pct);
        const stem = sanitizeFileStem(theo.name);
        const meta = ["Chemistry Lab", "Spectrum: " + theo.name, "Type: Simulated IR",
          "X: Wavenumber (cm^-1)", "Y: " + headers.yHeader + " (" + headers.yUnit + ")",
          "FWHM: " + fwhm + " cm^-1", "Scaling: " + scaleFactor, "Normalization: Per Spectrum"];
        downloadTextFile(stem + "_XY.txt", buildXYText(
          [{ xHeader: "Wavenumber_cm-1", yHeader: headers.yHeader, xs, ys }], meta));
        return;
      }
      const exp = loadedExperimentalIRSpectra[idx];
      if (!exp) return;
      const xs = exp.raw_data.map(p => p.wavenumber_cm);
      const ys = exp.raw_data.map(p => (irYAxisMode === "absorbance" ? p.absorbance : p.transmittance_pct));
      const yHeader = irYAxisMode === "absorbance" ? "Absorbance" : "Transmittance_pct";
      const stem = sanitizeFileStem(exp.label || exp.file_name);
      const meta = ["Chemistry Lab", "Spectrum: " + (exp.label || exp.file_name),
        "Type: Experimental FTIR", "Source file: " + exp.file_name,
        "X: Wavenumber (cm^-1)", "Y: " + yHeader];
      downloadTextFile(stem + "_XY.txt", buildXYText(
        [{ xHeader: "Wavenumber_cm-1", yHeader, xs, ys }], meta));
    }
    function exportIRXYVisible() {
      const scaleFactor = parseFloat(irScaleSlider?.value || "1.0");
      const fwhm = parseFloat(irFwhmSlider?.value || "15.0");
      const shift = parseFloat(irShiftSlider?.value || "0");
      const headers = irDisplayedHeaders();
      const sharedNormMax = getIRSharedNormMax(scaleFactor, fwhm, shift, 400, 4000);
      const columns = [];
      const meta = ["Chemistry Lab", "View: " + irViewMode, "X: Wavenumber (cm^-1)",
        "Y: " + headers.yHeader + " (" + headers.yUnit + ")", "FWHM: " + fwhm + " cm^-1",
        "Normalization: " + (sharedNormMax ? "Shared" : "Per Spectrum")];
      loadedTheoreticalIRSpectra.filter(t => t.visible && irViewAllows("theo")).forEach(theo => {
        const curve = computeIRConvolution(theo.modes, scaleFactor, fwhm, shift, 400, 4000, 2, sharedNormMax);
        const ordered = curve.slice().reverse();
        let ys;
        if (irYAxisMode === "absorbance") ys = ordered.map(p => p.absorbance);
        else if (irYAxisMode === "theory_intensity") {
          const maxModeInt = theo.modes.length ? Math.max(...theo.modes.map(m => m.intensity_km_mol || 0), 10) : 100;
          ys = ordered.map(p => p.absorbance_norm * maxModeInt);
        } else ys = ordered.map(p => p.transmittance_pct);
        columns.push({ xHeader: "Wavenumber_cm-1", yHeader: theo.name,
          xs: ordered.map(p => p.wavenumber_cm), ys });
      });
      loadedExperimentalIRSpectra.filter(e => e.visible && irViewAllows("exp")).forEach(exp => {
        columns.push({ xHeader: "Wavenumber_cm-1_" + sanitizeFileStem(exp.label || exp.file_name),
          yHeader: (exp.label || exp.file_name) + "_" + (irYAxisMode === "absorbance" ? "Absorbance" : "Transmittance_pct"),
          xs: exp.raw_data.map(p => p.wavenumber_cm),
          ys: exp.raw_data.map(p => (irYAxisMode === "absorbance" ? p.absorbance : p.transmittance_pct)) });
      });
      if (!columns.length) return;
      downloadTextFile("IR_visible_curves_XY.txt", buildXYText(columns, meta));
    }
    const irExportXYVisibleBtn = document.getElementById("ir-export-xy-visible");
    if (irExportXYVisibleBtn) {
      irExportXYVisibleBtn.addEventListener("click", exportIRXYVisible);
    }
    const irViewModeSel = document.getElementById("ir-view-mode");
    if (irViewModeSel) {
      irViewModeSel.addEventListener("change", (e) => {
        irViewMode = e.target.value;
        updateIRSpectrumLayersTray();
        drawIRMultiSpectrum();
      });
    }
    [["ir-graph-title", "title"], ["ir-x-title", "x"], ["ir-y-title", "y"]].forEach(([id, key]) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", (e) => { irCustomTitles[key] = e.target.value; drawIRMultiSpectrum(); });
    });
    const irTitlesReset = document.getElementById("ir-titles-reset");
    if (irTitlesReset) {
      irTitlesReset.addEventListener("click", () => {
        irCustomTitles = { title: "", x: "", y: "" };
        ["ir-graph-title", "ir-x-title", "ir-y-title"].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.value = "";
        });
        drawIRMultiSpectrum();
      });
    }
    const irRemoveImportedBtn = document.getElementById("ir-remove-imported");
    if (irRemoveImportedBtn) {
      irRemoveImportedBtn.addEventListener("click", () => {
        for (let i = loadedTheoreticalIRSpectra.length - 1; i >= 0; i--) {
          if (loadedTheoreticalIRSpectra[i].imported) loadedTheoreticalIRSpectra.splice(i, 1);
        }
        updateIRSpectrumLayersTray();
        drawIRMultiSpectrum();
      });
    }

    const irImportBtn = document.getElementById("engine-ir-import-freq-btn");
    const irImportInput = document.getElementById("engine-ir-import-freq-input");
    if (irImportBtn && irImportInput) {
      irImportBtn.addEventListener("click", () => irImportInput.click());
      irImportInput.addEventListener("change", async () => {
        const files = Array.from(irImportInput.files || []);
        irImportInput.value = "";
        if (!files.length) return;
        const statusEl = document.getElementById("engine-ir-import-status");
        const renderStatus = (rows) => {
          if (!statusEl) return;
          statusEl.classList.remove("hidden");
          statusEl.innerHTML = rows.map(r =>
            `<div style="font-size:0.78rem; padding:0.15rem 0;">${escapeHtml(r.label)}: ${escapeHtml(r.text)}</div>`
          ).join("");
        };
        const rows = [];
        irImportBtn.disabled = true;
        try {
          const fd = new FormData();
          files.slice(0, 20).forEach(f => fd.append("files", f));
          const resp = await fetch("/api/orca/engine/import-orca-output", { method: "POST", body: fd });
          const data = await resp.json();
          if (!resp.ok || !data.ok) {
            renderStatus([{ label: "Import failed", text: (data && data.error) || ("HTTP " + resp.status) }]);
            return;
          }
          data.results.forEach(r => {
            if (r.status === "loaded" && r.capabilities && r.capabilities.ir && r.ir && r.ir.modes.length) {
              const exists = loadedTheoreticalIRSpectra.some(t => t.raw_hash === r.raw_hash);
              if (exists) {
                rows.push({ label: r.file_name, text: "already loaded (duplicate skipped)" });
                return;
              }
              addTheoreticalIRCalculationToStudio(
                r.display_name || r.file_name, r.ir.modes,
                r.ir.method || "ORCA DFT", r.ir.basis_set || "",
                { imported: true, raw_hash: r.raw_hash, file_name: r.file_name,
                  imaginary_count: r.ir.imaginary_frequency_count || 0 });
              rows.push({ label: r.file_name, text: "loaded - " + r.ir.modes.length + " modes"
                + (r.ir.imaginary_frequency_count > 0
                    ? " - " + r.ir.imaginary_frequency_count + " imaginary frequency(ies) detected and excluded from the displayed curve"
                    : "")
                + (r.capabilities.uv ? " - TD-DFT/UV data also available in the UV-Vis studio" : "") });
            } else if (r.status === "loaded") {
              rows.push({ label: r.file_name, text: "no IR/FREQ data in this output"
                + (r.capabilities && r.capabilities.uv ? " (TD-DFT/UV data available - import it from the UV-Vis studio)" : "") });
            } else if (r.status === "duplicate") {
              rows.push({ label: r.file_name, text: "already loaded (duplicate of " + (r.duplicate_of || "an imported file") + ")" });
            } else {
              rows.push({ label: r.file_name, text: "rejected - " + (r.reason || "unknown reason") });
            }
          });
          renderStatus(rows);
          updateIRSpectrumLayersTray();
          drawIRMultiSpectrum();
        } catch (err) {
          renderStatus([{ label: "Import failed", text: String((err && err.message) || err) }]);
        } finally {
          irImportBtn.disabled = false;
        }
      });
    }

    function uvDisplayedColumn(curve) {
      const normalized = spectrumNormalizeMode in { "all": 1, "theoretical_only": 1 };
      const normDiv = normalized ? Math.max(...curve.map(p => p.intensity), 0.001) : 1.0;
      return {
        ys: curve.map(p => p.intensity / normDiv),
        yHeader: normalized ? "Theoretical_Intensity_Norm" : "Theoretical_Intensity",
      };
    }
    function exportUVXYSingle(kind, idx) {
      const sigma = parseFloat(document.getElementById("engine-sigma-slider")?.value || "20");
      const shift = parseFloat(document.getElementById("engine-shift-slider")?.value || "0");
      if (kind === "theo") {
        const theo = loadedTheoreticalSpectra[idx];
        if (!theo) return;
        const curve = computeUvvisConvolution(theo.transitions, sigma, shift, 180, 800, 1);
        if (!curve.length) return;
        const col = uvDisplayedColumn(curve);
        const stem = sanitizeFileStem(theo.name);
        const meta = ["Chemistry Lab", "Spectrum: " + theo.name, "Type: Simulated UV-Vis (TD-DFT)",
          "X: Wavelength (nm)", "Y: " + col.yHeader, "Gaussian sigma: " + sigma + " nm",
          "Normalization: " + spectrumNormalizeMode];
        downloadTextFile(stem + "_XY.txt", buildXYText(
          [{ xHeader: "Wavelength_nm", yHeader: col.yHeader, xs: curve.map(p => p.wavelength_nm), ys: col.ys }], meta));
        return;
      }
      const exp = loadedExperimentalSpectra[idx];
      if (!exp) return;
      const xs = exp.raw_data.map(p => p.wavelength_nm);
      const ys = exp.raw_data.map(p => p.absorbance);
      const stem = sanitizeFileStem(exp.label || exp.file_name);
      const meta = ["Chemistry Lab", "Spectrum: " + (exp.label || exp.file_name),
        "Type: Experimental UV-Vis", "Source file: " + exp.file_name,
        "X: Wavelength (nm)", "Y: Absorbance"];
      downloadTextFile(stem + "_XY.txt", buildXYText(
        [{ xHeader: "Wavelength_nm", yHeader: "Absorbance", xs, ys }], meta));
    }
    function exportUVXYVisible() {
      const sigma = parseFloat(document.getElementById("engine-sigma-slider")?.value || "20");
      const shift = parseFloat(document.getElementById("engine-shift-slider")?.value || "0");
      const columns = [];
      const meta = ["Chemistry Lab", "View: " + uvViewMode, "X: Wavelength (nm)",
        "Y: follows the active normalization mode (" + spectrumNormalizeMode + ")",
        "Gaussian sigma: " + sigma + " nm"];
      loadedTheoreticalSpectra.filter(t => t.visible && uvViewAllows("theo")).forEach(theo => {
        const curve = computeUvvisConvolution(theo.transitions, sigma, shift, 180, 800, 1);
        if (!curve.length) return;
        const col = uvDisplayedColumn(curve);
        columns.push({ xHeader: "Wavelength_nm", yHeader: theo.name,
          xs: curve.map(p => p.wavelength_nm), ys: col.ys });
      });
      loadedExperimentalSpectra.filter(e => e.visible && uvViewAllows("exp")).forEach(exp => {
        columns.push({ xHeader: "Wavelength_nm_" + sanitizeFileStem(exp.label || exp.file_name),
          yHeader: (exp.label || exp.file_name) + "_Absorbance",
          xs: exp.raw_data.map(p => p.wavelength_nm),
          ys: exp.raw_data.map(p => p.absorbance) });
      });
      if (!columns.length) return;
      downloadTextFile("UV_visible_curves_XY.txt", buildXYText(columns, meta));
    }
    const uvExportXYVisibleBtn = document.getElementById("uv-export-xy-visible");
    if (uvExportXYVisibleBtn) {
      uvExportXYVisibleBtn.addEventListener("click", exportUVXYVisible);
    }
    const uvViewModeSel = document.getElementById("uv-view-mode");
    if (uvViewModeSel) {
      uvViewModeSel.addEventListener("change", (e) => {
        uvViewMode = e.target.value;
        updateSpectrumLayersTray();
        drawUVVisMultiSpectrum();
      });
    }
    [["uv-graph-title", "title"], ["uv-x-title", "x"], ["uv-y-title", "y"]].forEach(([id, key]) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", (e) => { uvCustomTitles[key] = e.target.value; drawUVVisMultiSpectrum(); });
    });
    const uvTitlesReset = document.getElementById("uv-titles-reset");
    if (uvTitlesReset) {
      uvTitlesReset.addEventListener("click", () => {
        uvCustomTitles = { title: "", x: "", y: "" };
        ["uv-graph-title", "uv-x-title", "uv-y-title"].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.value = "";
        });
        drawUVVisMultiSpectrum();
      });
    }
    const uvRemoveImportedBtn = document.getElementById("uv-remove-imported");
    if (uvRemoveImportedBtn) {
      uvRemoveImportedBtn.addEventListener("click", () => {
        for (let i = loadedTheoreticalSpectra.length - 1; i >= 0; i--) {
          if (loadedTheoreticalSpectra[i].imported) loadedTheoreticalSpectra.splice(i, 1);
        }
        updateSpectrumLayersTray();
        drawUVVisMultiSpectrum();
      });
    }

    const uvImportBtn = document.getElementById("engine-uvvis-import-btn");
    const uvImportInput = document.getElementById("engine-uvvis-import-input");
    if (uvImportBtn && uvImportInput) {
      uvImportBtn.addEventListener("click", () => uvImportInput.click());
      uvImportInput.addEventListener("change", async () => {
        const files = Array.from(uvImportInput.files || []);
        uvImportInput.value = "";
        if (!files.length) return;
        const statusEl = document.getElementById("engine-uvvis-import-status");
        const renderStatus = (rows) => {
          if (!statusEl) return;
          statusEl.classList.remove("hidden");
          statusEl.innerHTML = rows.map(r =>
            `<div style="font-size:0.78rem; padding:0.15rem 0;">${escapeHtml(r.label)}: ${escapeHtml(r.text)}</div>`
          ).join("");
        };
        const rows = [];
        uvImportBtn.disabled = true;
        try {
          const fd = new FormData();
          files.slice(0, 20).forEach(f => fd.append("files", f));
          const resp = await fetch("/api/orca/engine/import-orca-output", { method: "POST", body: fd });
          const data = await resp.json();
          if (!resp.ok || !data.ok) {
            renderStatus([{ label: "Import failed", text: (data && data.error) || ("HTTP " + resp.status) }]);
            return;
          }
          data.results.forEach(r => {
            if (r.status === "loaded" && r.capabilities && r.capabilities.uv && r.uv && r.uv.transitions.length) {
              const exists = loadedTheoreticalSpectra.some(t => t.raw_hash === r.raw_hash);
              if (exists) {
                rows.push({ label: r.file_name, text: "already loaded (duplicate skipped)" });
                return;
              }
              addTheoreticalCalculationToStudio(
                r.display_name || r.file_name, r.uv.transitions,
                r.uv.method || "TD-DFT", r.uv.basis_set || "",
                { imported: true, raw_hash: r.raw_hash, file_name: r.file_name });
              rows.push({ label: r.file_name, text: "loaded - " + r.uv.transitions.length + " transitions"
                + (r.capabilities.ir ? " - FREQ/IR data also available in the IR studio" : "") });
            } else if (r.status === "loaded") {
              rows.push({ label: r.file_name, text: "no TD-DFT/UV data in this output"
                + (r.capabilities && r.capabilities.ir ? " (FREQ/IR data available - import it from the IR studio)" : "") });
            } else if (r.status === "duplicate") {
              rows.push({ label: r.file_name, text: "already loaded (duplicate of " + (r.duplicate_of || "an imported file") + ")" });
            } else {
              rows.push({ label: r.file_name, text: "rejected - " + (r.reason || "unknown reason") });
            }
          });
          renderStatus(rows);
          updateSpectrumLayersTray();
          drawUVVisMultiSpectrum();
        } catch (err) {
          renderStatus([{ label: "Import failed", text: String((err && err.message) || err) }]);
        } finally {
          uvImportBtn.disabled = false;
        }
      });
    }

    if (irViewportSelect) {
      irViewportSelect.addEventListener("change", (e) => {
        irViewportMode = e.target.value;
        if (irCustomRangeWrap) {
          irCustomRangeWrap.style.display = irViewportMode === "custom" ? "flex" : "none";
          irCustomRangeWrap.classList.toggle("hidden", irViewportMode !== "custom");
        }
        drawIRMultiSpectrum();
      });
    }

    if (irMinWnInp) irMinWnInp.addEventListener("input", drawIRMultiSpectrum);
    if (irMaxWnInp) irMaxWnInp.addEventListener("input", drawIRMultiSpectrum);

    // Manual Axis Range Controls (IR)
    const irManualToggle = document.getElementById("ir-manual-range-toggle");
    const irManualInputs = document.getElementById("ir-manual-axis-inputs");
    const irApplyAxisBtn = document.getElementById("ir-apply-axis-btn");
    const irAutoAxisBtn = document.getElementById("ir-auto-axis-btn");
    const irMinXInp = document.getElementById("ir-manual-min-x");
    const irMaxXInp = document.getElementById("ir-manual-max-x");
    const irMinYInp = document.getElementById("ir-manual-min-y");
    const irMaxYInp = document.getElementById("ir-manual-max-y");

    if (irManualToggle) {
      irManualToggle.addEventListener("change", (e) => {
        irManualRangeEnabled = e.target.checked;
        if (irManualInputs) irManualInputs.style.display = irManualRangeEnabled ? "flex" : "none";
        drawIRMultiSpectrum();
      });
    }
    if (irApplyAxisBtn) {
      irApplyAxisBtn.addEventListener("click", () => {
        const minX = parseFloat(irMinXInp?.value);
        const maxX = parseFloat(irMaxXInp?.value);
        const minY = parseFloat(irMinYInp?.value);
        const maxY = parseFloat(irMaxYInp?.value);
        if (!isNaN(minX)) irManualMinX = minX;
        if (!isNaN(maxX)) irManualMaxX = maxX;
        if (!isNaN(minY)) irManualMinY = minY;
        if (!isNaN(maxY)) irManualMaxY = maxY;
        irManualRangeEnabled = true;
        if (irManualToggle) irManualToggle.checked = true;
        if (irManualInputs) irManualInputs.style.display = "flex";
        drawIRMultiSpectrum();
        showToast("Applied manual axis limits to IR chart.");
      });
    }
    if (irAutoAxisBtn) {
      irAutoAxisBtn.addEventListener("click", () => {
        irManualRangeEnabled = false;
        irManualMinX = null;
        irManualMaxX = null;
        irManualMinY = null;
        irManualMaxY = null;
        if (irManualToggle) irManualToggle.checked = false;
        if (irManualInputs) irManualInputs.style.display = "none";
        if (irMinXInp) irMinXInp.value = "";
        if (irMaxXInp) irMaxXInp.value = "";
        if (irMinYInp) irMinYInp.value = "";
        if (irMaxYInp) irMaxYInp.value = "";
        drawIRMultiSpectrum();
        showToast("Reset IR axes to Auto Range.");
      });
    }

    if (irToggleAllExp) {
      irToggleAllExp.addEventListener("change", (e) => {
        loadedExperimentalIRSpectra.forEach(exp => exp.visible = e.target.checked);
        updateIRSpectrumLayersTray();
        drawIRMultiSpectrum();
      });
    }

    const irLegendToggleBtn = document.getElementById("engine-ir-legend-toggle");
    const irLegendCb = document.getElementById("ir-toggle-legend");
    const setIRLegend = (show) => {
      showIRLegend = show;
      if (irLegendCb) irLegendCb.checked = show;
      if (irLegendToggleBtn) irLegendToggleBtn.textContent = show ? "🏷️ Hide Legend" : "🏷️ Show Legend";
      drawIRMultiSpectrum();
    };
    if (irLegendToggleBtn) {
      irLegendToggleBtn.addEventListener("click", () => setIRLegend(!showIRLegend));
    }
    if (irLegendCb) {
      irLegendCb.addEventListener("change", (e) => setIRLegend(e.target.checked));
    }

    if (irToggleAllTheo) {
      irToggleAllTheo.addEventListener("change", (e) => {
        loadedTheoreticalIRSpectra.forEach(theo => theo.visible = e.target.checked);
        updateIRSpectrumLayersTray();
        drawIRMultiSpectrum();
      });
    }

    if (irToggleSticks) {
      irToggleSticks.addEventListener("change", (e) => {
        showTheoreticalIRSticks = e.target.checked;
        drawIRMultiSpectrum();
      });
    }

    // Interactive Hover Tooltip for IR Canvas
    if (irCanvas && irTooltip) {
      irCanvas.addEventListener("mousemove", (e) => {
        const activeTheos = loadedTheoreticalIRSpectra.filter(t => t.visible && irViewAllows("theo"));
        const activeExps = loadedExperimentalIRSpectra.filter(e => e.visible && irViewAllows("exp"));
        if (!activeTheos.length && !activeExps.length) {
          irTooltip.classList.add("hidden");
          return;
        }

        const rect = irCanvas.getBoundingClientRect();
        const canvasRectW = rect.width || irCanvas.clientWidth || irCanvas.width || 1000;
        const canvasRectH = rect.height || irCanvas.clientHeight || irCanvas.height || 380;
        const mouseX = (e.clientX != null && rect.left != null) ? (e.clientX - rect.left) : (e.offsetX || 300);
        const mouseY = (e.clientY != null && rect.top != null) ? (e.clientY - rect.top) : (e.offsetY || 150);
        const scaleX = irCanvas.width / canvasRectW;
        const scaleY = irCanvas.height / canvasRectH;
        const canvasX = mouseX * scaleX;
        const canvasY = mouseY * scaleY;

        const padding = getIRPadding();
        const plotW = irCanvas.width - padding.left - padding.right;
        const plotH = irCanvas.height - padding.top - padding.bottom;

        if (canvasX < padding.left || canvasX > irCanvas.width - padding.right || canvasY < padding.top || canvasY > irCanvas.height - padding.bottom) {
          irTooltip.classList.add("hidden");
          return;
        }

        const { minWn, maxWn } = getIRBounds(activeTheos, activeExps);
        const wnHover = maxWn - ((canvasX - padding.left) / plotW) * (maxWn - minWn);

        let tooltipHtml = `<div style="font-weight:700; border-bottom:1px solid rgba(255,255,255,0.2); margin-bottom:4px; padding-bottom:2px;">Wavenumber (ν̃): ${wnHover.toFixed(2)} cm⁻¹</div>`;
        let hasReadings = false;

        const scaleFactor = parseFloat(irScaleSlider?.value || "1.0");
        const fwhm = parseFloat(irFwhmSlider?.value || "15.0");
        const shift = parseFloat(irShiftSlider?.value || "0");
        const sharedNormMax = getIRSharedNormMax(scaleFactor, fwhm, shift, minWn, maxWn);

        // Precise theoretical calculation/interpolation
        activeTheos.forEach(theo => {
          const curve = computeIRConvolution(theo.modes, scaleFactor, fwhm, shift, minWn, maxWn, 2, sharedNormMax);
          const ptAbs = interpolatePoint(curve, wnHover, "wavenumber_cm", "absorbance");
          const ptTrans = interpolatePoint(curve, wnHover, "wavenumber_cm", "transmittance_pct");
          const ptNorm = interpolatePoint(curve, wnHover, "wavenumber_cm", "absorbance_norm");

          if (ptAbs !== null && !isNaN(ptAbs)) {
            hasReadings = true;
            let valStr = "";
            if (irYAxisMode === "theory_intensity") {
              const maxModeInt = theo.modes.length ? Math.max(...theo.modes.map(m => m.intensity_km_mol || 0), 10) : 100;
              valStr = `${((ptNorm !== null ? ptNorm : ptAbs) * maxModeInt).toFixed(2)} km/mol`;
            } else if (irYAxisMode === "transmittance") {
              valStr = `${(ptTrans !== null ? ptTrans : (100 * Math.pow(10, -ptAbs))).toFixed(2)} %T`;
            } else {
              valStr = `Abs = ${ptAbs.toFixed(4)} AU`;
            }
            tooltipHtml += `<div style="display:flex; align-items:center; gap:6px; margin:2px 0;"><span style="color:${theo.color}; font-size:1.1em;">●</span> <span>${escapeHtml(theo.name)}:</span> <strong>${valStr}</strong></div>`;
          }
        });

        // Precise experimental interpolation
        activeExps.forEach(exp => {
          const ptAbs = interpolatePoint(exp.raw_data, wnHover, "wavenumber_cm", "absorbance");
          const ptTrans = interpolatePoint(exp.raw_data, wnHover, "wavenumber_cm", "transmittance_pct");

          if (ptAbs !== null && !isNaN(ptAbs)) {
            hasReadings = true;
            let valStr = "";
            if (irYAxisMode === "transmittance") {
              const transVal = (ptTrans !== null && !isNaN(ptTrans)) ? ptTrans : (100 * Math.pow(10, -ptAbs));
              valStr = `${transVal.toFixed(2)} %T`;
            } else {
              valStr = `Abs = ${ptAbs.toFixed(4)} AU`;
            }
            tooltipHtml += `<div style="display:flex; align-items:center; gap:6px; margin:2px 0;"><span style="color:${exp.color}; font-size:1.1em;">■</span> <span>${escapeHtml(exp.label || exp.file_name)}:</span> <strong>${valStr}</strong></div>`;
          }
        });

        if (hasReadings) {
          irTooltip.innerHTML = tooltipHtml;
          const tooltipWidth = 240;
          const posX = (mouseX + 15 + tooltipWidth > rect.width) ? (mouseX - tooltipWidth - 10) : (mouseX + 15);
          const posY = Math.max(10, Math.min(mouseY - 20, rect.height - 80));
          irTooltip.style.left = `${posX}px`;
          irTooltip.style.top = `${posY}px`;
          irTooltip.classList.remove("hidden");
        } else {
          irTooltip.classList.add("hidden");
        }
      });

      irCanvas.addEventListener("mouseleave", () => {
        irTooltip.classList.add("hidden");
      });
    }

    // High-Res IR PNG Download
    if (irDownloadPngBtn) {
      irDownloadPngBtn.addEventListener("click", () => {
        const activeTheos = loadedTheoreticalIRSpectra.filter(t => t.visible && irViewAllows("theo"));
        const activeExps = loadedExperimentalIRSpectra.filter(e => e.visible && irViewAllows("exp"));
        if (!activeTheos.length && !activeExps.length) {
          showToast("No IR spectrum data available to export.");
          return;
        }

        const scale = 2.5;
        const offCanvas = document.createElement("canvas");
        offCanvas.width = 1000 * scale;
        offCanvas.height = 380 * scale;

        renderIRSpectrumToCanvas(offCanvas, scale, true);

        offCanvas.toBlob((blob) => {
          if (!blob) return;
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          const name = currentEngineData?.name || "ir_spectrum";
          a.download = `${name.replace(/\.[^/.]+$/, "")}_publication_ir.png`;
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          showToast("High-resolution publication IR spectrum PNG saved.");
        }, "image/png");
      });
    }

    // IR CSV Export
    if (irExportCsvBtn) {
      irExportCsvBtn.addEventListener("click", () => {
        const activeTheos = loadedTheoreticalIRSpectra.filter(t => t.visible && irViewAllows("theo"));
        const activeExps = loadedExperimentalIRSpectra.filter(e => e.visible && irViewAllows("exp"));

        if (!activeTheos.length && !activeExps.length) {
          showToast("No active IR spectrum layers to export.");
          return;
        }

        const scaleFactor = parseFloat(irScaleSlider?.value || "1.0");
        const fwhm = parseFloat(irFwhmSlider?.value || "15.0");
        const shift = parseFloat(irShiftSlider?.value || "0");

        let csv = "Wavenumber_cm-1";
        activeTheos.forEach(t => {
          csv += `,"Theory_${t.name.replace(/,/g, '_')}_${irYAxisMode === 'transmittance' ? 'Transmittance_pct' : 'Absorbance'}"`;
        });
        activeExps.forEach(e => {
          csv += `,"Exp_${e.file_name.replace(/,/g, '_')}_${irYAxisMode === 'transmittance' ? 'Transmittance_pct' : 'Absorbance'}"`;
        });
        csv += "\n";

        for (let wn = 4000; wn >= 400; wn -= 2) {
          let row = `${wn}`;
          activeTheos.forEach(t => {
            const curve = computeIRConvolution(t.modes, scaleFactor, fwhm, shift, wn, wn, 2);
            const val = curve[0] ? (irYAxisMode === 'transmittance' ? curve[0].transmittance_pct : curve[0].absorbance) : 0;
            row += `,${val}`;
          });
          activeExps.forEach(e => {
            const pt = e.raw_data.find(p => Math.abs(p.wavenumber_cm - wn) < 1.0);
            row += `,${pt ? (irYAxisMode === 'transmittance' ? pt.transmittance_pct : pt.absorbance) : ''}`;
          });
          csv += `${row}\n`;
        }

        const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `ir_multi_spectrum_${Date.now()}.csv`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        showToast("Exported multi-spectrum IR dataset to CSV.");
      });
    }

    const dlOrbPngBtn = document.getElementById("engine-orbitals-download-png");
    if (dlOrbPngBtn) {
      dlOrbPngBtn.addEventListener("click", () => {
        if (!currentEngineData) {
          showToast("No calculation loaded.");
          return;
        }
        const job = currentEngineData.latest_job || {};
        const molName = currentEngineData.name || "Molecule";

        // Extract and calculate physical values
        const homoVal = job.homo_ev !== undefined ? job.homo_ev : (job.homo_energy_ev !== undefined ? job.homo_energy_ev : null);
        const lumoVal = job.lumo_ev !== undefined ? job.lumo_ev : (job.lumo_energy_ev !== undefined ? job.lumo_energy_ev : null);
        const gapVal = job.homo_lumo_gap_ev !== undefined ? job.homo_lumo_gap_ev : (homoVal !== null && lumoVal !== null ? Math.abs(lumoVal - homoVal) : null);

        const ipVal = job.ionization_potential_ev !== undefined ? job.ionization_potential_ev : (homoVal !== null ? -homoVal : null);
        const eaVal = job.electron_affinity_ev !== undefined ? job.electron_affinity_ev : (lumoVal !== null ? -lumoVal : null);
        const hardnessVal = job.chemical_hardness_ev !== undefined ? job.chemical_hardness_ev : (ipVal !== null && eaVal !== null ? (ipVal - eaVal) / 2 : null);
        const softnessVal = job.chemical_softness_ev !== undefined ? job.chemical_softness_ev : (hardnessVal !== null && hardnessVal > 0 ? 1 / (2 * hardnessVal) : null);
        const electronegativityVal = job.electronegativity_ev !== undefined ? job.electronegativity_ev : (ipVal !== null && eaVal !== null ? (ipVal + eaVal) / 2 : null);
        const potentialVal = job.chemical_potential_ev !== undefined ? job.chemical_potential_ev : (electronegativityVal !== null ? -electronegativityVal : null);
        const electrophilicityVal = job.electrophilicity_index_ev !== undefined ? job.electrophilicity_index_ev : (potentialVal !== null && hardnessVal !== null && hardnessVal > 0 ? (potentialVal * potentialVal) / (2 * hardnessVal) : null);

        // High-DPI Canvas for 300+ DPI publication printing
        const canvas = document.createElement("canvas");
        canvas.width = 2400;
        canvas.height = 1450;
        const ctx = canvas.getContext("2d");

        // Helper: Rounded Rectangles
        function drawRoundedRect(c, x, y, width, height, radius, fillStyle, strokeStyle, lineWidth = 1) {
          c.beginPath();
          c.moveTo(x + radius, y);
          c.lineTo(x + width - radius, y);
          c.quadraticCurveTo(x + width, y, x + width, y + radius);
          c.lineTo(x + width, y + height - radius);
          c.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
          c.lineTo(x + radius, y + height);
          c.quadraticCurveTo(x, y + height, x, y + height - radius);
          c.lineTo(x, y + radius);
          c.quadraticCurveTo(x, y, x + radius, y);
          c.closePath();
          if (fillStyle) {
            c.fillStyle = fillStyle;
            c.fill();
          }
          if (strokeStyle) {
            c.strokeStyle = strokeStyle;
            c.lineWidth = lineWidth;
            c.stroke();
          }
        }

        // Helper: Arrow
        function drawDoubleArrow(c, x, y1, y2, headLen = 10, color = "#dc2626", lineWidth = 2) {
          c.save();
          c.strokeStyle = color;
          c.fillStyle = color;
          c.lineWidth = lineWidth;
          c.beginPath();
          c.moveTo(x, y1);
          c.lineTo(x, y2);
          c.stroke();

          // Top arrowhead
          c.beginPath();
          c.moveTo(x, y1);
          c.lineTo(x - headLen, y1 + headLen * 1.4);
          c.lineTo(x + headLen, y1 + headLen * 1.4);
          c.closePath();
          c.fill();

          // Bottom arrowhead
          c.beginPath();
          c.moveTo(x, y2);
          c.lineTo(x - headLen, y2 - headLen * 1.4);
          c.lineTo(x + headLen, y2 - headLen * 1.4);
          c.closePath();
          c.fill();
          c.restore();
        }

        // 1. Clean White Background
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, 2400, 1450);

        // Outer publication frame
        drawRoundedRect(ctx, 40, 40, 2320, 1370, 16, "#ffffff", "#e2e8f0", 2);

        // 2. Publication Header
        ctx.textAlign = "left";
        ctx.fillStyle = "#0f172a";
        ctx.font = "bold 36px 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
        ctx.fillText(`Frontier Molecular Orbitals & Conceptual DFT Descriptors - ${molName}`, 80, 105);

        // Dynamic engine version extraction from parsed calculation output file
        let orcaVer = job.orca_version || currentEngineData?.orca_version || (currentEngineData?.latest_job && currentEngineData?.latest_job?.orca_version);
        if (!orcaVer || orcaVer === "Unknown" || orcaVer === "undefined") {
          const raw = currentEngineData?.raw_text || currentEngineData?.content || currentEngineData?.output || "";
          const vMatch = raw.match(/Program\s+Version\s+([\d.]+)/i) || raw.match(/ORCA[^\d\n]*([\d.]+)/i);
          if (vMatch) orcaVer = vMatch[1];
        }
        let engineLabel = "ORCA";
        if (orcaVer && orcaVer !== "Unknown") {
          engineLabel = orcaVer.toLowerCase().startsWith("orca") ? orcaVer : `ORCA ${orcaVer}`;
        } else {
          engineLabel = "ORCA";
        }

        // Metadata chips
        const metaChips = [
          `Formula: ${job.formula || job.chemical_formula || currentEngineData?.formula || "-"}`,
          `Theory: ${job.method || job.functional || 'DFT'} / ${job.basis_set || 'Def2-TZVP'}`,
          `Solvation: ${job.solvation || (job.solvation_model ? `${job.solvation_model} (${job.solvent || ''})` : job.solvent) || 'Gas Phase'}`,
          `Engine: ${engineLabel}`
        ];

        let chipX = 80;
        metaChips.forEach(chip => {
          ctx.font = "500 16px 'Inter', sans-serif";
          const tw = ctx.measureText(chip).width;
          drawRoundedRect(ctx, chipX, 125, tw + 28, 36, 18, "#f1f5f9", "#cbd5e1", 1);
          ctx.fillStyle = "#475569";
          ctx.fillText(chip, chipX + 14, 149);
          chipX += tw + 40;
        });

        // ─────────────────────────────────────────────────────────────
        // LEFT PANEL: Frontier Orbitals Energy Ladder
        // ─────────────────────────────────────────────────────────────
        const leftPanelX = 80;
        const leftPanelY = 190;
        const leftPanelW = 1070;
        const leftPanelH = 1150;

        drawRoundedRect(ctx, leftPanelX, leftPanelY, leftPanelW, leftPanelH, 12, "#ffffff", "#e2e8f0", 1.5);
        drawRoundedRect(ctx, leftPanelX, leftPanelY, leftPanelW, 68, 12, "#f8fafc", "#e2e8f0", 1.5);

        ctx.fillStyle = "#0f172a";
        ctx.font = "bold 22px 'Inter', sans-serif";
        ctx.fillText("Frontier Molecular Orbital Energy Ladder", leftPanelX + 30, leftPanelY + 42);

        ctx.fillStyle = "#64748b";
        ctx.font = "500 15px 'Inter', sans-serif";
        ctx.textAlign = "right";
        ctx.fillText("Eigenvalues in eV", leftPanelX + leftPanelW - 30, leftPanelY + 42);
        ctx.textAlign = "left";

        // Vertical Energy Axis
        const axisX = leftPanelX + 110;
        const axisTopY = leftPanelY + 140;
        const axisBottomY = leftPanelY + 1020;

        ctx.strokeStyle = "#94a3b8";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(axisX, axisBottomY);
        ctx.lineTo(axisX, axisTopY);
        ctx.stroke();

        // Axis Top Arrow
        ctx.fillStyle = "#475569";
        ctx.beginPath();
        ctx.moveTo(axisX, axisTopY - 10);
        ctx.lineTo(axisX - 6, axisTopY + 4);
        ctx.lineTo(axisX + 6, axisTopY + 4);
        ctx.closePath();
        ctx.fill();

        ctx.fillStyle = "#334155";
        ctx.font = "bold 16px 'Inter', sans-serif";
        ctx.fillText("Energy (eV)", axisX - 35, axisTopY - 22);

        // Energy axis tick marks
        const ticks = [4, 2, 0, -2, -4, -6, -8, -10];
        const minScaleE = -10, maxScaleE = 4;
        ticks.forEach(tVal => {
          const normY = axisBottomY - ((tVal - minScaleE) / (maxScaleE - minScaleE)) * (axisBottomY - axisTopY);
          ctx.strokeStyle = "#cbd5e1";
          ctx.lineWidth = 1.5;
          ctx.beginPath();
          ctx.moveTo(axisX - 8, normY);
          ctx.lineTo(axisX + 8, normY);
          ctx.stroke();

          ctx.fillStyle = tVal === 0 ? "#0f172a" : "#64748b";
          ctx.font = tVal === 0 ? "bold 14px 'Inter', monospace" : "13px 'Inter', monospace";
          ctx.textAlign = "right";
          ctx.fillText(`${tVal > 0 ? '+' : ''}${tVal}.0`, axisX - 14, normY + 5);

          // Vacuum Zero reference line
          if (tVal === 0) {
            ctx.save();
            ctx.setLineDash([6, 6]);
            ctx.strokeStyle = "#94a3b8";
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(axisX + 15, normY);
            ctx.lineTo(leftPanelX + leftPanelW - 40, normY);
            ctx.stroke();

            ctx.fillStyle = "#64748b";
            ctx.font = "italic 13px 'Inter', sans-serif";
            ctx.textAlign = "right";
            ctx.fillText("Vacuum Level (0.00 eV)", leftPanelX + leftPanelW - 45, normY - 8);
            ctx.restore();
          }
        });
        ctx.textAlign = "left";

        // Orbital Level Positions
        const lumoY = leftPanelY + 310;
        const homoY = leftPanelY + 770;
        const barX = axisX + 45;
        const barW = leftPanelW - (barX - leftPanelX) - 50;
        const barH = 76;

        // 1. LUMO Level (Virtual Orbital - Sapphire / Cobalt)
        drawRoundedRect(ctx, barX, lumoY, barW, barH, 10, "#0284c7", "#0369a1", 1.5);

        // LUMO Title & Value (Strictly "LUMO", no "(Virtual)")
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 26px 'Inter', sans-serif";
        ctx.fillText("LUMO", barX + 30, lumoY + 47);

        ctx.font = "bold 24px 'JetBrains Mono', Consolas, monospace";
        ctx.textAlign = "right";
        ctx.fillText(`${lumoVal !== null ? lumoVal.toFixed(3) : "-"} eV`, barX + barW - 30, lumoY + 47);
        ctx.textAlign = "left";

        // 2. HOMO-LUMO Gap Dimension & Callout
        const gapCenterX = barX + barW / 2;
        const gapMidY = (lumoY + barH + homoY) / 2;

        // Gap dimension line with dual arrows
        drawDoubleArrow(ctx, gapCenterX, lumoY + barH + 12, homoY - 12, 10, "#dc2626", 2.5);

        // Gap Pill Badge
        const gapPillW = 340;
        const gapPillH = 88;
        drawRoundedRect(ctx, gapCenterX - gapPillW / 2, gapMidY - gapPillH / 2, gapPillW, gapPillH, 20, "#fff5f5", "#fca5a5", 2);

        ctx.textAlign = "center";
        ctx.fillStyle = "#dc2626";
        ctx.font = "bold 24px 'Inter', sans-serif";
        ctx.fillText(`ΔEgap = ${gapVal !== null ? gapVal.toFixed(3) : "-"} eV`, gapCenterX, gapMidY - 10);

        if (gapVal !== null && gapVal > 0) {
          const kcalGap = (gapVal * 23.0605).toFixed(2);
          const nmEdge = (1239.84193 / gapVal).toFixed(1);
          ctx.fillStyle = "#991b1b";
          ctx.font = "500 15px 'Inter', sans-serif";
          ctx.fillText(`${kcalGap} kcal/mol  •  λedge = ${nmEdge} nm`, gapCenterX, gapMidY + 22);
        }
        ctx.textAlign = "left";

        // 3. HOMO Level (Occupied Orbital - Rich Terracotta / Amber)
        drawRoundedRect(ctx, barX, homoY, barW, barH, 10, "#d97706", "#b45309", 1.5);

        // Spin-paired electron representation (Antiparallel arrows)
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 26px 'Inter', sans-serif";
        ctx.fillText("HOMO", barX + 30, homoY + 47);

        // Electron occupancy indicator: ⥮
        ctx.font = "bold 26px 'Inter', sans-serif";
        ctx.fillStyle = "#fef3c7";
        ctx.fillText("  ⥮", barX + 115, homoY + 47);

        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 24px 'JetBrains Mono', Consolas, monospace";
        ctx.textAlign = "right";
        ctx.fillText(`${homoVal !== null ? homoVal.toFixed(3) : "-"} eV`, barX + barW - 30, homoY + 47);
        ctx.textAlign = "left";

        // Bottom Orbital Stats Card
        const orbCardY = leftPanelY + leftPanelH - 140;
        drawRoundedRect(ctx, leftPanelX + 30, orbCardY, leftPanelW - 60, 100, 8, "#f8fafc", "#e2e8f0", 1);

        ctx.fillStyle = "#334155";
        ctx.font = "bold 15px 'Inter', sans-serif";
        ctx.fillText("Orbital Summary & Boundary Metrics", leftPanelX + 50, orbCardY + 34);

        ctx.font = "500 14px 'Inter', sans-serif";
        ctx.fillStyle = "#64748b";
        const kjGap = gapVal !== null ? (gapVal * 96.485).toFixed(1) : "-";
        ctx.fillText(`HOMO-LUMO Energy Difference: ${gapVal !== null ? gapVal.toFixed(4) : "-"} eV (${kjGap} kJ/mol)`, leftPanelX + 50, orbCardY + 66);

        // ─────────────────────────────────────────────────────────────
        // RIGHT PANEL: Conceptual DFT Reactivity Descriptors
        // ─────────────────────────────────────────────────────────────
        const rightPanelX = 1190;
        const rightPanelY = 190;
        const rightPanelW = 1130;
        const rightPanelH = 1150;

        drawRoundedRect(ctx, rightPanelX, rightPanelY, rightPanelW, rightPanelH, 12, "#ffffff", "#e2e8f0", 1.5);
        drawRoundedRect(ctx, rightPanelX, rightPanelY, rightPanelW, 68, 12, "#f8fafc", "#e2e8f0", 1.5);

        ctx.fillStyle = "#0f172a";
        ctx.font = "bold 22px 'Inter', sans-serif";
        ctx.fillText("Conceptual DFT (CDFT) Reactivity Descriptors", rightPanelX + 30, rightPanelY + 42);

        ctx.fillStyle = "#64748b";
        ctx.font = "500 15px 'Inter', sans-serif";
        ctx.textAlign = "right";
        ctx.fillText("Global Reactivity Indices", rightPanelX + rightPanelW - 30, rightPanelY + 42);
        ctx.textAlign = "left";

        // CDFT Table Header
        const tableX = rightPanelX + 30;
        const tableY = rightPanelY + 100;
        const tableW = rightPanelW - 60;
        const headerH = 52;

        drawRoundedRect(ctx, tableX, tableY, tableW, headerH, 6, "#f1f5f9", "#cbd5e1", 1);

        ctx.fillStyle = "#334155";
        ctx.font = "bold 15px 'Inter', sans-serif";
        ctx.fillText("Reactivity Descriptor", tableX + 24, tableY + 33);
        ctx.textAlign = "center";
        ctx.fillText("Symbol", tableX + 410, tableY + 33);
        ctx.textAlign = "right";
        ctx.fillText("Value", tableX + 680, tableY + 33);
        ctx.textAlign = "left";
        ctx.fillText("Operational Formula", tableX + 740, tableY + 33);

        // CDFT Rows
        const cdftRows = [
          { name: "Chemical Hardness", symbol: "η", value: hardnessVal !== null ? `${hardnessVal.toFixed(3)} eV` : "-", formula: "(I - A) / 2", desc: "Resistance to charge transfer" },
          { name: "Chemical Softness", symbol: "S", value: softnessVal !== null ? `${softnessVal.toFixed(3)} eV⁻¹` : "-", formula: "1 / (2η)", desc: "Polarizability & reactivity capacity" },
          { name: "Electronegativity", symbol: "χ", value: electronegativityVal !== null ? `${electronegativityVal.toFixed(3)} eV` : "-", formula: "(I + A) / 2", desc: "Mulliken global electronegativity" },
          { name: "Chemical Potential", symbol: "μ", value: potentialVal !== null ? `${potentialVal.toFixed(3)} eV` : "-", formula: "-χ", desc: "Escaping tendency of electrons" },
          { name: "Electrophilicity Index", symbol: "ω", value: electrophilicityVal !== null ? `${electrophilicityVal.toFixed(3)} eV` : "-", formula: "μ² / (2η)", desc: "Energy stabilization capacity" },
          { name: "Ionization Potential", symbol: "I", value: ipVal !== null ? `${ipVal.toFixed(3)} eV` : "-", formula: "-E(HOMO)", desc: "Energy required to remove electron" },
          { name: "Electron Affinity", symbol: "A", value: eaVal !== null ? `${eaVal.toFixed(3)} eV` : "-", formula: "-E(LUMO)", desc: "Energy released upon electron addition" },
        ];

        let curRowY = tableY + headerH + 10;
        const rowH = 92;

        cdftRows.forEach((r, idx) => {
          const isEven = idx % 2 === 0;
          drawRoundedRect(ctx, tableX, curRowY, tableW, rowH, 6, isEven ? "#ffffff" : "#f8fafc", "#e2e8f0", 1);

          // Name & Description
          ctx.fillStyle = "#0f172a";
          ctx.font = "bold 16px 'Inter', sans-serif";
          ctx.fillText(r.name, tableX + 24, curRowY + 36);

          ctx.fillStyle = "#64748b";
          ctx.font = "13px 'Inter', sans-serif";
          ctx.fillText(r.desc, tableX + 24, curRowY + 64);

          // Symbol
          ctx.textAlign = "center";
          ctx.fillStyle = "#0284c7";
          ctx.font = "bold 20px 'Inter', Georgia, serif";
          ctx.fillText(r.symbol, tableX + 410, curRowY + 50);

          // Value (Monospace)
          ctx.textAlign = "right";
          ctx.fillStyle = "#0f172a";
          ctx.font = "bold 18px 'JetBrains Mono', Consolas, monospace";
          ctx.fillText(r.value, tableX + 680, curRowY + 50);

          // Formula
          ctx.textAlign = "left";
          ctx.fillStyle = "#475569";
          ctx.font = "500 15px 'JetBrains Mono', Consolas, monospace";
          ctx.fillText(r.formula, tableX + 740, curRowY + 50);

          curRowY += rowH + 8;
        });

        // CDFT Reference Footnote Card (Clean definitions without literature citations)
        const footY = rightPanelY + rightPanelH - 120;
        drawRoundedRect(ctx, tableX, footY, tableW, 80, 8, "#f8fafc", "#e2e8f0", 1);

        ctx.fillStyle = "#334155";
        ctx.font = "bold 14px 'Inter', sans-serif";
        ctx.fillText("Approximation & Fundamental Relations", tableX + 24, footY + 32);

        ctx.font = "13px 'Inter', sans-serif";
        ctx.fillStyle = "#64748b";
        ctx.fillText("Finite-difference approximations: Ionization potential I ≈ -E(HOMO) and Electron affinity A ≈ -E(LUMO).", tableX + 24, footY + 58);

        // Download PNG directly without bottom watermark/citations banner
        const imgUri = canvas.toDataURL("image/png");
        const a = document.createElement("a");
        a.href = imgUri;
        a.download = `${molName}_Frontier_Orbitals_and_CDFT.png`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        showToast(`Exported publication diagram for <strong>${molName}</strong>.`);
      });
    }

    // Full JSON Export
    const exportJsonBtn = document.getElementById("engine-btn-export-json");
    if (exportJsonBtn) {
      exportJsonBtn.addEventListener("click", () => {
        if (!currentEngineData) return;
        const blob = new Blob([JSON.stringify(currentEngineData, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${currentEngineData.name || "orca"}_parsed_data.json`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      });
    }
  }
  initQuantumEngine();

    // ───────────────────────────────────────────────
  // Reaction Thermochemistry Controller (Two-Slot Multi-Level & Conditions)
  // ───────────────────────────────────────────────
  function initReactionThermochemistry() {
    const form = document.getElementById("thermo-form");
    const eqDisplay = document.getElementById("thermo-live-equation");
    const eqInput = document.getElementById("thermo-equation");
    const reactantsList = document.getElementById("thermo-reactants-list");
    const productsList = document.getElementById("thermo-products-list");
    const addReactantBtn = document.getElementById("thermo-add-reactant-btn");
    const addProductBtn = document.getElementById("thermo-add-product-btn");
    const archiveDropzone = document.getElementById("thermo-archive-dropzone");
    const archiveFileInput = document.getElementById("thermo-archive-file-input");
    const archiveBrowseBtn = document.getElementById("thermo-archive-browse-btn");
    const archiveStatus = document.getElementById("thermo-archive-status");
    const loadingEl = document.getElementById("thermo-loading");
    const errorEl = document.getElementById("thermo-error");
    const resultCard = document.getElementById("thermo-result");

    const customCondCheckbox = document.getElementById("thermo-enable-custom-conditions");
    const customParamsInputs = document.getElementById("thermo-custom-params-inputs");
    const customTempInput = document.getElementById("thermo-temp");
    const customPressInput = document.getElementById("thermo-pressure");

    if (customCondCheckbox && customParamsInputs) {
      customCondCheckbox.addEventListener("change", () => {
        const isEnabled = customCondCheckbox.checked;
        customParamsInputs.style.opacity = isEnabled ? "1" : "0.6";
        customParamsInputs.style.pointerEvents = isEnabled ? "auto" : "none";
      });
    }

    // Internal reactive state for Reactants and Products
    const state = {
      reactants: [
        {
          id: "r1",
          coeff: 1,
          name: "Reactant 1",
          content: "",
          primaryName: "",
          primaryMode: "upload",
          sp_content: "",
          spName: "",
          spMode: "upload"
        }
      ],
      products: [
        {
          id: "p1",
          coeff: 1,
          name: "Product 1",
          content: "",
          primaryName: "",
          primaryMode: "upload",
          sp_content: "",
          spName: "",
          spMode: "upload"
        }
      ],
      extractedArchiveFiles: {}
    };

    function updateLiveEquation() {
      const rParts = state.reactants
        .filter(r => r.name.trim())
        .map(r => `${r.coeff > 1 ? r.coeff + ' ' : ''}${r.name.trim()}`);
      const pParts = state.products
        .filter(p => p.name.trim())
        .map(p => `${p.coeff > 1 ? p.coeff + ' ' : ''}${p.name.trim()}`);

      const rStr = rParts.length > 0 ? rParts.join(" + ") : "Reactants";
      const pStr = pParts.length > 0 ? pParts.join(" + ") : "Products";
      const fullEq = `${rStr} -> ${pStr}`;
      const prettyEq = `${rStr} ➔ ${pStr}`;

      if (eqDisplay) eqDisplay.textContent = prettyEq;
      if (eqInput) eqInput.value = fullEq;
    }

    function renderSpeciesCards(container, list, isReactant) {
      if (!container) return;
      container.innerHTML = "";
      const savedJobs = loadJobs();

      list.forEach((item, index) => {
        const card = document.createElement("div");
        card.className = "species-card-item";
        card.dataset.id = item.id;

        let jobsOptions = '<option value="">-- Choose Kaggle job --</option>';
        savedJobs.forEach(j => {
          jobsOptions += `<option value="${j.jobId || j.id}">${j.name || j.jobId}</option>`;
        });

        // Determine status badge
        let statusBadgeHtml = '';
        if (item.content && item.sp_content) {
          statusBadgeHtml = `<span class="species-status-badge badge-primary">⚡ Multi-Level (SP + Freq)</span>`;
        } else if (item.content) {
          statusBadgeHtml = `<span class="species-status-badge badge-success">✓ Single-Level Ready</span>`;
        } else {
          statusBadgeHtml = `<span class="species-status-badge badge-neutral">No Opt/Freq File</span>`;
        }

        card.innerHTML = `
          <div class="species-card-header">
            <label class="field-hint" style="margin:0;">Coeff:</label>
            <input type="number" class="species-coeff-input" value="${item.coeff}" min="1" step="1">
            <input type="text" class="species-name-input" value="${item.name}" placeholder="e.g. ${isReactant ? 'Reactant ' + (index + 1) : 'Product ' + (index + 1)}">
            ${statusBadgeHtml}
            ${list.length > 1 ? `<button type="button" class="species-delete-btn" title="Remove Species">✕</button>` : ''}
          </div>

          <div class="species-dual-slots-container">
            <!-- Slot 1: Primary Opt/Freq File (Mandatory) -->
            <div class="species-slot-box primary-slot ${item.content ? 'has-file' : ''}">
              <div class="slot-header-bar">
                <span class="slot-badge-req">File 1 (Required)</span>
                <strong class="slot-title">Geometry &amp; Frequencies (Opt/Freq)</strong>
              </div>
              <div class="species-source-tabs">
                <button type="button" class="btn-source-tab ${item.primaryMode === 'upload' ? 'active' : ''}" data-slot="primary" data-mode="upload">📁 File Upload</button>
                <button type="button" class="btn-source-tab ${item.primaryMode === 'job' ? 'active' : ''}" data-slot="primary" data-mode="job">⚡ Kaggle Job</button>
                <button type="button" class="btn-source-tab ${item.primaryMode === 'paste' ? 'active' : ''}" data-slot="primary" data-mode="paste">📝 Paste Text</button>
              </div>
              <div class="slot-content-panel">
                ${item.primaryMode === 'job' ? `
                  <select class="select-small primary-job-select">${jobsOptions}</select>
                ` : item.primaryMode === 'paste' ? `
                  <textarea class="mono primary-paste-area" rows="2" placeholder="Paste ORCA Opt/Freq output...">${item.content}</textarea>
                ` : `
                  <div class="species-file-drop primary-drop ${item.content ? 'has-file' : ''}">
                    <span>${item.content ? `✓ Attached: ${item.primaryName || 'Opt/Freq Output'}` : '📄 Click to attach Opt/Freq .out/.log'}</span>
                    <input type="file" class="primary-file-input hidden" accept=".out,.log,.property.txt,.txt">
                  </div>
                `}
              </div>
            </div>

            <!-- Slot 2: High-Level Single Point (Optional for Multi-Level Composite) -->
            <div class="species-slot-box sp-slot ${item.sp_content ? 'has-file' : ''}">
              <div class="slot-header-bar">
                <span class="slot-badge-opt">File 2 (Optional)</span>
                <strong class="slot-title">High-Level SP Energy</strong>
                <span class="slot-desc-pill">e.g. DLPNO-CCSD(T) / QZVPP</span>
              </div>
              <div class="species-source-tabs">
                <button type="button" class="btn-source-tab ${item.spMode === 'upload' ? 'active' : ''}" data-slot="sp" data-mode="upload">📁 Attach SP</button>
                <button type="button" class="btn-source-tab ${item.spMode === 'job' ? 'active' : ''}" data-slot="sp" data-mode="job">⚡ Kaggle Job</button>
                <button type="button" class="btn-source-tab ${item.spMode === 'paste' ? 'active' : ''}" data-slot="sp" data-mode="paste">📝 Paste SP</button>
                <button type="button" class="btn-source-tab ${item.spMode === 'none' ? 'active' : ''}" data-slot="sp" data-mode="none" title="Clear SP and use single-level Opt/Freq only">✕ Single-Level</button>
              </div>
              <div class="slot-content-panel">
                ${item.spMode === 'none' ? `
                  <span class="field-hint" style="padding:0.4rem 0;">Single-level mode active. Opt/Freq electronic energy will be used.</span>
                ` : item.spMode === 'job' ? `
                  <select class="select-small sp-job-select">${jobsOptions}</select>
                ` : item.spMode === 'paste' ? `
                  <textarea class="mono sp-paste-area" rows="2" placeholder="Paste high-level SP output...">${item.sp_content}</textarea>
                ` : `
                  <div class="species-file-drop sp-drop ${item.sp_content ? 'has-file' : ''}">
                    <span>${item.sp_content ? `⚡ Attached SP: ${item.spName || 'SP Output'}` : '📄 Click to attach High-Level SP .out/.log'}</span>
                    <input type="file" class="sp-file-input hidden" accept=".out,.log,.property.txt,.txt">
                  </div>
                `}
              </div>
            </div>
          </div>
        `;

        // Coeff listener
        const coeffInput = card.querySelector(".species-coeff-input");
        coeffInput.addEventListener("input", () => {
          item.coeff = parseInt(coeffInput.value) || 1;
          updateLiveEquation();
        });

        // Name listener
        const nameInput = card.querySelector(".species-name-input");
        nameInput.addEventListener("input", () => {
          item.name = nameInput.value.trim();
          updateLiveEquation();
          // Check archive match
          const clean = item.name.toLowerCase();
          if (state.extractedArchiveFiles[item.name] || state.extractedArchiveFiles[clean]) {
            item.content = state.extractedArchiveFiles[item.name] || state.extractedArchiveFiles[clean];
            item.primaryName = item.name + ".out";
            renderSpeciesCards(container, list, isReactant);
          }
        });

        // Delete button
        const delBtn = card.querySelector(".species-delete-btn");
        if (delBtn) {
          delBtn.addEventListener("click", () => {
            const idx = list.indexOf(item);
            if (idx > -1) {
              list.splice(idx, 1);
              renderSpeciesCards(container, list, isReactant);
              updateLiveEquation();
            }
          });
        }

        // Mode tabs for Primary and SP slots
        card.querySelectorAll(".btn-source-tab").forEach(tabBtn => {
          tabBtn.addEventListener("click", () => {
            const slot = tabBtn.dataset.slot;
            const mode = tabBtn.dataset.mode;
            if (slot === "primary") {
              item.primaryMode = mode;
            } else if (slot === "sp") {
              item.spMode = mode;
              if (mode === "none") {
                item.sp_content = "";
                item.spName = "";
              }
            }
            renderSpeciesCards(container, list, isReactant);
          });
        });

        // Primary file drop & input
        const primaryDrop = card.querySelector(".species-file-drop.primary-drop");
        const primaryInp = card.querySelector(".primary-file-input");
        if (primaryDrop && primaryInp) {
          primaryDrop.addEventListener("click", () => primaryInp.click());
          
          const handlePrimaryFile = async (file) => {
            if (!file) return;
            if (/\.(zip|rar|tar|gz|tgz|tar\.gz)$/i.test(file.name)) {
              handleArchiveUpload(file);
              return;
            }
            item.content = await file.text();
            item.primaryName = file.name;
            renderSpeciesCards(container, list, isReactant);
          };

          primaryInp.addEventListener("change", async () => {
            if (primaryInp.files && primaryInp.files[0]) {
              await handlePrimaryFile(primaryInp.files[0]);
            }
          });

          ["dragenter", "dragover"].forEach(evt => {
            primaryDrop.addEventListener(evt, (e) => { e.preventDefault(); primaryDrop.classList.add("drag-over"); });
          });
          ["dragleave", "drop"].forEach(evt => {
            primaryDrop.addEventListener(evt, (e) => { e.preventDefault(); primaryDrop.classList.remove("drag-over"); });
          });
          primaryDrop.addEventListener("drop", async (e) => {
            if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
              await handlePrimaryFile(e.dataTransfer.files[0]);
            }
          });
        }

        // Primary job select
        const primaryJobSel = card.querySelector(".primary-job-select");
        if (primaryJobSel) {
          primaryJobSel.addEventListener("change", async () => {
            const jobId = primaryJobSel.value;
            if (jobId) {
              try {
                const res = await postJSON("/api/orca/engine/analyze-job", { ...credsFor(jobId), job_id: jobId });
                item.content = res.raw_text || res.content || "";
                item.primaryName = res.filename || `Job ${jobId.substring(0, 8)}`;
                renderSpeciesCards(container, list, isReactant);
              } catch (e) {
                showToast(`Failed to load job output: ${e.message}`);
              }
            }
          });
        }

        // Primary paste area
        const primaryPaste = card.querySelector(".primary-paste-area");
        if (primaryPaste) {
          primaryPaste.addEventListener("input", () => {
            item.content = primaryPaste.value.trim();
            item.primaryName = "Pasted Text";
          });
        }

        // SP file drop & input
        const spDrop = card.querySelector(".species-file-drop.sp-drop");
        const spInp = card.querySelector(".sp-file-input");
        if (spDrop && spInp) {
          spDrop.addEventListener("click", () => spInp.click());

          const handleSpFile = async (file) => {
            if (!file) return;
            if (/\.(zip|rar|tar|gz|tgz|tar\.gz)$/i.test(file.name)) {
              handleArchiveUpload(file);
              return;
            }
            item.sp_content = await file.text();
            item.spName = file.name;
            showToast(`Attached high-level SP for <strong>${item.name || 'species'}</strong>. Multi-level composite energy will be evaluated.`);
            renderSpeciesCards(container, list, isReactant);
          };

          spInp.addEventListener("change", async () => {
            if (spInp.files && spInp.files[0]) {
              await handleSpFile(spInp.files[0]);
            }
          });

          ["dragenter", "dragover"].forEach(evt => {
            spDrop.addEventListener(evt, (e) => { e.preventDefault(); spDrop.classList.add("drag-over"); });
          });
          ["dragleave", "drop"].forEach(evt => {
            spDrop.addEventListener(evt, (e) => { e.preventDefault(); spDrop.classList.remove("drag-over"); });
          });
          spDrop.addEventListener("drop", async (e) => {
            if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
              await handleSpFile(e.dataTransfer.files[0]);
            }
          });
        }

        // SP job select
        const spJobSel = card.querySelector(".sp-job-select");
        if (spJobSel) {
          spJobSel.addEventListener("change", async () => {
            const jobId = spJobSel.value;
            if (jobId) {
              try {
                const res = await postJSON("/api/orca/engine/analyze-job", { ...credsFor(jobId), job_id: jobId });
                item.sp_content = res.raw_text || res.content || "";
                item.spName = res.filename || `SP Job ${jobId.substring(0, 8)}`;
                showToast(`Attached SP job for <strong>${item.name || 'species'}</strong>.`);
                renderSpeciesCards(container, list, isReactant);
              } catch (e) {
                showToast(`Failed to load SP job: ${e.message}`);
              }
            }
          });
        }

        // SP paste area
        const spPaste = card.querySelector(".sp-paste-area");
        if (spPaste) {
          spPaste.addEventListener("input", () => {
            item.sp_content = spPaste.value.trim();
            item.spName = "Pasted SP Text";
          });
        }

        container.appendChild(card);
      });
    }

    // Add Reactant / Product buttons
    if (addReactantBtn) {
      addReactantBtn.addEventListener("click", () => {
        state.reactants.push({
          id: "r_" + Date.now(),
          coeff: 1,
          name: "Reactant " + (state.reactants.length + 1),
          content: "",
          primaryName: "",
          primaryMode: "upload",
          sp_content: "",
          spName: "",
          spMode: "upload"
        });
        renderSpeciesCards(reactantsList, state.reactants, true);
        updateLiveEquation();
      });
    }

    if (addProductBtn) {
      addProductBtn.addEventListener("click", () => {
        state.products.push({
          id: "p_" + Date.now(),
          coeff: 1,
          name: "Product " + (state.products.length + 1),
          content: "",
          primaryName: "",
          primaryMode: "upload",
          sp_content: "",
          spName: "",
          spMode: "upload"
        });
        renderSpeciesCards(productsList, state.products, false);
        updateLiveEquation();
      });
    }

    // Archive uploader for Thermochemistry
    if (archiveBrowseBtn && archiveFileInput) {
      archiveBrowseBtn.addEventListener("click", () => archiveFileInput.click());
    }

    if (archiveDropzone && archiveFileInput) {
      archiveDropzone.addEventListener("click", (e) => {
        if (e.target !== archiveBrowseBtn) archiveFileInput.click();
      });
      ["dragenter", "dragover"].forEach(evt => {
        archiveDropzone.addEventListener(evt, (e) => { e.preventDefault(); archiveDropzone.classList.add("drag-over"); });
      });
      ["dragleave", "drop"].forEach(evt => {
        archiveDropzone.addEventListener(evt, (e) => { e.preventDefault(); archiveDropzone.classList.remove("drag-over"); });
      });
      archiveDropzone.addEventListener("drop", (e) => {
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
          archiveFileInput.files = e.dataTransfer.files;
          handleArchiveUpload(e.dataTransfer.files[0]);
        }
      });
      archiveFileInput.addEventListener("change", () => {
        if (archiveFileInput.files && archiveFileInput.files[0]) {
          handleArchiveUpload(archiveFileInput.files[0]);
        }
      });
    }

    async function handleArchiveUpload(file) {
      if (archiveStatus) {
        archiveStatus.textContent = `Processing ${file.name}…`;
        archiveStatus.className = "prop-badge badge-primary";
        archiveStatus.classList.remove("hidden");
      }
      try {
        const formData = new FormData();
        formData.append("file", file);
        const resp = await fetch("/api/orca/engine/parse", { method: "POST", body: formData });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || "Could not parse file.");

        if (data.is_archive && data.archive_entries && data.archive_entries.length > 0) {
          data.archive_entries.forEach(entry => {
            const base = entry.basename.toLowerCase().replace(/\.[^/.]+$/, "");
            state.extractedArchiveFiles[entry.basename] = entry.raw_text;
            state.extractedArchiveFiles[entry.basename.toLowerCase()] = entry.raw_text;
            state.extractedArchiveFiles[base] = entry.raw_text;
            const formula = entry.molecule?.chemical_formula || entry.latest_job?.chemical_formula || entry.latest_job?.formula;
            if (formula) {
              state.extractedArchiveFiles[formula] = entry.raw_text;
              state.extractedArchiveFiles[formula.toLowerCase()] = entry.raw_text;
            }
          });
          if (archiveStatus) {
            archiveStatus.textContent = `✓ Extracted ${data.archive_entries.length} calculations`;
            archiveStatus.className = "prop-badge badge-success";
          }
          // Auto-match to current reactants & products
          let matchedCount = 0;
          [...state.reactants, ...state.products].forEach(sp => {
            const clean = sp.name.toLowerCase().trim();
            for (const entry of data.archive_entries) {
              const entryBase = entry.basename.toLowerCase();
              const formula = (entry.molecule?.chemical_formula || entry.latest_job?.chemical_formula || entry.latest_job?.formula || "").toLowerCase();
              if (entryBase.includes(clean) || clean.includes(entryBase.replace(/\.[^/.]+$/, "")) || (formula && (formula === clean || clean.includes(formula)))) {
                sp.content = entry.raw_text;
                sp.primaryName = entry.basename;
                matchedCount++;
                break;
              }
            }
          });
          // If some species remain unmatched and there are unassigned archive entries, assign in order
          const unassignedEntries = data.archive_entries.filter(e => ![...state.reactants, ...state.products].some(s => s.content === e.raw_text));
          let unassignedIdx = 0;
          [...state.reactants, ...state.products].forEach(sp => {
            if (!sp.content && unassignedIdx < unassignedEntries.length) {
              const entry = unassignedEntries[unassignedIdx++];
              sp.content = entry.raw_text;
              sp.primaryName = entry.basename;
              matchedCount++;
            }
          });

          renderSpeciesCards(reactantsList, state.reactants, true);
          renderSpeciesCards(productsList, state.products, false);
          showToast(`Archive <strong>${file.name}</strong> unpacked: ${data.archive_entries.length} calculations extracted, ${matchedCount} matched.`);
        } else {
          // Single .out / .log calculation file uploaded to the dropzone
          const text = data.raw_text || "";
          const base = (data.name || file.name).replace(/\.[^/.]+$/, "");
          state.extractedArchiveFiles[file.name] = text;
          state.extractedArchiveFiles[file.name.toLowerCase()] = text;
          state.extractedArchiveFiles[base] = text;
          state.extractedArchiveFiles[base.toLowerCase()] = text;
          const formula = data.molecule?.chemical_formula || data.latest_job?.chemical_formula || data.latest_job?.formula;
          if (formula) {
            state.extractedArchiveFiles[formula] = text;
            state.extractedArchiveFiles[formula.toLowerCase()] = text;
          }

          // Match to an empty species or matching species
          let matched = false;
          const allSp = [...state.reactants, ...state.products];
          for (const sp of allSp) {
            const clean = sp.name.toLowerCase().trim();
            if (clean && (file.name.toLowerCase().includes(clean) || (formula && clean.includes(formula.toLowerCase())))) {
              sp.content = text;
              sp.primaryName = file.name;
              matched = true;
              break;
            }
          }
          if (!matched) {
            // Assign to first empty species
            const emptySp = allSp.find(s => !s.content);
            if (emptySp) {
              emptySp.content = text;
              emptySp.primaryName = file.name;
              matched = true;
            }
          }

          if (archiveStatus) {
            archiveStatus.textContent = `✓ Loaded calculation for ${file.name}`;
            archiveStatus.className = "prop-badge badge-success";
          }
          renderSpeciesCards(reactantsList, state.reactants, true);
          renderSpeciesCards(productsList, state.products, false);
          showToast(`Calculation <strong>${file.name}</strong> loaded and attached to reaction.`);
        }
      } catch (err) {
        if (archiveStatus) {
          archiveStatus.textContent = `Error: ${err.message}`;
          archiveStatus.className = "prop-badge badge-error";
        }
        showToast(`File error: ${err.message}`, "error");
      }
    }

    // Initial render of cards & equation
    renderSpeciesCards(reactantsList, state.reactants, true);
    renderSpeciesCards(productsList, state.products, false);
    updateLiveEquation();

    // Form submission
    if (form) {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        if (loadingEl) loadingEl.classList.remove("hidden");
        if (errorEl) errorEl.classList.add("hidden");
        if (resultCard) resultCard.classList.add("hidden");

        const isCustomConditions = customCondCheckbox ? customCondCheckbox.checked : false;
        const customTemp = parseFloat(customTempInput?.value || "298.15");
        const customPressure = parseFloat(customPressInput?.value || "1.0");
        const equation = eqInput ? eqInput.value.trim() : "";

        const moleculesPayload = {};

        // Verify and collect all reactants & products
        const allSpecies = [...state.reactants, ...state.products];
        for (const sp of allSpecies) {
          if (!sp.name) continue;
          if (!sp.content) {
            if (errorEl) showError(errorEl, `Please attach an ORCA Opt/Freq output file for species "${sp.name}".`);
            if (loadingEl) loadingEl.classList.add("hidden");
            return;
          }
          moleculesPayload[sp.name] = sp.content;
        }

        try {
          const res = await postJSON("/api/orca/engine/thermochemistry", {
            equation,
            reactants: state.reactants.map(r => ({ name: r.name, coefficient: r.coeff, content: r.content, sp_content: r.sp_content || "" })),
            products: state.products.map(p => ({ name: p.name, coefficient: p.coeff, content: p.content, sp_content: p.sp_content || "" })),
            molecules: moleculesPayload,
            enable_custom_conditions: isCustomConditions,
            custom_temperature_k: isCustomConditions ? customTemp : null,
            custom_pressure_atm: isCustomConditions ? customPressure : null,
          });

          const r = res.result;
          const eqDisp = document.getElementById("thermo-eq-display");
          const balanceBadge = document.getElementById("thermo-balance-badge");
          const conditionsBadge = document.getElementById("thermo-conditions-badge");
          const dgKcal = document.getElementById("thermo-val-dg-kcal");
          const dgKj = document.getElementById("thermo-val-dg-kj");
          const dhKcal = document.getElementById("thermo-val-dh-kcal");
          const dhKj = document.getElementById("thermo-val-dh-kj");
          const dsCal = document.getElementById("thermo-val-ds");
          const dsSi = document.getElementById("thermo-val-ds-si");
          const keqEl = document.getElementById("thermo-val-keq");
          const warningEl = document.getElementById("thermo-consistency-warning");
          const tbody = document.getElementById("thermo-species-breakdown-tbody");

          if (eqDisp) eqDisp.textContent = res.equation || equation;
          if (balanceBadge) {
            if (r.atom_balanced && r.charge_balanced) {
              balanceBadge.className = "prop-badge badge-success";
              balanceBadge.textContent = "Stoichiometrically Balanced";
            } else if (r.atom_balanced === false) {
              balanceBadge.className = "prop-badge badge-warning";
              const imb = r.atom_imbalance || {};
              const parts = Object.entries(imb).map(([el, d]) => `${el}${d > 0 ? '+' + d : d}`);
              balanceBadge.textContent = parts.length > 0 ? `Atom Imbalance (${parts.join(', ')})` : "Equation Atom Imbalanced";
            } else if (r.charge_balanced === false) {
              balanceBadge.className = "prop-badge badge-warning";
              balanceBadge.textContent = `Charge Imbalance (Net ${r.charge_imbalance > 0 ? '+' : ''}${r.charge_imbalance})`;
            } else {
              balanceBadge.className = "prop-badge badge-neutral";
              balanceBadge.textContent = "Balance Unchecked (No coordinates)";
            }
          }

          if (conditionsBadge && res.result?.applied_conditions) {
            const cond = res.result.applied_conditions;
            if (cond.is_custom) {
              conditionsBadge.className = "prop-badge badge-primary";
              conditionsBadge.textContent = `🌡️ Custom: ${cond.temperature_k?.toFixed(2) || '298.15'} K, ${cond.custom_pressure_atm?.toFixed(2) || '1.00'} atm (Gas Phase)`;
            } else {
              conditionsBadge.className = "prop-badge badge-neutral";
              conditionsBadge.textContent = `📄 Conditions: ${cond.temperature_k != null ? cond.temperature_k.toFixed(2) + ' K' : '298.15 K'} (ORCA Output)`;
            }
          }

          const dgVal = r.delta_g_kcal_mol ?? r.delta_gibbs_kcal_mol;
          const dhVal = r.delta_h_kcal_mol ?? r.delta_enthalpy_kcal_mol;
          const dsValCal = r.delta_s_cal_mol_k ?? r.delta_entropy_cal_mol_k;
          const dsValJ = r.delta_s_j_mol_k ?? r.delta_entropy_j_mol_k ?? (dsValCal != null ? dsValCal * 4.184 : null);
          const de0Val = r.delta_e0_kcal_mol;
          const deelVal = r.delta_electronic_kcal_mol;
          const keqVal = r.equilibrium_constant_keq ?? r.k_eq;

          if (dgKcal) dgKcal.textContent = dgVal != null ? `${dgVal.toFixed(2)} kcal/mol` : "-";
          if (dgKj) dgKj.textContent = dgVal != null ? `${(dgVal * 4.184).toFixed(2)} kJ/mol` : "-";
          if (dhKcal) dhKcal.textContent = dhVal != null ? `${dhVal.toFixed(2)} kcal/mol` : "-";
          if (dhKj) dhKj.textContent = dhVal != null ? `${(dhVal * 4.184).toFixed(2)} kJ/mol` : "-";
          if (dsCal) dsCal.textContent = dsValCal != null ? `${dsValCal.toFixed(2)} cal/(mol·K)` : "-";
          if (dsSi) dsSi.textContent = dsValJ != null ? `${dsValJ.toFixed(2)} J/(mol·K)` : "-";
          if (keqEl) keqEl.textContent = keqVal != null ? (keqVal > 1e6 || keqVal < 1e-4 ? keqVal.toExponential(4) : keqVal.toFixed(4)) : "-";

          const de0El = document.getElementById("thermo-val-de0");
          if (de0El) de0El.textContent = de0Val != null ? `${de0Val.toFixed(2)} kcal/mol (${(de0Val * 4.184).toFixed(2)} kJ/mol)` : "-";

          const deelEl = document.getElementById("thermo-val-deel");
          if (deelEl) deelEl.textContent = deelVal != null ? `${deelVal.toFixed(2)} kcal/mol` : "-";

          if (warningEl) {
            if (r.method_consistent === false) {
              warningEl.textContent = "Warning: The participating species use different levels of theory, basis sets, or solvation models. Energetics may not be strictly comparable.";
              warningEl.classList.remove("hidden");
            } else {
              warningEl.classList.add("hidden");
            }
          }

          // Populate Species Breakdown table with real parsed energetics
          if (tbody) {
            tbody.innerHTML = "";
            const spData = r.species_energetics || {};

            const resolveSpeciesInfo = (item) => {
              if (!item) return {};
              const candidates = [
                item.name,
                item.name?.toLowerCase(),
                item.name?.trim().toLowerCase(),
                item.name?.replace(/\s+/g, '_'),
                item.name?.replace(/[\s_-]+/g, ''),
                item.primaryName,
                item.primaryName?.replace(/\.[^/.]+$/, ''),
                item.primaryName?.toLowerCase(),
                item.primaryName?.replace(/\.[^/.]+$/, '').toLowerCase(),
                item.id
              ];
              for (const cand of candidates) {
                if (cand && spData[cand]) return spData[cand];
              }
              return {};
            };

            const renderBadge = (info) => {
              if (info.is_composite) return ' <span class="prop-badge badge-primary" style="font-size:0.7rem;" title="Multi-Level Composite: High-Level SP electronic energy with lower-level thermal corrections">⚡ Multi-Level (SP+Freq)</span>';
              if (info.is_electronic_only) return ' <span class="prop-badge badge-warning" style="font-size:0.7rem;" title="Electronic energy only - no vibrational frequency calculation found in output file">Electronic Only</span>';
              if (info.enthalpy_eh != null) return ' <span class="prop-badge badge-success" style="font-size:0.7rem;">Opt + Freq</span>';
              return '';
            };

            const renderEnergyCell = (info, field, label) => {
              const val = info[field];
              if (val == null) return '<span style="color:var(--text-muted); font-size:0.75rem;">-</span>';
              if (info.is_composite && field === 'e_elec_eh') {
                return `<span class="mono">${val.toFixed(6)} Eh</span><br><span style="color:var(--primary); font-size:0.75rem;">SP: ${info.method_sp || 'High-Level SP'}</span>`;
              }
              if (info.is_composite && field === 'enthalpy_eh') {
                const corr = info.h_thermal_corr_eh;
                const corrStr = corr != null ? `ΔH<sub>therm</sub>: ${corr >= 0 ? '+' : ''}${corr.toFixed(5)} Eh` : '';
                return `<span class="mono">${val.toFixed(6)} Eh</span><br><span style="color:var(--text-muted); font-size:0.75rem;">${corrStr}</span>`;
              }
              if (info.is_composite && field === 'gibbs_eh') {
                const corr = info.g_thermal_corr_eh;
                const corrStr = corr != null ? `ΔG<sub>therm</sub>: ${corr >= 0 ? '+' : ''}${corr.toFixed(5)} Eh` : '';
                return `<span class="mono">${val.toFixed(6)} Eh</span><br><span style="color:var(--text-muted); font-size:0.75rem;">${corrStr}</span>`;
              }
              return `<span class="mono">${val.toFixed(6)} Eh</span>`;
            };

            const renderEntropyCell = (info) => {
              if (info.entropy_cal_mol_k != null) {
                const sCal = info.entropy_cal_mol_k;
                const sJ = info.entropy_j_mol_k ?? (sCal * 4.184);
                return `${sCal.toFixed(2)} cal/(mol·K)<br><span style="color:var(--text-muted); font-size:0.75rem;">${sJ.toFixed(2)} J/(mol·K)</span>`;
              }
              return '<span style="color:var(--text-muted); font-size:0.75rem;" title="Requires frequency calculation">N/A</span>';
            };

            state.reactants.forEach(rItem => {
              const info = resolveSpeciesInfo(rItem);
              const tr = document.createElement("tr");
              tr.innerHTML = `
                <td><span class="prop-badge badge-primary">Reactant</span></td>
                <td class="mono font-bold">${rItem.coeff}</td>
                <td><strong>${rItem.name}</strong>${renderBadge(info)}</td>
                <td>${renderEnergyCell(info, 'e_elec_eh', 'Electronic')}</td>
                <td>${renderEnergyCell(info, 'enthalpy_eh', 'Enthalpy')}</td>
                <td>${renderEnergyCell(info, 'gibbs_eh', 'Gibbs')}</td>
                <td class="mono">${renderEntropyCell(info)}</td>
              `;
              tbody.appendChild(tr);
            });
            state.products.forEach(pItem => {
              const info = resolveSpeciesInfo(pItem);
              const tr = document.createElement("tr");
              tr.innerHTML = `
                <td><span class="prop-badge badge-success">Product</span></td>
                <td class="mono font-bold">${pItem.coeff}</td>
                <td><strong>${pItem.name}</strong>${renderBadge(info)}</td>
                <td>${renderEnergyCell(info, 'e_elec_eh', 'Electronic')}</td>
                <td>${renderEnergyCell(info, 'enthalpy_eh', 'Enthalpy')}</td>
                <td>${renderEnergyCell(info, 'gibbs_eh', 'Gibbs')}</td>
                <td class="mono">${renderEntropyCell(info)}</td>
              `;
              tbody.appendChild(tr);
            });
          }

          if (resultCard) {
            resultCard.classList.remove("hidden");
            resultCard.scrollIntoView({ behavior: "smooth", block: "start" });
          }
          showToast("Reaction energetics calculated successfully.");
        } catch (err) {
          if (errorEl) showError(errorEl, err.message);
        } finally {
          if (loadingEl) loadingEl.classList.add("hidden");
        }
      });
    }
  }
  initReactionThermochemistry();

  // =========================================================================
  // Calculated NMR Spectrum Analyzer & Visualizer Studio (1H & 13C)
  // =========================================================================
  let currentNMRData = null;
  let currentNMRNucleus = "1H";
  let currentNMRReference = "none";
  let nmrGroupPeaks = false;
  let nmrGroupTolerance = 0.02;
  let selectedNMRPeakIndex = null;
  let hoveredNMRPeakIndex = null;

  const NMR_PRESETS = {
    "1H": [
      { id: "none", name: "None (Display Raw Isotropic Shielding σ in ppm)", shielding: null, method: null, basis: null },
      { id: "tms_tpss_pcsseg3", name: "TMS (TPSS / pcSseg-3) - 31.77 ppm", shielding: 31.77, method: "TPSS", basis: "pcSseg-3" },
      { id: "custom", name: "Custom Reference Shielding (Manual Entry)...", shielding: null, method: "Custom", basis: null }
    ],
    "13C": [
      { id: "none", name: "None (Display Raw Isotropic Shielding σ in ppm)", shielding: null, method: null, basis: null },
      { id: "tms_b3lyp_tzvpp", name: "TMS (B3LYP / TZVPP) - 184.30 ppm", shielding: 184.30, method: "B3LYP", basis: "TZVPP" },
      { id: "tms_bp86_tzvpp", name: "TMS (BP86 / TZVPP) - 184.80 ppm", shielding: 184.80, method: "BP86", basis: "TZVPP" },
      { id: "tms_hf_tzvpp", name: "TMS (HF / TZVPP) - 194.10 ppm", shielding: 194.10, method: "HF", basis: "TZVPP" },
      { id: "tms_tpss_pcsseg3", name: "TMS (TPSS / pcSseg-3) - 188.10 ppm", shielding: 188.10, method: "TPSS", basis: "pcSseg-3" },
      { id: "custom", name: "Custom Reference Shielding (Manual Entry)...", shielding: null, method: "Custom", basis: null }
    ]
  };

  function normLevelStr(s) {
    if (!s) return "";
    return String(s).replace(/[\s\-_/]+/g, "").toUpperCase();
  }

  function updateNMRPresetOptions() {
    const select = document.getElementById("nmr-ref-preset");
    if (!select) return;
    const presets = NMR_PRESETS[currentNMRNucleus] || NMR_PRESETS["1H"];
    const prevVal = select.value;
    select.innerHTML = "";

    const calcMethod = normLevelStr(currentNMRData?.provenance?.method);
    const calcBasis = normLevelStr(currentNMRData?.provenance?.basis_set);

    presets.forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.id;
      let label = p.name;
      if (p.method && p.basis && calcMethod && calcBasis) {
        const pm = normLevelStr(p.method);
        const pb = normLevelStr(p.basis);
        const normCalcM = calcMethod === "B86" ? "BP86" : calcMethod;
        const normPresetM = pm === "B86" ? "BP86" : pm;
        if (normCalcM === normPresetM && calcBasis === pb) {
          label += " (Recommended - Level of Theory Matches)";
        }
      }
      opt.textContent = label;
      select.appendChild(opt);
    });

    const exists = presets.some(p => p.id === prevVal);
    select.value = exists ? prevVal : "none";
    currentNMRReference = select.value;
    updateNMRRefCustomVisibility();
  }

  function updateNMRRefCustomVisibility() {
    const wrap = document.getElementById("nmr-custom-ref-wrap");
    const warning = document.getElementById("nmr-ref-warning");
    const select = document.getElementById("nmr-ref-preset");
    if (wrap) {
      wrap.style.display = (select && select.value === "custom") ? "block" : "none";
    }
    if (warning) {
      if (!select || select.value === "none") {
        warning.className = "alert alert-warning";
        warning.innerHTML = "⚠️ Displaying absolute isotropic shielding (σ, ppm). Reference correction is not configured.";
      } else if (select.value === "custom") {
        warning.className = "alert alert-info";
        warning.innerHTML = "ℹ️ Displaying chemical shifts (δ = σ<sub>ref</sub> - σ<sub>sample</sub>) with custom user-supplied reference shielding.";
      } else {
        const presets = NMR_PRESETS[currentNMRNucleus] || [];
        const found = presets.find(p => p.id === select.value);
        const calcMethod = currentNMRData?.provenance?.method;
        const calcBasis = currentNMRData?.provenance?.basis_set;
        
        let hasMismatch = false;
        if (found && found.method && found.basis && calcMethod && calcMethod !== "Unknown") {
          const nm = normLevelStr(calcMethod);
          const nb = normLevelStr(calcBasis);
          const pm = normLevelStr(found.method);
          const pb = normLevelStr(found.basis);
          if (nm !== pm || (nb && nb !== pb)) {
            hasMismatch = true;
          }
        }

        if (hasMismatch) {
          warning.className = "alert alert-warning";
          warning.innerHTML = `⚠️ <strong>Level of Theory Warning:</strong> Reference is calibrated for ${found.method}/${found.basis}, but current calculation is ${calcMethod || "Unknown"}${calcBasis ? "/" + calcBasis : ""}. Chemical shifts may contain level-of-theory errors.`;
        } else {
          warning.className = "alert alert-info";
          warning.innerHTML = `✅ Chemical shifts (δ = σ<sub>ref</sub> - σ<sub>sample</sub>) calibrated against ${found ? found.name : "reference"}.`;
        }
      }
    }
  }

  async function recomputeNMRSpectrum() {
    if (!currentNMRData || !currentNMRData.atoms) return;

    let refShielding = null;
    let refMethod = "Custom";
    let refBasis = null;
    const select = document.getElementById("nmr-ref-preset");
    const presetId = select ? select.value : currentNMRReference;

    if (presetId === "custom") {
      const customInp = document.getElementById("nmr-custom-ref-val");
      const val = parseFloat(customInp ? customInp.value : "");
      refShielding = !isNaN(val) ? val : null;
      refMethod = "Custom";
    } else if (presetId !== "none") {
      const presets = NMR_PRESETS[currentNMRNucleus] || [];
      const found = presets.find(p => p.id === presetId);
      if (found && found.shielding != null) {
        refShielding = found.shielding;
        refMethod = found.method || "Preset";
        refBasis = found.basis || null;
      }
    }

    try {
      const payload = {
        atoms: currentNMRData.atoms,
        nucleus: currentNMRNucleus,
        reference_shielding: refShielding,
        reference_method: refMethod,
        reference_basis: refBasis,
        group_peaks: nmrGroupPeaks,
        group_tolerance_ppm: nmrGroupTolerance,
        method: currentNMRData.provenance?.method || "Unknown",
        basis_set: currentNMRData.provenance?.basis_set || "Unknown",
        solvent: currentNMRData.provenance?.solvent || "None",
        orca_version: currentNMRData.provenance?.orca_version || "Unknown"
      };

      const res = await fetch("/api/orca/engine/nmr/spectrum", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      if (res.ok) {
        const json = await res.json();
        if (json.ok && json.spectrum) {
          if (currentNMRNucleus === "1H") {
            currentNMRData.h1_spectrum = json.spectrum;
          } else {
            currentNMRData.c13_spectrum = json.spectrum;
          }
        }
      }
    } catch (err) {
      console.warn("Client-side fallback for NMR spectrum recalculation:", err);
    }

    drawNMRCanvas();
    updateNMRTable();
  }

  function loadNMRDataIntoStudio(nmrData, moleculeName, job) {
    currentNMRData = nmrData;
    selectedNMRPeakIndex = null;
    hoveredNMRPeakIndex = null;

    const provBadge = document.getElementById("nmr-provenance-badge");
    if (provBadge) {
      const methodStr = (job && job.method) ? job.method : (nmrData.provenance?.method || "DFT / GIAO");
      const basisStr = (job && job.basis_set) ? ` / ${job.basis_set}` : "";
      provBadge.textContent = `${methodStr}${basisStr}`;
    }

    // Default to 1H if present, else 13C
    if (nmrData.has_h1) {
      currentNMRNucleus = "1H";
    } else if (nmrData.has_c13) {
      currentNMRNucleus = "13C";
    } else {
      currentNMRNucleus = "1H";
    }

    // Update tab styles
    const tab1h = document.getElementById("nmr-tab-1h");
    const tab13c = document.getElementById("nmr-tab-13c");
    if (tab1h && tab13c) {
      tab1h.className = currentNMRNucleus === "1H" ? "btn btn-primary btn-small is-active" : "btn btn-ghost btn-small";
      tab13c.className = currentNMRNucleus === "13C" ? "btn btn-primary btn-small is-active" : "btn btn-ghost btn-small";
    }

    const nucBadge = document.getElementById("nmr-nucleus-badge");
    if (nucBadge) {
      nucBadge.textContent = currentNMRNucleus === "1H" ? "¹H NMR Active" : "¹³C NMR Active";
    }

    updateNMRPresetOptions();
    recomputeNMRSpectrum();
  }

  function renderNMRSpectrumToCanvas(canvas, scale = 1, isExport = false) {
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const width = canvas.width / scale;
    const height = canvas.height / scale;

    ctx.save();
    ctx.scale(scale, scale);
    ctx.clearRect(0, 0, width, height);

    const isLight = document.body.classList.contains("light-mode") || document.body.classList.contains("theme-light");

    // Canvas Background
    ctx.fillStyle = isLight ? "#ffffff" : "#0d1424";
    ctx.fillRect(0, 0, width, height);

    if (!currentNMRData) {
      ctx.fillStyle = isLight ? "#475569" : "#91A5BE";
      ctx.font = "14px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No calculated NMR shielding data loaded.", width / 2, height / 2);
      ctx.restore();
      return;
    }

    const spectrum = currentNMRNucleus === "1H" ? currentNMRData.h1_spectrum : currentNMRData.c13_spectrum;
    if (!spectrum || !spectrum.peaks || spectrum.peaks.length === 0) {
      ctx.fillStyle = isLight ? "#475569" : "#91A5BE";
      ctx.font = "14px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(`No calculated ${currentNMRNucleus} NMR signals found in this molecule.`, width / 2, height / 2);
      ctx.restore();
      return;
    }

    const padding = { top: 45, right: 45, bottom: 55, left: 65 };
    const plotW = width - padding.left - padding.right;
    const plotH = height - padding.top - padding.bottom;

    const minPpm = spectrum.min_ppm;
    const maxPpm = spectrum.max_ppm;
    const isRefApplied = spectrum.is_reference_applied;

    // Inverted axis for chemical shift (conventional high ppm -> low ppm)
    // Non-inverted for shielding
    const mapX = (ppm) => {
      if (maxPpm === minPpm) return padding.left + plotW / 2;
      if (isRefApplied) {
        return padding.left + ((maxPpm - ppm) / (maxPpm - minPpm)) * plotW;
      } else {
        return padding.left + ((ppm - minPpm) / (maxPpm - minPpm)) * plotW;
      }
    };

    const maxIntensity = Math.max(1.0, ...spectrum.peaks.map(p => p.intensity || 1.0));
    const mapY = (intensity) => {
      const norm = intensity / maxIntensity;
      return (padding.top + plotH) - (norm * (plotH * 0.75));
    };

    const baselineY = padding.top + plotH;

    // Grid lines
    ctx.strokeStyle = isLight ? "rgba(0, 0, 0, 0.06)" : "rgba(255, 255, 255, 0.06)";
    ctx.lineWidth = 1;

    // Determine major ticks
    const span = maxPpm - minPpm;
    let tickStep = 1.0;
    if (span > 100) tickStep = 20.0;
    else if (span > 50) tickStep = 10.0;
    else if (span > 20) tickStep = 5.0;
    else if (span > 8) tickStep = 2.0;
    else if (span > 3) tickStep = 1.0;
    else tickStep = 0.5;

    const startTick = Math.ceil(minPpm / tickStep) * tickStep;
    for (let t = startTick; t <= maxPpm; t += tickStep) {
      const x = mapX(t);
      if (x >= padding.left && x <= padding.left + plotW) {
        ctx.beginPath();
        ctx.moveTo(x, padding.top);
        ctx.lineTo(x, baselineY);
        ctx.stroke();

        // Tick mark & label
        ctx.fillStyle = isLight ? "#64748b" : "#94a3b8";
        ctx.font = "11px var(--font-mono, monospace)";
        ctx.textAlign = "center";
        ctx.fillText(t.toFixed(tickStep < 1 ? 1 : 0), x, baselineY + 18);
      }
    }

    // Baseline axis
    ctx.strokeStyle = isLight ? "#94a3b8" : "#334155";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(padding.left, baselineY);
    ctx.lineTo(padding.left + plotW, baselineY);
    ctx.stroke();

    // Left Y Axis line
    ctx.beginPath();
    ctx.moveTo(padding.left, padding.top);
    ctx.lineTo(padding.left, baselineY);
    ctx.stroke();

    // Y Axis Ticks (Intensity / Atom Count)
    for (let i = 0; i <= maxIntensity; i += Math.max(1, Math.ceil(maxIntensity / 4))) {
      const y = mapY(i);
      ctx.fillStyle = isLight ? "#64748b" : "#94a3b8";
      ctx.font = "10px var(--font-mono, monospace)";
      ctx.textAlign = "right";
      ctx.fillText(i.toString(), padding.left - 8, y + 3);

      ctx.beginPath();
      ctx.moveTo(padding.left - 4, y);
      ctx.lineTo(padding.left, y);
      ctx.stroke();
    }

    // Axis Labels
    ctx.fillStyle = isLight ? "#334155" : "#cbd5e1";
    ctx.font = "12px Inter, sans-serif";
    ctx.textAlign = "center";

    const xLabel = isRefApplied
      ? `Chemical Shift δ (ppm)  [High ppm ➔ Low ppm (Reversed Axis)]`
      : `Absolute Isotropic Shielding σ (ppm)  [Uncalibrated Shielding]`;
    ctx.fillText(xLabel, padding.left + plotW / 2, height - 12);

    // Y Label (Rotated)
    ctx.save();
    ctx.translate(18, padding.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText("Relative Intensity / Atom Multiplicity", 0, 0);
    ctx.restore();

    // Spectrum Header Title
    ctx.fillStyle = isLight ? "#0f172a" : "#f8fafc";
    ctx.font = "bold 13px Inter, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(spectrum.title || `${currentNMRNucleus} NMR Calculated Spectrum`, padding.left, 24);

    // Draw NMR Stick Signals
    const stickColor = currentNMRNucleus === "1H" ? "#38bdf8" : "#34d399";
    const selectedColor = "#f59e0b";

    spectrum.peaks.forEach((peak, idx) => {
      const x = mapX(peak.position_ppm);
      const topY = mapY(peak.intensity);
      const isSelected = (selectedNMRPeakIndex === idx);
      const isHovered = (hoveredNMRPeakIndex === idx);

      // Stick Line
      ctx.strokeStyle = isSelected ? selectedColor : (isHovered ? "#60a5fa" : stickColor);
      ctx.lineWidth = isSelected ? 3.5 : (isHovered ? 2.5 : 2.0);
      ctx.beginPath();
      ctx.moveTo(x, baselineY);
      ctx.lineTo(x, topY);
      ctx.stroke();

      // Top Cap / Dot
      ctx.fillStyle = isSelected ? selectedColor : (isHovered ? "#60a5fa" : stickColor);
      ctx.beginPath();
      ctx.arc(x, topY, isSelected ? 5.5 : 4.0, 0, 2 * Math.PI);
      ctx.fill();

      if (isSelected) {
        ctx.strokeStyle = "rgba(245, 158, 11, 0.4)";
        ctx.lineWidth = 4;
        ctx.beginPath();
        ctx.arc(x, topY, 8.5, 0, 2 * Math.PI);
        ctx.stroke();
      }

      // Assignment Label Above Stick
      const labelText = peak.assignments.length > 0 ? peak.assignments.join(",") : `P${idx + 1}`;
      ctx.fillStyle = isSelected ? "#f59e0b" : (isLight ? "#475569" : "#94a3b8");
      ctx.font = isSelected ? "bold 11px var(--font-mono, monospace)" : "10px var(--font-mono, monospace)";
      ctx.textAlign = "center";
      ctx.fillText(labelText, x, topY - 8);
    });

    ctx.restore();
  }

  function drawNMRCanvas() {
    const canvas = document.getElementById("engine-nmr-canvas");
    if (!canvas) return;
    renderNMRSpectrumToCanvas(canvas, 1, false);
  }

  function updateNMRTable() {
    const tbody = document.getElementById("engine-nmr-tbody");
    const countEl = document.getElementById("nmr-peaks-count");
    if (!tbody || !currentNMRData) return;

    const spectrum = currentNMRNucleus === "1H" ? currentNMRData.h1_spectrum : currentNMRData.c13_spectrum;
    if (!spectrum || !spectrum.peaks || spectrum.peaks.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--text-muted);">No calculated ${currentNMRNucleus} NMR signals found.</td></tr>`;
      if (countEl) countEl.textContent = "0 signals assigned";
      return;
    }

    if (countEl) countEl.textContent = `${spectrum.peaks.length} signals assigned (${spectrum.atoms.length} active atoms)`;

    tbody.innerHTML = "";
    spectrum.peaks.forEach((peak, pIdx) => {
      peak.atoms.forEach((atom, aIdx) => {
        const tr = document.createElement("tr");
        tr.style.cursor = "pointer";
        if (selectedNMRPeakIndex === pIdx) {
          tr.style.backgroundColor = "rgba(56, 189, 248, 0.15)";
        }

        const coordStr = atom.coordinate ? atom.coordinate.map(c => c.toFixed(3)).join(", ") : "-";
        const deltaStr = atom.chemical_shift != null ? atom.chemical_shift.toFixed(4) : '<span style="color:var(--text-muted);">N/A (Shielding)</span>';
        const anisoStr = atom.anisotropy != null ? atom.anisotropy.toFixed(4) : "-";

        tr.innerHTML = `
          <td class="mono font-bold" style="color:var(--primary);">#${pIdx + 1}${peak.atoms.length > 1 ? `.${aIdx + 1}` : ''}</td>
          <td><strong>${atom.assignment}</strong></td>
          <td>${atom.element}</td>
          <td><span class="prop-badge badge-neutral" style="font-size:0.75rem;">${atom.isotope}</span></td>
          <td class="mono">${atom.isotropic_shielding.toFixed(4)}</td>
          <td class="mono" style="font-weight:600; color:var(--accent);">${deltaStr}</td>
          <td class="mono">${anisoStr}</td>
          <td><span class="prop-badge badge-primary">${atom.assignment}</span></td>
          <td class="mono" style="font-size:0.75rem; color:var(--text-muted);">${coordStr}</td>
        `;

        tr.addEventListener("click", () => {
          selectedNMRPeakIndex = pIdx;
          drawNMRCanvas();
          updateNMRTable();
        });

        tbody.appendChild(tr);
      });
    });
  }

  function initNMRStudio() {
    // Nucleus Switcher
    const tab1h = document.getElementById("nmr-tab-1h");
    const tab13c = document.getElementById("nmr-tab-13c");
    const nucBadge = document.getElementById("nmr-nucleus-badge");

    if (tab1h) {
      tab1h.addEventListener("click", () => {
        currentNMRNucleus = "1H";
        selectedNMRPeakIndex = null;
        if (tab1h) tab1h.className = "btn btn-primary btn-small is-active";
        if (tab13c) tab13c.className = "btn btn-ghost btn-small";
        if (nucBadge) nucBadge.textContent = "¹H NMR Active";
        updateNMRPresetOptions();
        recomputeNMRSpectrum();
      });
    }

    if (tab13c) {
      tab13c.addEventListener("click", () => {
        currentNMRNucleus = "13C";
        selectedNMRPeakIndex = null;
        if (tab13c) tab13c.className = "btn btn-primary btn-small is-active";
        if (tab1h) tab1h.className = "btn btn-ghost btn-small";
        if (nucBadge) nucBadge.textContent = "¹³C NMR Active";
        updateNMRPresetOptions();
        recomputeNMRSpectrum();
      });
    }

    // Reference Preset Select
    const refPresetSelect = document.getElementById("nmr-ref-preset");
    if (refPresetSelect) {
      refPresetSelect.addEventListener("change", () => {
        currentNMRReference = refPresetSelect.value;
        updateNMRRefCustomVisibility();
        recomputeNMRSpectrum();
      });
    }

    // Custom Reference Input
    const customRefInput = document.getElementById("nmr-custom-ref-val");
    if (customRefInput) {
      customRefInput.addEventListener("input", () => {
        recomputeNMRSpectrum();
      });
    }

    // Peak Grouping Toggle
    const groupToggle = document.getElementById("nmr-group-peaks-toggle");
    const groupTolWrap = document.getElementById("nmr-group-tol-wrap");
    const groupTolInput = document.getElementById("nmr-group-tol");

    if (groupToggle) {
      groupToggle.addEventListener("change", () => {
        nmrGroupPeaks = groupToggle.checked;
        if (groupTolWrap) groupTolWrap.style.display = nmrGroupPeaks ? "block" : "none";
        recomputeNMRSpectrum();
      });
    }

    if (groupTolInput) {
      groupTolInput.addEventListener("input", () => {
        const val = parseFloat(groupTolInput.value);
        if (!isNaN(val) && val > 0) {
          nmrGroupTolerance = val;
          recomputeNMRSpectrum();
        }
      });
    }

    // Interactive Hover & Click on Canvas
    const canvas = document.getElementById("engine-nmr-canvas");
    const tooltip = document.getElementById("nmr-tooltip");

    if (canvas && tooltip) {
      canvas.addEventListener("mousemove", (e) => {
        if (!currentNMRData) return;
        const spectrum = currentNMRNucleus === "1H" ? currentNMRData.h1_spectrum : currentNMRData.c13_spectrum;
        if (!spectrum || !spectrum.peaks || spectrum.peaks.length === 0) return;

        const rect = canvas.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;

        const scaleX = canvas.width / rect.width;
        const scaleY = canvas.height / rect.height;
        const cX = mouseX * scaleX;
        const cY = mouseY * scaleY;

        const padding = { top: 45, right: 45, bottom: 55, left: 65 };
        const plotW = canvas.width - padding.left - padding.right;
        const minPpm = spectrum.min_ppm;
        const maxPpm = spectrum.max_ppm;
        const isRefApplied = spectrum.is_reference_applied;

        const mapX = (ppm) => {
          if (maxPpm === minPpm) return padding.left + plotW / 2;
          return isRefApplied
            ? padding.left + ((maxPpm - ppm) / (maxPpm - minPpm)) * plotW
            : padding.left + ((ppm - minPpm) / (maxPpm - minPpm)) * plotW;
        };

        // Find nearest peak within 14px
        let foundIdx = null;
        let minDist = 14;

        spectrum.peaks.forEach((peak, idx) => {
          const px = mapX(peak.position_ppm);
          const dist = Math.abs(cX - px);
          if (dist < minDist) {
            minDist = dist;
            foundIdx = idx;
          }
        });

        hoveredNMRPeakIndex = foundIdx;
        drawNMRCanvas();

        if (foundIdx !== null) {
          const peak = spectrum.peaks[foundIdx];
          canvas.style.cursor = "pointer";
          tooltip.classList.remove("hidden");
          tooltip.style.left = `${mouseX + 15}px`;
          tooltip.style.top = `${mouseY - 10}px`;

          const posLabel = isRefApplied ? `Chemical Shift (δ): <strong>${peak.position_ppm.toFixed(4)} ppm</strong>` : `Isotropic Shielding (σ): <strong>${peak.position_ppm.toFixed(4)} ppm</strong>`;
          const atomNames = peak.assignments.join(", ");
          const shieldings = peak.atoms.map(a => `${a.assignment}: ${a.isotropic_shielding.toFixed(4)} ppm`).join("<br>");

          tooltip.innerHTML = `
            <div style="font-weight:bold; color:var(--primary); margin-bottom:0.25rem;">🔬 Signal #${foundIdx + 1} (${currentNMRNucleus})</div>
            <div style="font-size:0.8rem; margin-bottom:0.2rem;">Atom(s): <strong>${atomNames}</strong> (×${peak.intensity})</div>
            <div style="font-size:0.8rem; color:var(--accent); margin-bottom:0.25rem;">${posLabel}</div>
            <div style="font-size:0.75rem; color:var(--text-muted); border-top:1px solid rgba(255,255,255,0.1); padding-top:0.2rem;">
              <strong>Shielding Tensors:</strong><br>${shieldings}
            </div>
          `;
        } else {
          canvas.style.cursor = "default";
          tooltip.classList.add("hidden");
        }
      });

      canvas.addEventListener("mouseleave", () => {
        hoveredNMRPeakIndex = null;
        tooltip.classList.add("hidden");
        drawNMRCanvas();
      });

      canvas.addEventListener("click", () => {
        if (hoveredNMRPeakIndex !== null) {
          selectedNMRPeakIndex = hoveredNMRPeakIndex;
          drawNMRCanvas();
          updateNMRTable();
        }
      });
    }

    // Export Handlers
    const exportCsvBtn = document.getElementById("engine-nmr-export-csv");
    if (exportCsvBtn) {
      exportCsvBtn.addEventListener("click", () => {
        if (!currentNMRData || !currentNMRData.atoms) {
          showToast("No NMR data available for CSV export.");
          return;
        }
        const spectrum = currentNMRNucleus === "1H" ? currentNMRData.h1_spectrum : currentNMRData.c13_spectrum;
        const atoms = (spectrum && spectrum.atoms) ? spectrum.atoms : currentNMRData.atoms;

        let csv = "atom_index,element,isotope,assignment,isotropic_shielding_ppm,chemical_shift_ppm,anisotropy_ppm,coord_x,coord_y,coord_z\n";
        atoms.forEach(a => {
          const cx = a.coordinate ? a.coordinate[0] : "";
          const cy = a.coordinate ? a.coordinate[1] : "";
          const cz = a.coordinate ? a.coordinate[2] : "";
          const cs = a.chemical_shift != null ? a.chemical_shift.toFixed(4) : "";
          const aniso = a.anisotropy != null ? a.anisotropy.toFixed(4) : "";
          csv += `${a.atom_index},${a.element},${a.isotope},${a.assignment},${a.isotropic_shielding.toFixed(4)},${cs},${aniso},${cx},${cy},${cz}\n`;
        });

        const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `calculated_nmr_${currentNMRNucleus}.csv`;
        link.click();
        showToast(`Exported ${currentNMRNucleus} NMR data to CSV.`);
      });
    }

    const exportJsonBtn = document.getElementById("engine-nmr-export-json");
    if (exportJsonBtn) {
      exportJsonBtn.addEventListener("click", () => {
        if (!currentNMRData) {
          showToast("No NMR data available for JSON export.");
          return;
        }
        const blob = new Blob([JSON.stringify(currentNMRData, null, 2)], { type: "application/json" });
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `calculated_nmr_data.json`;
        link.click();
        showToast("Exported complete NMR calculation payload to JSON.");
      });
    }

    const downloadPngBtn = document.getElementById("engine-nmr-download-png");
    if (downloadPngBtn) {
      downloadPngBtn.addEventListener("click", () => {
        if (!currentNMRData) {
          showToast("No NMR data loaded.");
          return;
        }
        const offCanvas = document.createElement("canvas");
        const scale = 2.5;
        offCanvas.width = 1000 * scale;
        offCanvas.height = 420 * scale;
        renderNMRSpectrumToCanvas(offCanvas, scale, true);

        offCanvas.toBlob((blob) => {
          if (!blob) return;
          const link = document.createElement("a");
          link.href = URL.createObjectURL(blob);
          link.download = `calculated_nmr_${currentNMRNucleus}_spectrum.png`;
          link.click();
          showToast("High-resolution NMR spectrum exported as PNG.");
        });
      });
    }
  }
  initNMRStudio();

})();




/* ─── Theme toggle (light/dark) - Chemistry Lab UI refresh ────────────────
   style.css defines the dark palette on :root via design tokens; theme.css
   redefines those tokens for html[data-theme="light"]. The choice persists
   in localStorage; first visit follows the OS preference. */
(function () {
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    var btn = document.getElementById('theme-toggle');
    if (btn) {
      btn.textContent = theme === 'dark' ? '\u2600\uFE0F' : '\uD83C\uDF19';
      btn.setAttribute('aria-pressed', String(theme === 'dark'));
    }
  }
  var saved = null;
  try { saved = localStorage.getItem('chemlab_theme'); } catch (_) {}
  var prefersDark = false;
  try { prefersDark = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches); } catch (_) {}
  applyTheme(saved === 'dark' || saved === 'light' ? saved : (prefersDark ? 'dark' : 'light'));
  var btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', function () {
      var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      try { localStorage.setItem('chemlab_theme', next); } catch (_) {}
    });
  }
})();