#!/usr/bin/env python3
"""4unik.mail — importadores (seed + sync incremental).

Fontes opcionais: proposals/propostas_contatos.csv, propostas_sem_resposta.csv,
sent_log.txt, drafts/*.md (classificações do monitor) e Gmail via gws
(somente leitura, agendado pela fila para respeitar o guardrail de offline).
"""
import csv
import io
import json
import re
import subprocess
import os
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

import db

try:
    import openpyxl
except ImportError:  # a instalação padrão do dashboard declara openpyxl
    openpyxl = None

try:
    import xlrd
except ImportError:
    xlrd = None

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


def _gws(args, home=None):
    env = None
    if home:
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
    out = subprocess.run(["gws", "gmail"] + args, capture_output=True, text=True,
                         timeout=60, env=env)
    try:
        return json.loads(out.stdout)
    except Exception:
        return None


def import_propostas_csv():
    """Seed opcional por CSV legado. Em pacotes limpos, inicia sem contatos."""
    c = db.conn()
    sem_resposta = set()
    f_sem = db.PROPOSALS / "propostas_sem_resposta.csv"
    if f_sem.exists():
        for r in csv.DictReader(open(f_sem, encoding="utf-8-sig")):
            sem_resposta.add(r["email"].split(";")[0].strip().lower())

    n = 0
    f_main = db.PROPOSALS / "propostas_contatos.csv"
    if not f_main.exists():
        c.close()
        db.meta_set("lista_reativacao", json.dumps([]))
        return {"contatos": 0, "lista_reativacao": 0}
    for r in csv.DictReader(open(f_main, encoding="utf-8-sig")):
        email = r["email"].split(";")[0].strip().lower()
        if not EMAIL_RE.match(email):
            continue
        seg = db.segmento_por_data(r.get("primeiro_envio", ""))
        c.execute("""INSERT INTO contacts(email,nome,empresa,modulos,mensalidades,setup,validade,
                       primeiro_envio,ultimo_envio,envios,respondeu,segmento,origem,msgid,thread)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(email) DO UPDATE SET
                       nome=excluded.nome, empresa=excluded.empresa, modulos=excluded.modulos,
                       mensalidades=excluded.mensalidades, setup=excluded.setup,
                       validade=excluded.validade, primeiro_envio=excluded.primeiro_envio,
                       ultimo_envio=MAX(contacts.ultimo_envio, excluded.ultimo_envio),
                       envios=MAX(contacts.envios, excluded.envios),
                       respondeu=excluded.respondeu, segmento=excluded.segmento,
                       msgid=excluded.msgid, thread=excluded.thread""",
                  (email, r.get("nome", ""), r.get("empresa", ""), r.get("modulos", ""),
                   r.get("mensalidades", ""), r.get("setup", ""), r.get("validade", ""),
                   r.get("primeiro_envio", ""), r.get("data", ""), int(r.get("envios") or 0),
                   r.get("respondeu", "não"), seg, "propostas_csv",
                   r.get("msgid", ""), r.get("thread", "")))
        n += 1
    c.commit()
    c.close()
    db.meta_set("lista_reativacao", json.dumps(sorted(sem_resposta)))
    return {"contatos": n, "lista_reativacao": len(sem_resposta)}


def import_sent_log():
    """sent_log.txt: 'data | msgid | email | campanha-etapa | thread:xxx'."""
    f = db.PROPOSALS / "sent_log.txt"
    if not f.exists():
        return {"envios": 0}
    c = db.conn()
    n = 0
    for line in open(f, encoding="utf-8"):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        data, msgid, email, etapa = parts[0], parts[1], parts[2].lower(), parts[3]
        thread = parts[4].replace("thread:", "").strip()
        cur = c.execute("INSERT OR IGNORE INTO sends(data,msgid,email,etapa,thread) VALUES(?,?,?,?,?)",
                        (data, msgid, email, etapa, thread))
        n += cur.rowcount
        c.execute("""INSERT INTO contacts(email,origem,envios,primeiro_envio,ultimo_envio)
                     VALUES(?,?,1,?,?)
                     ON CONFLICT(email) DO UPDATE SET
                       ultimo_envio=MAX(contacts.ultimo_envio, excluded.ultimo_envio)""",
                  (email, "sent_log", data, data))
    # recontagem de envios reais por contato registrado no log
    for r in c.execute("SELECT email, COUNT(*) AS n FROM sends GROUP BY email").fetchall():
        c.execute("UPDATE contacts SET envios=MAX(envios,?) WHERE email=?", (r["n"], r["email"]))
    c.commit()
    c.close()
    return {"envios": n}


