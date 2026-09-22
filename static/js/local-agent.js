// static/js/local-agent.js
// Chemistry Lab Local & HPC Companion Agent Frontend Client
(function(window) {
    'use strict';

    const LocalAgentClient = {
        devices: [],
        selectedDeviceId: null,
        _deviceFetchGeneration: 0,
        _deviceFetchController: null,

        async init() {
            this.bindEvents();
            this.loadSavedPreferences();
            await this.fetchPackages();
            await this.fetchDevices();
        },

        loadSavedPreferences() {
            try {
                const orcaPathInput = document.getElementById('local-orca-executable-path');
                if (orcaPathInput) {
                    const saved = localStorage.getItem('chemlab_local_orca_path');
                    if (saved) orcaPathInput.value = saved;
                }
                const workDirInput = document.getElementById('local-working-directory');
                if (workDirInput) {
                    const saved = localStorage.getItem('chemlab_local_workdir');
                    if (saved) workDirInput.value = saved;
                }
                const customInpInput = document.getElementById('local-custom-input-path');
                if (customInpInput) {
                    const saved = localStorage.getItem('chemlab_local_custom_inp');
                    if (saved) customInpInput.value = saved;
                }
                const siteKeyInput = document.getElementById('input-site-api-key');
                if (siteKeyInput) {
                    const saved = sessionStorage.getItem('orca_site_api_key');
                    if (saved) siteKeyInput.value = saved;
                }
            } catch (e) {
                console.warn('Could not load saved local agent preferences:', e);
            }
        },

        bindEvents() {
            const connectBtn = document.getElementById('btn-open-connect-computer');
            const connectModal = document.getElementById('connect-computer-modal');
            const closeModalBtn = document.getElementById('btn-close-connect-modal');
            const submitApiBtn = document.getElementById('btn-submit-connection-api');
            const importTxtInput = document.getElementById('pairing-txt-file-input');
            const importTxtBtn = document.getElementById('btn-import-pairing-txt');
            const deviceSelect = document.getElementById('execution_target_device');

            const downloadAgentBtn = document.getElementById('btn-open-download-agent');
            const downloadModal = document.getElementById('download-agent-modal');
            const closeDownloadBtn = document.getElementById('btn-close-download-modal');

            const enableKaggleBtn = document.getElementById('btn-open-kaggle-modal');
            const kaggleModal = document.getElementById('kaggle-cloud-modal');
            const closeKaggleBtn = document.getElementById('btn-close-kaggle-modal');
            const saveKaggleBtn = document.getElementById('btn-save-kaggle-creds');

            if (connectBtn && connectModal) {
                connectBtn.addEventListener('click', () => {
                    connectModal.classList.remove('hidden');
                    const apiInput = document.getElementById('input-connection-api');
                    if (apiInput) apiInput.focus();
                });
            }

            if (closeModalBtn && connectModal) {
                closeModalBtn.addEventListener('click', () => {
                    connectModal.classList.add('hidden');
                    const apiInput = document.getElementById('input-connection-api');
                    if (apiInput) apiInput.value = '';
                });
            }

            if (downloadAgentBtn && downloadModal) {
                downloadAgentBtn.addEventListener('click', () => {
                    downloadModal.classList.remove('hidden');
                    this.fetchPackages();
                });
            }

            if (closeDownloadBtn && downloadModal) {
                closeDownloadBtn.addEventListener('click', () => {
                    downloadModal.classList.add('hidden');
                });
            }

            if (enableKaggleBtn && kaggleModal) {
                enableKaggleBtn.addEventListener('click', () => {
                    kaggleModal.classList.remove('hidden');
                    try {
                        const savedP = sessionStorage.getItem('orca_kaggle_passcode');
                        const pInput = document.getElementById('modal-kaggle-passcode');
                        if (pInput && savedP) pInput.value = savedP;
                    } catch (e) {}
                });
            }

            if (closeKaggleBtn && kaggleModal) {
                closeKaggleBtn.addEventListener('click', () => {
                    kaggleModal.classList.add('hidden');
                });
            }

            if (saveKaggleBtn) {
                saveKaggleBtn.addEventListener('click', async () => {
                    const uInput = document.getElementById('modal-kaggle-username');
                    const kInput = document.getElementById('modal-kaggle-key');
                    const pInput = document.getElementById('modal-kaggle-passcode');
                    const errBox = document.getElementById('kaggle-modal-error');
                    if (errBox) errBox.classList.add('hidden');

                    const username = (uInput ? uInput.value : '').trim();
                    const key = (kInput ? kInput.value : '').trim();
                    const passcode = (pInput ? pInput.value : '').trim();
                    if (!username || !key) {
                        if (errBox) {
                            errBox.textContent = 'Please enter both Kaggle username and API key.';
                            errBox.classList.remove('hidden');
                        }
                        return;
                    }

                    if (errBox) errBox.classList.add('hidden');
                    try {
                        const vResp = await fetch('/api/kaggle/verify-passcode', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ passcode: passcode })
                        });
                        const vData = await vResp.json().catch(() => ({}));
                        if (!vResp.ok || !vData.ok) {
                            if (errBox) {
                                errBox.textContent = vData.message || 'Invalid execution passcode. On cloud/domain deployments, please enter the administrator secret passcode.';
                                errBox.classList.remove('hidden');
                            }
                            if (pInput) pInput.focus();
                            return;
                        }
                    } catch (e) {}

                    if (passcode) {
                        try {
                            sessionStorage.setItem('orca_kaggle_passcode', passcode);
                            sessionStorage.setItem('orca_kaggle_unlocked', 'true');
                            const kPass = document.getElementById('kaggle-passcode');
                            if (kPass) kPass.value = passcode;
                        } catch (e) {}
                    }

                    try {
                        saveKaggleBtn.disabled = true;
                        saveKaggleBtn.textContent = 'Saving...';
                        const resp = await fetch('/api/kaggle/credentials', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                kaggle_username: username,
                                kaggle_key: key,
                                username: username,
                                key: key
                            })
                        });
                        const data = await resp.json();
                        if (!resp.ok || !data.ok) {
                            throw new Error(data.error || 'Failed to save Kaggle credentials');
                        }
                        if (kaggleModal) kaggleModal.classList.add('hidden');
                        if (typeof window.setKaggleSignedIn === 'function') {
                            window.setKaggleSignedIn(username, key);
                        }
                        alert('Kaggle credentials saved securely and enabled.');
                        const kaggleTab = document.getElementById('backend-tab-kaggle');
                        if (kaggleTab) kaggleTab.classList.remove('disabled');
                    } catch (err) {
                        if (errBox) {
                            errBox.textContent = err.message || 'Error saving credentials';
                            errBox.classList.remove('hidden');
                        }
                    } finally {
                        saveKaggleBtn.disabled = false;
                        saveKaggleBtn.textContent = 'Save & Enable';
                    }
                });
            }

            if (importTxtBtn && importTxtInput) {
                importTxtBtn.addEventListener('click', () => importTxtInput.click());
                importTxtInput.addEventListener('change', (e) => {
                    const file = e.target.files && e.target.files[0];
                    if (!file) return;
                    const reader = new FileReader();
                    reader.onload = (evt) => {
                        const content = evt.target.result;
                        const match = content.match(/CLA_[A-Za-z0-9_-]+/);
                        if (match) {
                            const apiInput = document.getElementById('input-connection-api');
                            if (apiInput) apiInput.value = match[0];
                        } else {
                            alert('No Connection API (CLA_...) found in imported file.');
                        }
                    };
                    reader.readAsText(file);
                });
            }

            if (submitApiBtn) {
                submitApiBtn.addEventListener('click', async () => {
                    const apiInput = document.getElementById('input-connection-api');
                    const nameInput = document.getElementById('input-device-custom-name');
                    const errorBox = document.getElementById('connect-modal-error');
                    if (errorBox) errorBox.classList.add('hidden');

                    const token = (apiInput ? apiInput.value : '').trim();
                    const customName = (nameInput ? nameInput.value : '').trim();

                    if (!token || !token.startsWith('CLA_')) {
                        if (errorBox) {
                            errorBox.textContent = 'Please enter a valid Connection API starting with CLA_';
                            errorBox.classList.remove('hidden');
                        }
                        return;
                    }

                    try {
                        submitApiBtn.disabled = true;
                        submitApiBtn.textContent = 'Connecting...';
                        const res = await this.claimConnectionAPI(token, customName);
                        if (connectModal) connectModal.classList.add('hidden');
                        if (apiInput) apiInput.value = '';
                        alert('Successfully connected to ' + res.display_name + '.');
                    } catch (err) {
                        if (errorBox) {
                            errorBox.textContent = err.message || 'Connection failed';
                            errorBox.classList.remove('hidden');
                        }
                    } finally {
                        submitApiBtn.disabled = false;
                        submitApiBtn.textContent = 'Connect Computer →';
                    }
                });
            }

            const inlineSubmitBtn = document.getElementById('inline-btn-submit-connection-api');
            if (inlineSubmitBtn) {
                inlineSubmitBtn.addEventListener('click', async () => {
                    const apiInput = document.getElementById('inline-input-connection-api');
                    const nameInput = document.getElementById('inline-input-device-custom-name');
                    const errorBox = document.getElementById('inline-connect-modal-error');
                    if (errorBox) errorBox.classList.add('hidden');

                    const token = (apiInput ? apiInput.value : '').trim();
                    const customName = (nameInput ? nameInput.value : '').trim();

                    if (!token || !token.startsWith('CLA_')) {
                        if (errorBox) {
                            errorBox.textContent = 'Please enter a valid Connection API starting with CLA_';
                            errorBox.classList.remove('hidden');
                        }
                        return;
                    }

                    try {
                        inlineSubmitBtn.disabled = true;
                        inlineSubmitBtn.textContent = 'Connecting...';
                        const res = await this.claimConnectionAPI(token, customName);
                        if (apiInput) apiInput.value = '';
                        alert('Successfully connected to ' + res.display_name + '.');
                    } catch (err) {
                        if (errorBox) {
                            errorBox.textContent = err.message || 'Connection failed';
                            errorBox.classList.remove('hidden');
                        }
                    } finally {
                        inlineSubmitBtn.disabled = false;
                        inlineSubmitBtn.textContent = 'Connect Device';
                    }
                });
            }

            const inlineImportTxtBtn = document.getElementById('inline-btn-import-pairing-txt');
            const inlineImportTxtInput = document.getElementById('inline-pairing-txt-file-input');
            if (inlineImportTxtBtn && inlineImportTxtInput) {
                inlineImportTxtBtn.addEventListener('click', () => inlineImportTxtInput.click());
                inlineImportTxtInput.addEventListener('change', (e) => {
                    const file = e.target.files && e.target.files[0];
                    if (!file) return;
                    const reader = new FileReader();
                    reader.onload = (evt) => {
                        const content = evt.target.result;
                        const match = content.match(/CLA_[A-Za-z0-9_-]+/);
                        if (match) {
                            const apiInput = document.getElementById('inline-input-connection-api');
                            if (apiInput) apiInput.value = match[0];
                        } else {
                            alert('No Connection API (CLA_...) found in imported file.');
                        }
                    };
                    reader.readAsText(file);
                });
            }

            if (deviceSelect) {
                deviceSelect.addEventListener('change', () => {
                    this.selectedDeviceId = deviceSelect.value;
                    this.updateResourceUI();
                });
            }
        },

        updateResourceUI() {
            const hpcFields = document.getElementById('hpc-resource-fields');
            const badgeContainer = document.getElementById('selected-device-status-badge');
            const deviceSelect = document.getElementById('execution_target_device');
            const selVal = deviceSelect ? deviceSelect.value : (this.selectedDeviceId || '');

            if (!selVal || selVal === 'no_device') {
                if (hpcFields) hpcFields.classList.add('hidden');
                if (badgeContainer) {
                    badgeContainer.className = 'badge badge-danger';
                    badgeContainer.textContent = '🔴 No Local Computer Connected';
                }
                return;
            }

            if (selVal === 'kaggle_cloud') {
                if (hpcFields) hpcFields.classList.add('hidden');
                if (badgeContainer) {
                    badgeContainer.className = 'badge badge-secondary';
                    badgeContainer.textContent = '☁️ Kaggle Cloud Backend';
                }
                return;
            }

            const matched = this.devices.find(d => d.agent_session_id === selVal);
            if (matched) {
                const isHpc = matched.backend_kind === 'hpc';
                if (hpcFields) {
                    if (isHpc) hpcFields.classList.remove('hidden');
                    else hpcFields.classList.add('hidden');
                }
                if (badgeContainer) {
                    const isOnline = matched.status === 'ONLINE';
                    badgeContainer.className = isOnline ? 'badge badge-success' : 'badge badge-danger';
                    badgeContainer.textContent = (isOnline ? '🟢 Online: ' : '🔴 OFFLINE: ') + matched.display_name;
                }
            }
        },

        async fetchPackages() {
            try {
                const resp = await fetch('/api/v1/local-agent/packages');
                if (resp.ok) {
                    const data = await resp.json();
                    this.renderPackageDownloads(data.packages || []);
                }
            } catch (err) {
                console.warn('Failed to fetch package manifest:', err);
            }
        },

        renderPackageDownloads(packages) {
            const container = document.getElementById('agent-packages-download-list');
            if (!container) return;
            container.innerHTML = '';

            packages.forEach(pkg => {
                const card = document.createElement('div');
                card.className = 'p-3 border rounded mb-2 bg-surface d-flex justify-between align-center';

                const infoDiv = document.createElement('div');
                const title = document.createElement('div');
                title.className = 'font-bold';
                title.textContent = pkg.display_name + ' (v' + pkg.version + ')';
                infoDiv.appendChild(title);

                const meta = document.createElement('div');
                meta.className = 'text-xs text-muted';
                meta.textContent = pkg.filename + ' · ' + (pkg.size_bytes / 1024).toFixed(1) + ' KB · SHA-256: ' + pkg.sha256.substring(0, 12) + '...';
                infoDiv.appendChild(meta);

                const downloadBtn = document.createElement('a');
                downloadBtn.href = '/api/v1/local-agent/packages/' + encodeURIComponent(pkg.package_id);
                downloadBtn.className = 'btn btn-primary btn-small';
                downloadBtn.download = pkg.filename;
                downloadBtn.textContent = 'Download ' + pkg.platform.toUpperCase();

                card.appendChild(infoDiv);
                card.appendChild(downloadBtn);
                container.appendChild(card);
            });
        },

        async fetchDevices() {
            const generation = ++this._deviceFetchGeneration;
            if (this._deviceFetchController) this._deviceFetchController.abort();
            const controller = new AbortController();
            this._deviceFetchController = controller;
            try {
                let clientId = localStorage.getItem('orca_local_client_id');
                if (!clientId) {
                    clientId = 'client_' + Math.random().toString(36).substring(2, 15);
                    localStorage.setItem('orca_local_client_id', clientId);
                }
                const siteApiKey = sessionStorage.getItem('orca_site_api_key') || '';
                const headers = { 'X-User-Id': clientId };
                if (siteApiKey) headers['X-Site-API-Key'] = siteApiKey;

                const resp = await fetch('/api/v1/local-agent/devices', {
                    headers,
                    signal: controller.signal,
                    cache: 'no-store'
                });
                if (resp.ok) {
                    const data = await resp.json();
                    if (generation !== this._deviceFetchGeneration || controller.signal.aborted) return [];
                    this.devices = data.devices || [];
                    this.renderDeviceList();
                    this.renderDeviceDropdown();
                    this.updateResourceUI();
                    return this.devices;
                }
            } catch (err) {
                if (err.name !== 'AbortError') console.warn('Failed to fetch runtime devices:', err);
            } finally {
                if (generation === this._deviceFetchGeneration) this._deviceFetchController = null;
            }
            return [];
        },

        async claimConnectionAPI(connectionApi, customDeviceName) {
            const clean = (connectionApi || '').trim();
            if (!clean.startsWith('CLA_')) {
                throw new Error('Invalid Connection API format (must start with CLA_)');
            }
            let clientId = localStorage.getItem('orca_local_client_id');
            if (!clientId) {
                clientId = 'client_' + Math.random().toString(36).substring(2, 15) + Math.random().toString(36).substring(2, 15);
                localStorage.setItem('orca_local_client_id', clientId);
            }
            const siteApiKey = sessionStorage.getItem('orca_site_api_key') || '';
            const headers = { 'Content-Type': 'application/json', 'X-User-Id': clientId };
            if (siteApiKey) {
                headers['X-Site-API-Key'] = siteApiKey;
            }
            const resp = await fetch('/api/v1/local-agent/runtime/claim', {
                method: 'POST',
                headers: headers,
                body: JSON.stringify({
                    connection_api: clean,
                    custom_device_name: customDeviceName || null
                })
            });
            const data = await resp.json();
            if (!resp.ok || !data.ok) {
                const msg = (data.detail && data.detail.message) || data.error || 'Pairing failed';
                throw new Error(msg);
            }
            await this.fetchDevices();
            return data;
        },

        renderDeviceList() {
            const containerIds = ['connected-devices-list', 'settings-connected-devices-list'];
            containerIds.forEach(containerId => {
                const container = document.getElementById(containerId);
                if (!container) return;
                container.innerHTML = '';

                if (this.devices.length === 0) {
                    const empty = document.createElement('div');
                    empty.className = 'small text-muted p-2';
                    empty.textContent = 'No local computers or clusters connected yet. Click "+ Connect Computer" to add one.';
                    container.appendChild(empty);
                    return;
                }

                const list = document.createElement('div');
                list.className = 'space-y-2';

                this.devices.forEach(dev => {
                    const item = document.createElement('div');
                    item.className = 'd-flex justify-between align-center p-2 border rounded bg-base-100 mb-1';

                    const left = document.createElement('div');
                    const nameSpan = document.createElement('span');
                    nameSpan.className = 'font-bold';
                    nameSpan.textContent = dev.display_name;
                    left.appendChild(nameSpan);

                    const statusBadge = document.createElement('span');
                    const isOnline = dev.status === 'ONLINE';
                    statusBadge.className = 'badge ' + (isOnline ? 'badge-success' : 'badge-danger') + ' text-xs ml-2';
                    statusBadge.textContent = isOnline ? '🟢 Online' : '🔴 Offline';
                    left.appendChild(statusBadge);

                    const platBadge = document.createElement('span');
                    platBadge.className = 'badge badge-outline text-xs ml-1';
                    platBadge.textContent = dev.backend_kind === 'hpc' ? ('HPC (' + (dev.scheduler_type || 'Cluster').toUpperCase() + ')') : dev.platform.toUpperCase();
                    left.appendChild(platBadge);

                    const sessDiv = document.createElement('div');
                    sessDiv.className = 'text-xs text-muted';
                    sessDiv.textContent = 'Session: ' + dev.agent_session_id.substring(0, 12) + '...';
                    left.appendChild(sessDiv);

                    const discBtn = document.createElement('button');
                    discBtn.type = 'button';
                    discBtn.className = 'btn btn-ghost btn-xs text-danger';
                    discBtn.textContent = 'Disconnect';
                    discBtn.addEventListener('click', () => this.disconnectDevice(dev.agent_session_id));

                    item.appendChild(left);
                    item.appendChild(discBtn);
                    list.appendChild(item);
                });

                container.appendChild(list);
            });
        },

        async disconnectDevice(sessionId) {
            if (!confirm('Disconnect this computer?')) return;
            try {
                const resp = await fetch('/api/v1/local-agent/devices/' + encodeURIComponent(sessionId), { method: 'DELETE' });
                if (!resp.ok) {
                    const data = await resp.json().catch(() => ({}));
                    alert('Could not disconnect device: ' + (data.detail || data.error || 'Access denied'));
                }
                await this.fetchDevices();
            } catch (err) {
                console.error('Error disconnecting device:', err);
            }
        },

        renderDeviceDropdown() {
            const selectEl = document.getElementById('execution_target_device');
            if (!selectEl) return;

            const prevVal = selectEl.value;
            selectEl.innerHTML = '';

            // Preserve server-side targets while refreshing the asynchronous
            // Local Agent device list.
            [['server_local', 'Server Host ORCA'], ['kaggle_cloud', 'Kaggle Cloud Backend']].forEach(([value, label]) => {
                const opt = document.createElement('option');
                opt.value = value;
                opt.textContent = label;
                selectEl.appendChild(opt);
            });

            const onlineDevices = this.devices.filter(d => d.status === 'ONLINE');

            if (this.devices && this.devices.length > 0) {
                this.devices.forEach(dev => {
                    const opt = document.createElement('option');
                    opt.value = dev.agent_session_id;
                    const statusBadge = dev.status === 'ONLINE' ? '🟢 Online' : '🔴 Offline';
                    opt.textContent = dev.display_name + ' (' + dev.platform.toUpperCase() + ') - ' + statusBadge;
                    selectEl.appendChild(opt);
                });
            } else {
                const noDevOpt = document.createElement('option');
                noDevOpt.value = 'no_device';
                noDevOpt.textContent = '🔴 No Local Computer Connected (Click "+ Connect Computer")';
                selectEl.appendChild(noDevOpt);
            }

            if (prevVal && Array.from(selectEl.options).some(o => o.value === prevVal)) {
                selectEl.value = prevVal;
            } else if (onlineDevices.length > 0) {
                selectEl.value = onlineDevices[0].agent_session_id;
            } else {
                selectEl.value = 'server_local';
            }
            this.selectedDeviceId = selectEl.value;
        },

        getExecutionPayload() {
            const deviceSelect = document.getElementById('execution_target_device');
            const selVal = deviceSelect ? deviceSelect.value : (this.selectedDeviceId || '');

            if (!selVal || selVal === 'no_device') {
                throw new Error('No local computer connected. Please click "+ Connect Computer" to pair your Local Agent.');
            }

            if (selVal !== 'kaggle_cloud') {
                const matched = this.devices.find(d => d.agent_session_id === selVal);
                if (!matched) {
                    throw new Error('Selected execution device was not found.');
                }
                if (matched.status !== 'ONLINE') {
                    throw new Error('Selected computer is offline.');
                }
            }

            const cpuInput = document.getElementById('resource_cpu_cores');
            const ramInput = document.getElementById('resource_ram_gb');
            const diskInput = document.getElementById('resource_disk_gb');
            const nodesInput = document.getElementById('resource_hpc_nodes');
            const walltimeInput = document.getElementById('resource_hpc_walltime');
            const partitionInput = document.getElementById('resource_hpc_partition');

            return {
                target_device: selVal,
                resources: {
                    cpu_cores: parseInt(cpuInput ? cpuInput.value : '4', 10) || 4,
                    ram_gb: parseFloat(ramInput ? ramInput.value : '8.0') || 8.0,
                    disk_gb: parseFloat(diskInput ? diskInput.value : '20.0') || 20.0,
                    hpc_nodes: parseInt(nodesInput ? nodesInput.value : '1', 10) || 1,
                    hpc_walltime: (walltimeInput ? walltimeInput.value : '04:00:00') || '04:00:00',
                    hpc_partition: (partitionInput ? partitionInput.value : 'standard') || 'standard',
                }
            };
        }
    };

    window.LocalAgentClient = LocalAgentClient;
    document.addEventListener('DOMContentLoaded', () => LocalAgentClient.init());
})(window);
