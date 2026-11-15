"use strict";

const SCHEDULES = [[0, "Off"], [15, "Every 15 minutes"], [60, "Every hour"], [360, "Every 6 hours"], [1440, "Every day"]];
const EVERY = { 15: "every 15 minutes", 60: "every hour", 360: "every 6 hours", 1440: "once a day" };
const RUNNING = ["queued", "uploading", "fixing", "albums"];
const view = document.getElementById("view");
let state = null;
let identity = null;
let connection = null; // whether the saved connection works: { connected, name, version, error }
let connectionStale = false; // the connection page checks it again each time it opens
// The dialog that walks through connecting when Immich can't be reached on load.
const wizard = { step: "address", url: "", key: "", error: "", busy: false, version: null, result: null };
const wizardDialog = document.getElementById("wizard");
let connectError = "";
let formError = "";
let saveError = "";
let notice = "";
let freed = null; // what Delete temporary files just removed
let leftoverError = "";
let draftUrl = null;
let draftKey = "";
let draft = null; // unsaved choices on the current page
let preview = null; // { page, result }
let previewLoading = false;
let folders = null;
let folderError = "";
let foldersLoading = false;
// Photos and videos in each folder of the picker, by path. A count can take a few seconds.
const folderCounts = new Map();
let countRound = 0;
let mosaicText = "";
let lastPage = "";
let lastTick = 0;
let timeZones = null; // the names Settings offers, loaded when it opens
let timeZonesLoading = false;
let zoneNotice = "";
let zoneError = "";
const zoneSupport = {};
// The log on a library's page: the lines so far, and how the reader left the box.
let logLines = [];
let logLast = 0;
let logOpen = null; // null until the reader opens or closes it; it starts open while something runs
let logScroll = { top: 0, pinned: true };
const LOG_SHOWN = 300;
// Recent readings of how much a sync has sent, to work out its speed.
const pace = { job: null, samples: [] };
const reports = {};
const missing = {};
const found = {}; // what each library's check found, from /findings
// The page for setting dates and locations keeps these across refreshes.
const fix = { library: null, filter: "location", open: new Set(), selected: new Set(), places: null,
  place: null, draft: { name: "", coords: "", date: "" }, error: "", notice: "", busy: false };

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const count = (value) => Number(value || 0).toLocaleString();
const plural = (value, one, many) => `${count(value)} ${value === 1 ? one : many || one + "s"}`;
const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
// The time zone chosen in Settings, unless this browser doesn't know it.
function zone() {
  const name = state?.timeZone;
  if (!name) return {};
  if (!(name in zoneSupport)) {
    try {
      new Intl.DateTimeFormat([], { timeZone: name });
      zoneSupport[name] = true;
    } catch {
      zoneSupport[name] = false;
    }
  }
  return zoneSupport[name] ? { timeZone: name } : {};
}
const when = (value) => new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short", ...zone() });
const clock = (value) => new Date(value).toLocaleTimeString([], { timeStyle: "short", ...zone() });
const clockSeconds = (value) => new Date(value).toLocaleTimeString([], { hour12: false, ...zone() });
const isRunning = (job) => !!job && RUNNING.includes(job.phase);

function timeLeft(seconds) {
  if (seconds < 90) return "about a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `about ${minutes} minutes`;
  const hours = Math.round(minutes / 60);
  return hours < 36 ? `about ${hours} hours` : `about ${Math.round(hours / 24)} days`;
}

// A progress bar. Without a total, it shows that work is going on, but not how far along it is.
function bar(done, total, label) {
  if (!total) return `<div class="bar unknown" role="progressbar" aria-label="${escapeHtml(label)}"><i></i></div>`;
  const percent = Math.max(0, Math.min(100, (done / total) * 100));
  return `<div class="bar" role="progressbar" aria-label="${escapeHtml(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(percent)}"><i style="width:${percent.toFixed(1)}%"></i></div>`;
}

function bytes(value) {
  if (!value) return "0 GB";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const power = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
  return `${(value / 1024 ** power).toFixed(power > 2 ? 1 : 0)} ${units[power]}`;
}

async function api(path, body) {
  const init = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const response = await fetch(path, init);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(typeof detail.detail === "string" ? detail.detail : `Request failed (${response.status}).`);
  }
  return response.headers.get("content-type")?.includes("json") ? response.json() : response.text();
}

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// A library counts as set up once anything from it has reached Immich.
const isSetUp = (library) => !!(library.lastSync || library.counts.done);
const setupDone = () => state.libraries.some(isSetUp);
// The top of the shared folder has no path of its own, and the folder browser calls it NAS.
const shownPath = (library) => library.path || "NAS";

function page() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "connect") return ["connection"];
  if (!parts.length) return state.libraries.length ? ["libraries"] : ["add"];
  return parts;
}

function currentLibrary() {
  const [section, id] = page();
  return ["add", "library"].includes(section) ? state.libraries.find((l) => l.id === id) : undefined;
}

function drawMosaic() {
  const canvas = document.getElementById("mosaic");
  const legend = document.getElementById("legend");
  const total = mosaicText.length;
  if (!canvas || !total) return;

  const colors = {
    w: css("--unexposed"), d: css("--exposed"), s: css("--earlier"),
    f: css("--problem"), m: css("--warning"), x: css("--held-back"),
  };
  legend.innerHTML = [["d", "Sent this run"], ["s", "Already in Immich"], ["w", "Waiting"],
    ["m", "Original not on the NAS"], ["f", "Failed"], ["x", "Left out"]]
    .map(([code, text]) => `<span><i style="background:${colors[code]}"></i>${text}</span>`).join("");

  // Small libraries shouldn't leave an empty band under the squares.
  const width = canvas.parentElement.getBoundingClientRect().width;
  const maxHeight = window.innerWidth < 544 ? 176 : 240;
  const cell = Math.min(26, Math.max(1, Math.floor(Math.sqrt((width * maxHeight) / total))));
  const gap = cell > 5 ? 2 : cell > 2 ? 1 : 0;
  const columns = Math.floor(width / cell);
  canvas.style.height = `${Math.ceil(total / columns) * cell}px`;

  const ratio = window.devicePixelRatio || 1;
  const box = canvas.getBoundingClientRect();
  canvas.width = Math.round(box.width * ratio);
  canvas.height = Math.round(box.height * ratio);
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, box.width, box.height);

  for (let index = 0; index < total; index += 1) {
    context.fillStyle = colors[mosaicText[index]] || colors.w;
    context.fillRect((index % columns) * cell, Math.floor(index / columns) * cell, cell - gap, cell - gap);
  }
}

function renderNav(current) {
  const tabs = document.getElementById("tabs");
  const steps = document.getElementById("steps");
  const gear = document.getElementById("gear");
  const onConnection = current[0] === "connection";
  if (onConnection) gear.setAttribute("aria-current", "page");
  else gear.removeAttribute("aria-current");
  if (connection) gear.dataset.connected = String(connection.connected);
  tabs.hidden = !setupDone() || onConnection;
  const settings = current[0] === "settings";
  tabs.innerHTML = `<a href="#/libraries" ${settings ? "" : 'aria-current="page"'}>Libraries</a>
    <a href="#/settings" ${settings ? 'aria-current="page"' : ""}>Settings</a>`;

  const adding = current[0] === "add";
  steps.hidden = !adding;
  if (!adding) return;
  const names = ["Choose library", "Read library", "Choose what to send"];
  const index = !current[1] ? 0 : current[2] === "choose" ? 2 : 1;
  steps.innerHTML = names.map((name, i) =>
    `<li class="${i < index ? "done" : ""}" ${i === index ? 'aria-current="step"' : ""}>${i + 1}. ${name}</li>`).join("");
}

const figure = (value, label, detail = "") =>
  `<div class="figure"><strong>${value}</strong><span>${label}</span>${detail ? `<small>${detail}</small>` : ""}</div>`;
const errorLine = (text) => text ? `<p class="error" role="alert">${escapeHtml(text)}</p>` : "";

function renderConnectForm() {
  const key = state.keyFromEnv
    ? `<p>The API key comes from this container's settings.</p>`
    : `<label class="field">API key
        <small>In Immich, open Account Settings, then API Keys. ${state.hasKey ? "Leave this empty to keep the saved key." : ""}</small>
        <input id="key" type="password" autocomplete="off" value="${escapeHtml(draftKey)}">
      </label>`;
  return `<form id="connect">
      <label class="field">Immich address
        <small>Include the port, for example http://192.168.1.20:2283.</small>
        <input id="url" type="url" required value="${escapeHtml(draftUrl ?? state.immichUrl ?? "")}">
      </label>
      ${key}
      ${errorLine(connectError)}
      <div class="actions"><button class="button primary" type="submit">Connect</button>
        ${identity ? `<span class="status good">Saved.</span>` : ""}</div>
    </form>`;
}

