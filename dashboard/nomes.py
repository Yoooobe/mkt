#!/usr/bin/env python3
"""4unik.mail — inteligência de qualidade de nomes (a IA entende; o código decide).

Muitos contatos vieram com `nome` derivado da parte local do e-mail ("Danielpassosmartins",
"Iubtelecom", "Comercial"). Este módulo separa o joio do trigo em 2 passos:

  1. suspeito()      — pré-filtro barato (sem IA) que sinaliza candidatos a correção.
  2. analisar_lote() — manda só os suspeitos para a IA, que devolve, por contato:
                       acao ∈ {manter, corrigir, limpar}, nome_sugerido, confianca, motivo.

É PURO (não toca no banco): reaproveitado pela limpeza em massa e por importações futuras.
"""
import json
import re
import unicodedata

import ai

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


def _strip_acentos(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _norm(s):
    return _strip_acentos((s or "").strip().lower())


def _localpart(email):
    return (email or "").split("@")[0]


def suspeito(nome, email, empresa=""):
    """Decide barato (sem IA) se este nome merece análise. Devolve (bool, motivo)."""
    nome = (nome or "").strip()
    if not nome:
        return True, "nome vazio"
    n = _norm(nome)
    tokens = [t for t in re.split(r"\s+", n) if t]
    local = _norm(_localpart(email))
    # 1 token só e bate (quase) com a parte local do e-mail → derivado do e-mail
    derivado = (len(tokens) == 1 and local and n.replace(" ", "") == local.replace(".", "").replace("_", ""))
    if any(ch.isdigit() for ch in nome):
        return True, "nome contém número"
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


PROMPT_REGRAS = """Você é um especialista em QUALIDADE DE DADOS de uma base de contatos B2B no Brasil. \
Muitos campos "nome" vieram poluídos da parte local do \
e-mail (ex.: "Danielpassosmartins", "iubtelecom", "comercial").

Para CADA contato (email, nome_atual, empresa) decida uma ação e devolva SOMENTE JSON válido:
{"resultados":[{"email":"...","acao":"manter|corrigir|limpar","nome_sugerido":"...","confianca":0.0,"motivo":"curto"}]}

REGRAS:
- "corrigir": é um nome HUMANO real porém "colado"/maljeitoso → reescreva em Title Case pt-BR, \
separando nomes e sobrenomes e respeitando partículas (de, da, do, dos, e). Use o e-mail e a \
empresa como pistas. Ex.: "Danielpassosmartins" → "Daniel Passos Martins"; "carloshenrique" → \
"Carlos Henrique"; "maxwilliamdelima" → "Max William de Lima".
- "limpar": NÃO é nome de pessoa (é setor/função como "Comercial"/"Diretoria", ou marca/empresa/sigla \
como "Iubtelecom"/"Viptelecompc"/"Ultrafibramais"). Devolva nome_sugerido vazio "".
- "manter": já é um nome de pessoa adequado (ex.: "Leandro Amaral", "José Geraldo", "Jonas"). \
nome_sugerido = igual ao atual.
- Não invente sobrenome que não esteja no nome/e-mail. Na dúvida entre corrigir e manter, prefira \
"manter" com confianca baixa. confianca alta só quando tiver certeza.
- Responda TODOS os contatos da lista, casando por "email" exatamente."""


def analisar_lote(rows, batch=25, timeout=240):
    """rows: [{email,nome,empresa}] (já filtrados por suspeito()). Devolve lista de decisões
    {email, acao, nome_sugerido, confianca, motivo}. Robusto: default 'manter' se a IA falhar/omitir."""
    rows = [r for r in rows if r.get("email")]
    out = []
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        lst = [{"email": r["email"], "nome_atual": r.get("nome", ""), "empresa": r.get("empresa", "")}
               for r in chunk]
        prompt = f"{PROMPT_REGRAS}\n\nCONTATOS:\n{json.dumps(lst, ensure_ascii=False)}"
        por_email = {}
        try:
            res = ai._claude_json(prompt, timeout=timeout)
            for d in (res.get("resultados") or []):
                em = (d.get("email") or "").strip().lower()
                if em:
                    por_email[em] = d
        except Exception:
            por_email = {}   # lote inteiro cai no default 'manter' abaixo
        for r in chunk:
            em = r["email"].strip().lower()
            d = por_email.get(em, {})
            acao = d.get("acao") if d.get("acao") in ("manter", "corrigir", "limpar") else "manter"
            sug = (d.get("nome_sugerido") or "").strip()
            if acao == "corrigir" and not sug:
                acao = "manter"
            if acao == "manter":
                sug = r.get("nome", "")
            if acao == "limpar":
                sug = ""
            try:
                conf = max(0.0, min(1.0, float(d.get("confianca", 0))))
            except Exception:
                conf = 0.0
            out.append({"email": r["email"], "acao": acao, "nome_sugerido": sug,
                        "confianca": conf, "motivo": (d.get("motivo") or "")[:120]})
    return out
