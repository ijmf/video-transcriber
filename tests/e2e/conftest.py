"""Testes da página no navegador (Playwright).

O app roda numa thread deste processo, então os testes podem simular a
transcrição com monkeypatch. A IA das pautas é um servidor falso que fala a API
compatível com a da OpenAI: o app chega nele pelo caminho real do ia.py, como
se fosse um Ollama.

Microfone: o Chromium usa um dispositivo falso (--use-fake-device-for-media-stream).
Compartilhamento de tela: o navegador não deixa automatizar a escolha da tela, então
getDisplayMedia é trocado por um script que devolve vídeo de um canvas e um tom de áudio.
"""

import json
import threading

import pytest
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

import app as app_module

PAUTAS_FALSAS = {
    "titulo": "Lançamento do produto",
    "objetivo": "Definir a nova data de lançamento",
    "contexto": "",
    "participantes": [
        {"rotulo": "Ana", "nome_sugerido": "", "papel": "Gerente"},
        {"rotulo": "Falante B", "nome_sugerido": "Bruno", "papel": ""},
    ],
    "problemas_levantados": [],
    "decisoes": [{"descricao": "Adiar o lançamento para sexta", "timestamp": "00:04"}],
    "solucoes_propostas": [],
    "pendencias": [{"descricao": "Avisar o time", "responsavel": "Bruno", "prazo": "sexta", "timestamp": ""}],
    "proximos_passos": [],
}


class Servidor:
    def __init__(self, wsgi_app):
        self._srv = make_server("127.0.0.1", 0, wsgi_app, threaded=True)
        self.url = f"http://127.0.0.1:{self._srv.server_port}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def parar(self):
        self._srv.shutdown()


@pytest.fixture(scope="session")
def app_url():
    srv = Servidor(app_module.app)
    yield srv.url
    srv.parar()


@pytest.fixture(scope="session")
def ia_falsa():
    fake = Flask("ia_falsa")
    fake.pedidos = []

    @fake.get("/v1/models")
    def modelos():
        return jsonify(data=[{"id": "modelo-falso"}, {"id": "qwen2.5:7b"}])

    @fake.post("/v1/chat/completions")
    def chat():
        fake.pedidos.append(request.get_json())
        return jsonify(choices=[{"finish_reason": "stop",
                                 "message": {"content": json.dumps(PAUTAS_FALSAS)}}])

    srv = Servidor(fake)
    srv.pedidos = fake.pedidos
    yield srv
    srv.parar()


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    return {
        **browser_type_launch_args,
        "args": ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                 "--autoplay-policy=no-user-gesture-required"],
    }


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "permissions": ["microphone"], "accept_downloads": True}