function connectionLine() {
  if (!connection) return `<p class="status-line">Checking the connection…</p>`;
  if (connection.connected) {
    return `<p class="status-line good">Connected to ${escapeHtml(state.immichUrl)} as ${escapeHtml(connection.name)}, Immich ${escapeHtml(connection.version)}.</p>`;
  }
  return `<p class="status-line bad">${connection.error ? `Not connected. ${escapeHtml(connection.error)}` : "Not connected yet."}</p>`;
}

function renderConnection() {
  const [href, text] = state.libraries.length ? ["#/libraries", "All libraries"] : ["#/add", "Choose a library"];
  return `<p><a class="back" href="${href}">${text}</a></p>
    <h2>Immich connection</h2>
    <p class="lead">Every library uploads to the account that owns this API key. Immigrate saves the connection once Immich accepts it, and uses it for every sync after that.</p>
    ${connectionLine()}
    ${renderConnectForm()}`;
}

function renderFolders() {
  if (!folders) {
    return folderError ? `<div class="note bad"><p>${escapeHtml(folderError)}</p></div>` : `<p class="lead">Loading folders…</p>`;
  }
  const added = new Map(state.libraries.map((library) => [library.path, library]));
  const parts = folders.path ? folders.path.split("/") : [];
  const crumbs = [`<button data-folder="">NAS</button>`, ...parts.map((name, index) =>
    `<button data-folder="${escapeHtml(parts.slice(0, index + 1).join("/"))}">${escapeHtml(name)}</button>`)].join("<span>/</span>");
  const rows = folders.folders.map((folder) => {
    if (!folder.library) {
      return `<li><button class="folder" data-folder="${escapeHtml(folder.path)}">${escapeHtml(folder.name)}${countLine(folder.path)}</button></li>`;
    }
    const library = added.get(folder.path);
    const action = library && isSetUp(library)
      ? `<a class="button" href="#/library/${library.id}">Open</a>`
      : `<button class="button primary" data-pick="${escapeHtml(folder.path)}">Use this library</button>`;
    return `<li class="library"><span>${escapeHtml(folder.name)}<small>${library && isSetUp(library) ? "Already added" : "Photos library"}</small></span>${action}</li>`;
  }).join("");
  const empty = folders.folders.length ? "" : `<li class="empty">There are no folders here.</li>`;
  const more = folders.more ? `<li class="empty">And ${plural(folders.more, "more folder")}.</li>` : "";
  const here = added.get(folders.path);
  const useHere = here && isSetUp(here)
    ? `<a class="button" href="#/library/${here.id}">Open</a>`
    : `<button class="button" data-pick="${escapeHtml(folders.path)}">Use this folder</button>`;
  return `<nav class="crumbs" aria-label="Folder">${crumbs}</nav>
    <div class="actions use-folder">${useHere}${countLine(folders.path, "Here and in its subfolders: ")}</div>
    ${errorLine(folderError)}
    <ul class="folders">${rows}${empty}${more}</ul>`;
}

function renderWizardLibrary() {
  return `<h2>Choose a Photos library or a folder</h2>
    <p class="lead">Open the folders on your NAS until you find what you want to send. A Photos library's name ends in .photoslibrary. From any other folder, Immigrate sends the photos and videos inside it, including its subfolders. Immigrate only reads them.</p>
    ${renderFolders()}
    ${errorLine(formError)}
    ${setupDone() ? `<div class="actions"><a class="button" href="#/libraries">Cancel</a></div>` : ""}`;
}

function renderUnreadable(summary) {
  if (!summary.unreadable) return "";
  const names = summary.unreadableFolders.map(escapeHtml).join(", ");
  const more = summary.unreadable > summary.unreadableFolders.length ? ", and more" : "";
  return `<div class="note"><p>${plural(summary.unreadable, "folder")} couldn't be opened, so nothing in ${summary.unreadable === 1 ? "it" : "them"} can be sent: ${names}${more}. Check that the container's user can read ${summary.unreadable === 1 ? "it" : "them"}, then choose Read again.</p></div>`;
}

function renderSummary(summary, kind) {
  if (kind === "folder") {
    return `<div class="figures">
        ${figure(count(summary.total), "items")}
        ${figure(count(summary.photos), "photos")}
        ${figure(count(summary.videos), "videos")}
        ${figure(count(summary.raw), "RAW pairs")}
        ${figure(count(summary.albums), "folders")}
        ${figure(bytes(summary.bytesWithEdits), "total size")}
      </div>
      ${renderUnreadable(summary)}`;
  }
  return `<div class="figures">
      ${figure(count(summary.total), "items")}
      ${figure(count(summary.photos), "photos")}
      ${figure(count(summary.videos), "videos")}
      ${figure(count(summary.albums), "albums")}
      ${figure(bytes(summary.bytesWithEdits), "total size")}
    </div>
    <details><summary>More about this library</summary>
      <div class="figures">
        ${figure(count(summary.edited), "edited")}
        ${figure(count(summary.live), "Live Photos")}
        ${figure(count(summary.raw), "RAW pairs")}
        ${figure(count(summary.favorites), "favorites")}
        ${figure(count(summary.hidden), "hidden")}
      </div>
    </details>`;
}

function renderWizardRead(library) {
  const { running, done, total, error, summary } = library.scan;
  const leave = setupDone() ? `<button class="button" data-action="cancel">Cancel</button>` : "";
  if (running) {
    const checking = library.scan.phase !== "reading";
    const opening = library.kind === "folder"
      ? "Looking through the folder. Large folders can take a few minutes."
      : "Opening the Photos database. Large libraries can take a few minutes.";
    const line = checking ? progressLine(library)
      : total ? `${count(done)} of ${count(total)} items read.`
      : done ? `${plural(done, "item")} found so far.` : opening;
    const after = checking ? "The first check reads every file, so it can take a while. Later checks only read what changed." : "";
    return `<h2>${checking ? "Checking" : "Reading"} ${escapeHtml(library.name)}</h2>
      <div class="progress">${progressBar(library)}<p>${line} ${after}</p></div>
      ${renderLog(true)}
      ${checking ? `<div class="actions"><button class="button" data-action="stop">Stop</button></div>` : ""}`;
  }
  if (!summary) {
    return `<h2>Read ${escapeHtml(library.name)}</h2>
      <p class="lead">Immigrate reads ${escapeHtml(shownPath(library))} and doesn't change it.</p>
      ${error ? `<div class="note bad"><p>${escapeHtml(error)}</p></div>` : ""}
      ${errorLine(formError)}
      <div class="actions"><button class="button primary" data-action="read">Read library</button>
        <button class="button" data-action="back">Back</button>${leave}</div>`;
  }
  const before = isSetUp(library)
    ? " Immigrate has synced this library before, so items already in Immich won't be sent again." : "";
  const empty = !summary.total && library.kind === "folder";
  return `<h2>${plural(summary.total, "item")} in ${escapeHtml(library.name)}</h2>
    <p class="lead">Read ${when(summary.scannedAt)}.${before}</p>
    ${empty ? `<div class="note"><p>There are no photos or videos in this folder. A Photos library inside it has to be added on its own.</p></div>` : renderSummary(summary, library.kind)}
    ${renderMissing(library)}
    ${empty ? "" : renderFindings(library)}
    ${errorLine(formError)}
    <div class="actions">
      ${empty ? "" : `<a class="button primary" href="#/add/${library.id}/choose">Choose what to send</a>`}
      <button class="button" data-action="read">Read again</button>
      <button class="button" data-action="back">Back</button>${leave}
    </div>`;
}

const MISSING_REASONS = { icloud: "Only in iCloud", referenced: "Outside the library", not_found: "File not found" };

function renderMissing(library) {
  const summary = library.scan.summary;
  if (!summary?.missing) return "";
  const items = missing[library.id]?.items || [];
  const icloud = summary.missingInIcloud || 0;
  const referenced = summary.missingReferenced || 0;
  const notFound = summary.missing - icloud - referenced;
  const all = (n) => n === summary.missing;
  const who = (n) => all(n) ? (n === 1 ? "It's" : "They're") : `${count(n)} ${n === 1 ? "is" : "are"}`;
  const them = (n) => n === 1 ? "it" : "them";
  const advice = [];
  if (icloud) {
    advice.push(`<p>${who(icloud)} only in iCloud. On your Mac, open Photos, then Settings, then iCloud, and choose Download Originals to this Mac. Then copy the library back.</p>`);
  }
  if (referenced) {
    advice.push(`<p>${who(referenced)} kept outside the library, in the folder ${referenced === 1 ? "it was" : "they were"} imported from. To bring ${them(referenced)} in, select ${them(referenced)} in Photos on your Mac and choose File, then Consolidate. Then copy the library back.</p>`);
  }
  if (notFound) {
    advice.push(`<p>${who(notFound)} listed in the library, but the ${notFound === 1 ? "file isn't" : "files aren't"} there. The copy on the NAS may be incomplete. Compare the locations below with the library on your Mac.</p>`);
  }
  const rows = items.slice(0, 200).map((item) => `<tr>
      <td>${escapeHtml(item.filename)}<br><small>${new Date(item.taken).toLocaleDateString()}</small></td>
      <td>${MISSING_REASONS[item.reason] || ""}</td>
      <td class="path">${escapeHtml(item.path || "Unknown")}</td>
    </tr>`).join("");
  const more = items.length > 200 ? `<p>And ${plural(items.length - 200, "more item")}.</p>` : "";
  return `<div class="note">
      <p>${plural(summary.missing, "item isn't", "items aren't")} stored on the NAS, so ${summary.missing === 1 ? "it" : "they"} can't be sent. The rest can still be sent.</p>
      ${advice.join("")}
    </div>
    ${rows ? `<details><summary>Show the missing items</summary>
      <table><thead><tr><th>Item</th><th>Why</th><th>Expected location</th></tr></thead><tbody>${rows}</tbody></table>
      ${more}</details>` : ""}`;
}

