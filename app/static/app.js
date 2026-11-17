/* Drive to Text: one page, four stages.
   link -> files -> batch -> results. State lives here; the DOM mirrors it. */

const LANGUAGE_NAMES = { eng: "English", fra: "French", ara: "Arabic" };
const METHOD_LABELS = { "pdf-parser": "PDF parser", ocr: "OCR", mixed: "PDF parser + OCR" };
const STAGES = ["link", "files", "batch", "results"];
const TEXT_PREVIEW_LIMIT = 200_000;
const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
// The copy is English, so numbers are too: "3.941 characters" from an ar-TN browser reads as a decimal.
const NUMBER = new Intl.NumberFormat("en");
const formatCount = (value) => NUMBER.format(value);

const state = {
  capabilities: null,
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
  splash: $("splash"),
  capabilities: $("capabilities"),
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
  jobFold: $("jobFold"),
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
    void section.offsetHeight; // commit the closed state so the opening transition runs
    section.classList.add("is-open");
  }
  const index = STAGES.indexOf(name);
  document.querySelectorAll(".spine-node").forEach((node, i) => {
    node.classList.toggle("is-done", i < index);
    node.classList.toggle("is-current", i === index);
  });
  el.spineFill.style.height = `${(index / (STAGES.length - 1)) * 100}%`;
  if (scroll) {
    setTimeout(() => {
      section.scrollIntoView({ behavior: REDUCED_MOTION ? "auto" : "smooth", block: "start" });
    }, 80);
  }
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

/* ------------------------------------------------------------------ splash */

function dismissSplash() {
  if (REDUCED_MOTION) {
    el.splash.remove();
    return;
  }
  setTimeout(() => {
    el.splash.classList.add("is-leaving");
    el.splash.addEventListener("transitionend", () => el.splash.remove(), { once: true });
  }, 1500);
}

/* ------------------------------------------------------------------ capabilities */

async function loadCapabilities() {
  const driveChip = el.capabilities.querySelector('[data-meta="drive"]');
  const ocrChip = el.capabilities.querySelector('[data-meta="ocr"]');
  try {
    state.capabilities = await api("/api/capabilities");
  } catch (error) {
    driveChip.textContent = "Server unreachable";
    driveChip.classList.add("is-warn");
    ocrChip.hidden = true;
    return;
  }
  const caps = state.capabilities;
  if (caps.drive_mode === "drive-api") {
    driveChip.textContent = "Drive API key";
    driveChip.classList.add("is-good");
  } else {
    driveChip.textContent = "Drive: public page mode";
    driveChip.classList.add("is-warn");
    driveChip.title = "No GOOGLE_API_KEY set. Listing works for public folders but file sizes are unknown.";
  }
  if (caps.ocr_available) {
    ocrChip.textContent = `OCR: ${caps.ocr_languages.map((code) => LANGUAGE_NAMES[code] || code).join(", ")}`;
    ocrChip.classList.add("is-good");
  } else {
    ocrChip.textContent = "OCR unavailable";
    ocrChip.classList.add("is-warn");
    ocrChip.title = "Tesseract was not found. Images and scanned PDFs cannot be processed.";
  }
  renderLanguageChips();
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
    await show.done(folder.files.length);
    if (run !== reel.run) return;
    renderFiles();
    openStage("files");
    focusHeading(el.filesTitle);
  } catch (error) {
    show.stop();
    failLink(error.message);
  } finally {
    if (run === reel.run) {
      setBusy(el.linkSubmit, false);
      el.linkField.classList.remove("is-busy");
      el.fetching.hidden = true;
    }
  }
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

/* The fetching card, an automatic slider of what the scan finds. While the first folder is
   read it sweeps a blank page; then each document's first page is requested from Drive as soon
   as the document is found, and slides in the moment it arrives, so the order on screen is the
   order things were fetched. Once the scan is done the list opens after a few more pages. */
