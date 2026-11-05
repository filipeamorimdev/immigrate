"use strict";

const SCHEDULES = [[0, "Off"], [15, "Every 15 minutes"], [60, "Every hour"], [360, "Every 6 hours"], [1440, "Every day"]];
const EVERY = { 15: "every 15 minutes", 60: "every hour", 360: "every 6 hours", 1440: "once a day" };
const RUNNING = ["queued", "uploading", "albums"];
const view = document.getElementById("view");
let state = null;
let identity = null;
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
let mosaicText = "";
let lastPage = "";
let lastTick = 0;
let timeZones = null; // the names Settings offers, loaded when it opens
let timeZonesLoading = false;
let zoneNotice = "";
let zoneError = "";
const zoneSupport = {};
const reports = {};
const missing = {};

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
const isRunning = (job) => !!job && RUNNING.includes(job.phase);

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

function page() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (!state.hasKey) return ["connect"];
  if (parts[0] === "connect") return setupDone() ? ["settings"] : ["add"];
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
  tabs.hidden = !setupDone();
  const settings = current[0] === "settings";
  tabs.innerHTML = `<a href="#/libraries" ${settings ? "" : 'aria-current="page"'}>Libraries</a>
    <a href="#/settings" ${settings ? 'aria-current="page"' : ""}>Settings</a>`;

  const wizard = ["connect", "add"].includes(current[0]);
  steps.hidden = !wizard;
  if (!wizard) return;
  const names = ["Connect to Immich", "Choose library", "Read library", "Choose what to send"];
  let index = current[0] === "connect" ? 0 : !current[1] ? 1 : current[2] === "choose" ? 3 : 2;
  const shown = setupDone() ? names.slice(1) : names;
  if (setupDone()) index -= 1;
  steps.innerHTML = shown.map((name, i) =>
    `<li class="${i < index ? "done" : ""}" ${i === index ? 'aria-current="step"' : ""}>${i + 1}. ${name}</li>`).join("");
}

const figure = (value, label) => `<div class="figure"><strong>${value}</strong><span>${label}</span></div>`;
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
      ${identity ? `<div class="note good"><p>Connected as ${escapeHtml(identity.name)}, Immich ${escapeHtml(identity.version)}. Saved.</p></div>` : ""}
      <div class="actions"><button class="button primary" type="submit">Connect</button></div>
    </form>`;
}

function renderSetupConnect() {
  return `<h2>Connect to Immich</h2>
    <p class="lead">Photos upload to the account that owns this API key. Immigrate saves the connection once Immich accepts it.</p>
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
      return `<li><button class="folder" data-folder="${escapeHtml(folder.path)}">${escapeHtml(folder.name)}</button></li>`;
    }
    const library = added.get(folder.path);
    const action = library && isSetUp(library)
      ? `<a class="button" href="#/library/${library.id}">Open</a>`
      : `<button class="button primary" data-pick="${escapeHtml(folder.path)}">Use this library</button>`;
    return `<li class="library"><span>${escapeHtml(folder.name)}<small>${library && isSetUp(library) ? "Already added" : "Photos library"}</small></span>${action}</li>`;
  }).join("");
  const empty = folders.folders.length ? "" : `<li class="empty">There are no folders here.</li>`;
  const more = folders.more ? `<li class="empty">And ${plural(folders.more, "more folder")}.</li>` : "";
  return `<nav class="crumbs" aria-label="Folder">${crumbs}</nav>
    ${errorLine(folderError)}
    <ul class="folders">${rows}${empty}${more}</ul>`;
}

function renderWizardLibrary() {
  return `<h2>Choose a Photos library</h2>
    <p class="lead">Open the folders on your NAS until you find the library. Its name ends in .photoslibrary. Immigrate only reads it.</p>
    ${renderFolders()}
    ${errorLine(formError)}
    ${setupDone() ? `<div class="actions"><a class="button" href="#/libraries">Cancel</a></div>` : ""}`;
}

