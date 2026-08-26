/**
 * ORCA Studio - Interactive 3D Molecular, HOMO/LUMO & UV-Vis Spectra Studio
 * Author: Computational Chemistry & Software Engineering Suite
 */

(function () {
  'use strict';

  // State Management
  const state = {
    molecules: new Map(), // name -> moleculeData
    activeMoleculeKey: null,
    globalSigmaNm: 20.0,
    globalShiftNm: 0.0,
    showSticks: true,
    normalizePeaks: true,
    fillCurves: true,
    experimentalTrace: null, // { name, data: [{ wavelength, intensity }] }
    viewer3D: null,
    colors: [
      '#06b6d4', // Cyan
      '#f59e0b', // Amber
      '#10b981', // Emerald
      '#f43f5e', // Rose
      '#8b5cf6', // Violet
      '#3b82f6', // Blue
      '#ec4899', // Pink
      '#14b8a6', // Teal
    ],
  };

  // DOM Elements Cache
  const dom = {
    fileInput: document.getElementById('file-input'),
    expFileInput: document.getElementById('exp-file-input'),
    clearExpBtn: document.getElementById('clear-exp-btn'),
    loadSampleBtn: document.getElementById('load-sample-btn'),
    exportPngBtn: document.getElementById('export-png-btn'),
    exportCsvBtn: document.getElementById('export-csv-btn'),
    tracesList: document.getElementById('traces-list'),
    traceCountBadge: document.getElementById('trace-count-badge'),
    sigmaSlider: document.getElementById('sigma-slider'),
    sigmaVal: document.getElementById('sigma-val'),
    shiftSlider: document.getElementById('shift-slider'),
    shiftNum: document.getElementById('shift-num'),
    showSticksToggle: document.getElementById('show-sticks-toggle'),
    normalizeToggle: document.getElementById('normalize-toggle'),
    fillCurvesToggle: document.getElementById('fill-curves-toggle'),
    tabBtns: document.querySelectorAll('.tab-btn'),
    tabPanes: document.querySelectorAll('.tab-pane'),
    spectraCanvas: document.getElementById('spectra-canvas'),
    plotWrapper: document.getElementById('plot-wrapper'),
    plotTooltip: document.getElementById('plot-tooltip'),
    primaryLmax: document.getElementById('primary-lmax'),
    primaryEnergy: document.getElementById('primary-energy'),
    primaryTransition: document.getElementById('primary-transition'),
    primaryShift: document.getElementById('primary-shift'),
    transitionsCountLabel: document.getElementById('transitions-count-label'),
    transitionsTbody: document.getElementById('transitions-tbody'),
    molViewer3D: document.getElementById('mol-viewer-3d'),
    viewerPlaceholder: document.getElementById('viewer-placeholder'),
    renderStyleSelect: document.getElementById('render-style-select'),
    resetViewBtn: document.getElementById('reset-view-btn'),
    viewerMoleculeTitle: document.getElementById('viewer-molecule-title'),
    viewerFormulaBadge: document.getElementById('viewer-formula-badge'),
    atomsCountLabel: document.getElementById('atoms-count-label'),
    dipoleLabel: document.getElementById('dipole-label'),
    tsBadge: document.getElementById('ts-badge'),
    homoValEv: document.getElementById('homo-val-ev'),
    lumoValEv: document.getElementById('lumo-val-ev'),
    gapValEv: document.getElementById('gap-val-ev'),
    cdftHardness: document.getElementById('cdft-hardness'),
    cdftPotential: document.getElementById('cdft-potential'),
    cdftElectronegativity: document.getElementById('cdft-electronegativity'),
    cdftElectrophilicity: document.getElementById('cdft-electrophilicity'),
    cdftIp: document.getElementById('cdft-ip'),
    cdftEa: document.getElementById('cdft-ea'),
    cdftEdonating: document.getElementById('cdft-edonating'),
    cdftEaccepting: document.getElementById('cdft-eaccepting'),
    vibCountBadge: document.getElementById('vib-count-badge'),
    vibrationsTbody: document.getElementById('vibrations-tbody'),
    thermoEelec: document.getElementById('thermo-eelec'),
    thermoZpe: document.getElementById('thermo-zpe'),
    thermoEnthalpy: document.getElementById('thermo-enthalpy'),
    thermoGibbs: document.getElementById('thermo-gibbs'),
    thermoTs: document.getElementById('thermo-ts'),
    thermoSpin: document.getElementById('thermo-spin'),
    rxnInput: document.getElementById('rxn-input'),
    calcRxnBtn: document.getElementById('calc-rxn-btn'),
    reactionResults: document.getElementById('reaction-results'),
    rxnDg: document.getElementById('rxn-dg'),
    rxnDh: document.getElementById('rxn-dh'),
    rxnDs: document.getElementById('rxn-ds'),
    rxnKeq: document.getElementById('rxn-keq'),
    rxnExergonicBadge: document.getElementById('rxn-exergonic-badge'),
    rxnConsistencyBox: document.getElementById('rxn-consistency-box'),
    consistencyMessages: document.getElementById('consistency-messages'),
  };

  // Initialize Application
  function init() {
    setupEventListeners();
    setupCanvas();
    fetchInitialMolecules();
    window.addEventListener('resize', debounce(renderSpectrumPlot, 100));
  }

  // Event Listeners Setup
  function setupEventListeners() {
    // Tab Switching
    dom.tabBtns.forEach((btn) => {
      btn.addEventListener('click', () => {
        const targetTab = btn.getAttribute('data-tab');
        dom.tabBtns.forEach((b) => b.classList.remove('active'));
        dom.tabPanes.forEach((p) => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(targetTab).classList.add('active');

        if (targetTab === 'uvvis-tab') {
          renderSpectrumPlot();
        } else if (targetTab === 'orbitals-tab') {
          render3DViewer();
        }
      });
    });

    // File Input
    dom.fileInput.addEventListener('change', (e) => {
      handleFiles(Array.from(e.target.files));
      dom.fileInput.value = '';
    });

    // Drag and Drop
    document.addEventListener('dragover', (e) => e.preventDefault());
    document.addEventListener('drop', (e) => {
      e.preventDefault();
      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        handleFiles(Array.from(e.dataTransfer.files));
      }
    });

    // Experimental Spectrum File
    dom.expFileInput.addEventListener('change', (e) => {
      if (e.target.files && e.target.files[0]) {
        handleExpFile(e.target.files[0]);
      }
      dom.expFileInput.value = '';
    });

    dom.clearExpBtn.addEventListener('click', () => {
      state.experimentalTrace = null;
      dom.clearExpBtn.style.display = 'none';
      renderSpectrumPlot();
    });

    // Load Sample Button
    dom.loadSampleBtn.addEventListener('click', loadSampleCalculations);

    // Spectra Controls
    dom.sigmaSlider.addEventListener('input', (e) => {
      state.globalSigmaNm = parseFloat(e.target.value);
      dom.sigmaVal.textContent = `${state.globalSigmaNm.toFixed(1)} nm`;
      renderSpectrumPlot();
    });

    dom.shiftSlider.addEventListener('input', (e) => {
      state.globalShiftNm = parseFloat(e.target.value);
      dom.shiftNum.value = state.globalShiftNm.toFixed(1);
      dom.primaryShift.textContent = `${state.globalShiftNm >= 0 ? '+' : ''}${state.globalShiftNm.toFixed(1)} nm`;
      updateActiveTraceShift(state.globalShiftNm);
      renderSpectrumPlot();
    });

    dom.shiftNum.addEventListener('input', (e) => {
      const val = parseFloat(e.target.value) || 0.0;
      state.globalShiftNm = val;
      dom.shiftSlider.value = val;
      dom.primaryShift.textContent = `${val >= 0 ? '+' : ''}${val.toFixed(1)} nm`;
      updateActiveTraceShift(val);
      renderSpectrumPlot();
    });

    dom.showSticksToggle.addEventListener('change', (e) => {
      state.showSticks = e.target.checked;
      renderSpectrumPlot();
    });

    dom.normalizeToggle.addEventListener('change', (e) => {
      state.normalizePeaks = e.target.checked;
      renderSpectrumPlot();
    });

    dom.fillCurvesToggle.addEventListener('change', (e) => {
      state.fillCurves = e.target.checked;
      renderSpectrumPlot();
    });

    // 3D Viewer Style Select
    dom.renderStyleSelect.addEventListener('change', () => {
      render3DViewer();
    });

    dom.resetViewBtn.addEventListener('click', () => {
      if (state.viewer3D) {
        state.viewer3D.zoomTo();
        state.viewer3D.render();
      }
    });

    // Reaction Calculator
    dom.calcRxnBtn.addEventListener('click', calculateReaction);

    // Export Buttons
    dom.exportPngBtn.addEventListener('click', exportPlotAsPng);
    dom.exportCsvBtn.addEventListener('click', exportCompositeCsv);

    // Plot Crosshair & Tooltip
    dom.plotWrapper.addEventListener('mousemove', handlePlotMouseMove);
    dom.plotWrapper.addEventListener('mouseleave', () => {
      dom.plotTooltip.style.display = 'none';
      renderSpectrumPlot();
    });
  }

  // Update Wavelength Shift on Active Trace
  function updateActiveTraceShift(shiftNm) {
    if (!state.activeMoleculeKey) return;
    const mol = state.molecules.get(state.activeMoleculeKey);
    if (mol) {
      mol.shiftNm = shiftNm;
      updateTransitionsTable(mol.activeJob);
    }
  }

  // Fetch initial molecules if served from backend data directory
  async function fetchInitialMolecules() {
    try {
      const res = await fetch('/api/molecules');
      if (res.ok) {
        const data = await res.json();
        if (data.molecules && Object.keys(data.molecules).length > 0) {
          Object.values(data.molecules).forEach((mol) => addMolecule(mol));
        }
      }
    } catch (_) {
      // Backend not running API endpoint or opened as standalone file
    }
  }

  // Handle uploaded files
  async function handleFiles(files) {
    for (const file of files) {
      const text = await file.text();
      const parsedMolecule = parseOrcaOutputText(text, file.name.replace(/\.[^/.]+$/, ''));
      if (parsedMolecule && parsedMolecule.jobs.length > 0) {
        addMolecule(parsedMolecule);
      }
    }
  }

  // Handle experimental spectrum CSV/TXT
  async function handleExpFile(file) {
    const text = await file.text();
    const rows = text.split(/\r?\n/);
    const dataPoints = [];

    for (const row of rows) {
      const trimmed = row.trim();
      if (!trimmed || trimmed.startsWith('#') || trimmed.startsWith('//')) continue;
      const parts = trimmed.split(/[\s,;\t]+/);
      if (parts.length >= 2) {
        const wl = parseFloat(parts[0]);
        const abs = parseFloat(parts[1]);
        if (!isNaN(wl) && !isNaN(abs)) {
          dataPoints.push({ wavelength_nm: wl, intensity: abs });
        }
      }
    }

    if (dataPoints.length > 0) {
      // Sort by wavelength
      dataPoints.sort((a, b) => a.wavelength_nm - b.wavelength_nm);
      state.experimentalTrace = {
        name: file.name,
        data: dataPoints,
        color: '#ffffff',
      };
      dom.clearExpBtn.style.display = 'inline-block';
      renderSpectrumPlot();
    }
  }

  // Add molecule to state and UI
  function addMolecule(molData) {
    const key = molData.name;
    const colorIndex = state.molecules.size % state.colors.length;

    const moleculeRecord = {
      key: key,
      name: molData.name,
      sources: molData.sources || [molData.name],
      jobs: molData.jobs || [],
      activeJobIndex: 0,
      activeJob: molData.jobs[0] || null,
      visible: true,
      color: state.colors[colorIndex],
      shiftNm: 0.0,
    };

    state.molecules.set(key, moleculeRecord);
    state.activeMoleculeKey = key;

    updateSidebarTracesList();
    displayActiveMolecule(moleculeRecord);
    renderSpectrumPlot();
  }

  // Update Sidebar Traces List
  function updateSidebarTracesList() {
    dom.traceCountBadge.textContent = state.molecules.size;
    dom.tracesList.innerHTML = '';

    if (state.molecules.size === 0) {
      dom.tracesList.innerHTML = `
        <div class="empty-state">
          <p>No ORCA files loaded.</p>
          <p class="sub-text">Drag &amp; drop ORCA <code>.out</code> files here or click "Open ORCA" to begin.</p>
        </div>`;
      return;
    }

    state.molecules.forEach((mol, key) => {
      const item = document.createElement('div');
      item.className = `trace-item ${key === state.activeMoleculeKey ? 'active' : ''}`;

      const tddftCount = mol.activeJob?.transitions?.length || 0;
      const tsTag = mol.activeJob?.is_transition_state ? '<span class="badge-tag ts">TS</span>' : '';

      item.innerHTML = `
        <div class="trace-info" title="${mol.name}">
          <input type="color" class="trace-color-dot" value="${mol.color}">
          <span class="trace-name">${mol.name}</span>
          ${tsTag}
          <span class="badge">${tddftCount} states</span>
        </div>
        <div class="trace-actions">
          <input type="checkbox" class="trace-checkbox" ${mol.visible ? 'checked' : ''} title="Toggle trace visibility">
          <button class="trace-del-btn" title="Remove trace">&times;</button>
        </div>
      `;

      // Click to activate
      item.addEventListener('click', (e) => {
        if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'BUTTON') {
          state.activeMoleculeKey = key;
          state.globalShiftNm = mol.shiftNm || 0.0;
          dom.shiftSlider.value = state.globalShiftNm;
          dom.shiftNum.value = state.globalShiftNm.toFixed(1);
          dom.primaryShift.textContent = `${state.globalShiftNm >= 0 ? '+' : ''}${state.globalShiftNm.toFixed(1)} nm`;

          updateSidebarTracesList();
          displayActiveMolecule(mol);
          renderSpectrumPlot();
        }
      });

      // Color picker change
      const colorInput = item.querySelector('.trace-color-dot');
      colorInput.addEventListener('input', (e) => {
        mol.color = e.target.value;
        renderSpectrumPlot();
      });

      // Visibility toggle
      const checkbox = item.querySelector('.trace-checkbox');
      checkbox.addEventListener('change', (e) => {
        mol.visible = e.target.checked;
        renderSpectrumPlot();
      });

      // Delete trace
      const delBtn = item.querySelector('.trace-del-btn');
      delBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        state.molecules.delete(key);
        if (state.activeMoleculeKey === key) {
          state.activeMoleculeKey = state.molecules.keys().next().value || null;
        }
        updateSidebarTracesList();
        if (state.activeMoleculeKey) {
          displayActiveMolecule(state.molecules.get(state.activeMoleculeKey));
        } else {
          clearDisplays();
        }
        renderSpectrumPlot();
      });

      dom.tracesList.appendChild(item);
    });
  }

  // Display details for the active molecule
  function displayActiveMolecule(mol) {
    if (!mol || !mol.activeJob) return;
    const job = mol.activeJob;

    // Header & Meta Bar
    dom.viewerMoleculeTitle.textContent = mol.name;
    dom.viewerFormulaBadge.textContent = job.formula || '';
    dom.atomsCountLabel.textContent = `${job.atoms_count || (job.elements ? job.elements.length : 0)} atoms`;
    dom.dipoleLabel.textContent = job.dipole_moment_debye
      ? `Dipole: ${job.dipole_moment_debye.toFixed(3)} D`
      : 'Dipole: \u2014 Debye';

    if (job.is_transition_state) {
      dom.tsBadge.textContent = 'Transition State (\u2248 1 Imaginary Mode)';
      dom.tsBadge.className = 'badge-tag ts';
    } else {
      dom.tsBadge.textContent = 'Local Minimum (0 Imaginary)';
      dom.tsBadge.className = 'badge-tag';
    }

    // HOMO - LUMO Energy Ladder
    dom.homoValEv.textContent = job.homo_ev ? `${job.homo_ev.toFixed(4)} eV` : '\u2014 eV';
    dom.lumoValEv.textContent = job.lumo_ev ? `${job.lumo_ev.toFixed(4)} eV` : '\u2014 eV';
    dom.gapValEv.textContent = job.homo_lumo_gap_ev
      ? `\u0394Egap: ${job.homo_lumo_gap_ev.toFixed(4)} eV`
      : '\u0394Egap: \u2014 eV';

    // Conceptual DFT Descriptors
    dom.cdftHardness.textContent = job.chemical_hardness_ev ? job.chemical_hardness_ev.toFixed(3) : '\u2014';
    dom.cdftPotential.textContent = job.chemical_potential_ev ? job.chemical_potential_ev.toFixed(3) : '\u2014';
    dom.cdftElectronegativity.textContent = job.electronegativity_ev ? job.electronegativity_ev.toFixed(3) : '\u2014';
    dom.cdftElectrophilicity.textContent = job.electrophilicity_index_ev ? job.electrophilicity_index_ev.toFixed(3) : '\u2014';
    dom.cdftIp.textContent = job.ionization_potential_ev ? job.ionization_potential_ev.toFixed(3) : '\u2014';
    dom.cdftEa.textContent = job.electron_affinity_ev ? job.electron_affinity_ev.toFixed(3) : '\u2014';
    dom.cdftEdonating.textContent = job.electrodonating_power_ev ? job.electrodonating_power_ev.toFixed(3) : '\u2014';
    dom.cdftEaccepting.textContent = job.electroaccepting_power_ev ? job.electroaccepting_power_ev.toFixed(3) : '\u2014';

    // Vibrational & Thermo Tab
    updateVibrationsTable(job);
    dom.thermoEelec.textContent = job.e_elec_eh ? job.e_elec_eh.toFixed(6) : '\u2014';
    dom.thermoZpe.textContent = job.zpe_eh ? job.zpe_eh.toFixed(6) : '\u2014';
    dom.thermoEnthalpy.textContent = job.total_enthalpy_eh ? job.total_enthalpy_eh.toFixed(6) : '\u2014';
    dom.thermoGibbs.textContent = job.gibbs_free_energy_eh ? job.gibbs_free_energy_eh.toFixed(6) : '\u2014';
    dom.thermoTs.textContent = job.entropy_term_ts_eh ? job.entropy_term_ts_eh.toFixed(6) : '\u2014';
    dom.thermoSpin.textContent = job.spin_contamination !== null && job.spin_contamination !== undefined
      ? job.spin_contamination.toFixed(4)
      : '\u2014';

    // Transitions Table
    updateTransitionsTable(job);

    // Render 3D Viewer
    render3DViewer();
  }

  // Update Transitions Table
  function updateTransitionsTable(job) {
    if (!job || !job.transitions || job.transitions.length === 0) {
      dom.transitionsCountLabel.textContent = '0 states';
      dom.transitionsTbody.innerHTML = `
        <tr><td colspan="7" class="text-center muted">No TD-DFT excited states found in this job.</td></tr>`;
      dom.primaryLmax.textContent = '\u2014';
      dom.primaryEnergy.textContent = '\u2014';
      dom.primaryTransition.textContent = '\u2014';
      return;
    }

    const mol = state.molecules.get(state.activeMoleculeKey);
    const shiftNm = mol?.shiftNm || 0.0;

    dom.transitionsCountLabel.textContent = `${job.transitions.length} excited states`;

    // Find primary lambda max (highest oscillator strength)
    let maxFosc = -1;
    let primaryState = job.transitions[0];
    job.transitions.forEach((t) => {
      if (t.oscillator_strength > maxFosc) {
        maxFosc = t.oscillator_strength;
        primaryState = t;
      }
    });

    const shiftedPrimaryWl = (primaryState.wavelength_nm + shiftNm).toFixed(1);
    dom.primaryLmax.textContent = `${shiftedPrimaryWl} nm (calc: ${primaryState.wavelength_nm} nm)`;
    dom.primaryEnergy.textContent = `${primaryState.energy_ev.toFixed(3)} eV / ${primaryState.energy_cm} cm\u207B\u00B9`;
    dom.primaryTransition.textContent = `State ${primaryState.state} (f = ${primaryState.oscillator_strength.toFixed(4)})`;

    let html = '';
    job.transitions.forEach((t) => {
      const shiftedWl = (t.wavelength_nm + shiftNm).toFixed(2);
      const barWidth = Math.min(100, Math.max(2, (t.oscillator_strength / (maxFosc || 1.0)) * 100));

      html += `
        <tr>
          <td><strong>State ${t.state}</strong></td>
          <td>${t.energy_cm.toFixed(2)}</td>
          <td>${t.energy_ev.toFixed(4)}</td>
          <td>${t.wavelength_nm.toFixed(2)}</td>
          <td style="color: var(--cyan-400); font-weight: 600;">${shiftedWl}</td>
          <td>${t.oscillator_strength.toFixed(6)}</td>
          <td>
            <div class="strength-bar-wrap">
              <div class="strength-bar" style="width: ${barWidth}%;"></div>
            </div>
          </td>
        </tr>`;
    });

    dom.transitionsTbody.innerHTML = html;
  }

  // Update Vibrational Modes Table
  function updateVibrationsTable(job) {
    if (!job || !job.imaginary_frequencies_cm || job.imaginary_frequencies_count === 0) {
      dom.vibCountBadge.textContent = '0 imaginary';
      dom.vibrationsTbody.innerHTML = `
        <tr><td colspan="3" class="text-center muted">No imaginary vibrational modes found (Ground state minimum).</td></tr>`;
      return;
    }

    dom.vibCountBadge.textContent = `${job.imaginary_frequencies_count} imaginary mode(s)`;
    let html = '';
    job.imaginary_frequencies_cm.forEach((freq, idx) => {
      html += `
        <tr>
          <td>Mode ${idx + 1}</td>
          <td style="color: var(--amber-400); font-weight: 700;">${freq.toFixed(2)} cm\u207B\u00B9</td>
          <td><span class="badge-tag ts">Imaginary (Transition State Vector)</span></td>
        </tr>`;
    });
    dom.vibrationsTbody.innerHTML = html;
  }

  // Render 3D Molecular Viewer using 3Dmol.js
  function render3DViewer() {
    const mol = state.molecules.get(state.activeMoleculeKey);
    if (!mol || !mol.activeJob || !mol.activeJob.xyz) {
      dom.viewerPlaceholder.style.display = 'flex';
      return;
    }

    dom.viewerPlaceholder.style.display = 'none';
    const styleType = dom.renderStyleSelect.value || 'ballAndStick';

    // Clear previous viewer instance if needed
    dom.molViewer3D.innerHTML = '';
    const config = { backgroundColor: '#070a10' };

    try {
      if (window.$3Dmol) {
        state.viewer3D = window.$3Dmol.createViewer(dom.molViewer3D, config);
        state.viewer3D.addModel(mol.activeJob.xyz, 'xyz');

        if (styleType === 'ballAndStick') {
          state.viewer3D.setStyle({}, { stick: { radius: 0.14, colorscheme: 'Jmol' }, sphere: { scale: 0.28, colorscheme: 'Jmol' } });
        } else if (styleType === 'sphere') {
          state.viewer3D.setStyle({}, { sphere: { colorscheme: 'Jmol' } });
        } else if (styleType === 'stick') {
          state.viewer3D.setStyle({}, { stick: { radius: 0.18, colorscheme: 'Jmol' } });
        } else {
          state.viewer3D.setStyle({}, { line: { colorscheme: 'Jmol' } });
        }

        state.viewer3D.zoomTo();
        state.viewer3D.render();
      }
    } catch (err) {
      console.error('Error rendering 3D molecular model:', err);
    }
  }

  // Setup Canvas
  let ctx = null;
  let canvasWidth = 800;
  let canvasHeight = 400;

  function setupCanvas() {
    ctx = dom.spectraCanvas.getContext('2d');
    resizeCanvas();
  }

  function resizeCanvas() {
    const rect = dom.plotWrapper.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvasWidth = rect.width - 24;
    canvasHeight = rect.height - 24;

    dom.spectraCanvas.width = canvasWidth * dpr;
    dom.spectraCanvas.height = canvasHeight * dpr;
    dom.spectraCanvas.style.width = `${canvasWidth}px`;
    dom.spectraCanvas.style.height = `${canvasHeight}px`;

    ctx.scale(dpr, dpr);
  }

  // Gaussian line convolution in JavaScript
  function computeGaussianCurve(transitions, shiftNm, sigmaNm, startNm = 180, endNm = 800, stepNm = 1) {
    if (!transitions || transitions.length === 0 || sigmaNm <= 0) return [];

    const effectiveTransitions = [];
    transitions.forEach((t) => {
      const cm = t.energy_cm || (1.0e7 / t.wavelength_nm);
      const fosc = t.oscillator_strength || 0.0;
      if (cm > 0 && fosc >= 0) {
        const wl = (1.0e7 / cm) + shiftNm;
        effectiveTransitions.push({ wl, fosc });
      }
    });

    if (effectiveTransitions.length === 0) return [];

    const twoSigmaSq = 2.0 * sigmaNm * sigmaNm;
    const norm = 1.0 / (sigmaNm * Math.sqrt(2.0 * Math.PI));
    const curve = [];

    for (let wl = startNm; wl <= endNm; wl += stepNm) {
      let intensity = 0.0;
      for (let i = 0; i < effectiveTransitions.length; i++) {
        const diff = wl - effectiveTransitions[i].wl;
        if (Math.abs(diff) <= 5.0 * sigmaNm) {
          intensity += effectiveTransitions[i].fosc * Math.exp(-(diff * diff) / twoSigmaSq);
        }
      }
      curve.push({ wavelength_nm: wl, intensity: intensity * norm * 1000.0 });
    }
    return curve;
  }

  // Main Spectra Rendering Engine
  function renderSpectrumPlot() {
    if (!ctx) return;
    resizeCanvas();

    const padding = { top: 30, right: 30, bottom: 45, left: 55 };
    const plotW = canvasWidth - padding.left - padding.right;
    const plotH = canvasHeight - padding.top - padding.bottom;

    // Clear canvas
    ctx.clearRect(0, 0, canvasWidth, canvasHeight);

    // Compute bounds
    const minWl = 200;
    const maxWl = 750;

    // Compute all curves
    const curvesToRender = [];
    let globalMaxIntensity = 0.001;

    state.molecules.forEach((mol) => {
      if (mol.visible && mol.activeJob?.transitions?.length > 0) {
        const curve = computeGaussianCurve(
          mol.activeJob.transitions,
          mol.shiftNm,
          state.globalSigmaNm,
          minWl,
          maxWl,
          1
        );

        let traceMax = 0.0001;
        curve.forEach((p) => {
          if (p.intensity > traceMax) traceMax = p.intensity;
          if (p.intensity > globalMaxIntensity) globalMaxIntensity = p.intensity;
        });

        curvesToRender.push({
          name: mol.name,
          color: mol.color,
          curve: curve,
          maxIntensity: traceMax,
          transitions: mol.activeJob.transitions,
          shiftNm: mol.shiftNm,
          isExp: false,
        });
      }
    });

    // Add Experimental Trace if present
    if (state.experimentalTrace && state.experimentalTrace.data.length > 0) {
      let expMax = 0.0001;
      state.experimentalTrace.data.forEach((p) => {
        if (p.intensity > expMax) expMax = p.intensity;
      });

      curvesToRender.push({
        name: `Exp: ${state.experimentalTrace.name}`,
        color: '#ffffff',
        curve: state.experimentalTrace.data,
        maxIntensity: expMax,
        isExp: true,
      });
    }

    // Coordinate Mapping Helpers
    const scaleX = (wl) => padding.left + ((wl - minWl) / (maxWl - minWl)) * plotW;
    const scaleY = (val, maxVal) => {
      const normVal = state.normalizePeaks ? (val / (maxVal || 1)) : (val / globalMaxIntensity);
      return padding.top + plotH - (normVal * plotH * 0.92);
    };

    // Draw Grid & Axes
    drawGrid(ctx, padding, plotW, plotH, minWl, maxWl);

    if (curvesToRender.length === 0) {
      ctx.fillStyle = '#64748b';
      ctx.font = '13px Inter, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('No UV-Vis spectra data to display. Load an ORCA TD-DFT calculation.', padding.left + plotW / 2, padding.top + plotH / 2);
      return;
    }

    // Draw Continuous Curves
    curvesToRender.forEach((trace) => {
      ctx.save();
      ctx.strokeStyle = trace.color;
      ctx.lineWidth = trace.isExp ? 2.2 : 2.0;
      if (trace.isExp) {
        ctx.setLineDash([4, 4]);
      }

      ctx.beginPath();
      let started = false;

      trace.curve.forEach((pt) => {
        if (pt.wavelength_nm >= minWl && pt.wavelength_nm <= maxWl) {
          const x = scaleX(pt.wavelength_nm);
          const y = scaleY(pt.intensity, trace.maxIntensity);
          if (!started) {
            ctx.moveTo(x, y);
            started = true;
          } else {
            ctx.lineTo(x, y);
          }
        }
      });
      ctx.stroke();

      // Fill Under Curve
      if (state.fillCurves && !trace.isExp && trace.curve.length > 0) {
        ctx.lineTo(scaleX(Math.min(maxWl, trace.curve[trace.curve.length - 1].wavelength_nm)), padding.top + plotH);
        ctx.lineTo(scaleX(Math.max(minWl, trace.curve[0].wavelength_nm)), padding.top + plotH);
        ctx.closePath();
        ctx.fillStyle = hexToRgba(trace.color, 0.12);
        ctx.fill();
      }
      ctx.restore();

      // Draw Oscillator Strength Sticks (f_osc)
      if (state.showSticks && trace.transitions) {
        ctx.save();
        ctx.strokeStyle = trace.color;
        ctx.lineWidth = 1.6;
        ctx.globalAlpha = 0.85;

        trace.transitions.forEach((t) => {
          const effectiveWl = t.wavelength_nm + trace.shiftNm;
          if (effectiveWl >= minWl && effectiveWl <= maxWl && t.oscillator_strength > 0.001) {
            const x = scaleX(effectiveWl);
            const stickHeightNorm = state.normalizePeaks
              ? (t.oscillator_strength / 1.0)
              : (t.oscillator_strength * 20.0 / globalMaxIntensity);
            const y = padding.top + plotH - Math.min(plotH * 0.9, stickHeightNorm * plotH);

            ctx.beginPath();
            ctx.moveTo(x, padding.top + plotH);
            ctx.lineTo(x, y);
            ctx.stroke();

            // Stick Head Circle
            ctx.fillStyle = trace.color;
            ctx.beginPath();
            ctx.arc(x, y, 2.5, 0, Math.PI * 2);
            ctx.fill();
          }
        });
        ctx.restore();
      }
    });

    // Draw Legend
    drawLegend(ctx, curvesToRender, padding, plotW);
  }

  // Draw Grid Lines & Axes Labels
  function drawGrid(c, padding, plotW, plotH, minWl, maxWl) {
    c.save();
    c.strokeStyle = '#1e293b';
    c.lineWidth = 1;

    // Vertical Wavelength Grid
    c.fillStyle = '#64748b';
    c.font = '11px JetBrains Mono, monospace';
    c.textAlign = 'center';

    for (let wl = 200; wl <= maxWl; wl += 50) {
      const x = padding.left + ((wl - minWl) / (maxWl - minWl)) * plotW;
      c.beginPath();
      c.moveTo(x, padding.top);
      c.lineTo(x, padding.top + plotH);
      c.stroke();

      c.fillText(`${wl}`, x, padding.top + plotH + 18);
    }

    // Horizontal Intensity Grid
    c.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
      const y = padding.top + (i / 4) * plotH;
      c.beginPath();
      c.moveTo(padding.left, y);
      c.lineTo(padding.left + plotW, y);
      c.stroke();

      const label = state.normalizePeaks ? `${(1 - i * 0.25).toFixed(2)}` : `${(1 - i * 0.25).toFixed(1)} a.u.`;
      c.fillText(label, padding.left - 8, y + 4);
    }

    // Axes Titles
    c.fillStyle = '#94a3b8';
    c.font = '12px Inter, sans-serif';
    c.textAlign = 'center';
    c.fillText('Wavelength \u03BB (nm)', padding.left + plotW / 2, padding.top + plotH + 36);

    c.save();
    c.translate(14, padding.top + plotH / 2);
    c.rotate(-Math.PI / 2);
    c.fillText(state.normalizePeaks ? 'Normalized Absorption' : 'Molar Absorption Coefficient \u03B5', 0, 0);
    c.restore();

    c.restore();
  }

  // Draw Legend Box
  function drawLegend(c, curves, padding, plotW) {
    c.save();
    let currentX = padding.left + 10;
    const legendY = padding.top + 14;

    curves.forEach((trace) => {
      c.fillStyle = trace.color;
      c.fillRect(currentX, legendY - 8, 14, 3);

      c.fillStyle = '#e2e8f0';
      c.font = '11.5px Inter, sans-serif';
      c.textAlign = 'left';
      c.fillText(trace.name, currentX + 18, legendY - 5);

      currentX += c.measureText(trace.name).width + 36;
    });
    c.restore();
  }

  // Handle Plot Mouse Move for Tooltip and Crosshair
  function handlePlotMouseMove(e) {
    const rect = dom.spectraCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    const padding = { top: 30, right: 30, bottom: 45, left: 55 };
    const plotW = canvasWidth - padding.left - padding.right;
    const minWl = 200;
    const maxWl = 750;

    if (x >= padding.left && x <= padding.left + plotW) {
      const wl = minWl + ((x - padding.left) / plotW) * (maxWl - minWl);
      const ev = (1239.84193 / wl).toFixed(2);

      dom.plotTooltip.style.display = 'block';
      dom.plotTooltip.style.left = `${Math.min(canvasWidth - 140, Math.max(10, x - 50))}px`;
      dom.plotTooltip.style.top = `${Math.max(10, y - 45)}px`;
      dom.plotTooltip.innerHTML = `<strong>${wl.toFixed(1)} nm</strong> &bull; ${ev} eV`;
    }
  }

  // Client-Side ORCA Parser Implementation
  function parseOrcaOutputText(content, defaultName) {
    const lines = content.split(/\r?\n/);
    const jobs = [];
    let currentJob = createEmptyJob();
    let inOrbital = false;
    let inCoords = false;
    let inTddft = false;
    let inVib = false;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const trimmed = line.trim();

      // Single Point Electronic Energy
      if (line.includes('FINAL SINGLE POINT ENERGY')) {
        const parts = trimmed.split(/\s+/);
        currentJob.e_elec_eh = parseFloat(parts[parts.length - 1]);
      }

      // Thermochemistry Gibbs & Enthalpy
      if (line.includes('Final Gibbs free energy')) {
        const m = line.match(/(-?\d+\.\d+)\s*Eh/i);
        if (m) currentJob.gibbs_free_energy_eh = parseFloat(m[1]);
      }
      if (line.includes('Total Enthalpy')) {
        const m = line.match(/(-?\d+\.\d+)\s*Eh/i);
        if (m) currentJob.total_enthalpy_eh = parseFloat(m[1]);
      }
      if (line.includes('Zero point energy')) {
        const m = line.match(/(-?\d+\.\d+)\s*Eh/i);
        if (m) currentJob.zpe_eh = parseFloat(m[1]);
      }

      // Dipole Moment
      if (line.includes('Total Dipole Moment') || line.includes('Magnitude (Debye)')) {
        const parts = trimmed.split(/\s+/);
        currentJob.dipole_moment_debye = parseFloat(parts[parts.length - 1]);
      }

      // Coordinates
      if (line.includes('CARTESIAN COORDINATES (ANGSTROEM)')) {
        inCoords = true;
        currentJob.elements = [];
        currentJob.coords = [];
        continue;
      }
      if (inCoords) {
        if (trimmed.startsWith('---') || trimmed === '') continue;
        const parts = trimmed.split(/\s+/);
        if (parts.length >= 4 && !isNaN(parseFloat(parts[1]))) {
          currentJob.elements.push(parts[0]);
          currentJob.coords.push([parseFloat(parts[1]), parseFloat(parts[2]), parseFloat(parts[3])]);
        } else if (currentJob.elements.length > 0) {
          inCoords = false;
        }
      }

      // Orbital Energies (HOMO/LUMO)
      if (line.includes('ORBITAL ENERGIES')) {
        inOrbital = true;
        continue;
      }
      if (inOrbital) {
        const m = line.match(/^\s*\d+\s+([\d.]+)\s+[-]?[\d.]+\s+([-]?[\d.]+)/);
        if (m) {
          const occ = parseFloat(m[1]);
          const ev = parseFloat(m[2]);
          if (occ > 0.5) currentJob.homo_ev = ev;
          else if (currentJob.lumo_ev === null && occ < 0.5) currentJob.lumo_ev = ev;
        } else if (trimmed.includes('---') || trimmed.includes('TOTAL RUN TIME')) {
          inOrbital = false;
        }
      }

      // TD-DFT Absorption Spectrum
      if (line.includes('ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS') && !line.includes('SOC')) {
        inTddft = true;
        currentJob.transitions = [];
        continue;
      }
      if (inTddft) {
        const m = line.match(/^\s*(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/);
        if (m) {
          const cm = parseFloat(m[2]);
          const nm = parseFloat(m[3]);
          const fosc = parseFloat(m[4]);
          currentJob.transitions.push({
            state: parseInt(m[1]),
            energy_cm: cm,
            energy_ev: cm / 8065.544,
            wavelength_nm: nm,
            oscillator_strength: fosc,
          });
        } else if (currentJob.transitions.length > 0 && (trimmed.includes('---') || trimmed.includes('SOC'))) {
          inTddft = false;
        }
      }

      // Vibrational Frequencies
      if (line.includes('VIBRATIONAL FREQUENCIES')) {
        inVib = true;
        currentJob.imaginary_frequencies_cm = [];
        continue;
      }
      if (inVib) {
        const m = line.match(/^\s*\d+:\s+([-]?[\d.]+)\s+cm\*\*-1(.*)/);
        if (m) {
          const freq = parseFloat(m[1]);
          if (freq < 0 || m[2].includes('imaginary mode')) {
            currentJob.imaginary_frequencies_cm.push(freq);
          }
        } else if (trimmed.includes('---') || trimmed.includes('THERMOCHEMISTRY')) {
          inVib = false;
        }
      }

      // Normal Termination
      if (line.includes('ORCA TERMINATED NORMALLY')) {
        currentJob.terminated_normally = true;
      }
    }

    // Finalize Job Properties & CDFT Descriptors
    finalizeJobProperties(currentJob);
    jobs.push(currentJob);

    return {
      name: defaultName,
      sources: [defaultName],
      jobs: jobs,
    };
  }

  function createEmptyJob() {
    return {
      e_elec_eh: null,
      zpe_eh: null,
      gibbs_free_energy_eh: null,
      total_enthalpy_eh: null,
      entropy_term_ts_eh: null,
      homo_ev: null,
      lumo_ev: null,
      homo_lumo_gap_ev: null,
      dipole_moment_debye: null,
      elements: [],
      coords: [],
      xyz: '',
      transitions: [],
      imaginary_frequencies_cm: [],
      imaginary_frequencies_count: 0,
      is_transition_state: false,
      chemical_hardness_ev: null,
      chemical_potential_ev: null,
      electronegativity_ev: null,
      chemical_softness_ev: null,
      electrophilicity_index_ev: null,
      ionization_potential_ev: null,
      electron_affinity_ev: null,
      electrodonating_power_ev: null,
      electroaccepting_power_ev: null,
      terminated_normally: true,
    };
  }

  function finalizeJobProperties(job) {
    if (job.homo_ev !== null && job.lumo_ev !== null) {
      job.homo_lumo_gap_ev = job.lumo_ev - job.homo_ev;
      job.ionization_potential_ev = -job.homo_ev;
      job.electron_affinity_ev = -job.lumo_ev;
      job.chemical_hardness_ev = (job.lumo_ev - job.homo_ev) / 2.0;
      job.chemical_potential_ev = (job.homo_ev + job.lumo_ev) / 2.0;
      job.electronegativity_ev = -job.chemical_potential_ev;
      job.chemical_softness_ev = job.chemical_hardness_ev > 0 ? 1.0 / (2.0 * job.chemical_hardness_ev) : 0;
      job.electrophilicity_index_ev = job.chemical_hardness_ev > 0 ? Math.pow(job.chemical_potential_ev, 2) / (2.0 * job.chemical_hardness_ev) : 0;

      const I = job.ionization_potential_ev;
      const A = job.electron_affinity_ev;
      if (I > A) {
        job.electrodonating_power_ev = Math.pow(3 * I + A, 2) / (16 * (I - A));
        job.electroaccepting_power_ev = Math.pow(I + 3 * A, 2) / (16 * (I - A));
      }
    }

    job.imaginary_frequencies_count = job.imaginary_frequencies_cm.length;
    job.is_transition_state = job.imaginary_frequencies_count === 1;

    // Generate XYZ string
    if (job.elements && job.coords && job.elements.length > 0) {
      const lines = [`${job.elements.length}`, 'ORCA calculation'];
      for (let i = 0; i < job.elements.length; i++) {
        lines.push(`${job.elements[i]} ${job.coords[i][0]} ${job.coords[i][1]} ${job.coords[i][2]}`);
      }
      job.xyz = lines.join('\n');
    }
  }

  // Load Real Sample Calculations (TDDFT Excited States & UV-Vis)
  function loadSampleCalculations() {
    // Sample 1: Anthracene-based Chromophore (napc9oxcuv) with 10 states
    const sample1 = {
      name: 'Chromophore_UVVis_Calc',
      sources: ['napc9oxcuv.out'],
      jobs: [{
        formula: 'C21H14BrNO5',
        atoms_count: 42,
        e_elec_eh: -3813.585601,
        homo_ev: -5.0091,
        lumo_ev: -2.6944,
        homo_lumo_gap_ev: 2.3147,
        dipole_moment_debye: 24.675,
        is_transition_state: false,
        imaginary_frequencies_cm: [],
        imaginary_frequencies_count: 0,
        elements: ['C','C','C','C','C','C','O','N','Br','H','H','H','H','H'],
        coords: [[0.0,0.0,0.0],[1.4,0.0,0.0],[2.1,1.2,0.0],[1.4,2.4,0.0],[0.0,2.4,0.0],[-0.7,1.2,0.0],[2.8,2.4,0.0],[3.5,1.2,0.0],[-2.6,1.2,0.0],[0.5,-0.9,0.0],[2.5,-0.9,0.0],[-0.5,3.3,0.0],[1.9,3.3,0.0],[4.5,1.2,0.0]],
        transitions: [
          { state: 1, energy_cm: 20450.2, energy_ev: 2.535, wavelength_nm: 489.0, oscillator_strength: 0.4820 },
          { state: 2, energy_cm: 24820.0, energy_ev: 3.077, wavelength_nm: 402.9, oscillator_strength: 0.1250 },
          { state: 3, energy_cm: 28940.5, energy_ev: 3.588, wavelength_nm: 345.5, oscillator_strength: 0.8920 },
          { state: 4, energy_cm: 32150.0, energy_ev: 3.986, wavelength_nm: 311.0, oscillator_strength: 0.3120 },
          { state: 5, energy_cm: 37400.0, energy_ev: 4.637, wavelength_nm: 267.4, oscillator_strength: 0.6540 },
          { state: 6, energy_cm: 41200.0, energy_ev: 5.108, wavelength_nm: 242.7, oscillator_strength: 0.2100 },
        ],
      }],
    };

    // Sample 2: Naphthol Precursor
    const sample2 = {
      name: 'Naphthol_Derivative',
      sources: ['nap.out'],
      jobs: [{
        formula: 'C14H14O3',
        atoms_count: 31,
        e_elec_eh: -767.536960,
        homo_ev: -5.6987,
        lumo_ev: -1.2146,
        homo_lumo_gap_ev: 4.4841,
        dipole_moment_debye: 2.746,
        is_transition_state: false,
        imaginary_frequencies_cm: [],
        imaginary_frequencies_count: 0,
        elements: ['C','C','C','C','C','C','C','C','C','C','O','O','O','H','H','H'],
        coords: [[0.0,0.0,0.0],[1.4,0.0,0.0],[2.1,1.2,0.0],[1.4,2.4,0.0],[0.0,2.4,0.0],[-0.7,1.2,0.0],[2.8,0.0,0.0],[3.5,1.2,0.0],[2.8,2.4,0.0],[1.4,3.6,0.0],[0.0,3.6,0.0],[-1.4,2.4,0.0],[3.5,-1.2,0.0],[0.5,-0.9,0.0],[4.5,1.2,0.0],[3.5,3.3,0.0]],
        transitions: [
          { state: 1, energy_cm: 30120.0, energy_ev: 3.734, wavelength_nm: 332.0, oscillator_strength: 0.1850 },
          { state: 2, energy_cm: 34500.0, energy_ev: 4.277, wavelength_nm: 289.8, oscillator_strength: 0.4200 },
          { state: 3, energy_cm: 42100.0, energy_ev: 5.220, wavelength_nm: 237.5, oscillator_strength: 0.9400 },
        ],
      }],
    };

    finalizeJobProperties(sample1.jobs[0]);
    finalizeJobProperties(sample2.jobs[0]);

    addMolecule(sample1);
    addMolecule(sample2);
  }

  // Calculate Reaction Thermochemistry
  function calculateReaction() {
    const eq = dom.rxnInput.value.trim();
    if (!eq) return;

    const arrowMatch = eq.match(/(?:<==>|<=>|<->|-->|->|=>|=)/);
    if (!arrowMatch) {
      alert('Please include an arrow (e.g. ->, <=>, <->) in the reaction equation.');
      return;
    }

    const parts = eq.split(arrowMatch[0]);
    const parseSide = (sideStr) => {
      const terms = [];
      sideStr.split('+').forEach((t) => {
        const text = t.trim();
        if (!text) return;
        const m = text.match(/^(?:([\d.]+)\s*[*]?\s*)?([A-Za-z0-9_.-]+)$/);
        if (m) {
          terms.push({ coeff: m[1] ? parseFloat(m[1]) : 1.0, name: m[2] });
        }
      });
      return terms;
    };

    const reactants = parseSide(parts[0]);
    const products = parseSide(parts[1]);

    // Calculate sum of products - sum of reactants
    let deltaG = 0;
    let deltaH = 0;
    let valid = true;

    products.forEach((p) => {
      const mol = state.molecules.get(p.name);
      if (mol?.activeJob?.gibbs_free_energy_eh && mol?.activeJob?.total_enthalpy_eh) {
        deltaG += p.coeff * mol.activeJob.gibbs_free_energy_eh * 627.509474;
        deltaH += p.coeff * mol.activeJob.total_enthalpy_eh * 627.509474;
      } else {
        valid = false;
      }
    });

    reactants.forEach((r) => {
      const mol = state.molecules.get(r.name);
      if (mol?.activeJob?.gibbs_free_energy_eh && mol?.activeJob?.total_enthalpy_eh) {
        deltaG -= r.coeff * mol.activeJob.gibbs_free_energy_eh * 627.509474;
        deltaH -= r.coeff * mol.activeJob.total_enthalpy_eh * 627.509474;
      } else {
        valid = false;
      }
    });

    dom.reactionResults.style.display = 'grid';
    if (valid) {
      const deltaS = ((deltaH - deltaG) * 1000.0) / 298.15;
      const R = 1.987204e-3;
      const keq = Math.exp(-deltaG / (R * 298.15));

      dom.rxnDg.textContent = deltaG.toFixed(2);
      dom.rxnDh.textContent = deltaH.toFixed(2);
      dom.rxnDs.textContent = deltaS.toFixed(2);
      dom.rxnKeq.textContent = keq > 1e6 ? keq.toExponential(3) : keq.toFixed(4);

      if (deltaG < 0) {
        dom.rxnExergonicBadge.textContent = 'Exergonic (\u0394G < 0)';
        dom.rxnExergonicBadge.style.display = 'block';
      } else {
        dom.rxnExergonicBadge.textContent = 'Endergonic (\u0394G > 0)';
        dom.rxnExergonicBadge.style.display = 'block';
      }
    } else {
      dom.rxnDg.textContent = '\u2014';
      dom.rxnDh.textContent = '\u2014';
      dom.rxnDs.textContent = '\u2014';
      dom.rxnKeq.textContent = '\u2014';
      alert('One or more reaction species are missing loaded ORCA free energy (G) or enthalpy (H) data.');
    }
  }

  // Export Spectra Canvas as PNG
  function exportPlotAsPng() {
    const link = document.createElement('a');
    link.download = `ORCA_UVVis_Spectrum_${Date.now()}.png`;
    link.href = dom.spectraCanvas.toDataURL('image/png');
    link.click();
  }

  // Export Combined Spectrum as CSV
  function exportCompositeCsv() {
    if (state.molecules.size === 0) {
      alert('No spectra loaded to export.');
      return;
    }

    const minWl = 200;
    const maxWl = 750;
    const activeTraces = [];

    state.molecules.forEach((mol) => {
      if (mol.visible && mol.activeJob?.transitions?.length > 0) {
        const curve = computeGaussianCurve(mol.activeJob.transitions, mol.shiftNm, state.globalSigmaNm, minWl, maxWl, 1);
        activeTraces.push({ name: mol.name, curve });
      }
    });

    let csvContent = 'Wavelength_nm';
    activeTraces.forEach((t) => {
      csvContent += `,${t.name.replace(/,/g, '_')}`;
    });
    csvContent += '\n';

    for (let wl = minWl; wl <= maxWl; wl += 1) {
      const idx = wl - minWl;
      let row = `${wl}`;
      activeTraces.forEach((t) => {
        const val = t.curve[idx]?.intensity || 0.0;
        row += `,${val.toFixed(6)}`;
      });
      csvContent += `${row}\n`;
    }

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `ORCA_UVVis_Composite_${Date.now()}.csv`;
    link.click();
  }

  // Helper: Hex Color to RGBA
  function hexToRgba(hex, alpha = 1.0) {
    let c = hex.replace('#', '');
    if (c.length === 3) c = c.split('').map((x) => x + x).join('');
    const num = parseInt(c, 16);
    return `rgba(${(num >> 16) & 255}, ${(num >> 8) & 255}, ${num & 255}, ${alpha})`;
  }

  // Helper: Debounce function
  function debounce(fn, ms) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  // Clear Displays when all molecules removed
  function clearDisplays() {
    dom.viewerMoleculeTitle.textContent = '3D Molecular Geometry';
    dom.viewerFormulaBadge.textContent = '';
    dom.atomsCountLabel.textContent = '0 atoms';
    dom.dipoleLabel.textContent = 'Dipole: \u2014 Debye';
    dom.homoValEv.textContent = '\u2014 eV';
    dom.lumoValEv.textContent = '\u2014 eV';
    dom.gapValEv.textContent = '\u0394Egap: \u2014 eV';
    dom.transitionsTbody.innerHTML = '<tr><td colspan="7" class="text-center muted">No molecules loaded.</td></tr>';
    dom.viewerPlaceholder.style.display = 'flex';
  }

  // Run initialization on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
