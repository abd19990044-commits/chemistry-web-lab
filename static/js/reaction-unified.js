// static/js/reaction-unified.js
// Unified Reaction Quantum Setup, Execution Queue, and Downloadable Diagrams
(function() {
  'use strict';

  let activeReactionId = null;
  let activeReactionData = null;
  let executionPollInterval = null;

  function initUnifiedReactionController() {
    const calcThermoBtn = document.getElementById('reaction-calc-thermo-btn');
    const modal = document.getElementById('unified-reaction-setup-modal');
    const closeBtn = document.getElementById('btn-close-unified-setup');
    const cancelBtn = document.getElementById('btn-cancel-unified-setup');
    const setupForm = document.getElementById('unified-setup-form');
    const eqDisplay = document.getElementById('unified-setup-equation');
    const speciesTags = document.getElementById('unified-setup-species-tags');

    // Wire "Calculate Thermochemistry" on 2D Reaction drawing
    if (calcThermoBtn) {
      calcThermoBtn.addEventListener('click', (e) => {
        e.preventDefault();
        const reactants = (document.getElementById('reaction-reactants')?.value || '').trim();
        const products = (document.getElementById('reaction-products')?.value || '').trim();
        if (!reactants || !products) {
          alert('Please enter and draw both reactants and products first.');
          return;
        }
        const equation = `${reactants} -> ${products}`;

        // Launch the Main Input Generator Wizard in Reaction Mode
        if (typeof window.openOrcaWizardForReaction === 'function') {
          window.openOrcaWizardForReaction({ equation, reactants, products });
        } else if (modal) {
          if (eqDisplay) eqDisplay.textContent = equation;
          if (speciesTags) {
            speciesTags.innerHTML = '';
            const terms = (reactants + ' + ' + products).split('+').map(s => s.trim()).filter(Boolean);
            terms.forEach(term => {
              const tag = document.createElement('span');
              tag.className = 'badge badge-info';
              tag.textContent = `🟢 3D Ready: ${term}`;
              speciesTags.appendChild(tag);
            });
          }
          modal.classList.remove('hidden');
        }
      });
    }

    // Modal Close
    if (closeBtn) closeBtn.addEventListener('click', () => modal?.classList.add('hidden'));
    if (cancelBtn) cancelBtn.addEventListener('click', () => modal?.classList.add('hidden'));

    // Handle Form Submit -> Setup & Start
    if (setupForm) {
      setupForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const submitBtn = document.getElementById('btn-submit-unified-setup');
        if (submitBtn) {
          submitBtn.disabled = true;
          submitBtn.textContent = 'Generating 3D Inputs...';
        }

        const reactants = (document.getElementById('reaction-reactants')?.value || '').trim();
        const products = (document.getElementById('reaction-products')?.value || '').trim();
        const equation = `${reactants} -> ${products}`;

        const stagePreset = document.getElementById('unified-setup-stages')?.value || 'opt_freq';
        let stageDefs = [
          { kind: 'OPT', label: 'Geometry Optimization', order: 0 },
          { kind: 'FREQ', label: 'Frequency & Thermochemistry', order: 1 }
        ];
        if (stagePreset === 'opt') {
          stageDefs = [{ kind: 'OPT', label: 'Geometry Optimization', order: 0 }];
        } else if (stagePreset === 'freq') {
          stageDefs = [{ kind: 'FREQ', label: 'Frequency & Thermochemistry', order: 0 }];
        } else if (stagePreset === 'sp') {
          stageDefs = [{ kind: 'SP', label: 'Single Point Energy', order: 0 }];
        } else if (stagePreset === 'opt_freq_sp') {
          stageDefs = [
            { kind: 'OPT', label: 'Geometry Optimization', order: 0 },
            { kind: 'FREQ', label: 'Frequency & Thermochemistry', order: 1 },
            { kind: 'SP', label: 'High-Level Single Point', order: 2 }
          ];
        }

        const solventVal = document.getElementById('unified-setup-solvent')?.value || 'none';
        const targetHost = document.getElementById('unified-setup-target-device')?.value || 'server_local';
        const concurrencyLimit = parseInt(document.getElementById('unified-setup-concurrency')?.value || '1', 10);

        // Check if user selected connected computer / local agent
        let agentSessionId = null;
        if (targetHost === 'local_agent') {
          const devSelect = document.getElementById('execution_target_device');
          if (devSelect && devSelect.value && devSelect.value !== 'server_local' && devSelect.value !== 'kaggle_cloud') {
            agentSessionId = devSelect.value;
          }
        }

        const workflowConfig = {
          method: document.getElementById('unified-setup-method')?.value || 'B3LYP',
          basis: document.getElementById('unified-setup-basis')?.value || 'def2-SVP',
          disp: document.getElementById('unified-setup-disp')?.value || 'D3BJ',
          solv_model: solventVal !== 'none' ? 'CPCM' : 'none',
          solvent: solventVal !== 'none' ? solventVal : 'Water',
          cores: parseInt(document.getElementById('resource_cpu_cores')?.value || '4', 10),
          ram: parseInt(document.getElementById('resource_ram_gb')?.value || '2', 10) * 1000,
          stages: stageDefs,
          backend: targetHost,
          target_device: agentSessionId,
        };

        try {
          // 1. Create unified reaction & generate 3D inputs
          const setupRes = await fetch('/api/v1/reactions/unified-setup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              equation: equation,
              workflow_config: workflowConfig,
            })
          });
          const setupData = await setupRes.json();
          if (!setupData.ok || !setupData.reaction) {
            alert('Setup failed: ' + (setupData.error?.message || setupData.error || 'Unknown error'));
            return;
          }

          const rxn = setupData.reaction;
          activeReactionId = rxn.reaction_id;
          activeReactionData = rxn;
          modal?.classList.add('hidden');

          // 2. Start queued execution
          const startRes = await fetch(`/api/v1/reactions/${encodeURIComponent(activeReactionId)}/start-execution`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ max_concurrency: concurrencyLimit })
          });
          const startData = await startRes.json();
          if (startData.reaction) activeReactionData = startData.reaction;

          // 3. Scroll to reaction workspace & render
          renderReactionExecutionWorkspace(activeReactionData);
          startExecutionPolling(activeReactionId);

          const wfPanel = document.getElementById('reaction-workflow');
          if (wfPanel) {
            wfPanel.scrollIntoView({ behavior: 'smooth' });
          }
        } catch (err) {
          alert('Failed to start reaction workflow: ' + err.message);
        } finally {
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = '⚡ Generate 3D Inputs & Run Workflow →';
          }
        }
      });
    }

    // Direct reaction equation form on Studio Area 1
    const rxnCreateForm = document.getElementById('rxn-wf-create-form');
    if (rxnCreateForm) {
      rxnCreateForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const eqInput = document.getElementById('rxn-wf-eq-input');
        const equation = (eqInput?.value || '').trim();
        if (!equation) return;

        if (eqDisplay) eqDisplay.textContent = equation;
        if (speciesTags) {
          speciesTags.innerHTML = '';
          const parts = equation.split('->');
          const terms = (parts[0] + ' + ' + (parts[1] || '')).split('+').map(s => s.trim()).filter(Boolean);
          terms.forEach(term => {
            const tag = document.createElement('span');
            tag.className = 'badge badge-info';
            tag.textContent = `🟢 3D Ready: ${term}`;
            speciesTags.appendChild(tag);
          });
        }
        if (modal) modal.classList.remove('hidden');
      });
    }
  }

  function startExecutionPolling(rxnId) {
    if (executionPollInterval) clearInterval(executionPollInterval);
    executionPollInterval = setInterval(async () => {
      try {
        const res = await fetch(`/api/v1/reactions/${encodeURIComponent(rxnId)}`);
        const data = await res.json();
        if (data.ok && data.reaction) {
          activeReactionData = data.reaction;
          renderReactionExecutionWorkspace(activeReactionData);

          if (data.reaction.state === 'COMPLETE' || data.reaction.thermodynamics) {
            clearInterval(executionPollInterval);
            executionPollInterval = null;
            renderThermodynamicsResults(data.reaction);
          }
        }
      } catch (err) {
        console.warn('Execution poll error:', err);
      }
    }, 2500);
  }

  function renderReactionExecutionWorkspace(rxn) {
    const ws = document.getElementById('rxn-wf-active-workspace');
    if (ws) ws.classList.remove('hidden');

    const stateBadge = document.getElementById('rxn-wf-rxn-state');
    if (stateBadge) {
      stateBadge.textContent = rxn.state || 'READY';
      stateBadge.className = 'badge ' + (rxn.state === 'COMPLETE' ? 'badge-success' : (rxn.state === 'RUNNING' ? 'badge-primary' : 'badge-info'));
    }

    const tbody = document.getElementById('rxn-wf-species-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    (rxn.species || []).forEach(sp => {
      const tr = document.createElement('tr');

      const tdName = document.createElement('td');
      const strongName = document.createElement('strong');
      strongName.textContent = sp.display_name || '';
      tdName.appendChild(strongName);
      tr.appendChild(tdName);

      const tdFormula = document.createElement('td');
      tdFormula.textContent = sp.formula || sp.display_name || '';
      tr.appendChild(tdFormula);

      const tdRole = document.createElement('td');
      tdRole.textContent = (sp.role || 'reactant').toUpperCase();
      tdRole.className = sp.role === 'reactant' ? 'text-primary font-bold' : 'text-success font-bold';
      tr.appendChild(tdRole);

      const tdNu = document.createElement('td');
      tdNu.textContent = String(sp.nu || sp.stoichiometric_coefficient || 1);
      tr.appendChild(tdNu);

      const tdCharge = document.createElement('td');
      tdCharge.textContent = `${sp.charge || 0} (2S+1=${sp.multiplicity || 1})`;
      tr.appendChild(tdCharge);

      const td3D = document.createElement('td');
      const span3D = document.createElement('span');
      if (sp.initial_geometry) {
        span3D.className = 'badge badge-success';
        span3D.textContent = '✓ 3D Coords';
      } else {
        span3D.className = 'badge badge-warning';
        span3D.textContent = 'Auto';
      }
      td3D.appendChild(span3D);
      tr.appendChild(td3D);

      // Stages Progress
      const tdStages = document.createElement('td');
      const stages = sp.stages || [];
      if (stages.length > 0) {
        stages.forEach((st, idx) => {
          let bClass = 'badge-secondary';
          if (st.state === 'COMPLETE') bClass = 'badge-success';
          else if (st.state === 'RUNNING') bClass = 'badge-primary';
          else if (st.state === 'QUEUED') bClass = 'badge-info';
          else if (st.state === 'FAILED') bClass = 'badge-danger';
          const badge = document.createElement('span');
          badge.className = `badge ${bClass}`;
          badge.style.marginRight = '4px';
          badge.textContent = `Step ${idx+1}: ${st.kind || ''} (${st.state || ''})`;
          tdStages.appendChild(badge);
        });
      } else {
        const pending = document.createElement('span');
        pending.className = 'text-muted';
        pending.textContent = 'Pending';
        tdStages.appendChild(pending);
      }
      tr.appendChild(tdStages);

      // Actions / Downloads
      const tdAction = document.createElement('td');
      const completedStage = (sp.stages || []).find(s => s.output_text || s.agent_job_id);
      if (completedStage && completedStage.agent_job_id) {
        const dFlex = document.createElement('div');
        dFlex.className = 'd-flex gap-1';

        const jobIdSafe = encodeURIComponent(String(completedStage.agent_job_id));

        const aOut = document.createElement('a');
        aOut.href = `/api/v1/local-agent/jobs/${jobIdSafe}/out`;
        aOut.className = 'btn btn-ghost btn-small';
        aOut.title = 'Download Output Log';
        aOut.textContent = '⬇ .out';

        const aXyz = document.createElement('a');
        aXyz.href = `/api/v1/local-agent/jobs/${jobIdSafe}/xyz`;
        aXyz.className = 'btn btn-ghost btn-small';
        aXyz.title = 'Download 3D Geometry';
        aXyz.textContent = '⬇ .xyz';

        dFlex.appendChild(aOut);
        dFlex.appendChild(aXyz);
        tdAction.appendChild(dFlex);
      } else {
        const spanState = document.createElement('span');
        spanState.className = 'small text-muted';
        spanState.textContent = sp.state || 'READY';
        tdAction.appendChild(spanState);
      }
      tr.appendChild(tdAction);

      tbody.appendChild(tr);
    });
  }

  function renderThermodynamicsResults(rxn) {
    const resultsContainer = document.getElementById('rxn-wf-thermo-results');
    if (resultsContainer) resultsContainer.classList.remove('hidden');

    const thermo = rxn.thermodynamics;
    if (!thermo) return;

    const tbody = document.getElementById('rxn-wf-thermo-tbody');
    if (tbody) {
      const dE_kj = thermo.dE_elec_kj_mol || (thermo.delta_E_elec_hartree * 2625.5);
      const dH_kj = thermo.dH_kj_mol || (thermo.delta_H_hartree * 2625.5);
      const dG_kj = thermo.dG_kj_mol || (thermo.delta_G_hartree * 2625.5);
      const dS = thermo.dS_j_mol_k || thermo.delta_S_j_mol_k || 0;
      const log10k = thermo.log10_k !== undefined ? thermo.log10_k.toFixed(2) : '-';
      const kVal = thermo.k ? (thermo.k > 1e6 || thermo.k < 1e-4 ? thermo.k.toExponential(3) : thermo.k.toFixed(4)) : '-';

      tbody.innerHTML = '';
      const tr = document.createElement('tr');

      const tdDe = document.createElement('td');
      tdDe.className = 'font-bold';
      tdDe.textContent = dE_kj ? dE_kj.toFixed(2) : '-';
      tr.appendChild(tdDe);

      const tdDh = document.createElement('td');
      tdDh.className = 'font-bold text-primary';
      tdDh.textContent = dH_kj ? dH_kj.toFixed(2) : '-';
      tr.appendChild(tdDh);

      const tdDg = document.createElement('td');
      tdDg.className = 'font-bold ' + (dG_kj < 0 ? 'text-success' : 'text-danger');
      tdDg.textContent = dG_kj ? dG_kj.toFixed(2) : '-';
      tr.appendChild(tdDg);

      const tdDs = document.createElement('td');
      tdDs.textContent = dS ? dS.toFixed(2) : '-';
      tr.appendChild(tdDs);

      const tdLogK = document.createElement('td');
      tdLogK.textContent = String(log10k);
      tr.appendChild(tdLogK);

      const tdK = document.createElement('td');
      tdK.className = 'mono font-bold';
      tdK.textContent = String(kVal);
      tr.appendChild(tdK);

      tbody.appendChild(tr);
    }

    // Set download links
    const imgBtn = document.getElementById('rxn-wf-download-diagram-png-btn');
    const zipBtn = document.getElementById('rxn-wf-download-all-zip-btn');
    const pdfBtn = document.getElementById('rxn-wf-download-pdf-btn');
    const diagramImg = document.getElementById('rxn-wf-diagram-img');
    const diagramPreviewBox = document.getElementById('rxn-wf-diagram-preview-container');

    const imgUrl = `/api/v1/reactions/${encodeURIComponent(rxn.reaction_id)}/thermodynamics/image`;
    const zipUrl = `/api/v1/reactions/${encodeURIComponent(rxn.reaction_id)}/download-all-outputs`;

    if (imgBtn) {
      imgBtn.href = imgUrl;
      imgBtn.download = `reaction_thermodynamics_${rxn.reaction_id.slice(0, 8)}.png`;
    }
    if (zipBtn) {
      zipBtn.href = zipUrl;
      zipBtn.download = `reaction_outputs_${rxn.reaction_id.slice(0, 8)}.zip`;
    }
    if (pdfBtn && rxn.thermo_report_id) {
      pdfBtn.href = `/api/v1/thermo-reports/${encodeURIComponent(rxn.thermo_report_id)}`;
    }

    // Show image preview
    if (diagramImg && diagramPreviewBox) {
      diagramImg.src = imgUrl + '?t=' + Date.now();
      diagramPreviewBox.classList.remove('hidden');
    }
  }

  // Expose global client for wizard and UI integration
  window.ReactionUnifiedClient = {
    startExecution: async function(reactionId, options = {}) {
      if (!reactionId) return;
      activeReactionId = reactionId;
      try {
        const concurrencyLimit = options.max_concurrency || 1;
        const startRes = await fetch(`/api/v1/reactions/${encodeURIComponent(reactionId)}/start-execution`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ max_concurrency: concurrencyLimit })
        });
        const startData = await startRes.json();
        if (startData && startData.reaction) activeReactionData = startData.reaction;

        renderReactionExecutionWorkspace(activeReactionData || { reaction_id: reactionId, state: 'RUNNING' });
        startExecutionPolling(reactionId);

        const wfPanel = document.getElementById('reaction-workflow') || document.getElementById('rxn-wf-active-workspace');
        if (wfPanel) {
          wfPanel.scrollIntoView({ behavior: 'smooth' });
        }
      } catch (err) {
        console.error('Failed to start reaction execution:', err);
      }
    },
    renderWorkspace: renderReactionExecutionWorkspace,
    pollExecution: startExecutionPolling
  };

  document.addEventListener('DOMContentLoaded', () => {
    initUnifiedReactionController();
  });
})();