function renderSummary(summary) {
  return `<div class="figures">
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
    return `<h2>Reading ${escapeHtml(library.name)}</h2>
      <p class="lead">${total ? `${count(done)} of ${count(total)} items read.` : "Opening the Photos database. Large libraries can take a few minutes."}</p>`;
  }
  if (!summary) {
    return `<h2>Read ${escapeHtml(library.name)}</h2>
      <p class="lead">Immigrate reads ${escapeHtml(library.path)} and doesn't change it.</p>
      ${error ? `<div class="note bad"><p>${escapeHtml(error)}</p></div>` : ""}
      ${errorLine(formError)}
      <div class="actions"><button class="button primary" data-action="read">Read library</button>
        <button class="button" data-action="back">Back</button>${leave}</div>`;
  }
  const before = isSetUp(library)
    ? " Immigrate has synced this library before, so items already in Immich won't be sent again." : "";
  return `<h2>${plural(summary.total, "item")} in ${escapeHtml(library.name)}</h2>
    <p class="lead">Read ${when(summary.scannedAt)}.${before}</p>
    ${renderSummary(summary)}
    ${renderMissing(library)}
    ${errorLine(formError)}
    <div class="actions">
      <a class="button primary" href="#/add/${library.id}/choose">Choose what to send</a>
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

function radio(values, name, value, title, detail) {
  return `<label class="choice"><input type="radio" name="${name}" value="${value}" ${values.options[name] === value ? "checked" : ""}>
    <span>${title}</span><small>${detail}</small></label>`;
}
function checkbox(values, name, title, detail) {
  return `<label class="choice"><input type="checkbox" name="${name}" ${values.options[name] ? "checked" : ""}>
    <span>${title}</span><small>${detail}</small></label>`;
}

function renderChoices(values) {
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
      <fieldset><legend>Keep in sync</legend>
        <label class="field"><small>Immigrate checks the library on this schedule and sends anything new or changed.</small>
          <select name="autoMinutes">${SCHEDULES.map(([value, text]) =>
            `<option value="${value}" ${values.autoMinutes === value ? "selected" : ""}>${text}</option>`).join("")}</select>
        </label>
      </fieldset>
    </form>`;
}

function readChoices(base) {
  const form = document.getElementById("choices");
  const data = new FormData(form);
  return {
    options: { ...base.options, edits: data.get("edits"), hidden: data.get("hidden"),
      albums: data.has("albums"), people_as_tags: data.has("people_as_tags") },
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
      ${figure(count(result.newAlbums), "new albums")}
    </div>
    <p class="lead">Immich needs extra space for thumbnails and transcoded videos on top of this.</p>` : "";
  return `<h2>Choose what to send</h2>
    <p class="lead">These start from the defaults in Settings. They're saved for ${escapeHtml(library.name)} when you start the sync, and you can change them later on its page. Nothing is deleted. Photos you remove from Apple Photos stay in Immich.</p>
    ${renderChoices(values)}
    ${summary}
    ${errorLine(formError)}
    <div class="actions"><button class="button primary" data-action="start">Start sync</button>
      <a class="button" href="#/add/${library.id}">Back</a></div>`;
}

const PHASES = {
  queued: "Starting", uploading: "Sending photos", albums: "Building albums",
  finished: "Last synced", stopped: "Last sync stopped", failed: "Last sync stopped early",
};

function statusLine(library) {
  const job = library.job;
  if (isRunning(job)) {
    return job.phase === "albums"
      ? `Building albums: ${count(job.albumsDone)} of ${count(job.albumsTotal)}.`
      : `Sending: ${count(job.total - job.waiting)} of ${count(job.total)} items checked, ${count(job.done)} sent.`;
  }
  const { running, done, total } = library.scan;
  if (running) return total ? `Reading the library: ${count(done)} of ${count(total)} items.` : "Reading the library.";
  if (library.queued) return "Starting the sync.";
  const last = library.lastSync;
  if (!last) return library.scan.summary ? "Read, but not synced yet." : "Not synced yet.";
  const date = last.finishedAt ? ` ${when(last.finishedAt)}` : "";
  const failed = last.failed ? `, ${count(last.failed)} failed` : "";
  return `${PHASES[last.phase]}${date}: ${count(last.done)} sent, ${count(last.skipped)} already in Immich${failed}.`;
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

function renderDashboard() {
  const cards = state.libraries.map((library) => `<li class="card">
      <h3><a href="#/library/${library.id}">${escapeHtml(library.name)}</a></h3>
      <p class="muted path">${escapeHtml(library.path)}</p>
      <p>${statusLine(library)}</p>
      <p class="muted">${scheduleLine(library)}</p>
      <div class="actions">${syncButton(library)}<a class="button" href="#/library/${library.id}">Open</a></div>
    </li>`).join("");
  return `<h2>Libraries</h2>
    <p class="lead">Sending to ${escapeHtml(state.immichUrl)}. Open a library to see its details or change its settings.</p>
    <ul class="cards">${cards}</ul>
    ${errorLine(formError)}
    <div class="actions"><a class="button" href="#/add">Add a library</a></div>`;
}

function renderActivity(library) {
  const job = library.job;
  const run = job || library.lastSync;
  if (library.scan.running || library.queued) {
    return `<h3 class="section">${library.queued ? "Reading, then syncing" : "Reading the library"}</h3>
      <p class="lead">${statusLine(library)}</p>`;
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
    <p class="lead">${escapeHtml(library.path)}<br>${scheduleLine(library)}</p>
    ${renderActivity(library)}
    ${renderCheck(library)}
    ${summary ? `<h3 class="section">In the library</h3>
      <p class="lead">${plural(summary.total, "item")}, read ${when(summary.scannedAt)}.</p>
      ${renderSummary(summary)}${renderMissing(library)}` : ""}
    <h3 class="section">Settings for this library</h3>
    <p class="lead">Every sync of ${escapeHtml(library.name)} uses these, by hand or on the schedule. Libraries you add later start from the defaults in <a href="#/settings">Settings</a>.</p>
    ${renderChoices(values)}
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
    <h3 class="section first">Immich connection</h3>
    <p class="lead">Every library uploads to the account that owns this API key. Immigrate saves the connection once Immich accepts it.</p>
    ${renderConnectForm()}
    <h3 class="section">Time zone</h3>
    ${renderTimeZone()}
    <h3 class="section">Defaults for new libraries</h3>
    <p class="lead">A library you add starts with these choices. Changing them doesn't change libraries you've already added. Change those on each library's page.</p>
    ${renderChoices(values)}
    ${saveBar("save-defaults", saved)}`;
}

function renderPage(current) {
  const library = currentLibrary();
  switch (current[0]) {
    case "connect": return renderSetupConnect();
    case "add":
      if (!library) return renderWizardLibrary();
      return current[2] === "choose" && library.scan.summary ? renderWizardChoose(library) : renderWizardRead(library);
    case "library": return library ? renderLibrary(library) : renderDashboard();
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
    window.scrollTo(0, 0);
  }
  if (view.contains(document.activeElement) && document.activeElement.matches("input, select")) {
    drawMosaic();
    return;
  }
  renderNav(current);
  view.innerHTML = renderPage(current);
  drawMosaic();
  loadPageData(current);
}

async function openFolder(path) {
  try {
    folders = await api(`/api/folders?path=${encodeURIComponent(path)}`);
    folderError = "";
  } catch (error) {
    folderError = error.message;
  }
}

async function loadFolders() {
  foldersLoading = true;
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

function loadPageData(current) {
  const library = currentLibrary();
  if (current[0] === "settings" && !timeZones && !timeZonesLoading) loadTimeZones();
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
  const action = event.target.closest("[data-action]");
  if (action) act(action.dataset.action, action);
});

document.addEventListener("input", (event) => {
  if (event.target.id === "url") draftUrl = event.target.value;
  if (event.target.id === "key") draftKey = event.target.value;
});

document.addEventListener("change", async (event) => {
  if (event.target.id === "timeZone") { await saveTimeZone(event.target); return; }
  if (!event.target.closest("#choices")) return;
  const current = page();
  const library = currentLibrary();
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

document.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.target.id !== "connect") return;
  connectError = "";
  identity = null;
  draftUrl = document.getElementById("url").value;
  try {
    const result = await api("/api/connect", { url: draftUrl, key: draftKey });
    draftKey = "";
    document.activeElement.blur();
    if (page()[0] === "connect") location.hash = "#/add";
    await tick();
    identity = result;
  } catch (error) {
    connectError = error.message;
    document.activeElement.blur();
  }
  render();
});

async function tick() {
  state = await api("/api/state");
  lastTick = Date.now();
  const library = currentLibrary();
  mosaicText = library?.job ? await api("/api/mosaic") : "";
  render();
}

window.addEventListener("resize", drawMosaic);
window.addEventListener("hashchange", () => tick().catch(render));

(async function start() {
  await tick();
  setInterval(() => {
    const busy = state.libraries.some((l) => l.scan.running || l.queued || isRunning(l.job));
    // Quiet pages still refresh now and then, so a scheduled sync shows up.
    if (busy || Date.now() - lastTick > 15000) tick().catch(() => {});
  }, 1000);
})();
