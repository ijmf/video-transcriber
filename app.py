"""Video Transcriber — transcrição de reuniões com identificação de falantes e
organização por pautas.

Fluxo: upload do vídeo -> ffprobe valida a duração -> ffmpeg extrai o áudio ->
transcrição (AssemblyAI com identificação de falantes, ou Whisper local se não
houver chave da AssemblyAI) -> a página consulta o progresso em
/status/<job_id> -> a IA escolhida organiza a transcrição em pautas (objetivo,
contexto, problemas, decisões...) -> tudo fica no histórico.

Este arquivo tem só o servidor web. O resto está em:
  midia.py        ffmpeg: duração e extração do áudio
  transcricao.py  AssemblyAI e Whisper, idiomas
  pautas.py       pedido à IA e Markdown das pautas
  ia.py           provedores de IA (Claude, OpenAI, Gemini, OpenRouter, Ollama)
  trabalhos.py    fila, estado dos jobs e pautas automáticas
  historico.py    histórico em SQLite
"""

import os
import shutil
import tempfile
import uuid

from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, render_template, request
from werkzeug.utils import secure_filename

load_dotenv()

import historico  # noqa: E402
import ia  # noqa: E402 — lê o .env ao ser importado
import midia  # noqa: E402
import pautas as pautas_mod  # noqa: E402
import trabalhos  # noqa: E402
import transcricao  # noqa: E402
from trabalhos import get_done_job, get_job, jobs, jobs_lock, render_transcript  # noqa: E402

# .webm é o formato das gravações feitas pelo navegador.
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".mp3", ".webm"}
MAX_DURATION_SECONDS = 45 * 60
# 45 minutos de vídeo de reunião em 1080p ficam entre 1 e 2 GB; o MP3, abaixo de 100 MB.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# O app só atende pelo próprio computador. Recusar qualquer outro Host barra o
# DNS rebinding: um site que aponte o domínio dele para 127.0.0.1 não passa daqui.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


@app.before_request
def only_local_host():
    hostname = request.host.rsplit(":", 1)[0] if not request.host.endswith("]") else request.host
    if hostname.lower() not in ALLOWED_HOSTS:
        abort(403)


def require_same_origin(json_body=True):
    """Só a própria página pode mudar a configuração ou apagar o histórico: outro site
    aberto no navegador não consegue trocar a chave nem apontar as pautas para um
    servidor dele."""
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        abort(403)
    if json_body and not request.is_json:
        abort(415)


def upload_limit_label():
    """O limite de upload para mostrar na tela, ex.: "2 GB"."""
    gb = app.config["MAX_CONTENT_LENGTH"] / 1024 ** 3
    return f"{gb:g} GB".replace(".", ",") if gb >= 0.1 else f"{app.config['MAX_CONTENT_LENGTH']} bytes"


# --- Página e envio ----------------------------------------------------------------------

@app.get("/")
def index():
    return render_template(
        "index.html",
        max_minutes=MAX_DURATION_SECONDS // 60,
        max_bytes=app.config["MAX_CONTENT_LENGTH"],
        max_label=upload_limit_label(),
        languages=transcricao.LANGUAGES,
        default_language=transcricao.DEFAULT_LANGUAGE,
        extensions=", ".join(sorted(e.lstrip(".").upper() for e in ALLOWED_EXTENSIONS)),
        accept=",".join(sorted(ALLOWED_EXTENSIONS)),
        engine=transcricao.engine_label(),
    )


@app.post("/upload")
def upload():
    if not midia.ffmpeg_available():
        return jsonify(error="ffmpeg/ffprobe não encontrado no PATH. Veja o README."), 500

    file = request.files.get("video")
    if file is None or not file.filename:
        return jsonify(error="Nenhum arquivo enviado."), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify(error=f"Formato não suportado: {ext or '(sem extensão)'}."), 400

    language = request.form.get("idioma") or transcricao.DEFAULT_LANGUAGE
    if language not in transcricao.LANGUAGES:
        return jsonify(error=f"Idioma não suportado: {language}."), 400

    workdir = tempfile.mkdtemp(prefix="video-transcriber-")
    video_path = os.path.join(workdir, "video" + ext)
    file.save(video_path)

    try:
        duration = midia.get_duration(video_path)
    except ValueError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify(error=str(exc)), 400

    if duration > MAX_DURATION_SECONDS:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify(
            error=f"O arquivo tem {midia.format_timestamp(duration)}; o limite é "
                  f"{MAX_DURATION_SECONDS // 60} minutos."
        ), 400

    job_id = uuid.uuid4().hex
    trabalhos.novo_job(job_id, secure_filename(file.filename) or "video", duration,
                       language, workdir, video_path)
    return jsonify(job_id=job_id, duration=duration)


