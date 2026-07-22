"""Exporta o pacote editorial de uma campanha (briefing + e-mails) em md/txt/docx."""
from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional, Tuple

import db

try:
    from docx import Document  # type: ignore
    HAS_DOCX = True
except Exception:
    Document = None  # type: ignore
    HAS_DOCX = False


def _briefing(cid: int) -> Dict[str, Any]:
    raw = db.meta_get(f"briefing_{cid}", "")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _por_email_map(brief: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    out = {}
    for e in brief.get("por_email") or []:
        try:
            out[int(e.get("ordem"))] = e
        except (TypeError, ValueError):
            continue
    return out


def coletar_pacote(cid: int) -> Optional[Dict[str, Any]]:
    """Monta o dict canônico usado pelos três formatos de export."""
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return None
    steps = [dict(r) for r in c.execute(
        "SELECT * FROM campaign_steps WHERE campaign_id=? ORDER BY ordem", (cid,)).fetchall()]
    sender = None
    if camp["sender_id"]:
        sender = c.execute("SELECT email,nome FROM senders WHERE id=?",
                           (camp["sender_id"],)).fetchone()
    emails = []
    for s in steps:
        ref = s["template_ref"] or ""
        tpl = None
        if ref.startswith("ai:"):
            try:
                tid = int(ref.split(":", 1)[1])
                tpl = c.execute("SELECT * FROM templates_ai WHERE id=?", (tid,)).fetchone()
            except ValueError:
                tpl = None
        emails.append({
            "ordem": s["ordem"],
            "offset_dias": s["offset_dias"],
            "template_ref": ref,
            "nome": (tpl["nome"] if tpl else "") or f"Email {s['ordem']}",
            "assunto": (tpl["assunto"] if tpl else "") or "",
            "assuntos_alt": (tpl["assuntos_alt"] if tpl else "") or "",
            "preheader": (tpl["preheader"] if tpl else "") or "",
            "cta": (tpl["cta"] if tpl else "") or "",
            "cta_url": (tpl["cta_url"] if tpl else "") or "",
            "texto_plano": (tpl["texto_plano"] if tpl else "") or "",
            "status": (tpl["status"] if tpl else "") or "",
            "versao_campanha": (tpl["versao_campanha"] if tpl else None),
        })
    c.close()
    brief = _briefing(cid)
    por = _por_email_map(brief)
    for e in emails:
        pe = por.get(e["ordem"]) or {}
        e["porque"] = pe.get("porque_este_angulo") or ""
        e["prova"] = pe.get("prova_e_origem") or ""
        if not e["assunto"] and pe.get("assunto_esperado"):
            e["assunto"] = pe["assunto_esperado"]
    return {
        "id": cid,
        "nome": camp["nome"] or f"Campanha {cid}",
        "objetivo": camp["objetivo"] or "",
        "contexto": camp["contexto"] or "",
        "tipo": camp["tipo"] or "",
        "status": camp["status"] or "",
        "versao": int(camp["versao"] or 1) if "versao" in camp.keys() else 1,
        "atualizada_em": (camp["atualizada_em"] if "atualizada_em" in camp.keys() else "") or "",
        "criada_em": camp["criada_em"] or "",
        "remetente": (sender["email"] if sender else ""),
        "briefing": brief,
        "emails": emails,
        "rewrite_log": db.rewrite_log_get(cid, limit=5),
    }


def _alts(raw: str) -> List[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def render_md(pack: Dict[str, Any]) -> str:
    b = pack.get("briefing") or {}
    med = b.get("medicao") or {}
    lines = [
        f"# {pack['nome']}",
        "",
        f"- **Versão:** v{pack['versao']}",
        f"- **Atualizada em:** {pack['atualizada_em'] or '—'}",
        f"- **Criada em:** {pack['criada_em'] or '—'}",
        f"- **Status:** {pack['status']} · tipo {pack['tipo']}",
        f"- **Remetente:** {pack['remetente'] or '—'}",
        "",
        "## Objetivo",
        "",
        pack["objetivo"] or "—",
        "",
        "## Planejamento / contexto",
        "",
        pack["contexto"] or "—",
        "",
    ]
    if b:
        lines += [
            "## Briefing de lançamento",
            "",
            f"**Recomendação:** {b.get('recomendacao', '—')}",
            "",
            "### Resumo",
            "",
            b.get("resumo") or "—",
            "",
            "### ICP e dor",
            "",
            b.get("icp_e_dor") or "—",
            "",
            "### Por que agora",
            "",
            b.get("por_que_agora") or "—",
            "",
            "### Justificativa",
            "",
            b.get("justificativa") or "—",
            "",
        ]
        if b.get("riscos"):
            lines += ["### Riscos", ""] + [f"- {r}" for r in b["riscos"]] + [""]
        lines += [
            "### Medição",
            "",
            f"- utm_campaign: `{med.get('utm_campaign', '—')}`",
            f"- evento GA4: `{med.get('evento_ga4', '—')}`",
            f"- métrica: {med.get('metrica_decisao', '—')}",
            "",
        ]
        if b.get("checklist_pre_lancamento"):
            lines += ["### Checklist pré-lançamento", ""] + [
                f"- [ ] {x}" for x in b["checklist_pre_lancamento"]] + [""]
    lines += ["## Sequência de e-mails", ""]
    for e in pack["emails"]:
        lines += [
            f"### E{e['ordem']} — {e['nome']}",
            "",
            f"**Assunto:** {e['assunto'] or '—'}",
        ]
        alts = _alts(e.get("assuntos_alt") or "")
        if alts:
            lines += ["", "**Assuntos alternativos:**"] + [f"- {a}" for a in alts]
        lines += [
            "",
            f"**Status do template:** {e['status'] or '—'} · versão do template: "
            f"{e['versao_campanha'] if e['versao_campanha'] is not None else '—'}",
            "",
            "#### Por que este e-mail",
            "",
            e.get("porque") or "—",
            "",
            "#### Prova / origem",
            "",
            e.get("prova") or "—",
            "",
            f"**CTA:** {e['cta'] or '—'} → {e['cta_url'] or '—'}",
            "",
            "#### Texto",
            "",
            e.get("texto_plano") or "—",
            "",
            "---",
            "",
        ]
    if pack.get("rewrite_log"):
        lines += ["## Histórico de versões", ""]
        for r in pack["rewrite_log"]:
            lines.append(
                f"- **v{r.get('versao')}** · {r.get('em', '—')} · "
                f"{r.get('origem', '')}: {(r.get('diretrizes') or '')[:180]}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_txt(pack: Dict[str, Any]) -> str:
    md = render_md(pack)
    # txt = md sem marcações óbvias
    txt = md
    for a, b in (("**", ""), ("## ", ""), ("### ", ""), ("#### ", ""),
                 ("# ", ""), ("`", ""), ("- [ ] ", "- ")):
        txt = txt.replace(a, b)
    return txt


def render_docx(pack: Dict[str, Any]) -> bytes:
    if not HAS_DOCX:
        raise RuntimeError("python-docx não instalado")
    doc = Document()
    doc.add_heading(pack["nome"], level=0)
    doc.add_paragraph(
        f"Versão v{pack['versao']} · Atualizada em {pack['atualizada_em'] or '—'} · "
        f"Remetente {pack['remetente'] or '—'}")
    doc.add_heading("Objetivo", level=1)
    doc.add_paragraph(pack["objetivo"] or "—")
    doc.add_heading("Planejamento / contexto", level=1)
    for para in (pack["contexto"] or "—").split("\n\n"):
        doc.add_paragraph(para.strip() or "—")
    b = pack.get("briefing") or {}
    if b:
        doc.add_heading("Briefing de lançamento", level=1)
        doc.add_paragraph(f"Recomendação: {b.get('recomendacao', '—')}")
        for titulo, chave in (("Resumo", "resumo"), ("ICP e dor", "icp_e_dor"),
                              ("Por que agora", "por_que_agora"),
                              ("Justificativa", "justificativa")):
            doc.add_heading(titulo, level=2)
            for para in (b.get(chave) or "—").split("\n\n"):
                doc.add_paragraph(para.strip() or "—")
    doc.add_heading("Sequência de e-mails", level=1)
    for e in pack["emails"]:
        doc.add_heading(f"E{e['ordem']} — {e['nome']}", level=2)
        doc.add_paragraph(f"Assunto: {e['assunto'] or '—'}")
        doc.add_paragraph(f"Status: {e['status'] or '—'} · "
                          f"versão template: {e['versao_campanha'] if e['versao_campanha'] is not None else '—'}")
        doc.add_heading("Por que este e-mail", level=3)
        for para in (e.get("porque") or "—").split("\n\n"):
            doc.add_paragraph(para.strip() or "—")
        doc.add_heading("Prova / origem", level=3)
        for para in (e.get("prova") or "—").split("\n\n"):
            doc.add_paragraph(para.strip() or "—")
        doc.add_paragraph(f"CTA: {e['cta'] or '—'} → {e['cta_url'] or '—'}")
        doc.add_heading("Texto", level=3)
        for para in (e.get("texto_plano") or "—").split("\n\n"):
            doc.add_paragraph(para.strip() or "—")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def exportar(cid: int, fmt: str) -> Tuple[bytes, str, str]:
    """Retorna (bytes, media_type, filename)."""
    pack = coletar_pacote(cid)
    if not pack:
        raise ValueError("campanha não encontrada")
    slug = (pack["nome"] or f"campanha-{cid}").strip().lower()
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug)[:60] or f"campanha-{cid}"
    fmt = (fmt or "md").lower().strip()
    if fmt == "md":
        return render_md(pack).encode("utf-8"), "text/markdown; charset=utf-8", f"{slug}-v{pack['versao']}.md"
    if fmt == "txt":
        return render_txt(pack).encode("utf-8"), "text/plain; charset=utf-8", f"{slug}-v{pack['versao']}.txt"
    if fmt in ("doc", "docx"):
        if not HAS_DOCX:
            raise RuntimeError("export .docx indisponível (instale python-docx)")
        return (render_docx(pack),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                f"{slug}-v{pack['versao']}.docx")
    raise ValueError("formato inválido — use md, txt ou docx")
