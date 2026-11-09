# Prompt inicial para o Codex

Cole este prompt no Codex depois de extrair o zip e abrir a pasta `gws-4unik`:

```text
Você está dentro da pasta gws-4unik no meu Mac. Quero instalar e subir o Yoobe.mail localmente.

Faça nesta ordem:
1. Leia SETUP-GUIDE-GENAU.md e CLAUDE.md.
2. Crie o ambiente Python local com:
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r dashboard/requirements.txt
3. Copie os exemplos de configuração:
   cp dashboard/.env.example dashboard/.env
   cp zapi-config.env.example zapi-config.env
   cp proposals/monitor_config.env.example proposals/monitor_config.env
4. Gere um API_KEY novo em dashboard/.env usando openssl rand -hex 16.
5. Não configure tracking nem Z-API por enquanto, deixe vazios se necessário.
6. Suba o dashboard com ./start-dashboard.sh.
7. Abra ou me informe a URL http://localhost:8377.
8. Use o CSV samples/contatos-exemplo-4unik.csv para importar contatos de teste no CRM.
9. Confirme que existe uma campanha exemplo da Yoobe em rascunho e que os templates estão prontos para revisão.
10. Não envie nenhum email real. Se for preparar um teste, use somente contatos com tag teste e me peça confirmação antes de ativar qualquer campanha.

Depois disso, me diga exatamente:
- se o dashboard subiu;
- qual API_KEY ficou em dashboard/.env;
- se a planilha de exemplo está pronta para importar;
- quais passos faltam para autenticar o Gmail via gws.
```
