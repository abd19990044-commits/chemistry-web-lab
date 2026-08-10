(() => {
  "use strict";

  const CFG = JSON.parse(document.getElementById("orca-config").textContent);
  const LS_KEYS = {
    kaggleUsername: "chemlab_kaggle_username",
    kaggleKey: "chemlab_kaggle_key",
    orcaSourceKind: "chemlab_orca_source_kind",
    orcaDataset: "chemlab_orca_dataset",
    orcaLink: "chemlab_orca_link",
    jobs: "chemlab_jobs",
    removedJobIds: "chemlab_removed_job_ids",
  };

  // The Kaggle key is held here for the session instead of being copied into
  // every saved job entry — that copy survived "remember me" being unticked and
  // survived Sign out, which contradicted the site's own privacy text.
  //
  // Declared HERE, at the top of the scope, on purpose: `let` bindings are in a
  // temporal dead zone until their declaration is evaluated, and `autoSignIn()`
  // below runs during page load. With the declaration further down the file
  // that assignment threw "Cannot access 'sessionKaggleKey' before
  // initialization", which aborted the rest of the script — so the Kaggle
  // sign-in form never got its submit handler and logging in did nothing.
  let sessionKaggleKey = "";

  // ───────────────────────────────────────────────
  // View routing: home -> draw / orca
  // ───────────────────────────────────────────────
  const views = document.querySelectorAll(".view");
  const backHomeBtn = document.getElementById("back-home-btn");
  const brandHomeBtn = document.getElementById("brand-home-btn");

  function showView(name) {
    views.forEach(v => v.classList.toggle("is-active", v.id === `${name}-view`));
    backHomeBtn.classList.toggle("hidden", name === "home");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  document.querySelectorAll(".choice-card").forEach(card => {
    card.addEventListener("click", () => showView(card.dataset.open));
  });
  backHomeBtn.addEventListener("click", () => showView("home"));
  brandHomeBtn.addEventListener("click", () => showView("home"));
  brandHomeBtn.addEventListener("keydown", (e) => { if (e.key === "Enter") showView("home"); });

  // Generic in-page navigation for anything marked data-view-link — used by
  // the footer's "Legal, Privacy & Disclaimer" link and by inline mentions
  // of it inside the Kaggle help disclosures below.
  document.querySelectorAll("[data-view-link]").forEach(el => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      showView(el.dataset.viewLink);
    });
  });

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
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({ ok: false, error: "Invalid response from server." }));
    if (!resp.ok || !data.ok) throw new Error(data.error || "An unexpected error occurred.");
    return data;
  }
  function showError(el, message) { el.textContent = message; el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }
  function show(el) { el.classList.remove("hidden"); }

  function showToast(html, timeoutMs = 9000) {
    const stack = document.getElementById("toast-stack");
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.innerHTML = html;
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
      showToast(`<strong>Sign-in failed</strong><br>${err.message}`);
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
      document.getElementById("explorer-title-out").textContent = data.title || data.iupac_name || query;
      document.getElementById("explorer-formula").textContent = data.formula || "—";
      document.getElementById("explorer-weight").textContent = data.weight ? `${data.weight} g/mol` : "—";
      document.getElementById("explorer-smiles").textContent = data.smiles;

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

  document.getElementById("explorer-send-to-orca").addEventListener("click", () => {
    if (!lastCompound) return;
    showView("orca");
    switchToTab("orca");
    openWizard({ prefillQuery: lastCompound.query });
  });

  // ───────────────────────────────────────────────
  // Reaction Drawing
  // ───────────────────────────────────────────────
  const reactionForm = document.getElementById("reaction-form");
  const reactionError = document.getElementById("reaction-error");
  const reactionLoading = document.getElementById("reaction-loading");
  const reactionResult = document.getElementById("reaction-result");

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
      });
      document.getElementById("reaction-image").src = b64ToDataUrl(data.image_png_base64, "image/png");
      setDownload(document.getElementById("reaction-download-rxn"), data.rxn_file_base64, "chemical/x-mdl-rxnfile", "reaction.rxn");
      document.getElementById("reaction-smiles").textContent = data.reaction_smiles;
      document.getElementById("reaction-equation").textContent = data.equation || "";

      // Whether the equation balances. Shown, never enforced — a mechanism step
      // or a fragment is legitimately unbalanced, and the failure this guards
      // against is not noticing.
      const balanceEl = document.getElementById("reaction-balance");
      const balance = data.balance || {};
      balanceEl.className = "reaction-badge " + (balance.balanced ? "badge-ok" : "badge-warn");
      balanceEl.textContent = (balance.balanced ? "✓ " : "⚠ ") + (balance.message || "");
      balanceEl.classList.toggle("hidden", !balance.message);

      // The file check. This reports a STRUCTURAL validation of the MDL RXN
      // block — ChemDraw is not installed on the server and cannot be, so the
      // honest claim is "this is a well-formed file in the format ChemDraw
      // imports", which is exactly what was verified.
      const fileEl = document.getElementById("reaction-filecheck");
      const report = data.file_report || {};
      if (report.valid) {
        fileEl.className = "reaction-badge badge-ok";
        fileEl.textContent = `✓ Valid ${report.format} file (${report.reactants} reactant `
          + `component${report.reactants === 1 ? "" : "s"}, ${report.products} product `
          + `component${report.products === 1 ? "" : "s"}) — opens in `
          + `${(report.opens_in || []).join(", ")}.`;
      } else {
        fileEl.className = "reaction-badge badge-warn";
        fileEl.textContent = "⚠ The reaction file did not pass its format check: "
          + ((report.problems || []).join("; ") || "unknown problem")
          + ". Please report this — the drawing above is still correct.";
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
  // there is something on screen — toggling it before the first draw would fire
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
    show(modal);
    renderStep("coords", opts);
  }
  function closeWizard() { hide(modal); }

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
    modalBody.innerHTML = "";
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
    tabs.append(tabSearch, tabUpload, tabManual);
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

    fetchBtn.addEventListener("click", async () => {
      const q = nameInput.value.trim();
      if (!q) return;
      fetchBtn.disabled = true;
      searchStatus.textContent = "Looking it up…";
      try {
        const data = await postJSON("/api/orca/coords", { query: q });
        wizard.coords = data.coords;
        wizard.name = data.name;
        setCoordsStatus(`${data.name} (${data.formula || "—"})`);
        renderStep("calc");
      } catch (err) {
        searchStatus.textContent = err.message;
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
        renderStep("calc");
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
    manualBtn.textContent = "Continue";
    manualPane.append(manualField, manualNameField, manualBtn);

    manualBtn.addEventListener("click", () => {
      const coords = manualTextarea.value.trim();
      if (!coords) return;
      wizard.coords = coords;
      wizard.name = manualNameInput.value.trim() || "molecule";
      setCoordsStatus(`${wizard.name} (manual entry)`);
      renderStep("calc");
    });

    modalBody.append(searchPane, uploadPane, manualPane);

    function activateTab(tab, pane) {
      [tabSearch, tabUpload, tabManual].forEach(t => t.classList.remove("is-active"));
      [searchPane, uploadPane, manualPane].forEach(p => p.classList.add("hidden"));
      tab.classList.add("is-active");
      pane.classList.remove("hidden");
    }
    tabSearch.addEventListener("click", () => activateTab(tabSearch, searchPane));
    tabUpload.addEventListener("click", () => activateTab(tabUpload, uploadPane));
    tabManual.addEventListener("click", () => activateTab(tabManual, manualPane));
  }

  function setCoordsStatus(text) {
    coordsStatus.textContent = `✔ Selected structure: ${text}`;
    coordsStatus.classList.add("is-set");
  }

  function stepCalc() {
    setModalChrome(2, "Calculation type");
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
    field.innerHTML = `<label>The first line must start with "!". You can add extra lines/blocks too, e.g. %geom ... end or %neb ... end — cores/RAM and the coordinate block are still appended automatically after this.</label>`;
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
      const isDLPNO = val.includes("DLPNO");
      if (wizard.family === "f_dft" && !isDLPNO) {
        renderStep("dispersion");
      } else if (isDLPNO || wizard.family === "f_comp") {
        wizard.ri_type = "none"; wizard.disp = "none";
        if (wizard.family === "f_comp") renderStep("solvation");
        else renderStep("basisFamily");
      } else {
        wizard.disp = "none";
        renderStep("ri");
      }
    }, wizard.theory));
    modalBody.appendChild(backButton(goBack));
  }

  function stepDispersion() {
    setModalChrome(5, "Dispersion model");
    modalBody.appendChild(optionGrid(CFG.dispersion_models, 2, (val) => {
      wizard.disp = val;
      renderStep("ri");
    }, wizard.disp));
    modalBody.appendChild(backButton(goBack));
  }

  function stepRI() {
    setModalChrome(6, "Acceleration (RI)");
    modalBody.appendChild(optionGrid(CFG.ri_options, 1, (val) => {
      wizard.ri_type = val;
      renderStep("basisFamily");
    }, wizard.ri_type));
    modalBody.appendChild(backButton(goBack));
  }

  const BASIS_FAMILIES = { def2: "Ahlrichs (def2)", dunning: "Dunning (cc-pV)", pople: "Pople (6-31G)" };

  function stepBasisFamily() {
    setModalChrome(7, "Basis set family");
    modalBody.appendChild(optionGrid(BASIS_FAMILIES, 1, (val) => {
      renderStep("basis", { family: val });
    }));
    modalBody.appendChild(backButton(goBack));
  }

  function stepBasis(opts) {
    setModalChrome(8, "Basis set");
    modalBody.appendChild(optionGrid(CFG.basis_map[opts.family], 2, (val) => {
      wizard.basis = val;
      renderStep("solvation");
    }, wizard.basis));
    modalBody.appendChild(backButton(goBack));
  }

  function stepSolvation() {
    setModalChrome(9, "Solvation model");
    modalBody.appendChild(optionGrid(CFG.solvation_models, 1, (val) => {
      wizard.solv_model = val;
      if (val === "none") renderStep("x2c");
      else renderStep("solvent");
    }, wizard.solv_model));
    modalBody.appendChild(backButton(goBack));
  }

  function stepSolvent() {
    setModalChrome(10, "Choose the solvent");
    modalBody.appendChild(optionGrid(CFG.solvents, 2, (val) => {
      wizard.solvent = val;
      renderStep("x2c");
    }, wizard.solvent));
    modalBody.appendChild(backButton(goBack));
  }

  function stepX2C() {
    setModalChrome(11, "Enable X2C (scalar relativistic)?");
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
      stepNumber: 12, title: "Molecular charge", label: "Charge", placeholder: "0",
      min: -10, max: 10, value: 0, key: "charge",
      errorText: "Enter a whole number between -10 and 10.", nextStep: "multiplicity",
    });
  }

  function stepMultiplicity() {
    numericStep({
      stepNumber: 13, title: "Spin multiplicity", label: "Multiplicity", placeholder: "1",
      min: 1, max: 20, value: 1, key: "mult",
      errorText: "Enter a whole number between 1 and 20.",
      nextStep: wizard.calc_type === "tddft" ? "nroots" : "cores",
    });
  }

  function stepNroots() {
    numericStep({
      stepNumber: 14, title: "Number of excited states (nroots)", label: "nroots", placeholder: "10",
      min: 1, max: 200, value: 10, key: "nroots",
      errorText: "Enter a whole number between 1 and 200.", nextStep: "cores",
    });
  }

  function stepCores() {
    numericStep({
      stepNumber: 15, title: "Processor cores", label: "Cores", placeholder: "4",
      min: 1, max: 128, value: 4, key: "cores",
      errorText: "Enter a whole number between 1 and 128.", nextStep: "ram",
    });
  }

  // A Kaggle CPU session has ~30 GB of RAM, and ORCA treats %maxcore as a soft
  // target it regularly exceeds, so the runner only lets ORCA have ~70% of the
  // machine. Anything above that is silently reduced at run time — which is a
  // nasty surprise to find by diffing the .inp against what you typed. Show the
  // arithmetic here instead, while it can still be changed.
  const SESSION_RAM_MB = 30000;
  const SESSION_RAM_USABLE = Math.round(SESSION_RAM_MB * 0.70);

  function stepRam() {
    setModalChrome(16, "RAM per core (MB)");
    const { field, input } = numberField("RAM per core (MB)", "6000", wizard.ram ?? 6000);
    const budget = document.createElement("p");
    budget.className = "modal-hint";
    const updateBudget = () => {
      const perCore = Number(input.value);
      const cores = Number(wizard.cores) || 1;
      if (!Number.isFinite(perCore) || perCore <= 0) { budget.textContent = ""; return; }
      const total = perCore * cores;
      const fits = total <= SESSION_RAM_USABLE;
      const perCoreFits = Math.floor(SESSION_RAM_USABLE / cores);
      budget.textContent = fits
        ? `${perCore} MB × ${cores} core(s) = ${total} MB. Fits within the ~${SESSION_RAM_USABLE} MB a Kaggle session can safely give ORCA.`
        : `⚠ ${perCore} MB × ${cores} core(s) = ${total} MB, above the ~${SESSION_RAM_USABLE} MB a Kaggle session can safely give ORCA. `
          + `It will be reduced to ${perCoreFits} MB per core at run time. To keep ${perCore} MB, use ${Math.max(1, Math.floor(SESSION_RAM_USABLE / perCore))} core(s) instead.`;
      budget.style.color = fits ? "" : "var(--amber)";
    };
    input.addEventListener("input", updateBudget);
    updateBudget();
    const err = document.createElement("p");
    err.className = "modal-hint";
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    actions.append(backButton(goBack));
    const genBtn = document.createElement("button");
    genBtn.type = "button"; genBtn.className = "btn btn-primary"; genBtn.textContent = "🧪 Generate file";
    genBtn.addEventListener("click", async () => {
      const n = Number(input.value);
      if (!Number.isFinite(n) || n < 100 || n > 64000) { err.textContent = "Enter a value between 100 and 64000."; return; }
      wizard.ram = n;
      genBtn.disabled = true;
      err.textContent = "Generating…";
      try {
        const data = await postJSON("/api/orca/generate", { ...wizard, name: wizard.name });
        document.getElementById("orca-output").textContent = data.input_text;
        setDownload(document.getElementById("orca-download-inp"), data.file_base64, "text/plain", data.filename);
        show(document.getElementById("orca-output-wrap"));
        lastOrcaFile = { filename: data.filename, content: data.input_text };
        closeWizard();
        document.getElementById("orca-output-wrap").scrollIntoView({ behavior: "smooth" });
      } catch (e) {
        err.textContent = e.message;
      } finally {
        genBtn.disabled = false;
      }
    });
    actions.append(genBtn);
    modalBody.append(field, budget, err, actions);
  }

  const STEP_RENDERERS = {
    coords: stepCoords, calc: stepCalc, customLine: stepCustomLine, family: stepFamily,
    method: stepMethod, dispersion: stepDispersion, ri: stepRI, basisFamily: stepBasisFamily,
    basis: stepBasis, solvation: stepSolvation, solvent: stepSolvent, x2c: stepX2C,
    charge: stepCharge, multiplicity: stepMultiplicity, nroots: stepNroots, cores: stepCores, ram: stepRam,
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
        if (local.jobId !== rj.job_id) {
          const known = new Set((local.chain || []).map(w => w.id));
          local.jobId = rj.job_id;
          local.kaggleUrl = rj.kaggle_url || local.kaggleUrl;
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
        kaggleUsername: username,
        status: "unknown",
        submittedAt,
      });
      added++;
    });
    if (added > 0) saveJobs(jobs);
    renderJobs();
  }

  async function attemptLogin(username, key, opts) {
    const silent = opts && opts.silent;
    if (!silent) { hide(kaggleLoginError); show(kaggleLoginLoading); }
    try {
      const data = await postJSON("/api/kaggle/login", { kaggle_username: username, kaggle_key: key });
      setSignedIn(username, key);
      mergeRemoteJobs(username, key, data.jobs || []);
      pollAllActiveJobs();
      return true;
    } catch (err) {
      if (!silent) showError(kaggleLoginError, err.message);
      return false;
    } finally {
      if (!silent) hide(kaggleLoginLoading);
    }
  }

  // If credentials were remembered from a previous visit, sign in silently.
  (function autoSignIn() {
    const savedUser = localStorage.getItem(LS_KEYS.kaggleUsername);
    const savedKey = localStorage.getItem(LS_KEYS.kaggleKey);
    if (savedUser && savedKey) {
      kaggleUsernameInput.value = savedUser;
      kaggleKeyInput.value = savedKey;
      sessionKaggleKey = savedKey;
      attemptLogin(savedUser, savedKey, { silent: true });
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
    if (ok) sessionKaggleKey = key;
    if (ok && kaggleRememberBox.checked) {
      localStorage.setItem(LS_KEYS.kaggleUsername, username);
      localStorage.setItem(LS_KEYS.kaggleKey, key);
    } else if (ok) {
      localStorage.removeItem(LS_KEYS.kaggleUsername);
      localStorage.removeItem(LS_KEYS.kaggleKey);
    }
  });

  document.getElementById("kaggle-signout-btn").addEventListener("click", () => {
    localStorage.removeItem(LS_KEYS.kaggleUsername);
    localStorage.removeItem(LS_KEYS.kaggleKey);
    sessionKaggleKey = "";
    // Any credential a previous version left inside the job list goes too, so
    // "Sign out removes it" is true of every copy.
    const jobs = loadJobs();
    jobs.forEach(j => delete j.kaggleKey);
    saveJobs(jobs);
    setSignedOut();
  });

  // ORCA source toggle: Kaggle Dataset <-> Google Drive / direct link.
  const datasetRow = document.getElementById("kaggle-dataset-row");
  const orcaLinkRow = document.getElementById("kaggle-orca-link-row");
  const datasetInput = document.getElementById("kaggle-dataset");
  const orcaLinkInput = document.getElementById("kaggle-orca-link");
  const orcaSourceLinkRadio = document.getElementById("orca-source-link");

  function applyOrcaSourceToggle() {
    const useLink = orcaSourceLinkRadio.checked;
    datasetRow.classList.toggle("hidden", useLink);
    orcaLinkRow.classList.toggle("hidden", !useLink);
  }

  // The dataset identifier and the Drive link are the same for every job a
  // person submits — they point at their own copy of ORCA — so retyping them
  // on every visit is pure friction. They are remembered on this device the
  // same way the username is. Neither is a credential: a Kaggle Dataset ref is
  // a public-shaped identifier, and the link is one the person pasted in
  // themselves; both stay in their own browser and are cleared with the
  // "Forget" control below.
  function rememberOrcaSource() {
    localStorage.setItem(LS_KEYS.orcaSourceKind,
                         orcaSourceLinkRadio.checked ? "link" : "dataset");
    localStorage.setItem(LS_KEYS.orcaDataset, datasetInput.value.trim());
    localStorage.setItem(LS_KEYS.orcaLink, orcaLinkInput.value.trim());
  }

  (function restoreOrcaSource() {
    const savedDataset = localStorage.getItem(LS_KEYS.orcaDataset);
    const savedLink = localStorage.getItem(LS_KEYS.orcaLink);
    if (savedDataset) datasetInput.value = savedDataset;
    if (savedLink) orcaLinkInput.value = savedLink;
    if (localStorage.getItem(LS_KEYS.orcaSourceKind) === "link") {
      orcaSourceLinkRadio.checked = true;
    }
    applyOrcaSourceToggle();
  })();

  [datasetInput, orcaLinkInput].forEach(el => el.addEventListener("change", rememberOrcaSource));
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
      datasetInput.value = "";
      orcaLinkInput.value = "";
      document.getElementById("orca-source-dataset").checked = true;
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

  kaggleForm.addEventListener("submit", async (e) => {
    e.preventDefault();
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

    const form = new FormData();
    form.append("kaggle_username", username);
    form.append("kaggle_key", key);
    form.append("dataset_sources", useOrcaLink ? "" : document.getElementById("kaggle-dataset").value.trim());
    form.append("orca_link", useOrcaLink ? document.getElementById("kaggle-orca-link").value.trim() : "");
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
      const resp = await fetch("/api/kaggle/submit", { method: "POST", body: form });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || "Submission failed.");
      rememberOrcaSource();
      kaggleResult.innerHTML = `${data.message}<br><a href="${data.kaggle_url}" target="_blank" rel="noopener">${data.kaggle_url}</a>`;
      show(kaggleResult);

      addJob({
        name: data.job_title || jobLabel,
        jobId: data.job_id,
        // rootId never changes, so the job keeps one identity in this list no
        // matter how many continuation notebooks it runs through. `chain`
        // keeps every window's own link: a long calculation spans several
        // Kaggle notebooks, and losing the earlier ones means losing the
        // output they hold.
        rootId: data.job_id,
        chainIds: [data.job_id],
        chain: [{ id: data.job_id, url: data.kaggle_url }],
        kaggleUrl: data.kaggle_url,
        kaggleUsername: username,
        status: "queued",
        submittedAt: Date.now(),
      });
      switchToTab("jobs");
    } catch (err) {
      showError(kaggleError, err.message);
    } finally {
      hide(kaggleLoading);
    }
  });

  // ───────────────────────────────────────────────
  // Jobs tracker (localStorage + polling)
  // ───────────────────────────────────────────────
  const jobsListEl = document.getElementById("jobs-list");
  const jobsEmptyEl = document.getElementById("jobs-empty");

  function credsFor(job) {
    return {
      kaggle_username: job.kaggleUsername || localStorage.getItem(LS_KEYS.kaggleUsername) || "",
      kaggle_key: sessionKaggleKey || localStorage.getItem(LS_KEYS.kaggleKey) || "",
    };
  }

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
    jobs.unshift(job);
    saveJobs(jobs);
    renderJobs();
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
    queued: "Queued", running: "Running", restarting: "Restarting",
    complete: "Complete", error: "Error", cancelled: "Cancelled", unknown: "Unknown",
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

      const statusBadge = document.createElement("span");
      statusBadge.className = `job-status status-${job.status}`;
      statusBadge.textContent = STATUS_LABELS[job.status] || job.status;
      nameRow.appendChild(statusBadge);

      const timerBadge = document.createElement("span");
      timerBadge.className = "job-timer";
      timerBadge.dataset.submitted = String(job.submittedAt);
      if (job.finishedAt) timerBadge.dataset.finished = String(job.finishedAt);
      timerBadge.textContent = `⏱ ${formatElapsed((job.finishedAt || Date.now()) - job.submittedAt)}`;
      nameRow.appendChild(timerBadge);

      if (job.status === "complete") {
        const downloadBtn = document.createElement("button");
        downloadBtn.type = "button";
        downloadBtn.className = "job-download-link";
        downloadBtn.textContent = "⬇ Download results";
        downloadBtn.addEventListener("click", () => downloadJobResults(job, downloadBtn));
        nameRow.appendChild(downloadBtn);
      }

      const meta = document.createElement("div");
      meta.className = "job-meta";
      const restartNote = job.restarts
        ? ` — continued ${job.restarts} time${job.restarts > 1 ? "s" : ""}`
        : "";
      meta.textContent = `${job.jobId} — submitted ${new Date(job.submittedAt).toLocaleString()}${restartNote}`;

      info.append(nameRow, meta);

      // Every notebook this job has run in, oldest first. The current one is
      // also the "View on Kaggle" button below; these are here so a link is
      // never lost when a job continues into a new notebook.
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
        const shown = job.warning.length > 180 ? `${job.warning.slice(0, 177)}…` : job.warning;
        warn.textContent = `⚠ ${shown}`;
        warn.title = job.warning;
        info.appendChild(warn);
      }

      const actions = document.createElement("div");
      actions.className = "job-actions";
      const kaggleLink = document.createElement("a");
      kaggleLink.className = "btn btn-ghost btn-small";
      kaggleLink.href = job.kaggleUrl;
      kaggleLink.target = "_blank";
      kaggleLink.rel = "noopener";
      kaggleLink.textContent = "View on Kaggle";
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "job-remove-btn";
      removeBtn.textContent = "Delete";
      removeBtn.title = "Permanently delete this job's notebook from Kaggle";
      removeBtn.addEventListener("click", () => removeJob(job.jobId));
      actions.append(kaggleLink, removeBtn);

      li.append(info, actions);
      jobsListEl.appendChild(li);
    });
  }

  // Ticks every running timer once a second, independent of the 45s status poll.
  setInterval(() => {
    document.querySelectorAll(".job-timer").forEach(el => {
      const submitted = Number(el.dataset.submitted);
      const finished = el.dataset.finished ? Number(el.dataset.finished) : Date.now();
      el.textContent = `⏱ ${formatElapsed(finished - submitted)}`;
    });
  }, 1000);

  async function pollJob(job) {
    try {
      const data = await postJSON("/api/kaggle/status", {
        ...credsFor(job), job_id: job.jobId,
      });

      if (data.status === "restarting" && data.next_job_id) {
        // The window ended (time limit, disk limit, or ORCA's own cycle
        // budget) and pushed a continuation notebook, which the server has
        // already confirmed exists. Follow it — but ADD it to the chain
        // instead of replacing what came before: every window holds its own
        // ORCA output, so the earlier links have to stay reachable. The card
        // itself keeps its original name and rootId, so from here the job
        // still looks like one job.
        const chainIds = (job.chainIds && job.chainIds.length) ? job.chainIds : [job.jobId];
        const chain = (job.chain && job.chain.length)
          ? job.chain : [{ id: job.jobId, url: job.kaggleUrl }];
        const nextUrl = data.next_kaggle_url || job.kaggleUrl;
        if (chainIds.includes(data.next_job_id)) return;   // already followed
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
        updateJob(job.jobId, { status: "complete", finishedAt: Date.now(), warning: data.warning || null });
        if (data.warning) {
          showToast(`<strong>${job.name}</strong> stopped early — only partial results are available. Open the Jobs tab for details.`);
        } else {
          showToast(`<strong>${job.name}</strong> finished. Open the Jobs tab to download the results.`);
        }
        return;
      }
      const patch = { status: data.status };
      if (TERMINAL_STATUSES.includes(data.status)) patch.finishedAt = Date.now();
      // A kernel that dies before writing JOB_NOTE.txt (dataset mount failure,
      // OOM during import, internet disabled on an unverified account) used to
      // show a bare red "Error" badge with no text at all. Kaggle's own failure
      // message is in `note`; it is the only explanation available.
      if (data.warning) patch.warning = data.warning;
      else if (data.status === "error" && data.note) patch.warning = data.note;
      updateJob(job.jobId, patch);
    } catch (err) {
      // Leave status as-is; a transient network/API error shouldn't flip the badge to "error".
      console.warn(`Status check failed for ${job.jobId}:`, err.message);
    }
  }

  async function downloadJobResults(job, buttonEl) {
    const originalLabel = buttonEl.textContent;
    buttonEl.disabled = true;
    buttonEl.textContent = "Fetching from Kaggle…";
    try {
      const resp = await fetch("/api/kaggle/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...credsFor(job), job_id: job.jobId }),
      });
      if (!resp.ok) {
        let message = "Could not fetch results from Kaggle.";
        try { const errData = await resp.json(); message = errData.error || message; } catch { /* non-JSON error body */ }
        throw new Error(message);
      }
      const blob = await resp.blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = `${job.jobId}_results.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
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
  }

  document.getElementById("jobs-refresh-btn").addEventListener("click", pollAllActiveJobs);

  // Browsers heavily throttle (or fully suspend) setInterval timers in
  // backgrounded/minimized tabs — exactly the situation for most of an
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
    downloadBtn.type = 'button'; downloadBtn.className = 'btn btn-ghost btn-small'; downloadBtn.textContent = '⬇ Save kaggle.json';
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
      const blob = new Blob([JSON.stringify({username, key}, null, 2) + '\n'], {type:'application/json'});
      const url = URL.createObjectURL(blob); const a = document.createElement('a');
      a.href = url; a.download = 'kaggle.json'; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });

    function isolateLocalJobsTo(username) {
      try { saveJobs(loadJobs().filter(j => !j.kaggleUsername || j.kaggleUsername.toLowerCase() === username.toLowerCase())); renderJobs(); } catch (_) {}
    }
    const observer = new MutationObserver(() => { if (currentKaggle?.username) isolateLocalJobsTo(currentKaggle.username); });
    if (kaggleSignedInAs) observer.observe(kaggleSignedInAs, {childList:true, characterData:true, subtree:true});

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
      function refresh() { const raw = linkInput.value.trim(), url = direct(raw); open.href = raw || '#'; download.href = url || '#'; open.style.pointerEvents = raw ? '' : 'none'; download.style.pointerEvents = url ? '' : 'none'; status.textContent = raw ? 'Ready — this link can also be opened/downloaded in the browser.' : ''; }
      linkInput.addEventListener('input', refresh); linkInput.addEventListener('change', refresh); tools2.append(open, download, status); wrap.appendChild(tools2); refresh();
    }
  })();

})();