/* Drive to Text: one page, four stages.
   link -> files -> batch -> results. State lives here; the DOM mirrors it. */

const LANGUAGE_NAMES = { eng: "English", fra: "French", ara: "Arabic" };
const METHOD_LABELS = { "pdf-parser": "PDF parser", ocr: "OCR", mixed: "PDF parser + OCR" };
const STAGES = ["link", "files", "batch", "results"];
const TEXT_PREVIEW_LIMIT = 200_000;
// The copy is English, so numbers are too: "3.941 characters" from an ar-TN browser reads as a decimal.
const NUMBER = new Intl.NumberFormat("en");
const formatCount = (value) => NUMBER.format(value);

const state = {
  folder: null,
  files: [],
  selected: new Set(),
  languages: [],
  batch: null,
  jobs: new Map(),
  eventSource: null,
  clockTimer: null,
  startedAt: 0,
  activeFilter: "all",
  searchQuery: "",
};

const $ = (id) => document.getElementById(id);
const el = {
  spineFill: $("spineFill"),
  linkForm: $("linkForm"),
  linkField: $("linkField"),
  linkInput: $("linkInput"),
  linkSubmit: $("linkSubmit"),
  linkError: $("linkError"),
  fetching: $("fetching"),
  fetchReel: $("fetchReel"),
  fetchText: $("fetchText"),
  fetchName: $("fetchName"),
  filesTitle: $("filesTitle"),
  folderSummary: $("folderSummary"),
  folderLink: $("folderLink"),
  selectAll: $("selectAll"),
  selectedCount: $("selectedCount"),
  totalCount: $("totalCount"),
  fileList: $("fileList"),
  filesEmpty: $("filesEmpty"),
  filterEmpty: $("filterEmpty"),
  fileSearch: $("fileSearch"),
  filterChips: $("filterChips"),
  langPicker: $("langPicker"),
  langChips: $("langChips"),
  runButton: $("runButton"),
  runError: $("runError"),
  batchTitle: $("batchTitle"),
  batchSummary: $("batchSummary"),
  batchClock: $("batchClock"),
  cancelBatchButton: $("cancelBatchButton"),
  progress: $("progress"),
  progressFill: $("progressFill"),
  jobList: $("jobList"),
  resultsTitle: $("resultsTitle"),
  resultsSummary: $("resultsSummary"),
  exportLink: $("exportLink"),
  exportTxtLink: $("exportTxtLink"),
  exportZipLink: $("exportZipLink"),
  copyAllButton: $("copyAllButton"),
  toggleAllSheetsButton: $("toggleAllSheetsButton"),
  retryFailedButton: $("retryFailedButton"),
  restartButton: $("restartButton"),
  sheets: $("sheets"),
};
const tpl = {
  file: $("tpl-file"),
  job: $("tpl-job"),
  sheet: $("tpl-sheet"),
};

/* ------------------------------------------------------------------ helpers */

function formatBytes(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function formatDuration(ms) {
  if (ms == null) return "";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function plural(count, singular, pluralForm = `${singular}s`) {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

// "English/archive/scan.pdf" for a file found in a subfolder, the bare name otherwise.
const filePath = (file) => (file.folder ? `${file.folder}/${file.name}` : file.name);

function kindLabel(file) {
  if (file.kind === "pdf") return "PDF";
  return file.mime_type === "image/png" ? "PNG" : "JPEG";
}

function methodLabel(job) {
  if (!job.method) return "";
  const base = METHOD_LABELS[job.method] || job.method;
  return job.ocr_language && job.method !== "pdf-parser" ? `${base} (${job.ocr_language})` : base;
}

function stampClass(method) {
  return { ocr: "stamp-ocr", mixed: "stamp-mixed" }[method] || "stamp-parser";
}

function refusal(response, payload) {
  return (
    payload?.error ||
    (typeof payload?.detail === "string" ? payload.detail : null) ||
    (response.status >= 500
      ? `The server hit an error (HTTP ${response.status}). Try again in a moment; if it keeps failing, the server log has the details.`
      : `The request was refused (HTTP ${response.status}). Reload the page and try again.`)
  );
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch {
    throw new Error("Cannot reach the server. Check that it is still running, then try again.");
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new Error(refusal(response, payload));
  }
  return payload;
}

function setBusy(button, busy) {
  button.classList.toggle("is-busy", busy);
  button.disabled = busy;
}

function showError(target, message) {
  target.textContent = message;
  target.hidden = !message;
}

/* ------------------------------------------------------------------ stages */

function openStage(name, { scroll = true } = {}) {
  const section = document.getElementById(`stage-${name}`);
  if (section.hidden) {
    section.hidden = false;
    section.classList.add("is-open");
  }
  const index = STAGES.indexOf(name);
  document.querySelectorAll(".spine-node").forEach((node, i) => {
    node.classList.toggle("is-done", i < index);
    node.classList.toggle("is-current", i === index);
  });
  el.spineFill.style.transform = `scaleY(${index / (STAGES.length - 1)})`;
  if (scroll) section.scrollIntoView({ block: "start" });
}

/* Move focus to a stage's heading when it opens, so keyboard and screen-reader users land
   on the new content instead of being left on a disabled button or <body>. */
function focusHeading(heading) {
  heading.focus({ preventScroll: true });
}

function closeStagesAfter(name) {
  const index = STAGES.indexOf(name);
  STAGES.slice(index + 1).forEach((stage) => {
    const section = document.getElementById(`stage-${stage}`);
    section.classList.remove("is-open");
    section.hidden = true;
  });
}

/* ------------------------------------------------------------------ capabilities */

async function loadCapabilities() {
  let capabilities;
  try {
    capabilities = await api("/api/capabilities");
  } catch {
    capabilities = null;
  }
  renderLanguageChips(capabilities);
}

/* ------------------------------------------------------------------ stage 1: link */

el.linkForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = el.linkInput.value.trim();
  showError(el.linkError, "");
  el.linkField.classList.remove("is-error");
  if (!url) {
    failLink("Paste a Google Drive folder link to get started.");
    return;
  }
  setBusy(el.linkSubmit, true);
  el.linkField.classList.add("is-busy");
  const run = ++reel.run;
  const show = reelStart(run);
  el.fetching.hidden = false;
  closeStagesAfter("link");
  stopBatchStream();
  try {
    const folder = await scanFolder(url, (folderId, files) => show.add(folderId, files));
    if (run !== reel.run) return;
    state.folder = folder;
    state.files = folder.files;
    state.selected = new Set();
    renderFiles();
  } catch (error) {
    failLink(error.message);
    return;
  } finally {
    show.stop();
    if (run === reel.run) {
      setBusy(el.linkSubmit, false);
      el.linkField.classList.remove("is-busy");
      el.fetching.hidden = true;
    }
  }
  openStage("files");
  focusHeading(el.filesTitle);
});

// List a folder through the streaming endpoint: onFound(folderId, files) runs after each
// folder Drive returns, and the promise resolves with the full listing.
async function scanFolder(url, onFound) {
  let response;
  try {
    response = await fetch("/api/folders/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
  } catch {
    throw new Error("Cannot reach the server. Check that it is still running, then try again.");
  }
  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    throw new Error(refusal(response, payload));
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    let chunk;
    try {
      chunk = await reader.read();
    } catch {
      break;
    }
    buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line);
      if (event.type === "found") onFound(event.folder_id, event.files);
      else if (event.type === "done") return event.folder;
      else if (event.type === "error") throw new Error(event.error);
    }
    if (chunk.done) break;
  }
  throw new Error("The connection dropped before the folder was fully read. Try again.");
}