def import_drafts():
    """drafts/*.md do monitor: extrai classe/confiança das respostas escaladas."""
    n = 0
    c = db.conn()
    for f in sorted((db.PROPOSALS / "drafts").glob("*.md")):
        txt = f.read_text(encoding="utf-8", errors="replace")
        de = re.search(r"^De: (.+)$", txt, re.M)
        cl = re.search(r"^Classe: (\w+) \((\d+)%\)", txt, re.M)
        gm = re.search(r"#all/([0-9a-f]+)", txt)
        resumo = re.search(r"## Resumo\n(.+)", txt)
        if not (de and cl):
            continue
        m = re.search(r"[\w.+-]+@[\w.-]+", de.group(1))
        email = m.group(0).lower() if m else ""
        msgid = gm.group(1) if gm else f.stem
        cur = c.execute("""INSERT OR IGNORE INTO replies(msgid,email,data,classe,confianca,resumo)
                           VALUES(?,?,?,?,?,?)""",
                        (msgid, email, datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d"),
                         cl.group(1), int(cl.group(2)) / 100, (resumo.group(1).strip() if resumo else "")))
        n += cur.rowcount
        if email:
            c.execute("UPDATE contacts SET respondeu='sim', classe_monitor=? WHERE email=?",
                      (cl.group(1), email))
    c.commit()
    c.close()
    return {"respostas": n}


def _thread_identity(thread_id):
    """Remetente/caixa e IDs enviados conhecidos para uma thread de campanha."""
    c = db.conn()
    row = c.execute(
        """SELECT sd.email AS sender_email, sd.slug
             FROM sends s LEFT JOIN campaigns cp ON cp.id=s.campaign_id
             LEFT JOIN senders sd ON sd.id=cp.sender_id
            WHERE s.thread=? ORDER BY s.id DESC LIMIT 1""",
        (thread_id,),
    ).fetchone()
    ids = {r["msgid"] for r in c.execute(
        "SELECT msgid FROM sends WHERE thread=? AND msgid!=''", (thread_id,)
    ).fetchall()}
    emails = {r["email"].lower() for r in c.execute(
        "SELECT email FROM senders WHERE email!=''"
    ).fetchall()}
    c.close()
    sender_email = (row["sender_email"] if row else "") or ""
    slug = (row["slug"] if row else "") or ""
    home = (db.GWS_DIR.parent / "gws_accounts" / slug) if slug else None
    return sender_email.lower(), home, ids, emails


def check_thread_via_gws(thread_id, email):
    """Detecta somente respostas posteriores ao último envio da nossa caixa.

    Mensagens com label SENT, Gmail id já gravado em sends ou endereço de um
    remetente cadastrado são nossas. Se a thread não contiver nenhum envio
    reconhecível, a verificação falha fechada (None) em vez de pausar o lead.
    """
    sender_email, home, our_ids, sender_emails = _thread_identity(thread_id)
    t = _gws(["users", "threads", "get", "--params",
              json.dumps({"userId": "me", "id": thread_id, "format": "metadata"})],
             home=home)
    if not t:
        return None  # falha de rede/thread — quem chamou decide (fila faz retry)
    msgs = t.get("messages", [])

    def nossa(m):
        frm = next((h["value"] for h in m.get("payload", {}).get("headers", [])
                    if h["name"].lower() == "from"), "")
        addr = parseaddr(frm)[1].strip().lower()
        return (m.get("id") in our_ids or "SENT" in set(m.get("labelIds") or [])
                or bool(addr and addr in (sender_emails | {sender_email})))

    nossos = [m for m in msgs if nossa(m)]
    if not nossos:
        return None
    nosso_dt = max(int(m.get("internalDate", "0") or 0) for m in nossos)
    externas = 0
    for m in msgs:
        frm = next((h["value"] for h in m.get("payload", {}).get("headers", [])
                    if h["name"].lower() == "from"), "")
        if (frm and not nossa(m)
                and int(m.get("internalDate", "0") or 0) > nosso_dt):
            externas += 1
    if externas:
        c = db.conn()
        c.execute("UPDATE contacts SET respondeu='sim' WHERE email=?", (email,))
        # resposta = pausa imediata da sequência desse contato em todas as campanhas
        c.execute("""UPDATE campaign_contacts SET status='pausado_resposta'
                     WHERE email=? AND status IN ('pendente','ativo')""", (email,))
        c.commit()
        c.close()
    return externas


