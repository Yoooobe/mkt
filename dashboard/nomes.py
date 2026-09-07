#!/usr/bin/env python3
"""4unik.mail — inteligência de qualidade de nomes/leads (a IA entende; o código decide).

Muitos contatos vieram com `nome` derivado da parte local do e-mail ("Danielpassosmartins",
"Iubtelecom", "Comercial"). Este módulo separa o joio do trigo em 2 passos:

  1. suspeito() / candidato() — pré-filtro barato (sem IA) que sinaliza quem merece análise.
  2. analisar_lote() — manda suspeitos + evidência pública para a IA, que devolve ação,
                       sugestões de nome/empresa/cargo/telefone, confiança e motivo.

É PURO (não toca no banco): reaproveitado pela limpeza em massa e por importações futuras.
"""
import json
import re
import unicodedata

import agent_engine

# Auto-aplica no CRM quando a IA devolve confiança >= este limiar (híbrido UI + auto).
AUTO_APPLY_MIN_CONF = 0.8

# Palavras de setor/função → quase nunca são nome de pessoa (viram 'limpar').
PALAVRAS_SETOR = {
    "comercial", "diretoria", "diretor", "gerencia", "gerente", "financeiro", "faturamento",
    "suporte", "atendimento", "contato", "contatos", "vendas", "venda", "adm", "administrativo",
    "rh", "ti", "coordenacao", "coordenador", "recepcao", "compras", "marketing", "noc", "cobranca",
    "sac", "ouvidoria", "secretaria", "operacional", "tecnico", "tecnologia", "informatica",
}
# Tokens que denunciam marca/empresa colada no "nome".
TOKENS_MARCA = {
    "telecom", "fibra", "net", "internet", "empresa", "telecomunicacoes", "wifi", "wi-fi",
    "link", "connect", "conect", "banda", "veloc", "turbo", "max", "mais", "online", "digital",
    "sistemas", "solucoes", "servicos", "ltda", "eireli",
}

CAMPOS_CRITICOS = ("nome", "empresa", "cargo", "telefone")


