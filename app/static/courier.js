/* Aplicativo de rua: check-in, entrega, check-out.
 *
 * Tudo o que a tela precisa vem de `/api/field/state`, numa chamada só. O
 * painel da loja pode pagar trinta e quatro requisições porque roda num tablet
 * no balcão; aqui a rede é a do bairro, com a moto parada no sinal, e a
 * segunda requisição é a que não volta.
 *
 * Depois de qualquer ação o estado inteiro é relido em vez de remendado na
 * mão. É mais tráfego e é o certo: o mesmo turno pode estar aberto no celular
 * e no tablet do balcão ao mesmo tempo, e remendar localmente faria as duas
 * telas divergirem justo quando alguém precisa fechar o turno pela outra.
 */

const estado = { data: null, escolhido: null, enviando: false, carregando: false, courierId: null };

const moeda = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });

document.addEventListener("DOMContentLoaded", async () => {
  ligar();
  await window.erpAuth.ready;
  const sair = document.getElementById("field-signout");
  sair.hidden = !window.erpAuth.canSignOut;
  sair.addEventListener("click", () => window.erpAuth.signOut());
  await carregar();
  // Pedido novo da Anota Aí não deve depender de alguém tocar em atualizar no
  // painel da loja. Só atualiza com a tela visível para não gastar a rede do
  // entregador enquanto o celular está no bolso.
  window.setInterval(() => {
    if (document.visibilityState === "visible" && !estado.enviando) carregar();
  }, 20000);
});

function ligar() {
  // O check-in identifica o entregador pelo corpo, e nao pela URL: ele e a
  // unica chamada que acontece antes de existir turno, entao e nela que o
  // modo demonstrativo precisa dizer de quem e o turno que esta nascendo.
  document.getElementById("field-checkin").addEventListener("click", async () => {
    await acao("/api/field/check-in", { courier_id: courierIdAtual() }, "Check-in feito. Bom turno.");
  });
  document.getElementById("field-checkout").addEventListener("click", encerrar);
  document.getElementById("field-refresh").addEventListener("click", () => carregar());
  document.getElementById("field-manual").addEventListener("click", () => abrirEntrega(null));
  // Lançar o dia esquecido está nos dois estados: fora de turno é justamente
  // quando alguém lembra do que não registrou ontem.
  ["field-late", "field-late-open"].forEach((id) => {
    document.getElementById(id).addEventListener("click", () => abrirEntrega(null, { atrasada: true }));
  });
  document.getElementById("field-history-toggle").addEventListener("click", alternarHistorico);
  document.getElementById("run-zone").addEventListener("change", (evento) => {
    const nova = evento.target.value === "__nova__";
    document.getElementById("run-new-zone-field").hidden = !nova;
    if (nova) document.getElementById("run-new-zone").focus();
  });
  document.getElementById("field-pick-list").addEventListener("click", (evento) => {
    const escolhido = evento.target.closest("[data-pick]");
    if (!escolhido) return;
    // Guardado no aparelho: o balcão fecha o turno de um, atende, e volta —
    // reescolher a cada recarga tornaria a tela inútil justamente na hora
    // corrida em que ela é usada.
    try { localStorage.setItem("sdm-courier", escolhido.dataset.pick); } catch (_) { /* aparelho sem armazenamento */ }
    estado.courierId = escolhido.dataset.pick;
    carregar();
  });
  document.getElementById("field-queue").addEventListener("click", (evento) => {
    const cartao = evento.target.closest("[data-order]");
    if (cartao) abrirEntrega(cartao.dataset.order);
  });
  document.getElementById("field-route").addEventListener("click", (evento) => {
    const chegou = evento.target.closest("[data-finish]");
    if (chegou) return finalizar(chegou.dataset.finish, false);
    const falhou = evento.target.closest("[data-failed]");
    if (falhou) return finalizar(falhou.dataset.failed, true);
  });
  document.getElementById("run-form").addEventListener("submit", enviarEntrega);
  document.getElementById("run-add-item").addEventListener("click", () => adicionarItem());
  document.querySelectorAll("[data-close-run]").forEach((botao) => botao.addEventListener("click", fecharEntrega));
  document.getElementById("run-modal").addEventListener("cancel", fecharEntrega);
}

