"""Video Transcriber — transcrição de vídeos com identificação de falantes e
organização por pautas.

Fluxo: upload do vídeo -> ffprobe valida a duração -> ffmpeg extrai o áudio ->
transcrição (AssemblyAI com identificação de falantes, ou Whisper local se não
houver chave da AssemblyAI) -> a página consulta o progresso em
/status/<job_id> -> o usuário renomeia os falantes -> o Claude organiza a
transcrição em pautas (objetivo, contexto, problemas, decisões...).
"""

import glob
import importlib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import types
import uuid

import anthropic
import assemblyai as aai
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, render_template, request
from werkzeug.utils import secure_filename

load_dotenv()

import ia  # noqa: E402 — lê o .env ao ser importado

# .webm é o formato das gravações feitas pelo navegador na landing.
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".mp3", ".webm"}
MAX_DURATION_SECONDS = 45 * 60
LANGUAGE = "pt"
WHISPER_MODEL = "small"
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()

# Com chave da AssemblyAI a transcrição identifica os falantes; sem ela, cai
# no Whisper local (sem falantes), como na primeira versão do projeto.
TRANSCRIBER = "assemblyai" if ASSEMBLYAI_API_KEY else "whisper"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB

jobs = {}
jobs_lock = threading.Lock()
job_queue = queue.Queue()

# O app só atende pelo próprio computador. Recusar qualquer outro Host barra o
# DNS rebinding: um site que aponte o domínio dele para 127.0.0.1 não passa daqui.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


@app.before_request
def only_local_host():
    hostname = request.host.rsplit(":", 1)[0] if not request.host.endswith("]") else request.host
    if hostname.lower() not in ALLOWED_HOSTS:
        abort(403)


# --- Utilitários ---------------------------------------------------------------