/* Show thumbnails as they arrive while Drive is scanning. The file list opens when the scan
   completes, regardless of which thumbnails have loaded. */
const REEL_DWELL_MS = 220;
const REEL_POOL = 24; // first pages requested at most, in the order documents are found
const reel = { run: 0 };

function reelStart(run) {
  const blank = reelSlide(null);
  blank.classList.add("is-current", "is-blank");
  blank.style.setProperty("--o", 0);
  el.fetchReel.replaceChildren(blank);
  el.fetchReel.classList.add("is-scanning");
  el.fetchText.textContent = "Reading the folder on Drive";
  el.fetchName.textContent = "";

  const arrived = [];
  let requested = 0;
  let found = 0;
  let lastShown = -Infinity;
  let timer = 0;
  let stopped = false;
  const live = () => !stopped && run === reel.run;

  const schedule = () => {
    if (timer || !arrived.length) return;
    const delay = Math.max(0, lastShown + REEL_DWELL_MS - performance.now());
    timer = setTimeout(() => {
      timer = 0;
      if (!live()) return;
      const { file, src } = arrived.shift();
      lastShown = performance.now();
      reelShow(file, src);
      schedule();
    }, delay);
  };

  return {
    add(folderId, files) {
      if (!live()) return;
      found += files.length;
      if (found) el.fetchText.textContent = `Found ${plural(found, "document")} so far`;
      for (const file of files) {
        if (requested >= REEL_POOL) break;
        requested += 1;
        const img = new Image();
        img.onload = () => {
          if (!live()) return;
          arrived.push({ file, src: img.src });
          schedule();
        };
        img.src = thumbUrl(file, folderId);
      }
    },
    stop() {
      stopped = true;
      clearTimeout(timer);
      arrived.length = 0;
      if (run === reel.run) el.fetchReel.classList.remove("is-scanning");
    },
  };
}

function reelSlide(src) {
  const slide = document.createElement("div");
  slide.className = "fetch-slide";
  if (src) {
    const img = document.createElement("img");
    img.alt = "";
    img.src = src;
    slide.appendChild(img);
  }
  const beam = document.createElement("b");
  beam.className = "fetch-beam";
  slide.appendChild(beam);
  return slide;
}

function reelShow(file, src) {
  const slide = reelSlide(src);
  slide.classList.add("is-incoming");
  el.fetchReel.appendChild(slide);
  void slide.offsetWidth; // start from the right, then move in
  for (const other of [...el.fetchReel.children]) {
    if (other === slide) continue;
    const offset = Number(other.style.getPropertyValue("--o") || 0) - 1;
    other.style.setProperty("--o", offset);
    other.classList.remove("is-current");
    if (other.classList.contains("is-blank")) other.classList.add("is-gone");
    if (offset < -2 || other.classList.contains("is-gone")) setTimeout(() => other.remove(), 170);
  }
  slide.classList.remove("is-incoming");
  slide.classList.add("is-current");
  slide.style.setProperty("--o", 0);
  el.fetchName.textContent = filePath(file);
}

