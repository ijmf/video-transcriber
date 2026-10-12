"""Video Transcriber — aplicação web local para transcrever vídeos com Whisper.

Fluxo: upload do vídeo -> ffprobe valida a duração -> ffmpeg extrai o áudio
(WAV mono 16 kHz) -> Whisper transcreve em português -> a página consulta o
progresso em /status/<job_id> e exibe o resultado com timestamps.
"""

import glob
import importlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import types
import uuid

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

import whisper

# "whisper.transcribe" é sobrescrito pela função de mesmo nome no __init__ do
# pacote; importlib devolve o módulo de fato.
whisper_transcribe = importlib.import_module("whisper.transcribe")

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
MAX_DURATION_SECONDS = 30 * 60
MODEL_NAME = "small"
LANGUAGE = "pt"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB

jobs = {}
jobs_lock = threading.Lock()
job_queue = queue.Queue()

_model = None
# Callback de progresso do job em andamento. Só existe uma thread de
# transcrição, então uma variável global basta.
_progress_callback = None


# --- Progresso do Whisper ----------------------------------------------------
# whisper.transcribe() usa tqdm.tqdm(total=<frames>) para a barra de progresso.
# Substituímos o "tqdm" visto por esse módulo por uma barra que repassa o
# avanço para o job atual, sem afetar o tqdm do resto do processo.

class _ProgressBar:
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


whisper_transcribe.tqdm = types.SimpleNamespace(tqdm=_ProgressBar)


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
        raise ValueError("Não foi possível ler o vídeo. O arquivo pode estar corrompido.")
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        raise ValueError("Não foi possível determinar a duração do vídeo.")


def extract_audio(video_path, audio_path):
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-i", video_path,
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            audio_path,
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise RuntimeError(f"Falha ao extrair o áudio com ffmpeg:\n{tail}")


def update_job(job_id, **fields):
    with jobs_lock:
        jobs[job_id].update(fields)


def get_model():
    global _model
    if _model is None:
        _model = whisper.load_model(MODEL_NAME)
    return _model


# --- Worker ----------------------------------------------------------------------

def process_job(job_id):
    global _progress_callback
    job = jobs[job_id]
    workdir = job["workdir"]
    try:
        update_job(job_id, status="extracting", message="Extraindo áudio do vídeo…", progress=0)
        audio_path = os.path.join(workdir, "audio.wav")
        extract_audio(job["video_path"], audio_path)

        if _model is None:
            update_job(
                job_id, status="loading",
                message=f"Carregando modelo Whisper '{MODEL_NAME}' (na 1ª vez ele é baixado)…",
            )
        model = get_model()

        update_job(job_id, status="transcribing", message="Transcrevendo…", progress=0)
        _progress_callback = lambda p: update_job(job_id, progress=round(p * 100, 1))
        result = whisper.transcribe(
            model,
            audio_path,
            language=LANGUAGE,
            verbose=False,
            fp16=model.device.type == "cuda",
        )

        segments = [
            {
                "start": seg["start"],
                "end": seg["end"],
                "timestamp": format_timestamp(seg["start"]),
                "text": seg["text"].strip(),
            }
            for seg in result["segments"]
            if seg["text"].strip()
        ]
        text = "\n".join(f"[{s['timestamp']}] {s['text']}" for s in segments)
        update_job(
            job_id, status="done", message="Transcrição concluída.",
            progress=100, segments=segments, text=text,
        )
    except Exception as exc:  # noqa: BLE001 — qualquer falha vira erro do job
        update_job(job_id, status="error", message=str(exc))
    finally:
        _progress_callback = None
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

@app.get("/")
def index():
    return render_template(
        "index.html",
        max_minutes=MAX_DURATION_SECONDS // 60,
        extensions=", ".join(sorted(e.lstrip(".").upper() for e in ALLOWED_EXTENSIONS)),
        accept=",".join(sorted(ALLOWED_EXTENSIONS)),
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
            error=f"O vídeo tem {format_timestamp(duration)}; o limite é "
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
            "text": "",
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
        }
        if job["status"] == "done":
            payload["segments"] = job["segments"]
            payload["text"] = job["text"]
    return jsonify(payload)


@app.get("/download/<job_id>")
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None or job["status"] != "done":
            return jsonify(error="Transcrição não disponível."), 404
        text = job["text"]
        name = os.path.splitext(job["filename"])[0] or "transcricao"
    return Response(
        text + "\n",
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.txt"'},
    )


@app.errorhandler(413)
def too_large(_err):
    return jsonify(error="Arquivo grande demais (limite de 4 GB)."), 413


if __name__ == "__main__":
    if not ffmpeg_available():
        print("AVISO: ffmpeg/ffprobe não encontrado no PATH. Instale antes de enviar vídeos.")
    print("Abra http://127.0.0.1:5000 no navegador.")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