def _header(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _value(row, *names):
    for name in names:
        value = row.get(_header(name), "")
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _rows_from_upload(content: bytes, filename: str):
    """Lê CSV/XLSX/XLS e retorna dicionários com cabeçalhos normalizados."""
    suffix = Path(filename or "upload.csv").suffix.lower()
    if suffix == ".csv":
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="replace")))
        return [{_header(k): (v or "").strip() for k, v in row.items()} for row in reader]
    if suffix == ".xlsx":
        if not openpyxl:
            raise ValueError("Leitor XLSX indisponível no servidor")
        sheet = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True).active
        values = sheet.iter_rows(values_only=True)
        first = next(values, [])
        # Bases legadas frequentemente chegam como uma única coluna de e-mails,
        # sem linha de cabeçalho. Nesse caso a primeira célula também é um lead;
        # tratá-la como ``email`` evita perder o primeiro registro.
        first_email = str(first[0] or "").strip().lower() if len(first) == 1 else ""
        if first_email and EMAIL_RE.match(first_email):
            rows = [{"email": first_email}]
            rows.extend({"email": str(line[0] or "").strip().lower()}
                        for line in values if line)
            return rows
        headers = [_header(v) for v in first]
        return [{headers[i]: "" if v is None else str(v).strip()
                 for i, v in enumerate(line) if i < len(headers) and headers[i]}
                for line in values]
    if suffix == ".xls":
        if not xlrd:
            raise ValueError("Leitor XLS indisponível no servidor")
        book = xlrd.open_workbook(file_contents=content)
        sheet = book.sheet_by_index(0)
        headers = [_header(v) for v in sheet.row_values(0)]
        return [{headers[i]: str(v).strip() for i, v in enumerate(sheet.row_values(n)) if headers[i]}
                for n in range(1, sheet.nrows)]
    raise ValueError("Formato não suportado: use CSV, XLSX ou XLS")


def _source_tags(filename, row):
    name = (filename or "").lower()
    tags = []
    if "mailing_base_inativa" in name or "mailing base inativa" in name:
        # A origem fica explícita para filtros e relatórios do CRM. A decisão
        # de incluir alguém em uma audiência continua sendo manual.
        tags += ["origem-base-inativa"]
    elif "rd summit" in name:
        tags += ["origem-rd-summit"]
    elif "demo realizada" in name:
        tags += ["origem-demo-realizada", "fez-demo"]
    elif "leads demo" in name:
        tags += ["origem-calendly", "pediu-agendamento"]
    else:
        tags += ["origem-upload"]
    estado = _value(row, "estado")
    setor = _value(row, "setor")
    if estado:
        tags.append("estado-" + db.tag_norm(estado))
    if setor:
        tags.append("setor-" + db.tag_norm(setor))
    # cancelamentos e ausência de permissão continuam no CRM, mas nunca em audiência.
    accepted = _value(row, "invitee accepted marketing emails", "accepts email marketing", "aceita marketing")
    canceled = _value(row, "canceled")
    if accepted.lower() in ("false", "não", "nao", "no", "0") or canceled.lower() in ("true", "sim", "yes", "1"):
        tags.append("nao-contatar")
    return tags


