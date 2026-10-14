"""Os trabalhos (jobs): fila de transcrição, estado em memória, histórico e pautas.

Uma thread processa um arquivo por vez: ffmpeg extrai o áudio, a transcrição roda,
o resultado vai para o histórico e, se a IA estiver pronta, as pautas começam
sozinhas numa thread própria. A página acompanha tudo por /status/<job_id>.
"""

import logging
import os
import queue
import shutil
import threading
import time

import historico
import ia
import midia
import pautas
import transcricao

log = logging.getLogger(__name__)

jobs = {}
jobs_lock = threading.Lock()
job_queue = queue.Queue()


def novo_job(job_id, filename, duration, language, workdir, video_path):
    """Registra um envio e o põe na fila."""
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "message": "Aguardando na fila…",
            "progress": 0,
            "filename": filename,
            "duration": duration,
            "engine": transcricao.TRANSCRIBER,
            "created": time.time(),
            "language": language,
            "detected_language": "",
            "workdir": workdir,
            "video_path": video_path,
            "segments": [],
            "speakers": {},
            "pautas": None,
            "pautas_status": None,
            "pautas_error": "",
        }
    job_queue.put(job_id)


def update_job(job_id, **fields):
    with jobs_lock:
        jobs[job_id].update(fields)


def get_job(job_id):
    """Job em memória ou, depois de um reinício, do histórico. Chamar com jobs_lock."""
    job = jobs.get(job_id)
    if job is None:
        job = historico.carregar(job_id)
        if job is not None:
            jobs[job_id] = job
    return job


def get_done_job(job_id):
    """Devolve o job concluído ou None. Chamar com jobs_lock adquirido."""
    job = get_job(job_id)
    if job is None or job["status"] != "done":
        return None
    return job


def guardar_historico(job_id, job):
    """Grava no histórico sem derrubar o job se o disco falhar: o resultado continua na tela."""
    try:
        historico.salvar(job_id, job)
    except Exception:  # noqa: BLE001
        log.exception("Não foi possível gravar a transcrição %s no histórico", job_id)


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


def job_language(job):
    """Idioma efetivo da reunião: o escolhido ou, no automático, o detectado ("" se não deu)."""
    language = job.get("language") or transcricao.DEFAULT_LANGUAGE
    return job.get("detected_language", "") if language == "auto" else language


# --- Pautas ------------------------------------------------------------------------------

def run_pautas(job_id):
    """Gera as pautas de um job concluído e guarda no histórico.

    Quem chama já marcou pautas_status = "gerando". Devolve (pautas, None) ou
    (None, (mensagem, código))."""
    with jobs_lock:
        transcript_text = render_transcript(jobs[job_id])
        language = job_language(jobs[job_id])
    try:
        result = pautas.generate_pautas(transcript_text, language)
    except Exception as exc:  # noqa: BLE001 — qualquer falha vira mensagem na tela
        erro = pautas.pautas_error(exc) or (f"Erro inesperado ao gerar as pautas: {exc}", 500)
        update_job(job_id, pautas_status="erro", pautas_error=erro[0])
        return None, erro
    with jobs_lock:
        job = jobs[job_id]
        job.update(pautas=result, pautas_status=None, pautas_error="")
        guardar_historico(job_id, job)
    return result, None


def start_auto_pautas(job_id):
    """Ao fim da transcrição, gera as pautas sozinho se a IA estiver pronta."""
    if not ia.auto_enabled() or not ia.is_configured(ia.current_provider()):
        return
    with jobs_lock:
        job = jobs[job_id]
        if not job["segments"]:
            return
        job.update(pautas_status="gerando", pautas_error="")
    # Thread própria: a IA pode levar minutos e a fila de transcrição não espera.
    threading.Thread(target=run_pautas, args=(job_id,), daemon=True).start()


# --- Fila de transcrição -------------------------------------------------------------------

def process_job(job_id):
    job = jobs[job_id]
    workdir = job["workdir"]
    try:
        update_job(job_id, status="extracting", message="Extraindo o áudio…", progress=0)
        assemblyai = transcricao.TRANSCRIBER == "assemblyai"
        audio_path = os.path.join(workdir, "audio" + (".mp3" if assemblyai else ".wav"))
        midia.extract_audio(job["video_path"], audio_path)

        transcribe = transcricao.transcribe_assemblyai if assemblyai else transcricao.transcribe_whisper
        segments, detected = transcribe(
            audio_path, job.get("language") or transcricao.DEFAULT_LANGUAGE,
            lambda **fields: update_job(job_id, **fields),
        )

        speakers = {}
        for seg in segments:
            if seg["speaker"] and seg["speaker"] not in speakers:
                speakers[seg["speaker"]] = f"Falante {seg['speaker']}"

        # O áudio já não é necessário: apaga antes de avisar a página que terminou.
        shutil.rmtree(workdir, ignore_errors=True)
        with jobs_lock:
            job.update(status="done", message="Transcrição concluída.", progress=100,
                       segments=segments, speakers=speakers, detected_language=detected)
            guardar_historico(job_id, job)
        start_auto_pautas(job_id)
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