def _strip_acentos(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _norm(s):
    return _strip_acentos((s or "").strip().lower())


def _localpart(email):
    return (email or "").split("@")[0]


def campos_vazios(contato):
    """Lista campos críticos vazios no contato."""
    out = []
    for k in CAMPOS_CRITICOS:
        if not (contato.get(k) or "").strip():
            out.append(k)
    return out


def suspeito(nome, email, empresa=""):
    """Decide barato (sem IA) se este nome merece análise. Devolve (bool, motivo)."""
    nome = (nome or "").strip()
    empresa = (empresa or "").strip()
    if not nome:
        return True, "nome vazio"
    n = _norm(nome)
    tokens = [t for t in re.split(r"\s+", n) if t]
    local = _norm(_localpart(email))
    emp_n = _norm(empresa)
    # 1 token só e bate (quase) com a parte local do e-mail → derivado do e-mail
    derivado = (len(tokens) == 1 and local and n.replace(" ", "") == local.replace(".", "").replace("_", ""))
    if any(ch.isdigit() for ch in nome):
        return True, "nome contém número"
    if emp_n and (n == emp_n or (len(tokens) == 1 and tokens[0] in emp_n and len(tokens[0]) >= 4)):
        return True, "nome parece igual/parte da empresa"
    if len(tokens) == 1:
        t = tokens[0]
        if t in PALAVRAS_SETOR:
            return True, "palavra de setor (não-pessoa)"
        if any(mk in t for mk in TOKENS_MARCA):
            return True, "parece marca/empresa"
        if len(t) >= 13:
            return True, "token único muito longo (provável nomes colados)"
        if derivado:
            return True, "nome = parte do e-mail (token único)"
    if nome == nome.lower():
        return True, "nome todo minúsculo"
    return False, ""


def candidato(contato):
    """True se o contato precisa de análise (nome suspeito ou campos críticos vazios)."""
    ok, motivo = suspeito(contato.get("nome"), contato.get("email", ""),
                          contato.get("empresa", ""))
    if ok:
        return True, motivo
    vazios = campos_vazios(contato)
    if vazios:
        return True, "campos vazios: " + ",".join(vazios)
    return False, ""


PROMPT_REGRAS = """Você é um especialista em QUALIDADE DE DADOS de uma base de contatos B2B no Brasil. \
Muitos campos "nome" vieram poluídos da parte local do \
e-mail (ex.: "Danielpassosmartins", "iubtelecom", "comercial"). Você recebe DADOS DO CRM \
(email, nome, empresa, cargo, telefone) e, quando houver, EVIDÊNCIA PÚBLICA do site/domínio.

Para CADA contato decida uma ação e devolva SOMENTE JSON válido:
{"resultados":[{"email":"...","acao":"manter|corrigir|limpar|enriquecer",\
"nome_sugerido":"...","empresa_sugerida":"...","cargo_sugerido":"...","telefone_sugerido":"...",\
"campos_vazios":["cargo","telefone"],"confianca":0.0,"motivo":"curto",\
"fontes":[{"url":"","evidencia":""}]}]}

REGRAS:
- "corrigir": é um nome HUMANO real porém "colado"/maljeitoso → reescreva em Title Case pt-BR, \
separando nomes e sobrenomes e respeitando partículas (de, da, do, dos, e). Use o e-mail, a \
empresa e a evidência como pistas. Ex.: "Danielpassosmartins" → "Daniel Passos Martins".
- "limpar": NÃO é nome de pessoa (é setor/função como "Comercial"/"Diretoria", ou marca/empresa) \
E não há evidência pública de pessoa real. Devolva nome_sugerido vazio "".
- "enriquecer": nome ok ou corrigível, mas há campos vazios (empresa/cargo/telefone) preenchíveis \
com evidência pública. Preencha só o que a evidência confirmar.
- "manter": já é um nome de pessoa adequado e não há enriquecimento confiável. \
nome_sugerido = igual ao atual.
- NÃO invente fatos fora da evidência ou do e-mail/empresa. Telefone só se corporativo publicado.
- Se nome estiver vazio/sujo MAS a evidência mostrar pessoa clara na empresa → preferir \
corrigir/enriquecer (não limpar).
- fontes[].url só de URLs presentes na evidência.
- campos_vazios: liste os campos CRM ainda vazios após suas sugestões.
- confianca alta (≥0.8) só com evidência clara ou correção óbvia do e-mail. Na dúvida, confianca baixa.
- Responda TODOS os contatos da lista, casando por "email" exatamente."""


def _normalize_decisao(d, row):
    """Normaliza uma decisão da IA para o formato interno."""
    acao = d.get("acao") if d.get("acao") in ("manter", "corrigir", "limpar", "enriquecer") else "manter"
    sug = (d.get("nome_sugerido") or "").strip()
    emp = (d.get("empresa_sugerida") or "").strip()
    cargo = (d.get("cargo_sugerido") or "").strip()
    tel = (d.get("telefone_sugerido") or "").strip()
    if acao == "corrigir" and not sug:
        acao = "manter"
    if acao == "manter":
        sug = row.get("nome", "") or sug
    if acao == "limpar":
        sug = ""
    # enriquecer sem nenhuma sugestão útil → manter
    if acao == "enriquecer":
        nome_atual = (row.get("nome") or "").strip()
        if not sug and nome_atual:
            sug = nome_atual
        if not any([sug != nome_atual, emp, cargo, tel]):
            acao = "manter"
    try:
        conf = max(0.0, min(1.0, float(d.get("confianca", 0))))
    except Exception:
        conf = 0.0
    vazios = d.get("campos_vazios") or []
    if not isinstance(vazios, list):
        vazios = []
    vazios = [str(x) for x in vazios if str(x) in CAMPOS_CRITICOS]
    fontes = d.get("fontes") or []
    if not isinstance(fontes, list):
        fontes = []
    fontes_ok = []
    for f in fontes[:5]:
        if isinstance(f, dict) and f.get("url"):
            fontes_ok.append({"url": str(f.get("url", ""))[:300],
                              "evidencia": str(f.get("evidencia", ""))[:160]})
    return {
        "email": row["email"],
        "acao": acao,
        "nome_sugerido": sug,
        "empresa_sugerida": emp,
        "cargo_sugerido": cargo,
        "telefone_sugerido": tel,
        "campos_vazios": vazios,
        "confianca": conf,
        "motivo": (d.get("motivo") or "")[:160],
        "fontes": fontes_ok,
    }


def tem_mudanca(decisao, contato):
    """True se a decisão altera algum campo do CRM."""
    acao = decisao.get("acao")
    if acao == "limpar":
        return bool((contato.get("nome") or "").strip())
    if acao == "manter":
        # ainda pode enriquecer campos laterais se vieram preenchidos
        pass
    nome_novo = (decisao.get("nome_sugerido") or "").strip()
    nome_atual = (contato.get("nome") or "").strip()
    if acao in ("corrigir", "enriquecer") and nome_novo and nome_novo != nome_atual:
        return True
    for key, sug_key in (("empresa", "empresa_sugerida"), ("cargo", "cargo_sugerido"),
                         ("telefone", "telefone_sugerido")):
        sug = (decisao.get(sug_key) or "").strip()
        atual = (contato.get(key) or "").strip()
        if sug and sug != atual:
            return True
    return acao == "limpar"


def analisar_lote(rows, batch=25, timeout=240):
    """rows: [{email,nome,empresa,cargo,telefone,evidence_text?}] (já filtrados).
    Devolve lista de decisões com sugestões multi-campo."""
    rows = [r for r in rows if r.get("email")]
    out = []
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        lst = []
        for r in chunk:
            item = {
                "email": r["email"],
                "nome_atual": r.get("nome", ""),
                "empresa": r.get("empresa", ""),
                "cargo": r.get("cargo", ""),
                "telefone": r.get("telefone", ""),
                "campos_vazios_crm": campos_vazios(r),
            }
            ev = (r.get("evidence_text") or "").strip()
            if ev:
                item["evidencia_publica"] = ev[:2000]
            lst.append(item)
        prompt = f"{PROMPT_REGRAS}\n\nCONTATOS:\n{json.dumps(lst, ensure_ascii=False)}"
        por_email = {}
        try:
            # Preferência: Agent Engine name_analyzer; fallback Vertex generate_json.
            res = agent_engine.name_analyzer_json(prompt, timeout=timeout)
            for d in (res.get("resultados") or []):
                em = (d.get("email") or "").strip().lower()
                if em:
                    por_email[em] = d
        except Exception:
            por_email = {}   # lote inteiro cai no default 'manter' abaixo
        for r in chunk:
            em = r["email"].strip().lower()
            d = por_email.get(em, {})
            out.append(_normalize_decisao(d, r))
    return out
