# Video Transcriber — guia para mudanças

App Flask de página única que transcreve reuniões (AssemblyAI com falantes, ou Whisper local), organiza as pautas com a IA que a pessoa escolher e guarda tudo num histórico local. Roda só no computador de quem usa (127.0.0.1). Tudo em português do Brasil: interface, mensagens, commits e PRs.

## Onde fica cada coisa

| Arquivo | O que tem |
|---|---|
| `app.py` | Só as rotas Flask, a proteção de Host e o limite de upload |
| `midia.py` | ffmpeg: achar no PATH, medir duração, extrair áudio |
| `transcricao.py` | AssemblyAI e Whisper, `LANGUAGES`, `TRANSCRIBER`. As funções recebem `(audio, idioma, avisar)` e devolvem `(falas, idioma_detectado)` |
| `pautas.py` | Schema JSON, pedido à IA (`pautas_system`), `pautas_error`, `pautas_markdown(pautas)` |
| `ia.py` | Provedores (Claude pelo SDK; OpenAI, Gemini, OpenRouter, Ollama por httpx) e `ia_config.json` |
| `trabalhos.py` | Fila (uma thread), `jobs` em memória, pautas automáticas, gravação no histórico |
| `historico.py` | SQLite (`historico.db`); migra colunas novas sozinho (`_migrar`) |
| `templates/index.html` | Só o HTML. A configuração do servidor vai num `<script id="config" type="application/json">` |
| `static/estilo.css` | Todo o visual (cores em variáveis no `:root`) |
| `static/js/` | Scripts clássicos que compartilham o escopo global, nesta ordem: `comum`, `envio`, `ia`, `historico`, `gravacao` |
| `tests/` | pytest; `tests/e2e/` com Playwright |

## Comandos

```powershell
.\.venv\Scripts\python.exe app.py                                  # rodar (http://127.0.0.1:5000)
.\.venv\Scripts\python.exe -m pytest --browser-channel chrome     # todos os testes (110), usando o Chrome instalado
.\.venv\Scripts\python.exe -m pytest --ignore=tests/e2e            # só os rápidos, sem navegador
docker compose up -d --build                                       # Docker (WHISPER=1 para incluir o Whisper)
```

## Regras que não podem quebrar

- **Nenhum segredo no repositório.** `.env`, `ia_config.json` e `historico.db` estão no `.gitignore` (e no `.dockerignore`). A página nunca recebe a chave inteira, só os 4 últimos caracteres.
- **Só o próprio computador.** `only_local_host` recusa qualquer Host que não seja local (contra DNS rebinding). Toda rota que muda estado chama `require_same_origin()` (Origin igual; JSON obrigatório, exceto `DELETE`).
- **Texto do usuário entra como texto.** No JS, use `el(...)`/`textContent`, nunca `innerHTML`.
- **Testes nunca tocam os arquivos reais.** O `tests/conftest.py` troca `ia.CONFIG_PATH` e `historico.DB_PATH` por pastas temporárias e zera as chaves do `.env`. Módulo novo que grava em disco precisa do mesmo isolamento.
- **Simulações nos testes:** para transcrever, `transcricao_falsa` (troca `transcricao.transcribe_assemblyai`); para a IA, `monkeypatch` em `ia.generate` ou o servidor `ia_falsa` do `tests/e2e/conftest.py`, que fala a API da OpenAI. Para simular, troque o atributo no módulo onde a função mora (`transcricao`, `trabalhos`, `midia`, `pautas`), não no `app`.

## Ao mudar...

- **Uma dependência:** edite o `.in` e gere de novo os `.txt` travados, nesta ordem: `requirements`, `requirements-whisper`, `requirements-dev`. Use `python -m uv pip compile <arquivo>.in --universal --python-version 3.13 -o <arquivo>.txt`. Depois `pip install -r requirements-dev.txt -r requirements-whisper.txt` e rode os testes.
- **O banco:** coluna nova entra no `_SCHEMA` **e** no `_migrar` (bancos antigos já existem nas máquinas). Valor ausente em linha antiga precisa de um padrão ao ler.
- **Um arquivo Python novo na raiz:** o Dockerfile já copia `*.py`. Um arquivo novo em `static/` também já vai junto.
- **Um script novo da página:** inclua em `templates/index.html` na ordem certa. O que roda ao carregar só pode usar o que veio antes.
- **Texto da interface:** mantenha o tom (frases curtas, sem jargão) e ajuste os testes que procuram esse texto (`tests/e2e/test_pagina.py`).
- **Funcionalidade visível:** atualize o README (lista de recursos, "Como funciona" e "Estrutura").

## Fluxo de entrega

1. Branch a partir da `main` (`feat/…`, `fix/…`, `chore/…`, `refactor/…`).
2. Testes passando localmente com `--browser-channel chrome`.
3. Commit em português, explicando o porquê.
4. Push, PR e esperar o CI (4 checks: pytest + Playwright, pip-audit ×2, Imagem Docker).
5. **Squash merge só depois dos 4 checks verdes.** O repositório não exige checks, então um merge automático (`--auto`) acontece na hora, sem esperar o CI. Depois, apague a branch.

## Armadilhas já encontradas

- **Substituições sem conferência.** Editar arquivo com substituição sem conferir pode falhar em silêncio. Foi assim que o `HISTORICO_DB` ficou fora do Dockerfile. Confira que a troca aconteceu.
- **Escapes no `tojson`.** O Jinja escreve acentos como `ê`. Nos testes, compare o JSON lido (`json.loads`), não o texto.
- **Valores mostrados na página** (ex.: "2 GB") usam `&nbsp;` para não quebrar linha no celular.
- **Avisos de CRLF** no `git add`, no Windows, são normais.

## Ideias que ficaram para depois

- Processar mais de um vídeo por vez (a fila hoje é uma thread só, e o Whisper local aguenta um por vez).
- Proteção da branch `main` exigindo os checks do CI. Isso é configuração no GitHub, quem faz é o dono do repositório.