function immichLink(assetId) {
  if (!assetId || !state.immichUrl) return "";
  const base = state.immichUrl.replace(/\/+$/, "");
  return `<a href="${escapeHtml(base)}/photos/${encodeURIComponent(assetId)}" target="_blank" rel="noopener">Open in Immich</a>`;
}

const hasPlace = (gap) => gap.fix?.latitude != null && gap.fix?.longitude != null;

function gapCounts(data) {
  const dates = data.gaps.filter((g) => g.missingDate);
  const places = data.gaps.filter((g) => g.missingLocation);
  return { date: dates.length, location: places.length,
    dateSet: dates.filter((g) => g.fix?.date).length, locationSet: places.filter(hasPlace).length };
}

function splitLine(split) {
  return `${plural(split.photos, "photo")}, ${plural(split.videos, "video")}`;
}

function renderFindings(library) {
  const { check, checkError, running } = library.scan;
  if (running) return "";
  const again = `<button class="button" data-action="check">Check again</button>`;
  if (checkError) {
    return `<h3 class="section">Duplicates and missing details</h3>
      <div class="note bad"><p>${escapeHtml(checkError)}</p></div><div class="actions">${again}</div>`;
  }
  if (!check) {
    return `<h3 class="section">Duplicates and missing details</h3>
      <p class="lead">This library hasn't been checked for duplicates, or for missing dates and locations.</p>
      <div class="actions"><button class="button" data-action="check">Check library</button></div>`;
  }
  const data = found[library.id]?.data;
  if (!data) return `<h3 class="section">Duplicates and missing details</h3><p class="lead">Loading…</p>`;
  const folder = library.kind === "folder";
  const counts = gapCounts(data);
  const notes = [];
  if (!check.complete) notes.push("The check stopped before it finished, so these numbers are incomplete. Choose Check again to finish it. Files already read aren't read again.");
  if (folder && !check.detailsRead) notes.push("Dates and locations weren't checked, because exiftool isn't installed where Immigrate runs.");
  if (!check.immichAsked) notes.push("Immich wasn't asked which items it already has. Connect to Immich, then choose Check again.");
  if (check.trashed) notes.push(`${plural(check.trashed, "file Immich already has is", "files Immich already has are")} in its trash. Restore them in Immich if you want them back in the timeline.`);
  if (check.unreadable) notes.push(`${plural(check.unreadable, "file")} couldn't be read: ${check.unreadableFiles.map(escapeHtml).join(", ")}${check.unreadable > check.unreadableFiles.length ? ", and more" : ""}.`);
  const copies = data.copies.length ? `<details><summary>Copies (${count(check.copies)})</summary>
      <table><thead><tr><th>Kept</th><th>Copies of it</th></tr></thead><tbody>
      ${data.copies.map((group) => `<tr><td class="path">${escapeHtml(group.keeper)}</td><td class="path">${group.copies.map(escapeHtml).join("<br>")}</td></tr>`).join("")}
      </tbody></table>${data.copyGroups > data.copies.length ? `<p>And ${plural(data.copyGroups - data.copies.length, "more file")}.</p>` : ""}</details>` : "";
  const existing = data.existing.length ? `<details><summary>Already in Immich (${count(data.existingCount)})</summary>
      <table><tbody>${data.existing.map((item) => `<tr><td class="path">${escapeHtml(item.filename)}${item.trashed ? "<br><small>In Immich's trash</small>" : ""}</td><td>${immichLink(item.assetId)}</td></tr>`).join("")}</tbody></table>
      ${data.existingCount > data.existing.length ? `<p>And ${plural(data.existingCount - data.existing.length, "more item")}.</p>` : ""}</details>` : "";
  const section = page()[0] === "add" ? "add" : "library";
  const fixButton = counts.date || counts.location
    ? `<a class="button" href="#/${section}/${library.id}/fix">Set dates and locations</a>` : "";
  return `<h3 class="section">Duplicates and missing details</h3>
    <p class="lead">Checked ${when(check.checkedAt)}.</p>
    <div class="figures">
      ${figure(count(check.copies), "copies", splitLine(data.split.copies))}
      ${figure(count(data.existingCount), "already in Immich", splitLine(data.split.existing))}
      ${folder ? figure(count(counts.date), "without a date", splitLine(data.split.date)) : ""}
      ${figure(count(counts.location), "without a location", splitLine(data.split.location))}
    </div>
    ${notes.length ? `<div class="note">${notes.map((n) => `<p>${n}</p>`).join("")}</div>` : ""}
    ${copies}${existing}
    <div class="actions">${fixButton}${again}</div>`;
}

function gapStatus(gap, filter) {
  if (filter === "date") {
    if (gap.fix?.date) return `Set to ${when(gap.fix.date)}${gap.applied ? ". In Immich" : ""}`;
    return `Immich uses the file's time, ${when(gap.date)}`;
  }
  if (hasPlace(gap)) {
    const where = gap.fix.place || `${gap.fix.latitude.toFixed(4)}, ${gap.fix.longitude.toFixed(4)}`;
    return `Set to ${escapeHtml(where)}${gap.applied ? ". In Immich" : ""}`;
  }
  const hint = gap.suggestion;
  if (!hint) return "No location";
  const apart = hint.minutes < 1 ? "taken at the same time" : hint.minutes < 120 ? `${count(hint.minutes)} min apart` : `${count(Math.round(hint.minutes / 60))} h apart`;
  return `<button class="link" data-suggest="${escapeHtml(gap.uuid)}">Use the location of ${escapeHtml(hint.from)}</button> (${apart})`;
}

