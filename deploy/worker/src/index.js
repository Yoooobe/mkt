import { Container, getContainer } from "@cloudflare/containers";

/**
 * Container único do dashboard 4unik.mail.
 * sleepAfter alto: o app só liga loops de envio com campanha ativa;
 * mesmo assim mantemos a VM acordada o suficiente para UI/CRM.
 */
export class DashboardContainer extends Container {
  defaultPort = 8000;
  sleepAfter = "30m";
}

export default {
  async fetch(request, env) {
    // Uma única instância nomeada — SQLite/Litestream não aceita multi-writer.
    const stub = getContainer(env.DASHBOARD, "4unik-mail");
    return stub.fetch(request);
  },
};
