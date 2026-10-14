"""Histórico em SQLite e pautas automáticas ao fim da transcrição."""

import threading
import time

import pytest

import historico
import ia
import trabalhos
from dados import SEGMENTOS

ORIGEM = {"Origin": "http://localhost"}
PAUTAS = ia._normalize({"titulo": "Lançamento", "decisoes": [{"descricao": "Adiar", "timestamp": "00:04"}]})


def job_concluido(**extra):
    job = {"filename": "reuniao.mp3", "duration": 62.0, "engine": "assemblyai", "created": 1000.0,
           "segments": [dict(s) for s in SEGMENTOS], "speakers": {"A": "Falante A", "B": "Falante B"},
           "pautas": None}
    job.update(extra)
    return job


def esperar(client, job_id, condicao, limite=30):
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        dados = client.get(f"/status/{job_id}").get_json()
        if condicao(dados):
            return dados
        time.sleep(0.05)
    raise AssertionError(f"condição não atingida: {dados}")


def enviar(client, audio_mp3):
    with open(audio_mp3, "rb") as fh:
        return client.post("/upload", data={"video": (fh, "reuniao.mp3")}).get_json()["job_id"]


def reiniciar_app():
    """Simula reiniciar o servidor: some tudo que estava só na memória."""
    with trabalhos.jobs_lock:
        trabalhos.jobs.clear()


@pytest.fixture
def ia_pronta():
    ia.update({"provider": "ollama", "modelo": "qwen2.5:7b", "ollama_url": "http://127.0.0.1:1"})


@pytest.fixture
def ia_falsa(monkeypatch):
    chamadas = []

    def gerar(transcricao, system, schema):
        chamadas.append(transcricao)
        return PAUTAS

    monkeypatch.setattr(ia, "generate", gerar)
    return chamadas


# --- historico.py ------------------------------------------------------------------------


def test_salvar_carregar_e_listar(historico_isolado):
    historico.salvar("velho", job_concluido(created=1000.0))
    historico.salvar("novo", job_concluido(created=2000.0, filename="outra.webm", pautas=PAUTAS))

    assert historico_isolado.exists()
    job = historico.carregar("novo")
    assert job["status"] == "done" and job["pautas"] == PAUTAS
    assert job["segments"][0]["text"] == SEGMENTOS[0]["text"]
    assert historico.carregar("nao-existe") is None

    lista = historico.listar()
    assert [t["id"] for t in lista] == ["novo", "velho"]
    assert lista[0] == {"id": "novo", "criado_em": 2000.0, "filename": "outra.webm",
                        "duration": 62.0, "tem_pautas": True, "titulo": "Lançamento",
                        "idioma": "pt"}
    assert lista[1]["tem_pautas"] is False and lista[1]["titulo"] == ""


def test_salvar_de_novo_atualiza_falantes_e_pautas_sem_duplicar():
    historico.salvar("j", job_concluido())
    historico.salvar("j", job_concluido(speakers={"A": "Ana", "B": "Falante B"}, pautas=PAUTAS,
                                        created=9999.0))
    job = historico.carregar("j")
    assert job["speakers"]["A"] == "Ana" and job["pautas"] == PAUTAS
    assert job["created"] == 1000.0  # a data original fica
    assert len(historico.listar()) == 1


def test_apagar():
    historico.salvar("j", job_concluido())
    assert historico.apagar("j") is True
    assert historico.apagar("j") is False
    assert historico.listar() == []


