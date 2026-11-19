"use strict";

const message = document.getElementById("message");
const folder = document.getElementById("folder");
const files = document.getElementById("files");
const scopes = {
  folder: "https://www.googleapis.com/auth/drive.readonly",
  files: "https://www.googleapis.com/auth/drive.file",
};

const scripts = new Map();

function script(url) {
  if (scripts.has(url)) return scripts.get(url);
  const pending = new Promise((resolve, reject) => {
    const node = document.createElement("script");
    node.onload = resolve;
    node.onerror = () => {
      node.remove();
      scripts.delete(url);
      reject(new Error("Google could not be reached. Try again."));
    };
    node.src = url;
    document.head.appendChild(node);
  });
  scripts.set(url, pending);
  return pending;
}

async function pick(config, token) {
  await script("https://apis.google.com/js/api.js");
  await new Promise((resolve, reject) => gapi.load("picker", {
    callback: resolve, onerror: () => reject(new Error("Google Picker could not load.")),
    timeout: 15000, ontimeout: () => reject(new Error("Google Picker timed out.")),
  }));
  return new Promise((resolve, reject) => {
    const picker = google.picker;
    const view = new picker.DocsView().setIncludeFolders(true)
      .setSelectFolderEnabled(false).setMimeTypes("application/pdf,image/png,image/jpeg");
    const dialog = new picker.PickerBuilder().addView(view).setAppId(config.project_number)
      .setDeveloperKey(config.picker_key).setOAuthToken(token).setOrigin(location.origin)
      .enableFeature(picker.Feature.MULTISELECT_ENABLED)
      .setCallback((data) => {
        if (data.action === picker.Action.PICKED) {
          resolve(data.docs.map((doc) => ({id: doc.id, resource_key: doc.resourceKey || null})));
        } else if (data.action === picker.Action.CANCEL) {
          reject(new Error("File selection cancelled."));
        }
      }).build();
    dialog.setVisible(true);
  });
}

async function start() {
  const config = await (await fetch("/api/drive/config")).json();
  if (!config.client_id) throw new Error("Set GOOGLE_OAUTH_CLIENT_ID on the local server to connect private Drive.");
  await script("https://accounts.google.com/gsi/client");
  message.textContent = "";
  files.hidden = !(config.picker_key && config.project_number);
  folder.disabled = false;

  function ready(text) {
    folder.disabled = files.disabled = false;
    message.textContent = text;
  }
  for (const button of [folder, files]) {
    const mode = button.id;
    const client = google.accounts.oauth2.initTokenClient({
      client_id: config.client_id, scope: scopes[mode], include_granted_scopes: false,
      error_callback: () => ready("Sign-in closed. Choose an option to try again."),
      callback: async (auth) => {
        try {
          if (auth.error || !google.accounts.oauth2.hasGrantedAllScopes(auth, scopes[mode])) {
            throw new Error("Drive access was not granted.");
          }
          const expires = Date.now() + Number(auth.expires_in) * 1000;
          const selected = mode === "files" ? await pick(config, auth.access_token) : [];
          message.textContent = "Connecting to Drive…";
          const res = await fetch("/api/drive/session", {
            method: "POST", headers: {"Content-Type": "application/json", Authorization: "Bearer " + auth.access_token},
            body: JSON.stringify({mode, files: selected, expires_in: Math.floor((expires - Date.now()) / 1000)}),
          });
          if (!res.ok) {
            const data = await res.json();
            throw new Error(data.error || (typeof data.detail === "string" && data.detail) || "Could not connect to Drive.");
          }
          location.replace(mode === "files" ? "/#picked" : "/");
        } catch (error) { ready(error.message); }
      },
    });
    button.addEventListener("click", () => {
      folder.disabled = files.disabled = true;
      message.textContent = "Waiting for Google…";
      client.requestAccessToken();
    });
  }
}
start().catch((error) => { message.textContent = error.message; });
