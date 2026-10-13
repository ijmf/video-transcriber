"""Fluxos da página: configurar a IA, enviar arquivo, gravar reunião e gerar as pautas."""

import re

from playwright.sync_api import Page, expect

from dados import SEGMENTOS
from midia_falsa import TELA_COM_AUDIO, TELA_RECUSADA

ESPERA = 30_000  # envio + ffmpeg + consulta do status a cada 1 s


def configurar_ia_falsa(page: Page, ia_falsa):
    page.click("#ia-open")
    page.get_by_role("radio", name=re.compile("Ollama")).check()
    page.fill("#ia-url", ia_falsa.url)
    page.click("#ia-test")
    expect(page.locator("#ia-status")).to_contain_text("Conexão ok: 2 modelo(s)")
    expect(page.locator("#ia-model")).to_have_value("qwen2.5:7b")  # preferido entre os listados
    page.click("#ia-save")
    expect(page.locator("#ia-status")).to_have_text("Salvo. As pautas serão feitas por Ollama (local).")
    expect(page.locator("#ia-current")).to_have_text("Ollama (local) · qwen2.5:7b")
    page.click("#ia-close")


def test_enviar_arquivo_renomear_falantes_e_gerar_pautas(page: Page, app_url, ia_falsa,
                                                        audio_mp3, transcricao_falsa):
    page.goto(app_url)
    expect(page.locator("#ia-current")).to_have_text("não configurada")
    configurar_ia_falsa(page, ia_falsa)

    page.set_input_files("#video", str(audio_mp3))
    expect(page.locator("#results")).to_be_visible(timeout=ESPERA)
    expect(page.locator("#res-file")).to_have_text("reuniao.mp3")
    linhas = page.locator(".tx-line")
    expect(linhas).to_have_count(2)
    expect(linhas.first).to_contain_text("Falante A" + SEGMENTOS[0]["text"])

    page.fill("#spk-A", "Ana")
    page.click("#spk-save")
    expect(page.locator("#spk-note")).to_have_text("Nomes salvos. A transcrição já usa esses nomes.")
    expect(linhas.first).to_contain_text("Ana" + SEGMENTOS[0]["text"])

    expect(page.locator("#pautas-using")).to_have_text(
        "As pautas serão feitas por Ollama (local), modelo qwen2.5:7b.")
    page.click("#pautas-go")
    saida = page.locator("#pautas-out")
    expect(saida.locator(".title")).to_have_text("Lançamento do produto", timeout=ESPERA)
    expect(saida).to_contain_text("Adiar o lançamento para sexta")
    expect(saida.locator(".tag-o")).to_have_text("Bruno")
    expect(page.locator("#pautas-go")).to_have_text("Refazer pautas")

    # A IA recebeu a transcrição com o nome salvo e sugeriu o nome do Falante B.
    pedido = ia_falsa.pedidos[-1]
    assert pedido["model"] == "qwen2.5:7b"
    assert "[00:00] Ana: Bom dia" in pedido["messages"][1]["content"]
    expect(page.locator("#spk-B")).to_have_value("Bruno")
    expect(page.locator("#spk-B")).to_have_class("suggested")

    with page.expect_download() as baixado:
        page.click("#tx-dl")
    assert baixado.value.suggested_filename == "reuniao.txt"
    with page.expect_download() as baixado:
        page.click("#pautas-dl")
    assert baixado.value.suggested_filename == "reuniao-pautas.md"
    with open(baixado.value.path(), encoding="utf-8") as fh:
        assert "- Avisar o time (responsável: Bruno; prazo: sexta)" in fh.read()

    page.click("#again")
    expect(page.locator("#drop")).to_be_visible()
    expect(page.locator("#work")).to_be_hidden()


def test_formato_nao_suportado(page: Page, app_url, tmp_path):
    notas = tmp_path / "notas.txt"
    notas.write_text("oi")
    page.goto(app_url)
    page.set_input_files("#video", str(notas))
    expect(page.locator("#send-status")).to_contain_text("Formato não suportado: .txt")
    expect(page.locator("#work")).to_be_hidden()


def test_arquivo_longo_demais_mostra_o_erro_do_servidor(page: Page, app_url, audio_mp3, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "MAX_DURATION_SECONDS", 1)
    page.goto(app_url)
    page.set_input_files("#video", str(audio_mp3))
    expect(page.locator("#send-status")).to_contain_text("o limite é 0 minutos", timeout=ESPERA)
    expect(page.locator("#drop")).to_be_visible()


def test_pautas_sem_ia_configurada_abrem_o_painel(page: Page, app_url, audio_mp3, transcricao_falsa):
    page.goto(app_url)
    page.set_input_files("#video", str(audio_mp3))
    expect(page.locator("#pautas-go")).to_have_text("Configurar a IA", timeout=ESPERA)
    page.click("#pautas-go")
    expect(page.locator("#ia-panel")).to_be_visible()
    expect(page.locator("#ia-status")).to_have_text(
        "Configure a IA das pautas e depois clique em Organizar por pautas.")


def test_gravar_reuniao_com_microfone_e_audio_do_computador(page: Page, app_url, audio_mp3,
                                                          transcricao_falsa):
    page.add_init_script(TELA_COM_AUDIO)
    page.goto(app_url)
    page.click("#rec-start")
    expect(page.locator("#rec-live")).to_be_visible()
    expect(page.locator("#rec-msg")).to_have_text("Gravando a sua voz e o áudio da reunião.")
    expect(page.locator("#lvl-sys")).not_to_have_class(re.compile(r"\boff\b"))
    expect(page.locator("#drop")).to_be_hidden()
    expect(page.locator("#rec-time")).to_have_text("00:02", timeout=5_000)

    page.click("#rec-stop")
    expect(page.locator("#results")).to_be_visible(timeout=ESPERA)
    expect(page.locator("#res-file")).to_have_text(re.compile(r"^reuniao-\d{4}-\d\d-\d\d-\d\dh\d\d\.webm$"))
    expect(page.locator("#rec-dl")).to_be_visible()
    expect(page.locator(".tx-line")).to_have_count(2)
    # O servidor recebeu um .webm de verdade e o ffmpeg extraiu o áudio dele.
    assert transcricao_falsa[-1].endswith("audio.mp3")


def test_gravar_sem_compartilhar_a_tela_grava_so_o_microfone(page: Page, app_url, audio_mp3,
                                                            transcricao_falsa):
    page.add_init_script(TELA_RECUSADA)
    page.goto(app_url)
    page.click("#rec-start")
    expect(page.locator("#rec-msg")).to_contain_text("Gravando só o microfone")
    expect(page.locator("#lvl-sys")).to_have_class(re.compile(r"\boff\b"))
    expect(page.locator("#rec-time")).to_have_text("00:01", timeout=5_000)

    page.on("dialog", lambda d: d.accept())
    page.click("#rec-cancel")
    expect(page.locator("#rec-idle")).to_be_visible()
    expect(page.locator("#drop")).to_be_visible()
    expect(page.locator("#work")).to_be_hidden()
    assert not transcricao_falsa
