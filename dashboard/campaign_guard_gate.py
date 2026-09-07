#!/usr/bin/env python3
"""Gate de economia de tokens — envolve ~/Scripts/campaign-guard/campaign_guard.py.

Antes de lotes LLM sobre uma campanha: exit 0 = ok; 1 = bloqueado; 2 = erro.
Se o script não existir (dev sem Scripts), libera (não bloqueia o dashboard).

Sincroniza o estado da campanha do SQLite local para um JSON temporário no
formato esperado pelo guard (id, status, last_update_at), porque o sample
padrão não conhece os IDs numéricos do dashboard.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import db

GUARD = Path.home() / "Scripts" / "campaign-guard" / "campaign_guard.py"


def _to_iso_utc(local_ts: str) -> str:
    """Converte 'YYYY-MM-DD HH:MM:SS' local do SQLite para ISO UTC aproximado."""
    s = (local_ts or "").strip()
    if not s:
        return datetime.now(timezone.utc).isoformat()
    try:
        # timestamps do dashboard são localtime sem tz — assume local → UTC via astimezone
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _dashboard_campaign_payload(campaign_id: int) -> dict | None:
    c = db.conn()
    row = c.execute(
        "SELECT id, nome, status, atualizada_em, criada_em FROM campaigns WHERE id=?",
        (int(campaign_id),)).fetchone()
    c.close()
    if not row:
        return None
    status = (row["status"] or "").strip().lower()
    # Guard: só "completed" é tratado como completo; demais = incompleto/ativo
    guard_status = "completed" if status in ("encerrada", "completed") else "running"
    last = (row["atualizada_em"] or row["criada_em"] or "").strip()
    return {
        "id": str(int(campaign_id)),
        "name": row["nome"] or f"campanha {campaign_id}",
        "status": guard_status,
        "last_update_at": _to_iso_utc(last),
    }


def allow_campaign_llm(campaign_id: int) -> tuple[bool, str]:
    """True se pode gastar tokens de LLM na campanha."""
    if not campaign_id:
        return True, "sem campaign_id"
    if not GUARD.is_file():
        return True, "campaign_guard ausente — liberado"

    payload = _dashboard_campaign_payload(int(campaign_id))
    if not payload:
        return False, f"campanha {campaign_id} não encontrada no dashboard"

    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([payload], fh, ensure_ascii=False)
            tmp_path = fh.name
        try:
            out = subprocess.run(
                ["python3", str(GUARD), str(int(campaign_id)), "--campaigns", tmp_path],
                capture_output=True, text=True, timeout=20,
                env=os.environ.copy(),
            )
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    except Exception as exc:
        return False, f"campaign_guard erro: {exc}"

    if out.returncode == 0:
        return True, (out.stdout or "ALLOW").strip()[:200]
    if out.returncode == 1:
        return False, (out.stdout or out.stderr or "BLOCK").strip()[:200]
    return False, (out.stderr or out.stdout or "ERROR").strip()[:200]