const REEL_DWELL_MS = 650; // each page stays in front at least this long
const REEL_LIMIT = 8; // once the scan is done, the list opens after this many pages in all
const REEL_TAIL_MS = 4000; // ... or this long after the scan finished, whichever comes first
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
  let settled = 0;
  let found = 0;
  let shown = 0;
  let lastShown = -Infinity;
  let finishedAt = null;
  let finish = null;
  let timer = 0;
  const live = () => run === reel.run;

  const step = () => {
    if (!live()) return finish?.();
    const now = performance.now();
    if (arrived.length && now - lastShown >= REEL_DWELL_MS) {
      const { file, src } = arrived.shift();
      shown += 1;
      lastShown = now;
      reelShow(file, src);
    }
    if (finishedAt !== null) {
      const nothingLeft = settled === requested && !arrived.length;
      if (shown >= REEL_LIMIT || nothingLeft || now - finishedAt >= REEL_TAIL_MS) {
        // Let the page in front be seen before the list replaces it.
        timer = setTimeout(finish, shown ? Math.max(0, lastShown + REEL_DWELL_MS - now) : 0);
        return;
      }
    }
    timer = setTimeout(step, 50);
  };
  step();

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
          settled += 1;
          if (live()) arrived.push({ file, src: img.src });
        };
        img.onerror = () => {
          settled += 1;
        };
        img.src = thumbUrl(file, folderId);
      }
    },
    done(total) {
      el.fetchReel.classList.remove("is-scanning");
      el.fetchText.textContent = total
        ? `Found ${plural(total, "document")}`
        : "No documents in this folder";
      finishedAt = performance.now();
      return new Promise((resolve) => {
        finish = resolve;
      });
    },
    stop() {
      clearTimeout(timer);
      el.fetchReel.classList.remove("is-scanning");
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
    if (offset < -2 || other.classList.contains("is-gone")) setTimeout(() => other.remove(), 600);
  }
  slide.classList.remove("is-incoming");
  slide.classList.add("is-current");
  slide.style.setProperty("--o", 0);
  // Name the page once it is mostly in front, not while the previous one still is.
  setTimeout(() => {
    if (slide.classList.contains("is-current")) el.fetchName.textContent = filePath(file);
  }, REDUCED_MOTION ? 0 : 140);
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
  state.selected = el.selectAll.checked ? new Set(state.files.map((file) => file.id)) : new Set();
  el.fileList.querySelectorAll(".file-check").forEach((checkbox) => {
    checkbox.checked = el.selectAll.checked;
  });
  updateSelection();
});

function updateSelection() {
  const total = state.files.length;
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
  el.selectAll.checked = total > 0 && count === total;
  el.selectAll.indeterminate = count > 0 && count < total;
  el.runButton.querySelector(".button-label").textContent =
    count === 0 ? "Extract text" : `Extract ${plural(count, "file")}`;
  el.runButton.disabled = count === 0;
  showError(el.runError, "");
}

function renderLanguageChips() {
  const caps = state.capabilities;
  el.langChips.replaceChildren();
  if (!caps || !caps.ocr_available) {
    const note = document.createElement("p");
    note.className = "lang-note";
    note.textContent = "OCR is not installed on the server. Native PDFs still work; images will fail and scanned pages stay empty.";
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
  el.jobFold.classList.remove("is-folded");
  el.batchTitle.textContent = "Extracting";
  el.batchSummary.textContent = "Starting the batch";
  el.progress.classList.remove("is-finished", "has-failures");
  setProgress(0);
  if (el.cancelBatchButton) el.cancelBatchButton.hidden = false;
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
    try {
      await api(`/api/batches/${state.batch.batch_id}/cancel`, { method: "POST" });
    } catch {
      // Ignore — the SSE stream will reflect the cancellation regardless.
    } finally {
      el.cancelBatchButton.disabled = false;
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
  el.progressFill.style.width = `${percent}%`;
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
  el.batchTitle.textContent = failed === 0 ? "All done" : done === 0 ? "Nothing extracted" : "Finished with errors";
  el.batchSummary.textContent =
    failed === 0
      ? `${plural(done, "file")} extracted in ${el.batchClock.textContent}.`
      : `${done} of ${total} extracted, ${plural(failed, "file")} failed. The reasons are in the results.`;
  el.progress.classList.add("is-finished");
  el.progress.classList.toggle("has-failures", failed > 0);
  renderResults(batch);
  // Let the last files finish on screen, fold the list away, and put the results in its place.
  afterLive(() => {
    el.jobFold.classList.add("is-folded");
    setTimeout(() => {
      openStage("results");
      focusHeading(el.resultsTitle);
    }, REDUCED_MOTION ? 0 : LIVE_FOLD_MS);
  });
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

  // Reset toggle-all label.
  if (el.toggleAllSheetsButton) el.toggleAllSheetsButton.textContent = "Expand all";

  el.sheets.replaceChildren();
  jobs.forEach((job, index) => {
    const sheet = buildSheet(job);
    sheet.style.transitionDelay = `${Math.min(index, 8) * 60}ms`;
    el.sheets.appendChild(sheet);
  });
  const first = el.sheets.querySelector(".sheet:not(.is-failed)");
  if (first) toggleSheet(first, true);
  observeSheets();
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
}

function observeSheets() {
  const sheets = el.sheets.querySelectorAll(".sheet");
  if (REDUCED_MOTION || !("IntersectionObserver" in window)) {
    sheets.forEach((sheet) => sheet.classList.add("is-visible"));
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    },
    { rootMargin: "0px 0px -8% 0px", threshold: 0.05 }
  );
  sheets.forEach((sheet) => observer.observe(sheet));
}

el.restartButton.addEventListener("click", () => {
  stopBatchStream();
  state.batch = null;
  state.jobs = new Map();
  state.selected = new Set();
  closeStagesAfter("link");
  el.linkInput.value = "";
  openStage("link");
  setTimeout(() => el.linkInput.focus(), 400);
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
    const anyOpen = !!el.sheets.querySelector(".sheet.is-open");
    el.sheets.querySelectorAll(".sheet").forEach((sheet) => toggleSheet(sheet, !anyOpen));
    el.toggleAllSheetsButton.textContent = anyOpen ? "Expand all" : "Collapse all";
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
    openStage("files");
    setTimeout(() => el.runButton.scrollIntoView({ behavior: "smooth", block: "nearest" }), 200);
  });
}