function failLink(message) {
  showError(el.linkError, message);
  el.linkField.classList.remove("is-error");
  void el.linkField.offsetWidth;
  el.linkField.classList.add("is-error");
  el.linkInput.focus();
}

el.linkInput.addEventListener("input", () => {
  if (!el.linkError.hidden) {
    showError(el.linkError, "");
    el.linkField.classList.remove("is-error");
  }
});

/* ------------------------------------------------------------------ stage 2: files */

function renderFiles() {
  const { folder, files } = state;
  const name = folder.folder_name ? `"${folder.folder_name}"` : "This folder";
  const skipped = folder.skipped ? `, ${plural(folder.skipped, "other entry", "other entries")} ignored` : "";
  const hasSizes = files.some((file) => file.size != null);
  const sizeNote = files.length && !hasSizes ? " Sizes need a Drive API key." : "";
  const scanned = folder.folders_scanned > 1 ? ` across ${plural(folder.folders_scanned, "folder")}` : "";
  const leftOut = folder.folders_skipped
    ? ` ${plural(folder.folders_skipped, "subfolder was", "subfolders were")} left out (too deep, over the folder limit, or not readable).`
    : "";
  el.folderSummary.textContent = files.length
    ? `${name} holds ${plural(files.length, "document")}${scanned}${skipped}.${leftOut}${sizeNote}`
    : `${name} has no PDFs or images${scanned}${skipped}.${leftOut}`;
  // Built here from the id (Drive's alphabet only) rather than trusting a URL from the response.
  el.folderLink.href = /^[A-Za-z0-9_-]+$/.test(folder.folder_id)
    ? `https://drive.google.com/drive/folders/${folder.folder_id}`
    : "https://drive.google.com/";
  el.fileList.classList.toggle("no-sizes", !hasSizes);

  el.fileList.replaceChildren();
  files.forEach((file, index) => {
    const node = tpl.file.content.firstElementChild.cloneNode(true);
    node.style.setProperty("--i", index);
    node.dataset.id = file.id;
    const checkbox = node.querySelector(".file-check");
    checkbox.setAttribute("aria-label", `Select ${filePath(file)}`);
    checkbox.addEventListener("change", () => toggleFile(file.id, checkbox.checked));
    node.querySelector(".file-name-text").textContent = file.name;
    loadInto(node.querySelector(".file-thumb img"), thumbUrl(file));
    const where = node.querySelector(".file-folder");
    where.textContent = file.folder ? `${file.folder.replaceAll("/", " / ")} / ` : "";
    where.hidden = !file.folder;
    node.querySelector(".file-kind").textContent = kindLabel(file);
    node.querySelector(".file-size").textContent = formatBytes(file.size);
    el.fileList.appendChild(node);
  });
  el.filesEmpty.hidden = files.length > 0;
  el.fileList.hidden = files.length === 0;
  document.getElementById("runBar").hidden = files.length === 0;
  el.totalCount.textContent = files.length;
  updateSelection();
  // Reset search/filter state whenever a new folder is loaded.
  state.activeFilter = "all";
  state.searchQuery = "";
  if (el.fileSearch) el.fileSearch.value = "";
  applyFileFilter();
}

function toggleFile(id, checked) {
  if (checked) state.selected.add(id);
  else state.selected.delete(id);
  updateSelection();
}

/* File search + filter chips ---------------------------------------- */

function applyFileFilter() {
  const query = (state.searchQuery || "").toLowerCase().trim();
  const filter = state.activeFilter || "all";
  let visibleCount = 0;

  el.fileList.querySelectorAll(".file-row").forEach((row) => {
    const file = state.files.find((f) => f.id === row.dataset.id);
    if (!file) return;
    const matchesKind =
      filter === "all" ||
      (filter === "pdf" && file.kind === "pdf") ||
      (filter === "image" && file.kind !== "pdf");
    const haystack = filePath(file).toLowerCase();
    const matchesSearch = !query || haystack.includes(query);
    const visible = matchesKind && matchesSearch;
    row.hidden = !visible;
    if (visible) visibleCount += 1;
  });

  // Update visible count display.
  const total = state.files.length;
  el.totalCount.textContent = visibleCount < total ? `${visibleCount} of ${total}` : String(total);
  if (el.filterEmpty) {
    el.filterEmpty.hidden = !(state.files.length > 0 && visibleCount === 0);
  }

  // Keep selectAll state in sync with what's visible and selected.
  const visibleIds = [...el.fileList.querySelectorAll(".file-row:not([hidden])")]
    .map((row) => row.dataset.id);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => state.selected.has(id));
  const someVisibleSelected = visibleIds.some((id) => state.selected.has(id));
  el.selectAll.checked = allVisibleSelected;
  el.selectAll.indeterminate = !allVisibleSelected && someVisibleSelected;
}

if (el.fileSearch) {
  el.fileSearch.addEventListener("input", () => {
    state.searchQuery = el.fileSearch.value;
    applyFileFilter();
  });
}

if (el.filterChips) {
  el.filterChips.addEventListener("click", (event) => {
    const chip = event.target.closest(".filter-chip");
    if (!chip) return;
    state.activeFilter = chip.dataset.filter || "all";
    el.filterChips.querySelectorAll(".filter-chip").forEach((c) => {
      c.classList.toggle("is-active", c === chip);
      c.setAttribute("aria-pressed", String(c === chip));
    });
    applyFileFilter();
  });
}

