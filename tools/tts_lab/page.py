"""The cloning studio's front end, as one static page.

Kept separate from :mod:`tools.tts_lab.serve` for size, and kept free of
server-side templating on purpose: everything it needs arrives over the
JSON API, so there are no Python format braces fighting the CSS and JS.

The clip picker is the whole design, and the bar above it is the reason.
Chatterbox truncates a reference at ten seconds for the decoder and
fifteen for the tokenizer, so a selection is not a set -- it is an
ordered list with a cliff in it, and clips past the cliff are read and
discarded. Every other studio for this shows a file list and a Clone
button, which is fine right up until someone spends an afternoon
concluding that clip 14 sounds bad when clip 14 was never heard. So the
selection renders as a proportional bar with both cutoffs drawn on it,
and reordering is a first-class action rather than a detail of upload
sequence.

Microphone capture is gone, along with the raw-PCM path that served it:
her voice cannot be performed, so recording could only produce a
different one.
"""

from __future__ import annotations

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aiko voice studio</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { font: 15px/1.55 ui-sans-serif, system-ui, sans-serif; margin: 0;
       background: #14161a; color: #e6e8ec; padding: 28px; }
h1 { font-size: 20px; margin: 0 0 3px; }
h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .06em;
     color: #8b919c; margin: 0 0 12px; }
.sub { color: #8b919c; font-size: 13px; margin: 0 0 20px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
        align-items: start; max-width: 1180px; }
.card { background: #1b1e24; border: 1px solid #2a2e36; border-radius: 10px;
        padding: 16px 18px; }
.card.wide { grid-column: 1 / -1; }
button { font: inherit; background: #2a2f38; color: #e6e8ec; border: 0;
         border-radius: 6px; padding: 8px 14px; cursor: pointer; }
button:hover:not(:disabled) { background: #353b46; }
button:disabled { opacity: .45; cursor: default; }
button.primary { background: #4a6fa5; }
button.rec { background: #a5484f; }
input[type=text], select, textarea {
  font: inherit; background: #14161a; color: #e6e8ec;
  border: 1px solid #2a2e36; border-radius: 6px; padding: 7px 10px;
  width: 100%; }
textarea { resize: vertical; min-height: 62px; }
label { display: block; font-size: 12px; color: #8b919c; margin: 12px 0 4px; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
audio { width: 100%; height: 34px; margin-top: 10px; }
.meter { height: 8px; background: #23272e; border-radius: 4px;
         overflow: hidden; margin-top: 10px; }
.meter > i { display: block; height: 100%; width: 0;
             background: linear-gradient(90deg,#3d7a4e,#c9b45c,#a5484f);
             transition: width .06s linear; }
.stat { font-variant-numeric: tabular-nums; color: #8b919c; font-size: 12px;
        margin-top: 8px; }
.warn { color: #e0a458; }
.bad { color: #e07a7a; }
.good { color: #9fd0aa; }
.script { max-height: 178px; overflow-y: auto; margin-top: 10px;
          border: 1px solid #2a2e36; border-radius: 6px; }
.script div { padding: 6px 10px; border-bottom: 1px solid #23272e;
              font-size: 13px; color: #b9c0cb; }
.script div:last-child { border-bottom: 0; }
.script div.done { color: #5d636e; text-decoration: line-through; }
.script div.now { background: #24303f; color: #e6e8ec; }
table { border-collapse: collapse; width: 100%; font-size: 13px;
        margin-top: 6px; }
td, th { text-align: left; padding: 5px 8px;
         border-bottom: 1px solid #23272e; }
th { color: #8b919c; font-weight: 600; }
code { background: #23272e; padding: 1px 5px; border-radius: 4px;
       font-size: 12px; }
.hint { font-size: 12px; color: #6b7280; margin-top: 8px; }
/* dataset table: one row per clip, transcript editable in place */
#dstable td { vertical-align: top; }
#dstable audio { height: 30px; margin: 0; min-width: 190px; }
#dstable textarea { min-height: 46px; font-size: 13px; }
#dstable tr.review textarea { border-color: #6b5424; }
#dstable .who { font-size: 12px; color: #8b919c; word-break: break-all;
                max-width: 150px; }
#dstable .kill { background: none; padding: 4px 8px; color: #8b919c; }
#dstable .kill:hover { color: #e07a7a; background: none; }
.pill { font-size: 11px; padding: 1px 6px; border-radius: 10px;
        background: #23272e; color: #8b919c; }
.pill.review { background: #3b2f14; color: #e0a458; }
/* clip picker */
.picker { max-height: 300px; overflow-y: auto; border: 1px solid #2a2e36;
          border-radius: 6px; margin-top: 8px; }
.picker .clip { display: flex; gap: 8px; align-items: center;
                padding: 5px 9px; border-bottom: 1px solid #23272e;
                font-size: 13px; }
.picker .clip:last-child { border-bottom: 0; }
.picker .clip:hover { background: #1f232a; }
.picker .clip.on { background: #24303f; }
.picker .clip .nm { flex: 1; min-width: 0; overflow: hidden;
                    text-overflow: ellipsis; white-space: nowrap; }
.picker .clip .num { font-variant-numeric: tabular-nums; color: #8b919c;
                     font-size: 12px; }
.picker .clip button { padding: 2px 7px; font-size: 12px; }
/* the conditioning-window bar: what the engine will actually hear */
.budget { display: flex; height: 26px; border-radius: 5px;
          overflow: hidden; margin-top: 10px; background: #23272e;
          position: relative; }
.budget .seg { border-right: 1px solid #14161a; min-width: 2px;
               font-size: 10px; color: #0d1014; overflow: hidden;
               display: flex; align-items: center; justify-content: center; }
.budget .seg.dec { background: #6f9c7d; }
.budget .seg.enc { background: #8a7f4e; }
.budget .seg.out { background: #4a4f58; }
.budget .mark { position: absolute; top: 0; bottom: 0; width: 2px;
                background: #e6e8ec; opacity: .85; }
.budget .mark i { position: absolute; top: -1px; left: 4px;
                  font-size: 10px; color: #e6e8ec; font-style: normal;
                  white-space: nowrap; }
.legend { font-size: 11px; color: #8b919c; margin-top: 6px; }
.legend b { font-weight: 600; }
.legend .k { display: inline-block; width: 9px; height: 9px;
             border-radius: 2px; margin: 0 3px 0 10px; }
.sel { margin-top: 10px; }
.sel .row2 { display: flex; gap: 8px; align-items: center;
             padding: 4px 0; font-size: 13px;
             border-bottom: 1px solid #23272e; }
.sel .row2 .nm { flex: 1; min-width: 0; overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
.sel .row2 input[type=text] { width: 210px; font-size: 12px;
                              padding: 4px 7px; }
.sel .row2.past .nm { color: #6b7280; text-decoration: line-through; }
</style>
</head>
<body>
<h1>Aiko voice studio</h1>
<p class="sub">Pick source clips, build a reference, clone it, audition,
save. Prototype tool &mdash; it does not touch the running app.</p>

<div class="grid">

  <div class="card">
    <h2>1 &middot; Source clips</h2>
    <div class="row">
      <select id="folder" style="max-width:250px"></select>
      <button id="upick">Add files&hellip;</button>
      <input type="file" id="ufile" multiple
             accept=".wav,.mp3,.flac,.ogg,.opus,.m4a,audio/*" hidden>
    </div>
    <div class="stat" id="clipstat"></div>
    <div class="picker" id="picker"></div>
    <div class="row" style="margin-top:10px">
      <button id="suggest">Fill 10s with the brightest</button>
      <button id="clearsel">Clear</button>
    </div>
    <div class="hint">Brightness is the one axis a real recording beats
      her generated reference on &mdash; 15.7&nbsp;kHz against
      6.2&nbsp;kHz &mdash; so it is the default sort. It is a starting
      point, not a verdict: listen and reorder.</div>
  </div>

  <div class="card">
    <h2>2 &middot; Reference <span class="pill" id="selcount">0 clips</span></h2>
    <p class="sub" style="margin:0 0 6px">Chatterbox <em>truncates</em>
    the reference: the first 10s condition the decoder, the first 15s
    prime articulation, and the rest is read and discarded. Order
    decides what it hears.</p>
    <div class="budget" id="budget"></div>
    <div class="legend">
      <span class="k" style="background:#6f9c7d"></span> decoder (10s)
      <span class="k" style="background:#8a7f4e"></span> tokenizer only
      <span class="k" style="background:#4a4f58"></span> discarded
    </div>
    <div class="sel" id="sel"></div>
    <div class="row" style="margin-top:12px">
      <button id="build" class="primary" disabled>Build reference</button>
      <button id="pace" disabled>Check pace</button>
      <span class="stat" id="buildstat"></span>
    </div>
    <div class="row" style="margin-top:8px">
      <label for="declare" style="margin:0">
        <input type="checkbox" id="declare" style="width:auto"> hold her to
        her own pace (declare 6.55 syl/s in the manifest)
      </label>
    </div>
    <audio id="refplay" controls preload="none"></audio>
    <div class="stat" id="refqual"></div>
    <div class="hint"><b>Pacing clones along with timbre.</b> Check pace
      speaks one probe sentence and measures it &mdash; a reference of
      drawled single words gives a clone that drawls, and that is
      inaudible in the clip itself. Measured here: ten parts at a 0.92s
      median delivered 5.50 syllables per second against 7.36 from her
      sentence-length reference.</div>
    <div class="hint">A transcript is only needed if the clip is in
      <em>English</em>, and it must match the sounds. The app measures
      her tempo target from these, so a guess aims her pacing at words
      nobody said &mdash; three or more are needed before it takes a
      target at all, and blank parts simply do not count.</div>
  </div>

  <div class="card wide">
    <h2>3 &middot; Audition</h2>
    <div class="grid" style="max-width:none">
      <div>
        <label for="engine">Engine</label>
        <select id="engine"></select>
        <div class="stat" id="engstat"></div>
        <label for="voice">Voice</label>
        <select id="voice"></select>
        <div class="stat" id="voicestat"></div>
        <label for="text">Phrase</label>
        <textarea id="text">Hey, I was just thinking about you. How did the build go?</textarea>
        <div class="row" style="margin-top:12px">
          <button id="synth" class="primary" disabled>Speak</button>
          <span class="stat" id="synthstat"></span>
        </div>
        <audio id="out" controls preload="none"></audio>
      </div>
      <div>
        <div class="row">
          <button id="loadknobs">Read this engine's knobs</button>
          <button id="resetknobs">Defaults</button>
        </div>
        <div class="stat" id="knobstat">Loads the engine and reports the
          real <code>generate()</code> keywords.</div>
        <table id="knobtable"></table>
        <label for="knobs">Extra options (JSON)</label>
        <input type="text" id="knobs" placeholder="{&quot;language_id&quot;: &quot;ja&quot;}">
        <div class="hint">Read off the installed code, not a model card.
          Turbo ships <code>exaggeration=0.0, cfg_weight=0.0</code> where
          every published tip quotes 0.5&thinsp;/&thinsp;0.5 &mdash; those
          tips are about a different model in the same family, so a panel
          built from the docs would offer dials that do nothing here.
          Blank means the engine's own default.</div>
      </div>
    </div>
  </div>

  <div class="card wide">
    <h2>4 &middot; Save</h2>
    <div class="row">
      <input type="text" id="vname" placeholder="aiko2" style="max-width:240px">
      <button id="save" disabled>Save voice</button>
      <span class="stat" id="savestat"></span>
    </div>
    <div class="hint" id="savehint"></div>
    <table id="voices"><tr><th>existing voices</th><th>size</th></tr></table>
  </div>

  <div class="card wide">
    <h2>5 &middot; Training dataset</h2>
    <p class="sub" style="margin:0 0 14px">Cloning above needs seconds of
    audio and copies the voice as it is. A <em>fine-tune</em> needs many
    labelled clips and can beat the original &mdash; but only if the audio
    is real. Drop the files in, fix the drafted transcripts, build.</p>
    <div class="row">
      <button id="dspick" class="primary">Add audio files</button>
      <input type="file" id="dsfile" multiple
             accept=".wav,.mp3,.flac,.ogg,.opus,.m4a,audio/*" hidden>
      <button id="dsdraft">Draft missing transcripts</button>
      <button id="dsclear">Clear</button>
      <span class="stat" id="dsasr"></span>
    </div>
    <table id="dstable"></table>
    <div class="row" style="margin-top:14px">
      <input type="text" id="dsname" placeholder="aiko-real"
             style="max-width:200px">
      <input type="text" id="dsspeaker" value="aiko" style="max-width:110px">
      <button id="dssave" class="primary" disabled>Build dataset</button>
      <span class="stat" id="dsstat"></span>
    </div>
    <div class="hint">Transcripts must match the <em>sounds</em>, so spell
    numbers out &mdash; write &ldquo;four fifteen&rdquo;, not
    &ldquo;4:15&rdquo;. Clips over 15s are rejected: most trainers window
    shorter and would truncate them. Text is kept in this browser per
    filename, so a reload will not lose your typing.</div>
  </div>

</div>

<script>
const $ = (id) => document.getElementById(id);
let refId = null, engines = {};
// The clip pool for the open folder, and the ordered selection. Two
// separate things on purpose: the selection outlives a folder change, so
// a reference can draw on more than one pack.
let pool = [], picked = [], DEC = 10, ENC = 15;

// ── engines ──
async function loadEngines() {
  const r = await fetch('api/engines').then(r => r.json());
  engines = {};
  $('engine').innerHTML = '';
  r.engines.forEach(e => {
    engines[e.name] = e;
    const o = document.createElement('option');
    o.value = e.name;
    o.textContent = e.available ? e.name : e.name + '  (not installed)';
    o.disabled = !e.available;
    $('engine').appendChild(o);
  });
  showEngine();
}
function showEngine() {
  const e = engines[$('engine').value];
  if (!e) return;
  const bits = [e.params, e.sample_rate + ' Hz'];
  if (e.numeric_expressiveness) bits.push('dial: ' + e.numeric_expressiveness);
  if (e.inline_tags && e.inline_tags.length) bits.push('tags: ' + e.inline_tags.join(' '));
  bits.push(e.native_rate ? 'native rate' : 'no rate control');
  $('engstat').textContent = bits.join(' \u00b7 ');
  $('savehint').innerHTML = e.saves_as === 'safetensors'
    ? 'pocket-tts exports a speaker embedding (.safetensors) \u2014 the '
      + 'same format the app loads today.'
    : 'This engine clones per call, so the clip <em>is</em> the voice. A '
      + 'built reference saves as a folder \u2014 the wav plus its parts '
      + 'and manifest \u2014 because the app reads the manifest beside '
      + 'the reference to find her tempo target, and a bare wav loses it '
      + 'without saying so.';
}
$('engine').onchange = () => { showEngine(); showVoice(); };

// ── voice source ──
// The audition used to require a freshly recorded reference, which made
// it impossible to hear an already-saved voice -- including the one
// committed copy of Aiko's. A saved voice is a legitimate starting point.
let savedVoices = [];
function fillVoices() {
  const sel = $('voice');
  const prev = sel.value;
  sel.innerHTML = '';
  const ref = document.createElement('option');
  ref.value = '@ref';
  ref.textContent = refId ? 'the reference just built'
                          : 'built reference (none yet)';
  ref.disabled = !refId;
  sel.appendChild(ref);
  savedVoices.forEach(v => {
    const o = document.createElement('option');
    o.value = v.name; o.textContent = v.name;
    sel.appendChild(o);
  });
  // Prefer whatever was already chosen, then her committed reference, so
  // the page opens on something that can actually speak.
  const wanted = [prev, 'reference/aiko_reference.wav'].filter(Boolean);
  for (const w of wanted) {
    if ([...sel.options].some(o => o.value === w && !o.disabled)) {
      sel.value = w; break;
    }
  }
  showVoice();
}
function showVoice() {
  const v = $('voice').value;
  const e = engines[$('engine').value];
  const usable = v === '@ref' ? !!refId : true;
  let note = '';
  if (v === '@ref') {
    note = refId ? 'using the reference from step 2'
                 : 'build one in step 2, or pick a saved voice';
  } else if (v.endsWith('.safetensors')) {
    note = e && e.saves_as === 'safetensors'
      ? 'a pocket-tts speaker embedding'
      : 'embeddings are pocket-tts only \u2014 this engine needs a .wav';
  } else {
    note = 'cloned from this clip on every call';
  }
  $('voicestat').textContent = note;
  $('synth').disabled = !usable;
  // Any saved voice can be paced, not just a freshly built one -- that
  // is how a candidate gets compared against her incumbent reference on
  // the same words.
  $('pace').disabled = !usable;
}
$('voice').onchange = showVoice;

// ── clip pool ──
async function loadFolders() {
  const r = await fetch('api/clips/folders').then(r => r.json());
  $('folder').innerHTML = '';
  r.folders.forEach(f => {
    const o = document.createElement('option');
    o.value = f.rel;
    o.textContent = f.rel + '  (' + f.clips + ')';
    $('folder').appendChild(o);
  });
  if (r.folders.length) await loadClips();
}
$('folder').onchange = loadClips;

async function loadClips() {
  const dir = $('folder').value;
  if (!dir) return;
  $('clipstat').textContent = 'measuring ' + dir + '\u2026';
  const r = await fetch('api/clips?dir=' + encodeURIComponent(dir))
    .then(r => r.json());
  if (r.error) {
    $('clipstat').innerHTML = '<span class="bad">' + r.error + '</span>';
    return;
  }
  pool = r.clips; DEC = r.decoder_window_s; ENC = r.encoder_window_s;
  const usable = pool.filter(c => !c.warnings.length).length;
  const total = pool.reduce((n, c) => n + c.duration_s, 0);
  $('clipstat').textContent = pool.length + ' clips \u00b7 '
    + (total / 60).toFixed(2) + ' min \u00b7 ' + usable + ' clean';
  $('suggest').dataset.suggested = JSON.stringify(r.suggested || []);
  renderPool();
}

const inSel = (rel) => picked.some(s => s.rel === rel);

function renderPool() {
  const box = $('picker');
  box.innerHTML = '';
  pool.forEach(c => {
    const row = document.createElement('div');
    row.className = 'clip' + (inSel(c.rel) ? ' on' : '');
    const bad = c.warnings.length;
    row.innerHTML =
      '<span class="nm" title="' + escapeHtml(c.rel) + '">'
        + escapeHtml(c.name) + '</span>'
      + '<span class="num">' + c.duration_s.toFixed(2) + 's</span>'
      + '<span class="num">' + (c.bandwidth_hz / 1000).toFixed(1) + 'k</span>'
      + (bad ? '<span class="pill review">' + escapeHtml(c.warnings[0])
               + '</span>' : '');
    const play = document.createElement('button');
    play.textContent = '\u25b6';
    play.title = 'listen';
    play.onclick = (ev) => {
      ev.stopPropagation();
      new Audio('api/clip/' + encodeURI(c.rel)).play().catch(() => {});
    };
    const add = document.createElement('button');
    add.textContent = inSel(c.rel) ? '\u2212' : '+';
    add.onclick = (ev) => { ev.stopPropagation(); toggle(c); };
    row.appendChild(play); row.appendChild(add);
    row.onclick = () => toggle(c);
    box.appendChild(row);
  });
}

function toggle(clip) {
  const at = picked.findIndex(s => s.rel === clip.rel);
  if (at >= 0) picked.splice(at, 1);
  else picked.push({ rel: clip.rel, name: clip.name,
                     duration_s: clip.duration_s, phrase: '' });
  renderPool(); renderSel();
}

$('suggest').onclick = () => {
  let want = [];
  try { want = JSON.parse($('suggest').dataset.suggested || '[]'); }
  catch (e) { want = []; }
  want.forEach(rel => {
    if (inSel(rel)) return;
    const c = pool.find(x => x.rel === rel);
    if (c) picked.push({ rel: c.rel, name: c.name,
                         duration_s: c.duration_s, phrase: '' });
  });
  renderPool(); renderSel();
};
$('clearsel').onclick = () => { picked = []; renderPool(); renderSel(); };

$('upick').onclick = () => $('ufile').click();
$('ufile').onchange = async (ev) => {
  const files = [...ev.target.files];
  ev.target.value = '';
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    $('clipstat').textContent = 'uploading ' + (i + 1) + '/' + files.length;
    const ext = (f.name.split('.').pop() || '').toLowerCase();
    const url = 'api/clips/upload?ext=' + encodeURIComponent(ext)
      + '&name=' + encodeURIComponent(f.name);
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/octet-stream' },
      body: await f.arrayBuffer(),
    }).then(r => r.json()).catch(e => ({ error: String(e) }));
    if (r.error) {
      $('clipstat').innerHTML = '<span class="bad">' + r.error + '</span>';
      return;
    }
    $('folder').value = r.dir;
  }
  await loadFolders();
  await loadClips();
};

// ── the selection, and what the engine will hear of it ──
// A gap goes between parts, so the budget maths has to include it or the
// bar disagrees with the reference that gets built.
const GAP_S = 0.22;

function renderSel() {
  $('selcount').textContent = picked.length
    + (picked.length === 1 ? ' clip' : ' clips');
  $('build').disabled = picked.length === 0;
  const bar = $('budget'), box = $('sel');
  bar.innerHTML = ''; box.innerHTML = '';
  if (!picked.length) {
    bar.innerHTML = '<div class="seg out" style="flex:1">nothing selected</div>';
    return;
  }
  let at = 0;
  const spans = picked.map(s => {
    const start = at;
    at += s.duration_s + GAP_S;
    return { start, end: start + s.duration_s };
  });
  const total = Math.max(at - GAP_S, 0.001);
  const scale = Math.max(total, ENC);
  picked.forEach((s, i) => {
    const sp = spans[i];
    const seg = document.createElement('div');
    const kind = sp.start >= ENC ? 'out' : (sp.start >= DEC ? 'enc' : 'dec');
    seg.className = 'seg ' + kind;
    seg.style.flex = String(s.duration_s);
    seg.title = s.name + ' \u2014 ' + sp.start.toFixed(1) + 's to '
      + sp.end.toFixed(1) + 's';
    seg.textContent = String(i + 1);
    bar.appendChild(seg);
  });
  if (scale > total) {
    const pad = document.createElement('div');
    pad.className = 'seg'; pad.style.flex = String(scale - total);
    pad.style.background = '#23272e';
    bar.appendChild(pad);
  }
  [[DEC, ' 10s'], [ENC, ' 15s']].forEach(([at_s, label]) => {
    if (at_s > scale) return;
    const m = document.createElement('div');
    m.className = 'mark';
    m.style.left = (at_s / scale * 100) + '%';
    m.innerHTML = '<i>' + label + '</i>';
    bar.appendChild(m);
  });

  picked.forEach((s, i) => {
    const sp = spans[i];
    const row = document.createElement('div');
    row.className = 'row2' + (sp.start >= ENC ? ' past' : '');
    row.innerHTML = '<span class="num">' + (i + 1) + '</span>'
      + '<span class="nm" title="' + escapeHtml(s.rel) + '">'
      + escapeHtml(s.name) + '</span>'
      + '<span class="num">' + sp.start.toFixed(1) + '\u2013'
      + sp.end.toFixed(1) + 's</span>';
    const phrase = document.createElement('input');
    phrase.type = 'text';
    phrase.placeholder = 'English transcript (optional)';
    phrase.value = s.phrase;
    phrase.oninput = () => { s.phrase = phrase.value; };
    row.appendChild(phrase);
    [['\u2191', -1], ['\u2193', 1]].forEach(([glyph, delta]) => {
      const b = document.createElement('button');
      b.textContent = glyph;
      b.disabled = (delta < 0 && i === 0)
        || (delta > 0 && i === picked.length - 1);
      b.onclick = () => {
        const j = i + delta;
        [picked[i], picked[j]] = [picked[j], picked[i]];
        renderSel();
      };
      row.appendChild(b);
    });
    const kill = document.createElement('button');
    kill.textContent = '\u00d7';
    kill.onclick = () => { picked.splice(i, 1); renderPool(); renderSel(); };
    row.appendChild(kill);
    box.appendChild(row);
  });

  const over = total - ENC;
  if (over > 0.05) {
    const note = document.createElement('div');
    note.className = 'legend warn';
    note.textContent = over.toFixed(1) + 's past the 15s cutoff will be '
      + 'read and thrown away \u2014 trim it or move those clips earlier.';
    box.appendChild(note);
  }
}

$('build').onclick = async () => {
  $('build').disabled = true;
  $('buildstat').textContent = 'building\u2026';
  const r = await fetch('api/reference/build', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      parts: picked.map(s => ({ rel: s.rel, phrase: s.phrase })),
      gap_ms: Math.round(GAP_S * 1000),
      // Only meaningful when the parts cannot supply a measured rate,
      // which is every reference built from non-English source audio.
      target_syl_s: $('declare').checked ? 6.55 : 0,
    }),
  }).then(r => r.json()).catch(e => ({ error: String(e) }));
  $('build').disabled = false;
  if (r.error) {
    $('buildstat').innerHTML = '<span class="bad">' + r.error + '</span>';
    return;
  }
  refId = r.id;
  $('refplay').src = 'api/audio/' + r.file + '?t=' + Date.now();
  const q = r.quality, w = r.windows, t = r.targets || {}, sh = r.shape || {};
  const bits = [q.duration_s.toFixed(1) + 's',
                (r.sample_rate / 1000).toFixed(1) + ' kHz',
                (r.bandwidth_hz / 1000).toFixed(1) + ' kHz wide',
                'peak ' + q.peak.toFixed(2)];
  let html = bits.join(' \u00b7 ');
  html += q.warnings.length
    ? ' <span class="warn">\u2014 ' + q.warnings.join(', ') + '</span>'
    : ' <span class="good">\u2014 usable</span>';
  if (sh.parts) {
    html += '<div>shape: ' + sh.parts + ' parts, median '
      + sh.median_part_s.toFixed(2) + 's, '
      + (sh.gap_share * 100).toFixed(0) + '% gaps '
      + (sh.connected ? '<span class="good">\u2014 connected speech</span>'
                      : '<span class="bad">\u2014 isolated words</span>')
      + '</div>';
  }
  if (sh.warning) {
    html += '<div class="bad">' + escapeHtml(sh.warning) + '</div>';
  }
  // What the running app will aim its per-clip corrections at. Reported
  // here because both are silently optional in the app: a missing target
  // disables that correction, and finding out from a log line hours
  // later is how a reference gets blamed for a flat delivery.
  html += '<div>app targets: brightness '
    + (t.tilt_db == null ? '<span class="warn">off</span>'
       : 'tilt ' + t.tilt_db.toFixed(2) + ' dB')
    + ' \u00b7 tempo '
    + (r.manifest.target_syl_s
       ? '<span class="good">' + r.manifest.target_syl_s.toFixed(2)
         + ' syl/s declared</span>'
       : t.rate_syl_s == null
       ? '<span class="warn">off</span> (' + (t.rate_parts || 0) + ' of '
         + r.min_rate_parts + ' measurable transcripts)'
       : t.rate_syl_s.toFixed(2) + ' syl/s from ' + t.rate_parts
         + ' parts, hers is ' + (t.rate_incumbent || 0).toFixed(2))
    + '</div>';
  if (t.rate_warning) {
    html += '<div class="bad">' + escapeHtml(t.rate_warning) + '</div>';
  }
  (t.rate_skipped || []).forEach(s => {
    html += '<div class="warn" style="font-size:12px">no tempo from '
      + escapeHtml(s.part.split('/').pop()) + ': ' + escapeHtml(s.why)
      + '</div>';
  });
  if (w.straddling && w.straddling.length) {
    html += '<div class="warn">cut mid-clip at 10s: '
      + w.straddling.map(escapeHtml).join(', ') + '</div>';
  }
  if (w.discarded && w.discarded.length) {
    html += '<div class="warn">never heard: '
      + w.discarded.map(escapeHtml).join(', ') + '</div>';
  }
  $('refqual').innerHTML = html;
  $('buildstat').innerHTML = '<span class="good">' + w.decoder_s
    + 's conditions the decoder</span>';
  $('save').disabled = false;
  $('pace').disabled = false;
  fillVoices();
  $('voice').value = '@ref';
  showVoice();
};

// ── pace ──
// Deliberately measures whatever the Voice picker is on, not only the
// freshly built reference, so a candidate can be compared against
// reference/aiko_reference.wav with the same words and the same engine.
$('pace').onclick = async () => {
  $('pace').disabled = true;
  $('buildstat').textContent = 'speaking a probe sentence\u2026';
  const pick = $('voice').value;
  const body = { engine: $('engine').value };
  if (pick === '@ref') body.reference = refId; else body.voice = pick;
  const r = await fetch('api/pace', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => r.json()).catch(e => ({ error: String(e) }));
  $('pace').disabled = false;
  if (r.error) {
    $('buildstat').innerHTML = '<span class="bad">' + r.error + '</span>';
    return;
  }
  const off = (r.needs - 1) * 100;
  let msg = '<b>' + r.delivered_syl_s.toFixed(2) + ' syl/s</b> against her '
    + r.her_pace_syl_s.toFixed(2) + ' \u2014 ';
  if (Math.abs(off) < 4) {
    msg += '<span class="good">on pace</span>';
  } else {
    msg += (off > 0 ? 'slow' : 'fast') + ' by ' + Math.abs(off).toFixed(0)
      + '%, ' + (r.fixable_in_app
        ? '<span class="warn">within the app\u2019s '
          + (r.app_limit * 100).toFixed(0) + '% correction</span>'
        : '<span class="bad">past the app\u2019s '
          + (r.app_limit * 100).toFixed(0) + '% cap \u2014 pick longer '
          + 'clips</span>');
  }
  $('buildstat').innerHTML = msg;
  $('out').src = 'api/audio/' + r.file + '?t=' + Date.now();
};

// ── generation knobs ──
// Built from what the sidecar read off the installed generate(), so the
// panel cannot offer a dial the engine ignores. A field left blank sends
// nothing, which is not the same as sending the default: "as shipped" is
// the only defensible baseline for an audition.
let knobDefaults = {};

$('loadknobs').onclick = async () => {
  $('loadknobs').disabled = true;
  $('knobstat').textContent = 'loading the engine\u2026';
  const r = await fetch('api/knobs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ engine: $('engine').value }),
  }).then(r => r.json()).catch(e => ({ error: String(e) }));
  $('loadknobs').disabled = false;
  if (r.error) {
    $('knobstat').innerHTML = '<span class="bad">' + r.error + '</span>';
    return;
  }
  knobDefaults = r.defaults || {};
  const rt = r.runtime || {};
  const bits = [];
  if (rt.torch) bits.push('torch ' + rt.torch);
  if (rt.threads) bits.push(rt.threads + ' threads');
  if (r.languages && r.languages.length) {
    bits.push(r.languages.length + ' languages');
  }
  $('knobstat').textContent = r.note
    || (bits.length ? bits.join(' \u00b7 ') : 'ready');
  renderKnobs(r.accepts || []);
};
$('resetknobs').onclick = () => {
  [...$('knobtable').querySelectorAll('input')].forEach(i => { i.value = ''; });
  keepKnobs();
};

function renderKnobs(accepts) {
  const t = $('knobtable');
  t.innerHTML = '';
  // Only the numeric and string dials. Everything else on a generate()
  // signature is plumbing -- the audio prompt, the target text -- and
  // putting it in a tuning panel invites breaking the call.
  const skip = ['text', 'audio_prompt_path', 'prompt', 'voice',
                'conds', 'target_voice_path'];
  const rows = accepts.filter(k => !skip.includes(k));
  if (!rows.length) {
    t.innerHTML = '<tr><td class="stat">no per-call knobs</td></tr>';
    return;
  }
  t.innerHTML = '<tr><th>knob</th><th>shipped</th><th>override</th></tr>';
  rows.forEach(k => {
    const tr = document.createElement('tr');
    const shipped = knobDefaults[k];
    tr.innerHTML = '<td><code>' + escapeHtml(k) + '</code></td>'
      + '<td class="stat">'
      + (shipped === undefined || shipped === null
         ? '\u2014' : escapeHtml(String(shipped))) + '</td>';
    const cell = document.createElement('td');
    const box = document.createElement('input');
    box.type = 'text';
    box.dataset.knob = k;
    box.placeholder = shipped === undefined || shipped === null
      ? 'engine default' : String(shipped);
    // Restore a value tuned in an earlier session. Finding the setting
    // that steadies a word and losing it to a page reload is a bad way
    // to spend an afternoon; keyed per engine, since the same number
    // means a different intervention on each.
    const kept = savedKnobs()[k];
    if (kept !== undefined) box.value = String(kept);
    box.onchange = keepKnobs;
    cell.appendChild(box);
    tr.appendChild(cell);
    t.appendChild(tr);
  });
}

function knobKey() { return 'ttslab.knobs.' + $('engine').value; }

function savedKnobs() {
  try { return JSON.parse(localStorage.getItem(knobKey()) || '{}'); }
  catch (e) { return {}; }
}

function keepKnobs() {
  try { localStorage.setItem(knobKey(), JSON.stringify(knobValues())); }
  catch (e) { /* private browsing; the panel still works for this session */ }
}

function knobValues() {
  const out = {};
  [...$('knobtable').querySelectorAll('input')].forEach(i => {
    const raw = i.value.trim();
    if (!raw) return;
    const num = Number(raw);
    out[i.dataset.knob] = Number.isFinite(num) && raw !== '' ? num : raw;
  });
  return out;
}

// ── synth ──
$('synth').onclick = async () => {
  let kwargs = knobValues();
  const raw = $('knobs').value.trim();
  if (raw) {
    try { kwargs = Object.assign(kwargs, JSON.parse(raw)); }
    catch (e) { $('synthstat').innerHTML = '<span class="bad">bad JSON</span>'; return; }
  }
  $('synth').disabled = true;
  $('synthstat').textContent = 'generating\u2026 (first call loads the model)';
  const pick = $('voice').value;
  const body = { engine: $('engine').value, text: $('text').value, kwargs };
  if (pick === '@ref') body.reference = refId; else body.voice = pick;
  const r = await fetch('api/synth', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => r.json());
  $('synth').disabled = false;
  if (r.error) { $('synthstat').innerHTML = '<span class="bad">' + r.error + '</span>'; return; }
  $('out').src = 'api/audio/' + r.file + '?t=' + Date.now();
  $('out').play().catch(() => {});
  $('synthstat').textContent = r.duration_s.toFixed(2) + 's in '
    + r.total_ms.toFixed(0) + 'ms \u00b7 rtf ' + r.rtf.toFixed(2)
    + ' \u00b7 ' + r.sample_rate + ' Hz';
};

// ── save ──
$('save').onclick = async () => {
  const name = $('vname').value.trim();
  if (!name) { $('savestat').innerHTML = '<span class="bad">name it first</span>'; return; }
  $('savestat').textContent = 'saving\u2026';
  const r = await fetch('api/save', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      engine: $('engine').value, reference: refId, name,
      // Whatever the audition is currently tuned to travels with the
      // voice. Without this the app falls back to the engine's shipped
      // defaults and any value found here is lost on save.
      kwargs: knobValues(),
    }),
  }).then(r => r.json());
  if (r.error) { $('savestat').innerHTML = '<span class="bad">' + r.error + '</span>'; return; }
  let msg = '<span class="good">saved ' + r.path + '</span>';
  if (r.parts) {
    msg += ' <span class="stat">' + r.parts + ' parts + manifest \u2014 '
      + 'pick <code>' + r.voice_id + '</code> in Aiko\u2019s voice '
      + 'settings</span>';
  }
  if (r.tuned && Object.keys(r.tuned).length) {
    msg += '<div class="good">tuned on ' + $('engine').value + ': '
      + Object.entries(r.tuned).map(([k, v]) => k + '=' + v).join(', ')
      + ' \u2014 the app will use these for this voice</div>';
  }
  // Every engine this voice now carries numbers for. Tuning one
  // reference on several is the point of having them side by side, and
  // the manifest keeps them apart -- worth showing, so a save on the
  // second engine does not look like it replaced the first.
  if (r.engines_tuned && r.engines_tuned.length > 1) {
    msg += '<div class="stat">tuning stored for: '
      + r.engines_tuned.join(', ') + '</div>';
  }
  if (r.kb) msg += ' <span class="stat">' + (r.kb / 1024).toFixed(1) + ' MB</span>';
  // pocket-tts keeps the whole reference in the speaker state, so the
  // file size tracks clip length: her shipped aiko1_refined is 4.8 MB
  // and a 27s clip produced 16 MB. Longer is not better here.
  if (r.kb > 8192) {
    msg += ' <span class="warn">\u2014 large. pocket-tts stores the whole '
      + 'reference, so a shorter clip (~10s) gives a much smaller '
      + 'embedding; aiko1_refined is 4.8 MB.</span>';
  }
  $('savestat').innerHTML = msg;
  loadVoices();
};

async function loadVoices() {
  const r = await fetch('api/voices').then(r => r.json());
  savedVoices = r.voices;
  const rows = r.voices.map(v =>
    '<tr><td><code>' + v.name + '</code></td><td>'
    + (v.kb > 1024 ? (v.kb / 1024).toFixed(1) + ' MB' : v.kb.toFixed(0) + ' KB')
    + '</td></tr>').join('');
  $('voices').innerHTML = '<tr><th>existing voices</th><th>size</th></tr>' + rows;
  fillVoices();
}

// ── dataset ──
// Transcripts are the expensive part, so they are kept in localStorage
// keyed by filename and size rather than by the server's clip id: ids die
// with the process, and losing an afternoon of typing to a restart is how
// a half-labelled set happens.
const LABELS = 'aiko.tts.labels';
const labelKey = (f) => f.name + ':' + f.size;
function labelsRead() {
  try { return JSON.parse(localStorage.getItem(LABELS) || '{}'); }
  catch (e) { return {}; }
}
function labelSave(key, text) {
  const all = labelsRead();
  if (text) all[key] = text; else delete all[key];
  try { localStorage.setItem(LABELS, JSON.stringify(all)); } catch (e) {}
}

let clips = [];
$('dspick').onclick = () => $('dsfile').click();
$('dsfile').onchange = async (ev) => {
  const files = [...ev.target.files];
  ev.target.value = '';
  const known = labelsRead();
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    $('dsstat').textContent = 'reading ' + (i + 1) + '/' + files.length
      + ' \u2014 ' + f.name;
    const ext = (f.name.split('.').pop() || '').toLowerCase();
    const url = 'api/dataset/add?ext=' + encodeURIComponent(ext)
      + '&name=' + encodeURIComponent(f.name);
    let r;
    try {
      r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: await f.arrayBuffer(),
      }).then(r => r.json());
    } catch (e) { r = { error: String(e) }; }
    if (r.error) { clips.push({ bad: r.error, source: f.name }); continue; }
    r.key = labelKey(f);
    r.text = known[r.key] || '';
    clips.push(r);
  }
  $('dsstat').textContent = '';
  renderClips();
};

$('dsclear').onclick = () => {
  if (clips.length && !confirm('Remove all ' + clips.length
      + ' clips from the list? Typed transcripts are kept.')) return;
  clips = []; renderClips();
};

function renderClips() {
  const t = $('dstable');
  if (!clips.length) {
    t.innerHTML = '';
    $('dssave').disabled = true;
    return;
  }
  t.innerHTML = '<tr><th>file</th><th>clip</th><th>transcript</th>'
    + '<th>notes</th><th></th></tr>';
  clips.forEach((c, i) => {
    const tr = document.createElement('tr');
    if (c.bad) {
      tr.innerHTML = '<td class="who">' + c.source + '</td>'
        + '<td colspan="3" class="bad">' + c.bad + '</td>';
    } else {
      const q = c.quality;
      const secs = q.duration_s.toFixed(1) + 's \u00b7 '
        + (c.sample_rate / 1000).toFixed(1) + ' kHz';
      const notes = (c.notes || []).map(n =>
        '<div class="warn" style="font-size:12px">' + n + '</div>').join('');
      const flag = c.review
        ? '<span class="pill review">check</span> ' : '';
      tr.innerHTML =
        '<td class="who">' + c.source + '<div class="stat">' + secs
          + '</div></td>'
        + '<td><audio controls preload="none" src="api/audio/' + c.file
          + '"></audio></td>'
        + '<td style="min-width:290px"><textarea>' + escapeHtml(c.text || '')
          + '</textarea></td>'
        + '<td>' + flag + notes + (c.asr || '') + '</td>';
      const box = tr.querySelector('textarea');
      box.oninput = () => {
        c.text = box.value;
        labelSave(c.key, box.value);
        updateSave();
      };
      if (c.review) tr.classList.add('review');
    }
    const kill = document.createElement('td');
    const b = document.createElement('button');
    b.className = 'kill'; b.textContent = '\u00d7';
    b.title = 'remove from the list';
    b.onclick = () => { clips.splice(i, 1); renderClips(); };
    kill.appendChild(b);
    tr.appendChild(kill);
    t.appendChild(tr);
  });
  updateSave();
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;');
}

function updateSave() {
  const ready = clips.filter(c => !c.bad && (c.text || '').trim()).length;
  $('dssave').disabled = ready === 0;
  const total = clips.filter(c => !c.bad).length;
  $('dsstat').textContent = total
    ? ready + ' of ' + total + ' labelled' : '';
}

$('dsdraft').onclick = async () => {
  const todo = clips.filter(c => !c.bad && !(c.text || '').trim());
  if (!todo.length) { $('dsstat').textContent = 'all labelled already'; return; }
  $('dsdraft').disabled = true;
  for (let i = 0; i < todo.length; i++) {
    const c = todo[i];
    $('dsstat').textContent = 'transcribing ' + (i + 1) + '/' + todo.length
      + ' \u2014 ' + c.source;
    const r = await fetch('api/dataset/transcribe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: c.id }),
    }).then(r => r.json()).catch(e => ({ error: String(e) }));
    if (r.error) { c.asr = '<div class="bad">' + r.error + '</div>'; continue; }
    c.text = r.text;
    c.review = r.needs_review;
    c.asr = (r.warnings || []).map(w =>
      '<div class="warn" style="font-size:12px">' + w + '</div>').join('');
    labelSave(c.key, r.text);
    renderClips();
  }
  $('dsdraft').disabled = false;
  updateSave();
};

$('dssave').onclick = async () => {
  const items = clips.filter(c => !c.bad && (c.text || '').trim())
    .map(c => ({ id: c.id, text: c.text }));
  $('dssave').disabled = true;
  $('dsstat').textContent = 'building\u2026';
  const r = await fetch('api/dataset/save', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: $('dsname').value.trim(),
      speaker: $('dsspeaker').value.trim() || 'aiko',
      items,
    }),
  }).then(r => r.json()).catch(e => ({ error: String(e) }));
  $('dssave').disabled = false;
  if (r.error) {
    let msg = '<span class="bad">' + r.error + '</span>';
    (r.rejects || []).forEach(x => {
      msg += '<div class="warn" style="font-size:12px">' + x.file + ': '
        + x.reason + '</div>';
    });
    $('dsstat').innerHTML = msg;
    return;
  }
  let msg = '<span class="good">' + r.clips + ' clips, '
    + r.minutes.toFixed(1) + ' min at ' + r.sample_rate + ' Hz \u2014 '
    + r.path + '</span>';
  if (r.minutes < 10) {
    msg += ' <span class="warn">\u2014 thin for a fine-tune; 10\u201330 min '
      + 'is where it starts beating zero-shot cloning.</span>';
  }
  (r.rejects || []).forEach(x => {
    msg += '<div class="warn" style="font-size:12px">skipped ' + x.file
      + ': ' + x.reason + '</div>';
  });
  $('dsstat').innerHTML = msg;
};

async function loadAsr() {
  const r = await fetch('api/dataset/asr').then(r => r.json());
  if (!r.available) {
    $('dsasr').innerHTML = '<span class="warn">no cached whisper model '
      + '\u2014 transcripts must be typed</span>';
    $('dsdraft').disabled = true;
    return;
  }
  $('dsasr').textContent = 'whisper ' + r.model
    + (r.cached.length > 1 ? ' (also: ' + r.cached.slice(1).join(', ') + ')' : '');
}

loadEngines(); loadFolders(); loadVoices(); loadAsr(); renderSel();
</script>
</body>
</html>
"""
