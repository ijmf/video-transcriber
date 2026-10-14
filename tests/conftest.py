"""Configuração comum dos testes.

As variáveis abaixo são definidas antes de importar o app: o load_dotenv() não
sobrescreve o que já existe no ambiente, então as chaves do .env de quem roda os
testes nunca entram em jogo. O ia_config.json e o historico.db reais também
ficam intocados: cada teste usa arquivos próprios numa pasta temporária.
"""

import os
import sys

import pytest

for _var in ("ASSEMBLYAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
             "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
    os.environ[_var] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import historico  # noqa: E402
import ia  # noqa: E402
import midia  # noqa: E402
import trabalhos  # noqa: E402
import transcricao  # noqa: E402
from dados import SEGMENTOS, gerar_audio  # noqa: E402


@pytest.fixture(autouse=True)
def ia_config_isolado(tmp_path, monkeypatch):
    monkeypatch.setattr(ia, "CONFIG_PATH", str(tmp_path / "ia_config.json"))
    return tmp_path / "ia_config.json"


@pytest.fixture(autouse=True)
def historico_isolado(tmp_path, monkeypatch):
    monkeypatch.setattr(historico, "DB_PATH", str(tmp_path / "historico.db"))
    return tmp_path / "historico.db"


@pytest.fixture(autouse=True)
def jobs_limpos():
    with trabalhos.jobs_lock:
        trabalhos.jobs.clear()
    yield
    with trabalhos.jobs_lock:
        trabalhos.jobs.clear()


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def job_pronto():
    """Cria um job já transcrito, como se o worker tivesse terminado."""

    def criar(segments=SEGMENTOS, pautas=None):
        job_id = "job-teste"
        speakers = {s["speaker"]: f"Falante {s['speaker']}" for s in segments if s["speaker"]}
        with trabalhos.jobs_lock:
            trabalhos.jobs[job_id] = {
                "status": "done", "message": "Transcrição concluída.", "progress": 100,
                "filename": "reuniao.mp4", "duration": 8.0, "workdir": "", "video_path": "",
                "segments": [dict(s) for s in segments], "speakers": speakers, "pautas": pautas,
            }
        return job_id

    return criar


@pytest.fixture
def transcricao_falsa(monkeypatch):
    """Troca a AssemblyAI por uma resposta fixa; o ffmpeg continua de verdade."""
    chamadas = []

    def falsa(audio_path, language, avisar):
        chamadas.append(audio_path)
        avisar(status="transcribing", message="Transcrevendo…")
        return [dict(s) for s in SEGMENTOS], ""

    monkeypatch.setattr(transcricao, "TRANSCRIBER", "assemblyai")
    monkeypatch.setattr(transcricao, "transcribe_assemblyai", falsa)
    return chamadas


@pytest.fixture
def audio_mp3(tmp_path):
    if not midia.ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe não encontrado")
    return gerar_audio(tmp_path / "reuniao.mp3")
