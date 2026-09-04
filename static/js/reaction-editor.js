// static/js/reaction-editor.js
// Unified Reaction Workspace, Execution Dispatch & Thermochemistry Controller
(function() {
  'use strict';

  let currentReaction = null;
  let pollTimer = null;

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function initUnifiedReactionsWorkspace() {
    const eqForm = document.getElementById('reactions-eq-form');
    const eqInput = document.getElementById('reactions-eq-input');
    const calcThermoBtn = document.getElementById('btn-calculate-thermodynamics');
    const downloadPdfBtn = document.getElementById('btn-download-reaction-pdf');

    if (eqForm) {
      eqForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const equation = (eqInput ? eqInput.value : '').trim();
        if (!equation) return;

        try {
          const res = await fetch('/api/v1/reactions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ equation: equation })
          });
          const data = await res.json();
          if (!data.ok) {
            alert(data.error ? data.error.message : 'Failed to create reaction');
            return;
          }
          currentReaction = data.reaction;
          renderReactionsTable(currentReaction);
          updateBalanceBadge(currentReaction);
        } catch (err) {
          alert('Network error: ' + err.message);
        }
      });
    }

    if (calcThermoBtn) {
      calcThermoBtn.addEventListener('click', async () => {
        if (!currentReaction) {
          alert('Please validate a reaction equation first.');
          return;
        }

        // 1. Get execution backend & hardware resource configuration
        let executionPayload = null;
        try {
          if (window.LocalAgentClient && typeof window.LocalAgentClient.getExecutionPayload === 'function') {
            executionPayload = window.LocalAgentClient.getExecutionPayload();
          }
        } catch (err) {
          alert('Execution Blocked: ' + err.message);
          return;
        }

        calcThermoBtn.disabled = true;
        calcThermoBtn.textContent = 'Calculating Stages & Thermochemistry...';

        try {
          // Compute thermodynamics via authoritative backend
          const res = await fetch('/api/v1/reactions/' + encodeURIComponent(currentReaction.reaction_id) + '/thermodynamics/calculate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              target_device: executionPayload ? executionPayload.target_device : null,
              resources: executionPayload ? executionPayload.resources : {}
            })
          });
          const data = await res.json();
          if (data.ok && data.thermodynamics) {
            renderThermoResults(data.thermodynamics);
            if (downloadPdfBtn) downloadPdfBtn.classList.remove('hidden');
          } else {
            const warnBox = document.getElementById('reactions-warnings-container');
            if (warnBox) {
              warnBox.textContent = (data.error && data.error.message) || data.error || 'Thermochemistry calculation pending stage completions.';
              warnBox.classList.remove('hidden');
            }
          }
        } catch (err) {
          console.warn('Thermodynamics trigger:', err);
        } finally {
          calcThermoBtn.disabled = false;
          calcThermoBtn.textContent = '⚡ Calculate Thermodynamics';
        }
      });
    }

    if (downloadPdfBtn) {
      downloadPdfBtn.addEventListener('click', async () => {
        if (!currentReaction) return;
        try {
          const res = await fetch('/api/v1/reactions/' + encodeURIComponent(currentReaction.reaction_id) + '/thermodynamics/report', { method: 'POST' });
          const data = await res.json();
          if (data.ok && data.report) {
            window.open('/api/v1/reactions/' + encodeURIComponent(currentReaction.reaction_id) + '/thermodynamics/report/' + encodeURIComponent(data.report.report_id) + '/pdf', '_blank');
          }
        } catch (err) {
          alert('Could not generate PDF: ' + err.message);
        }
      });
    }
  }

  function updateBalanceBadge(rxn) {
    const badge = document.getElementById('reactions-balance-badge');
    if (!badge) return;
    badge.classList.remove('hidden');
    if (rxn.balance_valid) {
      badge.className = 'reaction-badge badge-success';
      badge.textContent = '✓ Stoichiometrically Balanced';
    } else {
      badge.className = 'reaction-badge badge-warning';
      badge.textContent = '⚠ Atom Imbalance: ' + (rxn.balance_warnings || []).join('; ');
    }
  }

  function renderReactionsTable(rxn) {
    const tbody = document.getElementById('reactions-species-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    (rxn.species || []).forEach(sp => {
      const tr = document.createElement('tr');

      const roleTd = document.createElement('td');
      roleTd.textContent = sp.role === 'reactant' ? 'Reactant' : 'Product';
      tr.appendChild(roleTd);

      const nameTd = document.createElement('td');
      nameTd.className = 'font-bold';
      nameTd.textContent = sp.display_name;
      tr.appendChild(nameTd);

      const coeffTd = document.createElement('td');
      coeffTd.textContent = String(sp.nu || sp.stoichiometric_coefficient || '1');
      tr.appendChild(coeffTd);

      const stagesTd = document.createElement('td');
      stagesTd.textContent = (sp.stages && sp.stages.length > 0)
        ? sp.stages.map(s => s.kind).join(' → ')
        : 'Opt → Freq';
      tr.appendChild(stagesTd);

      const statusTd = document.createElement('td');
      const badge = document.createElement('span');
      badge.className = 'badge ' + (sp.state === 'COMPLETE' ? 'badge-success' : 'badge-info');
      badge.textContent = sp.state || 'READY';
      statusTd.appendChild(badge);
      tr.appendChild(statusTd);

      const eTd = document.createElement('td');
      eTd.textContent = sp.final_result && sp.final_result.energy_hartree ? sp.final_result.energy_hartree.toFixed(6) : '-';
      tr.appendChild(eTd);

      const gTd = document.createElement('td');
      gTd.textContent = sp.final_result && sp.final_result.gibbs_hartree ? sp.final_result.gibbs_hartree.toFixed(6) : '-';
      tr.appendChild(gTd);

      tbody.appendChild(tr);
    });
  }

  function renderThermoResults(th) {
    const dE = document.getElementById('val-rxn-delta-e');
    const dZpe = document.getElementById('val-rxn-delta-zpe');
    const dH = document.getElementById('val-rxn-delta-h');
    const dG = document.getElementById('val-rxn-delta-g');
    const dS = document.getElementById('val-rxn-delta-s');
    const kEq = document.getElementById('val-rxn-k-eq');
    const warnBox = document.getElementById('reactions-warnings-container');

    if (dE) dE.textContent = th.delta_E_elec_kcal_mol ? `${th.delta_E_elec_kcal_mol.toFixed(2)} kcal/mol` : (th.delta_E_elec_hartree ? `${th.delta_E_elec_hartree.toFixed(6)} Eh` : '-');
    if (dZpe) dZpe.textContent = th.delta_ZPE_kcal_mol ? `${th.delta_ZPE_kcal_mol.toFixed(2)} kcal/mol` : (th.delta_ZPE_hartree ? `${th.delta_ZPE_hartree.toFixed(6)} Eh` : '-');
    if (dH) dH.textContent = th.delta_H_kcal_mol ? `${th.delta_H_kcal_mol.toFixed(2)} kcal/mol` : (th.delta_H_hartree ? `${th.delta_H_hartree.toFixed(6)} Eh` : '-');
    if (dG) dG.textContent = th.delta_G_kcal_mol ? `${th.delta_G_kcal_mol.toFixed(2)} kcal/mol` : (th.delta_G_hartree ? `${th.delta_G_hartree.toFixed(6)} Eh` : '-');
    if (dS) dS.textContent = th.delta_S_j_mol_k ? `${th.delta_S_j_mol_k.toFixed(2)}` : '-';
    if (kEq) kEq.textContent = th.k_eq_scientific || (th.log10_k_eq !== undefined ? `log10 K = ${th.log10_k_eq.toFixed(2)}` : '-');

    if (warnBox) {
      if (th.warnings && th.warnings.length > 0) {
        warnBox.textContent = '⚠ Warnings: ' + th.warnings.join(' | ');
        warnBox.classList.remove('hidden');
      } else {
        warnBox.classList.add('hidden');
      }
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    initUnifiedReactionsWorkspace();
  });
})();