el.selectAll.addEventListener("change", () => {
  // Only operate on the rows currently visible after any search/filter.
  const visibleRows = [...el.fileList.querySelectorAll(".file-row:not([hidden])")];
  const visibleIds = visibleRows.map((row) => row.dataset.id);
  if (el.selectAll.checked) {
    visibleIds.forEach((id) => state.selected.add(id));
  } else {
    visibleIds.forEach((id) => state.selected.delete(id));
  }
  visibleRows.forEach((row) => {
    const checkbox = row.querySelector(".file-check");
    if (checkbox) checkbox.checked = el.selectAll.checked;
  });
  updateSelection();
});

function updateSelection() {
  const count = state.selected.size;
  el.fileList.querySelectorAll(".file-row").forEach((row) => {
    row.classList.toggle("is-selected", state.selected.has(row.dataset.id));
  });
  if (el.selectedCount.textContent !== String(count)) {
    el.selectedCount.textContent = count;
    el.selectedCount.classList.remove("is-ticking");
    void el.selectedCount.offsetWidth;
    el.selectedCount.classList.add("is-ticking");
  }
  // Drive selectAll state from visible rows so it reflects the active filter.
  const visibleIds = [...el.fileList.querySelectorAll(".file-row:not([hidden])")].map(
    (row) => row.dataset.id,
  );
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => state.selected.has(id));
  const someVisibleSelected = visibleIds.some((id) => state.selected.has(id));
  el.selectAll.checked = allVisibleSelected;
  el.selectAll.indeterminate = !allVisibleSelected && someVisibleSelected;
  el.runButton.querySelector(".button-label").textContent =
    count === 0 ? "Extract text" : `Extract ${plural(count, "file")}`;
  el.runButton.disabled = count === 0;
  showError(el.runError, "");
}

function renderLanguageChips(caps) {
  el.langChips.replaceChildren();
  if (!caps || !caps.ocr_available) {
    const note = document.createElement("p");
    note.className = "lang-note";
    note.textContent = caps
      ? "OCR is not installed on the server. Native PDFs still work; images will fail and scanned pages stay empty."
      : "Could not check OCR availability. Reload the page to try again.";
    el.langChips.appendChild(note);
    return;
  }
  const installed = new Set(caps.ocr_languages);
  const defaults = caps.ocr_default_language.split("+").filter((code) => installed.has(code));
  state.languages = defaults.length ? defaults : [caps.ocr_languages[0]];

  const order = [...Object.keys(LANGUAGE_NAMES), ...caps.ocr_languages.filter((c) => !LANGUAGE_NAMES[c])];
  order.forEach((code) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "lang-chip";
    chip.dataset.code = code;
    const available = installed.has(code);
    chip.disabled = !available;
    chip.setAttribute("aria-pressed", String(state.languages.includes(code)));
    const codeTag = document.createElement("small");
    codeTag.textContent = code;
    chip.append(`${LANGUAGE_NAMES[code] || code} `, codeTag);
    if (!available) chip.title = `Language pack "${code}" is not installed for Tesseract.`;
    chip.addEventListener("click", () => toggleLanguage(code, chip));
    el.langChips.appendChild(chip);
  });
  const note = document.createElement("p");
  note.className = "lang-note";
  note.id = "langNote";
  el.langPicker.appendChild(note);
  updateLanguageNote();
}

function toggleLanguage(code, chip) {
  const active = state.languages.includes(code);
  if (active && state.languages.length === 1) return;
  state.languages = active
    ? state.languages.filter((item) => item !== code)
    : [...state.languages, code];
  chip.setAttribute("aria-pressed", String(!active));
  updateLanguageNote();
}

function updateLanguageNote() {
  const note = document.getElementById("langNote");
  if (!note) return;
  note.textContent =
    state.languages.length > 1
      ? `Tesseract will run with "${state.languages.join("+")}". Pick only the languages present in the scans; each extra one slows OCR down.`
      : "Used for images and for PDF pages that have no text layer.";
}

/* ------------------------------------------------------------------ stage 3: batch */

el.runButton.addEventListener("click", async () => {
  if (state.selected.size === 0) return;
  setBusy(el.runButton, true);
  showError(el.runError, "");
  stopBatchStream();
  try {
    const created = await api("/api/batches", {
      method: "POST",
      body: JSON.stringify({
        folder_id: state.folder.folder_id,
        file_ids: state.files.filter((file) => state.selected.has(file.id)).map((file) => file.id),
        ocr_language: state.languages.join("+") || null,
      }),
    });
    startBatch(created.batch_id);
  } catch (error) {
    showError(el.runError, error.message);
  } finally {
    setBusy(el.runButton, false);
  }
});

