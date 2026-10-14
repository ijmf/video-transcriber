# Video Transcriber — Python 3.13 + ffmpeg.
# Sem Whisper (usa a AssemblyAI):  docker build -t video-transcriber .
# Com Whisper local (CPU, ~3,6 GB): docker build --build-arg WHISPER=1 -t video-transcriber:whisper .
FROM python:3.13-slim

ARG WHISPER=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=5000 \
    IA_CONFIG=/dados/ia_config.json \
    HISTORICO_DB=/dados/historico.db \
    XDG_CACHE_HOME=/dados/cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-whisper.txt ./
# Com Whisper: o PyTorch de CPU na versão travada, sem as bibliotecas CUDA (cuda-*,
# nvidia-*), que o requirements-whisper.txt lista para Linux e somam vários GB.
RUN pip install -r requirements.txt \
    && if [ "$WHISPER" = "1" ]; then \
         pip install "$(grep -E '^torch==' requirements-whisper.txt | cut -d' ' -f1)" \
             --index-url https://download.pytorch.org/whl/cpu \
         && grep -vE '^(cuda-|nvidia-)' requirements-whisper.txt > /tmp/whisper-cpu.txt \
         && pip install -r /tmp/whisper-cpu.txt && rm /tmp/whisper-cpu.txt; \
       fi

COPY app.py ia.py historico.py ./
COPY templates ./templates

# /dados guarda a escolha da IA (ia_config.json), o histórico (historico.db)
# e o modelo do Whisper baixado.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /dados && chown app:app /dados
USER app
VOLUME /dados

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/ia', timeout=4)"

CMD ["python", "app.py"]