function renderFix(library, section) {
  const back = `<p><a class="back" href="#/${section}/${library.id}">${escapeHtml(library.name)}</a></p>`;
  const where = library.kind === "folder" ? "folder" : "library";
  const head = `${back}<h2>Dates and locations</h2>
    <p class="lead">Set what's missing. Immigrate sets it in Immich when it sends an item, or on the next sync if the item is already there. Nothing in the ${where} changes.</p>`;
  const data = found[library.id]?.data;
  if (!data) return `${head}<p class="lead">Loading…</p>`;
  const counts = gapCounts(data);
  if (fix.library !== library.id) {
    Object.assign(fix, { library: library.id, filter: counts.location || !counts.date ? "location" : "date",
      open: new Set(), selected: new Set(), places: null, place: null, error: "", notice: "" });
  }
  const filter = fix.filter;
  const gaps = data.gaps.filter((g) => (filter === "date" ? g.missingDate : g.missingLocation));
  const tabs = library.kind === "folder" ? `<div class="segmented" role="group" aria-label="Show">
      <button class="button" data-fix-filter="location" aria-pressed="${filter === "location"}">Without a location (${count(counts.location)})</button>
      <button class="button" data-fix-filter="date" aria-pressed="${filter === "date"}">Without a date (${count(counts.date)})</button>
    </div>` : "";
  if (!gaps.length) return `${head}${tabs}<p class="lead">Nothing is missing a ${filter === "date" ? "date" : "location"}.</p>`;

  const groups = new Map();
  for (const gap of gaps) {
    if (!groups.has(gap.group)) groups.set(gap.group, []);
    groups.get(gap.group).push(gap);
  }
  const isSet = (gap) => (filter === "date" ? !!gap.fix?.date : hasPlace(gap));
  const rows = [...groups].map(([name, items]) => {
    const open = fix.open.has(name);
    const all = items.every((g) => fix.selected.has(g.uuid));
    const label = name || (library.kind === "folder" ? "Top folder" : "No date");
    const title = library.kind === "folder" ? escapeHtml(label) : new Date(`${name}T12:00:00`).toLocaleDateString([], { dateStyle: "medium" });
    const list = open ? `<ul class="gap-items">${items.map((gap) => `<li class="gap">
        <input type="checkbox" data-select="${escapeHtml(gap.uuid)}" aria-label="Select ${escapeHtml(gap.filename)}" ${fix.selected.has(gap.uuid) ? "checked" : ""}>
        <span>${escapeHtml(gap.where)}</span>
        <span class="detail"><small>${gapStatus(gap, filter)}</small>${immichLink(gap.assetId)}</span>
      </li>`).join("")}</ul>` : "";
    return `<li><div class="gap-group">
        <input type="checkbox" data-select-group="${escapeHtml(name)}" aria-label="Select everything in ${escapeHtml(label)}" ${all ? "checked" : ""}>
        <button class="folder" data-open-group="${escapeHtml(name)}" aria-expanded="${open}">${title}<small>${plural(items.length, "item")}, ${count(items.filter(isSet).length)} set</small></button>
      </div>${list}</li>`;
  }).join("");

  const selected = fix.selected.size;
  const chosen = fix.place ? `<p>Chosen: ${escapeHtml(fix.place.name || "Coordinates")}${fix.place.region ? `, ${escapeHtml(fix.place.region)}` : ""} (${fix.place.latitude.toFixed(4)}, ${fix.place.longitude.toFixed(4)})</p>` : "";
  const results = fix.places ? (fix.places.length ? `<ul class="places">${fix.places.map((place, i) => `<li><button class="link" data-place-index="${i}">${escapeHtml(place.name)}</button>${place.region ? ` <small>${escapeHtml(place.region)}</small>` : ""}</li>`).join("")}</ul>`
    : `<p class="muted">Immich doesn't know a place by that name.</p>`) : "";
  const form = filter === "date"
    ? `<label class="field">Date and time<small>In ${escapeHtml(state.timeZone)}, the time zone chosen in Settings.</small>
        <input id="fix-date" type="datetime-local" step="1" value="${escapeHtml(fix.draft.date)}"></label>
      <div class="actions"><button class="button primary" data-action="fix-set-date" ${selected && fix.draft.date && !fix.busy ? "" : "disabled"}>Set date</button>
        <button class="button" data-action="fix-clear-date" ${selected && !fix.busy ? "" : "disabled"}>Clear date</button></div>`
    : `<label class="field" for="fix-place">Place<small>Search the places Immich knows, or paste coordinates such as 38.7223, -9.1393.</small></label>
      <div class="inline"><input id="fix-place" type="text" autocomplete="off" value="${escapeHtml(fix.draft.name)}">
        <button class="button" data-action="fix-search" ${fix.busy ? "disabled" : ""}>Search</button></div>
      ${results}${chosen}
      <div class="actions"><button class="button primary" data-action="fix-set-location" ${selected && fix.place && !fix.busy ? "" : "disabled"}>Set location</button>
        <button class="button" data-action="fix-clear-location" ${selected && !fix.busy ? "" : "disabled"}>Clear location</button></div>`;
  return `${head}${tabs}
    <p class="lead">${plural(gaps.length, "item")} in ${plural(groups.size, library.kind === "folder" ? "folder" : "day")}, ${count(gaps.filter(isSet).length)} set. Open a ${library.kind === "folder" ? "folder" : "day"} to see its items.</p>
    <ul class="gaps">${rows}</ul>
    <div class="fix-panel">
      <p><strong>${plural(selected, "item")} selected</strong>
        <button class="link" data-fix-select="all">Select all ${count(gaps.length)}</button>
        ${selected ? `<button class="link" data-fix-select="none">Clear selection</button>` : ""}</p>
      ${form}
      ${errorLine(fix.error)}
      ${fix.notice ? `<p class="status good">${escapeHtml(fix.notice)}</p>` : ""}
    </div>`;
}

function radio(values, name, value, title, detail) {
  return `<label class="choice"><input type="radio" name="${name}" value="${value}" ${values.options[name] === value ? "checked" : ""}>
    <span>${title}</span><small>${detail}</small></label>`;
}
function checkbox(values, name, title, detail) {
  return `<label class="choice"><input type="checkbox" name="${name}" ${values.options[name] ? "checked" : ""}>
    <span>${title}</span><small>${detail}</small></label>`;
}

function renderSchedule(values, thing) {
  return `<fieldset><legend>Keep in sync</legend>
        <label class="field"><small>Immigrate checks the ${thing} on this schedule and sends anything new or changed.</small>
          <select name="autoMinutes">${SCHEDULES.map(([value, text]) =>
            `<option value="${value}" ${values.autoMinutes === value ? "selected" : ""}>${text}</option>`).join("")}</select>
        </label>
      </fieldset>`;
}

function renderDuplicates(values) {
  return `<fieldset><legend>Copies inside the library</legend>
        ${radio(values, "copies", "include", "Add copies to their albums too", "Immich keeps one copy of identical files either way.")}
        ${radio(values, "copies", "skip", "Leave copies out", "Only the first copy of each file is looked at.")}
      </fieldset>
      <fieldset><legend>Already in Immich</legend>
        ${radio(values, "existing", "link", "Link them", "They're added to albums and favorites, and get the dates and locations you set, without uploading them again.")}
        ${radio(values, "existing", "skip", "Leave them as they are", "Immigrate doesn't send or change them.")}
      </fieldset>`;
}

function renderChoices(values, kind) {
  if (kind === "folder") {
    return `<form id="choices">
        <fieldset><legend>Organization</legend>
          ${checkbox(values, "albums", "Make an album from each folder", "Albums get names like Photos/Trips/Lisbon. Immigrate never removes albums, so leave this off if you have a lot of folders.")}
        </fieldset>
        ${renderDuplicates(values)}
        ${renderSchedule(values, "folder")}
      </form>`;
  }
  return `<form id="choices">
      <fieldset><legend>Edited photos</legend>
        ${radio(values, "edits", "stack", "Send edits and originals", "Immich shows the edit, with the original stacked behind it.")}
        ${radio(values, "edits", "original", "Send originals only", "Uses less space, but your edits won't appear.")}
      </fieldset>
      <fieldset><legend>Hidden photos</legend>
        ${radio(values, "hidden", "skip", "Leave them out", "They stay private in Apple Photos.")}
        ${radio(values, "hidden", "archive", "Send to Immich's Archive", "They stay out of the main timeline.")}
      </fieldset>
      <fieldset><legend>Organization</legend>
        ${checkbox(values, "albums", "Recreate albums", "Albums in folders become names like Trips/Lisbon.")}
        ${checkbox(values, "people_as_tags", "Add people as tags", "Names become tags such as People/Ana. Immich still finds faces on its own.")}
      </fieldset>
      ${renderDuplicates(values)}
      ${renderSchedule(values, "library")}
    </form>`;
}

function readChoices(base) {
  const form = document.getElementById("choices");
  const data = new FormData(form);
  // A folder's form leaves out the choices that only apply to Photos libraries.
  const chosen = (name, value) => form.elements[name] ? value : base.options[name];
  return {
    options: { ...base.options, edits: chosen("edits", data.get("edits")), hidden: chosen("hidden", data.get("hidden")),
      albums: data.has("albums"), people_as_tags: chosen("people_as_tags", data.has("people_as_tags")),
      copies: chosen("copies", data.get("copies")), existing: chosen("existing", data.get("existing")) },
    autoMinutes: Number(data.get("autoMinutes")),
  };
}

const sameChoices = (a, b) => JSON.stringify(a.options) === JSON.stringify(b.options) && a.autoMinutes === b.autoMinutes;

function saveBar(action, saved) {
  const changed = draft && !sameChoices(draft.values, saved);
  const status = changed ? `<span class="status">Not saved yet</span>`
    : notice ? `<span class="status good">${escapeHtml(notice)}</span>` : "";
  return `${errorLine(saveError)}
    <div class="actions"><button class="button primary" data-action="${action}" ${changed ? "" : "disabled"}>Save settings</button>${status}</div>`;
}

function wizardChoices(library) {
  return { options: library.options, autoMinutes: library.autoMinutes || state.defaults.autoMinutes };
}

function renderWizardChoose(library) {
  const values = draft?.values ?? wizardChoices(library);
  const result = preview?.page === lastPage ? preview.result : null;
  const summary = result ? `<div class="figures">
      ${figure(count(result.new + result.changed + result.retry), "to send")}
      ${figure(bytes(result.bytes), "to upload")}
      ${figure(count(result.unchanged), "already sent")}
      ${figure(count(result.linked), "found in Immich")}
      ${figure(count(result.newAlbums), "new albums")}
    </div>
    <p class="lead">Immich needs extra space for thumbnails and transcoded videos on top of this.</p>` : "";
  const removed = library.kind === "folder" ? "this folder" : "Apple Photos";
  return `<h2>Choose what to send</h2>
    <p class="lead">These start from the defaults in Settings. They're saved for ${escapeHtml(library.name)} when you start the sync, and you can change them later on its page. Nothing is deleted. Photos you remove from ${removed} stay in Immich.</p>
    ${renderChoices(values, library.kind)}
    ${summary}
    ${errorLine(formError)}
    <div class="actions"><button class="button primary" data-action="start">Start sync</button>
      <a class="button" href="#/add/${library.id}">Back</a></div>`;
}

const PHASES = {
  queued: "Starting", uploading: "Sending photos", fixing: "Setting dates and locations", albums: "Building albums",
  finished: "Last synced", stopped: "Last sync stopped", failed: "Last sync stopped early",
};

