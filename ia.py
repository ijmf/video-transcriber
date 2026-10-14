"""IA das pautas: cada pessoa escolhe o provedor e usa a própria conta.

A escolha e as chaves ficam em ia_config.json, ao lado do app (fora do git).
Variáveis do .env (ANTHROPIC_API_KEY etc.) continuam valendo como chave padrão.

Claude usa o SDK da Anthropic. OpenAI, Gemini, OpenRouter e Ollama falam a API
compatível com a da OpenAI, então um só adaptador (httpx) atende os quatro.
"""

import json
import os
import re
import threading

import anthropic
import httpx

# No Docker, IA_CONFIG aponta para um volume, para a escolha sobreviver a uma nova imagem.
CONFIG_PATH = os.environ.get("IA_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ia_config.json")
DEFAULT_OLLAMA_URL = "http://localhost:11434"
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
TIMEOUT = httpx.Timeout(900.0, connect=15.0)  # Ollama na CPU pode levar vários minutos

PROVIDERS = {
    "claude": {
        "nome": "Claude (Anthropic)",
        "env": "ANTHROPIC_API_KEY",
        "chave_url": "https://platform.claude.com/settings/keys",
        "modelo_padrao": os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
    },
    "openai": {
        "nome": "ChatGPT (OpenAI)",
        "env": "OPENAI_API_KEY",
        "base": "https://api.openai.com/v1",
        "chave_url": "https://platform.openai.com/api-keys",
        "modelo_padrao": "",
    },
    "gemini": {
        "nome": "Gemini (Google)",
        "env": "GEMINI_API_KEY",
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "chave_url": "https://aistudio.google.com/apikey",
        "modelo_padrao": "",
    },
    "openrouter": {
        "nome": "OpenRouter",
        "env": "OPENROUTER_API_KEY",
        "base": "https://openrouter.ai/api/v1",
        "chave_url": "https://openrouter.ai/keys",
        "modelo_padrao": "",
    },
    "ollama": {
        "nome": "Ollama (local)",
        "env": None,
        "chave_url": "https://ollama.com/download",
        "modelo_padrao": "",
    },
}

_lock = threading.Lock()


class IAError(Exception):
    """Erro com mensagem pronta para mostrar na tela."""


# --- Configuração ------------------------------------------------------------------


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def current_provider(cfg=None):
    cfg = load_config() if cfg is None else cfg
    provider = cfg.get("provider")
    return provider if provider in PROVIDERS else "claude"


def api_key(provider, cfg=None):
    cfg = load_config() if cfg is None else cfg
    key = (cfg.get("chaves") or {}).get(provider, "")
    env = PROVIDERS[provider]["env"]
    if not key and env:
        key = os.environ.get(env, "")
    return key.strip()


def model_for(provider, cfg=None):
    cfg = load_config() if cfg is None else cfg
    return (cfg.get("modelos") or {}).get(provider) or PROVIDERS[provider]["modelo_padrao"]


def ollama_url(cfg=None):
    cfg = load_config() if cfg is None else cfg
    return (cfg.get("ollama_url") or DEFAULT_OLLAMA_URL).rstrip("/")


def _base_url(provider, cfg):
    if provider == "ollama":
        return ollama_url(cfg) + "/v1"
    return PROVIDERS[provider]["base"]


def auto_enabled(cfg=None):
    """Gerar as pautas assim que a transcrição termina (ligado se nunca foi mudado)."""
    cfg = load_config() if cfg is None else cfg
    return cfg.get("pautas_auto", True) is not False


def is_configured(provider, cfg=None):
    cfg = load_config() if cfg is None else cfg
    if provider == "ollama":
        return bool(model_for(provider, cfg))
    return bool(api_key(provider, cfg)) and bool(model_for(provider, cfg))


def _mask(key):
    return key[-4:] if len(key) >= 8 else ""


def public_state():
    """O que a página pode ver: nunca devolve a chave inteira."""
    cfg = load_config()
    provider = current_provider(cfg)
    return {
        "provider": provider,
        "configurado": is_configured(provider, cfg),
        "pautas_auto": auto_enabled(cfg),
        "ollama_url": ollama_url(cfg),
        "providers": [
            {
                "id": pid,
                "nome": info["nome"],
                "chave_url": info["chave_url"],
                "precisa_chave": pid != "ollama",
                "chave_final": _mask(api_key(pid, cfg)) if pid != "ollama" else "",
                "tem_chave": bool(api_key(pid, cfg)) if pid != "ollama" else False,
                "modelo": model_for(pid, cfg),
                "configurado": is_configured(pid, cfg),
            }
            for pid, info in PROVIDERS.items()
        ],
    }


