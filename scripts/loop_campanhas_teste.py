#!/usr/bin/env python3
"""Modelo de teste do loop de sugestão e geração de campanhas (sem a UI).

Roda o mesmo loop do dashboard: sync dos mds originais dos skills →
estrategista GTM propõe o portfólio → copywriter critica e refina →
(opcional) geração sequencial de cada campanha como rascunho.

Uso:
  .venv/bin/python scripts/loop_campanhas_teste.py            # dry-run: só sugere e imprime
  .venv/bin/python scripts/loop_campanhas_teste.py --max 3    # limita o portfólio
  .venv/bin/python scripts/loop_campanhas_teste.py --gerar    # também cria os rascunhos

Guardrails: nada é ativado nem enviado — campanhas nascem em 'rascunho' com
emails 'pendente'; a aprovação e ativação continuam manuais no dashboard.
Requer a sessão do `claude` já configurada (a mesma usada pelo dashboard).
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import ai  # noqa: E402
import db  # noqa: E402


def _imprimir_portfolio(status):
    campanhas = status.get("campanhas") or []
    print(f"\n{'=' * 72}\nPORTFÓLIO SUGERIDO — {len(campanhas)} campanha(s)"
          f" · status: {status.get('status')} · {status.get('atualizado_em', '')}\n{'=' * 72}")
    for i, c in enumerate(campanhas, 1):
        aprovado = "✅ aprovada" if c.get("aprovado", True) else "❌ reprovada pelo copywriter"
        print(f"\n[{i}] {c.get('nome', '(sem nome)')} — {aprovado}")
        print(f"    ICP:       {c.get('icp', '—')}")
        print(f"    Segmento:  {', '.join(c.get('segmentos') or []) or '—'}"
              f" · {c.get('n_emails', 3)} email(s) · tipo {c.get('tipo', 'finita')}")
        print(f"    Objetivo:  {c.get('objetivo', '—')}")
        print(f"    Gancho:    {c.get('gancho', '—')}")
        print(f"    Prova:     {c.get('prova', '—')}")
        print(f"    Medição:   GA4 {c.get('evento_ga4', '—')} · utm_campaign={c.get('utm_campaign', '—')}")
        if c.get("critica"):
            print(f"    Copywriter: {c['critica']}")
    print()


def _acompanhar_etapas(parar):
    """Imprime a etapa corrente do loop enquanto a IA trabalha (leitura de meta)."""
    ultima = ""
    while not parar.is_set():
        p = ai.suggest_status() or {}
        etapa = p.get("etapa", "")
        if etapa and etapa != ultima:
            print(f"  … {etapa}")
            ultima = etapa
        parar.wait(3)


def main():
    parser = argparse.ArgumentParser(description="Teste do loop de sugestão de campanhas")
    parser.add_argument("--max", type=int, default=4, help="máximo de campanhas sugeridas (1–8)")
    parser.add_argument("--gerar", action="store_true",
                        help="além de sugerir, gera cada campanha como rascunho (sequencial)")
    parser.add_argument("--sender", type=int, default=1, help="id do remetente das campanhas geradas")
    args = parser.parse_args()

    db.init()
    claude = ai.ai_status()
    if not claude.get("ok"):
        print(f"⚠️  Claude indisponível: {claude.get('mensagem')}")
        sys.exit(1)
    print(f"Claude ok (modelo {claude.get('modelo')})")

    sincronizados = ai.sync_skill_kbs()
    print("Skills sincronizados para o KB: " + (", ".join(sincronizados) or "já atualizados"))

    parar = threading.Event()
    monitor = threading.Thread(target=_acompanhar_etapas, args=(parar,), daemon=True)
    monitor.start()
    inicio = time.time()
    try:
        status = ai.sugerir_campanhas(args.max)
    finally:
        parar.set()
    _imprimir_portfolio(status or {})
    print(f"Sugestão concluída em {time.time() - inicio:.0f}s.")

    if not args.gerar:
        print("Dry-run: nenhum rascunho criado. Rode com --gerar para criar as campanhas em rascunho,"
              "\nou use o botão '🚀 Gerar rascunhos' no dashboard (aba Campanhas).")
        return

    print("\nGerando rascunhos (sequencial — a IA escreve os emails de cada campanha)…")
    parar = threading.Event()
    monitor = threading.Thread(target=_acompanhar_etapas, args=(parar,), daemon=True)
    monitor.start()
    try:
        status = ai.gerar_portfolio(args.sender)
    finally:
        parar.set()
    criadas = (status or {}).get("criadas") or []
    print(f"\n{len(criadas)} campanha(s) criada(s) em RASCUNHO:")
    for c in criadas:
        print(f"  - #{c.get('cid')} {c.get('nome')} → http://localhost:8377/#/campanha/{c.get('cid')}")
    print("Revise, aprove os emails e ative manualmente no dashboard. Nada foi enviado.")


if __name__ == "__main__":
    main()