@app.errorhandler(413)
def too_large(_err):
    return jsonify(error=f"Arquivo grande demais: o limite é {upload_limit_label()}."), 413


# --- Andamento, falantes, pautas e downloads -------------------------------------------

@app.get("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = get_job(job_id)
        if job is None:
            return jsonify(error="Job não encontrado."), 404
        payload = {
            "status": job["status"],
            "message": job["message"],
            "progress": job["progress"],
            "filename": job["filename"],
            "engine": job.get("engine") or transcricao.TRANSCRIBER,
        }
        if job["status"] == "done":
            payload["segments"] = job["segments"]
            payload["speakers"] = job["speakers"]
            payload["text"] = render_transcript(job)
            payload["pautas"] = job["pautas"]
            payload["pautas_status"] = job.get("pautas_status")
            payload["pautas_error"] = job.get("pautas_error", "")
            payload["language"] = trabalhos.job_language(job)
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
        trabalhos.guardar_historico(job_id, job)
        return jsonify(speakers=job["speakers"], text=render_transcript(job))


@app.post("/pautas/<job_id>")
def pautas(job_id):
    with jobs_lock:
        job = get_done_job(job_id)
        if job is None:
            return jsonify(error="Transcrição não disponível."), 404
        if not render_transcript(job).strip():
            return jsonify(error="A transcrição está vazia."), 400
        if job.get("pautas_status") == "gerando":
            return jsonify(error="As pautas já estão sendo geradas. Aguarde."), 409
        job.update(pautas_status="gerando", pautas_error="")

    result, erro = trabalhos.run_pautas(job_id)
    if erro:
        return jsonify(error=erro[0]), erro[1]
    return jsonify(pautas=result)


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
            body = pautas_mod.pautas_markdown(job["pautas"])
            filename, mimetype = f"{name}-pautas.md", "text/markdown"
        else:
            body, filename, mimetype = render_transcript(job), f"{name}.txt", "text/plain"
    return Response(
        body + "\n",
        mimetype=f"{mimetype}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- IA das pautas -----------------------------------------------------------------------

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


# --- Histórico ---------------------------------------------------------------------------

@app.get("/historico")
def historico_lista():
    return jsonify(transcricoes=historico.listar())


@app.delete("/historico/<job_id>")
def historico_apagar(job_id):
    require_same_origin(json_body=False)
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None and (job["status"] != "done" or job.get("pautas_status") == "gerando"):
            return jsonify(error="Espere esta transcrição terminar para apagar."), 409
        jobs.pop(job_id, None)
        apagado = historico.apagar(job_id)
    if not apagado and job is None:
        return jsonify(error="Transcrição não encontrada."), 404
    return jsonify(ok=True)


if __name__ == "__main__":
    if not midia.ffmpeg_available():
        print("AVISO: ffmpeg/ffprobe não encontrado no PATH. Instale antes de enviar vídeos.")
    if transcricao.TRANSCRIBER == "whisper":
        print("AVISO: ASSEMBLYAI_API_KEY não definida — usando Whisper local, sem "
              "identificação de falantes.")
    if not ia.is_configured(ia.current_provider()):
        print("AVISO: a IA das pautas ainda não está configurada — escolha o provedor em "
              "\"IA das pautas\", no topo da página.")
    # HOST=0.0.0.0 só dentro do Docker, que publica a porta apenas em 127.0.0.1.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"Abra http://127.0.0.1:{port} no navegador.")
    app.run(host=host, port=port, debug=False, threaded=True)