function statusLine(library) {
  const job = library.job;
  if (isRunning(job)) {
    if (job.phase === "albums") return `Building albums: ${count(job.albumsDone)} of ${count(job.albumsTotal)}.`;
    if (job.phase === "fixing") return `Setting dates and locations: ${count(job.fixesDone)} of ${count(job.fixesTotal)} items.`;
    return `Sending: ${count(job.total - job.waiting)} of ${count(job.total)} items checked, ${count(job.done)} sent.`;
  }
  if (library.scan.running) return progressLine(library);
  if (library.queued) return "Starting the sync.";
  const last = library.lastSync;
  if (!last) return library.scan.summary ? "Read, but not synced yet." : "Not synced yet.";
  const date = last.finishedAt ? ` ${when(last.finishedAt)}` : "";
  const failed = last.failed ? `, ${count(last.failed)} failed` : "";
  return `${PHASES[last.phase]}${date}: ${count(last.done)} sent, ${count(last.skipped)} already in Immich${failed}.`;
}

// What a read or check is doing now, in one sentence.
function progressLine(library) {
  const { phase, done, total } = library.scan;
  if (phase === "checksums") {
    return total ? `Checking for duplicates: ${bytes(done)} of ${bytes(total)} read.` : "Checking for duplicates.";
  }
  if (phase === "details") return `Checking dates and locations: ${count(done)} of ${count(total)} files.`;
  if (phase === "immich") return "Asking Immich which items it already has.";
  if (total) return `Reading the library: ${count(done)} of ${count(total)} items.`;
  return done ? `Reading the library: ${plural(done, "item")} found so far.` : "Reading the library.";
}

function scheduleLine(library) {
  if (!library.autoMinutes) return "Not kept in sync. Turn on a schedule in its settings.";
  const checked = library.lastCheck ? ` Last checked ${when(library.lastCheck * 1000)}.` : "";
  const next = library.nextCheck && library.nextCheck * 1000 > Date.now()
    ? ` Next check around ${clock(library.nextCheck * 1000)}.` : " Next check in a minute or so.";
  return `Checks for changes ${EVERY[library.autoMinutes]}.${checked}${next}`;
}

function syncButton(library) {
  if (isRunning(library.job)) return `<button class="button" data-action="stop">Stop</button>`;
  if (!isSetUp(library) && !library.scan.running && !library.queued) {
    return `<a class="button primary" href="#/add/${library.id}">Continue setup</a>`;
  }
  const busy = library.scan.running || library.queued || !!state.syncing;
  return `<button class="button primary" data-action="sync" data-id="${library.id}" ${busy ? "disabled" : ""}>Start sync</button>`;
}

// Bytes a second over the last few readings, or 0 until there are enough of them.
function speedOf(job) {
  if (pace.job !== job.startedAt) Object.assign(pace, { job: job.startedAt, samples: [] });
  const now = Date.now();
  const last = pace.samples.at(-1);
  if (!last || now - last.at >= 900) pace.samples.push({ at: now, sent: job.bytesSent });
  while (pace.samples.length > 2 && now - pace.samples[0].at > 20000) pace.samples.shift();
  const first = pace.samples[0];
  const seconds = (now - first.at) / 1000;
  return seconds >= 3 ? Math.max(0, (job.bytesSent - first.sent) / seconds) : 0;
}

// The bar for whatever a library is doing now, or nothing when it's idle.
function progressBar(library) {
  const job = library.job;
  if (isRunning(job)) {
    if (job.phase === "albums") return bar(job.albumsDone, job.albumsTotal, "Albums built");
    if (job.phase === "fixing") return bar(job.fixesDone, job.fixesTotal, "Dates and locations set");
    if (job.phase === "uploading") {
      return job.bytesTotal ? bar(job.bytesSent, job.bytesTotal, "Sent to Immich") : bar(job.handled, job.todo, "Items sent");
    }
    return bar(0, 0, "Starting");
  }
  if (library.scan.running) {
    const { phase, done, total } = library.scan;
    // Asking Immich is one request, so there's no telling how far along it is.
    return bar(done, phase === "immich" ? 0 : total, phase === "reading" ? "Library read" : "Library checked");
  }
  return library.queued ? bar(0, 0, "Starting") : "";
}

function renderWorkers(job) {
  if (job.phase !== "uploading" || !job.workers?.length) return "";
  const rows = job.workers.map((worker) => {
    if (!worker.file) {
      return `<li class="worker idle"><span class="who">Worker ${worker.id}</span><span class="what">Waiting for the next item</span></li>`;
    }
    const detail = worker.size
      ? `${worker.part ? `${worker.part}, ` : ""}${bytes(worker.sent)} of ${bytes(worker.size)}`
      : "Linking to what Immich already has";
    return `<li class="worker"><span class="who">Worker ${worker.id}</span>
        <span class="what"><span class="path">${escapeHtml(worker.file)}</span><small>${detail}</small></span>
        ${worker.size ? bar(worker.sent, worker.size, `Worker ${worker.id}`) : ""}</li>`;
  }).join("");
  return `<h4>Workers</h4>
    <p class="muted">Each worker sends one item at a time, with its edit, RAW, and Live Photo video.</p>
    <ul class="workers">${rows}</ul>`;
}

// The bar and sentence at the top of a running sync.
function renderSyncProgress(library) {
  const job = library.job;
  let line = statusLine(library);
  if (job.phase === "uploading") {
    const speed = speedOf(job);
    const left = job.bytesTotal - job.bytesSent;
    line = `${count(job.handled)} of ${plural(job.todo, "item")} done.`;
    if (job.bytesTotal) line += ` ${bytes(job.bytesSent)} of ${bytes(job.bytesTotal)} sent${speed ? `, at ${bytes(speed)} a second` : ""}.`;
    if (speed && left > 0) line += ` ${timeLeft(left / speed).replace(/^a/, "A")} left.`;
    if (!job.todo) line = "Everything is already in Immich.";
  }
  return `<div class="progress">${progressBar(library)}<p>${line}</p></div>${renderWorkers(job)}`;
}

function renderLog(busy) {
  const shown = logLines.slice(-LOG_SHOWN);
  const rows = shown.map((line) =>
    `<p class="${line.level}"><time>${clockSeconds(line.at * 1000)}</time>${escapeHtml(line.text)}</p>`).join("");
  const note = logLines.length > shown.length
    ? `The last ${count(shown.length)} lines. The container's log has the rest.`
    : "What Immigrate has done since it started. The container's log has the same lines, and more.";
  return `<details class="log" id="log" ${logOpen ?? busy ? "open" : ""}><summary>Log</summary>
      <div class="log-lines" id="log-lines" role="log" aria-live="off" aria-label="Log" tabindex="0">${rows || `<p>Nothing yet.</p>`}</div>
      <p class="muted">${note}</p>
    </details>`;
}

function renderDashboard() {
  const cards = state.libraries.map((library) => `<li class="card">
      <h3><a href="#/library/${library.id}">${escapeHtml(library.name)}</a></h3>
      <p class="muted path">${escapeHtml(shownPath(library))}</p>
      <p>${statusLine(library)}</p>
      ${progressBar(library)}
      <p class="muted">${scheduleLine(library)}</p>
      <div class="actions">${syncButton(library)}<a class="button" href="#/library/${library.id}">Open</a></div>
    </li>`).join("");
  return `<h2>Libraries</h2>
    <p class="lead">${state.hasKey ? `Sending to ${escapeHtml(state.immichUrl)}.` : `Not connected to Immich yet. <a href="#/connection">Connect to Immich</a>.`} Open a library to see its details or change its settings.</p>
    <ul class="cards">${cards}</ul>
    ${errorLine(formError)}
    <div class="actions"><a class="button" href="#/add">Add a library</a></div>`;
}

function renderActivity(library) {
  const job = library.job;
  const run = job || library.lastSync;
  if (library.scan.running || library.queued) {
    return `<h3 class="section">${library.queued ? "Reading, then syncing" : "Reading the library"}</h3>
      <div class="progress">${progressBar(library)}<p>${statusLine(library)}</p></div>`;
  }
  if (!run) {
    return `<h3 class="section">Not synced yet</h3>
      <div class="actions">${syncButton(library)}</div>`;
  }
  const notes = [];
  if (run.error) notes.push(`<div class="note bad"><p>${escapeHtml(run.error)}</p></div>`);
  if (run.phase === "stopped") notes.push(`<div class="note"><p>Start the sync again to continue. Finished items won't be sent twice.</p></div>`);
  if (run.warningCount) {
    notes.push(`<details><summary>${plural(run.warningCount, "warning")}</summary>${run.warnings.map((w) => `<p>${escapeHtml(w)}</p>`).join("")}</details>`);
  }
  const running = isRunning(job);
  const album = run.phase === "albums" ? figure(`${count(run.albumsDone)} of ${count(run.albumsTotal)}`, "albums") : "";
  const finishedAt = run.finishedAt || library.lastSync?.finishedAt;
  const finished = !running && finishedAt ? `<p class="lead">Finished ${when(finishedAt)}.</p>` : "";
  const buttons = running
    ? `<button class="button" data-action="stop">Stop</button>`
    : `${syncButton(library)}<button class="button" data-action="verify">Check the result</button>`;
  return `<h3 class="section">${PHASES[run.phase]}</h3>
    ${finished}
    ${running ? renderSyncProgress(library) : ""}
    <div class="figures">
      ${figure(count(run.done), "sent")}
      ${figure(count(run.skipped), "already in Immich")}
      ${figure(count(run.failed), "failed")}
      ${figure(bytes(run.bytesDone), "uploaded")}
      ${album}
    </div>
    ${job && mosaicText ? `<canvas id="mosaic" role="img" aria-label="One square per item in the library, colored by sync status"></canvas><p class="legend" id="legend"></p>` : ""}
    ${notes.join("")}
    ${errorLine(formError)}
    <div class="actions">${buttons}</div>
    ${running ? "" : `<p class="muted">Start sync reads the library again, then sends anything new or changed.</p>`}`;
}

