# Video Transcriber

Aplicação web que transcreve vídeos em português, identifica quem está falando e organiza a conversa em pautas.

- Upload de vídeos **MP4, MOV, AVI e MKV** de até **45 minutos**
- Extração do áudio com **ffmpeg**
- Transcrição com **identificação de falantes** ([AssemblyAI](https://www.assemblyai.com/)), no formato `[00:00] João: texto`
- Renomear os falantes ("Falante A" → "João"); a IA sugere nomes quando eles aparecem na conversa
- **Organização por pautas** com o [Claude](https://www.anthropic.com/): objetivo, contexto, participantes, problemas levantados, decisões, soluções propostas, pendências (responsável e prazo) e próximos passos, cada item com o timestamp onde foi falado
- Copiar ou baixar a transcrição (`.txt`) e as pautas (`.md`)
- Sem chave da AssemblyAI, a transcrição roda no **Whisper local** (modelo `small`), sem identificação de falantes

## Requisitos

- **Python 3.13**
- **ffmpeg** (com `ffprobe`) no `PATH`
- Chave da **AssemblyAI** (transcrição com falantes) e da **Anthropic** (pautas)
- Para o modo Whisper local: ~3 GB livres (PyTorch + modelo `small`, ~460 MB, baixado no primeiro uso)

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
copy .env.example .env
```

Abra o `.env` e preencha `ASSEMBLYAI_API_KEY` e `ANTHROPIC_API_KEY`. O `.env` está no `.gitignore` e não vai para o repositório.

> O PyTorch instalado pelo pip no Windows é a versão CPU. Se você tiver GPU NVIDIA e for usar o Whisper local,
> instale a versão CUDA antes do `requirements.txt` (veja https://pytorch.org/get-started/locally/).

## Uso

```powershell
.\.venv\Scripts\Activate.ps1
python app.py
```

Abra http://127.0.0.1:5000 e envie um vídeo. Quando a transcrição terminar:

1. Em **Quem está falando**, dê nome a cada falante e clique em **Salvar nomes**.
2. Em **Pautas**, clique em **Organizar por pautas** (leva de 30 s a 2 min).
3. Copie ou baixe a transcrição e as pautas.

## Custos aproximados (vídeo de 45 min)

| Etapa | Serviço | Custo |
|---|---|---|
| Transcrição + falantes | AssemblyAI | ~US$ 0,20 |
| Pautas | Claude Opus 5.5 (padrão) | ~US$ 0,17 |

O modelo das pautas pode ser trocado com `CLAUDE_MODEL` no `.env` (ex.: `claude-sonnet-5-5`, mais barato). Confira os preços atuais nas páginas da AssemblyAI e da Anthropic.

## Como funciona

1. `POST /upload` recebe o vídeo, valida extensão e duração (via `ffprobe`) e cria um job.
2. Uma thread de trabalho processa um vídeo por vez: `ffmpeg` extrai o áudio → AssemblyAI transcreve com `speaker_labels` (ou o Whisper local, sem falantes).
3. A página consulta `GET /status/<job_id>` a cada segundo para mostrar o progresso.
4. `POST /speakers/<job_id>` renomeia os falantes.
5. `POST /pautas/<job_id>` envia a transcrição ao Claude, que devolve as pautas em JSON estruturado.
6. `GET /download/<job_id>` devolve a transcrição (`.txt`); com `?tipo=pautas`, as pautas (`.md`).

Os arquivos temporários são apagados ao fim de cada job. Os resultados ficam só em memória: reiniciar o servidor os descarta.

## Estrutura

```
video-transcriber/
├── app.py              # servidor Flask, fila de transcrição e pautas
├── requirements.txt
├── .env.example        # modelo do .env com as chaves
├── .gitignore
├── README.md
└── templates/
    └── index.html      # página única (HTML + CSS + JS)
```
