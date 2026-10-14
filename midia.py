"""Vídeo e áudio com o ffmpeg: encontrar o ffmpeg, medir a duração e extrair o áudio."""

import glob
import json
import os
import re
import shutil
import subprocess


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
