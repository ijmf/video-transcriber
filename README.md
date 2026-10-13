# Video Transcriber

Aplicação web local que transcreve vídeos em português usando o [OpenAI Whisper](https://github.com/openai/whisper) (modelo `small`), rodando 100% no seu computador, sem API key.

- Upload de vídeos **MP4, MOV, AVI e MKV** de até **45 minutos**
- Extração do áudio com **ffmpeg**
- Transcrição com timestamps no formato `[00:00] texto`
- Barra de progresso (envio → extração → modelo → transcrição)
- Copiar para a área de transferência ou baixar como `.txt`

## Requisitos

- **Python 3.13**
- **ffmpeg** (com `ffprobe`) no `PATH`
- ~3 GB livres (PyTorch + modelo Whisper `small`, ~460 MB, baixado no primeiro uso)

### Instalando o ffmpeg no Windows

```powershell
winget install Gyan.FFmpeg
```

Feche e abra o terminal depois e confira com `ffmpeg -version`.

Se o ffmpeg não estiver no `PATH`, o app procura sozinho a instalação do winget (`%LOCALAPPDATA%\Microsoft\WinGet\Packages\*FFmpeg*`). Para outro local, defina a variável `FFMPEG_DIR` com a pasta `bin` do ffmpeg.

## Instalação

```powershell
git clone https://github.com/ijmf/video-transcriber.git
cd video-transcriber
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> O PyTorch instalado pelo pip no Windows é a versão CPU. Se você tiver GPU NVIDIA,
> pode instalar a versão CUDA antes do `requirements.txt` (veja https://pytorch.org/get-started/locally/);
> a aplicação usa a GPU automaticamente se estiver disponível.

## Uso

```powershell
.\.venv\Scripts\Activate.ps1
python app.py
```

Abra http://127.0.0.1:5000, arraste um vídeo (ou clique para escolher) e acompanhe o progresso. Quando terminar, use **Copiar** ou **Baixar .txt**.

A primeira transcrição demora mais porque o modelo é baixado para `~/.cache/whisper`. Em CPU, conte com algo entre 0,5× e 1,5× a duração do vídeo, dependendo da máquina.

## Como funciona

1. `POST /upload` recebe o vídeo, valida extensão e duração (via `ffprobe`) e cria um job.
2. Uma thread de trabalho processa um vídeo por vez:
   `ffmpeg` extrai o áudio (WAV mono 16 kHz) → Whisper transcreve com `language="pt"`.
3. A página consulta `GET /status/<job_id>` a cada segundo para mostrar o progresso.
4. `GET /download/<job_id>` devolve a transcrição em `.txt`.

Os arquivos temporários são apagados ao fim de cada job. Os resultados ficam só em memória: reiniciar o servidor os descarta.

## Estrutura

```
video-transcriber/
├── app.py              # servidor Flask + fila de transcrição
├── requirements.txt
├── .gitignore
├── README.md
└── templates/
    └── index.html      # página única (HTML + CSS + JS)
```