function startBatch(batchId) {
  state.batch = { batch_id: batchId, status: "running" };
  state.jobs = new Map();
  closeStagesAfter("files");
  el.jobList.replaceChildren();
  resetLive();
  el.jobList.hidden = false;
  el.batchTitle.textContent = "Extracting";
  el.batchSummary.textContent = "Starting the batch";
  el.progress.classList.remove("is-finished", "has-failures");
  setProgress(0);
  if (el.cancelBatchButton) {
    el.cancelBatchButton.hidden = false;
    el.cancelBatchButton.disabled = false;
    el.cancelBatchButton.textContent = "Stop";
  }
  state.files
    .filter((file) => state.selected.has(file.id))
    .forEach((file) => {
      const job = { ...file, status: "pending" };
      state.jobs.set(file.id, job);
      el.jobList.appendChild(renderJob(job));
    });
  openStage("batch");
  focusHeading(el.batchTitle);
  startClock();

  const source = new EventSource(`/api/batches/${batchId}/events`);
  state.eventSource = source;
  source.addEventListener("snapshot", (event) => applySnapshot(JSON.parse(event.data)));
  source.addEventListener("file", (event) => applyJob(JSON.parse(event.data).file));
  source.addEventListener("page", (event) => applyPage(JSON.parse(event.data)));
  source.addEventListener("batch", (event) => finishBatch(JSON.parse(event.data)));
  source.onerror = () => {
    if (state.batch?.status === "running") {
      el.batchSummary.textContent = "Connection lost. Retrying";
    }
  };
}

if (el.cancelBatchButton) {
  el.cancelBatchButton.hidden = true;
  el.cancelBatchButton.addEventListener("click", async () => {
    if (!state.batch?.batch_id || state.batch?.status !== "running") return;
    el.cancelBatchButton.disabled = true;
    el.cancelBatchButton.textContent = "Stopping…";
    try {
      await api(`/api/batches/${state.batch.batch_id}/cancel`, { method: "POST" });
    } catch {
      // Ignore — the SSE stream will reflect the cancellation regardless.
    }
  });
}

function stopBatchStream() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  stopClock();
}

function startClock() {
  stopClock();
  state.startedAt = performance.now();
  el.batchClock.textContent = "0.0 s";
  state.clockTimer = setInterval(() => {
    el.batchClock.textContent = `${((performance.now() - state.startedAt) / 1000).toFixed(1)} s`;
  }, 100);
}

function stopClock() {
  if (state.clockTimer) clearInterval(state.clockTimer);
  state.clockTimer = null;
}

function applySnapshot(snapshot) {
  snapshot.files.forEach(applyJob);
  if (snapshot.status === "finished") finishBatch(snapshot);
}

function renderJob(job) {
  const node = tpl.job.content.firstElementChild.cloneNode(true);
  node.dataset.id = job.id;
  node.dataset.kind = job.kind;
  node.querySelector(".job-name").textContent = filePath(job);
  loadInto(node.querySelector(".job-page-img"), thumbUrl(job));
  updateJobNode(node, job);
  return node;
}

function applyJob(job) {
  const previous = state.jobs.get(job.id) || {};
  const merged = { ...previous, ...job };
  state.jobs.set(job.id, merged);
  const node = el.jobList.querySelector(`[data-id="${CSS.escape(job.id)}"]`);
  if (node) {
    updateJobNode(node, merged, previous.status);
    liveUpdate(node, merged);
  }
  updateBatchSummary();
}

function updateJobNode(node, job, previousStatus) {
  node.dataset.status = job.status;
  const labels = { pending: "Pending", processing: "Processing", done: "Done", failed: "Failed" };
  // The server announces the download finishing (with the first page) before reading starts.
  const phase = job.status !== "processing" ? "" : job.downloaded_bytes == null ? "download" : "read";
  node.dataset.phase = phase;
  if (phase) labels.processing = phase === "download" ? "Downloading" : "Reading";
  node.querySelector(".job-status-text").textContent = labels[job.status] || job.status;

  const pageImg = node.querySelector(".job-page-img");
  if (job.has_preview && pageImg.dataset.source !== "preview") {
    pageImg.dataset.source = "preview";
    loadInto(pageImg, previewUrl(job));
  }

  const stamp = node.querySelector(".job-stamp");
  if (job.method) {
    stamp.textContent = methodLabel(job);
    stamp.className = `job-stamp ${stampClass(job.method)}`;
    if (stamp.hidden && previousStatus !== job.status) stamp.classList.add("is-pressed");
    stamp.hidden = false;
  } else {
    stamp.hidden = true;
  }

  const meta = [];
  if (job.status === "done") {
    if (job.pages?.length) meta.push(plural(job.pages.length, "page"));
    meta.push(`${formatCount(job.char_count)} chars`);
  }
  if (job.duration_ms != null) meta.push(formatDuration(job.duration_ms));
  node.querySelector(".job-meta").textContent = meta.join(", ");

  // The first words actually read from the file, wiped in when it finishes live.
  const snippet = node.querySelector(".job-snippet");
  const preview = job.status === "done" ? snippetOf(job.text) : "";
  if (preview && snippet.hidden) {
    snippet.textContent = preview;
    snippet.classList.toggle("is-revealing", Boolean(previousStatus) && previousStatus !== "done");
    snippet.hidden = false;
  } else if (!preview) {
    snippet.hidden = true;
  }

  const error = node.querySelector(".job-error");
  error.textContent = job.error || "";
  error.hidden = !job.error;
}

/* ------------------------------------------------------------------ real page images */

// Drive's thumbnail of a listed file, fetched and re-encoded by the server.
function thumbUrl(file, folderId = state.folder?.folder_id) {
  const folder = encodeURIComponent(folderId || "");
  return `/api/folders/${folder}/files/${encodeURIComponent(file.id)}/thumbnail`;
}

// The first page as the server rendered it from the downloaded file.
function previewUrl(job) {
  const batch = encodeURIComponent(state.batch?.batch_id || "");
  return `/api/batches/${batch}/files/${encodeURIComponent(job.id)}/preview`;
}

