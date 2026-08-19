"""The cloning studio's front end, as one static page.

Kept separate from :mod:`tools.tts_lab.serve` for size, and kept free of
server-side templating on purpose: everything it needs arrives over the
JSON API, so there are no Python format braces fighting the CSS and JS.

Recording captures **raw PCM via the Web Audio API**, not
``MediaRecorder``. MediaRecorder hands back WebM/Opus, and decoding that
server-side would mean ffmpeg or a codec dependency for the one job of
producing a WAV. Raw PCM needs neither, and the app already does exactly
this for voice mode -- the browser owns capture and streams Int16 frames
over the wire. See ``docs/voice-mode.md``.
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
</style>
</head>
<body>
<h1>Aiko voice studio</h1>
<p class="sub">Record a reference, clone it into any installed engine,
audition it, and save it to <code>voices/</code>. Prototype tool &mdash;
it does not touch the running app.</p>

<div class="grid">

  <div class="card">
    <h2>1 &middot; Reference clip</h2>
    <div class="row">
      <button id="rec" class="primary">Record</button>
      <button id="stop" disabled>Stop</button>
      <button id="pick">Upload audio</button>
      <input type="file" id="file"
             accept=".wav,.mp3,.flac,.ogg,.opus,audio/*" hidden>
    </div>
    <div class="meter"><i id="level"></i></div>
    <div class="stat" id="recstat">Aim for 20&ndash;30 seconds. Read the
      script below so the clip covers her range.</div>
    <audio id="refplay" controls preload="none"></audio>
    <div class="stat" id="refqual"></div>
    <div class="script" id="script"></div>
    <div class="hint">Click a line to mark it read. The set is the same
      one <code>voicebank.py</code> uses &mdash; phonetically broad,
      varied intonation, deliberately dull content.</div>
  </div>

  <div class="card">
    <h2>2 &middot; Audition</h2>
    <label for="engine">Engine</label>
    <select id="engine"></select>
    <div class="stat" id="engstat"></div>
    <label for="text">Phrase</label>
    <textarea id="text">Hey, I was just thinking about you. How did the build go?</textarea>
    <label for="knobs">Generation options (JSON, blank = engine defaults)</label>
    <input type="text" id="knobs" placeholder="{&quot;exaggeration&quot;: 0.6}">
    <div class="row" style="margin-top:12px">
      <button id="synth" class="primary" disabled>Speak</button>
      <span class="stat" id="synthstat"></span>
    </div>
    <audio id="out" controls preload="none"></audio>
  </div>

  <div class="card wide">
    <h2>3 &middot; Save</h2>
    <div class="row">
      <input type="text" id="vname" placeholder="aiko2" style="max-width:240px">
      <button id="save" disabled>Save voice</button>
      <span class="stat" id="savestat"></span>
    </div>
    <div class="hint" id="savehint"></div>
    <table id="voices"><tr><th>existing voices</th><th>size</th></tr></table>
  </div>

</div>

<script>
const $ = (id) => document.getElementById(id);
let refId = null, engines = {}, ctx = null, stream = null, node = null;
let chunks = [], recording = false, recSr = 24000;

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
  $('savehint').textContent = e.saves_as === 'safetensors'
    ? 'pocket-tts exports a speaker embedding (.safetensors) \u2014 the same format the app loads today.'
    : 'This engine clones per call, so saving stores the reference clip as ' +
      'the voice. Point the engine at the .wav.';
}
$('engine').onchange = showEngine;

// ── reading script ──
async function loadScript() {
  const r = await fetch('api/script').then(r => r.json());
  $('script').innerHTML = '';
  r.phrases.forEach(p => {
    const d = document.createElement('div');
    d.textContent = p;
    d.onclick = () => d.classList.toggle('done');
    $('script').appendChild(d);
  });
}

// ── recording: raw PCM, no MediaRecorder ──
$('rec').onclick = async () => {
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: false,
               noiseSuppression: false, autoGainControl: false }
    });
  } catch (err) { $('recstat').innerHTML = '<span class="bad">mic denied: ' + err.message + '</span>'; return; }
  // Ask for the engine sample rate directly and let the browser resample;
  // doing it here avoids a server-side resampler for the one job of
  // producing a conditioning clip.
  ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: recSr });
  const src = ctx.createMediaStreamSource(stream);
  // ScriptProcessor is deprecated in favour of AudioWorklet, which needs
  // a separate module file. For a local prototype the simpler node wins.
  node = ctx.createScriptProcessor(4096, 1, 1);
  chunks = []; recording = true;
  node.onaudioprocess = (ev) => {
    if (!recording) return;
    const d = ev.inputBuffer.getChannelData(0);
    chunks.push(new Float32Array(d));
    let peak = 0;
    for (let i = 0; i < d.length; i++) peak = Math.max(peak, Math.abs(d[i]));
    $('level').style.width = Math.min(100, peak * 140) + '%';
    const secs = chunks.length * 4096 / ctx.sampleRate;
    $('recstat').textContent = secs.toFixed(1) + 's captured'
      + (secs < 20 ? ' \u2014 keep going, 20s+ is better' : ' \u2014 plenty');
  };
  src.connect(node); node.connect(ctx.destination);
  $('rec').disabled = true; $('stop').disabled = false;
  $('rec').classList.add('rec');
};

$('stop').onclick = async () => {
  recording = false;
  $('rec').disabled = false; $('stop').disabled = true;
  $('rec').classList.remove('rec');
  $('level').style.width = '0';
  if (stream) stream.getTracks().forEach(t => t.stop());
  if (node) node.disconnect();
  const total = chunks.reduce((n, c) => n + c.length, 0);
  if (!total) { $('recstat').textContent = 'nothing captured'; return; }
  const flat = new Float32Array(total);
  let at = 0; chunks.forEach(c => { flat.set(c, at); at += c.length; });
  const pcm = new Int16Array(total);
  for (let i = 0; i < total; i++) {
    const v = Math.max(-1, Math.min(1, flat[i]));
    pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }
  const rate = ctx.sampleRate;
  if (ctx) await ctx.close();
  await postReference(pcm.buffer, rate);
};

$('pick').onclick = () => $('file').click();
$('file').onchange = async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  $('recstat').textContent = 'decoding ' + f.name + '\u2026';
  const ext = (f.name.split('.').pop() || '').toLowerCase();
  const body = await f.arrayBuffer();
  const r = await fetch('api/reference/wav?ext=' + encodeURIComponent(ext), {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream' },
    body,
  }).then(r => r.json());
  applyReference(r);
};

async function postReference(buf, rate) {
  $('recstat').textContent = 'saving\u2026';
  const r = await fetch('api/reference?sample_rate=' + rate, {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream' },
    body: buf,
  }).then(r => r.json());
  applyReference(r);
}

function applyReference(r) {
  if (r.error) { $('refqual').innerHTML = '<span class="bad">' + r.error + '</span>'; return; }
  refId = r.id;
  $('refplay').src = 'api/audio/' + r.file + '?t=' + Date.now();
  const q = r.quality;
  const bits = [q.duration_s.toFixed(1) + 's',
                (r.sample_rate / 1000).toFixed(1) + ' kHz',
                'peak ' + q.peak.toFixed(2),
                'rms ' + q.rms.toFixed(3),
                (q.silence_share * 100).toFixed(0) + '% silence'];
  let html = bits.join(' \u00b7 ');
  html += q.warnings.length
    ? ' <span class="warn">\u2014 ' + q.warnings.join(', ') + '</span>'
    : ' <span class="good">\u2014 looks usable</span>';
  $('refqual').innerHTML = html;
  $('recstat').textContent = 'reference ready';
  $('synth').disabled = false; $('save').disabled = false;
}

// ── synth ──
$('synth').onclick = async () => {
  let kwargs = {};
  const raw = $('knobs').value.trim();
  if (raw) {
    try { kwargs = JSON.parse(raw); }
    catch (e) { $('synthstat').innerHTML = '<span class="bad">bad JSON</span>'; return; }
  }
  $('synth').disabled = true;
  $('synthstat').textContent = 'generating\u2026 (first call loads the model)';
  const r = await fetch('api/synth', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      engine: $('engine').value, reference: refId,
      text: $('text').value, kwargs,
    }),
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
    body: JSON.stringify({ engine: $('engine').value, reference: refId, name }),
  }).then(r => r.json());
  if (r.error) { $('savestat').innerHTML = '<span class="bad">' + r.error + '</span>'; return; }
  let msg = '<span class="good">saved ' + r.path + '</span>';
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
  const rows = r.voices.map(v =>
    '<tr><td><code>' + v.name + '</code></td><td>'
    + (v.kb > 1024 ? (v.kb / 1024).toFixed(1) + ' MB' : v.kb.toFixed(0) + ' KB')
    + '</td></tr>').join('');
  $('voices').innerHTML = '<tr><th>existing voices</th><th>size</th></tr>' + rows;
}

loadEngines(); loadScript(); loadVoices();
</script>
</body>
</html>
"""
