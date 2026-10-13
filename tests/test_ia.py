"""Testes de ia.py: configuração, adaptador OpenAI-compatível e tratamento de erros.

Nenhum teste fala com a internet: as chamadas do httpx são trocadas por respostas
montadas aqui.
"""

import json

import httpx
import pytest

import ia

SCHEMA = {"type": "object"}
PAUTAS = {
    "titulo": "Lançamento",
    "objetivo": "Definir a data",
    "contexto": "",
    "participantes": [{"rotulo": "Falante A", "nome_sugerido": "Ana", "papel": ""}],
    "problemas_levantados": [],
    "decisoes": [{"descricao": "Adiar para sexta", "timestamp": "00:04"}],
    "solucoes_propostas": [],
    "pendencias": [],
    "proximos_passos": [],
}


def resposta(status=200, corpo=None, url="http://teste/v1/chat/completions"):
    return httpx.Response(status, json=corpo or {}, request=httpx.Request("POST", url))


def chat(conteudo, finish_reason="stop"):
    return {"choices": [{"message": {"content": conteudo}, "finish_reason": finish_reason}]}


def configurar(**cfg):
    with open(ia.CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)


# --- Configuração --------------------------------------------------------------------


def test_sem_arquivo_usa_claude_nao_configurado():
    estado = ia.public_state()
    assert estado["provider"] == "claude"
    assert estado["configurado"] is False
    assert {p["id"] for p in estado["providers"]} == set(ia.PROVIDERS)


def test_config_invalida_vira_vazia(ia_config_isolado):
    ia_config_isolado.write_text("[1, 2", encoding="utf-8")
    assert ia.load_config() == {}
    ia_config_isolado.write_text("[1, 2]", encoding="utf-8")
    assert ia.load_config() == {}


def test_update_salva_e_nunca_devolve_a_chave_inteira():
    estado = ia.update({"provider": "openai", "chave": "  sk-segredo-1234  ", "modelo": "gpt-5"})
    openai = next(p for p in estado["providers"] if p["id"] == "openai")
    assert estado["provider"] == "openai" and estado["configurado"] is True
    assert openai["chave_final"] == "1234"
    assert "sk-segredo-1234" not in json.dumps(estado)
    assert ia.load_config()["chaves"]["openai"] == "sk-segredo-1234"


def test_chave_curta_nao_mostra_final():
    estado = ia.update({"provider": "gemini", "chave": "abc", "modelo": "g"})
    gemini = next(p for p in estado["providers"] if p["id"] == "gemini")
    assert gemini["tem_chave"] is True and gemini["chave_final"] == ""


def test_update_modelo_vazio_volta_ao_padrao():
    ia.update({"provider": "openai", "modelo": "gpt-5"})
    ia.update({"provider": "openai", "modelo": "  "})
    assert ia.model_for("openai") == ""


def test_update_rejeita_provedor_e_url_invalidos():
    with pytest.raises(ia.IAError):
        ia.update({"provider": "inexistente"})
    with pytest.raises(ia.IAError, match="http"):
        ia.update({"provider": "ollama", "ollama_url": "file:///etc/passwd"})


def test_ollama_nao_guarda_chave_e_so_precisa_de_modelo():
    estado = ia.update({"provider": "ollama", "chave": "x", "modelo": "qwen2.5:7b",
                        "ollama_url": "http://127.0.0.1:11434/"})
    assert "chaves" not in ia.load_config()
    assert estado["configurado"] is True
    assert estado["ollama_url"] == "http://127.0.0.1:11434"