function renderCheck(library) {
  const result = reports[library.id] || library.lastVerify;
  if (!result) return "";
  const gaps = (result.albums || []).filter((a) => a.matched < a.expected);
  const list = (title, items, total) => total ? `<details><summary>${title} (${count(total)})</summary>
    <p>${items.map(escapeHtml).join(", ")}${total > items.length ? ", and more" : ""}</p></details>` : "";
  const checked = result.checkedAt ? ` Checked ${when(result.checkedAt)}.` : "";
  return `<h3 class="section">${result.clean ? "Everything matches" : "Some items need attention"}</h3>
    <p class="lead">${count(result.confirmed)} of ${count(result.expected)} items confirmed in Immich.${checked}</p>
    ${list("Not sent yet", result.notSynced, result.notSyncedCount)}
    ${list("Removed from Immich since the sync", result.goneFromImmich, result.goneCount)}
    ${list("Originals not on the NAS", result.missingFromLibrary, result.missingCount)}
    ${gaps.length ? `<table><thead><tr><th>Album with gaps</th><th class="number">In Immich</th><th class="number">Expected</th></tr></thead><tbody>
      ${gaps.slice(0, 50).map((a) => `<tr><td>${escapeHtml(a.title)}</td><td class="number">${count(a.matched)}</td><td class="number">${count(a.expected)}</td></tr>`).join("")}
    </tbody></table>` : ""}
    ${result.albums?.length ? `<p>${plural(result.albums.length - gaps.length, "album matches", "albums match")}.</p>` : ""}`;
}

// Only once this library's import is done, and only while copies are actually there.
function renderLeftovers(library, busy) {
  if (!isSetUp(library) || busy) return "";
  const found = state.leftovers;
  if (!found && !freed) return "";
  const body = found
    ? `<p>${found.count === 1 ? "A library read" : `${count(found.count)} library reads`} didn't finish, for example because the container stopped. ${found.count === 1 ? "It" : "They"} left ${found.count === 1 ? "a copy" : "copies"} of the Photos database in /data/tmp, using ${bytes(found.bytes)}. Immigrate doesn't need ${found.count === 1 ? "it" : "them"}. Your library and Immich aren't touched.</p>
      ${errorLine(leftoverError)}
      <div class="actions"><button class="button" data-action="delete-leftovers">Delete temporary files</button></div>`
    : `<div class="note good"><p>Temporary files deleted. ${bytes(freed.bytes)} freed.</p></div>`;
  return `<h3 class="section">Temporary files</h3>${body}`;
}

function renderLibrary(library) {
  const saved = { options: library.options, autoMinutes: library.autoMinutes };
  const values = draft?.values ?? saved;
  const result = preview?.page === lastPage ? preview.result : null;
  const next = result
    ? `<p>With these settings, the next sync sends ${plural(result.new + result.changed + result.retry, "item")} (${bytes(result.bytes)}), going by the last read.</p>` : "";
  const summary = library.scan.summary;
  const busy = library.scan.running || library.queued || isRunning(library.job);
  return `<p><a class="back" href="#/libraries">All libraries</a></p>
    <h2>${escapeHtml(library.name)}</h2>
    <p class="lead">${escapeHtml(shownPath(library))}<br>${scheduleLine(library)}</p>
    ${renderActivity(library)}
    ${renderLog(busy)}
    ${renderCheck(library)}
    ${summary ? `<h3 class="section">In the library</h3>
      <p class="lead">${plural(summary.total, "item")}, read ${when(summary.scannedAt)}.</p>
      ${renderSummary(summary, library.kind)}${renderMissing(library)}${renderFindings(library)}` : ""}
    <h3 class="section">Settings for this library</h3>
    <p class="lead">Every sync of ${escapeHtml(library.name)} uses these, by hand or on the schedule. Libraries you add later start from the defaults in <a href="#/settings">Settings</a>.</p>
    ${renderChoices(values, library.kind)}
    ${next}
    ${saveBar("save-library", saved)}
    ${renderLeftovers(library, busy)}
    <h3 class="section">Remove this library</h3>
    <p>Immigrate stops reading and sending ${escapeHtml(library.name)}. Nothing is deleted from Immich or from the library. If you add it again, Immigrate picks up where it left off.</p>
    <div class="actions"><button class="button" data-action="remove" ${busy ? "disabled" : ""}>Remove library</button></div>`;
}

function renderTimeZone() {
  const names = timeZones ?? [state.timeZone];
  const hint = browserZone && browserZone !== state.timeZone && names.includes(browserZone)
    ? ` This browser is set to ${escapeHtml(browserZone)}.` : "";
  return `<label class="field"><small>Times on these pages and in the container's log use this time zone.${hint}</small>
      <select id="timeZone">${names.map((name) =>
        `<option value="${escapeHtml(name)}" ${name === state.timeZone ? "selected" : ""}>${escapeHtml(name)}</option>`).join("")}</select>
    </label>
    ${errorLine(zoneError)}
    ${zoneNotice ? `<div class="note good"><p>${escapeHtml(zoneNotice)}</p></div>` : ""}`;
}

function renderSettings() {
  const saved = state.defaults;
  const values = draft?.values ?? saved;
  return `<h2>Settings</h2>
    <p class="lead">The Immich connection is behind the gear at the top right.</p>
    <h3 class="section first">Time zone</h3>
    ${renderTimeZone()}
    <h3 class="section">Defaults for new libraries</h3>
    <p class="lead">A library you add starts with these choices. A folder only uses the schedule, and its albums start turned off. Changing these doesn't change libraries you've already added. Change those on each library's page.</p>
    ${renderChoices(values)}
    ${saveBar("save-defaults", saved)}`;
}

function renderPage(current) {
  const library = currentLibrary();
  switch (current[0]) {
    case "connection": return renderConnection();
    case "add":
      if (!library) return renderWizardLibrary();
      if (current[2] === "fix") return renderFix(library, "add");
      return current[2] === "choose" && library.scan.summary ? renderWizardChoose(library) : renderWizardRead(library);
    case "library":
      if (library && current[2] === "fix") return renderFix(library, "library");
      return library ? renderLibrary(library) : renderDashboard();
    case "settings": return renderSettings();
    default: return renderDashboard();
  }
}

function render() {
  if (!state) return;
  document.getElementById("version").textContent = `Immigrate ${state.version}`;
  const current = page();
  const key = current.join("/");
  if (key !== lastPage) {
    lastPage = key;
    formError = saveError = connectError = notice = leftoverError = zoneNotice = zoneError = "";
    draft = null;
    freed = null;
    identity = null;
    connectionStale = current[0] === "connection";
    window.scrollTo(0, 0);
  }
  if (view.contains(document.activeElement) && document.activeElement.matches("input, select")) {
    drawMosaic();
    return;
  }
  renderNav(current);
  const before = document.getElementById("log-lines");
  if (before && before.clientHeight) {
    logScroll = { top: before.scrollTop, pinned: before.scrollHeight - before.scrollTop - before.clientHeight < 12 };
  }
  view.innerHTML = renderPage(current);
  const box = document.getElementById("log-lines");
  // Stays on the newest line, unless the reader scrolled up to look at an older one.
  if (box) box.scrollTop = logScroll.pinned ? box.scrollHeight : logScroll.top;
  drawMosaic();
  loadPageData(current);
}

async function openFolder(path) {
  try {
    folders = await api(`/api/folders?path=${encodeURIComponent(path)}`);
    folderError = "";
    countFolders();
  } catch (error) {
    folderError = error.message;
  }
}

function countText(found) {
  if (!found) return "Counting…";
  if (found.error) return "Couldn't be counted";
  const total = found.photos + found.videos;
  if (!found.complete) return `More than ${plural(total, "photo or video", "photos and videos")}`;
  if (!total) return "No photos or videos";
  const parts = [];
  if (found.photos) parts.push(plural(found.photos, "photo"));
  if (found.videos) parts.push(plural(found.videos, "video"));
  return parts.join(" and ");
}

function countLine(path, prefix = "") {
  return `<small class="count" data-count="${escapeHtml(path)}" data-prefix="${escapeHtml(prefix)}">${escapeHtml(prefix + countText(folderCounts.get(path)))}</small>`;
}