// Load a real page image into its frame; the frame keeps its drawn lines until it arrives.
function loadInto(img, url) {
  const frame = img.parentElement;
  img.onload = () => {
    frame.classList.remove("is-missing");
    frame.classList.add("has-image");
  };
  img.onerror = () => {
    frame.classList.remove("has-image");
    frame.classList.add("is-missing");
  };
  img.src = url;
}

function snippetOf(text) {
  return (text || "").replace(/\s+/g, " ").trim().slice(0, 160);
}

function setProgress(percent) {
  el.progressFill.style.transform = `scaleX(${percent / 100})`;
  el.progress.setAttribute("aria-valuenow", Math.round(percent));
}

function updateBatchSummary() {
  const jobs = [...state.jobs.values()];
  const done = jobs.filter((job) => job.status === "done").length;
  const failed = jobs.filter((job) => job.status === "failed").length;
  const processing = jobs.filter((job) => job.status === "processing").length;
  const finished = done + failed;
  setProgress(jobs.length ? (finished / jobs.length) * 100 : 0);
  const parts = [`${finished} of ${jobs.length} finished`];
  if (processing) parts.push(`${processing} in progress`);
  if (failed) parts.push(`${failed} failed`);
  el.batchSummary.textContent = parts.join(", ");
}

function finishBatch(batch) {
  if (state.batch?.status === "finished") return;
  state.batch = { ...state.batch, ...batch, status: "finished" };
  stopBatchStream();
  if (el.cancelBatchButton) el.cancelBatchButton.hidden = true;
  const { done, failed, total } = batch.summary;
  const wasCancelled = [...state.jobs.values()].some(
    (job) => job.error === "Extraction was cancelled."
  );
  if (wasCancelled) {
    el.batchTitle.textContent = done === 0 ? "Cancelled" : "Cancelled (partial results)";
    el.batchSummary.textContent = `${done} of ${total} extracted before cancellation.`;
  } else {
    el.batchTitle.textContent =
      failed === 0 ? "All done" : done === 0 ? "Nothing extracted" : "Finished with errors";
    el.batchSummary.textContent =
      failed === 0
        ? `${plural(done, "file")} extracted in ${el.batchClock.textContent}.`
        : `${done} of ${total} extracted, ${plural(failed, "file")} failed. The reasons are in the results.`;
  }
  setProgress(100);
  el.progress.classList.add("is-finished");
  el.progress.classList.toggle("has-failures", failed > 0);
  resetLive();
  el.jobList.hidden = true;
  renderResults(batch);
  openStage("results");
  focusHeading(el.resultsTitle);
}

/* ------------------------------------------------------------------ stage 4: results */

function renderResults(batch) {
  const jobs = [...state.jobs.values()];
  const done = jobs.filter((job) => job.status === "done");
  const chars = done.reduce((sum, job) => sum + (job.char_count || 0), 0);
  const methods = new Map();
  done.forEach((job) => methods.set(job.method, (methods.get(job.method) || 0) + 1));
  const methodText = [...methods.entries()]
    .map(([method, count]) => `${count} by ${METHOD_LABELS[method] || method}`)
    .join(", ");
  el.resultsSummary.textContent = done.length
    ? `${formatCount(chars)} characters from ${plural(done.length, "file")} (${methodText}).`
    : "No text could be extracted from the selected files.";
  el.exportLink.href = `/api/batches/${batch.batch_id}/export`;
  el.exportLink.download = `drive-to-text-${batch.batch_id}.json`;

  if (el.exportTxtLink) {
    el.exportTxtLink.href = `/api/batches/${batch.batch_id}/export?format=txt`;
    el.exportTxtLink.download = `drive-to-text-${batch.batch_id}.txt`;
  }
  if (el.exportZipLink) {
    el.exportZipLink.href = `/api/batches/${batch.batch_id}/export?format=zip`;
    el.exportZipLink.download = `drive-to-text-${batch.batch_id}.zip`;
  }

  // Show "Retry failed" only when there are failed jobs.
  const failedJobs = jobs.filter((job) => job.status === "failed");
  if (el.retryFailedButton) el.retryFailedButton.hidden = failedJobs.length === 0;

  // Disable "Copy all" if no done files have non-empty text.
  const hasText = done.some((job) => job.text && job.text.trim());
  if (el.copyAllButton) el.copyAllButton.disabled = !hasText;

  el.sheets.replaceChildren();
  jobs.forEach((job) => el.sheets.appendChild(buildSheet(job)));
  const first = el.sheets.querySelector(".sheet:not(.is-failed)");
  if (first) toggleSheet(first, true);
  updateToggleAllButton();
}