def test_chave_do_env_vale_como_padrao(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-do-env-9999")
    assert ia.api_key("openai") == "sk-do-env-9999"
    configurar(chaves={"openai": "sk-do-arquivo-0000"})
    assert ia.api_key("openai") == "sk-do-arquivo-0000"


def test_forget_key():
    ia.update({"provider": "openai", "chave": "sk-segredo-1234"})
    ia.forget_key("openai")
    assert ia.api_key("openai") == ""


# --- Leitura da resposta ---------------------------------------------------------------


@pytest.mark.parametrize("texto", [
    '{"titulo": "x"}',
    'Aqui estão as pautas:\n```json\n{"titulo": "x"}\n```',
    'Claro! {"titulo": "x"} Espero ter ajudado.',
])
def test_parse_json_aceita_respostas_com_enfeites(texto):
    assert ia._parse_json(texto) == {"titulo": "x"}


@pytest.mark.parametrize("texto", ["", None, "sem json", "{quebrado: }"])
def test_parse_json_invalido(texto):
    with pytest.raises(ia.IAError, match="formato esperado"):
        ia._parse_json(texto)


def test_normalize_completa_campos_que_faltam():
    dados = ia._normalize({"titulo": "x", "decisoes": ["Adiar", {"descricao": "Contratar"}, 3],
                           "objetivo": None})
    assert dados["objetivo"] == ""
    assert dados["participantes"] == []
    assert dados["decisoes"] == [
        {"descricao": "Adiar", "timestamp": ""},
        {"descricao": "Contratar", "timestamp": ""},
    ]
    assert set(dados) == set(PAUTAS)


# --- Gerar as pautas (OpenAI-compatível) -------------------------------------------------


def test_generate_sem_chave_ou_modelo():
    configurar(provider="openai")
    with pytest.raises(ia.IAError, match="Falta a chave"):
        ia.generate("t", "s", SCHEMA)
    configurar(provider="openai", chaves={"openai": "sk-1234567"})
    with pytest.raises(ia.IAError, match="Escolha o modelo"):
        ia.generate("t", "s", SCHEMA)


def test_generate_recua_quando_o_modelo_nao_aceita_json_schema(monkeypatch):
    configurar(provider="openrouter", chaves={"openrouter": "sk-or-123456"}, modelos={"openrouter": "m"})
    pedidos = []

    def post(url, json, headers, timeout):
        pedidos.append(json)
        if json.get("response_format", {}).get("type") == "json_schema":
            return resposta(400, {"error": "unsupported"})
        return resposta(200, chat("```json\n" + ia.json.dumps(PAUTAS) + "\n```"))

    monkeypatch.setattr(ia.httpx, "post", post)
    assert ia.generate("[00:00] Ana: oi", "sistema", SCHEMA) == PAUTAS
    assert [p.get("response_format", {}).get("type") for p in pedidos] == ["json_schema", "json_object"]
    assert pedidos[0]["model"] == "m"
    assert "<transcricao>" in pedidos[0]["messages"][1]["content"]


def test_generate_ultima_tentativa_vai_sem_response_format(monkeypatch):
    configurar(provider="ollama", modelos={"ollama": "qwen"}, ollama_url="http://127.0.0.1:1")
    urls, formatos = [], []

    def post(url, json, headers, timeout):
        urls.append(url)
        formatos.append(json.get("response_format"))
        if "response_format" in json:
            return resposta(422)
        assert "Authorization" not in headers  # Ollama não usa chave
        return resposta(200, chat(ia.json.dumps(PAUTAS)))

    monkeypatch.setattr(ia.httpx, "post", post)
    assert ia.generate("t", "s", SCHEMA)["titulo"] == "Lançamento"
    assert formatos[-1] is None
    assert urls[0] == "http://127.0.0.1:1/v1/chat/completions"


def test_generate_resposta_cortada(monkeypatch):
    configurar(provider="openai", chaves={"openai": "sk-1234567"}, modelos={"openai": "m"})
    monkeypatch.setattr(ia.httpx, "post", lambda *a, **k: resposta(200, chat("{", "length")))
    with pytest.raises(ia.IAError, match="cortada"):
        ia.generate("t", "s", SCHEMA)


@pytest.mark.parametrize("status, trecho", [
    (401, "foi recusada"),
    (402, "Sem créditos"),
    (404, "modelo escolhido não existe"),
    (429, "Limite de uso"),
    (500, "(500)"),
])
def test_generate_erros_http_viram_mensagens(monkeypatch, status, trecho):
    configurar(provider="openai", chaves={"openai": "sk-1234567"}, modelos={"openai": "m"})
    monkeypatch.setattr(ia.httpx, "post", lambda *a, **k: resposta(status, {"error": "x"}))
    with pytest.raises(ia.IAError, match=trecho.replace("(", r"\(").replace(")", r"\)")):
        ia.generate("t", "s", SCHEMA)


def test_ollama_desligado(monkeypatch):
    configurar(provider="ollama", modelos={"ollama": "qwen"})

    def post(*_a, **_k):
        raise httpx.ConnectError("recusado")

    monkeypatch.setattr(ia.httpx, "post", post)
    with pytest.raises(ia.IAError, match="Ollama"):
        ia.generate("t", "s", SCHEMA)


# --- Testar conexão ----------------------------------------------------------------------


def test_list_models_compativel(monkeypatch):
    def get(url, headers, timeout):
        assert url == "https://generativelanguage.googleapis.com/v1beta/openai/models"
        assert headers["Authorization"] == "Bearer chave-nova"
        return resposta(200, {"data": [{"id": "models/gemini-2.5-pro"}, {"id": "models/gemini-2.5-flash"},
                                       {"id": ""}]}, url)

    monkeypatch.setattr(ia.httpx, "get", get)
    assert ia.list_models("gemini", chave="chave-nova") == ["gemini-2.5-flash", "gemini-2.5-pro"]


def test_list_models_exige_chave_e_ollama_vazio(monkeypatch):
    with pytest.raises(ia.IAError, match="Cole a chave"):
        ia.list_models("openai")
    monkeypatch.setattr(ia.httpx, "get", lambda *a, **k: resposta(200, {"data": []}))
    with pytest.raises(ia.IAError, match="ollama pull"):
        ia.list_models("ollama", url="http://127.0.0.1:1")


def test_openrouter_exchange_salva_a_chave(monkeypatch):
    monkeypatch.setattr(ia.httpx, "post", lambda *a, **k: resposta(200, {"key": "sk-or-v1-abcd1234"}))
    estado = ia.openrouter_exchange("codigo", "verificador")
    assert estado["provider"] == "openrouter"
    assert ia.api_key("openrouter") == "sk-or-v1-abcd1234"


def test_openrouter_exchange_sem_chave(monkeypatch):
    monkeypatch.setattr(ia.httpx, "post", lambda *a, **k: resposta(200, {}))
    with pytest.raises(ia.IAError, match="não devolveu"):
        ia.openrouter_exchange("codigo", "verificador")