// ------------------------------------------------------------------ estado

async function carregar() {
  // O intervalo, o botão Atualizar e o retorno de uma ação podem coincidir em
  // rede lenta. Uma única leitura em voo evita gasto duplicado e impede uma
  // resposta antiga de redesenhar a tela depois de uma nova.
  if (estado.carregando) return;
  estado.carregando = true;
  try {
    estado.data = await api(`/api/field/state${chave()}`);
    render();
  } catch (erro) {
    mostrar(erro.message || "Não foi possível carregar o turno.");
  } finally {
    estado.carregando = false;
  }
}

function render() {
  const dados = estado.data;
  if (!dados) return;
  document.getElementById("field").hidden = false;

  // A loja abriu a tela de rua e ainda não disse por quem vai lançar. A
  // resposta é uma lista, não uma recusa: ela entra aqui para fechar o turno
  // de quem ficou sem bateria, e precisa poder dizer de quem é o turno.
  const escolher = document.getElementById("field-pick");
  if (dados.needs_courier) {
    escolher.hidden = false;
    document.getElementById("field-closed").hidden = true;
    document.getElementById("field-open").hidden = true;
    document.getElementById("field-history-block").hidden = true;
    document.getElementById("field-name").textContent = "Escolha o entregador";
    document.getElementById("field-pick-list").innerHTML = dados.couriers.length
      ? dados.couriers.map((linha) => `<button class="field-action ghost" type="button" data-pick="${escapar(linha.id)}"><i data-lucide="user"></i><span>${escapar(linha.name)}${linha.vehicle ? ` · ${escapar(linha.vehicle)}` : ""}</span></button>`).join("")
      : '<p class="field-empty">Nenhum entregador cadastrado. Cadastre em Entregas, no painel da loja.</p>';
    return refreshIcons();
  }
  escolher.hidden = true;
  document.getElementById("field-history-block").hidden = false;
  document.getElementById("field-name").textContent = dados.courier.name;

  const emTurno = Boolean(dados.shift);
  document.getElementById("field-closed").hidden = emTurno;
  document.getElementById("field-open").hidden = !emTurno;
  if (!emTurno) return refreshIcons();

  const totais = dados.totals;
  texto("#field-since", duracao(dados.shift.minutes));
  texto("#field-count", String(totais.deliveries));
  texto("#field-freight", moeda.format(totais.freight));
  texto("#field-median", totais.minutes_median === null ? "--" : `${totais.minutes_median} min`);

  renderRota(dados.on_route);
  renderFila(dados.queue);
  renderEntregues(dados.runs.filter((linha) => !linha.open));
  refreshIcons();
}

function renderRota(rota) {
  document.getElementById("field-route-block").hidden = !rota.length;
  document.getElementById("field-route").innerHTML = rota.map((linha) => `
    <article class="field-run">
      <div class="field-run-top">
        <strong>${escapar(linha.zone_name)}</strong>
        <span class="field-minutes">${linha.minutes} min</span>
      </div>
      <p class="field-run-note">${escapar(resumoItens(linha))}${linha.customer_name ? ` · ${escapar(linha.customer_name)}` : ""}</p>
      ${linha.address ? `<p class="field-run-note destino">${escapar(linha.address)}</p>` : ""}
      ${linha.reference ? `<p class="field-run-note">${escapar(linha.reference)}</p>` : ""}
      ${botaoDoMapa(linha.address, linha.zone_name)}
      <div class="field-run-actions">
        <button class="field-action primary" type="button" data-finish="${linha.id}"><i data-lucide="check"></i><span>Cheguei</span></button>
        <button class="field-action ghost" type="button" data-failed="${linha.id}"><i data-lucide="undo-2"></i><span>Não entreguei</span></button>
      </div>
    </article>`).join("");
}

