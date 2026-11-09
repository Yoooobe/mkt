#!/usr/bin/env python3
"""Templates de email da Yoobe — identidade visual + biblioteca inicial."""

ASSINATURA = ""
WHATSAPP_NUMBER = "+55 41 98760-7512"
COMMERCIAL_EMAIL = "corporate@yoobe.co"
WA = "https://wa.me/5541987607512?text=Ol%C3%A1!%20Quero%20conhecer%20a%20Yoobe"
MAIL = f"mailto:{COMMERCIAL_EMAIL}?subject=Contato%20comercial%20via%20Yoobe"
DEMO = "https://corporate.yoobe.co/"
SITE = "https://corporate.yoobe.co"


LOGO_URL = "https://corporate.yoobe.co/brand/yoobe-logo-lockup.webp"
TEAL = "#62d8be"


def shell(preheader, inner, cta_label, cta_url=DEMO, assinatura=None, unsubscribe_url=""):
    """Moldura Gmail-safe para Yoobe.mail."""
    assinatura = assinatura if assinatura is not None else ASSINATURA
    sig = (f'<img src="{assinatura}" width="320" alt="Yoobe" style="display:block;border-radius:8px;max-width:320px">'
           if assinatura else
           '<p style="margin:0;font-size:13px;color:#dce7ef"><b>Time Yoobe</b><br>Infraestrutura de recompensas corporativas</p>')
    unsub = (f'<p style="margin:14px 0 0;font-size:11px;color:#7f8c96;line-height:1.5">'
             f'Voce recebe os e-mails da Yoobe porque esta na nossa base de relacionamento. '
             f'<a href="{unsubscribe_url}" style="color:#7f8c96;text-decoration:underline">Descadastrar</a>.</p>'
             ) if unsubscribe_url else ""
    contact = (f'<p style="margin:14px 0 0;font-size:12px;color:#aebdca;line-height:1.7">'
               f'Fale com a Yoobe: <a href="{WA}" style="color:{TEAL};text-decoration:underline">'
               f'WhatsApp {WHATSAPP_NUMBER}</a> &nbsp;·&nbsp; '
               f'<a href="{MAIL}" style="color:{TEAL};text-decoration:underline">{COMMERCIAL_EMAIL}</a></p>')
    platform = (f'<p style="margin:16px 0 0;font-size:13px;line-height:1.6">'
                f'<a href="{SITE}" style="color:{TEAL};font-weight:700;text-decoration:underline">'
                f'Conheça a plataforma Yoobe</a>'
                f'<span style="color:#7f8c96"> &nbsp;·&nbsp; corporate.yoobe.co</span> · '
                f'<a href="https://catalogo.yoobe.co/" style="color:{TEAL};text-decoration:underline">Catálogo</a></p>')
    # cta_label vazio = sem botão da moldura (o miolo já traz o próprio CTA;
    # evita dois botões iguais no mesmo e-mail).
    btn = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:28px auto 8px"><tr>'
           f'<td style="border-radius:10px;background-color:{TEAL}">'
           f'<a href="{cta_url}" style="display:inline-block;padding:14px 34px;font-family:Montserrat,Arial,sans-serif;'
           f'font-size:15px;font-weight:700;color:#0f1720;text-decoration:none">{cta_label}</a>'
           f'</td></tr></table>') if (cta_label or "").strip() else ""
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background-color:#eef2f4">
<div style="display:none;max-height:0;overflow:hidden">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#eef2f4">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%">
<tr><td style="background-color:#171f26;background-image:linear-gradient(160deg,#1f2933,#171f26);border-radius:14px 14px 0 0;padding:28px 36px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td style="vertical-align:middle">
      <img src="{LOGO_URL}" width="140" height="36" alt="Yoobe" style="display:block;border:0;outline:none;height:36px;width:auto;max-width:160px">
    </td>
    <td align="right" style="vertical-align:middle;font-family:'DM Sans',Arial,sans-serif;font-size:11px;letter-spacing:1.4px;color:#aebdca">REWARD INFRASTRUCTURE</td>
  </tr></table>
</td></tr>
<tr><td style="background-color:#ffffff;padding:36px;font-family:'DM Sans',Arial,sans-serif;font-size:15px;color:#18242f;line-height:1.6">
{inner}
{btn}
</td></tr>
<tr><td style="background-color:#101820;border-radius:0 0 14px 14px;padding:24px 36px;font-family:'DM Sans',Arial,sans-serif">
<p style="margin:0 0 14px;font-size:12px;color:#aebdca">Gamificacao, carteiras, loja corporativa, API e fulfillment em uma unica stack.</p>
{sig}
{contact}
{platform}
{unsub}
</td></tr>
</table></td></tr></table></body></html>"""


def card(titulo, linhas, cor="#66e3c4"):
    lis = "".join(f'<li style="margin-bottom:6px">{x}</li>' for x in linhas)
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0"><tr>
<td style="border-left:4px solid {cor};background-color:#f6f8f9;border-radius:0 10px 10px 0;padding:16px 20px">
<p style="margin:0 0 8px;font-family:Montserrat,Arial,sans-serif;font-size:14px;font-weight:800;color:#0f1720">{titulo}</p>
<ul style="margin:0;padding-left:18px;font-size:14px;color:#18242f">{lis}</ul>
</td></tr></table>"""