def update(data):
    provider = data.get("provider")
    if provider not in PROVIDERS:
        raise IAError("Provedor desconhecido.")
    with _lock:
        cfg = load_config()
        cfg["provider"] = provider
        chave = data.get("chave")
        if isinstance(chave, str) and chave.strip() and provider != "ollama":
            cfg.setdefault("chaves", {})[provider] = chave.strip()
        modelo = data.get("modelo")
        if isinstance(modelo, str):
            if modelo.strip():
                cfg.setdefault("modelos", {})[provider] = modelo.strip()
            else:
                cfg.setdefault("modelos", {}).pop(provider, None)
        url = data.get("ollama_url")
        if isinstance(url, str) and url.strip():
            if not re.match(r"^https?://", url.strip()):
                raise IAError("O endereço do Ollama deve começar com http:// ou https://.")
            cfg["ollama_url"] = url.strip().rstrip("/")
        if isinstance(data.get("pautas_auto"), bool):
            cfg["pautas_auto"] = data["pautas_auto"]
        _save_config(cfg)
    return public_state()


def forget_key(provider):
    with _lock:
        cfg = load_config()
        (cfg.get("chaves") or {}).pop(provider, None)
        _save_config(cfg)
    return public_state()


# --- Conexão -------------------------------------------------------------------------


def _headers(provider, key):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if provider == "openrouter":
        headers["HTTP-Referer"] = "http://localhost"
        headers["X-Title"] = "Video Transcriber"
    return headers


def _http_error(provider, exc):
    nome = PROVIDERS[provider]["nome"]
    if isinstance(exc, httpx.ConnectError):
        if provider == "ollama":
            return IAError("Não foi possível falar com o Ollama. Ele está instalado e aberto? "
                           "Confira o endereço em IA das pautas.")
        return IAError(f"Não foi possível conectar ao {nome}. Confira a internet.")
    if isinstance(exc, httpx.TimeoutException):
        return IAError(f"O {nome} demorou demais para responder.")
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return IAError(f"A chave do {nome} foi recusada. Confira em IA das pautas.")
        if code == 402:
            return IAError(f"Sem créditos no {nome}. Adicione créditos na sua conta.")
        if code == 404:
            return IAError(f"O modelo escolhido não existe no {nome}. Use Testar conexão "
                           "para ver os disponíveis.")
        if code == 429:
            return IAError(f"Limite de uso do {nome} atingido. Tente de novo em instantes.")
        detail = exc.response.text[:300]
        return IAError(f"Erro do {nome} ({code}): {detail}")
    return IAError(f"Erro ao falar com o {nome}: {exc}")