function buildSheet(job) {
  const sheet = tpl.sheet.content.firstElementChild.cloneNode(true);
  sheet.dataset.id = job.id;
  sheet.classList.toggle("is-failed", job.status === "failed");
  sheet.querySelector(".sheet-name").textContent = filePath(job);

  const stamp = sheet.querySelector(".sheet-stamp");
  if (job.method) {
    stamp.textContent = methodLabel(job);
    stamp.classList.add(stampClass(job.method));
  } else {
    stamp.remove();
  }
  const pages = sheet.querySelector(".sheet-pages");
  const chars = sheet.querySelector(".sheet-chars");
  const time = sheet.querySelector(".sheet-time");
  if (job.status === "done") {
    const ocrPages = (job.pages || []).filter((page) => page.method === "ocr").length;
    pages.textContent =
      job.kind === "pdf"
        ? `${plural(job.pages.length, "page")}${ocrPages && ocrPages < job.pages.length ? `, ${ocrPages} via OCR` : ""}`
        : kindLabel(job);
    chars.textContent = `${formatCount(job.char_count)} characters`;
  } else {
    pages.textContent = kindLabel(job);
    chars.textContent = "failed";
  }
  time.textContent = formatDuration(job.duration_ms);

  const warning = sheet.querySelector(".sheet-warning");
  const notes = job.status === "failed" ? [job.error] : job.warnings || [];
  if (notes.length) {
    warning.textContent = notes.join(" ");
    warning.hidden = false;
  }

  const text = sheet.querySelector(".sheet-text");
  const empty = sheet.querySelector(".sheet-empty");
  const copy = sheet.querySelector(".sheet-copy");
  if (job.status === "done" && job.text) {
    const clipped = job.text.length > TEXT_PREVIEW_LIMIT;
    text.textContent = clipped ? job.text.slice(0, TEXT_PREVIEW_LIMIT) : job.text;
    if (/[؀-ۿ]/.test(job.text)) text.lang = "ar";
    if (clipped) {
      const note = document.createElement("p");
      note.className = "sheet-clip";
      note.textContent = `Showing the first ${formatCount(TEXT_PREVIEW_LIMIT)} of ${formatCount(job.text.length)} characters. The JSON export holds the full text.`;
      text.after(note);
    }
  } else {
    text.remove();
    empty.hidden = false;
    if (job.status === "failed") empty.textContent = "This file could not be processed.";
    copy.remove();
  }

  // The whole card opens it, not just the title. Buttons keep their own job, and once the
  // card is open its text stays selectable: clicks there, or after selecting, do nothing.
  sheet.addEventListener("click", (event) => {
    if (event.target.closest("button:not(.sheet-toggle), a, input, textarea")) return;
    if (sheet.classList.contains("is-open") && event.target.closest(".sheet-body")) return;
    if (String(window.getSelection?.() || "")) return;
    toggleSheet(sheet);
  });
  copy?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(job.text);
      copy.textContent = "Copied";
      copy.classList.add("is-copied");
      setTimeout(() => {
        copy.textContent = "Copy text";
        copy.classList.remove("is-copied");
      }, 1600);
    } catch {
      copy.textContent = "Copy failed";
    }
  });
  return sheet;
}

function toggleSheet(sheet, force) {
  const open = force ?? !sheet.classList.contains("is-open");
  sheet.classList.toggle("is-open", open);
  sheet.querySelector(".sheet-toggle").setAttribute("aria-expanded", String(open));
  updateToggleAllButton();
}

function updateToggleAllButton() {
  if (!el.toggleAllSheetsButton) return;
  const sheets = [...el.sheets.querySelectorAll(".sheet")];
  if (!sheets.length) {
    el.toggleAllSheetsButton.disabled = true;
    return;
  }
  el.toggleAllSheetsButton.disabled = false;
  const allOpen = sheets.every((s) => s.classList.contains("is-open"));
  el.toggleAllSheetsButton.textContent = allOpen ? "Collapse all" : "Expand all";
}

el.restartButton.addEventListener("click", () => {
  stopBatchStream();
  state.batch = null;
  state.jobs = new Map();
  state.selected = new Set();
  closeStagesAfter("link");
  el.linkInput.value = "";
  openStage("link");
  el.linkInput.focus({ preventScroll: true });
});

/* Results action buttons -------------------------------------------- */

if (el.copyAllButton) {
  el.copyAllButton.addEventListener("click", async () => {
    const texts = [...state.jobs.values()]
      .filter((job) => job.status === "done" && job.text)
      .map((job) => `### ${filePath(job)}\n\n${job.text}`)
      .join("\n\n---\n\n");
    if (!texts) return;
    try {
      await navigator.clipboard.writeText(texts);
      el.copyAllButton.textContent = "Copied!";
      setTimeout(() => { el.copyAllButton.textContent = "Copy all"; }, 1800);
    } catch {
      el.copyAllButton.textContent = "Copy failed";
      setTimeout(() => { el.copyAllButton.textContent = "Copy all"; }, 2000);
    }
  });
}

if (el.toggleAllSheetsButton) {
  el.toggleAllSheetsButton.addEventListener("click", () => {
    const sheets = [...el.sheets.querySelectorAll(".sheet")];
    if (!sheets.length) return;
    const allOpen = sheets.every((s) => s.classList.contains("is-open"));
    sheets.forEach((sheet) => toggleSheet(sheet, !allOpen));
  });
}

if (el.retryFailedButton) {
  el.retryFailedButton.addEventListener("click", () => {
    // Pre-select only the failed files and go back to stage 2 to re-run.
    const failedIds = [...state.jobs.values()]
      .filter((job) => job.status === "failed")
      .map((job) => job.id);
    if (!failedIds.length) return;
    state.selected = new Set(failedIds);
    // Update checkboxes to match.
    el.fileList.querySelectorAll(".file-row").forEach((row) => {
      const checkbox = row.querySelector(".file-check");
      if (checkbox) checkbox.checked = state.selected.has(row.dataset.id);
    });
    updateSelection();
    closeStagesAfter("files");
    openStage("files", { scroll: false });
    focusHeading(el.filesTitle);
    el.runButton.scrollIntoView({ block: "nearest" });
  });
}

