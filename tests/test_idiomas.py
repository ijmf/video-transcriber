"""Idioma da reunião: escolha no envio, transcrição, detecção automática e pautas."""

import sqlite3
import time
import types

import pytest

import app as app_module
import historico
import ia
from dados import SEGMENTOS

PAUTAS = ia._normalize({"titulo": "Launch"})


def enviar(client, audio_mp3, idioma=None):
    data = {}
    if idioma is not None:
        data["idioma"] = idioma
    with open(audio_mp3, "rb") as fh:
        data["video"] = (fh, "reuniao.mp3")
        return client.post("/upload", data=data)


def esperar_done(client, job_id, limite=30):
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        dados = client.get(f"/status/{job_id}").get_json()
        if dados["status"] in ("done", "error"):
            return dados
        time.sleep(0.05)
    raise AssertionError("o job não terminou a tempo")


@pytest.fixture
def ia_captura(monkeypatch):
    sistemas = []

    def gerar(transcricao, system, schema):
        sistemas.append(system)
        return PAUTAS

    monkeypatch.setattr(ia, "generate", gerar)
    return sistemas


# --- Envio ------------------------------------------------------------------------------


def test_sem_idioma_usa_portugues(client, audio_mp3, transcricao_falsa):
    resp = enviar(client, audio_mp3)
    job_id = resp.get_json()["job_id"]
    assert app_module.jobs[job_id]["language"] == "pt"
    assert esperar_done(client, job_id)["language"] == "pt"


def test_idioma_escolhido_vai_para_o_job(client, audio_mp3, transcricao_falsa):
    job_id = enviar(client, audio_mp3, "en").get_json()["job_id"]
    assert esperar_done(client, job_id)["language"] == "en"


def test_idioma_desconhecido_e_recusado(client, audio_mp3):
    resp = enviar(client, audio_mp3, "klingon")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Idioma não suportado: klingon."
    assert not app_module.jobs


def test_pagina_lista_os_idiomas(client):
    html = client.get("/").get_data(as_text=True)
    assert '<option value="pt" selected>Português</option>' in html
    assert '<option value="auto">Detectar automaticamente</option>' in html
    assert '"auto": "Detectar automaticamente"' in html  # mapa usado pelo script da página


# --- Transcrição -------------------------------------------------------------------------


@pytest.fixture
def aai_falsa(monkeypatch):
    """Substitui o SDK da AssemblyAI e guarda a configuração recebida."""
    capturado = {}

    class Config:
        def __init__(self, **kwargs):
            capturado.update(kwargs)

    transcript = types.SimpleNamespace(
        status="completed", id="t1", error=None,
        utterances=[types.SimpleNamespace(start=0, speaker="A", text=" Hello team. ")],
        json_response={"language_code": "en_us"},
    )
    falsa = types.SimpleNamespace(
        settings=types.SimpleNamespace(api_key=None),
        TranscriptionConfig=Config,
        Transcriber=lambda: types.SimpleNamespace(submit=lambda path, config: transcript),
        Transcript=types.SimpleNamespace(get_by_id=lambda _id: transcript),
        TranscriptStatus=types.SimpleNamespace(queued="queued", processing="processing", error="error"),
    )
    monkeypatch.setattr(app_module, "aai", falsa)
    return capturado


def job_em_memoria(language):
    with app_module.jobs_lock:
        app_module.jobs["j"] = {"status": "extracting", "language": language, "detected_language": ""}
    return "j"


def test_assemblyai_recebe_o_idioma(aai_falsa):
    app_module.transcribe_assemblyai(job_em_memoria("es"), "audio.mp3")
    assert aai_falsa == {"speaker_labels": True, "language_code": "es"}
    assert app_module.jobs["j"]["detected_language"] == ""


def test_assemblyai_detecta_o_idioma(aai_falsa):
    segmentos = app_module.transcribe_assemblyai(job_em_memoria("auto"), "audio.mp3")
    assert aai_falsa == {"speaker_labels": True, "language_detection": True}
    assert app_module.jobs["j"]["detected_language"] == "en"  # "en_us" vira "en"
    assert segmentos[0]["text"] == "Hello team."


