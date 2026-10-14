// Enviar: o processo inteiro acontece aqui — envio, progresso, falantes, pautas e transcrição.
const ALLOWED = [".mp4", ".mov", ".avi", ".mkv", ".mp3", ".webm"];
// Lembra o último idioma escolhido neste navegador (só uma conveniência).
try {
  const saved = localStorage.getItem("idioma");
  if (saved && LANGUAGES[saved]) $("idioma").value = saved;
} catch (_) {}
$("idioma").addEventListener("change", (e) => {
  try { localStorage.setItem("idioma", e.target.value); } catch (_) {}
});
const STEP_ORDER = ["upload", "extracting", "prepare", "transcribing"];
const STATUS_STEP = { queued: "upload", extracting: "extracting", uploading: "prepare", loading: "prepare", transcribing: "transcribing" };
const OFFLINE = "Não foi possível falar com o app. Confira se o python app.py ainda está rodando e tente de novo.";
const PAUTAS_HINT = "Objetivo, contexto, problemas, decisões, soluções, pendências com responsável e prazo, e próximos passos, cada item com o minuto em que foi dito. Leva de 30 segundos a 2 minutos.";
const drop = $("drop"), videoInput = $("video"), sendStatus = $("send-status");
const work = $("work"), run = $("run"), results = $("results");
const runMeter = $("run-meter"), runBar = runMeter.firstElementChild;
let job = null, segments = [], speakers = {}, fullText = "", pautas = null, pautasTimer = 0;

function resetAll() {
  clearTimeout(pautasTimer);
  job = null; segments = []; speakers = {}; fullText = ""; pautas = null;
  work.hidden = true; results.hidden = true; run.hidden = false;
  drop.hidden = false; drop.classList.remove("busy"); videoInput.value = "";
  $("rec").hidden = false;
  sendStatus.textContent = ""; sendStatus.classList.remove("err");
  $("pautas-out").replaceChildren();
  $("pautas-status").classList.remove("err");
  $("pautas-status").textContent = PAUTAS_HINT;
  delete $("pautas-go").dataset.done;
  $("pautas-go").disabled = false;
  if (iaState) renderIASummary(); else $("pautas-go").textContent = "Organizar por pautas";
  $("pautas-copy").hidden = true; $("pautas-dl").hidden = true;
  $("spk-note").classList.remove("err");
  $("spk-note").textContent = "Dê nome a cada falante; a transcrição e as pautas passam a usar esses nomes.";
}
function failed(msg) {
  work.hidden = true; drop.hidden = false; drop.classList.remove("busy"); videoInput.value = "";
  $("rec").hidden = false;
  sendStatus.textContent = msg; sendStatus.classList.add("err");
}
function setProgress(text, pct, step) {
  $("run-text").textContent = text;
  const ind = pct === null;
  runMeter.classList.toggle("indeterminate", ind);
  runBar.style.transform = ind ? "" : `scaleX(${pct / 100})`;
  $("run-pct").textContent = ind ? "" : Math.floor(pct) + "%";
  const idx = STEP_ORDER.indexOf(step);
  document.querySelectorAll(".run-steps li").forEach((li) => {
    const k = STEP_ORDER.indexOf(li.dataset.step);
    li.classList.toggle("active", k === idx);
    li.classList.toggle("done", idx > k || step === "done");
  });
}

function sendVideo(file, recordingUrl = null) {
  const ext = "." + file.name.split(".").pop().toLowerCase();
  if (!ALLOWED.includes(ext)) return failed(`Formato não suportado: ${ext}. Envie um vídeo MP4, MOV, AVI ou MKV, ou um áudio MP3.`);
  // Avisa antes de enviar: o servidor só recusaria depois de receber o arquivo inteiro.
  if (file.size > MAX_BYTES) {
    const gb = (file.size / 1024 ** 3).toFixed(1).replace(".", ",");
    return failed(`O arquivo tem ${gb} GB; o limite é ${MAX_LABEL}. Envie só o áudio (MP3) ou um vídeo em resolução menor.`);
  }
  resetAll();
  drop.hidden = true; $("rec").hidden = true; work.hidden = false;
  const recDl = $("rec-dl");
  recDl.hidden = !recordingUrl;
  if (recordingUrl) { recDl.href = recordingUrl; recDl.download = file.name; }
  $("run-file").textContent = file.name;
  setProgress("Enviando o arquivo…", 0, "upload");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  work.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  const form = new FormData();
  form.append("video", file);
  form.append("idioma", $("idioma").value);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/upload");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) setProgress("Enviando o arquivo…", (e.loaded / e.total) * 100, "upload"); };
  xhr.upload.onload = () => setProgress("Conferindo a duração…", null, "upload");
  xhr.onerror = () => failed(OFFLINE);
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) {}
    if ([404, 405, 501].includes(xhr.status)) return failed(OFFLINE);
    if (xhr.status !== 200 || !data.job_id) return failed(data.error || `Erro ${xhr.status} ao enviar o arquivo.`);
    job = data.job_id;
    poll();
  };
  xhr.send(form);
}

