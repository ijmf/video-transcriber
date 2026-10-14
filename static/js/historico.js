// Histórico: transcrições guardadas no historico.db, que sobrevivem a reiniciar o app.
function fmtWhen(sec) {
  return new Date(sec * 1000).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}
function histMsg(text, err = false) { const st = $("hist-status"); st.textContent = text; st.classList.toggle("err", err); }
async function loadHistory() {
  let data;
  try {
    const res = await fetch("/historico");
    data = await res.json();
    if (!res.ok) throw new Error(data.error || `Erro ${res.status}`);
  } catch (_) { return; }
  const list = $("hist-list");
  list.replaceChildren(...data.transcricoes.map((t) => {
    const open = el("button", { type: "button", className: "button ghost", textContent: "Abrir" });
    open.addEventListener("click", () => openFromHistory(t.id, t.filename));
    const del = el("button", { type: "button", className: "button ghost", textContent: "Apagar" });
    del.setAttribute("aria-label", `Apagar ${t.filename}`);
    open.setAttribute("aria-label", `Abrir ${t.filename}`);
    del.addEventListener("click", () => deleteFromHistory(t.id, t.filename));
    const info = [fmtWhen(t.criado_em), fmtClock(t.duration),
      t.idioma && t.idioma !== "pt" ? LANGUAGES[t.idioma] || t.idioma : "",
      t.tem_pautas ? "com pautas" : "sem pautas"].filter(Boolean).join(" · ");
    return el("li", { className: "hist-item" + (t.id === job ? " current" : "") },
      el("div", {}, el("b", { textContent: t.titulo || t.filename }),
        el("small", { textContent: t.titulo ? `${t.filename} · ${info}` : info })),
      el("div", { className: "pane-actions" }, open, del));
  }));
  $("hist").hidden = !data.transcricoes.length;
}
async function openFromHistory(id, filename) {
  let data;
  try {
    const res = await fetch(`/status/${id}`);
    data = await res.json();
    if (!res.ok) throw new Error(data.error || `Erro ${res.status}`);
  } catch (err) {
    return histMsg("Não foi possível abrir: " + err.message, true);
  }
  histMsg("");
  resetAll();
  job = id;
  drop.hidden = true; $("rec").hidden = true; work.hidden = false;
  $("rec-dl").hidden = true;
  $("run-file").textContent = filename;
  showResults(data);
  work.scrollIntoView({ block: "start" });
}
async function deleteFromHistory(id, filename) {
  if (!confirm(`Apagar "${filename}" do histórico? A transcrição e as pautas serão perdidas.`)) return;
  try {
    const res = await fetch(`/historico/${id}`, { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Erro ${res.status}`);
  } catch (err) {
    return histMsg("Não foi possível apagar: " + err.message, true);
  }
  histMsg("");
  if (id === job) resetAll();
  loadHistory();
}
loadHistory();