@pytest.mark.parametrize("escolhido, enviado, detectado", [("fr", "fr", ""), ("auto", None, "fr")])
def test_whisper_recebe_ou_detecta_o_idioma(monkeypatch, escolhido, enviado, detectado):
    recebido = {}

    def transcribe(model, path, language, **_kw):
        recebido["language"] = language
        return {"language": "fr", "segments": [{"start": 0, "text": " Bonjour "}]}

    modelo = types.SimpleNamespace(device=types.SimpleNamespace(type="cpu"))
    monkeypatch.setattr(app_module, "load_whisper",
                        lambda: (types.SimpleNamespace(transcribe=transcribe), modelo))
    monkeypatch.setattr(app_module, "_whisper_model", modelo)
    segmentos = app_module.transcribe_whisper(job_em_memoria(escolhido), "audio.wav")
    assert recebido["language"] == enviado
    assert app_module.jobs["j"]["detected_language"] == detectado
    assert segmentos[0]["text"] == "Bonjour"


# --- Pautas no idioma da reunião ---------------------------------------------------------


@pytest.mark.parametrize("idioma, trecho", [
    ("pt", "Escreva todo o conteúdo em português do Brasil."),
    ("en", "escreva todo o conteúdo em inglês."),
    ("de", "escreva todo o conteúdo em alemão."),
    ("", "no mesmo idioma em que a transcrição está."),
    ("ja", "no mesmo idioma em que a transcrição está."),
])
def test_pedido_das_pautas_no_idioma(idioma, trecho):
    sistema = app_module.pautas_system(idioma)
    assert sistema.startswith("Você organiza transcrições de reuniões e conversas em pautas.")
    assert sistema.rstrip().endswith(trecho)


def test_pautas_seguem_o_idioma_detectado(client, audio_mp3, monkeypatch, ia_captura):
    def falsa(job_id, audio_path):
        app_module.update_job(job_id, detected_language="es")
        return [dict(s) for s in SEGMENTOS]

    monkeypatch.setattr(app_module, "TRANSCRIBER", "assemblyai")
    monkeypatch.setattr(app_module, "transcribe_assemblyai", falsa)
    job_id = enviar(client, audio_mp3, "auto").get_json()["job_id"]
    assert esperar_done(client, job_id)["language"] == "es"
    assert client.post(f"/pautas/{job_id}").status_code == 200
    assert ia_captura[-1].rstrip().endswith("escreva todo o conteúdo em espanhol.")


def test_idioma_sobrevive_a_reiniciar(client, job_pronto, ia_captura):
    job_id = job_pronto()
    app_module.jobs[job_id]["language"] = "it"
    historico.salvar(job_id, app_module.jobs[job_id])
    with app_module.jobs_lock:
        app_module.jobs.clear()

    assert client.get(f"/status/{job_id}").get_json()["language"] == "it"
    client.post(f"/pautas/{job_id}")
    assert ia_captura[-1].rstrip().endswith("escreva todo o conteúdo em italiano.")
    assert historico.listar()[0]["idioma"] == "it"


# --- Banco criado antes da coluna idioma -------------------------------------------------


def test_banco_antigo_ganha_a_coluna_idioma(historico_isolado):
    with sqlite3.connect(historico_isolado) as conn:
        conn.execute("""CREATE TABLE transcricoes (id TEXT PRIMARY KEY, criado_em REAL NOT NULL,
            filename TEXT NOT NULL, duration REAL NOT NULL, engine TEXT NOT NULL,
            segments TEXT NOT NULL, speakers TEXT NOT NULL, pautas TEXT)""")
        conn.execute("INSERT INTO transcricoes VALUES ('velha', 1, 'a.mp3', 10, 'whisper', '[]', '{}', NULL)")
    conn.close()

    assert historico.carregar("velha")["language"] == "pt"
    assert historico.listar()[0]["idioma"] == "pt"
    historico.salvar("nova", {"filename": "b.mp3", "duration": 5, "segments": [], "speakers": {},
                              "pautas": None, "language": "en"})
    assert historico.carregar("nova")["language"] == "en"
