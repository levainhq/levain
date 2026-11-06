// levain room_view.js — the cockpit's one Claude room (RM-F).
//
// A conversation with the room over GET /cockpit/room.json and POST /cockpit/room/*: a draft box with Send, Drop and
// Stop, the room's cards (a drop it proposed, which only YOUR tap commits), the reconcile of an ambiguous line, and a
// dictation control where the browser has one. The page decides nothing: what may be sent, whether a line arrived and
// whether a card became a drop are the server's words, drawn as given. Every word that came from the record is set with
// textContent, never as markup. The stream is drawn in the order the server sent (gseq); nothing here sorts it.
//
// A card's words are bytes you sent: the field holds your quoted message (editable), and the room's own summary is a
// separate, labelled note that is information and never your words. Dictation runs only while this page is in the
// foreground: it fills the draft, you read it, and Send or Drop works exactly as typing. No audio reaches Levain.
(function (root) {
  "use strict";

  var ROOM_PATH = "/cockpit/room.json";
  var POST_PREFIX = "/cockpit/room/";
  var SEND_WORDS = {
    accepted: "Sent.",
    ambiguous: "Sent, but it may not have arrived. It is not sent again; you can say whether it did."
  };
  var STOP_WORDS = {
    confirmed: "Stopped.",
    no_such_request: "Nothing was running to stop.",
    ambiguous: "The stop was sent; it was not confirmed.",
    rejected: "The stop was refused."
  };
  var MARK_WORDS = { accepted: "Marked: it arrived.", not_delivered: "Marked: it did not arrive." };
  var COMPOSER_WORDS = {
    none: "The room starts when you send the first line.",
    ended: "The room starts again when you send a line.",
    no_session: "The room starts again when you send a line.",
    not_live: "The room is not available right now.",
    checking: "The room is getting ready.",
    writable: ""
  };
  var SEND_STATES = { none: 1, ended: 1, no_session: 1, writable: 1 };
  var STOP_STATES = { writable: 1, checking: 1, not_live: 1 };
  var COLLAPSED = { vendor_tool_call: 1, vendor_tool_result: 1 };
  var HIDDEN = { vendor_echo: 1, vendor_turn_started: 1, vendor_turn_ended: 1, vendor_user_item: 1 };

  function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function peek(text) {
    var first = String(text || "").split("\n")[0].trim();
    return first.length > 120 ? first.slice(0, 120) + "..." : first;
  }

  var WRAPPED = /^\s*<tool_use_error>([\s\S]*?)<\/tool_use_error>([\s\S]*)$/;
  // Only the vendor's own error wrapper is removed: a body that opens with <tool_use_error>inner</tool_use_error> is its inner
  // text (then any text after the tag, on a new line) and the tag's name. Every other body, including one that is a single
  // other tag pair (a file or a command's output), is drawn exactly as recorded.
  function unwrap(text) {
    var m = WRAPPED.exec(text);
    if (!m) return { text: text, tag: "" };
    var rest = m[2].trim();
    return { text: m[1].trim() + (rest ? "\n" + rest : ""), tag: "tool_use_error" };
  }
  // the vendor's own refusal sentences, matched at the start of the unwrapped text (not a word anywhere in it). The first two
  // are read only from a body the vendor wrapped as an error (``tagged``): a tool that ran and printed such a sentence is not a
  // refusal. The vendor sends the outside-the-cwd refusal with no wrapper, and no tool result is worded like it, so it is read
  // from its words alone. A Read's own result opens with a line number, so a file's text never starts with one of these.
  var REFUSED = [
    { what: "read", tagged: true, re: /^File is in a directory that is denied by your permission settings/ },
    { what: "tool", tagged: true, re: /^Permission to use [^\n]{1,100} has been denied/ },
    { what: "read", tagged: false, re: /^[^\n;]{1,1000} is outside [^\n;]{1,1000}; the permissions\.blockReadsOutsideWorkingDirectories setting blocks reads outside the working directories/ },
    // the room's own read-guard hook (levain/coordinator/room_readguard.py ROOM_READ_REFUSAL): the vendor wraps a hook's deny
    // as "PreToolUse:<Tool> hook error: <reason>", untagged (recorded: tests/fixtures/claude_stream/room_hook_denied_read.jsonl).
    // The WHOLE sentence and the end of the text: another hook's reason that opens the same way is drawn as it is.
    { what: "read", tagged: false, re: /^PreToolUse:(?:Read|Grep|Glob) hook error: That is outside the folder this room can read, so nothing was read\. Tell the user so in plain words; do not suggest commands or ways to change what the room can read\.$/ }
  ];
  // what a refusal says on the page: plain words, never the vendor's sentence (it names a path and a settings key)
  var REFUSAL_HEAD = { read: "A read was refused", tool: "A tool was refused" };
  var REFUSAL_TEXT = { read: "That is outside what the room can read, so nothing was read.",
                       tool: "That is outside what the room can use, so the tool was not used." };
  // only the head of the text is tested: the vendor's sentences are short (a path can run long), and a very long error body costs nothing
  function refusalOf(text, tag) {
    var head = String(text).slice(0, 2300);
    for (var i = 0; i < REFUSED.length; i++) if ((tag === "tool_use_error" || !REFUSED[i].tagged) && REFUSED[i].re.test(head)) return REFUSED[i].what;
    return "";
  }
  // the head's words for a row: its speaker, else a short sentence for what the row is
  function headWords(l) {
    if (l.who) return l.who;
    if (l.kind === "vendor_tool_result") {
      if (l.refusal) return REFUSAL_HEAD[l.refusal];
      if (l.tag === "tool_use_error") return "A tool failed";
      return "A tool result";
    }
    if (l.tool) return l.tool.open ? "A tool is running" : "A tool ran";
    return l.kind === "vendor_error" ? "Error" : l.kind.replace(/_/g, " ");
  }

  // A Read's result: the vendor numbers every line (``cat -n``: a right-aligned number, a tab, the line). The number is the
  // vendor's formatting and not the file's text, so it is not drawn. Only a result of the tool named "Read" is read this way
  // (``tool_name``, the vendor's own name, kept on the row); a result of any other tool is drawn as recorded, and a row stored
  // before the name was kept (null) falls back to the shape: the leading run of lines that open with a number and a tab, each
  // one more than the last. The run ends at the first line that is not the next number; an error body and a refusal are never
  // read as a file. Display only: the record is not changed.
  var NUMBERED = /^ *(\d+)\t/;
  function stripsNumbers(it) { return it.tool_name == null || it.tool_name === "Read"; }
  function withoutLineNumbers(text) {
    var lines = text.split("\n"), n = 0, prev = -1;
    for (; n < lines.length; n++) {
      var m = NUMBERED.exec(lines[n]);
      if (!m || (prev >= 0 && Number(m[1]) !== prev + 1)) break;
      prev = Number(m[1]);
    }
    if (n === 0) return text;
    for (var i = 0; i < n; i++) lines[i] = lines[i].replace(NUMBERED, "");
    return lines.join("\n");
  }

  function lineModel(it) {
    var you = it.kind === "message" && it.source === "operator";
    var room = it.kind === "vendor_agent_text";
    // the session reports its permission mode on every turn as an error-kind row; the expected mode is not an error, and a
    // row that reads "Error" on every turn trains the eye past the one that is (any other mode is still drawn)
    var modeNoise = it.kind === "vendor_error" && it.body === "dontAsk";
    var text = it.body == null ? "" : String(it.body);
    // a tool's result arrives wrapped in its vendor's tag (e.g. <tool_use_error>...</tool_use_error>); the tag is read for
    // the head's words and the text is drawn without it, as text like every other word here
    var tag = "";
    var refusal = "";
    if (it.kind === "vendor_tool_result") {
      var un = unwrap(text); text = un.text; tag = un.tag;
      refusal = refusalOf(text, tag);
      if (refusal) text = REFUSAL_TEXT[refusal];
      else if (!tag && stripsNumbers(it)) text = withoutLineNumbers(text);
    }
    // a hook of the operator's own settings starting in the child (body e.g. "SessionStart:startup") and one that ended
    // in success are recorded as error-kind rows too (claude_session._on_hook, ref "error:hook_started:<id>"); they are
    // not errors (roomfix2, seen on a real open). A hook that ended any other way, and every other error, is drawn.
    var ref = it.ref == null ? "" : String(it.ref);
    var hookNoise = it.kind === "vendor_error" && (ref.indexOf("error:hook_started:") === 0 ||
      (ref.indexOf("error:hook_response:") === 0 && / success$/.test(text)));
    if (it.kind === "vendor_approval_request") text = "The room asked for something it cannot have; nothing was granted.";
    if (it.kind === "vendor_approval_resolved") text = "";
    // a call that has finished and has no words of its own is an empty chip: the result row beside it shows the outcome.
    // A call still open keeps its row, because that it is running is live information; so does one with no tool facts at all
    // (it is hidden only on an explicit completion, never on missing state).
    var emptyDoneCall = it.kind === "vendor_tool_call" && !text && !!it.tool && it.tool.open === false;
    return {
      gseq: it.gseq, ref: ref, at: it.at || "",
      who: you ? "You" : (room ? "The room" : ""), cls: you ? "rv-you" : (room ? "rv-room" : (it.kind === "vendor_error" ? "rv-error" : "")),
      kind: String(it.kind), text: text, collapsible: own(COLLAPSED, it.kind), hidden: own(HIDDEN, it.kind) || modeNoise || hookNoise || emptyDoneCall,
      tag: tag, refusal: refusal, rich: room,
      ambiguous: it.kind === "message" && it.status === "ambiguous",
      truncated: it.truncated === true, tool: it.tool ? { open: it.tool.open === true } : null
    };
  }

  // Whether a turn is running: the composer says nothing of it (an idle room and a running one are both "writable"), so it
  // is read from the turn rows the server sends in the stream (hidden from the page, still sent). Only the CURRENT launch's turn
  // rows count (the server names it, body.launch_id): a late row of an older or a worker launch decides nothing about it. Its
  // latest turn row is a start with no end after it => running. With no turn row of it in the stream and the body's gap flag
  // set, a start may be missing from the record, so the turn is taken as running (unknown is running: Stop stays enabled, and
  // a stop sent in a race still gets the server's "Nothing was running to stop."). No gap and no turn row is an idle room.
  function turnRunning(stream, launchId, gap) {
    if (typeof launchId !== "string" || !launchId) return false;
    var last = "";
    for (var i = 0; i < stream.length; i++) {
      var it = stream[i];
      if (it && it.launch_id === launchId && (it.kind === "vendor_turn_started" || it.kind === "vendor_turn_ended")) last = it.kind;
    }
    return last === "" ? gap === true : last === "vendor_turn_started";
  }

  function roomModel(body) {
    var c = body.composer || null, st = c && own(COMPOSER_WORDS, c.state) ? c.state : "none";
    var thread = typeof body.room_thread === "string" ? body.room_thread : null;
    var running = turnRunning(body.stream || [], body.launch_id, body.gap === true);
    return {
      thread: thread, open: thread !== null, running: running,
      // Stop is for a turn that can be stopped; a stop sent in a race still gets the server's "Nothing was running to stop."
      canSend: thread !== null && own(SEND_STATES, st), canStop: thread !== null && own(STOP_STATES, st) && running,
      state: st, words: thread === null ? "No room is open." : (c && c.why && st !== "writable" ? COMPOSER_WORDS[st] + " (" + c.why + ")" : COMPOSER_WORDS[st]),
      model: typeof body.model === "string" && body.model ? "Model in use: " + body.model : "",
      gap: body.gap === true,
      error: typeof body.room_error === "string" ? body.room_error : "",
      stream: (body.stream || []).map(lineModel).filter(function (l) { return !l.hidden; }),
      proposals: (body.proposals || []).map(function (p) {
        return { id: String(p.proposal_id), state: String(p.state), words: String(p.words == null ? "" : p.words),
                 summary: String(p.summary == null ? "" : p.summary), about: p.about == null ? "" : String(p.about) };
      })
    };
  }

  function sendWords(status, j) {
    if (status === 200 && j && j.receipt && own(SEND_WORDS, j.receipt.state)) return SEND_WORDS[j.receipt.state];
    if (status >= 500) return "The server answered " + status + "; the line may or may not have been sent. Sending again will not duplicate it.";
    if (status === 202) return (j && j.message) || "The line was sent; it is not settled.";
    if (j && j.error === "room_rotated") return "This conversation was replaced; reading the new one. Your draft is kept.";
    if (j && j.message) return "Not sent: " + j.message;
    return "Not sent: the server answered " + status + ".";
  }

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  // ordinary prose for the phone's keyboard (autocorrect, the dictation key); a dash-free name keeps Safari's AutoFill off it
  function prose(node, name) {
    [["name", name], ["autocapitalize", "sentences"], ["autocorrect", "on"], ["spellcheck", "true"], ["inputmode", "text"], ["autocomplete", "off"]]
      .forEach(function (kv) { node.setAttribute(kv[0], kv[1]); });
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // A message's words become DOM here and nowhere as markup: plain text, plus one shape, a fenced block (a line opening with
  // three backticks to the line closing with three), drawn as <pre><code> from the lines between. An unclosed fence (a reply
  // still streaming) stays plain text, backticks and all.
  // The room's own replies (and only those) also get an inline pass over their non-code text, built as elements and never as
  // markup: **bold** (which may hold `code`) and `code`, each within one line. A marker with no partner on its line stays
  // literal, a line longer than LONG_LINE is drawn plain (the pass is quadratic in a line's length), and nothing else is
  // parsed (no links, no images, no HTML, no lists); the words of tools and of the operator get no inline pass at all.
  var LONG_LINE = 20000;
  function closeBold(s, from) {
    var eol = s.indexOf("\n", from);
    if (eol < 0) eol = s.length;
    for (var k = from; k < eol; k++) {
      var c = s.charAt(k);
      if (c === "`") { var e = s.indexOf("`", k + 1); if (e > k + 1 && e < eol) { k = e; continue; } }
      if (c === "*" && s.charAt(k + 1) === "*" && !/\s/.test(s.charAt(k - 1))) return k;
    }
    return -1;
  }
  function inline(doc, target, s, bold) {
    var buf = "", i = 0;
    function flush() { if (buf) { target.appendChild(doc.createTextNode(buf)); buf = ""; } }
    while (i < s.length) {
      var c = s.charAt(i);
      if (c === "`") {
        var e = s.indexOf("`", i + 1);
        if (e > i + 1 && s.slice(i + 1, e).indexOf("\n") < 0) { flush(); target.appendChild(el(doc, "code", "rv-ic", s.slice(i + 1, e))); i = e + 1; continue; }
      } else if (!bold && c === "*" && s.charAt(i + 1) === "*" && s.charAt(i + 2) && !/\s/.test(s.charAt(i + 2))) {
        var j = closeBold(s, i + 2);
        if (j > i + 2) { flush(); var st = el(doc, "strong"); inline(doc, st, s.slice(i + 2, j), true); target.appendChild(st); i = j + 2; continue; }
      }
      buf += c; i++;
    }
    flush();
  }
  // the inline pass one line at a time (no marker pairs across a newline); a line over LONG_LINE is drawn plain
  function richText(doc, node, text) {
    text.split("\n").forEach(function (ln, n) {
      if (n) node.appendChild(doc.createTextNode("\n"));
      if (ln.length > LONG_LINE) node.appendChild(doc.createTextNode(ln)); else inline(doc, node, ln, false);
    });
  }
  function fillText(doc, node, text, rich) {
    var lines = text.split(/\r?\n/), parts = [], cur = [], i = 0;
    while (i < lines.length) {
      if (/^ {0,3}```/.test(lines[i])) {
        var j = i + 1;
        while (j < lines.length && !/^ {0,3}```[ \t]*$/.test(lines[j])) j++;
        if (j < lines.length) {
          if (cur.length) parts.push({ code: false, text: cur.join("\n") });
          parts.push({ code: true, text: lines.slice(i + 1, j).join("\n") });
          cur = []; i = j + 1; continue;
        }
      }
      cur.push(lines[i]); i++;
    }
    if (!parts.length) { if (rich) richText(doc, node, text); else node.textContent = text; return; }
    if (cur.length) parts.push({ code: false, text: cur.join("\n") });
    parts.forEach(function (p) {
      if (!p.code) {
        if (!p.text.replace(/\s+/g, "")) return;
        var plain = p.text.replace(/^\n+|\n+$/g, "");
        if (rich) richText(doc, node, plain); else node.appendChild(doc.createTextNode(plain));
        return;
      }
      var pre = el(doc, "pre", "rv-code"), code = el(doc, "code", null, p.text);
      pre.appendChild(code); node.appendChild(pre);
    });
  }

  // hooks: onMark(ref, delivered, note, done), onCommit(card, words, note, buttons), onDismiss(card, note, buttons); ui.open keyed by gseq
  function renderLine(doc, l, hooks, ui) {
    var row = el(doc, "div", "rv-line " + l.cls);
    var fold = l.collapsible && !!ui, shown = fold && ui.open[l.gseq] === true;
    var head = el(doc, fold ? "button" : "div", "rv-who");
    if (fold) { head.setAttribute("type", "button"); head.setAttribute("aria-expanded", shown ? "true" : "false"); }
    head.textContent = headWords(l);
    if (fold && !shown && l.text) head.textContent += " — " + peek(l.text);
    row.appendChild(head);
    var pre = l.text ? el(doc, "div", "rv-text") : null;
    if (pre) fillText(doc, pre, l.text, l.rich);
    if (pre) {
      if (fold) pre.hidden = !shown;
      row.appendChild(pre);
    }
    if (fold && pre) {
      head.addEventListener("click", function () {
        var now = pre.hidden;
        pre.hidden = !now; head.setAttribute("aria-expanded", now ? "true" : "false");
        if (now) ui.open[l.gseq] = true; else delete ui.open[l.gseq];
        head.textContent = headWords(l);
        if (!now && l.text) head.textContent += " — " + peek(l.text);
      });
    }
    if (l.truncated) row.appendChild(el(doc, "div", "rv-note", "Cut short; the vendor holds the rest."));
    if (l.ambiguous && l.ref && hooks && hooks.onMark) {
      var box = el(doc, "div", "rv-mark"), yes = el(doc, "button", "rv-mark-yes", "It arrived"),
        no = el(doc, "button", "rv-mark-no", "It did not arrive"), note = el(doc, "span", "rv-marknote");
      yes.setAttribute("type", "button"); no.setAttribute("type", "button"); note.setAttribute("role", "status");
      box.appendChild(el(doc, "span", "rv-marklabel", "This line may not have arrived. You decide:"));
      [[yes, true], [no, false]].forEach(function (p) {
        p[0].addEventListener("click", function () { yes.disabled = true; no.disabled = true; hooks.onMark(l.ref, p[1], note, function () { yes.disabled = false; no.disabled = false; }); });
      });
      box.appendChild(yes); box.appendChild(no); box.appendChild(note);
      row.appendChild(box);
    }
    return row;
  }

  // a proposal card: the field holds the words (yours, editable); the summary is the room's note, labelled information
  function renderCard(doc, c, hooks, drafts) {
    var card = el(doc, "div", "rv-card rv-card-" + c.state.replace(/[^a-z]/g, ""));
    card.setAttribute("data-proposal", c.id);
    if (c.state === "committed") { card.appendChild(el(doc, "div", "rv-note", "Dropped: these words went to the desk.")); card.appendChild(el(doc, "div", "rv-text", c.words)); return card; }
    if (c.state === "dismissed") { card.appendChild(el(doc, "div", "rv-note", "Not dropped.")); return card; }
    card.appendChild(el(doc, "div", "rv-note", c.state === "expired" ? "This card is from an earlier conversation and cannot be sent." : "The room can drop this to the desk. Nothing is sent until you tap."));
    var ta = el(doc, "textarea", "rv-cardwords");
    ta.setAttribute("rows", "3"); ta.setAttribute("aria-label", "The words to drop");
    prose(ta, "roomcardwords");
    ta.value = own(drafts, c.id) ? drafts[c.id] : c.words;     // an edit survives a redraw
    ta.addEventListener("input", function () { drafts[c.id] = ta.value; if (hooks.saveCards) hooks.saveCards(); });
    card.appendChild(ta);
    card.appendChild(el(doc, "div", "rv-summary-label rv-note", "The room's summary (information, not your words)"));
    card.appendChild(el(doc, "div", "rv-summary", c.summary));
    var note = el(doc, "div", "rv-sendnote"), go = el(doc, "button", "rv-commit", "Drop these words"), no = el(doc, "button", "rv-dismiss", "Not now");
    go.setAttribute("type", "button"); no.setAttribute("type", "button"); note.setAttribute("role", "status");
    if (c.state === "expired") go.disabled = true;
    go.addEventListener("click", function () { hooks.onCommit(c, ta.value, note, [go, no]); });
    no.addEventListener("click", function () { hooks.onDismiss(c, note, [go, no]); });
    card.appendChild(go); card.appendChild(no); card.appendChild(note);
    return card;
  }

  // The stream is redrawn whole; the cards are kept: a card's element persists across redraws while its state, words and
  // summary are unchanged, so a textarea keeps its focus and caret while the room streams. Order is reconciled by
  // moving only the nodes that are out of place (a node already in its slot is never re-inserted, which would blur it).
  function renderRoom(doc, host, m, hooks, ui, drafts) {
    var st = host.__rv;
    if (!st || st.streamBox.parentNode !== host || st.cardsBox.parentNode !== host) {
      clear(host);
      st = host.__rv = { streamBox: el(doc, "div", "rv-stream"), cardsBox: el(doc, "div", "rv-cards"), cards: {} };
      host.appendChild(st.streamBox); host.appendChild(st.cardsBox);
    }
    clear(st.streamBox);
    if (m.model) st.streamBox.appendChild(el(doc, "div", "rv-head", m.model));
    if (m.error) {
      var ban = el(doc, "div", "rv-gap rv-banner");
      ban.setAttribute("role", "status");
      ban.appendChild(el(doc, "div", "rv-banner-lead", "The room ran into a problem and may not be answering."));
      ban.appendChild(el(doc, "div", "rv-detail", "The room reported: " + m.error));
      st.streamBox.appendChild(ban);
    }
    if (m.gap) st.streamBox.appendChild(el(doc, "div", "rv-gap", "Some lines may be missing from this conversation."));
    if (!m.stream.length) st.streamBox.appendChild(el(doc, "div", "rv-note", m.open ? "Nothing has been said yet." : m.words));
    m.stream.forEach(function (l) { st.streamBox.appendChild(renderLine(doc, l, hooks, ui)); });
    var keep = {};
    m.proposals.forEach(function (c, i) {
      var key = c.state + "\u0000" + c.words + "\u0000" + c.summary, have = own(st.cards, c.id) ? st.cards[c.id] : null;
      if (!have || have.key !== key) { st.cards[c.id] = have = { key: key, node: renderCard(doc, c, hooks, drafts) }; }
      keep[c.id] = true;
      if (st.cardsBox.children[i] !== have.node) st.cardsBox.insertBefore(have.node, st.cardsBox.children[i] || null);
    });
    Object.keys(st.cards).forEach(function (id) {
      if (!own(keep, id)) { var n = st.cards[id].node; if (n.parentNode) n.parentNode.removeChild(n); delete st.cards[id]; }
    });
    while (st.cardsBox.children.length > m.proposals.length) st.cardsBox.removeChild(st.cardsBox.lastChild);
  }

  // mount(host, {fetch, headers, pollMs, speech, storage}): returns {stop, refresh, model}. Polls while mounted and redraws
  // only when the body changed. The dock lives OUTSIDE the redrawn body, so a poll never takes the draft.
  function mount(host, opts) {
    opts = opts || {};
    var doc = host.ownerDocument;
    var f = opts.fetch || root.fetch.bind(root);
    var headers = opts.headers || function () { return {}; };
    var pollMs = opts.pollMs == null ? 2000 : opts.pollMs;
    var Speech = opts.speech === undefined ? (root.SpeechRecognition || root.webkitSpeechRecognition) : opts.speech;
    // Dictation is on-device or absent: a browser whose recognizer cannot be told to stay local (it would send the audio to
    // the browser vendor's cloud) gets no control, and the keyboard's own dictation key fills the draft box instead.
    if (Speech && !(Speech.prototype && "processLocally" in Speech.prototype)) Speech = null;
    var store = opts.storage === undefined ? (function () { try { return root.sessionStorage; } catch (e) { return null; } })() : opts.storage;
    var stopped = false, timer = 0, gen = 0, last = "", thread = null, current = null, ui = { open: {} }, drafts = {};
    var wrap = el(doc, "div", "rv"), stage = el(doc, "div", "rv-stage"), body = el(doc, "div", "rv-body"), dock = el(doc, "div", "rv-dock"),
      jumpB = el(doc, "button", "rv-jump", "Jump to latest"), hushed = null;
    jumpB.setAttribute("type", "button"); jumpB.hidden = true;
    stage.appendChild(body); stage.appendChild(jumpB); wrap.appendChild(stage); wrap.appendChild(dock); host.appendChild(wrap);
    // On a phone the conversation is its own scroll box that ends above the dock: it follows the newest line while it is at its
    // end, and holds its place (with a way back down) once scrolled up. Where the box does not scroll (a desk) none of this shows.
    function atEnd() { return body.scrollHeight - body.scrollTop - body.clientHeight <= 48; }
    function paintJump() { if (!stopped) jumpB.hidden = atEnd(); }
    function toEnd() { body.scrollTop = body.scrollHeight; }
    var wasEnd = true;       // at its end as of the last scroll or draw: a resize (a phone turned) keeps a box that was at its end there
    body.addEventListener("scroll", function () { wasEnd = atEnd(); paintJump(); });
    // kept by name so stop() can remove it: a stopped mount left its listener, and the detached tree it held, on the window
    var onResize = function () { if (!stopped && wasEnd) toEnd(); paintJump(); };
    if (root.addEventListener) root.addEventListener("resize", onResize);
    jumpB.addEventListener("click", function () { toEnd(); paintJump(); });
    var ta = el(doc, "textarea", "rv-input"), actions = el(doc, "div", "rv-actions"),
      sendB = el(doc, "button", "rv-send", "Send"), dropB = el(doc, "button", "rv-drop", "Drop"),
      stopB = el(doc, "button", "rv-stop", "Stop"), note = el(doc, "div", "rv-sendnote"),
      openB = el(doc, "button", "rv-open", "Open the room");
    ta.setAttribute("rows", "3"); ta.setAttribute("aria-label", "Message to the room");
    prose(ta, "roomdraft");
    [sendB, dropB, stopB, openB].forEach(function (b) { b.setAttribute("type", "button"); });
    openB.hidden = true;           // shown only once a read says no room is open: with none, every other control is dark
    note.setAttribute("role", "status");
    actions.appendChild(sendB); actions.appendChild(dropB); actions.appendChild(stopB);
    var micB = null, rec = null;
    if (Speech) {
      micB = el(doc, "button", "rv-mic", "Dictate");
      micB.setAttribute("type", "button");
      micB.setAttribute("title", "On-device dictation where this browser provides it, and only while this page is open");
      actions.appendChild(micB);
    }
    var rotB = null;
    if (opts.rotate !== false) { rotB = el(doc, "button", "rv-rotate", "New conversation"); rotB.setAttribute("type", "button"); actions.appendChild(rotB); }
    dock.appendChild(openB); dock.appendChild(ta); dock.appendChild(actions); dock.appendChild(note);
    var flight = { send: false, drop: false, stop: false, open: false };
    try { if (store && store.getItem("levain.room.draft")) ta.value = store.getItem("levain.room.draft"); } catch (e) { /* no storage */ }
    ta.addEventListener("input", function () { try { if (store) store.setItem("levain.room.draft", ta.value); } catch (e) { /* no storage */ } });
    // The outbox id a line or a drop was last tried under, and the cards' edits, are kept beside the draft (roomfix1 R2):
    // the dashboard unmounts this view whenever the page is hidden (an app switch on the phone), and a remount that minted
    // a new id would send a line twice after an unanswered try. The store is the truth across mounts of one tab; with no
    // store they live in this mount only.
    var mem = {};
    function kept(key) {
      if (store) { try { var raw = store.getItem(key); return raw === null ? null : JSON.parse(raw); } catch (e) { /* fall back */ } }
      return own(mem, key) ? mem[key] : null;
    }
    // A stopped mount writes nothing kept (roomfix2 A1): its late answer to a send made before an app switch would clear
    // the id and the draft the mount that replaced it is showing, and a Send there would mint a new id for the same words.
    // The kept id stays; a resend under it is answered as the line it already is.
    function keep(key, v) {
      if (stopped) return;
      mem[key] = v;
      try { if (store) { if (v === null) store.removeItem(key); else store.setItem(key, JSON.stringify(v)); } } catch (e) { /* no storage */ }
    }
    // the id to try `text` under: the kept one when it was tried for these exact words, else a new one (kept at once)
    function tryId(key, text) {
      var p = kept(key);
      var id = (p && typeof p.id === "string" && p.text === text) ? p.id : newId();
      keep(key, { id: id, text: text });
      return id;
    }
    function settle(key, id) { var p = kept(key); if (p && p.id === id) keep(key, null); }
    var savedCards = kept("levain.room.cards");
    if (!savedCards || typeof savedCards !== "object") savedCards = {};
    var cardsThread = typeof savedCards.thread === "string" ? savedCards.thread : null;
    drafts = savedCards.drafts && typeof savedCards.drafts === "object" ? savedCards.drafts : {};

    function newId() {
      var c = root.crypto;
      return "ob-" + (c && c.randomUUID ? c.randomUUID().replace(/-/g, "") : String(Date.now()) + Math.random().toString(16).slice(2));
    }
    function paintDock() {
      var m = current;
      var can = !!m && m.canSend;
      sendB.disabled = !can || flight.send;
      dropB.disabled = !m || !m.open || flight.drop;
      stopB.disabled = !m || !m.canStop || flight.stop;
      ta.disabled = !m || !m.open;
      if (rotB) rotB.disabled = !m || !m.open;
      openB.hidden = !m || m.open;
      openB.disabled = flight.open;
    }
    function post(path, payload) {
      var h = Object.assign({ "Content-Type": "application/json" }, headers());
      return f(POST_PREFIX + path, { method: "POST", headers: h, body: JSON.stringify(payload) })
        .then(function (res) { return res.text().then(function (t) { var j = null; try { j = JSON.parse(t); } catch (e) { /* not JSON */ } return { status: res.status, json: j }; }); });
    }
    function clearDraft(text) {
      if (!stopped && ta.value === text) { ta.value = ""; try { if (store) store.removeItem("levain.room.draft"); } catch (e) { /* no storage */ } }
    }
    sendB.addEventListener("click", function () {
      endDictation();
      var text = ta.value;
      if (!current || !current.thread || !text.trim() || sendB.disabled || flight.send) return;
      var sendId = tryId("levain.room.send", text);                     // one id per draft text: a resend reuses it
      flight.send = true; paintDock(); note.textContent = "Sending...";
      post("send", { room_thread_id: current.thread, body: text, outbox_id: sendId }).then(function (r) {
        flight.send = false;
        if (r.status === 200 && r.json && r.json.receipt) { clearDraft(text); settle("levain.room.send", sendId); }
        else if (r.status < 500 && r.status !== 202) settle("levain.room.send", sendId);
        note.textContent = sendWords(r.status, r.json);
        last = ""; paintDock(); return pull();
      }).catch(function () {
        flight.send = false;
        note.textContent = "The server could not be reached; the line may or may not have been sent. Sending again will not duplicate it.";
        paintDock();
      });
    });
    dropB.addEventListener("click", function () {
      endDictation();
      var text = ta.value;
      if (!current || !text.trim() || dropB.disabled || flight.drop) return;
      var dropId = tryId("levain.room.drop", text);
      flight.drop = true; paintDock(); note.textContent = "Dropping...";
      post("drop", { words: text, outbox_id: dropId }).then(function (r) {
        flight.drop = false;
        if (r.status === 200) { clearDraft(text); settle("levain.room.drop", dropId); note.textContent = "Dropped: your words went to the desk."; }
        else { if (r.status < 500) settle("levain.room.drop", dropId); note.textContent = (r.json && r.json.message) ? "Not dropped: " + r.json.message : "Not dropped: the server answered " + r.status + "."; }
        paintDock();
      }).catch(function () { flight.drop = false; note.textContent = "The server could not be reached; the drop may or may not have gone. Dropping again will not duplicate it."; paintDock(); });
    });
    openB.addEventListener("click", function () {
      if (openB.disabled || flight.open) return;
      flight.open = true; paintDock(); note.textContent = "Opening the room...";
      post("open", {}).then(function (r) {
        note.textContent = r.status === 200 ? "The room is open." : ((r.json && r.json.message) ? "Not opened: " + r.json.message : "Not opened: the server answered " + r.status + ".");
        last = ""; return pull();
      }).catch(function () { note.textContent = "The server could not be reached; the room may or may not have opened."; })
        .then(function () { flight.open = false; paintDock(); });
    });
    stopB.addEventListener("click", function () {
      if (!current || !current.thread || stopB.disabled || flight.stop) return;
      flight.stop = true; paintDock(); note.textContent = "Stopping...";
      post("stop", { room_thread_id: current.thread }).then(function (r) {
        var o = r.json && r.json.outcome;
        note.textContent = o && own(STOP_WORDS, o) ? STOP_WORDS[o] : ((r.json && r.json.message) || "The stop was not sent.");
        last = ""; return pull();
      }).catch(function () { note.textContent = "The stop could not be sent: the server could not be reached."; })
        .then(function () { flight.stop = false; paintDock(); });
    });
    if (rotB) rotB.addEventListener("click", function () {
      if (rotB.disabled) return;
      rotB.disabled = true; note.textContent = "Starting a new conversation...";
      post("rotate", { room_thread_id: current && current.thread }).then(function (r) {
        note.textContent = r.status === 200 ? "A new conversation is open." : ((r.json && r.json.message) || "No new conversation was started.");
        // a one-poll bridge: the old conversation's problem is not the new one's, but a read that still names the OLD
        // thread is only the server catching up. The same error on a new thread is a new problem and shows.
        if (r.status === 200 && current && current.error) hushed = { thread: current.thread, error: current.error };
        last = ""; return pull();
      }).catch(function () { note.textContent = "The server could not be reached."; }).then(paintDock);
    });
    // dictation state: dbase = the text before the transcript, heard = the transcript already placed in the box
    var dbase = "", heard = "", lastHeard = "";
    function endDictation() {
      var r = rec; rec = null;
      if (micB) micB.textContent = "Dictate";
      if (r) { r.onresult = null; r.onend = null; r.onerror = null; try { r.stop(); } catch (e) { /* already stopped */ } }
    }
    if (micB) {
      var lang = doc.documentElement.lang || (root.navigator && root.navigator.language) || "en-US";
      micB.style.display = "none";                     // shown only once the browser says on-device dictation is available
      // probe:false skips the availability question. MEASURED 2026-11-04 (RM-G2): SpeechRecognition.available({processLocally:true})
      // froze the renderer of the installed headless Google Chrome on a bare page, so the cockpit page asks it nowhere; the mic
      // is then shown on the prototype test alone and the click-time processLocally readback and onerror stay the guard.
      if (opts.probe !== false && typeof Speech.available === "function") {
        var asked;
        try { asked = Speech.available({ langs: [lang], processLocally: true }); } catch (e) { asked = null; }
        if (asked && asked.then) {
          asked.then(function (st) {
            if (st === "unavailable") { micB.remove(); }
            else if (st === "downloadable" || st === "downloading") {
              micB.remove();
              if (!note.textContent) note.textContent = "On-device dictation is not installed on this browser; the keyboard dictation key works.";
            } else micB.style.display = "";
          }, function () { micB.style.display = ""; });
        } else micB.style.display = "";
      } else micB.style.display = "";
      ta.addEventListener("input", function () { if (rec) { dbase = ta.value; heard = lastHeard; } });
      micB.addEventListener("click", function () {
        if (rec) { endDictation(); return; }
        var r = new Speech();
        dbase = ta.value ? ta.value.replace(/\s*$/, " ") : ""; heard = ""; lastHeard = "";
        r.interimResults = true; r.continuous = false; r.processLocally = true; r.lang = lang;
        if (r.processLocally !== true) { note.textContent = "This browser will not keep dictation on this device, so it is off; the keyboard dictation key works."; return; }
        r.onresult = function (ev) {
          if (rec !== r) return;                       // a late result from a recognizer that was ended
          var t = "";
          for (var i = 0; i < ev.results.length; i++) t += ev.results[i][0].transcript;
          lastHeard = t;
          ta.value = dbase + (heard && t.indexOf(heard) === 0 ? t.slice(heard.length) : t);
          try { if (store) store.setItem("levain.room.draft", ta.value); } catch (e) { /* no storage */ }
        };
        r.onend = function () { if (rec === r) endDictation(); };
        r.onerror = function (ev) {
          if (rec !== r) return;
          endDictation();
          var c = ev && ev.error;
          note.textContent = c === "language-not-supported" ? "On-device dictation is not installed for this language; the keyboard's dictation key works here."
            : (c === "not-allowed" || c === "service-not-allowed") ? "Dictation needs permission to use the microphone; allow it in the browser's site settings."
            : c === "audio-capture" ? "No microphone was found for dictation."
            : c === "no-speech" ? "Heard nothing."
            : "Dictation stopped.";
        };
        rec = r; micB.textContent = "Stop dictating";
        try { r.start(); } catch (e) { endDictation(); }
      });
      // foreground only: leaving the page ends dictation
      var leave = function () { if (rec) endDictation(); };
      doc.addEventListener("visibilitychange", function () { if (doc.visibilityState === "hidden") leave(); });
      root.addEventListener && root.addEventListener("blur", leave);
    }

    var hooks = {
      onMark: function (ref, delivered, mnote, done) {
        mnote.textContent = "Marking...";
        post("mark", { room_thread_id: thread, message_id: ref, delivered: delivered }).then(function (r) {
          mnote.textContent = (r.status === 200 && r.json && own(MARK_WORDS, r.json.state)) ? MARK_WORDS[r.json.state] : "Not marked: " + ((r.json && r.json.message) || "the server answered " + r.status + ".");
          if (r.status === 200) { last = ""; pull(); } else done();
        }).catch(function () { mnote.textContent = "Not marked: the server could not be reached."; done(); });
      },
      onCommit: function (card, words, cnote, btns) {
        if (!words.trim()) { cnote.textContent = "Nothing to drop: the field is empty."; return; }
        btns.forEach(function (b) { b.disabled = true; });
        var key = card.id + "|" + words;
        if (!own(hooks.ids, key)) { hooks.ids[key] = newId(); saveCards(); }
        cnote.textContent = "Dropping...";
        post("proposal/" + encodeURIComponent(card.id) + "/commit", { words: words, outbox_id: hooks.ids[key] }).then(function (r) {
          if (r.status === 200) { delete drafts[card.id]; saveCards(); last = ""; pull(); return; }
          if (r.status < 500) { delete hooks.ids[key]; saveCards(); }
          cnote.textContent = "Not dropped: " + ((r.json && r.json.message) || "the server answered " + r.status + ".");
          btns.forEach(function (b) { b.disabled = card.state === "expired" && b.className === "rv-commit"; });
        }).catch(function () { cnote.textContent = "The server could not be reached; dropping again will not duplicate it."; btns.forEach(function (b) { b.disabled = false; }); });
      },
      onDismiss: function (card, cnote, btns) {
        btns.forEach(function (b) { b.disabled = true; });
        post("proposal/" + encodeURIComponent(card.id) + "/dismiss", {}).then(function (r) {
          if (r.status === 200) { delete drafts[card.id]; saveCards(); last = ""; pull(); return; }
          cnote.textContent = "Not dismissed: " + ((r.json && r.json.message) || "the server answered " + r.status + ".");
          btns.forEach(function (b) { b.disabled = false; });
        }).catch(function () { cnote.textContent = "The server could not be reached."; btns.forEach(function (b) { b.disabled = false; }); });
      },
      ids: savedCards.ids && typeof savedCards.ids === "object" ? savedCards.ids : {},
      saveCards: function () { saveCards(); }
    };
    // a stopped mount writes nothing: the mount that replaced it owns the kept cards
    function saveCards() { if (!stopped) keep("levain.room.cards", { thread: cardsThread, drafts: drafts, ids: hooks.ids }); }

    function pull() {
      if (stopped) return Promise.resolve();
      var mine = ++gen;       // a response older than the newest request is dropped, as the work-thread view does
      return f(ROOM_PATH, { cache: "no-store", headers: headers() }).then(function (res) {
        if (stopped || mine !== gen) return null;
        if (!res.ok) { clear(body); body.appendChild(el(doc, "div", "rv-note", res.status === 403 ? "This page carries no token for the room." : "The room could not be read.")); last = ""; return null; }
        return res.text().then(function (t) {
          if (stopped || mine !== gen) return;
          if (t !== last) {
            last = t;
            current = roomModel(JSON.parse(t));
            if (hushed) {
              if (current.thread === hushed.thread && current.error === hushed.error) current.error = "";
              hushed = null;                // spent on its first poll, or sooner when the thread or the error moved
            }
            var stick = thread === null || atEnd();
            if (current.thread !== thread) {
              stick = true;
              ui.open = {}; thread = current.thread;
              // the kept edits belong to the room they were made in: another room starts with none
              if (thread !== cardsThread) { drafts = {}; hooks.ids = {}; cardsThread = thread; saveCards(); }
            }
            renderRoom(doc, body, current, hooks, ui, drafts);
            if (stick) toEnd();
            wasEnd = atEnd(); paintJump();
          }
          paintDock();
        });
      }).catch(function () { if (!stopped) { clear(body); body.appendChild(el(doc, "div", "rv-note", "The room could not be read.")); last = ""; } });
    }
    function loop() { if (!stopped && pollMs > 0) timer = setTimeout(function () { pull().then(loop); }, pollMs); }
    paintDock();
    pull().then(loop);
    return { stop: function () { stopped = true; clearTimeout(timer); endDictation(); if (root.removeEventListener) root.removeEventListener("resize", onResize); }, refresh: function () { last = ""; return pull(); }, model: function () { return current; } };
  }

  var api = { roomModel: roomModel, lineModel: lineModel, sendWords: sendWords, renderRoom: renderRoom, mount: mount };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LevainRoomView = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
