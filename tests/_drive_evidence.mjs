// What the page driver records, passively, so a hang or a page that never drew leaves evidence in its timeout dump instead of silence
// (the driver renders the block into its timeout txt and onto stderr). Nothing here sends a command, polls or touches CDP: the driver hands in
// the events it already receives, a clock, and (at the timeout) the reads it started; the one timer is collectExtra's deadline. Every
// part of the block has a budget of its own, so no part crowds another out, and the whole block is cut at EVIDENCE_MAX with its end marker kept.

export const DIALOG_MAX = 12;       // dialogs kept (newest)
export const RING_MAX = 50;         // Document / Script / Stylesheet requests, plus any failed load, kept (newest)
export const FAILED_MAX = 20;       // failed loads kept in a ring the ordinary one cannot evict (newest)
export const SW_EVENT_MAX = 12;     // service-worker status events kept (newest)
export const TARGET_MAX = 12;       // targets listed
export const LINE_MAX = 130;        // one ring or target line
export const EVIDENCE_MAX = 16000;  // the whole block
export const SHELL_LINES_MAX = 60;  // the shell-cache section, lines
export const SHELL_CHARS_MAX = 4500; // the shell-cache section, characters
export const SHELL_NAMES_MAX = 16;  // assets read per shell cache
export const SHELL_HTML_BYTES = 65536; // the cached '/' markup is read up to here, to find the asset names
export const EVIDENCE_DEADLINE_MS = 4000; // every read at the timeout shares this one deadline, whatever the wait scale
const LATE = "not collected in time";
const END_MARK = "--- end drive evidence ---";
const KINDS = new Set(["Document", "Script", "Stylesheet"]);
const clip = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "~" : s; };
const path = (u) => clip(String(u ?? "").replace(/^[a-z][a-z0-9+.-]*:\/\/[^/]+/i, ""), 90);

// a CDP frame as an object, or null: a malformed frame is ignored by whoever listens
export const parseFrame = (data) => { try { const d = JSON.parse(data); return d && typeof d === "object" ? d : null; } catch { return null; } };
// whether a frame is the page speaking (an event of the page, not a reply to the driver's own command and not a service-worker status event)
export const pageSpoke = (d) => !!(d && d.method) && !/^ServiceWorker\./.test(d.method);

// The reads at the timeout run together under one deadline: each part is a promise already started by the caller; the answer holds what had
// finished by the deadline and a marker for the rest (a rejected part says why). A part still running after the deadline is left to its own bound.
export async function collectExtra({ targets, page, shell }, deadlineMs = EVIDENCE_DEADLINE_MS) {
  const got = {};
  const take = (k, p) => Promise.resolve(p).then((v) => { got[k] = { v }; }, (e) => { got[k] = { why: String((e && e.message) || e).slice(0, 150) }; });
  let timer; const deadline = new Promise((res) => { timer = setTimeout(res, deadlineMs); });
  try { await Promise.race([Promise.allSettled([take("targets", targets), take("page", page), take("shell", shell)]), deadline]); }
  finally { clearTimeout(timer); }
  const out = {};
  for (const [k, key] of [["targets", "error"], ["page", "error"], ["shell", "note"]]) {
    const g = got[k];
    out[k] = g ? ("v" in g ? g.v : { [key]: k === "shell" ? "the read failed: " + g.why : g.why }) : { [key]: LATE };
  }
  return out;
}

// installed with Page.addScriptToEvaluateOnNewDocument before the first navigation: a script or stylesheet that fails to load fires `error`
// on its element (it does not bubble, so the listener is a capture one); the list is small and each entry short
export const LOAD_ERRORS_SCRIPT = "(() => { try { addEventListener('error', (e) => { try { const t = e.target; if (!t || (t.tagName !== 'SCRIPT' && t.tagName !== 'LINK')) return;"
  + " const a = (window.__loadErrors = window.__loadErrors || []); if (a.length < 10) a.push(t.tagName + ' ' + String(t.src || t.href || '').slice(0, 150)); } catch (_) {} }, true); } catch (_) {} })();";

