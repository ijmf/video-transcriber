// Gravar a reunião: microfone + áudio do computador (compartilhamento de tela com áudio),
// misturados num só arquivo .webm que entra no mesmo fluxo de envio.
const REC_LIMIT = 45 * 60;
let rec = null;

function recFail(msg) {
  sendStatus.textContent = msg;
  sendStatus.classList.add("err");
}

function meterLoop(analyser, bar) {
  const data = new Uint8Array(analyser.fftSize);
  return () => {
    analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (const v of data) sum += ((v - 128) / 128) ** 2;
    bar.style.transform = `scaleX(${Math.min(1, Math.sqrt(sum / data.length) * 4)})`;
  };
}

async function startRecording() {
  sendStatus.textContent = ""; sendStatus.classList.remove("err");
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    return recFail("Este navegador não grava áudio. Use o Chrome ou o Edge.");
  }
  let mic;
  try {
    mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (_) {
    return recFail("Sem acesso ao microfone. Libere o microfone para esta página (ícone ao lado do endereço) e tente de novo.");
  }
  // O áudio do computador vem do compartilhamento de tela; o navegador exige pedir vídeo junto.
  let display = null;
  if (navigator.mediaDevices.getDisplayMedia) {
    try {
      display = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
        systemAudio: "include", selfBrowserSurface: "exclude",
      });
    } catch (_) { display = null; }
  }
  const sysTracks = display ? display.getAudioTracks() : [];

  const ctx = new AudioContext();
  const dest = ctx.createMediaStreamDestination();
  const loops = [];
  const micSrc = ctx.createMediaStreamSource(mic);
  const micAn = ctx.createAnalyser(); micAn.fftSize = 1024;
  micSrc.connect(micAn); micSrc.connect(dest);
  loops.push(meterLoop(micAn, $("lvl-mic").firstElementChild));
  $("lvl-sys").classList.toggle("off", !sysTracks.length);
  if (sysTracks.length) {
    const sysSrc = ctx.createMediaStreamSource(new MediaStream(sysTracks));
    const sysAn = ctx.createAnalyser(); sysAn.fftSize = 1024;
    sysSrc.connect(sysAn); sysSrc.connect(dest);
    loops.push(meterLoop(sysAn, $("lvl-sys").firstElementChild));
  } else {
    $("lvl-sys").firstElementChild.style.transform = "scaleX(0)";
  }

  const mimeType = ["audio/webm;codecs=opus", "audio/webm"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
  const recorder = new MediaRecorder(dest.stream, { mimeType, audioBitsPerSecond: 64000 });
  const chunks = [];
  recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };

  rec = { recorder, chunks, ctx, streams: [mic, display].filter(Boolean), started: Date.now(), timer: 0, raf: 0, sys: sysTracks.length > 0 };
  const msg = $("rec-msg");
  msg.classList.remove("warn");
  msg.textContent = rec.sys
    ? "Gravando a sua voz e o áudio da reunião."
    : "Gravando só o microfone: o áudio do computador não foi compartilhado. A voz da outra pessoa só entra se sair pelas caixas de som.";
  if (display) {
    display.getVideoTracks().forEach((t) => t.addEventListener("ended", () => {
      if (rec && rec.sys) { msg.classList.add("warn"); msg.textContent = "O compartilhamento de tela foi encerrado: daqui em diante, só o microfone está sendo gravado."; }
    }));
  }

  $("rec-idle").hidden = true; $("rec-live").hidden = false; drop.hidden = true;
  recorder.start(1000);
  const tick = () => {
    const sec = (Date.now() - rec.started) / 1000;
    $("rec-time").textContent = fmtClock(sec);
    if (sec >= REC_LIMIT - 60 && sec < REC_LIMIT - 1 && !msg.classList.contains("warn")) {
      msg.classList.add("warn");
      msg.textContent = "Falta 1 minuto para o limite de 45 minutos. A gravação para e é transcrita sozinha.";
    }
    if (sec >= REC_LIMIT - 1) stopRecording(true);
  };
  rec.timer = setInterval(tick, 250);
  const draw = () => { loops.forEach((f) => f()); if (rec) rec.raf = requestAnimationFrame(draw); };
  draw();
  window.addEventListener("beforeunload", warnLeave);
}

function warnLeave(e) { e.preventDefault(); e.returnValue = ""; }

function stopRecording(send) {
  if (!rec) return;
  const r = rec;
  rec = null;
  clearInterval(r.timer); cancelAnimationFrame(r.raf);
  window.removeEventListener("beforeunload", warnLeave);
  r.recorder.onstop = () => {
    r.streams.forEach((st) => st.getTracks().forEach((t) => t.stop()));
    r.ctx.close();
    $("rec-live").hidden = true; $("rec-idle").hidden = false; drop.hidden = false;
    document.querySelectorAll(".lvl i").forEach((i) => (i.style.transform = "scaleX(0)"));
    $("rec-time").textContent = "00:00";
    if (!send) return;
    const blob = new Blob(r.chunks, { type: r.recorder.mimeType || "audio/webm" });
    if (!blob.size) return recFail("A gravação ficou vazia. Confira o microfone e tente de novo.");
    const d = new Date(r.started), pad = (n) => String(n).padStart(2, "0");
    const name = `reuniao-${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-${pad(d.getHours())}h${pad(d.getMinutes())}.webm`;
    sendVideo(new File([blob], name, { type: blob.type }), URL.createObjectURL(blob));
  };
  r.recorder.stop();
}

$("rec-start").addEventListener("click", startRecording);
$("rec-stop").addEventListener("click", () => stopRecording(true));
$("rec-cancel").addEventListener("click", () => {
  if (confirm("Descartar a gravação? O áudio gravado até agora será perdido.")) stopRecording(false);
});