def test_gravacoes_em_paralelo_nao_se_atrapalham():
    erros = []

    def gravar(i):
        try:
            historico.salvar(f"j{i}", job_concluido(created=float(i)))
        except Exception as exc:  # noqa: BLE001
            erros.append(exc)

    threads = [threading.Thread(target=gravar, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not erros
    assert len(historico.listar()) == 20


# --- Sobrevive a reiniciar ---------------------------------------------------------------


def test_transcricao_sobrevive_a_reiniciar(client, audio_mp3, transcricao_falsa):
    job_id = enviar(client, audio_mp3)
    esperar(client, job_id, lambda d: d["status"] == "done")
    client.post(f"/speakers/{job_id}", json={"A": "Ana"})

    reiniciar_app()
    dados = client.get(f"/status/{job_id}").get_json()
    assert dados["status"] == "done"
    assert dados["filename"] == "reuniao.mp3"
    assert dados["speakers"] == {"A": "Ana", "B": "Falante B"}
    assert dados["text"].startswith("[00:00] Ana: Bom dia")
    assert dados["pautas"] is None

    reiniciar_app()
    assert client.get(f"/download/{job_id}").get_data(as_text=True).startswith("[00:00] Ana:")


def test_pautas_ficam_no_historico(client, job_pronto, ia_falsa):
    job_id = job_pronto()
    historico.salvar(job_id, trabalhos.jobs[job_id])
    assert client.post(f"/pautas/{job_id}").status_code == 200

    reiniciar_app()
    assert client.get(f"/status/{job_id}").get_json()["pautas"] == PAUTAS
    assert "- Adiar [00:04]" in client.get(f"/download/{job_id}?tipo=pautas").get_data(as_text=True)
    assert historico.listar()[0]["titulo"] == "Lançamento"


def test_falha_ao_gravar_o_historico_nao_derruba_a_transcricao(client, audio_mp3, transcricao_falsa,
                                                              monkeypatch):
    def quebra(*_a):
        raise OSError("disco cheio")

    monkeypatch.setattr(historico, "salvar", quebra)
    job_id = enviar(client, audio_mp3)
    dados = esperar(client, job_id, lambda d: d["status"] in ("done", "error"))
    assert dados["status"] == "done"


# --- Pautas automáticas ------------------------------------------------------------------


def test_pautas_saem_sozinhas_ao_fim_da_transcricao(client, audio_mp3, transcricao_falsa,
                                                    ia_pronta, ia_falsa):
    job_id = enviar(client, audio_mp3)
    dados = esperar(client, job_id, lambda d: d.get("pautas"))
    assert dados["pautas"] == PAUTAS
    assert dados["pautas_status"] is None
    assert ia_falsa[0].startswith("[00:00] Falante A: Bom dia")
    assert historico.carregar(job_id)["pautas"] == PAUTAS


def test_sem_ia_configurada_nao_gera_pautas(client, audio_mp3, transcricao_falsa, ia_falsa):
    job_id = enviar(client, audio_mp3)
    dados = esperar(client, job_id, lambda d: d["status"] == "done")
    time.sleep(0.2)
    assert dados["pautas_status"] is None and not ia_falsa


def test_pautas_automaticas_desligadas(client, audio_mp3, transcricao_falsa, ia_pronta, ia_falsa):
    ia.update({"provider": "ollama", "pautas_auto": False})
    assert ia.public_state()["pautas_auto"] is False
    job_id = enviar(client, audio_mp3)
    esperar(client, job_id, lambda d: d["status"] == "done")
    time.sleep(0.2)
    assert client.get(f"/status/{job_id}").get_json()["pautas"] is None
    assert not ia_falsa


def test_erro_nas_pautas_automaticas_aparece_no_status(client, audio_mp3, transcricao_falsa,
                                                       ia_pronta, monkeypatch):
    def gerar(*_a):
        raise ia.IAError("Não foi possível falar com o Ollama.")

    monkeypatch.setattr(ia, "generate", gerar)
    job_id = enviar(client, audio_mp3)
    dados = esperar(client, job_id, lambda d: d.get("pautas_status") == "erro")
    assert dados["pautas_error"] == "Não foi possível falar com o Ollama."
    assert dados["status"] == "done"  # a transcrição continua disponível


def test_nao_gera_pautas_duas_vezes_ao_mesmo_tempo(client, job_pronto, monkeypatch):
    liberar = threading.Event()

    def gerar(*_a):
        liberar.wait(5)
        return PAUTAS

    monkeypatch.setattr(ia, "generate", gerar)
    job_id = job_pronto()
    primeira = {}
    t = threading.Thread(target=lambda: primeira.update(r=client.post(f"/pautas/{job_id}")))
    t.start()
    esperar(client, job_id, lambda d: d.get("pautas_status") == "gerando")

    resp = client.post(f"/pautas/{job_id}")
    assert resp.status_code == 409
    assert client.delete(f"/historico/{job_id}", headers=ORIGEM).status_code == 409
    liberar.set()
    t.join()
    assert primeira["r"].status_code == 200


# --- Rotas do histórico ------------------------------------------------------------------


def test_listar_e_apagar_pela_rota(client):
    historico.salvar("a", job_concluido(created=1.0))
    historico.salvar("b", job_concluido(created=2.0, pautas=PAUTAS))
    lista = client.get("/historico").get_json()["transcricoes"]
    assert [t["id"] for t in lista] == ["b", "a"]

    client.get("/status/b")  # carrega na memória, como ao abrir pela página
    assert client.delete("/historico/b", headers=ORIGEM).get_json() == {"ok": True}
    assert "b" not in trabalhos.jobs
    assert client.get("/status/b").status_code == 404
    assert client.delete("/historico/b").status_code == 404
    assert [t["id"] for t in client.get("/historico").get_json()["transcricoes"]] == ["a"]


def test_outro_site_nao_apaga_o_historico(client):
    historico.salvar("a", job_concluido())
    assert client.delete("/historico/a", headers={"Origin": "https://evil.example"}).status_code == 403
    assert historico.carregar("a") is not None


def test_nao_apaga_transcricao_em_andamento(client):
    with trabalhos.jobs_lock:
        trabalhos.jobs["x"] = {"status": "transcribing"}
    assert client.delete("/historico/x").status_code == 409
