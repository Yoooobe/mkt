#!/usr/bin/env python3
"""Carga idempotente das planilhas comerciais locais para o CRM.

Uso: .venv/bin/python scripts/import_local_leads.py
Não imprime dados pessoais: apenas contagens por arquivo e estado final.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import db  # noqa: E402
import sync  # noqa: E402


SOURCE_ROOT = ROOT.parent
SOURCES = [
    SOURCE_ROOT / "Leads Demo 2025 jan-14maio.xlsx",
    SOURCE_ROOT / "base_leads" / "RD Summit - report.xlsx",
    SOURCE_ROOT / "base_leads" / "Demo.xlsx",
    SOURCE_ROOT / "base_leads" / "SN - Customer Success.xlsx",
    SOURCE_ROOT / "base_leads" / "email.xls",
    SOURCE_ROOT / "base_leads" / "SN - endomarketing.xlsx",
    SOURCE_ROOT / "base_leads" / "Demo Realizada (1).xlsx",
    SOURCE_ROOT / "base_leads" / "Leads Demo 2025 jan-14maio.xlsx",
    SOURCE_ROOT / "base_leads" / "Demo Realizada.xlsx",
]


def main():
    db.init()
    report = []
    for path in SOURCES:
        if not path.exists():
            report.append({"arquivo": path.name, "erro": "arquivo não encontrado"})
            continue
        result = sync.import_contacts_upload(path.read_bytes(), path.name)
        report.append({k: v for k, v in result.items() if k != "ignorados"})

    c = db.conn()
    total = c.execute("SELECT count(*) FROM contacts").fetchone()[0]
    suprimidos = c.execute("SELECT count(*) FROM contacts WHERE tags LIKE '%nao-contatar%'").fetchone()[0]
    elegiveis = len(db.resolve_audience())
    c.close()
    print(json.dumps({"arquivos": report, "crm": {
        "contatos_total": total, "nao_contatar": suprimidos, "elegiveis_campanha": elegiveis,
    }}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
