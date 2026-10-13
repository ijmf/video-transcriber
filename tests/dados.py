"""Dados e mídia usados pelos testes."""

import shutil
import subprocess

SEGMENTOS = [
    {"start": 0.0, "timestamp": "00:00", "speaker": "A", "text": "Bom dia, Bruno. Vamos falar do lançamento."},
    {"start": 4.0, "timestamp": "00:04", "speaker": "B", "text": "Bom dia. Decidi adiar para sexta."},
]


def gerar_audio(path, segundos=2):
    """Gera um áudio curto (tom de 440 Hz) com o ffmpeg."""
    subprocess.run(
        [shutil.which("ffmpeg"), "-nostdin", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={segundos}", str(path)],
        capture_output=True, check=True,
    )
    return path
