"""Testes das rotas e utilitários de app.py.

A transcrição (AssemblyAI) e a IA das pautas são simuladas; o ffmpeg é usado de
verdade nos testes de envio (são pulados se ele não estiver instalado).
"""

import io
import json
import os
import re
import time

import pytest

import app as app_module
import ia
import midia
import pautas
import trabalhos
import transcricao
from dados import SEGMENTOS, gerar_audio

ORIGEM = {"Origin": "http://localhost"}


def esperar_job(client, job_id, limite=30):
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        dados = client.get(f"/status/{job_id}").get_json()
        if dados["status"] in ("done", "error"):
            return dados
        time.sleep(0.1)
    raise AssertionError("o job não terminou a tempo")


# --- Utilitários ---------------------------------------------------------------------


@pytest.mark.parametrize("segundos, esperado", [
    (0, "00:00"), (59.9, "00:59"), (61, "01:01"), (3600, "01:00:00"), (3725, "01:02:05"),
])
def test_format_timestamp(segundos, esperado):
    assert midia.format_timestamp(segundos) == esperado


def test_render_transcript_com_e_sem_falante():
    job = {"speakers": {"A": "Ana"}, "segments": [
        {"timestamp": "00:00", "speaker": "A", "text": "Oi"},
        {"timestamp": "00:02", "speaker": None, "text": "Sem falante"},
    ]}
    assert trabalhos.render_transcript(job) == "[00:00] Ana: Oi\n[00:02] Sem falante"


def test_whisper_nao_instalado_da_mensagem_clara(monkeypatch):
    def sem_modulo(nome):
        raise ModuleNotFoundError(nome)

    monkeypatch.setattr(transcricao, "_whisper", None)
    monkeypatch.setattr(transcricao.importlib, "import_module", sem_modulo)
    with pytest.raises(RuntimeError, match="requirements-whisper.txt"):
        transcricao.load_whisper()


# --- Proteção contra DNS rebinding e outros sites --------------------------------------


@pytest.mark.parametrize("host, status", [
    ("localhost", 200), ("127.0.0.1:5000", 200), ("[::1]:5000", 200), ("LOCALHOST:5000", 200),
    ("evil.example", 403), ("evil.example:5000", 403), ("127.0.0.1.nip.io", 403),
])
def test_so_atende_host_local(client, host, status):
    assert client.get("/ia", headers={"Host": host}).status_code == status