def stat(numero, rotulo):
    return f"""<td align="center" style="padding:12px 6px;background-color:#0f1720;border-radius:10px">
<span style="font-family:Montserrat,Arial,sans-serif;font-size:22px;font-weight:800;color:#66e3c4">{numero}</span><br>
<span style="font-family:'DM Sans',Arial,sans-serif;font-size:11px;color:#aebdca">{rotulo}</span></td>"""


H = lambda t: f'<h1 style="margin:0 0 18px;font-family:Montserrat,Arial,sans-serif;font-size:22px;font-weight:800;color:#0f1720;line-height:1.3">{t}</h1>'
P = lambda t: f'<p style="margin:0 0 16px">{t}</p>'

reward_infra_1 = (
  H('Recompensa corporativa nao precisa virar operacao manual.')
  + P("O programa nasce com uma boa intencao: reconhecer, engajar, premiar. Mas logo aparecem compras, estoque, frete, planilhas e pedidos soltos no RH.")
  + card("Onde a operacao trava", [
      "Premios chegam tarde e o efeito do reconhecimento esfria.",
      "O RH vira compras, suporte e transportadora.",
      "Sem dashboard, ROI e taxa de resgate ficam invisiveis para a lideranca."], "#ef7d7d")
  + P("A Yoobe resolve isso como infraestrutura: motor de pontos e campanhas, loja corporativa, carteiras, catalogo e fulfillment conectados em uma unica plataforma.")
)

reward_infra_2 = (
  H('Da meta batida ao premio entregue: tudo rastreavel.')
  + P("A Yoobe conecta eventos do seu stack a recompensas reais. Pode ser uma meta de vendas, uma conclusao de curso, um marco de onboarding ou uma campanha interna.")
  + card("A stack Yoobe", [
      "API REST, SDKs e webhooks para integrar HRIS, CRM, intranet ou SaaS.",
      "Carteiras, saldo, extrato e resgates auditaveis.",
      "Loja VIP com produtos fisicos, digitais, kits e checkout autonomo."], "#66e3c4")
  + P("O colaborador escolhe, resgata e acompanha. O gestor governa budget, centros de custo, SLA e relatorios.")
)

reward_infra_3 = (
  H('Quer provar ROI do programa de reconhecimento?')
  + P("Campanha de engajamento sem dados vira custo fixo. A Yoobe foi desenhada para medir o ciclo completo: pontos distribuidos, taxa de resgate, eNPS, budget e entrega.")
  + '<table role="presentation" width="100%" cellpadding="0" cellspacing="6"><tr>'
  + stat("24h", "setup basico/API") + stat("48h", "entrega rastreavel no Brasil") + stat("ROI", "dashboard em tempo real")
  + "</tr></table>"
  + P("Se fizer sentido, marcamos uma demo curta para mapear um primeiro caso: onboarding, vendas, treinamento, eventos ou programa de reconhecimento.")
)

saas_api = (
  H('Recompensas fisicas como uma chamada de API.')
  + P("Se voce tem uma plataforma de employee experience, educacao, vendas ou comunidade, a Yoobe entra como camada de execucao: seu produto mantem a experiencia; a Yoobe cuida de catalogo, checkout, estoque, envio e rastreio.")
  + card("Integre sem virar operacao logistica", [
      "Endpoints para produtos, estoque, checkout e historico de resgates.",
      "Sandbox, SDKs Node.js/Python e webhooks de tracking.",
      "Fulfillment nacional e operacao internacional conforme o programa."], "#66e3c4")
)

EMAILS = [
  dict(id="4unik-infra-1", grupo="Yoobe · Reward Infrastructure", dia="Email 1", cadencia="D+0 · dor",
       assunto="Seu programa de reconhecimento virou operacao manual?",
       preheader="A Yoobe conecta gamificacao, loja, carteiras e fulfillment em uma unica stack.",
       cta="Agendar demo da Yoobe", cta_url=DEMO, inner=reward_infra_1),
  dict(id="4unik-infra-2", grupo="Yoobe · Reward Infrastructure", dia="Email 2", cadencia="D+4 · solucao",
       assunto="Da meta batida ao premio entregue, sem planilha no meio",
       preheader="API, carteiras, loja VIP e entrega rastreavel para programas corporativos.",
       cta="Ver como funciona", cta_url=SITE, inner=reward_infra_2),
  dict(id="4unik-infra-3", grupo="Yoobe · Reward Infrastructure", dia="Email 3", cadencia="D+10 · ROI",
       assunto="Como medir ROI de campanhas de reconhecimento",
       preheader="Budget, taxa de resgate, eNPS e SLA em tempo real no painel da Yoobe.",
       cta="Conversar com especialista", cta_url=DEMO, inner=reward_infra_3),
  dict(id="4unik-saas-api", grupo="Yoobe · SaaS e API", dia="Modelo API", cadencia="template",
       assunto="Recompensas fisicas como uma chamada de API",
       preheader="Sua plataforma mantem a experiencia; a Yoobe opera catalogo, checkout e entrega.",
       cta="Conhecer API e integrações", cta_url="https://corporate.yoobe.co/", inner=saas_api),
]