/* ------------------------------------------------------------------ live reading */
/* While a file is processed, show its latest page and text. Coalesce fast page updates
   so a native PDF can't flash through every page at once. */

const FRAME_MIN_MS = 140;
const live = new Map();

function resetLive() {
  live.forEach((view) => clearTimeout(view.timer));
  live.clear();
}

function rowFor(id) {
  return el.jobList.querySelector(`[data-id="${CSS.escape(id)}"]`);
}

function liveView(id) {
  if (!live.has(id)) {
    live.set(id, {
      shownAt: -Infinity,
      page: 0,
      pending: null,
      timer: null,
      imgSeq: 0,
      imgShown: 0,
    });
  }
  return live.get(id);
}

function liveUpdate(node, job) {
  if (job.status === "processing") {
    const view = liveView(job.id);
    if (!node.classList.contains("is-live")) {
      node.classList.add("is-live");
      setLiveImage(node, view, thumbUrl(job));
    }
    if (!view.page) {
      const where = node.querySelector(".live-where");
      if (job.downloaded_bytes == null) {
        where.textContent = "Downloading from Drive";
      } else {
        where.textContent = `Downloaded ${formatBytes(job.downloaded_bytes)}, opening it`;
        if (job.has_preview) setLiveImage(node, view, previewUrl(job));
      }
      if (job.progress) queueFrame(job.id, { ...job.progress, has_image: false, excerpt: "" });
    }
  } else if (live.has(job.id) && (job.status === "done" || job.status === "failed")) {
    clearTimeout(live.get(job.id).timer);
    live.delete(job.id);
    node.classList.remove("is-live");
  }
}

function applyPage(event) {
  const job = state.jobs.get(event.file_id);
  if (!job || job.status !== "processing") return; // a late page after the file finished
  job.progress = event;
  queueFrame(event.file_id, event);
}

function queueFrame(id, frame) {
  const view = liveView(id);
  view.pending = frame; // only the newest frame waits; the ones it replaces were never due
  tick(id);
}

// Show the latest waiting frame once the current one has had time to render.
function tick(id) {
  const view = live.get(id);
  if (!view?.pending) return;
  clearTimeout(view.timer);
  view.timer = null;
  const node = rowFor(id);
  if (!node) return;
  const delay = view.shownAt + FRAME_MIN_MS - performance.now();
  if (delay > 0) {
    view.timer = setTimeout(() => tick(id), delay);
    return;
  }
  showFrame(node, view, view.pending);
  view.pending = null;
}

function showFrame(node, view, frame) {
  view.shownAt = performance.now();
  const box = node.querySelector(".job-live");
  if (frame.has_image && frame.page !== view.page) setLiveImage(node, view, pageUrl(node.dataset.id, frame.page));
  view.page = frame.page;
  box.dataset.state = frame.state;
  node.querySelector(".live-where").textContent = `Page ${formatCount(frame.page)} of ${formatCount(frame.total)}`;
  node.querySelector(".live-method").textContent =
    frame.method === "ocr" ? (frame.state === "reading" ? "OCR, reading the scan" : "OCR") : "Text layer";
  node.querySelector(".live-bar i").style.transform = `scaleX(${Math.min(1, frame.page / Math.max(frame.total, 1))})`;
  const text = node.querySelector(".live-text");
  if (frame.state === "reading") {
    text.textContent = "";
    return;
  }
  text.textContent = frame.excerpt || "No text on this page.";
  text.classList.remove("is-new");
  void text.offsetWidth; // restart the wipe for every new page
  text.classList.add("is-new");
}

// Lay a new page on top once it has loaded; a slower, older page never covers a newer one.
function setLiveImage(node, view, url) {
  const frame = node.querySelector(".live-page");
  const seq = ++view.imgSeq;
  const img = document.createElement("img");
  img.className = "live-img";
  img.alt = "";
  img.decoding = "async";
  img.onload = () => {
    if (seq < view.imgShown || live.get(node.dataset.id) !== view) return;
    view.imgShown = seq;
    const pages = frame.querySelectorAll(".live-img");
    for (let i = 0; i < pages.length - 1; i += 1) pages[i].remove(); // keep one beneath
    frame.insertBefore(img, frame.querySelector(".live-beam"));
    frame.classList.add("has-image");
  };
  img.src = url;
}

function pageUrl(id, page) {
  const batch = encodeURIComponent(state.batch?.batch_id || "");
  return `/api/batches/${batch}/files/${encodeURIComponent(id)}/pages/${page}/preview`;
}

/* ------------------------------------------------------------------ spine follows scroll */

function trackScroll() {
  if (!("IntersectionObserver" in window)) return;
  const observer = new IntersectionObserver(
    (entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting);
      if (!visible.length) return;
      const top = visible.sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      const name = top.target.dataset.stage;
      document.querySelectorAll(".spine-node").forEach((node) => {
        node.classList.toggle("is-current", node.dataset.node === name);
      });
    },
    { rootMargin: "-20% 0px -60% 0px" }
  );
  document.querySelectorAll(".stage").forEach((stage) => observer.observe(stage));
}

/* ------------------------------------------------------------------ boot */

loadCapabilities();
trackScroll();
el.linkInput.focus({ preventScroll: true });
