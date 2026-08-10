(() => {
  "use strict";

  // This small UI layer makes the existing "Remember me" behavior explicit.
  // app.js remains the source of truth for authentication and automatic sign-in.
  const USER_KEY = "chemlab_kaggle_username";
  const KEY_KEY = "chemlab_kaggle_key";

  const username = document.getElementById("kaggle-username");
  const key = document.getElementById("kaggle-key");
  const remember = document.getElementById("kaggle-remember");
  const form = document.getElementById("kaggle-login-form");
  if (!username || !key || !remember || !form) return;

  const row = document.createElement("div");
  row.className = "form-row kaggle-credential-tools";
  row.innerHTML = `
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
      <button type="button" id="kaggle-forget-saved" class="btn btn-ghost btn-small">
        Forget saved Kaggle credentials
      </button>
      <span id="kaggle-saved-status" class="field-hint" aria-live="polite"></span>
    </div>
    <span class="field-hint">
      When “Remember me” is enabled, the username and API key/token stay only in this browser's local storage.
      They are not uploaded as a saved file. Do not enable this on a shared/public computer.
    </span>`;
  form.appendChild(row);

  const status = document.getElementById("kaggle-saved-status");
  const forget = document.getElementById("kaggle-forget-saved");

  function hasSavedCredentials() {
    return Boolean(localStorage.getItem(USER_KEY) && localStorage.getItem(KEY_KEY));
  }

  function render() {
    const saved = hasSavedCredentials();
    remember.checked = saved || remember.checked;
    status.textContent = saved
      ? `Saved on this browser for ${localStorage.getItem(USER_KEY)}.`
      : "No Kaggle credentials are saved on this browser.";
    forget.disabled = !saved;
  }

  forget.addEventListener("click", () => {
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem(KEY_KEY);
    username.value = "";
    key.value = "";
    remember.checked = false;

    // Reuse the application's normal sign-out path when it exists so that
    // current session state and local job credential remnants are cleared too.
    document.getElementById("kaggle-signout-btn")?.click();
    render();

    const stack = document.getElementById("toast-stack");
    if (stack) {
      const toast = document.createElement("div");
      toast.className = "toast";
      toast.textContent = "Saved Kaggle credentials were removed from this browser.";
      stack.appendChild(toast);
      setTimeout(() => toast.remove(), 6000);
    }
  });

  // Keep the status accurate after the main application's successful login
  // handler writes/removes the saved values.
  form.addEventListener("submit", () => {
    setTimeout(render, 0);
  });

  render();
})();
