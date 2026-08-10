from pathlib import Path

app = Path('static/js/app.js')
text = app.read_text(encoding='utf-8')
marker = '/* CHEMLAB KAGGLE BROWSER UPGRADE 2026-08-10 */'
if marker not in text:
    upgrade = r'''

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
'''
    needle = '\n})();'
    if needle not in text: raise SystemExit('Could not find app.js IIFE terminator')
    app.write_text(text.rsplit(needle, 1)[0] + upgrade + needle, encoding='utf-8')

html = Path('templates/index.html')
h = html.read_text(encoding='utf-8')
h = h.replace('Signing in also pulls back any jobs already submitted from this Kaggle account, so you can get\n        back to them even after clearing your browser data or switching devices.', 'Signing in also pulls the account\'s Chemistry Lab jobs directly from Kaggle, so the list can be recovered on a new browser or device.')
h = h.replace('Downloaded and extracted into /tmp on the Kaggle job at start-up.', 'The Kaggle job downloads and extracts it into scratch storage at start-up. You can also open the link or start the browser download here.')
html.write_text(h, encoding='utf-8')
print('Patch completed')