def import_contacts_upload(content: bytes, filename="upload.csv"):
    """Importa CSV/Excel de forma idempotente, incluindo dados comerciais do CRM.

    Telefone fica restrito ao CRM e não é enviado aos prompts da IA. Listas técnicas
    address/result/risk geram supressão; nunca audiência comercial.
    """
    try:
        rows = _rows_from_upload(content, filename)
    except Exception as exc:
        return {"erro": f"Não foi possível ler {filename}: {exc}"}
    headers = set().union(*(r.keys() for r in rows)) if rows else set()
    technical = "address" in headers and ("result" in headers or "risk" in headers)
    novos, atualizados, ignorados, duplicados = 0, 0, [], 0
    vistos = set()
    c = db.conn()
    # Higieniza a base inteira antes de escrever: a consulta única evita uma
    # busca por linha e torna importações grandes previsíveis e atômicas.
    existentes = {r["email"]: r["tags"] for r in c.execute("SELECT email,tags FROM contacts")}
    for r in rows:
        email = _value(r, "email", "invitee email", "address").lower()
        if not EMAIL_RE.match(email):
            ignorados.append(email or "(vazio)")
            continue
        if email in vistos:
            duplicados += 1
            continue
        vistos.add(email)
        existe = email in existentes
        if technical:
            tags = ["origem-lista-tecnica", "nao-contatar", "email-invalido"]
            nome = empresa = cargo = porte = estado = telefone = erp = etapa = qualificacao = motivo_perda = enriquecimento = ""
            origem = "lista-tecnica"
        else:
            tags = _source_tags(filename, r) + (_value(r, "tags").split(",") if _value(r, "tags") else [])
            nome = _value(r, "nome", "invitee name", "contato")
            if not nome:
                nome = " ".join(x for x in (_value(r, "first name", "first_name"),
                                             _value(r, "last name", "last_name")) if x).strip()
            empresa = _value(r, "empresa", "cliente", "company")
            cargo = _value(r, "cargo", "role", "job title")
            porte = _value(r, "número de funcionários", "numero de funcionarios", "porte", "porte assinantes")
            estado = _value(r, "estado")
            telefone = _value(r, "telefone", "phone", "celular", "whatsapp", "mobile")
            erp = _value(r, "erp", "sistema", "sistema erp", "stack")
            etapa = _value(r, "etapa funil", "etapa do funil", "stage")
            qualificacao = _value(r, "qualificação", "qualificacao", "perfil", "lead score")
            motivo_perda = _value(r, "motivo de perda", "motivo perda", "lost reason")
            enriquecimento = _value(r, "enriquecimento", "observações", "observacoes", "notas", "histórico", "historico")
            if not enriquecimento:
                extras = []
                total_pedidos = _value(r, "total orders", "total de pedidos")
                total_gasto = _value(r, "total spent", "total gasto")
                ultimo_acesso = _value(r, "ultimo_acesso", "último acesso")
                if total_pedidos: extras.append(f"Pedidos: {total_pedidos}")
                if total_gasto: extras.append(f"Valor acumulado: {total_gasto}")
                if ultimo_acesso: extras.append(f"Último acesso: {ultimo_acesso}")
                enriquecimento = " · ".join(extras)
            origem = tags[0].replace("origem-", "") if tags else "upload"
        segmento = ""
        previous_tags = existentes.get(email, "")
        merged = db.tags_str(db.tags_list(previous_tags) + tags)
        c.execute("""INSERT INTO contacts(email,nome,empresa,origem,tags,segmento,cargo,porte_funcionarios,estado_negocio,
                     telefone,erp,etapa_funil,qualificacao,motivo_perda,enriquecimento)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(email) DO UPDATE SET
                       nome=CASE WHEN excluded.nome!='' THEN excluded.nome ELSE contacts.nome END,
                       empresa=CASE WHEN excluded.empresa!='' THEN excluded.empresa ELSE contacts.empresa END,
                       origem=CASE WHEN excluded.origem!='' THEN excluded.origem ELSE contacts.origem END,
                       tags=excluded.tags,
                       segmento=CASE WHEN contacts.segmento='' AND excluded.segmento!='' THEN excluded.segmento ELSE contacts.segmento END,
                       cargo=CASE WHEN excluded.cargo!='' THEN excluded.cargo ELSE contacts.cargo END,
                       porte_funcionarios=CASE WHEN excluded.porte_funcionarios!='' THEN excluded.porte_funcionarios ELSE contacts.porte_funcionarios END,
                       estado_negocio=CASE WHEN excluded.estado_negocio!='' THEN excluded.estado_negocio ELSE contacts.estado_negocio END,
                       telefone=CASE WHEN excluded.telefone!='' THEN excluded.telefone ELSE contacts.telefone END,
                       erp=CASE WHEN excluded.erp!='' THEN excluded.erp ELSE contacts.erp END,
                       etapa_funil=CASE WHEN excluded.etapa_funil!='' THEN excluded.etapa_funil ELSE contacts.etapa_funil END,
                       qualificacao=CASE WHEN excluded.qualificacao!='' THEN excluded.qualificacao ELSE contacts.qualificacao END,
                       motivo_perda=CASE WHEN excluded.motivo_perda!='' THEN excluded.motivo_perda ELSE contacts.motivo_perda END,
                       enriquecimento=CASE WHEN excluded.enriquecimento!='' THEN excluded.enriquecimento ELSE contacts.enriquecimento END""",
                  (email, nome, empresa, origem, merged, segmento, cargo, porte, estado, telefone, erp, etapa,
                   qualificacao, motivo_perda, enriquecimento))
        if existe:
            atualizados += 1
        else:
            novos += 1
        existentes[email] = merged
    c.commit()
    c.close()
    return {"arquivo": filename, "novos": novos, "atualizados": atualizados,
            "ignorados": ignorados, "duplicados": duplicados,
            "tipo": "supressão" if technical else "contatos",
            "tratamento": "duplicatas removidas; origem marcada como base inativa"}


# Compatibilidade com integrações existentes.
def import_csv_upload(content: bytes):
    return import_contacts_upload(content, "upload.csv")


def sync_all():
    """Sync local completo (CSV + log + drafts). Checagens de Gmail vão para a fila."""
    res = {}
    res.update(import_propostas_csv())
    res.update(import_sent_log())
    res.update(import_drafts())
    db.meta_set("last_sync", db.now())
    return res


if __name__ == "__main__":
    db.init()
    print(json.dumps(sync_all(), ensure_ascii=False, indent=2))