async function poll() {
  let data;
  try {
    const res = await fetch(`/status/${job}`);
    data = await res.json();
    if (!res.ok) throw new Error(data.error || `Erro ${res.status}`);
  } catch (err) {
    return failed("Não foi possível acompanhar o processamento: " + err.message);
  }
  if (data.status === "error") return failed(data.message);
  if (data.status === "done") return showResults(data);
  // Só o Whisper local informa a porcentagem; nas outras etapas a barra fica em movimento.
  const pct = data.status === "transcribing" && data.engine === "whisper" ? data.progress : null;
  setProgress(data.message, pct, STATUS_STEP[data.status] || "upload");
  setTimeout(poll, 1000);
}

function showResults(data) {
  segments = data.segments; speakers = data.speakers; fullText = data.text;
  setProgress(data.message, 100, "done");
  run.hidden = true; results.hidden = false;
  $("res-file").textContent = $("run-file").textContent +
    (data.language && data.language !== "pt" && LANGUAGES[data.language] ? ` · ${LANGUAGES[data.language]}` : "");
  $("spk-pane").hidden = !Object.keys(speakers).length;
  $("pautas-pane").hidden = !segments.length;
  renderSpeakers();
  renderTranscript();
  applyPautasState(data);
  loadHistory();
}
// Pautas prontas, sendo geradas sozinhas ao fim da transcrição, ou com erro.
function applyPautasState(data) {
  const btn = $("pautas-go"), status = $("pautas-status");
  clearTimeout(pautasTimer);
  if (data.pautas) {
    pautas = data.pautas;
    renderPautas();
    status.classList.remove("err"); status.textContent = "";
    btn.dataset.done = "1"; btn.textContent = "Refazer pautas";
    $("pautas-copy").hidden = false; $("pautas-dl").hidden = false;
  }
  if (data.pautas_status === "gerando") {
    btn.disabled = true;
    status.classList.remove("err");
    status.textContent = `Gerando as pautas automaticamente${iaState && iaState.configurado ? " com " + iaProv(iaState.provider).nome : ""}. Costuma levar de 30 segundos a 2 minutos; a transcrição já está ao lado.`;
    const current = job;
    pautasTimer = setTimeout(async () => {
      if (job !== current) return;
      try {
        const res = await fetch(`/status/${current}`);
        const next = await res.json();
        if (job === current && res.ok) applyPautasState(next);
      } catch (_) {
        pautasTimer = setTimeout(() => applyPautasState(data), 2000);
      }
    }, 2000);
    return;
  }
  btn.disabled = false;
  if (data.pautas_status === "erro") {
    status.classList.add("err");
    status.textContent = "Não foi possível gerar as pautas automaticamente: " + data.pautas_error;
  }
  if (data.pautas) loadHistory();
}

function renderTranscript() {
  const out = $("tx-out");
  out.replaceChildren();
  if (!segments.length) {
    out.append(el("p", { className: "empty-note", textContent: "Nenhuma fala detectada na gravação." }));
    return;
  }
  for (const seg of segments) {
    out.append(el("div", { className: "tx-line" },
      el("span", { className: "t", textContent: seg.timestamp }),
      el("span", {}, seg.speaker ? el("b", { textContent: speakers[seg.speaker] }) : null, seg.text)));
  }
}

function renderSpeakers(suggestions = {}) {
  const list = $("spk-list");
  list.replaceChildren();
  for (const [code, name] of Object.entries(speakers)) {
    const input = el("input", { type: "text", id: `spk-${code}`, value: suggestions[code] || name, maxLength: 60, autocomplete: "off" });
    input.dataset.code = code;
    if (suggestions[code]) input.classList.add("suggested");
    list.append(el("div", { className: "spk" },
      el("label", { htmlFor: input.id, textContent: `Falante ${code}` }), input,
      suggestions[code] ? el("small", { textContent: "Sugerido pela IA. Confira e salve." }) : null));
  }
}