def test_config_da_ia_recusa_outro_site_e_corpo_que_nao_e_json(client):
    corpo = {"provider": "openai", "chave": "sk-ataque"}
    assert client.post("/ia", json=corpo, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/ia", data="provider=openai").status_code == 415
    assert ia.api_key("openai") == ""


# --- Página e envio ------------------------------------------------------------------


def test_pagina_inicial(client):
    resp = client.get("/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Gravar reunião" in html and "Organizar por pautas" in html


def test_upload_sem_arquivo(client):
    resp = client.post("/upload", data={})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Nenhum arquivo enviado."


def test_upload_formato_nao_suportado(client, tmp_path):
    (tmp_path / "notas.txt").write_text("oi")
    with open(tmp_path / "notas.txt", "rb") as fh:
        resp = client.post("/upload", data={"video": (fh, "notas.txt")})
    assert resp.status_code == 400
    assert "Formato não suportado: .txt" in resp.get_json()["error"]


def test_upload_arquivo_corrompido(client, tmp_path, audio_mp3):
    falso = tmp_path / "video.mp4"
    falso.write_bytes(b"isto nao e um video")
    with open(falso, "rb") as fh:
        resp = client.post("/upload", data={"video": (fh, "video.mp4")})
    assert resp.status_code == 400
    assert not trabalhos.jobs


def test_upload_acima_do_limite_de_duracao(client, monkeypatch, audio_mp3):
    monkeypatch.setattr(app_module, "MAX_DURATION_SECONDS", 1)
    with open(audio_mp3, "rb") as fh:
        resp = client.post("/upload", data={"video": (fh, "reuniao.mp3")})
    assert resp.status_code == 400
    assert "o limite é 0 minutos" in resp.get_json()["error"]


def test_upload_transcreve_e_apaga_a_pasta_temporaria(client, audio_mp3, transcricao_falsa):
    with open(audio_mp3, "rb") as fh:
        resp = client.post("/upload", data={"video": (fh, "reunião de pauta.mp3")})
    assert resp.status_code == 200
    corpo = resp.get_json()
    assert 1.5 < corpo["duration"] < 2.5

    dados = esperar_job(client, corpo["job_id"])
    assert dados["status"] == "done", dados["message"]
    assert dados["filename"] == "reuniao_de_pauta.mp3"
    assert dados["speakers"] == {"A": "Falante A", "B": "Falante B"}
    assert dados["text"].splitlines()[0] == "[00:00] Falante A: " + SEGMENTOS[0]["text"]
    assert transcricao_falsa[0].endswith("audio.mp3")
    assert not os.path.exists(trabalhos.jobs[corpo["job_id"]]["workdir"])


def test_falha_na_transcricao_vira_erro_do_job(client, audio_mp3, monkeypatch):
    def quebra(audio_path, language, avisar):
        raise RuntimeError("A AssemblyAI não conseguiu transcrever: áudio mudo")

    monkeypatch.setattr(transcricao, "TRANSCRIBER", "assemblyai")
    monkeypatch.setattr(transcricao, "transcribe_assemblyai", quebra)
    with open(audio_mp3, "rb") as fh:
        job_id = client.post("/upload", data={"video": (fh, "reuniao.mp3")}).get_json()["job_id"]
    dados = esperar_job(client, job_id)
    assert dados["status"] == "error"
    assert "áudio mudo" in dados["message"]
    assert not os.path.exists(trabalhos.jobs[job_id]["workdir"])


def test_duracao_lendo_o_audio_inteiro(tmp_path, audio_mp3):
    # Caminho usado nas gravações do MediaRecorder, que não trazem a duração no cabeçalho.
    webm = gerar_audio(tmp_path / "gravacao.webm", segundos=3)
    assert 2.5 < midia.decoded_duration(str(webm)) < 3.5


# --- Status, falantes, pautas e downloads --------------------------------------------


def test_status_de_job_inexistente(client):
    assert client.get("/status/nao-existe").status_code == 404


def test_renomear_falantes(client, job_pronto):
    job_id = job_pronto()
    resp = client.post(f"/speakers/{job_id}", json={"A": "  Ana  ", "B": "", "Z": "Intruso"})
    dados = resp.get_json()
    assert dados["speakers"] == {"A": "Ana", "B": "Falante B"}
    assert dados["text"].startswith("[00:00] Ana: Bom dia")
    longo = client.post(f"/speakers/{job_id}", json={"A": "x" * 100}).get_json()
    assert len(longo["speakers"]["A"]) == 60


def test_renomear_falantes_de_job_inexistente(client):
    assert client.post("/speakers/nao-existe", json={"A": "Ana"}).status_code == 404


def test_pautas_usam_os_nomes_salvos(client, job_pronto, monkeypatch):
    job_id = job_pronto()
    client.post(f"/speakers/{job_id}", json={"A": "Ana"})
    recebido = {}

    def gerar(transcricao, system, schema):
        recebido["texto"] = transcricao
        return ia._normalize({"titulo": "Lançamento", "decisoes": [{"descricao": "Adiar", "timestamp": "00:04"}]})

    monkeypatch.setattr(ia, "generate", gerar)
    resp = client.post(f"/pautas/{job_id}")
    assert resp.status_code == 200
    assert resp.get_json()["pautas"]["titulo"] == "Lançamento"
    assert "Ana: Bom dia" in recebido["texto"]

    md = client.get(f"/download/{job_id}?tipo=pautas")
    assert md.headers["Content-Disposition"] == 'attachment; filename="reuniao-pautas.md"'
    assert md.mimetype == "text/markdown"
    assert "## Decisões\n\n- Adiar [00:04]" in md.get_data(as_text=True)


def test_pautas_com_erro_da_ia(client, job_pronto, monkeypatch):
    job_id = job_pronto()

    def gerar(*_a):
        raise ia.IAError("A chave do OpenRouter foi recusada.")

    monkeypatch.setattr(ia, "generate", gerar)
    resp = client.post(f"/pautas/{job_id}")
    assert resp.status_code == 502
    assert resp.get_json()["error"] == "A chave do OpenRouter foi recusada."


def test_pautas_de_transcricao_vazia(client, job_pronto):
    job_id = job_pronto(segments=[])
    assert client.post(f"/pautas/{job_id}").status_code == 400


def test_download_da_transcricao(client, job_pronto):
    job_id = job_pronto()
    resp = client.get(f"/download/{job_id}")
    assert resp.headers["Content-Disposition"] == 'attachment; filename="reuniao.txt"'
    assert resp.get_data(as_text=True).startswith("[00:00] Falante A: Bom dia")


def test_download_de_pautas_ainda_nao_geradas(client, job_pronto):
    job_id = job_pronto()
    assert client.get(f"/download/{job_id}?tipo=pautas").status_code == 404


def test_pautas_markdown_completo():
    p = ia._normalize({
        "titulo": "Reunião", "objetivo": "Decidir", "contexto": "Atraso",
        "participantes": [{"rotulo": "Ana", "papel": "gerente"}, {"rotulo": "Bruno"}],
        "problemas_levantados": [{"descricao": "Atraso", "timestamp": "01:00"}],
        "pendencias": [{"descricao": "Enviar relatório", "responsavel": "Bruno", "prazo": "sexta"},
                       {"descricao": "Revisar"}],
        "proximos_passos": [{"descricao": "Nova reunião"}],
    })
    md = pautas.pautas_markdown(p)
    assert md.startswith("# Reunião\n\n## Objetivo\n\nDecidir\n\n## Contexto\n\nAtraso")
    assert "- Ana — gerente\n- Bruno\n" in md
    assert "- Atraso [01:00]" in md
    assert "- Enviar relatório (responsável: Bruno; prazo: sexta)\n- Revisar\n" in md
    assert "## Decisões" not in md
    assert md.rstrip().endswith("- Nova reunião")


# --- Rotas da IA das pautas -------------------------------------------------------------


def test_salvar_ia_pela_rota(client):
    resp = client.post("/ia", json={"provider": "openai", "chave": "sk-segredo-1234", "modelo": "gpt-5"},
                       headers=ORIGEM)
    assert resp.status_code == 200
    estado = client.get("/ia").get_json()
    assert estado["provider"] == "openai" and estado["configurado"] is True
    assert "sk-segredo" not in client.get("/ia").get_data(as_text=True)

    client.post("/ia/esquecer", json={"provider": "openai"}, headers=ORIGEM)
    assert client.get("/ia").get_json()["configurado"] is False


def test_rotas_da_ia_validam_a_entrada(client):
    assert client.post("/ia", json={"provider": "x"}).status_code == 400
    assert client.post("/ia/esquecer", json={"provider": "x"}).status_code == 400
    assert client.post("/ia/openrouter", json={"code": "c"}).status_code == 400
    assert client.post("/ia/testar", json={"provider": "openai"}).get_json()["error"] == \
        "Cole a chave de API para testar."


def test_testar_conexao_pela_rota(client, monkeypatch):
    monkeypatch.setattr(ia, "list_models", lambda p, c, u: ["modelo-a", "modelo-b"])
    resp = client.post("/ia/testar", json={"provider": "openai", "chave": "sk-1"})
    assert resp.get_json() == {"modelos": ["modelo-a", "modelo-b"]}


# --- Limite de tamanho do upload --------------------------------------------------------


def test_limite_de_upload_e_2_gb(client):
    assert app_module.app.config["MAX_CONTENT_LENGTH"] == 2 * 1024 ** 3
    assert app_module.upload_limit_label() == "2 GB"
    html = client.get("/").get_data(as_text=True)
    assert "com até 45 minutos e 2&nbsp;GB." in html
    config = json.loads(re.search(r'<script id="config" type="application/json">(.*?)</script>',
                                  html).group(1))
    assert config["maxBytes"] == 2147483648 and config["maxLabel"] == "2 GB"
    assert config["languages"]["auto"] == "Detectar automaticamente"


def test_estilo_e_scripts_da_pagina(client):
    html = client.get("/").get_data(as_text=True)
    caminhos = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
    assert caminhos == ["/static/estilo.css", "/static/js/comum.js", "/static/js/envio.js",
                        "/static/js/ia.js", "/static/js/historico.js", "/static/js/gravacao.js"]
    for caminho in caminhos:
        resp = client.get(caminho)
        assert resp.status_code == 200, caminho
        resp.close()
    assert client.get("/static/estilo.css", headers={"Host": "evil.example"}).status_code == 403


def test_upload_acima_do_limite_de_tamanho(client, monkeypatch):
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 1000)
    resp = client.post("/upload", data={"video": (io.BytesIO(b"x" * 5000), "grande.mp4")})
    assert resp.status_code == 413
    assert resp.get_json()["error"] == "Arquivo grande demais: o limite é 1000 bytes."
    assert not trabalhos.jobs