def list_models(provider, chave=None, url=None):
    """Testa a conexão e devolve os modelos disponíveis para essa conta."""
    if provider not in PROVIDERS:
        raise IAError("Provedor desconhecido.")
    cfg = load_config()
    if url:
        cfg = {**cfg, "ollama_url": url}
    key = (chave or "").strip() or api_key(provider, cfg)
    if provider != "ollama" and not key:
        raise IAError("Cole a chave de API para testar.")

    if provider == "claude":
        try:
            client = anthropic.Anthropic(api_key=key)
            return sorted((m.id for m in client.models.list(limit=100)), reverse=True)
        except anthropic.AuthenticationError:
            raise IAError("A chave do Claude (Anthropic) foi recusada.")
        except anthropic.APIConnectionError:
            raise IAError("Não foi possível conectar à Anthropic. Confira a internet.")
        except anthropic.APIStatusError as exc:
            raise IAError(f"Erro da Anthropic ({exc.status_code}): {exc.message}")

    try:
        resp = httpx.get(f"{_base_url(provider, cfg)}/models",
                         headers=_headers(provider, key), timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise _http_error(provider, exc)
    ids = [m.get("id", "") for m in resp.json().get("data", [])]
    ids = sorted({i.removeprefix("models/") for i in ids if i})
    if provider == "ollama" and not ids:
        raise IAError("O Ollama está rodando, mas não tem nenhum modelo. "
                      "Baixe um no terminal, ex.: ollama pull qwen2.5:7b")
    return ids


def openrouter_exchange(code, verifier):
    """Troca o código do login no OpenRouter por uma chave e salva."""
    try:
        resp = httpx.post(
            "https://openrouter.ai/api/v1/auth/keys",
            json={"code": code, "code_verifier": verifier, "code_challenge_method": "S256"},
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise _http_error("openrouter", exc)
    key = resp.json().get("key", "")
    if not key:
        raise IAError("O OpenRouter não devolveu uma chave. Tente entrar de novo.")
    with _lock:
        cfg = load_config()
        cfg["provider"] = "openrouter"
        cfg.setdefault("chaves", {})["openrouter"] = key
        _save_config(cfg)
    return public_state()


# --- Gerar as pautas -----------------------------------------------------------------


def _parse_json(text):
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise IAError("A IA não devolveu as pautas no formato esperado. Tente de novo.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise IAError("A IA não devolveu as pautas no formato esperado. Tente de novo.")


def _normalize(data):
    """Modelos menores às vezes omitem campos; completa para a página não quebrar."""
    def text(v):
        return v if isinstance(v, str) else ""

    def items(v, fields):
        out = []
        for it in v if isinstance(v, list) else []:
            if isinstance(it, dict):
                out.append({f: text(it.get(f)) for f in fields})
            elif isinstance(it, str):
                out.append({f: (it if f == fields[0] else "") for f in fields})
        return out

    item = ("descricao", "timestamp")
    return {
        "titulo": text(data.get("titulo")),
        "objetivo": text(data.get("objetivo")),
        "contexto": text(data.get("contexto")),
        "participantes": items(data.get("participantes"), ("rotulo", "nome_sugerido", "papel")),
        "problemas_levantados": items(data.get("problemas_levantados"), item),
        "decisoes": items(data.get("decisoes"), item),
        "solucoes_propostas": items(data.get("solucoes_propostas"), item),
        "pendencias": items(data.get("pendencias"), ("descricao", "responsavel", "prazo", "timestamp")),
        "proximos_passos": items(data.get("proximos_passos"), item),
    }


def _generate_claude(transcript, system, schema, key, model):
    client = anthropic.Anthropic(api_key=key)
    with client.beta.messages.stream(
        model=model,
        max_tokens=32000,
        system=system,
        messages=[{"role": "user", "content": f"<transcricao>\n{transcript}\n</transcricao>"}],
        output_config={"effort": CLAUDE_EFFORT, "format": {"type": "json_schema", "schema": schema}},
        # Se o classificador de segurança recusar por engano, a API refaz o
        # pedido em outro modelo em vez de devolver a recusa.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise IAError("O Claude recusou organizar esta transcrição.")
    if response.stop_reason == "max_tokens":
        raise IAError("A resposta do Claude foi cortada por ser longa demais.")
    return json.loads(next(b.text for b in response.content if b.type == "text"))


def _generate_compatible(provider, transcript, system, schema, key, model, cfg):
    # O formato do JSON vai também no prompt: nem todo modelo respeita response_format.
    prompt = (f"{system}\n\nResponda somente com um objeto JSON válido, sem texto antes ou "
              f"depois, seguindo este JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"<transcricao>\n{transcript}\n</transcricao>"},
        ],
    }
    formats = [
        {"type": "json_schema", "json_schema": {"name": "pautas", "schema": schema, "strict": True}},
        {"type": "json_object"},
        None,
    ]
    url = f"{_base_url(provider, cfg)}/chat/completions"
    for fmt in formats:
        body = {**payload, **({"response_format": fmt} if fmt else {})}
        try:
            resp = httpx.post(url, json=body, headers=_headers(provider, key), timeout=TIMEOUT)
            if resp.status_code in (400, 422) and fmt is not None:
                continue  # este modelo não aceita esse formato; tenta o próximo (o último vai sem)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise _http_error(provider, exc)
        choice = (resp.json().get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise IAError("A resposta da IA foi cortada por ser longa demais.")
        return _parse_json((choice.get("message") or {}).get("content"))
    raise IAError("A IA não aceitou o pedido.")  # inalcançável: a última tentativa sempre retorna ou levanta


def generate(transcript, system, schema):
    cfg = load_config()
    provider = current_provider(cfg)
    nome = PROVIDERS[provider]["nome"]
    model = model_for(provider, cfg)
    key = api_key(provider, cfg)
    if provider != "ollama" and not key:
        raise IAError(f"Falta a chave do {nome}. Configure em IA das pautas.")
    if not model:
        raise IAError(f"Escolha o modelo do {nome} em IA das pautas.")
    if provider == "claude":
        result = _generate_claude(transcript, system, schema, key, model)
    else:
        result = _generate_compatible(provider, transcript, system, schema, key, model, cfg)
    return _normalize(result)