def format_timestamp(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def ensure_ffmpeg_on_path():
    """Se o ffmpeg não estiver no PATH, procura em FFMPEG_DIR ou na instalação
    do winget (Gyan.FFmpeg) e adiciona a pasta ao PATH deste processo — o
    próprio Whisper chama "ffmpeg" pelo nome."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    candidates = []
    if os.environ.get("FFMPEG_DIR"):
        candidates.append(os.environ["FFMPEG_DIR"])
    winget_packages = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages"
    )
    candidates += sorted(
        glob.glob(os.path.join(winget_packages, "*FFmpeg*", "*", "bin")), reverse=True
    )
    for folder in candidates:
        if os.path.isfile(os.path.join(folder, "ffmpeg.exe")) or os.path.isfile(
            os.path.join(folder, "ffmpeg")
        ):
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
            return


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


ensure_ffmpeg_on_path()


def get_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", path,
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise ValueError("Não foi possível ler o arquivo. Ele pode estar corrompido.")
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        # Gravações do navegador (MediaRecorder) não trazem a duração no cabeçalho:
        # lê o áudio inteiro e usa o último tempo que o ffmpeg informa.
        return decoded_duration(path)


def decoded_duration(path):
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", path, "-vn", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if result.returncode != 0 or not times:
        raise ValueError("Não foi possível determinar a duração do arquivo.")
    h, m, sec = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(sec)


def extract_audio(video_path, audio_path):
    # WAV para o Whisper local; MP3 mono 64 kbps para a AssemblyAI (upload ~4x menor).
    if audio_path.endswith(".mp3"):
        codec = ["-c:a", "libmp3lame", "-b:a", "64k"]
    else:
        codec = ["-c:a", "pcm_s16le"]
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
         *codec, audio_path],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise RuntimeError(f"Falha ao extrair o áudio com ffmpeg:\n{tail}")


def update_job(job_id, **fields):
    with jobs_lock:
        jobs[job_id].update(fields)


def speaker_name(job, code):
    return job["speakers"].get(code) or f"Falante {code}"


def render_transcript(job):
    lines = []
    for seg in job["segments"]:
        prefix = f"[{seg['timestamp']}] "
        if seg["speaker"]:
            prefix += f"{speaker_name(job, seg['speaker'])}: "
        lines.append(prefix + seg["text"])
    return "\n".join(lines)


# --- Transcrição: AssemblyAI (com falantes) --------------------------------------

def transcribe_assemblyai(job_id, audio_path):
    aai.settings.api_key = ASSEMBLYAI_API_KEY
    config = aai.TranscriptionConfig(speaker_labels=True, language_code=LANGUAGE)

    update_job(job_id, status="uploading", message="Enviando o áudio para a AssemblyAI…")
    transcript = aai.Transcriber().submit(audio_path, config)

    update_job(job_id, status="transcribing",
               message="Transcrevendo e identificando os falantes…")
    while transcript.status in (aai.TranscriptStatus.queued, aai.TranscriptStatus.processing):
        time.sleep(3)
        transcript = aai.Transcript.get_by_id(transcript.id)

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"A AssemblyAI não conseguiu transcrever: {transcript.error}")

    return [
        {
            "start": utt.start / 1000,
            "timestamp": format_timestamp(utt.start / 1000),
            "speaker": utt.speaker,
            "text": utt.text.strip(),
        }
        for utt in transcript.utterances or []
        if utt.text.strip()
    ]


# --- Transcrição: Whisper local (sem falantes) ------------------------------------

_whisper = None
_whisper_model = None
# Callback de progresso do job em andamento. Só existe uma thread de
# transcrição, então uma variável global basta.
_progress_callback = None


class _ProgressBar:
    """Substitui o tqdm.tqdm(total=<frames>) que o whisper.transcribe() usa,
    repassando o avanço para o job atual."""

    def __init__(self, total=None, **_kwargs):
        self.total = total or 0
        self.n = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def update(self, n=1):
        self.n += n
        if _progress_callback and self.total:
            _progress_callback(min(self.n / self.total, 1.0))


def load_whisper():
    # Importado só quando usado: o torch é pesado e desnecessário com a AssemblyAI.
    global _whisper, _whisper_model
    if _whisper is None:
        try:
            _whisper = importlib.import_module("whisper")
        except ModuleNotFoundError:
            raise RuntimeError(
                "O Whisper local não está instalado. Instale com "
                "pip install -r requirements-whisper.txt ou defina ASSEMBLYAI_API_KEY no .env."
            )
        # "whisper.transcribe" é sobrescrito pela função de mesmo nome no
        # __init__ do pacote; importlib devolve o módulo de fato.
        module = importlib.import_module("whisper.transcribe")
        module.tqdm = types.SimpleNamespace(tqdm=_ProgressBar)
    if _whisper_model is None:
        _whisper_model = _whisper.load_model(WHISPER_MODEL)
    return _whisper, _whisper_model


def transcribe_whisper(job_id, audio_path):
    global _progress_callback
    if _whisper_model is None:
        update_job(
            job_id, status="loading",
            message=f"Carregando modelo Whisper '{WHISPER_MODEL}' (na 1ª vez ele é baixado)…",
        )
    whisper, model = load_whisper()

    update_job(job_id, status="transcribing", message="Transcrevendo…", progress=0)
    _progress_callback = lambda p: update_job(job_id, progress=round(p * 100, 1))
    try:
        result = whisper.transcribe(
            model, audio_path, language=LANGUAGE, verbose=False,
            fp16=model.device.type == "cuda",
        )
    finally:
        _progress_callback = None

    return [
        {
            "start": seg["start"],
            "timestamp": format_timestamp(seg["start"]),
            "speaker": None,
            "text": seg["text"].strip(),
        }
        for seg in result["segments"]
        if seg["text"].strip()
    ]


# --- Pautas (Claude) ---------------------------------------------------------------

_ITEM = {
    "type": "object",
    "properties": {
        "descricao": {"type": "string"},
        "timestamp": {
            "type": "string",
            "description": "Momento da transcrição onde o assunto aparece, ex.: 12:40. "
                           "String vazia se não houver um momento claro.",
        },
    },
    "required": ["descricao", "timestamp"],
    "additionalProperties": False,
}

PAUTAS_SCHEMA = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string", "description": "Título curto da reunião."},
        "objetivo": {"type": "string"},
        "contexto": {"type": "string"},
        "participantes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rotulo": {
                        "type": "string",
                        "description": "Nome exatamente como aparece na transcrição.",
                    },
                    "nome_sugerido": {
                        "type": "string",
                        "description": "Nome real inferido do diálogo, se o rótulo for "
                                       "genérico (ex.: 'Falante A'). String vazia se "
                                       "não der para inferir com segurança.",
                    },
                    "papel": {
                        "type": "string",
                        "description": "Papel na reunião, se ficar claro. String vazia se não.",
                    },
                },
                "required": ["rotulo", "nome_sugerido", "papel"],
                "additionalProperties": False,
            },
        },
        "problemas_levantados": {"type": "array", "items": _ITEM},
        "decisoes": {"type": "array", "items": _ITEM},
        "solucoes_propostas": {"type": "array", "items": _ITEM},
        "pendencias": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "descricao": {"type": "string"},
                    "responsavel": {
                        "type": "string",
                        "description": "String vazia se ninguém foi designado.",
                    },
                    "prazo": {
                        "type": "string",
                        "description": "Como foi dito (ex.: 'sexta-feira'). "
                                       "String vazia se não houve prazo.",
                    },
                    "timestamp": {"type": "string"},
                },
                "required": ["descricao", "responsavel", "prazo", "timestamp"],
                "additionalProperties": False,
            },
        },
        "proximos_passos": {"type": "array", "items": _ITEM},
    },
    "required": [
        "titulo", "objetivo", "contexto", "participantes", "problemas_levantados",
        "decisoes", "solucoes_propostas", "pendencias", "proximos_passos",
    ],
    "additionalProperties": False,
}

PAUTAS_SYSTEM = """Você organiza transcrições de reuniões e conversas em pautas, em português do Brasil.

Cada linha da transcrição tem o formato "[MM:SS] Falante: texto". Extraia:
- objetivo: para que a conversa aconteceu;
- contexto: a situação de fundo necessária para entender a conversa;
- participantes: quem falou e, se der para perceber, o papel de cada um. Quando o rótulo for genérico ("Falante A"), sugira o nome real só se o diálogo deixar isso claro (alguém chama a pessoa pelo nome, ela se apresenta etc.);
- problemas levantados, decisões tomadas, soluções propostas, pendências (com responsável e prazo, quando ditos) e próximos passos.

Use só o que foi dito na transcrição; não invente responsáveis, prazos nem decisões. Se uma seção não tiver conteúdo, deixe a lista vazia (ou a string vazia). Escreva cada item como uma frase curta e objetiva. Em "timestamp", use o momento da linha onde o assunto aparece."""


def generate_pautas(transcript_text):
    return ia.generate(transcript_text, PAUTAS_SYSTEM, PAUTAS_SCHEMA)


def pautas_markdown(job):
    p = job["pautas"]
    out = [f"# {p['titulo'] or 'Pautas'}", ""]

    def ts(item):
        return f" [{item['timestamp']}]" if item.get("timestamp") else ""

    for title, key in (("Objetivo", "objetivo"), ("Contexto", "contexto")):
        if p[key]:
            out += [f"## {title}", "", p[key], ""]

    if p["participantes"]:
        out += ["## Participantes", ""]
        for part in p["participantes"]:
            line = f"- {part['rotulo']}"
            if part["papel"]:
                line += f" — {part['papel']}"
            out.append(line)
        out.append("")

    sections = (
        ("Problemas levantados", "problemas_levantados"),
        ("Decisões", "decisoes"),
        ("Soluções propostas", "solucoes_propostas"),
    )
    for title, key in sections:
        if p[key]:
            out += [f"## {title}", ""] + [f"- {i['descricao']}{ts(i)}" for i in p[key]] + [""]

    if p["pendencias"]:
        out += ["## Pendências", ""]
        for i in p["pendencias"]:
            extras = [x for x in (
                f"responsável: {i['responsavel']}" if i["responsavel"] else "",
                f"prazo: {i['prazo']}" if i["prazo"] else "",
            ) if x]
            suffix = f" ({'; '.join(extras)})" if extras else ""
            out.append(f"- {i['descricao']}{suffix}{ts(i)}")
        out.append("")

    if p["proximos_passos"]:
        out += ["## Próximos passos", ""]
        out += [f"- {i['descricao']}{ts(i)}" for i in p["proximos_passos"]] + [""]

    return "\n".join(out)


# --- Worker ----------------------------------------------------------------------

def process_job(job_id):
    job = jobs[job_id]
    workdir = job["workdir"]
    try:
        update_job(job_id, status="extracting", message="Extraindo o áudio…", progress=0)
        ext = ".mp3" if TRANSCRIBER == "assemblyai" else ".wav"
        audio_path = os.path.join(workdir, "audio" + ext)
        extract_audio(job["video_path"], audio_path)

        if TRANSCRIBER == "assemblyai":
            segments = transcribe_assemblyai(job_id, audio_path)
        else:
            segments = transcribe_whisper(job_id, audio_path)

        speakers = {}
        for seg in segments:
            if seg["speaker"] and seg["speaker"] not in speakers:
                speakers[seg["speaker"]] = f"Falante {seg['speaker']}"

        update_job(
            job_id, status="done", message="Transcrição concluída.",
            progress=100, segments=segments, speakers=speakers,
        )
    except Exception as exc:  # noqa: BLE001 — qualquer falha vira erro do job
        update_job(job_id, status="error", message=str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def worker():
    while True:
        job_id = job_queue.get()
        try:
            process_job(job_id)
        finally:
            job_queue.task_done()


threading.Thread(target=worker, daemon=True).start()


# --- Rotas -----------------------------------------------------------------------

def get_done_job(job_id):
    """Devolve o job concluído ou None. Chamar com jobs_lock adquirido."""
    job = jobs.get(job_id)
    if job is None or job["status"] != "done":
        return None
    return job


@app.get("/")
def index():
    return render_template(
        "index.html",
        max_minutes=MAX_DURATION_SECONDS // 60,
        extensions=", ".join(sorted(e.lstrip(".").upper() for e in ALLOWED_EXTENSIONS)),
        accept=",".join(sorted(ALLOWED_EXTENSIONS)),
        engine="AssemblyAI (com identificação de falantes)" if TRANSCRIBER == "assemblyai"
        else f"Whisper local, modelo {WHISPER_MODEL} (sem identificação de falantes)",
    )


@app.post("/upload")
def upload():
    if not ffmpeg_available():
        return jsonify(error="ffmpeg/ffprobe não encontrado no PATH. Veja o README."), 500

    file = request.files.get("video")
    if file is None or not file.filename:
        return jsonify(error="Nenhum arquivo enviado."), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify(error=f"Formato não suportado: {ext or '(sem extensão)'}."), 400

    workdir = tempfile.mkdtemp(prefix="video-transcriber-")
    video_path = os.path.join(workdir, "video" + ext)
    file.save(video_path)

    try:
        duration = get_duration(video_path)
    except ValueError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify(error=str(exc)), 400

    if duration > MAX_DURATION_SECONDS:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify(
            error=f"O arquivo tem {format_timestamp(duration)}; o limite é "
                  f"{MAX_DURATION_SECONDS // 60} minutos."
        ), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "message": "Aguardando na fila…",
            "progress": 0,
            "filename": secure_filename(file.filename) or "video",
            "duration": duration,
            "workdir": workdir,
            "video_path": video_path,
            "segments": [],
            "speakers": {},
            "pautas": None,
        }
    job_queue.put(job_id)
    return jsonify(job_id=job_id, duration=duration)


@app.get("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify(error="Job não encontrado."), 404
        payload = {
            "status": job["status"],
            "message": job["message"],
            "progress": job["progress"],
            "filename": job["filename"],
            "engine": TRANSCRIBER,
        }
        if job["status"] == "done":
            payload["segments"] = job["segments"]
            payload["speakers"] = job["speakers"]
            payload["text"] = render_transcript(job)
    return jsonify(payload)


@app.post("/speakers/<job_id>")
def rename_speakers(job_id):
    names = request.get_json(silent=True) or {}
    with jobs_lock:
        job = get_done_job(job_id)
        if job is None:
            return jsonify(error="Transcrição não disponível."), 404
        for code, name in names.items():
            if code in job["speakers"]:
                job["speakers"][code] = str(name).strip()[:60] or f"Falante {code}"
        return jsonify(speakers=job["speakers"], text=render_transcript(job))


@app.post("/pautas/<job_id>")
def pautas(job_id):
    with jobs_lock:
        job = get_done_job(job_id)
        if job is None:
            return jsonify(error="Transcrição não disponível."), 404
        transcript_text = render_transcript(job)

    if not transcript_text.strip():
        return jsonify(error="A transcrição está vazia."), 400

    try:
        result = generate_pautas(transcript_text)
    except ia.IAError as exc:
        return jsonify(error=str(exc)), 502
    except anthropic.AuthenticationError:
        return jsonify(error="A chave do Claude (Anthropic) foi recusada. "
                             "Confira em IA das pautas."), 500
    except anthropic.RateLimitError:
        return jsonify(error="Limite de uso da API da Anthropic atingido. "
                             "Tente de novo em instantes."), 429
    except anthropic.APIConnectionError:
        return jsonify(error="Não foi possível conectar à API da Anthropic."), 502
    except anthropic.APIStatusError as exc:
        return jsonify(error=f"Erro da API da Anthropic ({exc.status_code}): {exc.message}"), 502
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502

    with jobs_lock:
        job["pautas"] = result
    return jsonify(pautas=result)


# --- IA das pautas -------------------------------------------------------------------


def require_same_origin():
    """Só a própria página pode mudar a configuração: outro site aberto no navegador
    não consegue trocar a chave nem apontar as pautas para um servidor dele."""
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        abort(403)
    if not request.is_json:
        abort(415)


@app.get("/ia")
def ia_state():
    return jsonify(ia.public_state())


@app.post("/ia")
def ia_save():
    require_same_origin()
    try:
        return jsonify(ia.update(request.get_json(silent=True) or {}))
    except ia.IAError as exc:
        return jsonify(error=str(exc)), 400


@app.post("/ia/esquecer")
def ia_forget():
    require_same_origin()
    provider = (request.get_json(silent=True) or {}).get("provider")
    if provider not in ia.PROVIDERS:
        return jsonify(error="Provedor desconhecido."), 400
    return jsonify(ia.forget_key(provider))


@app.post("/ia/testar")
def ia_test():
    require_same_origin()
    data = request.get_json(silent=True) or {}
    try:
        models = ia.list_models(data.get("provider"), data.get("chave"), data.get("ollama_url"))
    except ia.IAError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(modelos=models)


@app.post("/ia/openrouter")
def ia_openrouter():
    require_same_origin()
    data = request.get_json(silent=True) or {}
    if not data.get("code") or not data.get("verifier"):
        return jsonify(error="Login incompleto. Tente entrar de novo."), 400
    try:
        return jsonify(ia.openrouter_exchange(data["code"], data["verifier"]))
    except ia.IAError as exc:
        return jsonify(error=str(exc)), 400


@app.get("/download/<job_id>")
def download(job_id):
    kind = request.args.get("tipo", "transcricao")
    with jobs_lock:
        job = get_done_job(job_id)
        if job is None:
            return jsonify(error="Transcrição não disponível."), 404
        name = os.path.splitext(job["filename"])[0] or "transcricao"
        if kind == "pautas":
            if not job["pautas"]:
                return jsonify(error="As pautas ainda não foram geradas."), 404
            body, filename, mimetype = pautas_markdown(job), f"{name}-pautas.md", "text/markdown"
        else:
            body, filename, mimetype = render_transcript(job), f"{name}.txt", "text/plain"
    return Response(
        body + "\n",
        mimetype=f"{mimetype}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.errorhandler(413)
def too_large(_err):
    return jsonify(error="Arquivo grande demais (limite de 4 GB)."), 413


if __name__ == "__main__":
    if not ffmpeg_available():
        print("AVISO: ffmpeg/ffprobe não encontrado no PATH. Instale antes de enviar vídeos.")
    if TRANSCRIBER == "whisper":
        print("AVISO: ASSEMBLYAI_API_KEY não definida — usando Whisper local, sem "
              "identificação de falantes.")
    if not ia.is_configured(ia.current_provider()):
        print("AVISO: a IA das pautas ainda não está configurada — escolha o provedor em "
              "\"IA das pautas\", no topo da página.")
    print("Abra http://127.0.0.1:5000 no navegador.")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