function renderFila(fila) {
  const pedidos = fila.orders || [];
  document.getElementById("field-queue").innerHTML = pedidos.length
    ? pedidos.map((linha) => `
      <button class="field-order" type="button" data-order="${linha.order_id}">
        <div class="field-order-top">
          <strong>${linha.number ? `#${escapar(String(linha.number))}` : "Pedido"}</strong>
          <span class="field-minutes ${linha.minutes_waiting >= 20 ? "late" : ""}">${linha.minutes_waiting} min</span>
        </div>
        <p>${escapar(resumoItens(linha))}</p>
        <p class="field-run-note">${linha.customer_name ? `${escapar(linha.customer_name)} · ` : ""}${moeda.format(linha.freight)} de frete</p>
        ${linha.address ? `<p class="field-run-note destino">${escapar(linha.address)}</p>` : ""}
      </button>`).join("")
    : `<p class="field-empty">Nenhum pedido esperando. Assim que o balcão lançar uma entrega, ela aparece aqui.</p>`;
}

function renderEntregues(entregues) {
  document.getElementById("field-done-block").hidden = !entregues.length;
  document.getElementById("field-done").innerHTML = entregues.map((linha) => `
    <div class="field-done ${linha.failed ? "failed" : ""}">
      <span>${escapar(linha.zone_name)}</span>
      <small>${linha.failed ? "não entregue" : `${linha.minutes} min · ${moeda.format(linha.freight)}`}</small>
    </div>`).join("");
}

// ----------------------------------------------------------------- entrega

function abrirEntrega(orderId, opcoes = {}) {
  const dados = estado.data;
  const zonas = dados.queue.zones || [];
  const pedido = orderId ? (dados.queue.orders || []).find((linha) => linha.order_id === orderId) : null;
  estado.escolhido = pedido || null;

  const form = document.getElementById("run-form");
  form.reset();
  // Lista vazia deixava o entregador parado na calçada esperando o balcão
  // cadastrar um bairro. A saída fica sempre na própria lista, no fim.
  document.getElementById("run-zone").innerHTML = zonas
    .map((zona) => `<option value="${zona.id}">${escapar(zona.name)}${zona.proposed ? " (a confirmar)" : ""}</option>`)
    .join("") + '<option value="__nova__">+ Criar zona nova…</option>';
  document.getElementById("run-new-zone-field").hidden = true;
  document.getElementById("run-new-zone").value = "";
  // A zona que este cliente usou da última vez já vem marcada: ela é o único
  // dado que o pedido não traz, e adivinhá-la é o toque que sobra para cortar.
  if (pedido?.suggested_zone_id) document.getElementById("run-zone").value = pedido.suggested_zone_id;

  document.getElementById("run-manual").hidden = Boolean(pedido);
  const destino = pedido?.address ? ` · ${pedido.address}` : "";
  document.getElementById("run-summary").textContent = pedido
    ? `${resumoItens(pedido)} · ${moeda.format(pedido.freight)} de frete${pedido.customer_name ? ` · ${pedido.customer_name}` : ""}${destino}`
    : opcoes.atrasada
      ? "Entrega de outro dia que ficou sem registro. Ela entra na data que você escolher, e a venda vai para a loja conferir."
      : "Entrega que não tem pedido lançado no sistema. Diga o que vai junto, e quanto recebeu — a loja confere antes de virar faturamento.";
  if (!pedido) {
    document.getElementById("run-items").innerHTML = "";
    adicionarItem();
    const data = document.getElementById("run-date");
    // O campo só aceita a janela em que o servidor também aceita: recusar no
    // teclado é melhor do que recusar depois de o formulário inteiro ser
    // preenchido na calçada.
    const hoje = new Date();
    const limite = new Date(hoje.getTime() - 7 * 86400000);
    data.max = dataLocal(hoje);
    data.min = dataLocal(limite);
    data.value = opcoes.atrasada ? dataLocal(new Date(hoje.getTime() - 86400000)) : "";
    document.getElementById("run-date-field").hidden = false;
  }
  texto("#run-modal-title", opcoes.atrasada ? "Entrega de outro dia" : "Confirmar");
  document.getElementById("run-modal").showModal();
  refreshIcons();
}

function fecharEntrega() {
  document.getElementById("run-modal").close();
  estado.escolhido = null;
}

function adicionarItem() {
  const produtos = estado.data.queue.products || [];
  const opcoes = produtos.map((linha) => `<option value="${linha.id}">${escapar(linha.name)}</option>`).join("");
  document.getElementById("run-items").insertAdjacentHTML("beforeend", `
    <div class="recipe-line run-item">
      <label>Produto<select>${opcoes}</select></label>
      <label>Qtd<input type="number" min="1" max="50" step="1" value="1" /></label>
    </div>`);
  refreshIcons();
}

async function enviarEntrega(evento) {
  evento.preventDefault();
  const pedido = estado.escolhido;

  // Zona nova nasce antes da entrega, para a entrega já sair com ela. Se a
  // criação falhar, nada é lançado — melhor recusar do que gravar a entrega
  // numa zona que não existe.
  let zonaId = document.getElementById("run-zone").value;
  if (zonaId === "__nova__") {
    const nome = document.getElementById("run-new-zone").value.trim();
    if (nome.length < 2) return mostrar("Escreva o nome da zona nova.");
    try {
      const criada = await api(`/api/field/zones${chave()}`, { method: "POST", body: JSON.stringify({ name: nome }) });
      zonaId = criada.id;
    } catch (erro) {
      return mostrar(erro.message || "Não foi possível criar a zona.");
    }
  }

  const payload = {
    zone_id: zonaId,
    reference: document.getElementById("run-reference").value.trim() || null,
  };
  if (pedido) {
    payload.order_id = pedido.order_id;
  } else {
    payload.customer_name = document.getElementById("run-customer").value.trim() || null;
    const frete = numero(document.getElementById("run-freight").value);
    if (frete !== null) payload.freight = frete;
    payload.items = [...document.querySelectorAll("#run-items .run-item")]
      .map((linha) => ({
        product_id: linha.querySelector("select").value,
        quantity: Number(linha.querySelector("input").value),
      }))
      .filter((linha) => linha.product_id && linha.quantity > 0);
    if (!payload.items.length) return mostrar("Diga o que vai na entrega.");
    const recebido = numero(document.getElementById("run-total").value);
    if (recebido !== null) payload.declared_total = recebido;
    const pagamento = document.getElementById("run-payment").value;
    if (pagamento) payload.payment_method = pagamento;
    const dia = document.getElementById("run-date").value;
    // Meio-dia, e não meia-noite: o horário local vira UTC no envio, e a
    // meia-noite de uma data brasileira cai no dia anterior do outro lado.
    if (dia) payload.occurred_at = `${dia}T12:00:00`;
  }
  if (await acao(`/api/field/runs${chave()}`, payload, "Boa viagem. Toque em “Cheguei” na porta do cliente.")) {
    fecharEntrega();
  }
}

async function finalizar(runId, falhou) {
  if (falhou && !confirm("Marcar como não entregue?\n\nEla continua contando como rua rodada, mas fica fora do acerto e da média de tempo.")) return;
  await acao(`/api/field/runs/${runId}/finish${chave()}`, { failed: falhou }, falhou ? "Registrada como não entregue." : "Entrega registrada.");
}

async function encerrar() {
  const totais = estado.data?.totals;
  const resumo = totais ? `${totais.deliveries} entrega(s) · ${moeda.format(totais.freight)} de frete` : "";
  if (!confirm(`Encerrar o turno?\n\n${resumo}\n\nO acerto do dia é montado com esses números.`)) return;
  await acao(`/api/field/check-out${chave()}`, {}, "Turno encerrado. O acerto já está na loja.");
}

// -------------------------------------------------------------- histórico
//
// O turno fechado e a entrega de ontem sumiam da tela. O dado sempre esteve
// gravado — `checkin_at` e `checkout_at` guardam a hora desde o primeiro dia —,
// faltava alguém exibir. Sem isso o entregador não confere o próprio acerto, e
// conferir o próprio acerto é a razão de ele registrar.
//
// Carrega só quando o entregador pede: na calçada, cada requisição a mais é
// uma que pode não voltar.

async function alternarHistorico() {
  const alvo = document.getElementById("field-history");
  const botao = document.getElementById("field-history-toggle");
  if (!alvo.hidden) {
    alvo.hidden = true;
    botao.textContent = "Ver";
    return;
  }
  botao.textContent = "Carregando…";
  try {
    const historico = await api(`/api/field/history${chave()}`);
    renderHistorico(historico);
    alvo.hidden = false;
    botao.textContent = "Ocultar";
  } catch (erro) {
    botao.textContent = "Ver";
    mostrar(erro.message || "Não foi possível carregar o histórico.");
  }
}

function renderHistorico(historico) {
  const totais = historico.totals;
  const avulsas = historico.loose_runs.length
    ? `<article class="field-shift loose">
         <div class="field-shift-top"><strong>Fora de turno</strong><span>${historico.loose_runs.length} entrega(s)</span></div>
         ${historico.loose_runs.map((linha) => `<p class="field-run-note">${escapar(formatarDia(linha.date))} · ${escapar(linha.zone_name || "sem zona")} · ${moeda.format(linha.freight)}</p>`).join("")}
       </article>`
    : "";
  const turnos = historico.shifts.map((turno) => `
    <article class="field-shift">
      <div class="field-shift-top">
        <strong>${escapar(formatarDia(turno.date))}</strong>
        <span>${hora(turno.checkin_at)} → ${turno.open ? "em turno" : hora(turno.checkout_at)}</span>
      </div>
      <p class="field-run-note">${turno.deliveries} entrega(s) · ${moeda.format(turno.freight)} de frete · ${duracao(turno.minutes)}${turno.failed ? ` · ${turno.failed} não entregue(s)` : ""}</p>
      ${turno.auto_closed ? `<p class="field-run-note alerta">Fechado pelo sistema (${escapar(turno.auto_closed)}). A duração não foi medida.</p>` : ""}
    </article>`).join("");

  document.getElementById("field-history").innerHTML = `
    <p class="field-run-note">${totais.shifts} turno(s) · ${totais.deliveries} entrega(s) · ${moeda.format(totais.freight)} · ${totais.hours}h em turno</p>
    ${avulsas}${turnos || (avulsas ? "" : '<p class="field-empty">Nada registrado ainda nos últimos 30 dias.</p>')}`;
  refreshIcons();
}

// ---------------------------------------------------------------- serviço

async function acao(caminho, payload, sucesso) {
  // Toque duplo com luva de moto é a regra, não a exceção: sem esta trava o
  // mesmo pedido sairia duas vezes e o acerto do dia sairia inflado.
  if (estado.enviando) return false;
  estado.enviando = true;
  document.querySelectorAll(".field-action").forEach((botao) => { botao.disabled = true; });
  try {
    await api(caminho, { method: "POST", body: JSON.stringify(payload) });
    await carregar();
    mostrar(sucesso, "ok");
    return true;
  } catch (erro) {
    mostrar(erro.message || "Não foi possível registrar.");
    return false;
  } finally {
    estado.enviando = false;
    document.querySelectorAll(".field-action").forEach((botao) => { botao.disabled = false; });
  }
}

async function api(caminho, opcoes = {}) {
  const headers = { ...(opcoes.headers || {}) };
  const token = await window.erpAuth?.getToken?.();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opcoes.body) headers["Content-Type"] = "application/json";
  let resposta;
  try {
    resposta = await fetch(caminho, { ...opcoes, headers });
  } catch (_) {
    throw new Error((opcoes.method || "GET") === "GET"
      ? "Sem conexão. Tente de novo quando o sinal voltar."
      : "A conexão caiu e não deu para confirmar. Atualize antes de registrar de novo.");
  }
  if (!resposta.ok) {
    let mensagem = "Não foi possível concluir.";
    try {
      const detalhe = (await resposta.json()).detail;
      if (typeof detalhe === "string") mensagem = detalhe;
    } catch (_) { /* mantém a mensagem padrão */ }
    throw new Error(mensagem);
  }
  return resposta.status === 204 ? null : resposta.json();
}

// Sem autenticação (modo demonstrativo) não há conta para identificar quem é o
// entregador, então ele vem na URL. Com autenticação ligada o servidor ignora
// este parâmetro e usa a conta — aceitar os dois deixaria alguém lançar
// entrega no nome de outro.
function escolhidoNaUrl() {
  return new URLSearchParams(location.search).get("courier_id") || null;
}

function chave() {
  // Três origens, nesta ordem: o que a loja acabou de escolher, o que ela
  // escolheu antes neste aparelho, e o parâmetro na URL (que é como o modo
  // demonstrativo e o teste dizem de quem é o turno).
  const escolhido = courierIdAtual();
  return escolhido ? `?courier_id=${encodeURIComponent(escolhido)}` : "";
}

function courierIdAtual() {
  return estado.courierId || guardado() || escolhidoNaUrl();
}

function guardado() {
  try { return localStorage.getItem("sdm-courier") || ""; } catch (_) { return ""; }
}

// ---------------------------------------------------------------- auxiliares

function resumoItens(linha) {
  const itens = linha.items || [];
  if (!itens.length) return `${linha.units || 0} item(ns)`;
  return itens.map((item) => `${item.quantity}× ${item.name}`).join(", ");
}

function dataLocal(quando) {
  const deslocado = new Date(quando.getTime() - quando.getTimezoneOffset() * 60000);
  return deslocado.toISOString().slice(0, 10);
}

function formatarDia(valor) {
  return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short", weekday: "short" })
    .format(new Date(`${String(valor).slice(0, 10)}T12:00:00`));
}

function hora(valor) {
  if (!valor) return "--:--";
  return new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" }).format(new Date(valor));
}

// O mapa do entregador é o mapa que ele já tem no bolso.
//
// Desenhar um mapa aqui dentro custaria biblioteca, tiles e — o mais caro —
// geocodificação paga para transformar "Rua das Flores, 120" em coordenada.
// Nada disso melhora a decisão dele: quem está na moto quer a rota falada, na
// tela grande, com trânsito. Um link para o aplicativo de mapas entrega isso
// com zero dependência, funciona com Google Maps ou Waze, e continua servindo
// quando a loja só sabe o bairro.
function linkDoMapa(endereco, zona) {
  const alvo = [endereco, zona].filter(Boolean).join(", ").trim();
  if (!alvo) return "";
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(alvo)}`;
}

function botaoDoMapa(endereco, zona) {
  const url = linkDoMapa(endereco, zona);
  if (!url) return "";
  // `noopener` porque o destino abre em outra aba e não deve alcançar esta.
  return `<a class="field-map" href="${escapar(url)}" target="_blank" rel="noopener noreferrer"><i data-lucide="map-pin"></i><span>Abrir no mapa</span></a>`;
}

function duracao(minutos) {
  if (minutos < 60) return `${minutos} min`;
  return `${Math.floor(minutos / 60)}h${String(minutos % 60).padStart(2, "0")}`;
}

function numero(valor) {
  const texto = String(valor ?? "").trim().replace(",", ".");
  if (!texto) return null;
  const convertido = Number(texto);
  return Number.isFinite(convertido) && convertido >= 0 ? convertido : null;
}

function texto(seletor, valor) {
  const elemento = document.querySelector(seletor);
  if (elemento) elemento.textContent = valor;
}

function escapar(valor) {
  return String(valor ?? "").replace(/[&<>'"]/g, (letra) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[letra]));
}

function mostrar(mensagem, tipo = "error") {
  const elemento = document.getElementById("toast");
  elemento.textContent = mensagem;
  elemento.className = `toast ${tipo === "ok" ? "success" : "error"}`;
  elemento.hidden = false;
  clearTimeout(mostrar.timer);
  mostrar.timer = setTimeout(() => { elemento.hidden = true; }, 4200);
}

function refreshIcons() {
  window.lucide?.createIcons();
}