// evaluated inside the page origin's service worker (read-only: cache storage reads, no fetch): for each of the newest shell caches, the
// entries for the assets that cache's own '/' markup names, as [name, status, size] or [name, "MISSING"]. No asset body is read: the size is
// the stored content-length header, or null when there is none. Only the cached '/' markup is read, from its stream, up to a byte cap.
export const SHELL_CACHE_PROBE = "(async () => { const out = []; const names = (await caches.keys()).filter((n) => n.startsWith('levain-shell-v')).slice(-3);"
  + ` const capped = async (r) => { if (!r || !r.body || !r.body.getReader) return ''; const rd = r.body.getReader(); const dec = new TextDecoder(); let s = '', n = 0;`
  + ` try { while (n < ${SHELL_HTML_BYTES}) { const x = await rd.read(); if (x.done) break; n += x.value.byteLength; s += dec.decode(x.value, { stream: true }); } } finally { try { await rd.cancel(); } catch (_) {} } return s; };`
  + " for (const n of names) { const c = await caches.open(n); const page = await c.match('/'); const html = await capped(page);"
  + ` const re = /(?:src|href)=["']([^"']+\\.(?:js|css)[^"']*)["']/g; const all = []; let m; while ((m = re.exec(html)) && all.length < 400) all.push(m[1]);`
  + ` const named = all.filter((k) => /\\.js(?:[?#]|$)/.test(k)).concat(all.filter((k) => !/\\.js(?:[?#]|$)/.test(k))).slice(0, ${SHELL_NAMES_MAX});`
  + " const entries = []; for (const k of named) { const h = await c.match(k); const cl = h && h.headers && h.headers.get ? h.headers.get('content-length') : null;"
  + " entries.push(h ? [k.slice(0, 120), h.status, cl !== null && cl !== undefined && /^[0-9]+$/.test(String(cl)) ? Number(cl) : null] : [k.slice(0, 120), 'MISSING']); }"
  + " out.push({ cache: n.slice(0, 60), root: page ? page.status : 'none', total: all.length, entries }); } return JSON.stringify({ caches: names.length, shell: out }); })()";

export function createEvidence({ now = Date.now, t0 = now() } = {}) {
  const rel = () => `+${Math.max(0, now() - t0)}ms`;
  const dialogs = [];
  const replyOf = new Map();            // command id -> { dialog, sent }
  const ring = new Map();               // requestId -> entry, in arrival order
  const failed = new Map();             // requestId -> entry, a failed load (loadingFailed or a status of 400 and up), kept apart from the ring
  const swEvents = [];
  let resends = 0;
  const entryFor = (rid, type, url) => {
    let e = ring.get(rid) || failed.get(rid);
    if (!e) {
      e = { type: type || "?", url: "", at: rel(), status: null, sw: false, err: null, canceled: false, blocked: null, finished: false };
      ring.set(rid, e);
      while (ring.size > RING_MAX) ring.delete(ring.keys().next().value);
    }
    if (type) e.type = type;
    if (url) e.url = path(url);
    return e;
  };
  const markFailed = (rid, e) => {
    failed.delete(rid); failed.set(rid, e);
    while (failed.size > FAILED_MAX) failed.delete(failed.keys().next().value);
  };
  const onMessage = (d) => {
    try {
      const p = d.params || {};
      switch (d.method) {
        case "Page.javascriptDialogOpening":
          dialogs.push({ type: String(p.type), message: clip(p.message, 60), opened: rel(), closed: null, accepted: null, sent: null, reply: null });
          if (dialogs.length > DIALOG_MAX) dialogs.shift();
          break;
        case "Page.javascriptDialogClosed": {
          const open = [...dialogs].reverse().find((x) => x.closed === null);
          if (open) { open.closed = rel(); open.accepted = p.result; }
          break;
        }
        case "Network.requestWillBeSent":
          if (KINDS.has(p.type)) entryFor(p.requestId, p.type, p.request && p.request.url);
          break;
        case "Network.responseReceived":
          if (KINDS.has(p.type) || ring.has(p.requestId)) {
            const e = entryFor(p.requestId, p.type, p.response && p.response.url);
            e.status = p.response && p.response.status; e.sw = !!(p.response && p.response.fromServiceWorker);
            if (e.status >= 400) markFailed(p.requestId, e);
          }
          break;
        case "Network.loadingFinished":
          if (ring.has(p.requestId)) ring.get(p.requestId).finished = true;
          break;
        case "Network.loadingFailed": {
          const e = entryFor(p.requestId, p.type, null);
          e.err = clip(p.errorText || "failed", 60); e.canceled = !!p.canceled; e.blocked = p.blockedReason ? clip(p.blockedReason, 30) : null;
          if (!p.canceled) markFailed(p.requestId, e);   // a reload cancels the old document's loads as a matter of course: those stay in the ordinary ring and never push out a real failure
          break;
        }
        case "ServiceWorker.workerVersionUpdated":
          for (const v of (p.versions || [])) swEvents.push(`${rel()} version ${clip(v.versionId, 12)} ${clip(v.runningStatus, 20)}/${clip(v.status, 20)} ${path(v.scriptURL)}`);
          while (swEvents.length > SW_EVENT_MAX) swEvents.shift();
          break;
        default:
          if (d.id !== undefined && replyOf.has(d.id)) {
            const { dialog, sent } = replyOf.get(d.id); replyOf.delete(d.id);
            const took = Math.max(0, now() - sent);
            dialog.reply = d.error ? `error ${clip(d.error.message || JSON.stringify(d.error), 60)} after ${took}ms` : `ok after ${took}ms`;
          }
      }
    } catch { /* a malformed event is never a reason to lose the others */ }
  };
  // the driver sent Page.handleJavaScriptDialog as command `id` for the dialog that opened last
  const dialogReplySent = (id) => {
    const dialog = dialogs[dialogs.length - 1];
    if (!dialog) return;
    dialog.sent = rel(); replyOf.set(id, { dialog, sent: now() });
    if (replyOf.size > DIALOG_MAX * 2) replyOf.delete(replyOf.keys().next().value);
  };
  const resendFired = () => { resends++; return resends; };
  return { onMessage, dialogReplySent, resendFired, resends: () => resends, dialogs, ring, failed, swEvents };
}