/* ------------------------------------------------------------------ live reading */
/* While a file is processed its row opens onto what the server is reading right now: the
   real page as rendered from the downloaded file, "Page 37 of 1,015", and the text that came
   off that page. Frames come from `page` events. Each stays up at least FRAME_MIN_MS so fast
   text-layer PDFs stay legible, and a row stays open at least LIVE_MIN_MS before it folds
   back once its file is finished. */

const FRAME_MIN_MS = 180;
const LIVE_MIN_MS = 900;
const LIVE_FOLD_MS = 520; // --t-slow, the fold transition
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
      openedAt: performance.now(),
      shownAt: 0,
      page: 0,
      pending: null,
      timer: null,
      finishing: false,
      closed: false,
      imgSeq: 0,
      imgShown: 0,
    });
  }
  return live.get(id);
}

function liveUpdate(node, job) {
  if (job.status === "processing") {
    const view = liveView(job.id);
    if (view.closed) return;
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
    const view = live.get(job.id);
    view.finishing = true;
    tick(job.id);
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

// Show the waiting frame when the current one has had its moment; fold the row when done.
function tick(id) {
  const view = live.get(id);
  if (!view || view.closed) return;
  clearTimeout(view.timer);
  view.timer = null;
  const node = rowFor(id);
  if (!node) return;
  let now = performance.now();
  if (view.pending && now >= view.shownAt + FRAME_MIN_MS) {
    showFrame(node, view, view.pending);
    view.pending = null;
    now = performance.now();
  }
  let next = view.pending ? view.shownAt + FRAME_MIN_MS : 0;
  if (!view.pending && view.finishing) {
    const ready = Math.max(view.openedAt + LIVE_MIN_MS, view.shownAt + FRAME_MIN_MS);
    if (now >= ready) {
      view.closed = true;
      node.classList.remove("is-live");
      return;
    }
    next = ready;
  }
  if (next) view.timer = setTimeout(() => tick(id), Math.max(0, next - now));
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
    if (seq < view.imgShown || !frame.isConnected) return;
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

// Run once every open row has folded back (or after a few seconds, whatever happens).
function afterLive(callback, batchId = state.batch?.batch_id, deadline = performance.now() + 5000) {
  if (state.batch?.batch_id !== batchId) return; // a new batch started meanwhile
  if ([...live.values()].every((view) => view.closed) || performance.now() > deadline) callback();
  else setTimeout(() => afterLive(callback, batchId, deadline), 120);
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

dismissSplash();
loadCapabilities();
trackScroll();
if (!REDUCED_MOTION) setTimeout(() => el.linkInput.focus({ preventScroll: true }), 1700);
else el.linkInput.focus({ preventScroll: true });
