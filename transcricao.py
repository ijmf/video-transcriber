"""Transcrição: AssemblyAI (com identificação de falantes) ou Whisper local (sem falantes).

As duas funções recebem o idioma e uma função avisar(**campos) para informar etapa e
progresso, e devolvem (falas, idioma_detectado). O idioma detectado só vem preenchido
quando o pedido foi "auto".
"""

import importlib
import os
import time
import types

import assemblyai as aai
from dotenv import load_dotenv

from midia import format_timestamp

load_dotenv()

# Idiomas da reunião: código usado pela AssemblyAI e pelo Whisper -> nome (como aparece na
# página e no pedido das pautas). "auto" deixa o serviço detectar.
LANGUAGES = {
    "pt": "Português", "en": "Inglês", "es": "Espanhol", "fr": "Francês",
    "de": "Alemão", "it": "Italiano", "auto": "Detectar automaticamente",
}
DEFAULT_LANGUAGE = "pt"
WHISPER_MODEL = "small"
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()

# Com chave da AssemblyAI a transcrição identifica os falantes; sem ela, cai
# no Whisper local (sem falantes), como na primeira versão do projeto.
TRANSCRIBER = "assemblyai" if ASSEMBLYAI_API_KEY else "whisper"


def engine_label():
    if TRANSCRIBER == "assemblyai":
        return "AssemblyAI (com identificação de falantes)"
    return f"Whisper local, modelo {WHISPER_MODEL} (sem identificação de falantes)"


# --- AssemblyAI (com falantes) ---------------------------------------------------------

def transcribe_assemblyai(audio_path, language, avisar):
    aai.settings.api_key = ASSEMBLYAI_API_KEY
    if language == "auto":
        config = aai.TranscriptionConfig(speaker_labels=True, language_detection=True)
    else:
        config = aai.TranscriptionConfig(speaker_labels=True, language_code=language)

    avisar(status="uploading", message="Enviando o áudio para a AssemblyAI…")
    transcript = aai.Transcriber().submit(audio_path, config)

    avisar(status="transcribing", message="Transcrevendo e identificando os falantes…")
    while transcript.status in (aai.TranscriptStatus.queued, aai.TranscriptStatus.processing):
        time.sleep(3)
        transcript = aai.Transcript.get_by_id(transcript.id)

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"A AssemblyAI não conseguiu transcrever: {transcript.error}")
    detected = ""
    if language == "auto":
        code = (getattr(transcript, "json_response", None) or {}).get("language_code") or ""
        detected = code.split("_")[0].lower()  # "en_us" -> "en"

    segments = [
        {
            "start": utt.start / 1000,
            "timestamp": format_timestamp(utt.start / 1000),
            "speaker": utt.speaker,
            "text": utt.text.strip(),
        }
        for utt in transcript.utterances or []
        if utt.text.strip()
    ]
    return segments, detected


# --- Whisper local (sem falantes) ------------------------------------------------------

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
                "pip install -r requirements-whisper.txt (no Docker: WHISPER=1 docker compose up "
                "--build) ou defina ASSEMBLYAI_API_KEY no .env."
            )
        # "whisper.transcribe" é sobrescrito pela função de mesmo nome no
        # __init__ do pacote; importlib devolve o módulo de fato.
        module = importlib.import_module("whisper.transcribe")
        module.tqdm = types.SimpleNamespace(tqdm=_ProgressBar)
    if _whisper_model is None:
        _whisper_model = _whisper.load_model(WHISPER_MODEL)
    return _whisper, _whisper_model


def transcribe_whisper(audio_path, language, avisar):
    global _progress_callback
    if _whisper_model is None:
        avisar(status="loading",
               message=f"Carregando modelo Whisper '{WHISPER_MODEL}' (na 1ª vez ele é baixado)…")
    whisper, model = load_whisper()

    avisar(status="transcribing", message="Transcrevendo…", progress=0)
    _progress_callback = lambda p: avisar(progress=round(p * 100, 1))
    try:
        result = whisper.transcribe(
            model, audio_path, language=None if language == "auto" else language, verbose=False,
            fp16=model.device.type == "cuda",
        )
    finally:
        _progress_callback = None

    segments = [
        {
            "start": seg["start"],
            "timestamp": format_timestamp(seg["start"]),
            "speaker": None,
            "text": seg["text"].strip(),
        }
        for seg in result["segments"]
        if seg["text"].strip()
    ]
    return segments, (result.get("language") or "") if language == "auto" else ""