function showCount(path) {
  for (const line of view.querySelectorAll("[data-count]")) {
    if (line.dataset.count === path) line.textContent = line.dataset.prefix + countText(folderCounts.get(path));
  }
}

// Count the open folder and the folders in it, a few at a time. Opening another folder stops this.
function countFolders() {
  const round = ++countRound;
  const waiting = [folders.path, ...folders.folders.filter((folder) => !folder.library).map((folder) => folder.path)]
    .filter((path) => !folderCounts.has(path));
  const next = async () => {
    while (waiting.length && round === countRound) {
      const path = waiting.shift();
      try {
        folderCounts.set(path, await api(`/api/folders/count?path=${encodeURIComponent(path)}`));
      } catch {
        folderCounts.set(path, { error: true });
      }
      showCount(path);
    }
  };
  for (let i = 0; i < 3; i += 1) next();
}

async function loadFolders() {
  foldersLoading = true;
  folderCounts.clear();
  // Start beside the library added last, where the next one is likely to be.
  const latest = state.libraries.at(-1)?.path || "";
  const parent = latest.split("/").slice(0, -1).join("/");
  await openFolder(parent);
  if (folderError && parent) await openFolder("");
  foldersLoading = false;
  render();
}

async function refreshPreview(library, options) {
  previewLoading = true;
  const forPage = lastPage;
  try {
    preview = { page: forPage, result: await api(`/api/libraries/${library.id}/preview`, options) };
  } catch {
    preview = null;
  }
  previewLoading = false;
  render();
}

async function loadTimeZones() {
  timeZonesLoading = true;
  try {
    timeZones = await api("/api/timezones");
    timeZonesLoading = false;
    render();
  } catch {
    // The list only has the saved time zone. The next refresh tries again.
    timeZonesLoading = false;
  }
}

async function saveTimeZone(select) {
  zoneNotice = zoneError = "";
  select.disabled = true;
  try {
    const saved = await api("/api/timezone", { timeZone: select.value });
    zoneNotice = `Saved. Times now use ${saved.timeZone}.`;
  } catch (error) {
    zoneError = error.message;
  }
  select.blur();
  await tick();
}

// Findings change when the library is checked again and after each sync.
const findingsKey = (library) => `${library.scan.check?.checkedAt}|${library.lastSync?.finishedAt}`;

async function loadFindings(library) {
  const key = findingsKey(library);
  found[library.id] = { ...found[library.id], for: key };
  try {
    found[library.id] = { for: key, data: await api(`/api/libraries/${library.id}/findings`) };
  } catch {
    found[library.id] = { for: null, data: found[library.id]?.data };
    return;
  }
  render();
}

async function saveFix(library, body) {
  fix.error = fix.notice = "";
  fix.busy = true;
  render();
  try {
    const result = await api(`/api/libraries/${library.id}/fixes`, body);
    fix.notice = `Saved for ${plural(result.saved, "item")}. ${isSetUp(library) ? "Start sync to set it in Immich." : "It's set in Immich when the items are sent."}`;
    fix.selected.clear();
    await loadFindings(library);
  } catch (error) {
    fix.error = error.message;
  }
  fix.busy = false;
  render();
}

function parseCoordinates(text) {
  const match = text.trim().match(/^(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)$/);
  if (!match) return null;
  const [latitude, longitude] = [Number(match[1]), Number(match[2])];
  return Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180 ? { name: "", region: "", latitude, longitude } : null;
}

async function searchPlaces() {
  fix.error = "";
  const typed = fix.draft.name.trim();
  const coordinates = parseCoordinates(typed);
  if (coordinates) {
    fix.place = coordinates;
    fix.places = null;
    return;
  }
  fix.busy = true;
  render();
  try {
    fix.places = await api(`/api/places?name=${encodeURIComponent(typed)}`);
    fix.place = null;
  } catch (error) {
    fix.error = error.message;
  }
  fix.busy = false;
}

// The items the fix page is showing now.
function shownGaps(library) {
  const data = found[library.id]?.data;
  if (!data) return [];
  return data.gaps.filter((g) => (fix.filter === "date" ? g.missingDate : g.missingLocation));
}

function loadPageData(current) {
  const library = currentLibrary();
  if (library?.scan.check && !library.scan.running && found[library.id]?.for !== findingsKey(library)) {
    loadFindings(library);
  }
  if (current[0] === "settings" && !timeZones && !timeZonesLoading) loadTimeZones();
  if (current[0] === "connection" && connectionStale) {
    connectionStale = false;
    checkConnection();
  }
  if (current[0] === "add" && !library && !folders && !foldersLoading) loadFolders();
  const wantsPreview = current[0] === "library" || current[2] === "choose";
  if (library?.scan.summary && wantsPreview && preview?.page !== lastPage && !previewLoading) {
    const values = draft?.values ?? (current[0] === "add" ? wizardChoices(library) : library);
    refreshPreview(library, values.options);
  }
  const summary = library?.scan.summary;
  if (summary?.missing && missing[library.id]?.for !== summary.scannedAt) {
    missing[library.id] = { for: summary.scannedAt, items: [] };
    api(`/api/libraries/${library.id}/missing`).then((items) => {
      missing[library.id] = { for: summary.scannedAt, items };
      render();
    }).catch(() => {});
  }
}

async function checkConnection() {
  try {
    connection = await api("/api/connection");
  } catch {
    connection = { connected: false, error: "Immigrate couldn't check the connection. Reload the page to try again." };
  }
  render();
}

function openWizard() {
  Object.assign(wizard, { step: "address", url: state.immichUrl || "", key: "", error: "", busy: false, version: null, result: null });
  drawWizard();
  if (!wizardDialog.open) wizardDialog.showModal();
  wizardDialog.querySelector("input")?.focus();
}

function drawWizard() {
  const steps = [["address", "Immich address"], ...(state.keyFromEnv ? [] : [["key", "API key"]]), ["done", "Done"]];
  const index = steps.findIndex(([step]) => step === wizard.step);
  const list = `<ol class="steps" aria-label="Connection steps">${steps.map(([, name], i) =>
    `<li class="${i < index ? "done" : ""}" ${i === index ? 'aria-current="step"' : ""}>${i + 1}. ${name}</li>`).join("")}</ol>`;
  const busy = wizard.busy ? "disabled" : "";
  let body = "";
  if (wizard.step === "address") {
    const problem = connection?.error ? `<div class="note bad"><p>${escapeHtml(connection.error)}</p></div>` : "";
    body = `<h2 id="wizard-title">${connection?.error ? "Immigrate can't reach Immich" : "Connect to Immich"}</h2>
      <p class="lead">Immigrate sends your photos and videos to Immich. Tell it where Immich is, and it saves the connection for every sync.</p>
      ${problem}
      <form data-wizard="address">
        <label class="field">Immich address
          <small>Include the port, for example http://192.168.1.20:2283.</small>
          <input id="wizard-url" type="url" required value="${escapeHtml(wizard.url)}">
        </label>
        ${errorLine(wizard.error)}
        <div class="actions"><button class="button primary" type="submit" ${busy}>${state.keyFromEnv ? "Connect" : "Next"}</button>
          <button class="button" type="button" data-wizard-close>Not now</button></div>
      </form>`;
  } else if (wizard.step === "key") {
    const keep = state.hasKey ? " Leave this empty to keep the saved key." : "";
    body = `<h2 id="wizard-title">Add an API key</h2>
      <p class="lead">Immich ${escapeHtml(wizard.version)} answered at ${escapeHtml(wizard.url)}. Photos upload to the account that owns this key.</p>
      <ol class="howto">
        <li>Open Immich and sign in as the person the photos belong to.</li>
        <li>Open Account Settings, then API Keys, and choose New API Key.</li>
        <li>Give it permission to upload, read, and update assets, manage albums, and create stacks. Copy the key.</li>
      </ol>
      <form data-wizard="key">
        <label class="field">API key
          <small>Immigrate keeps it on the server and never shows it again.${keep}</small>
          <input id="wizard-key" type="password" autocomplete="off" ${state.hasKey ? "" : "required"} value="${escapeHtml(wizard.key)}">
        </label>
        ${errorLine(wizard.error)}
        <div class="actions"><button class="button primary" type="submit" ${busy}>Connect</button>
          <button class="button" type="button" data-wizard-back>Back</button></div>
      </form>`;
  } else {
    const who = wizard.result;
    body = `<h2 id="wizard-title">Connected to Immich</h2>
      <p class="lead">Connected as ${escapeHtml(who.name)}, Immich ${escapeHtml(who.version)}. Saved. Immigrate uses this connection for every sync. Change it any time with the gear at the top right.</p>
      <div class="actions"><button class="button primary" type="button" data-wizard-close>Done</button></div>`;
  }
  wizardDialog.innerHTML = list + body;
}