$("spk-save").addEventListener("click", async () => {
  const btn = $("spk-save"), note = $("spk-note"), names = {};
  document.querySelectorAll("#spk-list input").forEach((i) => (names[i.dataset.code] = i.value));
  btn.disabled = true;
  try {
    const res = await fetch(`/speakers/${job}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(names),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Erro ${res.status}`);
    speakers = data.speakers; fullText = data.text;
    renderSpeakers();
    renderTranscript();
    note.classList.remove("err");
    note.textContent = pautas
      ? "Nomes salvos. Clique em Refazer pautas para usá-los lá também."
      : "Nomes salvos. A transcrição já usa esses nomes.";
    flash(btn, "Salvo");
  } catch (err) {
    note.classList.add("err");
    note.textContent = "Não foi possível salvar os nomes: " + err.message;
  } finally {
    btn.disabled = false;
  }
});

$("pautas-go").addEventListener("click", async () => {
  if (!iaState || !iaState.configurado) return openIA("Configure a IA das pautas e depois clique em Organizar por pautas.");
  const btn = $("pautas-go"), status = $("pautas-status");
  btn.disabled = true;
  status.classList.remove("err");
  status.textContent = `Organizando a transcrição com ${iaProv(iaState.provider).nome}. Costuma levar de 30 segundos a 2 minutos…`;
  try {
    const res = await fetch(`/pautas/${job}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Erro ${res.status}`);
    pautas = data.pautas;
    renderPautas();
    status.textContent = "";
    btn.dataset.done = "1"; btn.textContent = "Refazer pautas";
    $("pautas-copy").hidden = false; $("pautas-dl").hidden = false;
    loadHistory();
  } catch (err) {
    status.classList.add("err");
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

function renderPautas() {
  const p = pautas, out = $("pautas-out");
  out.replaceChildren();
  const section = (title, ...content) => out.append(el("h4", { textContent: title }), ...content);
  const item = (i, extra = []) => el("li", {}, i.descricao, ...extra,
    i.timestamp ? el("span", { className: "tag-t", textContent: i.timestamp }) : null);
  if (p.titulo) out.append(el("p", { className: "title", textContent: p.titulo }));
  if (p.objetivo) section("Objetivo", el("p", { textContent: p.objetivo }));
  if (p.contexto) section("Contexto", el("p", { textContent: p.contexto }));
  if (p.participantes.length) {
    section("Participantes", el("ul", {}, ...p.participantes.map((x) =>
      el("li", { textContent: x.rotulo + (x.papel ? ` — ${x.papel}` : "") }))));
  }
  for (const [title, items] of [["Problemas levantados", p.problemas_levantados], ["Decisões", p.decisoes], ["Soluções propostas", p.solucoes_propostas]]) {
    if (items.length) section(title, el("ul", {}, ...items.map((i) => item(i))));
  }
  if (p.pendencias.length) {
    section("Pendências", el("ul", {}, ...p.pendencias.map((i) => item(i, [
      i.responsavel ? el("span", { className: "tag-o", textContent: i.responsavel }) : null,
      i.prazo ? el("span", { className: "tag-d", textContent: i.prazo }) : null,
    ]))));
  }
  if (p.proximos_passos.length) section("Próximos passos", el("ul", {}, ...p.proximos_passos.map((i) => item(i))));

  // Nomes sugeridos pela IA para falantes que ainda têm o rótulo genérico.
  const suggestions = {};
  for (const part of p.participantes) {
    const code = Object.keys(speakers).find((c) => speakers[c] === part.rotulo);
    if (code && part.nome_sugerido && speakers[code] === `Falante ${code}`) suggestions[code] = part.nome_sugerido;
  }
  if (Object.keys(suggestions).length) renderSpeakers(suggestions);
}

$("pautas-copy").addEventListener("click", async () => {
  try {
    const res = await fetch(`/download/${job}?tipo=pautas`);
    await navigator.clipboard.writeText(await res.text());
    flash($("pautas-copy"), "Copiado");
  } catch (_) {
    flash($("pautas-copy"), "Falha ao copiar");
  }
});
$("pautas-dl").addEventListener("click", () => { location.href = `/download/${job}?tipo=pautas`; });
$("tx-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(fullText);
    flash($("tx-copy"), "Copiado");
  } catch (_) {
    flash($("tx-copy"), "Falha ao copiar");
  }
});
$("tx-dl").addEventListener("click", () => { location.href = `/download/${job}`; });
$("again").addEventListener("click", () => { resetAll(); loadHistory(); drop.scrollIntoView({ block: "center" }); });

videoInput.addEventListener("change", () => { if (videoInput.files[0]) sendVideo(videoInput.files[0]); });
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) sendVideo(f); });