const ringLine = (e) => clip(`${e.at} ${e.type} ${e.url} `
  + (e.err ? `FAILED ${e.err}${e.canceled ? " canceled" : ""}${e.blocked ? " blocked=" + e.blocked : ""}`
    : e.status !== null ? `${e.status}${e.sw ? " via-sw" : ""}` : e.finished ? "finished" : "no response"), LINE_MAX);

// extra, what the caller read at the timeout:
//   targets: { source, list: [{ type, url, title, attached }] } | { error }
//   page:    { loadErrors } | { error }
//   shell:   the parsed SHELL_CACHE_PROBE answer | { note }
export function renderEvidence(ev, extra = {}) {
  const out = ["--- drive evidence ---", `resend fired: ${ev.resends()}`];
  out.push(`DIALOGS (${ev.dialogs.length}, newest kept):`);
  for (const d of ev.dialogs) out.push(clip(`  ${d.opened} ${d.type} "${d.message}" closed ${d.closed ?? "never"}${d.accepted === null ? "" : ` accepted=${d.accepted}`}; handleJavaScriptDialog sent ${d.sent ?? "never"}, reply ${d.reply ?? "none"}`, 200));
  out.push(`FAILED LOADS (${ev.failed.size} kept, newest ${FAILED_MAX}; a status of 400 and up counts):`);
  for (const e of ev.failed.values()) out.push("  " + ringLine(e));
  const ordinary = [...ev.ring.entries()].filter(([rid]) => !ev.failed.has(rid)).map(([, e]) => e);
  out.push(`REQUESTS (Document, Script, Stylesheet; ${ordinary.length} not listed above, of the newest ${RING_MAX}):`);
  for (const e of ordinary) out.push("  " + ringLine(e));
  out.push(`SERVICE WORKER EVENTS (${ev.swEvents.length}):`);
  for (const s of ev.swEvents) out.push("  " + clip(s, LINE_MAX));
  const t = extra.targets;
  if (!t) out.push("TARGETS: not read");
  else if (t.error) out.push("TARGETS: " + clip(t.error, 200));
  else {
    out.push(`TARGETS (${t.source}; ${t.list.length} listed):`);
    for (const x of t.list.slice(0, TARGET_MAX)) out.push("  " + clip(`${x.type} attached=${x.attached ?? "?"} url=${JSON.stringify(clip(x.url, 70))} title=${JSON.stringify(clip(x.title, 30))}`, LINE_MAX));
    if (t.list.length > TARGET_MAX) out.push(`  (+${t.list.length - TARGET_MAX} more)`);
  }
  const pg = extra.page;
  out.push("window.__loadErrors: " + (!pg ? "not read" : pg.error ? "page did not answer: " + clip(pg.error, 120) : clip(JSON.stringify(pg.loadErrors === undefined ? null : pg.loadErrors), 600)));
  const sh = extra.shell;
  if (!sh) out.push("SHELL CACHE: not read");
  else if (sh.note) out.push("SHELL CACHE: " + clip(sh.note, 200));
  else {
    const lines = [`SHELL CACHE (${sh.caches} shell caches; entries the cache's own '/' names; commit in flight: not read):`];
    for (const c of (sh.shell || []).slice(0, 3)) {
      lines.push("  " + clip(`${c.cache} '/' status=${c.root} names ${(c.entries || []).length} of ${Number.isFinite(c.total) ? c.total : (c.entries || []).length}`, LINE_MAX));
      for (const [name, status, bytes] of (c.entries || []).slice(0, SHELL_NAMES_MAX)) {
        lines.push("    " + clip(`${path(name)} ${status}${status === "MISSING" ? "" : typeof bytes === "number" ? " " + bytes + "B" : " size not read"}`, LINE_MAX));
      }
    }
    // the section's own budget, in lines and in characters
    let used = 0; const kept = [];
    for (const l of lines) { if (kept.length >= SHELL_LINES_MAX || used + l.length + 1 > SHELL_CHARS_MAX) break; kept.push(l); used += l.length + 1; }
    out.push(...kept);
    if (kept.length < lines.length) out.push(`  (+${lines.length - kept.length} shell cache lines not shown)`);
  }
  out.push(END_MARK);
  const text = out.join("\n");
  if (text.length <= EVIDENCE_MAX) return text;
  const tail = "\n[evidence cut]\n" + END_MARK;
  return text.slice(0, EVIDENCE_MAX - tail.length) + tail;
}
