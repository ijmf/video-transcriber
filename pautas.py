"""Pautas: o pedido à IA (formato JSON e instruções) e a versão em Markdown.

A IA em si (Claude, OpenAI, Gemini, OpenRouter ou Ollama) fica em ia.py.
"""

import anthropic

import ia
from transcricao import DEFAULT_LANGUAGE

_ITEM = {
    "type": "object",
    "properties": {
        "descricao": {"type": "string"},
        "timestamp": {
            "type": "string",
            "description": "Momento da transcrição onde o assunto aparece, ex.: 12:40. "
                           "String vazia se não houver um momento claro.",
        },
    },
    "required": ["descricao", "timestamp"],
    "additionalProperties": False,
}

PAUTAS_SCHEMA = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string", "description": "Título curto da reunião."},
        "objetivo": {"type": "string"},
        "contexto": {"type": "string"},
        "participantes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rotulo": {
                        "type": "string",
                        "description": "Nome exatamente como aparece na transcrição.",
                    },
                    "nome_sugerido": {
                        "type": "string",
                        "description": "Nome real inferido do diálogo, se o rótulo for "
                                       "genérico (ex.: 'Falante A'). String vazia se "
                                       "não der para inferir com segurança.",
                    },
                    "papel": {
                        "type": "string",
                        "description": "Papel na reunião, se ficar claro. String vazia se não.",
                    },
                },
                "required": ["rotulo", "nome_sugerido", "papel"],
                "additionalProperties": False,
            },
        },
        "problemas_levantados": {"type": "array", "items": _ITEM},
        "decisoes": {"type": "array", "items": _ITEM},
        "solucoes_propostas": {"type": "array", "items": _ITEM},
        "pendencias": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "descricao": {"type": "string"},
                    "responsavel": {
                        "type": "string",
                        "description": "String vazia se ninguém foi designado.",
                    },
                    "prazo": {
                        "type": "string",
                        "description": "Como foi dito (ex.: 'sexta-feira'). "
                                       "String vazia se não houve prazo.",
                    },
                    "timestamp": {"type": "string"},
                },
                "required": ["descricao", "responsavel", "prazo", "timestamp"],
                "additionalProperties": False,
            },
        },
        "proximos_passos": {"type": "array", "items": _ITEM},
    },
    "required": [
        "titulo", "objetivo", "contexto", "participantes", "problemas_levantados",
        "decisoes", "solucoes_propostas", "pendencias", "proximos_passos",
    ],
    "additionalProperties": False,
}

PAUTAS_SYSTEM = """Você organiza transcrições de reuniões e conversas em pautas.

Cada linha da transcrição tem o formato "[MM:SS] Falante: texto". Extraia:
- objetivo: para que a conversa aconteceu;
- contexto: a situação de fundo necessária para entender a conversa;
- participantes: quem falou e, se der para perceber, o papel de cada um. Quando o rótulo for genérico ("Falante A"), sugira o nome real só se o diálogo deixar isso claro (alguém chama a pessoa pelo nome, ela se apresenta etc.);
- problemas levantados, decisões tomadas, soluções propostas, pendências (com responsável e prazo, quando ditos) e próximos passos.

Use só o que foi dito na transcrição; não invente responsáveis, prazos nem decisões. Se uma seção não tiver conteúdo, deixe a lista vazia (ou a string vazia). Escreva cada item como uma frase curta e objetiva. Em "timestamp", use o momento da linha onde o assunto aparece.

{idioma}"""

# Em que língua a IA escreve as pautas, conforme o idioma da reunião.
PAUTAS_IDIOMA = {
    "pt": "Escreva todo o conteúdo em português do Brasil.",
    "en": "A reunião foi em inglês: escreva todo o conteúdo em inglês.",
    "es": "A reunião foi em espanhol: escreva todo o conteúdo em espanhol.",
    "fr": "A reunião foi em francês: escreva todo o conteúdo em francês.",
    "de": "A reunião foi em alemão: escreva todo o conteúdo em alemão.",
    "it": "A reunião foi em italiano: escreva todo o conteúdo em italiano.",
}
PAUTAS_IDIOMA_DA_TRANSCRICAO = "Escreva todo o conteúdo no mesmo idioma em que a transcrição está."


def pautas_system(language):
    return PAUTAS_SYSTEM.format(idioma=PAUTAS_IDIOMA.get(language, PAUTAS_IDIOMA_DA_TRANSCRICAO))


def generate_pautas(transcript_text, language=DEFAULT_LANGUAGE):
    return ia.generate(transcript_text, pautas_system(language), PAUTAS_SCHEMA)


def pautas_error(exc):
    """Mensagem e código HTTP para uma falha ao gerar as pautas (None se desconhecida)."""
    if isinstance(exc, ia.IAError):
        return str(exc), 502
    if isinstance(exc, anthropic.AuthenticationError):
        return "A chave do Claude (Anthropic) foi recusada. Confira em IA das pautas.", 500
    if isinstance(exc, anthropic.RateLimitError):
        return "Limite de uso da API da Anthropic atingido. Tente de novo em instantes.", 429
    if isinstance(exc, anthropic.APIConnectionError):
        return "Não foi possível conectar à API da Anthropic.", 502
    if isinstance(exc, anthropic.APIStatusError):
        return f"Erro da API da Anthropic ({exc.status_code}): {exc.message}", 502
    if isinstance(exc, RuntimeError):
        return str(exc), 502
    return None


def pautas_markdown(p):
    """As pautas em Markdown, para baixar como .md."""
    out = [f"# {p['titulo'] or 'Pautas'}", ""]

    def ts(item):
        return f" [{item['timestamp']}]" if item.get("timestamp") else ""

    for title, key in (("Objetivo", "objetivo"), ("Contexto", "contexto")):
        if p[key]:
            out += [f"## {title}", "", p[key], ""]

    if p["participantes"]:
        out += ["## Participantes", ""]
        for part in p["participantes"]:
            line = f"- {part['rotulo']}"
            if part["papel"]:
                line += f" — {part['papel']}"
            out.append(line)
        out.append("")

    sections = (
        ("Problemas levantados", "problemas_levantados"),
        ("Decisões", "decisoes"),
        ("Soluções propostas", "solucoes_propostas"),
    )
    for title, key in sections:
        if p[key]:
            out += [f"## {title}", ""] + [f"- {i['descricao']}{ts(i)}" for i in p[key]] + [""]

    if p["pendencias"]:
        out += ["## Pendências", ""]
        for i in p["pendencias"]:
            extras = [x for x in (
                f"responsável: {i['responsavel']}" if i["responsavel"] else "",
                f"prazo: {i['prazo']}" if i["prazo"] else "",
            ) if x]
            suffix = f" ({'; '.join(extras)})" if extras else ""
            out.append(f"- {i['descricao']}{suffix}{ts(i)}")
        out.append("")

    if p["proximos_passos"]:
        out += ["## Próximos passos", ""]
        out += [f"- {i['descricao']}{ts(i)}" for i in p["proximos_passos"]] + [""]

    return "\n".join(out)
