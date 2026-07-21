"""Base comum dos agentes ADK do 4unik.mail.

Carrega as KBs oficiais de dashboard/context/kb como contexto de instrução,
mantendo os agentes alinhados ao mesmo guia GTM usado pelo dashboard.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "dashboard" / "context" / "kb"
MAX_KB_CHARS = 14000

GUARDRAIL = (
    "GUARDRAIL: você produz SOMENTE rascunhos e classificações. Nunca afirme que "
    "algo foi enviado, publicado ou ativado — a aprovação e o envio são decisões "
    "do código e da equipe 4Unik, nunca suas."
)


def kb_context(max_chars: int = MAX_KB_CHARS) -> str:
    """Concatena as KBs (mesma ordem do dashboard: prefixo '0-' primeiro)."""
    if not KB_DIR.exists():
        return ""
    partes = []
    for f in sorted(KB_DIR.glob("*.md")):
        partes.append(f"### KB: {f.name}\n{f.read_text(encoding='utf-8', errors='replace')}")
    return "\n\n".join(partes)[:max_chars]