async function wizardSubmit(form) {
  wizard.error = "";
  wizard.busy = true;
  drawWizard();
  try {
    if (form.dataset.wizard === "address") {
      wizard.url = wizard.url.trim();
      if (state.keyFromEnv) {
        await wizardConnect();
      } else {
        wizard.version = (await api("/api/connection/address", { url: wizard.url })).version;
        wizard.step = "key";
      }
    } else {
      await wizardConnect();
    }
  } catch (error) {
    wizard.error = error.message;
  }
  wizard.busy = false;
  drawWizard();
  const field = wizardDialog.querySelector("input");
  if (field) field.focus();
  else wizardDialog.querySelector("button")?.focus();
}

async function wizardConnect() {
  wizard.result = await api("/api/connect", { url: wizard.url, key: wizard.key });
  wizard.key = "";
  connection = { connected: true, ...wizard.result };
  wizard.step = "done";
  await tick();
}

// A library that was only just added leaves again if the setup is abandoned.
async function leaveSetup(library, destination) {
  if (library && !isSetUp(library)) await api(`/api/libraries/${library.id}/remove`, {});
  location.hash = destination;
}

async function act(name, button) {
  formError = saveError = notice = leftoverError = "";
  if (button) button.disabled = true;
  const library = currentLibrary();
  try {
    if (name === "read") await api(`/api/libraries/${library.id}/read`, {});
    if (name === "sync") await api(`/api/libraries/${button.dataset.id || library.id}/sync`, { read: true });
    if (name === "stop") await api("/api/stop", {});
    if (name === "verify") reports[library.id] = await api(`/api/libraries/${library.id}/verify`, {});
    if (name === "check") await api(`/api/libraries/${library.id}/check`, {});
    if (name === "fix-search") await searchPlaces();
    if (name === "fix-set-location") {
      const { latitude, longitude, name: place, region } = fix.place;
      await saveFix(library, { uuids: [...fix.selected], latitude, longitude, place: [place, region].filter(Boolean).join(", ") });
    }
    if (name === "fix-clear-location") await saveFix(library, { uuids: [...fix.selected], clear: "location" });
    if (name === "fix-set-date") await saveFix(library, { uuids: [...fix.selected], date: fix.draft.date });
    if (name === "fix-clear-date") await saveFix(library, { uuids: [...fix.selected], clear: "date" });
    if (name === "back") await leaveSetup(library, "#/add");
    if (name === "cancel") await leaveSetup(library, "#/libraries");
    if (name === "start") {
      const values = readChoices(wizardChoices(library));
      await api(`/api/libraries/${library.id}/settings`, values);
      await api(`/api/libraries/${library.id}/sync`, { read: false });
      location.hash = `#/library/${library.id}`;
    }
    if (name === "save-library") {
      await api(`/api/libraries/${library.id}/settings`, draft.values);
      draft = null;
      notice = "Saved.";
    }
    if (name === "save-defaults") {
      await api("/api/defaults", draft.values);
      draft = null;
      notice = "Saved.";
    }
    if (name === "delete-leftovers") freed = await api("/api/leftovers/delete", {});
    if (name === "remove" && confirm(`Remove ${library.name} from Immigrate? Nothing is deleted from Immich.`)) {
      await api(`/api/libraries/${library.id}/remove`, {});
      location.hash = "#/libraries";
    }
  } catch (error) {
    if (name.startsWith("save")) saveError = error.message;
    else if (name === "delete-leftovers") leftoverError = error.message;
    else formError = error.message;
  }
  await tick();
}

document.addEventListener("click", async (event) => {
  if (event.target.closest("#log > summary")) {
    // The click comes before the box opens or closes, so this is what it's about to be.
    logOpen = !document.getElementById("log").open;
    logScroll = { top: 0, pinned: true };
    return;
  }
  if (event.target.closest("[data-wizard-close]")) { wizardDialog.close(); return; }
  if (event.target.closest("[data-wizard-back]")) {
    Object.assign(wizard, { step: "address", error: "" });
    drawWizard();
    wizardDialog.querySelector("input")?.focus();
    return;
  }
  const folder = event.target.closest("[data-folder]");
  if (folder) { await openFolder(folder.dataset.folder); render(); return; }
  const pick = event.target.closest("[data-pick]");
  if (pick) {
    pick.disabled = true;
    formError = "";
    try {
      const library = await api("/api/libraries", { path: pick.dataset.pick });
      location.hash = `#/add/${library.id}`;
      if (!library.scan.summary) await api(`/api/libraries/${library.id}/read`, {});
    } catch (error) {
      formError = error.message;
    }
    await tick();
    return;
  }
  const library = currentLibrary();
  const group = event.target.closest("[data-open-group]");
  if (group) {
    const name = group.dataset.openGroup;
    if (fix.open.has(name)) fix.open.delete(name);
    else fix.open.add(name);
    render();
    return;
  }
  const filter = event.target.closest("[data-fix-filter]");
  if (filter) {
    Object.assign(fix, { filter: filter.dataset.fixFilter, selected: new Set(), open: new Set(), error: "", notice: "" });
    render();
    return;
  }
  const select = event.target.closest("[data-fix-select]");
  if (select && library) {
    fix.selected = select.dataset.fixSelect === "all" ? new Set(shownGaps(library).map((g) => g.uuid)) : new Set();
    render();
    return;
  }
  const place = event.target.closest("[data-place-index]");
  if (place) {
    fix.place = fix.places[Number(place.dataset.placeIndex)];
    render();
    return;
  }
  const suggest = event.target.closest("[data-suggest]");
  if (suggest && library) {
    const gap = shownGaps(library).find((g) => g.uuid === suggest.dataset.suggest);
    const hint = gap?.suggestion;
    if (hint) await saveFix(library, { uuids: [gap.uuid], latitude: hint.latitude, longitude: hint.longitude, place: `the location of ${hint.from}` });
    return;
  }
  const action = event.target.closest("[data-action]");
  if (action) act(action.dataset.action, action);
});

document.addEventListener("input", (event) => {
  if (event.target.id === "fix-place") fix.draft.name = event.target.value;
  if (event.target.id === "fix-date") fix.draft.date = event.target.value;
  if (event.target.id === "url") draftUrl = event.target.value;
  if (event.target.id === "key") draftKey = event.target.value;
  if (event.target.id === "wizard-url") wizard.url = event.target.value;
  if (event.target.id === "wizard-key") wizard.key = event.target.value;
});

document.addEventListener("change", async (event) => {
  if (event.target.id === "timeZone") { await saveTimeZone(event.target); return; }
  const library = currentLibrary();
  if (event.target.dataset.select !== undefined) {
    if (event.target.checked) fix.selected.add(event.target.dataset.select);
    else fix.selected.delete(event.target.dataset.select);
    event.target.blur();
    render();
    return;
  }
  if (event.target.dataset.selectGroup !== undefined && library) {
    const items = shownGaps(library).filter((g) => g.group === event.target.dataset.selectGroup);
    for (const gap of items) {
      if (event.target.checked) fix.selected.add(gap.uuid);
      else fix.selected.delete(gap.uuid);
    }
    event.target.blur();
    render();
    return;
  }
  if (event.target.id === "fix-date") { event.target.blur(); render(); return; }
  if (!event.target.closest("#choices")) return;
  const current = page();
  const saved = current[0] === "settings" ? state.defaults
    : current[0] === "add" ? wizardChoices(library) : library;
  draft = { values: readChoices(saved) };
  notice = "";
  event.target.blur();
  if (library?.scan.summary) {
    await refreshPreview(library, draft.values.options);
  } else {
    render();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.id === "fix-place") {
    event.preventDefault();
    act("fix-search");
  }
});

document.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.target.dataset.wizard) { await wizardSubmit(event.target); return; }
  if (event.target.id !== "connect") return;
  connectError = "";
  identity = null;
  draftUrl = document.getElementById("url").value;
  try {
    const result = await api("/api/connect", { url: draftUrl, key: draftKey });
    draftKey = "";
    document.activeElement.blur();
    connection = { connected: true, ...result };
    await tick();
    identity = result;
  } catch (error) {
    connectError = error.message;
    document.activeElement.blur();
  }
  render();
});

async function loadLog() {
  let found = await api(`/api/log?after=${logLast}`);
  if (found.last < logLast) {
    // Immigrate restarted, so its log starts again.
    logLines = [];
    found = await api("/api/log?after=0");
  }
  logLast = found.last;
  logLines = logLines.concat(found.lines).slice(-1000);
}

async function tick() {
  state = await api("/api/state");
  lastTick = Date.now();
  const library = currentLibrary();
  mosaicText = library?.job ? await api("/api/mosaic") : "";
  await loadLog().catch(() => {});
  render();
}

window.addEventListener("resize", drawMosaic);
wizardDialog.addEventListener("close", render);
window.addEventListener("hashchange", () => tick().catch(render));

(async function start() {
  await tick();
  await checkConnection();
  if (!connection.connected) openWizard();
  setInterval(() => {
    const busy = state.libraries.some((l) => l.scan.running || l.queued || isRunning(l.job));
    // Quiet pages still refresh now and then, so a scheduled sync shows up.
    if (busy || Date.now() - lastTick > 15000) tick().catch(() => {});
  }, 1000);
})();
