// IA das pautas: cada pessoa escolhe o provedor e usa a própria conta.
const IA_HINTS = {
  claude: "Chave de API da Anthropic. A assinatura Claude Pro não vale para outros apps.",
  openai: "Chave de API da OpenAI, cobrada à parte do ChatGPT Plus.",
  gemini: "Chave do Google AI Studio. Tem plano gratuito, com limites.",
  openrouter: "Entre com a sua conta e use Claude, GPT, Gemini e outros com um login só.",
  ollama: "Roda no seu computador, sem conta e sem custo. Mais lento e menos preciso.",
};
const MODEL_PREFERENCE = ["claude-opus", "claude-sonnet", "anthropic/claude", "gpt-5", "gpt-4.1", "gemini-2.5-pro", "gemini-2.5-flash", "qwen2.5", "llama3"];
let iaState = null;
let iaPick = null;

function iaProv(id) { return iaState.providers.find((p) => p.id === id); }
function iaMsg(text, err = false) { const st = $("ia-status"); st.textContent = text; st.classList.toggle("err", err); }

function renderIASummary() {
  const p = iaProv(iaState.provider);
  $("ia-current").textContent = iaState.configurado ? `${p.nome} · ${p.modelo}` : "não configurada";
  $("pautas-using").textContent = iaState.configurado
    ? `As pautas serão feitas por ${p.nome}, modelo ${p.modelo}.`
    : "A IA das pautas ainda não está configurada.";
  $("pautas-go").textContent = iaState.configurado ? ($("pautas-go").dataset.done ? "Refazer pautas" : "Organizar por pautas") : "Configurar a IA";
}

function renderIAOptions() {
  const box = $("ia-options");
  box.querySelectorAll(".ia-opt").forEach((n) => n.remove());
  for (const p of iaState.providers) {
    const input = el("input", { type: "radio", name: "ia-provider", value: p.id, checked: p.id === iaPick });
    input.addEventListener("change", () => { iaPick = p.id; renderIAFields(); iaMsg(""); });
    box.append(el("label", { className: "ia-opt" }, input,
      el("b", { textContent: p.nome }),
      el("span", { textContent: IA_HINTS[p.id] }),
      p.configurado ? el("span", { className: "ok", textContent: "Configurado" }) : null));
  }
}

function renderIAFields() {
  const p = iaProv(iaPick);
  $("ia-key-field").hidden = !p.precisa_chave;
  $("ia-url-field").hidden = p.id !== "ollama";
  $("ia-or-login").hidden = p.id !== "openrouter";
  $("ia-forget").hidden = !p.tem_chave;
  $("ia-key").value = "";
  $("ia-key").placeholder = p.tem_chave ? "Chave salva. Cole outra para trocar." : "Cole a chave aqui";
  $("ia-key-link").href = p.chave_url;
  $("ia-key-saved").textContent = p.chave_final ? ` · salva, termina em ${p.chave_final}` : "";
  $("ia-url").value = iaState.ollama_url;
  $("ia-auto").checked = iaState.pautas_auto;
  $("ia-model").value = p.modelo || "";
  $("ia-models").replaceChildren();
}

function openIA(message = "") {
  iaPick = iaState.provider;
  renderIAOptions(); renderIAFields(); iaMsg(message);
  $("ia-panel").hidden = false; $("ia-open").setAttribute("aria-expanded", "true");
  $("ia-panel").scrollIntoView({ block: "start" });
}
function closeIA() { $("ia-panel").hidden = true; $("ia-open").setAttribute("aria-expanded", "false"); }

async function iaPost(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Erro ${res.status}`);
  return data;
}

async function iaTest() {
  const btn = $("ia-test"); btn.disabled = true; iaMsg("Testando a conexão…");
  try {
    const data = await iaPost("/ia/testar", { provider: iaPick, chave: $("ia-key").value, ollama_url: $("ia-url").value });
    $("ia-models").replaceChildren(...data.modelos.map((m) => el("option", { value: m })));
    if (!$("ia-model").value && data.modelos.length) {
      const pref = MODEL_PREFERENCE.map((k) => data.modelos.find((m) => m.includes(k))).find(Boolean);
      $("ia-model").value = pref || data.modelos[0];
    }
    iaMsg(`Conexão ok: ${data.modelos.length} modelo(s) disponível(is). Confira o modelo e clique em Salvar.`);
  } catch (err) { iaMsg(err.message, true); }
  finally { btn.disabled = false; }
}

async function iaSave() {
  const btn = $("ia-save"); btn.disabled = true;
  try {
    iaState = await iaPost("/ia", { provider: iaPick, chave: $("ia-key").value, modelo: $("ia-model").value, ollama_url: $("ia-url").value, pautas_auto: $("ia-auto").checked });
    renderIASummary(); renderIAOptions(); renderIAFields();
    const p = iaProv(iaPick);
    iaMsg(iaState.configurado ? `Salvo. As pautas serão feitas por ${p.nome}.`
      : (p.precisa_chave && !p.tem_chave ? "Salvo, mas falta a chave." : "Salvo, mas falta escolher o modelo."), !iaState.configurado);
  } catch (err) { iaMsg(err.message, true); }
  finally { btn.disabled = false; }
}

async function iaForget() {
  if (!confirm("Apagar a chave salva deste provedor neste computador?")) return;
  try { iaState = await iaPost("/ia/esquecer", { provider: iaPick }); renderIASummary(); renderIAOptions(); renderIAFields(); iaMsg("Chave apagada."); }
  catch (err) { iaMsg(err.message, true); }
}

// Login no OpenRouter (OAuth com PKCE): sai para o site dele e volta com ?code=.
function b64url(bytes) { return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
async function openRouterLogin() {
  if (job && !confirm("Entrar no OpenRouter sai desta página e a transcrição aberta será perdida da tela. Continuar?")) return;
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = b64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
  sessionStorage.setItem("or_verifier", verifier);
  const callback = location.origin + location.pathname;
  location.href = `https://openrouter.ai/auth?callback_url=${encodeURIComponent(callback)}&code_challenge=${challenge}&code_challenge_method=S256`;
}
async function finishOpenRouterLogin() {
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  if (!code) return;
  history.replaceState(null, "", location.pathname);
  const verifier = sessionStorage.getItem("or_verifier");
  sessionStorage.removeItem("or_verifier");
  if (!verifier) return;
  try {
    iaState = await iaPost("/ia/openrouter", { code, verifier });
    renderIASummary();
    openIA("Conectado ao OpenRouter. Testando a conexão para listar os modelos…");
    await iaTest();
  } catch (err) { openIA(err.message); iaMsg(err.message, true); }
}

$("ia-open").addEventListener("click", () => ($("ia-panel").hidden ? openIA() : closeIA()));
$("ia-close").addEventListener("click", closeIA);
$("ia-test").addEventListener("click", iaTest);
$("ia-save").addEventListener("click", iaSave);
$("ia-forget").addEventListener("click", iaForget);
$("ia-or-btn").addEventListener("click", openRouterLogin);

(async () => {
  try {
    const res = await fetch("/ia");
    iaState = await res.json();
    renderIASummary();
    await finishOpenRouterLogin();
  } catch (_) {
    $("ia-current").textContent = "indisponível";
  }
})();
