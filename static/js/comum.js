// Comum a todos os scripts da página. A configuração vem do servidor num bloco JSON
// do index.html; os scripts são clássicos e compartilham o escopo global, na ordem:
// comum, envio, ia, historico, gravacao.
const CONFIG = JSON.parse(document.getElementById("config").textContent);
const MAX_BYTES = CONFIG.maxBytes, MAX_LABEL = CONFIG.maxLabel, LANGUAGES = CONFIG.languages;
const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const n = Object.assign(document.createElement(tag), props);
  n.append(...children.filter(Boolean));
  return n;
}
function flash(btn, text) {
  const original = btn.dataset.label || btn.textContent;
  btn.dataset.label = original;
  btn.textContent = text;
  clearTimeout(btn._t);
  btn._t = setTimeout(() => (btn.textContent = original), 1600);
}

function fmtClock(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
