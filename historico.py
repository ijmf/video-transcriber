"""Histórico das transcrições em SQLite.

Cada transcrição concluída é guardada com os falantes e as pautas, para sobreviver
a um reinício do app. O arquivo fica ao lado do app (historico.db, fora do git);
no Docker, HISTORICO_DB aponta para o volume.
"""

import contextlib
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("HISTORICO_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "historico.db")

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcricoes (
    id TEXT PRIMARY KEY,
    criado_em REAL NOT NULL,
    filename TEXT NOT NULL,
    duration REAL NOT NULL,
    engine TEXT NOT NULL,
    segments TEXT NOT NULL,
    speakers TEXT NOT NULL,
    pautas TEXT,
    idioma TEXT
)
"""
_migrado = set()


def _migrar(conn):
    """Bancos criados antes da coluna idioma ganham a coluna (uma vez por arquivo)."""
    if DB_PATH in _migrado:
        return
    colunas = {r[1] for r in conn.execute("PRAGMA table_info(transcricoes)")}
    if "idioma" not in colunas:
        conn.execute("ALTER TABLE transcricoes ADD COLUMN idioma TEXT")
    _migrado.add(DB_PATH)


@contextlib.contextmanager
def _connect():
    """Abre, confirma e fecha a conexão (no Windows, conexão aberta prende o arquivo)."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(_SCHEMA)
            _migrar(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()


def salvar(job_id, job):
    """Grava (ou atualiza) uma transcrição concluída."""
    row = (
        job_id, job.get("created") or time.time(), job["filename"], job.get("duration") or 0,
        job.get("engine") or "", json.dumps(job["segments"], ensure_ascii=False),
        json.dumps(job["speakers"], ensure_ascii=False),
        json.dumps(job["pautas"], ensure_ascii=False) if job.get("pautas") else None,
        _idioma(job),
    )
    with _connect() as conn:
        conn.execute(
            "INSERT INTO transcricoes"
            " (id, criado_em, filename, duration, engine, segments, speakers, pautas, idioma)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET speakers = excluded.speakers, pautas = excluded.pautas",
            row,
        )


def _idioma(job):
    """Idioma efetivo: o escolhido ou, no automático, o detectado."""
    idioma = job.get("language") or "pt"
    return job.get("detected_language", "") if idioma == "auto" else idioma


def carregar(job_id):
    """Devolve a transcrição no mesmo formato dos jobs em memória, ou None."""
    with _connect() as conn:
        r = conn.execute("SELECT * FROM transcricoes WHERE id = ?", (job_id,)).fetchone()
    if r is None:
        return None
    return {
        "status": "done", "message": "Transcrição concluída.", "progress": 100,
        "filename": r["filename"], "duration": r["duration"], "engine": r["engine"],
        "created": r["criado_em"], "workdir": "", "video_path": "",
        "segments": json.loads(r["segments"]), "speakers": json.loads(r["speakers"]),
        "pautas": json.loads(r["pautas"]) if r["pautas"] else None,
        "pautas_status": None, "pautas_error": "",
        # Transcrições de antes desta coluna eram todas em português.
        "language": r["idioma"] if r["idioma"] is not None else "pt", "detected_language": "",
    }


def listar(limite=50):
    """Resumo das transcrições, da mais recente para a mais antiga."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, criado_em, filename, duration, pautas, idioma FROM transcricoes"
            " ORDER BY criado_em DESC LIMIT ?", (limite,)).fetchall()
    out = []
    for r in rows:
        pautas = json.loads(r["pautas"]) if r["pautas"] else None
        out.append({
            "id": r["id"], "criado_em": r["criado_em"], "filename": r["filename"],
            "duration": r["duration"], "tem_pautas": bool(pautas),
            "titulo": (pautas or {}).get("titulo", ""),
            "idioma": r["idioma"] if r["idioma"] is not None else "pt",
        })
    return out


def apagar(job_id):
    with _connect() as conn:
        return conn.execute("DELETE FROM transcricoes WHERE id = ?", (job_id,)).rowcount > 0
