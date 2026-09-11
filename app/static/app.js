const state = {
  dashboard: null,
  products: [],
  inventory: [],
  orders: [],
  expenses: [],
  expenseSeries: [],
  operating: null,
  media: [],
  mediaSummary: null,
  consumption: null,
  forecast: null,
  mediaFilter: "all",
  selectedMediaId: null,
  mediaObjectUrl: null,
  cart: new Map(),
  // Complementos escolhidos por linha do carrinho: produto -> (complemento -> porções
  // por unidade). Fica fora de `cart` para o passo a passo de quantidade continuar
  // sendo um número simples na hora de somar o pedido.
  cartAddons: new Map(),
  addons: [],
  crossing: null,
  period: 7,
  operatingPeriod: 30,
  channels: [],
  channelPerformance: null,
  salesDay: null,
  lines: null,
  // Dia escolhido no fechamento. Vazio significa hoje, no fuso da loja.
  closingDate: "",
  lineFilter: "all",
  growth: null,
  growthPeriod: 90,
  growthFilter: "all",
  productionRecipes: [],
  productionYield: null,
  productionEconomics: null,
  productionAdvice: null,
  packagingAudit: null,
  promotions: null,
  promotionMonth: null,
  promotionsPeriod: 90,
  sportAnchors: [],
  pricing: null,
  pricingPeriod: 90,
  marketPrices: [],
  planning: null,
  planningService: 0.9,
  couriers: [],
  deliveries: [],
  deliveryPerformance: null,
  // Zonas, quem está na rua e o que o território ensina. Carregado ao abrir a
  // tela de Entregas, não no loadAll.
  dispatch: null,
  deliveryPeriod: 30,
  catalogs: [],
  history: [],
  historyLimit: 60,
  historyFilter: "all",
};

const currency = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const number = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 3 });
const shortDate = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short" });

document.addEventListener("DOMContentLoaded", init);

async function init() {
  await window.erpAuth?.ready;
  document.getElementById("today-label").textContent = new Intl.DateTimeFormat("pt-BR", {
    weekday: "long", day: "2-digit", month: "long",
  }).format(new Date());
  bindNavigation();
  bindModals();
  bindForms();
  bindPwaInstall();
  bindConnectionStatus();
  bindIFood();
  const requestedView = window.location.hash.slice(1);
  if (document.getElementById(requestedView)) activateView(requestedView, false);
  document.getElementById("refresh-data").addEventListener("click", loadAll);
  bindSignOut();
  document.getElementById("period-select").addEventListener("change", async (event) => {
    state.period = Number(event.target.value);
    await loadDashboard();
  });
  document.getElementById("finance-period").addEventListener("change", async (event) => {
    state.operatingPeriod = Number(event.target.value);
    await loadOperatingResult();
  });
  document.getElementById("order-search").addEventListener("input", renderOrders);
  document.getElementById("closing-date").addEventListener("change", async (event) => {
    state.closingDate = event.target.value;
    await loadClosing();
  });
  document.getElementById("history-search").addEventListener("input", renderHistory);
  document.getElementById("history-refresh").addEventListener("click", loadHistory);
  document.getElementById("history-limit").addEventListener("change", async (event) => {
    state.historyLimit = Number(event.target.value);
    await loadHistory();
  });
  document.getElementById("delivery-period").addEventListener("change", async (event) => {
    state.deliveryPeriod = Number(event.target.value);
    await loadDelivery();
  });
  document.getElementById("planning-service").addEventListener("change", async (event) => {
    state.planningService = Number(event.target.value);
    await loadPlanning();
  });
  document.getElementById("growth-period").addEventListener("change", async (event) => {
    state.growthPeriod = Number(event.target.value);
    await loadGrowth();
  });
  document.getElementById("promotions-period").addEventListener("change", async (event) => {
    state.promotionsPeriod = Number(event.target.value);
    await loadPromotions();
  });
  document.getElementById("pricing-period").addEventListener("change", async (event) => {
    state.pricingPeriod = Number(event.target.value);
    await loadPricing();
  });
  document.getElementById("closing-today").addEventListener("click", async () => {
    state.closingDate = "";
    document.getElementById("closing-date").value = todayInputValue();
    await loadClosing();
  });
  document.getElementById("inventory-search").addEventListener("input", renderInventory);
  document.getElementById("consumption-search").addEventListener("input", renderConsumption);
  document.getElementById("mobile-menu").addEventListener("click", () => {
    setMobileNavigation(!document.querySelector(".sidebar").classList.contains("open"));
  });
  document.getElementById("mobile-nav-backdrop").addEventListener("click", () => setMobileNavigation(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setMobileNavigation(false);
  });
  await loadAll();
}

function bindSignOut() {
  const botao = document.getElementById("sign-out");
  const identidade = document.getElementById("account-label");
  const email = window.erpAuth?.email;
  if (email && identidade) identidade.textContent = email;
  // Sem login exigido (modo local) nao ha sessao para encerrar.
  if (!botao || !window.erpAuth?.canSignOut) return;
  botao.hidden = false;
  botao.addEventListener("click", async () => {
    if (!confirm("Sair da conta neste aparelho?")) return;
    await window.erpAuth.signOut();
  });
}

function bindPwaInstall() {
  const button = document.getElementById("install-app");
  if (!button) return;
  let installPrompt = null;
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    button.hidden = false;
  });
  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    button.hidden = true;
  });
  button.addEventListener("click", async () => {
    if (!installPrompt) {
      toast("Use o menu do navegador para instalar o aplicativo.");
      return;
    }
    installPrompt.prompt();
    await installPrompt.userChoice;
    installPrompt = null;
    button.hidden = true;
  });
}

function bindConnectionStatus() {
  const updateStatus = () => {
    document.getElementById("offline-banner").hidden = navigator.onLine;
  };
  window.addEventListener("offline", updateStatus);
  window.addEventListener("online", () => {
    updateStatus();
    toast("Conexao restabelecida.", "success");
    loadAll();
  });
  updateStatus();
}

function setMobileNavigation(open) {
  const sidebar = document.querySelector(".sidebar");
  const trigger = document.getElementById("mobile-menu");
  const backdrop = document.getElementById("mobile-nav-backdrop");
  sidebar.classList.toggle("open", open);
  trigger.setAttribute("aria-expanded", String(open));
  backdrop.hidden = !open;
}

// A carga do painel, em duas ondas.
//
// Antes as 34 consultas saíam juntas e a tela só aparecia quando a mais lenta
// voltava. Medido em produção: `/api/promotions/month` com 16,5 s de mediana e
// 27,8 s no pior caso — e como o Cloud Run atende com uma CPU, as 34 em
// paralelo ainda disputavam entre si e atrasavam umas às outras. O gestor
// esperava meio minuto por uma tela cujo conteúdo inicial estava pronto no
// primeiro segundo.
//
// A primeira onda traz o que a tela inicial mostra e o que os formulários
// precisam para abrir; a segunda traz os relatórios pesados e redesenha quando
// chegam. `renderAll` já tolera estado ausente — todo renderizador desiste
// quando o seu pedaço ainda é nulo —, então a tela intermediária é a tela
// final com menos coisa preenchida, e não uma tela quebrada.

const CARGA_ESSENCIAL = {
  dashboard: () => `/api/dashboard?days=${state.period}`,
  consumption: () => `/api/inventory/consumption?days=${state.period}`,
  inventory: () => "/api/inventory",
  products: () => "/api/products",
  addons: () => "/api/addons",
  orders: () => "/api/orders",
  channels: () => "/api/channels",
  catalogs: () => "/api/catalogs",
  couriers: () => "/api/couriers",
};

const CARGA_RELATORIOS = {
  crossing: () => `/api/inventory/crossing?days=${state.operatingPeriod}`,
  forecast: () => "/api/forecast?horizon=7",
  expenses: () => "/api/expenses",
  expenseSeries: () => "/api/expenses/recurring",
  operating: () => `/api/finance/operating?days=${state.operatingPeriod}`,
  mediaSummary: () => "/api/media/summary",
  media: () => "/api/media",
  channelPerformance: () => "/api/channels/performance?days=30",
  lines: () => "/api/products/lines?days=30",
  salesDay: () => `/api/sales/daily${state.closingDate ? `?day=${state.closingDate}` : ""}`,
  growth: () => `/api/growth?days=${state.growthPeriod}`,
  promotions: () => `/api/promotions/week?days=${state.promotionsPeriod}&horizon=7`,
  promotionMonth: () => `/api/promotions/month?days=${state.promotionsPeriod}&weeks=4`,
  pricing: () => `/api/pricing?days=${state.pricingPeriod}`,
  marketPrices: () => "/api/market-prices",
  recipeAudit: () => "/api/products/recipe-audit",
  productionRecipes: () => "/api/production/recipes",
  productionYield: () => "/api/production/yield?days=90",
  productionEconomics: () => "/api/production/economics?days=30",
  productionAdvice: () => "/api/production/input-advice?days=30",
  packagingAudit: () => "/api/packaging/audit?days=30",
  planning: () => `/api/planning?days=90&horizon=7&service_level=${state.planningService}`,
  deliveries: () => `/api/deliveries?days=${state.deliveryPeriod}`,
  deliveryPerformance: () => `/api/couriers/performance?days=${state.deliveryPeriod}`,
  history: () => `/api/audit?limit=${state.historyLimit}`,
};

async function carregarGrupo(grupo) {
  const chaves = Object.keys(grupo);
  const respostas = await Promise.all(chaves.map((chave) => api(grupo[chave]())));
  return Object.fromEntries(chaves.map((chave, indice) => [chave, respostas[indice]]));
}

// O mesmo, mas cada relatório responde por si.
//
// Aconteceu de verdade: um erro na análise de promoção — uma ordenação que
// tentava comparar dois produtos do mesmo tamanho — derrubava aquela rota com
// 500, e como o painel pedia os relatórios todos juntos, o erro apagava
// previsão, planejamento, financeiro e crescimento de uma vez. Telas que não
// têm nada a ver com promoção ficavam em branco, e não havia como o gestor
// saber por quê. Um relatório quebrado agora perde só a si mesmo, e diz o nome.
async function carregarGrupoTolerante(grupo) {
  const chaves = Object.keys(grupo);
  const respostas = await Promise.allSettled(chaves.map((chave) => api(grupo[chave]())));
  const dados = {};
  const falharam = [];
  respostas.forEach((resposta, indice) => {
    if (resposta.status === "fulfilled") dados[chaves[indice]] = resposta.value;
    else falharam.push(chaves[indice]);
  });
  return { dados, falharam };
}

async function loadAll() {
  setRefreshing(true);
  try {
    const [config, essencial] = await Promise.all([api("/api/config"), carregarGrupo(CARGA_ESSENCIAL)]);
    Object.assign(state, essencial);
    document.getElementById("storage-mode").textContent = config.storage === "firebase" ? "Firebase conectado" : "Modo demonstrativo";
    renderAll();
  } catch (error) {
    toast(error.message || "Não foi possível carregar os dados.", "error");
    setRefreshing(false);
    return false;
  }

  // Os relatórios chegam depois e redesenham. A falha aqui não derruba o que
  // já está na tela: perder o painel inteiro porque a análise de promoção não
  // respondeu seria trocar um problema grande por um maior.
  try {
    const { dados, falharam } = await carregarGrupoTolerante(CARGA_RELATORIOS);
    Object.assign(state, dados);
    renderAll();
    if (falharam.length) {
      toast(`${falharam.length} relatório(s) não carregaram: ${falharam.map(nomeDoRelatorio).join(", ")}. O resto da tela está atualizado.`, "error");
      return false;
    }
    return true;
  } finally {
    setRefreshing(false);
  }
}

// O nome da chave é do código; o gestor precisa do nome da tela.
const NOME_DO_RELATORIO = {
  crossing: "cruzamento de estoque", forecast: "previsão", expenses: "despesas",
  expenseSeries: "despesas fixas", operating: "resultado", mediaSummary: "acervo",
  media: "arquivos", channelPerformance: "canais", lines: "linhas", salesDay: "fechamento",
  growth: "crescimento", promotions: "promoções da semana", promotionMonth: "promoções do mês",
  pricing: "preços", marketPrices: "preços de mercado", productionRecipes: "fichas de fabricação",
  productionYield: "rendimento", productionEconomics: "custo da batida", productionAdvice: "insumos da batida",
  packagingAudit: "embalagens", planning: "planejamento", deliveries: "entregas",
  deliveryPerformance: "desempenho dos entregadores", history: "histórico",
};

function nomeDoRelatorio(chave) {
  return NOME_DO_RELATORIO[chave] || chave;
}

async function loadDashboard() {
  try {
    const [dashboard, consumption, operating] = await Promise.all([
      api(`/api/dashboard?days=${state.period}`),
      api(`/api/inventory/consumption?days=${state.period}`),
      api(`/api/finance/operating?days=${state.operatingPeriod}`),
    ]);
    state.dashboard = dashboard;
    state.consumption = consumption;
    state.operating = operating;
    renderDashboard();
    renderConsumption();
    renderFinance();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function loadOperatingResult() {
  try {
    const [operating, crossing] = await Promise.all([
      api(`/api/finance/operating?days=${state.operatingPeriod}`),
      api(`/api/inventory/crossing?days=${state.operatingPeriod}`),
    ]);
    state.operating = operating;
    state.crossing = crossing;
    state.expenseSeries = await api("/api/expenses/recurring");
    renderFinance();
    renderCrossing();
  } catch (error) {
    toast(error.message || "Não foi possível calcular o resultado operacional.", "error");
  }
}

function renderAll() {
  renderDashboard();
  renderClosing();
  renderOrders();
  renderInventory();
  renderConsumption();
  renderCrossing();
  renderPlanning();
  renderForecast();
  renderFinance();
  renderProducts();
  renderRecipeAudit();
  renderAddons();
  renderChannelsView();
  renderDelivery();
  // A operação de rua é carregada sob demanda, então ela não vem no `loadAll`
  // e o `state` dela continua o de antes da gravação. Sem esta releitura, a
  // zona recém-criada e a entrega recém-corrigida só apareciam ao trocar de
  // tela e voltar — o botão parecia não ter feito nada.
  if (state.dispatch) loadDispatch();
  renderBrand();
  renderGrowth();
  renderPromotions();
  renderPromotionMonth();
  renderPricing();
  renderProductionRecipes();
  renderProductionCost();
  renderPackagingAudit();
  renderMedia();
  renderCatalogs();
  renderHistory();
  populateSelects();
  // manage.js cuida das telas de edição e reaproveita este mesmo estado.
  window.sdmManage?.afterRender?.();
  refreshIcons();
}

function renderDashboard() {
  const dashboard = state.dashboard;
  if (!dashboard) return;
  const { metrics } = dashboard;
  setText('[data-metric="revenue"]', currency.format(metrics.revenue));
  setText('[data-metric="orders"]', number.format(metrics.orders));
  setText('[data-metric="ticket"]', currency.format(metrics.average_ticket));
  // Sem nenhum pedido com custo, a margem nao e zero: e desconhecida. Mostrar
  // "0%" faria o gestor ler prejuizo onde nao ha informacao nenhuma.
  const semCusto = (metrics.margin_coverage ?? 100) <= 0;
  setText('[data-metric="margin"]', semCusto ? "—" : `${number.format(metrics.gross_margin)}%`);
  // Pedido importado nao traz custo. Sem esse aviso, a margem parece valer para
  // o periodo inteiro quando na verdade cobre so parte do faturamento.
  const cobertura = metrics.margin_coverage ?? 100;
  setText('[data-metric="gross-profit"]', cobertura >= 99.5
    ? `${currency.format(metrics.gross_profit)} de lucro bruto`
    : cobertura <= 0
      ? "nenhum pedido do período tem custo lançado"
      : `sobre ${number.format(cobertura)}% do faturamento — ${currency.format(metrics.revenue_without_cost)} sem custo lançado`);
  const change = document.querySelector('[data-metric="revenue-change"]');
  const signal = metrics.revenue_change >= 0 ? "+" : "";
  change.textContent = `${signal}${number.format(metrics.revenue_change)}% vs. período anterior`;
  change.className = metrics.revenue_change >= 0 ? "positive" : "negative";
  setText("#chart-total", currency.format(metrics.revenue));
  renderChart(dashboard.sales_series);
  renderPeriodGap(metrics);
  renderInsights(dashboard.insights);
  renderChannels(dashboard.channels);
  renderRecentOrders(dashboard.recent_orders);
  const best = dashboard.top_products[0];
  setText("#featured-name", best ? best.name : "Produto campeão");
  setText("#featured-copy", best ? "Mantenha a ficha técnica e a reposição em dia para não perder vendas." : "Registre as vendas para identificar seu item campeão.");
  setText("#featured-amount", best ? `${best.quantity} unidades no período` : "Sem vendas no período");
  const stockCount = dashboard.low_stock.length;
  const stockBadge = document.getElementById("nav-stock-count");
  stockBadge.hidden = stockCount === 0;
  stockBadge.textContent = stockCount;
  refreshIcons();
}

// Mirrors needs_restock() no backend: minimo zero significa "nao definido".
function needsRestock(item) {
  return item.minimum_quantity > 0 && item.quantity <= item.minimum_quantity;
}

// Escala "bonita" acima do maior valor, para o eixo cair em numeros redondos.
function niceCeiling(value) {
  if (value <= 0) return 100;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const steps = [1, 2, 2.5, 5, 10];
  return magnitude * steps.find((step) => value <= magnitude * step);
}

function renderChart(series) {
  const container = document.getElementById("sales-chart");
  const peak = Math.max(...series.map((row) => row.value), 0);
  const top = niceCeiling(peak);
  const axis = document.getElementById("chart-y-axis");
  if (axis) {
    axis.innerHTML = [top, top / 2, 0]
      .map((value) => `<span>${escapeHtml(currency.format(value))}</span>`)
      .join("");
  }
  container.style.setProperty("--columns", series.length);
  container.innerHTML = series.map((row) => {
    const height = row.value > 0 ? Math.max(6, Math.round((row.value / top) * 150)) : 2;
    return `<div class="bar-group" title="${escapeHtml(formatDate(row.date))}: ${currency.format(row.value)}"><span class="bar" style="height:${height}px"></span><span class="bar-label">${escapeHtml(shortDate.format(new Date(`${row.date}T12:00:00`)).replace(".", ""))}</span></div>`;
  }).join("");
}

// A leitura tem a mesma forma em toda tela que a mostra, e o tipo escolhe o
// ícone. Repetir a marcação em cada render é como uma delas ficava com o ícone
// de outra categoria depois de qualquer ajuste.
function insightCard(item) {
  const icons = { attention: "triangle-alert", opportunity: "badge-dollar-sign", info: "lightbulb" };
  return `<article class="insight ${item.type}"><span class="insight-icon"><i data-lucide="${item.icon || icons[item.type] || "lightbulb"}"></i></span><div><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.description)}</p></div></article>`;
}

// Período sem venda não é resultado zero — é ausência de dado, e as duas coisas
// se parecem na tela. "Faturamento R$ 0,00 +0%" com 928 pedidos na base fez o
// gestor concluir que o sistema estava quebrado, e ele tinha razão em duvidar:
// nada ali dizia que a última venda foi há três semanas. Sem esta linha, todo
// relatório que depende do período parece sem nexo ao mesmo tempo.
function renderPeriodGap(metrics) {
  const aviso = document.getElementById("period-gap");
  if (!aviso) return;
  const vendidos = state.orders.filter((pedido) => pedido.status !== "Cancelado");
  if (metrics.orders > 0 || !vendidos.length) {
    aviso.hidden = true;
    return;
  }
  const ultima = vendidos.reduce((maior, pedido) => (pedido.created_at > maior ? pedido.created_at : maior), "");
  const dias = Math.max(0, Math.round((Date.now() - new Date(ultima).getTime()) / 86400000));
  aviso.hidden = false;
  aviso.innerHTML = `<i data-lucide="calendar-off"></i><span>Nenhuma venda nos últimos ${state.period} dias — é por isso que os números abaixo estão zerados. A última venda registrada foi em <strong>${escapeHtml(formatDate(ultima))}</strong>, ${dias} dia(s) atrás.${dias > state.period ? " Aumente o período acima para ver o movimento." : ""}</span>`;
  refreshIcons();
}

function renderInsights(insights) {
  document.getElementById("insight-list").innerHTML = insights.map(insightCard).join("");
}

function renderChannels(channels) {
  const container = document.getElementById("channel-list");
  const total = channels.reduce((sum, row) => sum + row.value, 0);
  container.innerHTML = channels.length ? channels.map((channel) => {
    const percentage = total ? (channel.value / total) * 100 : 0;
    return `<div class="channel-row"><span>${escapeHtml(channel.name)}</span><span class="channel-meter"><span style="width:${percentage}%"></span></span><strong>${number.format(percentage)}%</strong></div>`;
  }).join("") : '<p class="empty-state">Ainda não há vendas no período.</p>';
}

function renderRecentOrders(orders) {
  document.getElementById("recent-orders-body").innerHTML = orders.length ? orders.map(order => `
    <tr><td><strong>#${order.number}</strong></td><td><span class="channel-pill">${escapeHtml(order.channel)}</span></td><td>${escapeHtml(itemSummary(order))}</td><td>${escapeHtml(paymentLabel(order.payment_method))}</td><td class="align-right"><strong>${currency.format(order.total)}</strong></td></tr>
  `).join("") : emptyRow(5, "Nenhuma venda no período.");
}

function renderOrders() {
  const query = document.getElementById("order-search")?.value.trim().toLocaleLowerCase("pt-BR") || "";
  const orders = state.orders.filter(order => `${order.number} ${order.channel} ${itemSummary(order)}`.toLocaleLowerCase("pt-BR").includes(query));
  document.getElementById("orders-body").innerHTML = orders.length ? orders.map(orderRow).join("") : emptyRow(7, "Nenhum pedido encontrado.");
  refreshIcons();
}

// Pedido cancelado continua na lista, riscado: sumir com ele esconderia da
// loja que a venda existiu e por que caiu.
function orderRow(order) {
  const cancelled = order.status === "Cancelado";
  const reason = cancelled ? `<span class="cancel-reason">${escapeHtml(order.cancel_reason || "cancelado")}</span>` : "";
  return `<tr class="${cancelled ? "cancelled-row" : ""}"><td><strong>#${order.number}</strong>${reason}</td><td>${escapeHtml(formatDateTime(order.created_at))}</td><td><span class="channel-pill">${escapeHtml(order.channel)}</span></td><td>${escapeHtml(itemSummary(order) || "sem itens detalhados")}</td><td>${escapeHtml(paymentLabel(order.payment_method))}</td><td class="align-right"><strong>${currency.format(order.total)}</strong>${order.channel_fee ? `<small class="row-note">-${currency.format(order.channel_fee)} de taxa</small>` : ""}</td><td class="align-right">${orderActions(order)}</td></tr>`;
}

function orderActions(order) {
  if (order.status === "Cancelado") {
    return `<div class="row-actions"><button class="row-action danger" type="button" data-delete-order="${order.id}" title="Excluir do histórico"><i data-lucide="trash-2"></i></button></div>`;
  }
  return `<div class="row-actions">
    <button class="row-action" type="button" data-edit-order="${order.id}" title="Editar pedido"><i data-lucide="pencil"></i></button>
    <button class="row-action danger" type="button" data-cancel-order="${order.id}" title="Cancelar e devolver o estoque"><i data-lucide="ban"></i></button>
  </div>`;
}

function renderInventory() {
  const query = document.getElementById("inventory-search")?.value.trim().toLocaleLowerCase("pt-BR") || "";
  const items = state.inventory.filter(item => `${item.name} ${item.category}`.toLocaleLowerCase("pt-BR").includes(query));
  const critical = state.inventory.filter(needsRestock);
  const totalValue = state.inventory.reduce((sum, item) => sum + item.quantity * item.unit_cost, 0);
  document.getElementById("inventory-summary").innerHTML = `
    <article class="inventory-stat"><i data-lucide="boxes"></i><div><p>Itens cadastrados</p><strong>${state.inventory.length}</strong></div></article>
    <article class="inventory-stat ${critical.length ? "warning" : ""}"><i data-lucide="triangle-alert"></i><div><p>Abaixo do mínimo</p><strong>${critical.length} item${critical.length === 1 ? "" : "ns"}</strong></div></article>
    <article class="inventory-stat"><i data-lucide="landmark"></i><div><p>Valor em estoque</p><strong>${currency.format(totalValue)}</strong></div></article>`;
  document.getElementById("inventory-body").innerHTML = items.length ? items.map(item => {
    // Negativo significa que a venda saiu sem o insumo estar lancado: a contagem
    // esta atrasada, e isso pesa mais que qualquer outro aviso.
    const negative = item.quantity < 0;
    const low = !negative && needsRestock(item);
    // Sem contagem e sem minimo o item nao esta saudavel nem em falta: esta por configurar.
    const uncounted = !negative && !low && !item.quantity && !item.minimum_quantity;
    const status = negative
      ? { css: "danger", label: "Conferir" }
      : low ? { css: "pending", label: "Repor" }
      : uncounted ? { css: "neutral", label: "Sem contagem" }
      : { css: "success", label: "Saudável" };
    const dot = negative ? "negative" : low ? "low" : uncounted ? "idle" : "";
    const minimum = item.minimum_quantity ? `${number.format(item.minimum_quantity)} ${escapeHtml(item.unit)}` : "não definido";
    return `<tr><td><strong>${escapeHtml(item.name)}</strong></td><td>${escapeHtml(item.category)}</td><td><span class="stock-level"><i class="stock-dot ${dot}"></i><span class="stock-amount">${number.format(item.quantity)} ${escapeHtml(item.unit)}</span></span></td><td class="muted-value">${minimum}</td><td>${currency.format(item.unit_cost)}</td><td><span class="status-pill ${status.css}">${status.label}</span></td><td class="align-right"><div class="row-actions">
      <button class="row-action" type="button" data-purchase-item="${item.id}" title="Registrar compra"><i data-lucide="shopping-cart"></i></button>
      <button class="row-action" type="button" data-count-item="${item.id}" title="Contagem física"><i data-lucide="clipboard-check"></i></button>
      <button class="row-action" type="button" data-edit-inventory="${item.id}" title="Editar cadastro"><i data-lucide="pencil"></i></button>
      <button class="row-action danger" type="button" data-delete-inventory="${item.id}" title="Excluir insumo"><i data-lucide="trash-2"></i></button>
    </div></td></tr>`;
  }).join("") : emptyRow(7, "Nenhum insumo encontrado.");
  refreshIcons();
}

function renderConsumption() {
  const data = state.consumption;
  if (!data) return;
  const { totals } = data;
  setText("#consumption-total", currency.format(totals.total_cost));
  setText("#consumption-daily", `${currency.format(totals.daily_cost)} por dia, em média`);
  setText("#consumption-sale", currency.format(totals.sale_cost));
  setText("#consumption-withdrawal", currency.format(totals.withdrawal_cost || 0));
  setText("#consumption-loss", currency.format(totals.loss_cost));
  setText("#consumption-loss-share", totals.total_cost ? `${number.format(totals.loss_share)}% de tudo que saiu` : "nada registrado ainda");
  setText("#consumption-running-out", number.format(data.running_out.length));
  renderConsumptionChart(data.series);

  const query = document.getElementById("consumption-search")?.value.trim().toLocaleLowerCase("pt-BR") || "";
  const items = data.items.filter((item) => item.name.toLocaleLowerCase("pt-BR").includes(query));
  document.getElementById("consumption-body").innerHTML = items.length ? items.map((item) => {
    const duration = item.days_left === null
      ? '<span class="muted-value">sem consumo</span>'
      : `<span class="status-pill ${item.days_left <= 7 ? "pending" : "success"}">${number.format(item.days_left)} dias</span>`;
    const lost = item.lost > 0 ? `<strong class="negative">${number.format(item.lost)} ${escapeHtml(item.unit)}</strong>` : `<span class="muted-value">—</span>`;
    const withdrawn = item.withdrawn > 0 ? `<strong>${number.format(item.withdrawn)} ${escapeHtml(item.unit)}</strong>` : `<span class="muted-value">—</span>`;
    return `<tr><td><strong>${escapeHtml(item.name)}</strong></td><td class="align-right">${number.format(item.sold)} ${escapeHtml(item.unit)}</td><td class="align-right">${withdrawn}</td><td class="align-right">${lost}</td><td class="align-right">${currency.format(item.cost)}</td><td class="align-right muted-value">${number.format(item.quantity)} ${escapeHtml(item.unit)}</td><td>${duration}</td></tr>`;
  }).join("") : emptyRow(7, "Nenhuma saída de insumo no período.");

  document.getElementById("movement-body").innerHTML = data.history.length ? data.history.map((row) => {
    const labels = { venda: "Venda", retirada: "Retirada", perda: "Baixa", entrada: "Entrada", compra: "Compra", estorno: "Estorno" };
    const css = { venda: "success", retirada: "neutral", perda: "pending", entrada: "neutral", compra: "success", estorno: "neutral" };
    const signal = row.quantity_delta > 0 ? "+" : "";
    // Baixa de venda só muda pelo pedido: oferecer o lápis aqui separaria o
    // pedido do estoque, que é exatamente o que a edição precisa evitar.
    const actions = row.kind === "retirada"
      ? `<div class="row-actions"><button class="row-action danger" type="button" data-undo-withdrawal="${row.withdrawal_id}" title="Desfazer retirada completa"><i data-lucide="undo-2"></i></button></div>`
      : row.editable
      ? `<div class="row-actions"><button class="row-action" type="button" data-edit-movement="${row.id}" title="Corrigir lançamento"><i data-lucide="pencil"></i></button></div>`
      : `<span class="muted-value" title="Esta movimentação é vinculada a uma venda, estorno ou compra.">—</span>`;
    // A retirada lancada por cima do saldo e a excecao que precisa ficar a
    // vista: e ela que explica o negativo na tela de Estoque.
    const abaixo = row.below_balance ? ' <span class="status-pill pending" title="Registrada sem saldo: o estoque ficou negativo até a próxima contagem.">sem saldo</span>' : "";
    return `<tr><td class="muted-value">${escapeHtml(formatDateTime(row.created_at))}</td><td><strong>${escapeHtml(row.name)}</strong></td><td><span class="status-pill ${css[row.kind]}">${labels[row.kind]}</span></td><td>${escapeHtml(row.reason)}${abaixo}</td><td class="align-right"><strong>${signal}${number.format(row.quantity_delta)} ${escapeHtml(row.unit)}</strong></td><td class="align-right">${actions}</td></tr>`;
  }).join("") : emptyRow(6, "Nenhuma movimentação registrada.");
  refreshIcons();
}

function renderConsumptionChart(series) {
  const container = document.getElementById("consumption-chart");
  if (!container) return;
  const peak = Math.max(...series.map((row) => row.sale_cost + (row.withdrawal_cost || 0) + row.loss_cost), 0);
  const top = niceCeiling(peak);
  const axis = document.getElementById("consumption-y-axis");
  if (axis) axis.innerHTML = [top, top / 2, 0].map((value) => `<span>${escapeHtml(currency.format(value))}</span>`).join("");
  container.style.setProperty("--columns", series.length);
  container.innerHTML = series.map((row) => {
    const withdrawal = row.withdrawal_cost || 0;
    const total = row.sale_cost + withdrawal + row.loss_cost;
    const height = (value) => (value > 0 ? Math.max(3, Math.round((value / top) * 150)) : 0);
    const title = `${formatDate(row.date)}: ${currency.format(row.sale_cost)} em venda, ${currency.format(withdrawal)} em retirada, ${currency.format(row.loss_cost)} em perda`;
    const stack = total > 0
      ? `<span class="bar loss" style="height:${height(row.loss_cost)}px"></span><span class="bar withdrawal" style="height:${height(withdrawal)}px"></span><span class="bar sale" style="height:${height(row.sale_cost)}px"></span>`
      : `<span class="bar empty"></span>`;
    return `<div class="bar-group" title="${escapeHtml(title)}"><span class="bar-stack">${stack}</span><span class="bar-label">${escapeHtml(shortDate.format(new Date(`${row.date}T12:00:00`)).replace(".", ""))}</span></div>`;
  }).join("");
}

function renderForecast() {
  const f = state.forecast;
  if (!f) return;
  const vazio = document.getElementById("forecast-empty");
  const pronto = document.getElementById("forecast-ready");
  vazio.hidden = f.ready;
  pronto.hidden = !f.ready;

  if (!f.ready) {
    setText("#forecast-empty-reason", f.reason);
    setText("#forecast-progress", `${f.history_days} de ${f.required_days} dias necessários`);
    refreshIcons();
    return;
  }

  setText("#forecast-basis", `${f.explanation.baseline_note} ${f.explanation.trend_note}`);
  setText("#fc-baseline", currency.format(f.baseline));
  setText("#fc-baseline-note", `média de ${f.history_days} dias, incluindo os sem venda`);
  const sinal = f.trend.percent_per_week >= 0 ? "+" : "";
  setText("#fc-trend", `${sinal}${number.format(f.trend.percent_per_week)}%`);
  setText("#fc-error", `${number.format(f.accuracy.percent)}%`);
  setText("#fc-error-note", `${currency.format(f.accuracy.mean_absolute_error)} por dia, em média`);
  setText("#fc-days", `${f.history_days} dias`);
  setText("#fc-orders", `${number.format(f.orders)} pedidos`);

  const maior = Math.max(...f.weekday_effect.map((d) => d.factor), 1);
  document.getElementById("fc-weekday").innerHTML = f.weekday_effect.map((d) => `
    <div class="weekday-row ${d.reliable ? "" : "unsure"}">
      <span class="wd-name">${escapeHtml(d.name)}</span>
      <span class="wd-bar"><span style="width:${Math.round((d.factor / maior) * 100)}%"></span></span>
      <strong class="wd-factor">${number.format(d.factor)}×</strong>
      <span class="wd-reading">${escapeHtml(d.reading)}</span>
    </div>`).join("");

  const total = f.daily_forecast.reduce((soma, d) => soma + d.value, 0);
  setText("#fc-next-total", currency.format(total));
  document.getElementById("fc-next-body").innerHTML = f.daily_forecast.map((d) => `
    <tr><td><strong>${escapeHtml(d.weekday)}</strong> <span class="muted-value">${escapeHtml(formatDate(d.date))}</span></td><td class="align-right"><strong>${currency.format(d.value)}</strong></td><td class="align-right muted-value">${number.format(d.factor)}×${d.reliable ? "" : " *"}</td></tr>`).join("");

  const compra = f.purchase_plan;
  const painelCompra = document.getElementById("fc-purchase-panel");
  painelCompra.hidden = !compra.available;
  if (compra.available) {
    setText("#fc-purchase-total", currency.format(compra.total_cost));
    setText("#fc-purchase-note", compra.note);
    document.getElementById("fc-purchase-body").innerHTML = compra.items.length ? compra.items.map((i) => `
      <tr><td><strong>${escapeHtml(i.name)}</strong></td><td class="align-right">${number.format(i.needed)} ${escapeHtml(i.unit)}</td><td class="align-right muted-value">${number.format(i.in_stock)} ${escapeHtml(i.unit)}</td><td class="align-right">${i.to_buy > 0 ? `<strong>${number.format(i.to_buy)} ${escapeHtml(i.unit)}</strong>` : '<span class="muted-value">tem o bastante</span>'}</td><td class="align-right">${i.cost > 0 ? currency.format(i.cost) : "—"}</td></tr>`).join("") : emptyRow(5, "Sem receita cadastrada para converter a previsão em insumo.");
  }

  const projecao = compra.operating_projection;
  const painelOperacional = document.getElementById("fc-operating-panel");
  painelOperacional.hidden = !projecao;
  if (projecao) {
    const aviso = document.getElementById("fc-operating-warning");
    if (!projecao.available) {
      setText("#fc-operating-profit", "Aguardando dados");
      setText("#fc-operating-note", projecao.reason);
      aviso.innerHTML = "";
      document.getElementById("fc-operating-body").innerHTML = emptyRow(3, "O lucro previsto precisa de todas as vendas com itens e ficha tecnica.");
      document.getElementById("fc-operating-product-body").innerHTML = emptyRow(5, "Sem cruzamento completo de produto e insumo.");
    } else {
      const totals = projecao.totals;
      setText("#fc-operating-profit", currency.format(totals.operating_profit));
      setText("#fc-operating-note", `${projecao.note} Cobertura dos itens vendidos: ${number.format(projecao.coverage)}%.`);
      const rows = [
        ["Receita de produtos", "Unidades previstas x preco atual do cardapio", totals.product_revenue, "positive"],
        ["Frete previsto", "Media dos fretes nos pedidos do historico", totals.freight_revenue, "positive"],
        ["Taxas de canal", "Taxa media efetiva dos canais", -totals.channel_fees, "negative"],
        ["Custo dos insumos", "Receita de cada produto x custo atual de polpa, frutas, caldas e complementos", -totals.cost_of_goods, "negative"],
        ["Custo de entregas", "Media dos fechamentos de entregadores", -totals.delivery_cost, "negative"],
        ["Retiradas da equipe", "Insumo que sai para a operacao sem virar venda, pela media do historico", -(totals.withdrawal_cost || 0), "negative"],
        ["Perdas previstas", "Quebra e vencimento, pela media do historico", -(totals.loss_cost || 0), "negative"],
        ["Despesas operacionais", "Media das despesas, sem comprar insumo duas vezes", -totals.operating_expenses, "negative"],
        ["Lucro operacional previsto", `Margem operacional: ${number.format(totals.operating_margin)}%`, totals.operating_profit, totals.operating_profit >= 0 ? "positive" : "negative"],
      ];
      document.getElementById("fc-operating-body").innerHTML = rows.map(([name, note, value, css]) => `
        <tr><td><strong>${escapeHtml(name)}</strong></td><td class="muted-value">${escapeHtml(note)}</td><td class="align-right ${css}"><strong>${currency.format(value)}</strong></td></tr>`).join("");
      document.getElementById("fc-operating-product-body").innerHTML = projecao.products.map((product) => `
        <tr><td><strong>${escapeHtml(product.name)}</strong></td><td class="align-right">${number.format(product.units)}</td><td class="align-right">${currency.format(product.revenue)}</td><td class="align-right muted-value">${currency.format(product.cost)}</td><td class="align-right ${product.contribution >= 0 ? "positive" : "negative"}"><strong>${currency.format(product.contribution)}</strong></td></tr>`).join("");
      aviso.innerHTML = (projecao.warnings || []).map((warning) => `<p class="panel-note">${escapeHtml(warning)}</p>`).join("");
    }
  }

  const painelProduto = document.getElementById("fc-product-panel");
  painelProduto.hidden = f.product_trend.length === 0;
  document.getElementById("fc-product-body").innerHTML = f.product_trend.map((p) => {
    const css = p.reading === "crescendo" ? "success" : p.reading === "caindo" ? "pending" : "neutral";
    const sinal = p.change_percent >= 0 ? "+" : "";
    return `<tr><td><strong>${escapeHtml(p.name)}</strong></td><td class="align-right muted-value">${number.format(p.before)}</td><td class="align-right">${number.format(p.after)}</td><td class="align-right"><strong>${sinal}${number.format(p.change_percent)}%</strong></td><td><span class="status-pill ${css}">${p.reading}</span></td></tr>`;
  }).join("");
  refreshIcons();
}

function renderFinance() {
  const report = state.operating;
  if (!report) return;
  const totals = report.totals;
  const result = totals.operating_profit;
  setText("#finance-result", result === null ? "—" : currency.format(result));
  setText("#finance-revenue", currency.format(totals.revenue));
  setText("#finance-cost", currency.format(totals.total_operating_cost));
  setText("#finance-coverage", `${number.format(report.cost_coverage)}% com custo medido`);
  const missingCost = totals.revenue_without_cost;
  const resultCopy = report.complete
    ? `${result >= 0 ? "Lucro" : "Prejuízo"} operacional de ${currency.format(Math.abs(result))} no período: venda menos taxas, insumos vendidos, perdas, entregas e despesas.`
    : `Lucro ainda não fechado: ${currency.format(missingCost)} em vendas importadas não têm itens nem custo. O teto do resultado, antes desse custo, é ${currency.format(totals.profit_upper_bound)}.`;
  setText("#finance-result-copy", resultCopy);

  const rows = [
    ["Faturamento", "Pedidos concluídos no período", totals.revenue, "positive"],
    ["Taxas de canal", "Comissões do iFood e demais canais", -totals.channel_fees, "negative"],
    ["Custo dos produtos vendidos", "Receitas dos produtos × unidades vendidas", -totals.cost_of_goods, "negative"],
    ["Perdas de estoque", "Quebras, vencimentos e acertos negativos", -totals.inventory_losses, "negative"],
    ["Custo de entregas", "Pagamentos de turnos e salário rateado", -totals.delivery_cost, "negative"],
    ["Despesas operacionais", "Despesas do período, exceto compras de insumo", -totals.operating_expenses, "negative"],
  ];
  if (totals.inventory_purchase_cash) rows.push(["Compra para estoque", "Sai do caixa, mas só vira custo quando o insumo é vendido", -totals.inventory_purchase_cash, "neutral"]);
  rows.push([report.complete ? "Lucro operacional" : "Teto antes do custo pendente", report.complete ? "Resultado fechado" : "Ainda falta custo de pedidos importados", report.complete ? result : totals.profit_upper_bound, report.complete ? (result >= 0 ? "positive" : "negative") : "pending"]);
  document.getElementById("finance-operating-body").innerHTML = rows.map(([name, note, value, css]) => `
    <tr><td><strong>${escapeHtml(name)}</strong></td><td class="muted-value">${escapeHtml(note)}</td><td class="align-right ${css}"><strong>${value >= 0 && name === "Faturamento" ? "+" : ""}${currency.format(value)}</strong></td></tr>`).join("");
  setText("#finance-operating-note", `Período de ${formatDate(report.from)} a ${formatDate(report.to)}. Compras de estoque ficam fora desta equação para não duplicar o custo dos insumos.`);
  // É dinheiro da empresa: o que falta para o número fechar tem de estar
  // visível, item a item, e não espremido numa frase corrida embaixo da conta.
  document.getElementById("finance-blockers-panel").hidden = !report.blockers.length;
  document.getElementById("finance-blockers").innerHTML = report.blockers.map((linha) => `
    <article class="insight attention"><span class="insight-icon"><i data-lucide="triangle-alert"></i></span><div><p>${escapeHtml(linha)}</p></div></article>`).join("");
  document.getElementById("finance-product-body").innerHTML = report.products.length ? report.products.map((product) => `
    <tr><td><strong>${escapeHtml(product.name)}</strong></td><td class="align-right">${number.format(product.units)}</td><td class="align-right">${currency.format(product.revenue)}</td><td class="align-right muted-value">${currency.format(product.cost)}</td><td class="align-right ${product.contribution >= 0 ? "positive" : "negative"}"><strong>${currency.format(product.contribution)}</strong></td><td class="align-right"><strong>${number.format(product.margin)}%</strong></td></tr>`).join("") : emptyRow(6, "Nenhum produto com itens detalhados no período.");

  const pending = state.expenses.filter(expense => expense.status === "Pendente").sort((a, b) => a.due_date.localeCompare(b.due_date));
  setText("#pending-count", `${pending.length} pendente${pending.length === 1 ? "" : "s"}`);
  document.getElementById("due-list").innerHTML = pending.length ? pending.slice(0, 4).map(expense => {
    const date = new Date(`${expense.due_date}T12:00:00`);
    return `<article class="due-item"><span class="due-date">${String(date.getDate()).padStart(2, "0")}<br>${date.toLocaleDateString("pt-BR", { month: "short" }).replace(".", "")}</span><div><strong>${escapeHtml(expense.description)}</strong><span>${escapeHtml(expense.category)}</span></div><b>${currency.format(expense.amount)}</b></article>`;
  }).join("") : '<p class="empty-state">Nenhuma despesa pendente.</p>';
  renderExpenseTable();
  renderExpenseSeries();
  refreshIcons();
}

// A lista de despesas é o extrato da empresa. Ela precisa responder três
// perguntas sem sair da tela: o que vence, quanto já saiu e quanto falta — por
// situação, categoria e mês. Sem filtro e sem soma, a resposta era contar linha
// a linha, e ninguém confere dinheiro assim.
function renderExpenseTable() {
  const filtros = lerFiltrosDeDespesa();
  const todas = [...state.expenses].sort((a, b) => b.due_date.localeCompare(a.due_date));
  const visiveis = todas.filter((expense) =>
    (filtros.status === "all" || expense.status === filtros.status)
    && (filtros.category === "all" || expense.category === filtros.category)
    && (filtros.month === "all" || String(expense.due_date).slice(0, 7) === filtros.month));

  const soma = (linhas) => linhas.reduce((total, linha) => total + linha.amount, 0);
  const pagas = visiveis.filter((linha) => linha.status === "Paga");
  const pendentes = visiveis.filter((linha) => linha.status === "Pendente");
  setText("#expenses-summary", visiveis.length
    ? `${visiveis.length} lançamento(s) · ${currency.format(soma(visiveis))} no total · ${currency.format(soma(pagas))} pago em ${pagas.length} · ${currency.format(soma(pendentes))} pendente em ${pendentes.length}.`
    : "Nenhuma despesa com esses filtros.");

  document.getElementById("expenses-body").innerHTML = visiveis.length ? visiveis.map(expense => `
    <tr><td><strong>${escapeHtml(expense.description)}</strong>${expense.recurring ? '<span class="row-note">fixa</span>' : ""}</td><td>${escapeHtml(expense.category)}</td><td>${escapeHtml(formatDate(expense.due_date))}</td><td><span class="status-pill ${expense.status === "Paga" ? "success" : "pending"}">${escapeHtml(expense.status)}</span></td><td class="align-right"><strong>${currency.format(expense.amount)}</strong></td><td class="align-right"><div class="row-actions">
      ${expense.status === "Pendente" ? `<button class="row-action" type="button" data-pay-expense="${expense.id}" title="Marcar como paga"><i data-lucide="circle-check"></i></button>` : ""}
      <button class="row-action" type="button" data-edit-expense="${expense.id}" title="Editar despesa"><i data-lucide="pencil"></i></button>
      <button class="row-action danger" type="button" data-delete-expense="${expense.id}" title="Excluir despesa"><i data-lucide="trash-2"></i></button>
    </div></td></tr>
  `).join("") : emptyRow(6, state.expenses.length ? "Nenhuma despesa com esses filtros." : "Nenhuma despesa cadastrada.");
  refreshIcons();
}

function lerFiltrosDeDespesa() {
  const valor = (id) => document.getElementById(id)?.value || "all";
  // As listas nascem do que existe: categoria e mês inventados no HTML
  // envelheceriam no primeiro lançamento fora do padrão.
  preencherFiltro("expense-filter-category", [...new Set(state.expenses.map((linha) => linha.category))].sort());
  preencherFiltro("expense-filter-month", [...new Set(state.expenses.map((linha) => String(linha.due_date).slice(0, 7)))].sort().reverse(), mesLegivel);
  return { status: valor("expense-filter-status"), category: valor("expense-filter-category"), month: valor("expense-filter-month") };
}

function preencherFiltro(id, valores, rotulo = (valor) => valor) {
  const select = document.getElementById(id);
  if (!select) return;
  const atual = select.value;
  const primeira = select.querySelector('option[value="all"]');
  const opcoes = `<option value="all">${primeira ? escapeHtml(primeira.textContent) : "Todas"}</option>`
    + valores.map((valor) => `<option value="${escapeHtml(valor)}">${escapeHtml(rotulo(valor))}</option>`).join("");
  if (select.innerHTML !== opcoes) select.innerHTML = opcoes;
  if (atual && valores.includes(atual)) select.value = atual;
}

function mesLegivel(chave) {
  const [ano, mes] = String(chave).split("-");
  return new Intl.DateTimeFormat("pt-BR", { month: "long", year: "numeric" }).format(new Date(Number(ano), Number(mes) - 1, 1));
}

// As regras que se repetem. É o que separa "quanto gastei" de "quanto vou
// gastar": o custo fixo do mês que vem está aqui, não numa média do passado.
function renderExpenseSeries() {
  const rows = state.expenseSeries || [];
  const ativas = rows.filter((row) => row.active);
  const mensal = ativas.reduce((soma, row) => soma + row.amount, 0);
  setText("#recurring-total", ativas.length
    ? `${currency.format(mensal)} por mês em ${ativas.length} despesa${ativas.length === 1 ? "" : "s"}`
    : "nenhuma cadastrada");
  document.getElementById("recurring-body").innerHTML = rows.length ? rows.map((row) => `
    <tr class="${row.active ? "" : "cancelled-row"}">
      <td><strong>${escapeHtml(row.description)}</strong>${row.active ? "" : '<span class="row-note">parou de repetir</span>'}</td>
      <td>${escapeHtml(row.category)}</td>
      <td class="align-right"><strong>${currency.format(row.amount)}</strong></td>
      <td>${row.next_due ? escapeHtml(formatDate(row.next_due)) : '<span class="muted-value">—</span>'}</td>
      <td class="muted-value">${row.ends_on ? escapeHtml(monthLabel(row.ends_on)) : "sem fim previsto"}</td>
      <td class="align-right">${number.format(row.released)}</td>
      <td class="align-right"><div class="row-actions">
        <button class="row-action" type="button" data-edit-recurring="${row.id}" title="Editar regra"><i data-lucide="pencil"></i></button>
        ${row.active ? `<button class="row-action danger" type="button" data-stop-recurring="${row.id}" title="Parar de repetir"><i data-lucide="ban"></i></button>` : ""}
      </div></td>
    </tr>`).join("") : emptyRow(7, "Nenhuma despesa se repete ainda. Use <b>Custo fixo</b> no topo da tela para lançar aluguel, equipe e contas de uma vez — sem essa base a projeção de lucro não fecha.");
}

function monthLabel(month) {
  return `${month.slice(5)}/${month.slice(0, 4)}`;
}

// A ficha técnica é a peça de cadastro que mais se espalha: dela saem margem,
// preço sugerido, promoção e o custo do que foi vendido. Um erro de unidade ali
// — grama cadastrada onde o estoque está em quilo — vira um custo mil vezes
// maior que percorre todos esses relatórios parecendo um número calculado.
// Encontrar isso olhando é impossível; por isso a tela aponta, com a linha
// culpada e a correção provável ao lado.
function renderRecipeAudit() {
  const auditoria = state.recipeAudit;
  const painel = document.getElementById("recipe-audit-panel");
  if (!painel) return;
  const suspeitos = auditoria?.suspects || [];
  painel.hidden = !suspeitos.length;
  if (!suspeitos.length) return;
  document.getElementById("recipe-audit-list").innerHTML = suspeitos.map((produto) => `
    <article class="insight attention"><span class="insight-icon"><i data-lucide="triangle-alert"></i></span><div>
      <strong>${escapeHtml(produto.name)} — a ficha soma ${currency.format(produto.recipe_cost)} e o produto é vendido a ${currency.format(produto.price)}</strong>
      <p>${number.format(produto.ratio)}× o preço de venda. As linhas que mais pesam:</p>
      <div class="value-rows">${produto.lines.map((linha) => `
        <div class="ladder-row"><span>${number.format(linha.quantity)} ${escapeHtml(linha.unit || "")} de ${escapeHtml(linha.name)}${linha.diagnosis ? `<small class="muted-value"> ${escapeHtml(linha.diagnosis)}</small>` : ""}</span><b>${currency.format(linha.cost)}</b></div>`).join("")}</div>
    </div></article>`).join("");
}

function renderProducts() {
  document.getElementById("product-grid").innerHTML = state.products.map(product => `
    <article class="product-card ${product.image ? "featured-product" : ""} ${product.active === false ? "inactive-product" : ""}">
      <div class="product-top"><span class="category">${escapeHtml(product.category)}</span>${product.line && product.line !== "Classico" ? `<span class="line-pill line-${escapeHtml(String(product.line).toLowerCase())}">${escapeHtml(product.line)}</span>` : ""}${product.active === false ? '<span class="line-pill muted">fora do cardápio</span>' : ""}</div>
      <h2>${escapeHtml(product.name)}</h2>
      <span class="price">${currency.format(product.price)}</span>
      ${product.image ? `<img class="product-image" src="${product.image}" alt="${escapeHtml(product.name)}" />` : ""}
      <div class="product-footer"><span class="product-cost">Custo da receita<b>${currency.format(product.cost)}</b></span><span class="product-margin">Margem<strong>${number.format(product.margin)}%</strong></span></div>
      <div class="product-actions">
        <button class="text-button" type="button" data-dossier="${product.id}"><i data-lucide="file-search"></i>Dossiê de custo</button>
        <span class="row-actions">
          <button class="row-action" type="button" data-edit-product="${product.id}" title="Editar produto"><i data-lucide="pencil"></i></button>
          ${product.active === false
            ? `<button class="row-action danger" type="button" data-purge-product="${product.id}" title="Excluir do cadastro"><i data-lucide="trash-2"></i></button>`
            : `<button class="row-action danger" type="button" data-delete-product="${product.id}" title="Tirar do cardápio"><i data-lucide="trash-2"></i></button>`}
        </span>
      </div>
    </article>
  `).join("");
  refreshIcons();
}

function renderAddons() {
  const rows = state.addons;
  document.getElementById("addon-body").innerHTML = rows.length ? rows.map((addon) => {
    const semInsumo = !addon.inventory_name;
    return `<tr class="${addon.active === false ? "cancelled-row" : ""}">
      <td><strong>${escapeHtml(addon.name)}</strong><span class="row-note">${escapeHtml(addon.category || "Complemento")}${addon.active === false ? " · fora do balcão" : ""}</span></td>
      <td>${semInsumo ? '<span class="status-pill danger">insumo removido</span>' : escapeHtml(addon.inventory_name)}</td>
      <td class="align-right">${number.format(addon.quantity)} ${escapeHtml(addon.unit || "")}</td>
      <td class="align-right">${addon.price ? currency.format(addon.price) : '<span class="muted-value">cortesia</span>'}</td>
      <td class="align-right muted-value">${currency.format(addon.cost)}</td>
      <td class="align-right ${addon.contribution >= 0 ? "positive" : "negative"}"><strong>${currency.format(addon.contribution)}</strong></td>
      <td class="align-right">${addon.portions_available === null || addon.portions_available === undefined ? "—" : `${number.format(addon.portions_available)} porções`}</td>
      <td class="align-right"><div class="row-actions">
        <button class="row-action" type="button" data-edit-addon="${addon.id}" title="Editar complemento"><i data-lucide="pencil"></i></button>
        <button class="row-action danger" type="button" data-delete-addon="${addon.id}" title="Remover complemento"><i data-lucide="trash-2"></i></button>
      </div></td></tr>`;
  }).join("") : emptyRow(8, "Nenhum complemento cadastrado. Sem eles, a banana e a calda saem da prateleira sem aparecer no custo do pedido.");
  refreshIcons();
}

// Cruzamento: o que cada venda tirou do estoque, separando o que a receita
// mandou tirar do que o cliente pediu por cima.
function renderCrossing() {
  const data = state.crossing;
  if (!data) return;
  const totals = data.totals;
  setText("#crossing-coverage", `${number.format(data.coverage)}% das vendas com itens`);
  setText("#crossing-note", `${data.orders} pedido(s) e ${number.format(data.units)} unidades nos últimos ${data.period_days} dias. ${data.note}`);
  setText("#crossing-recipe-cost", currency.format(totals.recipe_cost));
  setText("#crossing-addon-cost", currency.format(totals.addon_cost));
  setText("#crossing-addon-revenue", totals.addon_revenue
    ? `${currency.format(totals.addon_revenue)} cobrados — sobra ${currency.format(totals.addon_contribution)}`
    : "nenhum complemento cobrado no período");
  setText("#crossing-cost-share", `${number.format(totals.cost_share)}%`);
  setText("#crossing-contribution", `sobra ${currency.format(totals.contribution)} depois do insumo`);

  document.getElementById("crossing-body").innerHTML = data.items.length ? data.items.map((item) => `
    <tr>
      <td><strong>${escapeHtml(item.name)}</strong><span class="row-note">${escapeHtml(item.category)}</span></td>
      <td class="align-right">${number.format(item.recipe_quantity)} ${escapeHtml(item.unit)}</td>
      <td class="align-right">${item.addon_quantity ? `${number.format(item.addon_quantity)} ${escapeHtml(item.unit)} <span class="muted-value">(${number.format(item.addon_share)}%)</span>` : '<span class="muted-value">—</span>'}</td>
      <td class="align-right"><strong>${number.format(item.quantity)} ${escapeHtml(item.unit)}</strong></td>
      <td class="align-right">${currency.format(item.cost)}</td>
      <td class="align-right ${item.in_stock < 0 ? "negative" : "muted-value"}">${number.format(item.in_stock)} ${escapeHtml(item.unit)}</td>
      <td>${item.days_left === null ? '<span class="muted-value">—</span>' : `<span class="status-pill ${item.days_left <= 3 ? "danger" : item.days_left <= 7 ? "pending" : "success"}">${number.format(item.days_left)} dias</span>`}</td>
    </tr>`).join("") : emptyRow(7, "Nenhuma venda com itens detalhados no período.");

  document.getElementById("crossing-addon-body").innerHTML = data.addons.length ? data.addons.map((addon) => `
    <tr>
      <td><strong>${escapeHtml(addon.name)}</strong>${addon.registered ? "" : '<span class="row-note">fora do cadastro atual</span>'}</td>
      <td class="align-right">${number.format(addon.portions)}</td>
      <td class="align-right muted-value">${number.format(addon.quantity)} ${escapeHtml(addon.unit || "")}</td>
      <td class="align-right">${currency.format(addon.revenue)}</td>
      <td class="align-right muted-value">${currency.format(addon.cost)}</td>
      <td class="align-right ${addon.contribution >= 0 ? "positive" : "negative"}"><strong>${currency.format(addon.contribution)}</strong></td>
      <td class="align-right">${number.format(addon.attach)}% dos copos</td>
    </tr>`).join("") : emptyRow(7, "Nenhum complemento marcado nas vendas do período.");

  document.getElementById("crossing-blockers").innerHTML = data.blockers.map((blocker) => `
    <article class="insight-item attention"><i data-lucide="circle-alert"></i><div><strong>Falta para fechar a conta</strong><p>${escapeHtml(blocker)}</p></div></article>`).join("");
  refreshIcons();
}

function populateAddonInventory() {
  const select = document.getElementById("addon-inventory");
  const current = select.value;
  select.innerHTML = state.inventory.map((item) => `<option value="${item.id}">${escapeHtml(item.name)} (${escapeHtml(item.unit)} · ${currency.format(item.unit_cost)})</option>`).join("");
  if (current && state.inventory.some((item) => item.id === current)) select.value = current;
  refreshAddonPreview();
}

function refreshAddonPreview() {
  const form = document.getElementById("addon-form");
  const item = state.inventory.find((row) => row.id === form.elements.inventory_id.value);
  const portion = Number(form.elements.quantity.value || 0);
  const price = Number(form.elements.price.value || 0);
  setText("#addon-unit-hint", item ? `Quanto sai do estoque por porção, em ${item.unit}.` : "Quanto sai do estoque por porção.");
  if (!item || portion <= 0) {
    setText("#addon-preview", "");
    return;
  }
  const cost = item.unit_cost * portion;
  const cabem = item.quantity > 0 ? Math.floor(item.quantity / portion) : 0;
  setText(
    "#addon-preview",
    `Cada porção custa ${currency.format(cost)} e deixa ${currency.format(price - cost)}. O saldo de hoje (${number.format(item.quantity)} ${item.unit}) dá para ${number.format(cabem)} porções.`,
  );
}

async function submitAddon(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const addonId = form.get("addon_id");
  const payload = {
    name: form.get("name"),
    inventory_id: form.get("inventory_id"),
    quantity: Number(form.get("quantity")),
    price: Number(form.get("price") || 0),
    category: form.get("category") || "Complemento",
    active: form.get("active") === "on",
  };
  if (addonId) {
    await submitData(`/api/addons/${addonId}`, payload, "Complemento atualizado.", "PATCH");
    return;
  }
  await submitData("/api/addons", payload, "Complemento cadastrado.");
}

function renderMedia() {
  const summary = state.mediaSummary;
  if (!summary) return;
  const reviewCount = summary.needs_review;
  const fileBadge = document.getElementById("nav-files-count");
  fileBadge.hidden = reviewCount === 0;
  fileBadge.textContent = reviewCount;
  document.getElementById("media-summary").innerHTML = `
    <article class="media-stat"><i data-lucide="files"></i><div><p>Arquivos no acervo</p><strong>${summary.total}</strong></div></article>
    <article class="media-stat attention"><i data-lucide="circle-alert"></i><div><p>Precisam de revisão</p><strong>${reviewCount}</strong></div></article>
    <article class="media-stat"><i data-lucide="image"></i><div><p>Imagens e comprovantes</p><strong>${summary.images}</strong></div></article>`;
  const query = document.getElementById("media-search")?.value.trim().toLocaleLowerCase("pt-BR") || "";
  const records = state.media.filter(record => {
    const matchesFilter = state.mediaFilter === "all" || record.kind === state.mediaFilter;
    const searchable = `${record.title} ${record.original_name} ${record.category} ${(record.tags || []).join(" ")}`.toLocaleLowerCase("pt-BR");
    return matchesFilter && searchable.includes(query);
  });
  document.getElementById("media-grid").innerHTML = records.length ? records.map(record => {
    const extension = record.original_name.split(".").pop().toUpperCase();
    const isImage = record.kind === "image";
    const visual = isImage
      ? `<div class="media-visual"><img src="/api/media/${record.id}/content" alt="${escapeHtml(record.title)}" loading="lazy" /><span class="file-extension">${extension}</span></div>`
      : `<div class="media-visual ${record.kind}"><i data-lucide="${mediaIcon(record.kind)}"></i><span class="file-extension">${extension}</span></div>`;
    const tags = (record.tags || []).slice(0, 2).map(tag => `<span>${escapeHtml(tag)}</span>`).join("");
    return `<article class="media-card">${visual}<div class="media-card-body"><div class="media-card-top"><h2 title="${escapeHtml(record.title)}">${escapeHtml(record.title)}</h2><button class="media-organize-button" type="button" data-open-media="${record.id}"><i data-lucide="pencil"></i><span>Organizar</span></button></div><div class="media-card-meta"><span class="media-category ${record.status === "Revisar" ? "review" : ""}">${escapeHtml(record.category)}</span>${record.entity_type ? `<span class="media-category">${escapeHtml(linkLabel(record.entity_type))}</span>` : ""}</div>${tags ? `<div class="media-tags">${tags}</div>` : ""}<div class="media-card-bottom"><span>${formatBytes(record.size)}</span><span>${record.source === "upload" ? "Enviado" : "Sincronizado"}</span></div></div></article>`;
  }).join("") : '<p class="empty-state">Nenhum arquivo corresponde ao filtro atual.</p>';
  refreshIcons();
}

function bindNavigation() {
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-view-target]");
    if (!trigger) return;
    activateView(trigger.dataset.viewTarget);
  });
  window.addEventListener("hashchange", () => {
    const target = window.location.hash.slice(1);
    if (document.getElementById(target)) activateView(target, false);
  });
}

function activateView(target, updateHash = true) {
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === target));
  // O estado da integração é buscado ao abrir a tela, e não no carregamento
  // geral: o iFood fora do ar não pode derrubar o painel inteiro.
  if (target === "settings") { loadIFood(); loadAnotaAi(); }
  if (target === "delivery") loadDispatch();
  document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.viewTarget === target));
  setMobileNavigation(false);
  if (updateHash) history.replaceState(null, "", `#${target}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function bindModals() {
  document.addEventListener("click", (event) => {
    const open = event.target.closest("[data-modal]");
    // O menu "Mais" precisa fechar junto com o clique que abre o modal, senão
    // ele fica aberto por cima e o gestor volta para uma lista suspensa que já
    // cumpriu o papel.
    if (open) { open.closest("details.more-actions")?.removeAttribute("open"); openModal(open.dataset.modal); }
    if (event.target.closest("[data-close-modal]")) closeModal();
  });
  document.getElementById("modal-backdrop").addEventListener("click", closeModal);
  // Clique fora fecha o menu aberto, como todo menu suspenso faz.
  document.addEventListener("click", (event) => {
    document.querySelectorAll("details.more-actions[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });
  document.querySelectorAll("dialog").forEach(dialog => dialog.addEventListener("cancel", () => closeModal()));
}

function openModal(name) {
  const dialog = document.getElementById(`${name}-modal`);
  if (!dialog) return;
  if (name === "sale") { clearCart(); renderSaleProducts(); renderCart(); populateSaleZones(); toggleSaleDelivery(); }
  if (name === "addon") { document.getElementById("addon-form").reset(); populateAddonInventory(); }
  if (name === "adjust") populateAdjustSelect();
  if (name === "purchase") { openPurchase(); return; }
  if (name === "withdrawal") { openWithdrawal(); return; }
  if (name === "production") { openProduction(); return; }
  if (name === "bulk-count") { openBulkCount(); return; }
  if (name === "product") { document.getElementById("product-form").reset(); document.getElementById("recipe-lines").innerHTML = ""; addRecipeLine(); }
  if (name === "fixed-cost") renderFixedCostSetup();
  if (name === "expense") { document.getElementById("expense-form").reset(); document.querySelector('#expense-form [name="due_date"]').value = dateInputValue(new Date()); toggleExpenseRepeat(); }
  dialog.showModal();
  document.getElementById("modal-backdrop").hidden = false;
  refreshIcons();
}

function closeModal() {
  document.querySelectorAll("dialog[open]").forEach(dialog => dialog.close());
  document.getElementById("modal-backdrop").hidden = true;
}

// Abre um modal que quem chamou já montou. openModal parte do nome do botão;
// esta versão parte do id, que é do que precisa quem prepara o formulário
// antes de mostrá-lo.
function openDialog(id) {
  const dialog = document.getElementById(id);
  if (!dialog) return;
  if (!dialog.open) dialog.showModal();
  document.getElementById("modal-backdrop").hidden = false;
  refreshIcons();
}

function bindForms() {
  document.getElementById("sale-products").addEventListener("click", (event) => {
    const button = event.target.closest("[data-cart-action]");
    if (!button) return;
    const productId = button.dataset.productId;
    const current = state.cart.get(productId) || 0;
    const amount = button.dataset.cartAction === "add" ? current + 1 : current - 1;
    if (amount > 0) state.cart.set(productId, amount); else state.cart.delete(productId);
    renderSaleProducts();
    renderCart();
  });
  // O complemento é escolhido na linha já montada: clicar percorre 0, 1, 2, 3
  // porções e volta a zero. Um passo a passo por complemento encheria o resumo
  // do pedido de botões e deixaria a venda mais lenta que o balcão.
  document.getElementById("cart-lines").addEventListener("click", (event) => {
    const chip = event.target.closest("[data-addon-toggle]");
    if (!chip) return;
    const { productId, addonToggle } = chip.dataset;
    const chosen = state.cartAddons.get(productId) || new Map();
    const portions = ((chosen.get(addonToggle) || 0) + 1) % 4;
    if (portions) chosen.set(addonToggle, portions); else chosen.delete(addonToggle);
    if (chosen.size) state.cartAddons.set(productId, chosen); else state.cartAddons.delete(productId);
    renderCart();
  });
  document.getElementById("sale-form").addEventListener("submit", submitSale);
  document.getElementById("sale-is-delivery").addEventListener("change", toggleSaleDelivery);
  document.getElementById("sale-zone").addEventListener("change", applySaleZoneFee);
  document.getElementById("addon-form").addEventListener("submit", submitAddon);
  document.getElementById("addon-inventory").addEventListener("change", refreshAddonPreview);
  document.querySelector('#addon-form [name="quantity"]').addEventListener("input", refreshAddonPreview);
  document.querySelector('#addon-form [name="price"]').addEventListener("input", refreshAddonPreview);
  document.getElementById("expense-form").addEventListener("submit", submitExpense);
  // O campo "repetir até" só existe quando a despesa se repete.
  document.getElementById("expense-recurring").addEventListener("change", toggleExpenseRepeat);
  document.getElementById("fixed-cost-form").addEventListener("submit", submitFixedCost);
  document.getElementById("fixed-cost-list").addEventListener("input", refreshFixedCostSum);
  ["expense-filter-status", "expense-filter-category", "expense-filter-month"].forEach((id) => {
    document.getElementById(id).addEventListener("change", renderExpenseTable);
  });
  document.getElementById("import-anota-report").addEventListener("click", () => document.getElementById("anota-report-file").click());
  document.getElementById("anota-report-file").addEventListener("change", importAnotaReport);
  document.getElementById("import-anota-items").addEventListener("click", () => document.getElementById("anota-items-file").click());
  document.getElementById("anota-items-file").addEventListener("change", importAnotaItems);
  document.getElementById("adjust-form").addEventListener("submit", submitAdjustment);
  document.getElementById("bulk-count-form").addEventListener("submit", submitBulkCount);
  document.getElementById("bulk-count-body").addEventListener("input", refreshBulkCountPreview);
  document.getElementById("bulk-count-body").addEventListener("change", refreshBulkCountPreview);
  document.getElementById("production-form").addEventListener("submit", submitProduction);
  document.getElementById("production-recipe").addEventListener("change", applyProductionRecipe);
  document.getElementById("add-production-line").addEventListener("click", () => { addProductionLine("production-lines"); refreshProductionPreview(); });
  document.getElementById("production-lines").addEventListener("click", (event) => {
    if (!event.target.closest("[data-remove-production]")) return;
    if (document.querySelectorAll("#production-lines .production-line").length === 1) {
      toast("A fabricação precisa de pelo menos um insumo.", "error");
      return;
    }
    event.target.closest(".production-line").remove();
    refreshProductionPreview();
  });
  document.getElementById("production-lines").addEventListener("input", refreshProductionPreview);
  document.getElementById("production-override").addEventListener("change", refreshProductionPreview);
  ["output_inventory_id", "output_quantity"].forEach((campo) => {
    document.querySelector(`#production-form [name="${campo}"]`).addEventListener("input", refreshProductionPreview);
  });
  document.getElementById("withdrawal-form").addEventListener("submit", submitWithdrawal);
  document.getElementById("add-withdrawal-line").addEventListener("click", addWithdrawalLine);
  document.getElementById("withdrawal-lines").addEventListener("click", (event) => {
    if (!event.target.closest("[data-remove-withdrawal]")) return;
    // Retirada sem nenhuma linha não existe: apagar a última deixaria o
    // funcionário diante de um formulário que só pode ser recusado.
    if (document.querySelectorAll("#withdrawal-lines .withdrawal-line").length === 1) {
      toast("A retirada precisa de pelo menos um insumo.", "error");
      return;
    }
    event.target.closest(".withdrawal-line").remove();
    refreshWithdrawalPreview();
  });
  document.getElementById("withdrawal-lines").addEventListener("input", refreshWithdrawalPreview);
  // Trocar o tamanho é uma escolha, não um palpite: a partir daqui a linha para
  // de abrir na unidade de manuseio por conta própria e respeita o que o
  // operador escolheu, mesmo depois de ele trocar o insumo.
  document.getElementById("withdrawal-lines").addEventListener("change", (event) => {
    const unidade = event.target.closest("[data-withdrawal-unit]");
    if (unidade) unidade.dataset.chosen = "1";
    refreshWithdrawalPreview();
  });
  document.getElementById("withdrawal-override").addEventListener("change", refreshWithdrawalPreview);
  document.getElementById("purchase-form").addEventListener("submit", submitPurchase);
  document.getElementById("purchase-inventory").addEventListener("change", (event) => {
    applyPurchasePack(state.inventory.find((row) => row.id === event.target.value));
    refreshPurchasePreview();
  });
  ["purchase_quantity", "purchase_unit", "units_per_purchase", "purchase_unit_cost"].forEach((field) => document.querySelector(`#purchase-form [name="${field}"]`).addEventListener("input", refreshPurchasePreview));
  document.getElementById("inventory-form").addEventListener("submit", submitInventory);
  document.getElementById("inventory-pack-cost").addEventListener("input", () => refreshInventoryPack("pack"));
  document.getElementById("inventory-unit-cost").addEventListener("input", () => refreshInventoryPack("unit"));
  ["unit", "pack_unit", "pack_size"].forEach((campo) => {
    document.querySelector(`#inventory-form [name="${campo}"]`).addEventListener("input", () => refreshInventoryPack());
  });
  document.getElementById("product-form").addEventListener("submit", submitProduct);
  document.getElementById("add-recipe-line").addEventListener("click", addRecipeLine);
  document.getElementById("recipe-lines").addEventListener("click", (event) => {
    if (event.target.closest("[data-remove-recipe]")) event.target.closest(".recipe-line").remove();
  });
  document.getElementById("media-search").addEventListener("input", renderMedia);
  document.querySelector(".media-filters").addEventListener("click", (event) => {
    const button = event.target.closest("[data-media-filter]");
    if (!button) return;
    state.mediaFilter = button.dataset.mediaFilter;
    document.querySelectorAll("[data-media-filter]").forEach(item => item.classList.toggle("active", item === button));
    renderMedia();
  });
  document.getElementById("media-grid").addEventListener("click", (event) => {
    const button = event.target.closest("[data-open-media]");
    if (button) openMedia(button.dataset.openMedia);
  });
  document.getElementById("sync-media").addEventListener("click", syncMedia);
  document.getElementById("media-upload").addEventListener("change", uploadMedia);
  document.getElementById("media-form").addEventListener("submit", submitMedia);
  document.getElementById("media-entity-type").addEventListener("change", (event) => populateEntityTargets(event.target.value));
  document.getElementById("anotaai-import-confirm").addEventListener("click", importarCardapioAnotaAi);
  bindAnotaAi();
}

function renderSaleProducts() {
  const container = document.getElementById("sale-products");
  container.innerHTML = state.products.filter(product => product.active).map(product => {
    const quantity = state.cart.get(product.id) || 0;
    return `<article class="sale-product ${quantity ? "selected" : ""}"><div><strong>${escapeHtml(product.name)}</strong><small>${currency.format(product.price)}</small></div><div class="stepper"><button type="button" data-cart-action="subtract" data-product-id="${product.id}" title="Remover uma unidade"><i data-lucide="minus"></i></button><span>${quantity}</span><button type="button" data-cart-action="add" data-product-id="${product.id}" title="Adicionar uma unidade"><i data-lucide="plus"></i></button></div></article>`;
  }).join("");
  refreshIcons();
}

function clearCart() {
  state.cart.clear();
  state.cartAddons.clear();
}

// Preço e custo da linha com o que foi marcado por cima. O custo aparece na
// venda de propósito: é o momento em que dá para perceber que a terceira dose
// de calda comeu a margem do copo.
function cartLine(productId, quantity) {
  const product = state.products.find((row) => row.id === productId);
  if (!product) return null;
  const chosen = state.cartAddons.get(productId) || new Map();
  const addons = [...chosen.entries()]
    .map(([id, portions]) => ({ addon: state.addons.find((row) => row.id === id), portions }))
    .filter((row) => row.addon);
  const addonsPrice = addons.reduce((sum, row) => sum + row.addon.price * row.portions, 0);
  const addonsCost = addons.reduce((sum, row) => sum + row.addon.cost * row.portions, 0);
  return {
    product,
    quantity,
    addons,
    total: (product.price + addonsPrice) * quantity,
    cost: (product.cost + addonsCost) * quantity,
  };
}

function renderCart() {
  const rows = [...state.cart.entries()].map(([id, quantity]) => cartLine(id, quantity)).filter(Boolean);
  const total = rows.reduce((sum, row) => sum + row.total, 0);
  const available = state.addons.filter((addon) => addon.active !== false);
  document.getElementById("cart-hint").hidden = !rows.length || !available.length;
  document.getElementById("cart-lines").innerHTML = rows.length ? rows.map(row => {
    const chosen = state.cartAddons.get(row.product.id) || new Map();
    const chips = available.map((addon) => {
      const portions = chosen.get(addon.id) || 0;
      const price = addon.price ? ` ${currency.format(addon.price)}` : "";
      return `<button type="button" class="addon-chip ${portions ? "active" : ""}" data-addon-toggle="${addon.id}" data-product-id="${row.product.id}" title="${escapeHtml(addon.inventory_name || "")}">${escapeHtml(addon.name)}${portions > 1 ? ` ×${portions}` : ""}${portions ? "" : price}</button>`;
    }).join("");
    const detail = row.addons.length
      ? `<small class="cart-line-addons">${row.addons.map((item) => `${escapeHtml(item.addon.name)}${item.portions > 1 ? ` ×${item.portions}` : ""}`).join(", ")}</small>`
      : "";
    return `<div class="cart-line-block">
      <div class="cart-line"><span>${row.quantity}x ${escapeHtml(row.product.name)}${detail}</span><strong>${currency.format(row.total)}</strong></div>
      ${chips ? `<div class="addon-chips">${chips}</div>` : ""}
      <small class="cart-line-cost">Custo do insumo nesta linha: ${currency.format(row.cost)}</small>
    </div>`;
  }).join("") : '<p class="empty-state">Selecione um produto ao lado.</p>';
  setText("#cart-total", currency.format(total));
}

async function submitSale(event) {
  event.preventDefault();
  if (!state.cart.size) return toast("Selecione pelo menos um produto.", "error");
  const form = new FormData(event.currentTarget);
  const payload = {
    items: [...state.cart.entries()].map(([product_id, quantity]) => ({
      product_id,
      quantity,
      addons: [...(state.cartAddons.get(product_id) || new Map()).entries()].map(([addon_id, portions]) => ({ addon_id, quantity: portions })),
    })),
    channel: form.get("channel"),
    payment_method: form.get("payment_method"),
    customer_name: form.get("customer_name") || null,
    discount: Number(form.get("discount") || 0),
  };
  // Campo vazio significa "agora"; preenchido, permite cadastrar o movimento
  // de outro dia sem que ele entre no histórico com a data de hoje.
  const occurred = form.get("occurred_at");
  if (occurred) payload.occurred_at = occurred;
  // Balcão é a maioria: os campos de entrega só valem quando ela é marcada.
  payload.is_delivery = form.get("is_delivery") === "on";
  payload.delivery_fee = payload.is_delivery ? Number(form.get("delivery_fee") || 0) : 0;
  payload.courier_id = payload.is_delivery ? (form.get("courier_id") || null) : null;
  payload.delivery_zone_id = payload.is_delivery ? (form.get("delivery_zone_id") || null) : null;
  payload.delivery_address = payload.is_delivery ? (form.get("delivery_address") || null) : null;
  const orderId = form.get("order_id");
  if (orderId) {
    await submitData(`/api/orders/${orderId}`, payload, "Pedido corrigido e estoque ajustado.", "PATCH");
    return;
  }
  await submitData("/api/orders", payload, "Venda registrada e estoque atualizado.");
}

// As contas que uma loja de acai paga todo mes, no vocabulario da propria
// planilha da loja. Nao ha de onde importar isso — o aluguel esta no contrato,
// nao num relatorio — entao o que da para fazer e reduzir a digitacao a uma
// tela so, uma vez.
const CUSTO_FIXO_SUGERIDO = [
  { description: "Funcionários", category: "Equipe", day: 5, note: "Folha do mês, incluindo encargos." },
  { description: "Entregadores", category: "Entregas", day: 5, note: "Só o valor fixo mensal. Diária e valor por entrega já entram pelo módulo de Entregas — lançar aqui contaria duas vezes." },
  { description: "Aluguel", category: "Fixo", day: 5, note: "Ponto comercial." },
  { description: "MEI", category: "Taxas", day: 20, note: "DAS mensal." },
  { description: "Luz", category: "Fixo", day: 10, note: "Use a média das últimas contas." },
  { description: "Internet", category: "Fixo", day: 10, note: "" },
  { description: "Segurança", category: "Fixo", day: 10, note: "Alarme, monitoramento." },
  { description: "Marketing", category: "Marketing", day: 5, note: "Material, brindes, parcerias." },
  { description: "Tráfego pago", category: "Marketing", day: 5, note: "Anúncios no Instagram e no Google." },
  { description: "Anota Aí", category: "Taxas", day: 5, note: "Mensalidade da plataforma." },
];

function renderFixedCostSetup() {
  const registradas = new Map(
    (state.expenseSeries || [])
      .filter((row) => row.active)
      .map((row) => [row.description.toLocaleLowerCase("pt-BR"), row]),
  );
  document.querySelector('#fixed-cost-form [name="starts_on"]').value = new Date().toISOString().slice(0, 7);
  document.getElementById("fixed-cost-list").innerHTML = CUSTO_FIXO_SUGERIDO.map((linha, indice) => {
    const existente = registradas.get(linha.description.toLocaleLowerCase("pt-BR"));
    const nota = existente ? "Já se repete todo mês. Edite pela tabela de despesas fixas." : linha.note;
    return `<div class="fixed-cost-row ${existente ? "done" : ""}">
      <span class="fixed-cost-name">${escapeHtml(linha.description)}${nota ? `<small>${escapeHtml(nota)}</small>` : ""}</span>
      <label>Valor por mês<input type="number" min="0" step="0.01" placeholder="0,00" data-fixed-index="${indice}" value="${existente ? existente.amount : ""}" ${existente ? "disabled" : ""} /></label>
      <label>Dia<input type="number" min="1" max="31" value="${existente ? existente.day : linha.day}" data-fixed-day="${indice}" ${existente ? "disabled" : ""} /></label>
    </div>`;
  }).join("");
  refreshFixedCostSum();
}

function refreshFixedCostSum() {
  const total = [...document.querySelectorAll("#fixed-cost-list [data-fixed-index]")]
    .reduce((soma, campo) => soma + (Number(campo.value) || 0), 0);
  setText("#fixed-cost-sum", currency.format(total));
}

async function submitFixedCost(event) {
  event.preventDefault();
  const items = [];
  document.querySelectorAll("#fixed-cost-list [data-fixed-index]").forEach((campo) => {
    if (campo.disabled) return;
    const valor = Number(campo.value);
    if (!(valor > 0)) return;
    const linha = CUSTO_FIXO_SUGERIDO[Number(campo.dataset.fixedIndex)];
    const dia = document.querySelector(`#fixed-cost-list [data-fixed-day="${campo.dataset.fixedIndex}"]`);
    items.push({ description: linha.description, category: linha.category, amount: valor, day: Number(dia.value) || linha.day });
  });
  if (!items.length) return toast("Preencha ao menos uma conta para montar o custo fixo.", "error");
  await submitData("/api/expenses/recurring", {
    items,
    starts_on: document.querySelector('#fixed-cost-form [name="starts_on"]').value || null,
    past_paid: document.querySelector('#fixed-cost-form [name="past_paid"]').checked,
  }, `${items.length} despesa(s) passam a ser lançadas todo mês.`);
}

function toggleExpenseRepeat() {
  const repete = document.getElementById("expense-recurring").checked;
  document.querySelectorAll(".expense-repeat-field").forEach((campo) => { campo.hidden = !repete; });
  if (!repete) document.querySelector('#expense-form [name="repeat_until"]').value = "";
}

async function submitExpense(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  const expenseId = payload.expense_id;
  delete payload.expense_id;
  payload.amount = Number(payload.amount);
  payload.recurring = form.get("recurring") === "on";
  // String vazia viraria um mês inválido no servidor; ausente significa "sem fim".
  payload.repeat_until = payload.recurring && payload.repeat_until ? payload.repeat_until : null;
  if (expenseId) {
    await submitData(`/api/expenses/${expenseId}`, payload, "Despesa atualizada.", "PATCH");
    return;
  }
  await submitData("/api/expenses", payload, "Despesa lançada no financeiro.");
}

async function importAnotaReport(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  const body = new FormData();
  body.append("report", file);
  try {
    const result = await api("/api/imports/anota-ai", { method: "POST", body });
    const detail = `${number.format(result.criados)} pedido(s) importado(s)${result.ignorados ? `; ${number.format(result.ignorados)} já existia(m)` : ""}.`;
    toast(detail, "success");
    await loadAll();
  } catch (error) {
    toast(error.message || "Não foi possível importar o relatório da Anota AI.", "error");
  } finally {
    event.target.value = "";
  }
}

// O segundo relatório da Anota AI: o que foi vendido em cada pedido.
//
// O relatório de pedidos traz o faturamento e não traz os itens, e é essa falta
// que deixa custo, margem por produto, planejamento e previsão sem base. O
// servidor já sabia fechar essa lacuna desde sempre — faltava o botão. Sem ele,
// a única forma de usar a rota era pela linha de comando, e nenhuma loja faz
// isso.
async function importAnotaItems(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  const body = new FormData();
  body.append("report", file);
  const botao = document.getElementById("import-anota-items");
  botao.disabled = true;
  try {
    const resultado = await api("/api/imports/anota-ai/items", { method: "POST", body });
    mostrarResultadoDosItens(resultado);
    await loadAll();
  } catch (error) {
    toast(error.message || "Não foi possível importar o relatório de itens.", "error");
  } finally {
    botao.disabled = false;
    event.target.value = "";
  }
}

// O resultado é uma lista de tarefas, não um aviso que some em quatro segundos:
// cada nome que não casou é um produto para cadastrar, e é ele que ainda está
// segurando o custo daqueles pedidos.
function mostrarResultadoDosItens(resultado) {
  const detalhados = resultado.pedidos_detalhados || 0;
  const resumo = document.getElementById("import-items-summary");
  resumo.textContent = detalhados
    ? `${number.format(detalhados)} pedido(s) ganharam os itens vendidos, de ${number.format(resultado.itens_lidos || 0)} linha(s) lidas. O custo e a margem desses pedidos passam a ser calculados pela ficha técnica.`
    : `Nenhum pedido foi atualizado, de ${number.format(resultado.itens_lidos || 0)} linha(s) lidas. Confira se este é o relatório de itens e se os pedidos correspondentes já foram importados.`;
  resumo.className = `modal-result full ${detalhados ? "positive" : "negative"}`;

  const semCadastro = resultado.produtos_sem_cadastro || [];
  const blocos = [];
  if (semCadastro.length) {
    blocos.push(`<article class="insight attention"><span class="insight-icon"><i data-lucide="triangle-alert"></i></span><div>
      <strong>${semCadastro.length} produto(s) da Anota AI não existem no seu cardápio</strong>
      <p>Enquanto eles não forem cadastrados com este nome, os pedidos que os contêm continuam sem custo. O número ao lado é quanto cada um vendeu.</p>
      <div class="value-rows">${semCadastro.map(([nome, unidades]) => `<div class="ladder-row"><span>${escapeHtml(nome)}</span><b>${number.format(unidades)} un</b></div>`).join("")}</div>
    </div></article>`);
  }
  const pendencias = [
    [resultado.pedidos_nao_encontrados, "pedido(s) do relatório de itens não existem aqui. Importe primeiro o relatório de pedidos do mesmo período."],
    [resultado.pedidos_ambiguos, "número(s) de pedido aparecem mais de uma vez e não dá para saber a qual pertence cada item."],
  ];
  pendencias.forEach(([lista, frase]) => {
    if (lista?.length) {
      blocos.push(`<article class="insight attention"><span class="insight-icon"><i data-lucide="help-circle"></i></span><div><strong>${lista.length} ${escapeHtml(frase)}</strong><p class="muted-value">${escapeHtml(lista.slice(0, 12).join(", "))}</p></div></article>`);
    }
  });
  if (resultado.pedidos_incompletos) {
    blocos.push(`<article class="insight"><span class="insight-icon"><i data-lucide="info"></i></span><div><strong>${resultado.pedidos_incompletos} pedido(s) ficaram com itens parciais</strong><p>Parte dos produtos casou e parte não. Eles continuam marcados como sem custo, porque meio custo apresentado como custo inteiro é pior que custo nenhum.</p></div></article>`);
  }
  document.getElementById("import-items-detail").innerHTML = blocos.join("") || "";
  openDialog("import-items-modal");
  refreshIcons();
}

async function submitAdjustment(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const inventoryId = form.get("inventory_id");
  await submitData(`/api/inventory/${inventoryId}/adjust`, { quantity_delta: Number(form.get("quantity_delta")), reason: form.get("reason") }, "Saldo de estoque atualizado.");
}

// -------------------------------------------------------- contagem geral
//
// Contar item a item pela tela de ajuste e a razao de a contagem nunca
// acontecer: sao dez idas ao mesmo formulario, e alguem desiste no meio. Aqui a
// loja percorre o estoque uma vez e confirma tudo junto.

function openBulkCount() {
  const form = document.getElementById("bulk-count-form");
  form.reset();
  document.getElementById("bulk-count-body").innerHTML = state.inventory.map((item) => {
    const embalagem = item.pack_size
      ? `<small class="muted-value"> \u2248 ${number.format(item.quantity / item.pack_size)} ${escapeHtml(item.pack_unit)}</small>`
      : "";
    return `<tr data-bulk-row="${item.id}"><td><strong>${escapeHtml(item.name)}</strong><small class="muted-value"> ${escapeHtml(item.category || "")}</small></td><td class="align-right muted-value">${number.format(item.quantity)} ${escapeHtml(item.unit)}${embalagem}</td><td class="align-right"><input type="number" min="0" step="0.001" class="bulk-count-input" placeholder="\u2014" aria-label="Contagem de ${escapeHtml(item.name)}" /></td><td class="align-right" data-bulk-diff>\u2014</td><td class="align-right"><input type="checkbox" class="bulk-count-remove" aria-label="Excluir ${escapeHtml(item.name)}" /></td></tr>`;
  }).join("");
  refreshBulkCountPreview();
  openDialog("bulk-count-modal");
}

function readBulkCount() {
  return [...document.querySelectorAll("#bulk-count-body [data-bulk-row]")].map((linha) => {
    const valor = linha.querySelector(".bulk-count-input").value;
    return {
      inventory_id: linha.dataset.bulkRow,
      // Vazio e "nao contei este", nao zero: insumo nao contado nao acabou.
      quantity: valor === "" ? null : Number(valor),
      remove: linha.querySelector(".bulk-count-remove").checked,
    };
  });
}

function refreshBulkCountPreview() {
  const preview = document.getElementById("bulk-count-preview");
  if (!preview) return;
  let falta = 0;
  let sobra = 0;
  let contados = 0;
  let excluidos = 0;
  readBulkCount().forEach((linha) => {
    const celula = document.querySelector(`[data-bulk-row="${linha.inventory_id}"] [data-bulk-diff]`);
    const item = state.inventory.find((row) => row.id === linha.inventory_id);
    if (!item || !celula) return;
    if (linha.remove) excluidos += 1;
    if (linha.quantity === null || Number.isNaN(linha.quantity)) {
      celula.textContent = "\u2014";
      celula.className = "align-right muted-value";
      return;
    }
    contados += 1;
    const diferenca = linha.quantity - item.quantity;
    const valor = diferenca * item.unit_cost;
    if (valor < 0) falta += valor; else sobra += valor;
    celula.innerHTML = diferenca
      ? `<strong class="${diferenca < 0 ? "negative" : "positive"}">${diferenca > 0 ? "+" : ""}${number.format(diferenca)}</strong>`
      : '<span class="muted-value">bateu</span>';
    celula.className = "align-right";
  });
  if (!contados && !excluidos) {
    preview.textContent = "Preencha o que voc\u00ea contou. O que ficar em branco n\u00e3o muda.";
    preview.className = "modal-result full neutral";
    return;
  }
  const liquido = falta + sobra;
  // Alinhar estoque nao e neutro: o que falta vira custo no resultado do dia, e
  // o gestor precisa ver o tamanho disso antes de mandar aplicar.
  preview.textContent = `${contados} contado(s), ${excluidos} para excluir. Falta ${currency.format(Math.abs(falta))}, sobra ${currency.format(sobra)} \u2014 l\u00edquido ${currency.format(liquido)}, que entra no resultado do dia.`;
  preview.className = `modal-result full ${liquido < 0 ? "negative" : liquido > 0 ? "positive" : "neutral"}`;
}

async function submitBulkCount(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const items = readBulkCount().filter((linha) => linha.remove || linha.quantity !== null);
  if (!items.length) return toast("Informe ao menos uma contagem ou uma exclus\u00e3o.", "error");
  const excluir = items.filter((linha) => linha.remove).length;
  if (excluir && !confirm(`Excluir ${excluir} insumo(s) do cadastro?\n\nO saldo deles \u00e9 descartado e fica registrado na auditoria.`)) return;
  await submitData("/api/inventory/bulk-count", {
    items,
    reason: form.get("reason") || "Contagem geral",
    responsible: form.get("responsible") || null,
  }, "Estoque alinhado com a contagem.");
}

// ---------------------------------------------------------- fabricação
//
// A barra sai do freezer e a base pronta entra. É o único movimento que tira de
// um item e põe em outro, e por isso a tela precisa mostrar as duas pontas ao
// mesmo tempo: o custo não some, ele muda de lugar, e o que decide a margem do
// copo é justamente quanto ficou o quilo de base.

function packLabel(item, quantity) {
  // A equipe conta em barra; o estoque fala em quilo. Mostrar as duas leituras
  // evita tanto a conta de cabeça quanto a balança em toda batida.
  if (!item || !item.pack_size || !item.pack_unit) return "";
  const packs = quantity / item.pack_size;
  return ` ≈ ${number.format(packs)} ${escapeHtml(item.pack_unit)}${packs === 1 ? "" : "s"}`;
}

function inventoryOptions(selected = "") {
  return state.inventory
    .map((item) => `<option value="${item.id}"${item.id === selected ? " selected" : ""}>${escapeHtml(item.name)} (${number.format(item.quantity)} ${escapeHtml(item.unit)})</option>`)
    .join("");
}

function refreshInventoryPack(origem = "") {
  // O gestor sabe quanto custa a barra; a ficha técnica precisa do quilo. Em
  // vez de exigir a divisão de cabeça, os dois campos existem e um preenche o
  // outro — quem digitou por último manda.
  const form = document.getElementById("inventory-form");
  if (!form) return;
  const unidade = (form.elements.unit.value || "unidade").trim();
  const embalagem = (form.elements.pack_unit.value || "").trim();
  const tamanho = Number(form.elements.pack_size.value || 0);
  const campo = document.getElementById("inventory-pack-cost-field");
  const ativo = Boolean(embalagem && tamanho > 0);

  campo.hidden = !ativo;
  setText("#inventory-pack-cost-label", embalagem || "unidade comprada");
  setText("#inventory-pack-size-hint", embalagem
    ? `Quanto uma ${embalagem} tem, em ${unidade}.`
    : "Na unidade de estoque.");

  if (!ativo) {
    setText("#inventory-unit-cost-hint", `O que a ficha técnica usa, por ${unidade}.`);
    return;
  }
  const porEmbalagem = Number(form.elements.pack_cost.value || 0);
  const porUnidade = Number(form.elements.unit_cost.value || 0);
  if (origem === "pack" && porEmbalagem > 0) {
    form.elements.unit_cost.value = (porEmbalagem / tamanho).toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  } else if (origem === "unit" && porUnidade > 0) {
    form.elements.pack_cost.value = (porUnidade * tamanho).toFixed(2);
  }
  const atual = Number(form.elements.unit_cost.value || 0);
  setText("#inventory-unit-cost-hint", atual > 0
    ? `${currency.format(atual)} por ${unidade} · ${currency.format(atual * tamanho)} por ${embalagem}. É o de cima que a ficha técnica usa.`
    : `O que a ficha técnica usa, por ${unidade}.`);
}

function addProductionLine(container, inventoryId = "", quantity = "") {
  document.getElementById(container).insertAdjacentHTML("beforeend", `<div class="recipe-line production-line"><label>Insumo<select required data-production-item>${inventoryOptions(inventoryId)}</select></label><label>Quantidade<input type="number" min="0.001" step="0.001" required placeholder="0,000" value="${quantity}" /><small class="field-hint" data-production-hint></small></label><button type="button" data-remove-production title="Remover insumo"><i data-lucide="trash-2"></i></button></div>`);
  const linha = document.getElementById(container).lastElementChild;
  linha.querySelector("[data-production-item]").addEventListener("change", () => describeProductionLine(linha));
  describeProductionLine(linha);
  refreshIcons();
}

// A conversão era escrita uma única vez, na montagem da linha, e continuava lá
// depois de o insumo mudar: a linha anunciava "1 barra = 2,8 kg" com a granola
// selecionada, e quem confiasse nela lançava a batida errada. Agora ela é
// reescrita a cada troca, e diz também em que unidade digitar quando o insumo
// não tem unidade de manuseio nenhuma.
function describeProductionLine(linha) {
  const hint = linha.querySelector("[data-production-hint]");
  if (!hint) return;
  const item = state.inventory.find((row) => row.id === linha.querySelector("[data-production-item]").value);
  const pack = packOf(item);
  hint.textContent = pack
    ? `1 ${pack.unit} = ${number.format(pack.size)} ${item.unit} · digite em ${item.unit}`
    : item ? `Em ${item.unit}` : "";
}

function readProductionLines(container) {
  return [...document.querySelectorAll(`#${container} .production-line`)]
    .map((linha) => ({ inventory_id: linha.querySelector("select").value, quantity: Number(linha.querySelector("input").value) }))
    .filter((linha) => linha.inventory_id && linha.quantity > 0);
}

function openProduction() {
  const form = document.getElementById("production-form");
  form.reset();
  document.getElementById("production-override").checked = false;
  document.getElementById("production-override-field").hidden = true;
  const fichas = state.productionRecipes.filter((linha) => linha.active !== false);
  document.getElementById("production-recipe").innerHTML =
    '<option value="">Sem ficha — lançar do zero</option>'
    + fichas.map((linha) => `<option value="${linha.id}">${escapeHtml(linha.name)}</option>`).join("");
  document.getElementById("production-output").innerHTML = inventoryOptions();
  document.getElementById("production-lines").innerHTML = "";
  if (fichas.length) {
    document.getElementById("production-recipe").value = fichas[0].id;
    applyProductionRecipe();
  } else {
    addProductionLine("production-lines");
    refreshProductionPreview();
  }
  openDialog("production-modal");
}

function applyProductionRecipe() {
  // A ficha preenche; quem manda é o que saiu. Rendimento varia com gelo e com
  // o ponto da barra, e é essa diferença que faz o custo do quilo ser o real.
  const ficha = state.productionRecipes.find((linha) => linha.id === document.getElementById("production-recipe").value);
  const form = document.getElementById("production-form");
  document.getElementById("production-lines").innerHTML = "";
  if (!ficha) {
    addProductionLine("production-lines");
    refreshProductionPreview();
    return;
  }
  ficha.inputs.forEach((linha) => addProductionLine("production-lines", linha.inventory_id, linha.quantity));
  form.elements.output_inventory_id.value = ficha.output_inventory_id;
  // O campo de saída fica vazio de propósito. Preenchê-lo com a previsão faz o
  // funcionário confirmar o palpite do sistema em vez de informar o que viu — e
  // aí o sistema passa a aprender com o próprio palpite, ganhando confiança num
  // número que ele mesmo inventou. O esperado aparece como dica ao lado.
  form.elements.output_quantity.value = "";
  refreshProductionPreview();
}

function productionYieldFor(recipeId) {
  return (state.productionYield?.recipes || []).find((linha) => linha.recipe_id === recipeId) || null;
}

function expectedOutput(recipeId, linhas) {
  // Sem balança, os dois lados ainda são contáveis: barra na entrada, cuba na
  // saída. O que falta é a razão entre eles — e ela não é um número, é a faixa
  // em que os lotes caíram. Devolver um ponto seria dar uma precisão que a
  // operação não tem.
  const rendimento = productionYieldFor(recipeId);
  if (!rendimento || !rendimento.range) return null;
  const entrada = linhas.reduce((total, linha) => total + linha.quantity, 0);
  if (!entrada) return null;
  const faixa = rendimento.range;
  return {
    low: entrada * faixa.low,
    quantity: entrada * faixa.likely,
    high: entrada * faixa.high,
    basis: faixa.basis,
    source: rendimento.source,
    confidence: rendimento.confidence,
    identical: Boolean(rendimento.observed?.identical),
    runs: rendimento.observed?.runs || 0,
  };
}

function refreshProductionPreview() {
  const preview = document.getElementById("production-preview");
  if (!preview) return;
  const form = document.getElementById("production-form");
  const linhas = readProductionLines("production-lines");
  const saida = state.inventory.find((row) => row.id === form.elements.output_inventory_id.value);
  const quantidade = Number(form.elements.output_quantity.value || 0);
  const unidade = document.getElementById("production-output-unit");
  if (unidade) unidade.textContent = saida ? `Na unidade do item: ${saida.unit}.` : "Na unidade do item.";

  const override = document.getElementById("production-override");
  const overrideField = document.getElementById("production-override-field");
  const faltando = [];
  let custo = 0;
  const pedido = new Map();
  linhas.forEach((linha) => pedido.set(linha.inventory_id, roundQuantity((pedido.get(linha.inventory_id) || 0) + linha.quantity)));
  pedido.forEach((quantity, inventoryId) => {
    const item = state.inventory.find((row) => row.id === inventoryId);
    if (!item) return;
    custo += quantity * item.unit_cost;
    if (item.quantity < quantity) faltando.push(`${item.name} (há ${number.format(item.quantity)} ${item.unit})`);
  });
  overrideField.hidden = !faltando.length;
  if (!faltando.length) override.checked = false;

  if (!linhas.length || !saida || !Number.isFinite(quantidade) || quantidade <= 0) {
    preview.textContent = "Escolha as barras e diga quanto saiu da batida.";
    preview.className = "modal-result full neutral";
    return;
  }
  if (pedido.has(saida.id)) {
    preview.textContent = "O item produzido deve ser diferente dos insumos consumidos.";
    preview.className = "modal-result full negative";
    return;
  }
  if (faltando.length && !override.checked) {
    preview.textContent = `Sem saldo para ${faltando.join(" · ")}. Confira, ou confirme abaixo para registrar assim mesmo.`;
    preview.className = "modal-result full negative";
    return;
  }
  // Quanto a batida deveria render, e o quanto a de hoje fugiu disso.
  const esperado = expectedOutput(document.getElementById("production-recipe").value, linhas);
  const dica = document.getElementById("production-output-unit");
  if (dica && esperado) {
    const embalagem = packLabel(saida, esperado.quantity);
    dica.textContent = esperado.identical
      ? `Conte a saída antes de digitar: os ${esperado.runs} lotes anteriores têm todos o mesmo valor, então a estimativa está se confirmando sozinha.`
      : `Deve dar entre ${number.format(esperado.low)} e ${number.format(esperado.high)} ${saida.unit}, mais provável ${number.format(esperado.quantity)}${embalagem} — ${esperado.basis}. Digite o que saiu de verdade.`;
  } else if (dica) {
    dica.textContent = saida ? `Na unidade do item: ${saida.unit}.` : "Na unidade do item.";
  }

  const porUnidade = custo / quantidade;
  const anterior = saida.unit_cost;
  const resultante = saida.quantity > 0
    ? (saida.quantity * anterior + quantidade * porUnidade) / (saida.quantity + quantidade)
    : porUnidade;
  const barras = pedido.size === 1
    ? packLabel(state.inventory.find((row) => row.id === [...pedido.keys()][0]), [...pedido.values()][0])
    : "";
  // Fugir do rendimento esperado nao e erro: e informacao. Ou a barra veio
  // diferente, ou sobrou na cuba — e nos dois casos o custo do quilo muda.
  const desvio = esperado && esperado.quantity
    ? ` Esperava-se ${number.format(esperado.quantity)} ${saida.unit}: ${Math.abs(Math.round((quantidade / esperado.quantity - 1) * 100))}% ${quantidade < esperado.quantity ? "abaixo" : "acima"}.`
    : "";
  preview.textContent = `${currency.format(custo)} em insumo${barras} para ${number.format(quantidade)} ${saida.unit}${packLabel(saida, quantidade)} — ${currency.format(porUnidade)} por ${saida.unit}. O custo médio de ${saida.name} vai de ${currency.format(anterior)} para ${currency.format(resultante)}.${Math.abs((quantidade / (esperado?.quantity || quantidade)) - 1) >= 0.08 ? desvio : ""}`;
  preview.className = `modal-result full ${resultante <= anterior ? "positive" : "neutral"}`;
}

async function submitProduction(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const inputs = readProductionLines("production-lines");
  if (!inputs.length) return toast("Inclua ao menos um insumo na fabricação.", "error");
  const payload = {
    recipe_id: form.get("recipe_id") || null,
    inputs,
    output_inventory_id: form.get("output_inventory_id"),
    output_quantity: Number(form.get("output_quantity")),
    responsible: form.get("responsible"),
    note: form.get("note") || null,
    allow_negative: document.getElementById("production-override").checked,
  };
  await submitData("/api/production/runs", payload, "Fabricação registrada: barra baixada e base pronta em estoque.");
}

function renderProductionAdvice(recipeId) {
  // A lista e conversao, nao recomendacao: o sistema nao sabe o que vai no
  // liquidificador e o que vai por cima do copo. Ele responde "se este entra na
  // batida, quanto vai" e deixa a escolha com quem bate.
  const dados = state.productionAdvice;
  const bloco = document.getElementById("production-recipe-advice");
  if (!bloco) return;
  const linhas = (dados?.suggestions || []).filter((linha) => !recipeId || linha.recipe_id === recipeId);
  bloco.hidden = !linhas.length;
  if (!linhas.length) return;
  setText("#production-recipe-advice-note", dados.note);
  document.getElementById("production-recipe-advice-list").innerHTML = linhas.map((linha) => {
    const embalagem = linha.pack_size ? ` ≈ ${number.format(linha.per_batch / linha.pack_size)} ${escapeHtml(linha.pack_unit)}` : "";
    return `<span title="${escapeHtml(linha.reading)}"><b>${number.format(linha.per_batch)} ${escapeHtml(linha.unit)}${embalagem}</b>${escapeHtml(linha.name)}</span>`;
  }).join("");
}

function renderPackagingAudit() {
  const dados = state.packagingAudit;
  const painel = document.getElementById("packaging-audit-panel");
  if (!painel) return;
  const linhas = dados?.packaging || [];
  painel.hidden = !linhas.length;
  if (!linhas.length) return;

  setText("#packaging-audit-headline", dados.totals.missing_cups
    ? `${number.format(dados.totals.missing_cups)} copos · ${currency.format(dados.totals.implied_revenue)}`
    : dados.counted_any ? "contagem bateu" : "aguardando contagem");
  document.getElementById("packaging-audit-body").innerHTML = linhas.map((linha) => {
    const diferenca = linha.counted
      ? `<strong class="${linha.count_difference < 0 ? "negative" : ""}">${linha.count_difference > 0 ? "+" : ""}${number.format(linha.count_difference)}</strong>`
      : '<span class="muted-value">não contado</span>';
    const falta = linha.missing
      ? `<strong class="negative">${number.format(linha.missing)}</strong><small class="muted-value"> ${number.format(linha.missing_percent)}%</small>`
      : '<span class="muted-value">—</span>';
    return `<tr title="${escapeHtml(linha.reading)}"><td><strong>${escapeHtml(linha.name)}</strong></td><td class="align-right">${number.format(linha.registered_sales)}</td><td class="align-right">${diferenca}</td><td class="align-right">${falta}</td><td class="align-right">${linha.implied_revenue ? `<strong class="negative">${currency.format(linha.implied_revenue)}</strong>` : '<span class="muted-value">—</span>'}</td></tr>`;
  }).join("");
  document.getElementById("packaging-audit-blockers").innerHTML = (dados.blockers || []).map((linha) => `
    <article class="insight attention"><span class="insight-icon"><i data-lucide="triangle-alert"></i></span><div><strong>${escapeHtml(linha.title)}</strong><p>${escapeHtml(linha.description)}</p></div></article>`).join("");
  refreshIcons();
}

function renderProductionCost() {
  const dados = state.productionEconomics;
  const painel = document.getElementById("production-cost-panel");
  if (!painel) return;
  painel.hidden = !dados || !dados.available;
  if (!dados || !dados.available) return;

  setText("#production-cost-headline", `${currency.format(dados.cost_per_cup_real)} por copo`);
  setText("#production-cost-reading", dados.reading);
  document.getElementById("production-cost-body").innerHTML = dados.products.map((linha) => {
    const sinal = linha.difference_total >= 0 ? "negative" : "positive";
    return `<tr><td><strong>${escapeHtml(linha.name)}</strong></td><td class="align-right">${number.format(linha.units)}</td><td class="align-right"><strong>${currency.format(linha.acai_cost_real)}</strong></td><td class="align-right muted-value">${currency.format(linha.acai_cost_registered)}</td><td class="align-right ${sinal}"><strong>${linha.difference_total >= 0 ? "+" : ""}${currency.format(linha.difference_total)}</strong></td></tr>`;
  }).join("");
  document.getElementById("production-cost-blockers").innerHTML = (dados.blockers || []).map((linha) => `
    <article class="insight attention"><span class="insight-icon"><i data-lucide="triangle-alert"></i></span><div><strong>${escapeHtml(linha.title)}</strong><p>${escapeHtml(linha.description)}</p></div></article>`).join("");
  refreshIcons();
}

function renderProductionRecipes() {
  const corpo = document.getElementById("production-recipes-body");
  if (!corpo) return;
  const insumos = Object.fromEntries(state.inventory.map((item) => [item.id, item]));
  corpo.innerHTML = state.productionRecipes.length
    ? state.productionRecipes.map((ficha) => {
        const consome = ficha.inputs.map((linha) => {
          const item = insumos[linha.inventory_id];
          if (!item) return '<span class="negative">insumo removido</span>';
          return `${number.format(linha.quantity)} ${escapeHtml(item.unit)} de ${escapeHtml(item.name)}${packLabel(item, linha.quantity)}`;
        }).join(" + ");
        const custo = ficha.inputs.reduce((total, linha) => total + (insumos[linha.inventory_id]?.unit_cost || 0) * linha.quantity, 0);
        const saida = insumos[ficha.output_inventory_id];
        const rendimento = productionYieldFor(ficha.id);
        const entradaDaFicha = ficha.inputs.reduce((t, l) => t + l.quantity, 0);
        const medido = rendimento && rendimento.range && saida
          ? `<small class="muted-value">na prática: ${number.format(rendimento.range.low * entradaDaFicha)} a ${number.format(rendimento.range.high * entradaDaFicha)} ${escapeHtml(saida.unit)} · ${escapeHtml(rendimento.range.basis)}</small>`
          : "";
        return `<tr><td><strong>${escapeHtml(ficha.name)}</strong>${ficha.note ? `<small class="muted-value"> ${escapeHtml(ficha.note)}</small>` : ""}</td><td class="muted-value">${consome}</td><td>${saida ? `<strong>${number.format(ficha.output_quantity)} ${escapeHtml(saida.unit)}</strong> de ${escapeHtml(saida.name)}${medido}` : '<span class="negative">item removido</span>'}</td><td class="align-right">${currency.format(custo)}</td><td class="align-right"><strong>${currency.format(custo / ficha.output_quantity)}</strong>${saida ? `<small class="muted-value">/${escapeHtml(saida.unit)}</small>` : ""}</td><td class="align-right"><div class="row-actions"><button class="row-action" type="button" data-edit-recipe="${ficha.id}" title="Editar ficha"><i data-lucide="pencil"></i></button></div></td></tr>`;
      }).join("")
    : emptyRow(6, "Nenhuma ficha cadastrada. Sem ela a batida é lançada do zero toda vez.");

  // Insumo nos dois lugares sai do estoque duas vezes: ao bater e ao vender.
  const duplicados = state.productionAdvice?.double_counted || [];
  document.getElementById("production-double-counted").innerHTML = duplicados.map((linha) => `
    <article class="insight attention"><span class="insight-icon"><i data-lucide="triangle-alert"></i></span><div><strong>${escapeHtml(linha.title)}</strong><p>${escapeHtml(linha.description)}</p></div></article>`).join("");
  refreshIcons();
}

// ------------------------------------------------------- retirada de insumo
//
// A baixa por venda sai sozinha da ficha técnica. O que a loja não tinha era
// como registrar o insumo que a equipe leva do estoque sem vender: a produção
// da manhã, a degustação, a reposição do balcão. Sem esse lançamento o saldo
// do sistema fica maior que o da prateleira, e a diferença só aparece na
// contagem, virando "perda" sem que ninguém saiba o que foi.
//
// Registrar só acontece se for mais rápido do que não registrar. Por isso a
// linha pergunta as três coisas que a equipe já tem na cabeça — qual insumo,
// quanto e em que tamanho — e converte para a unidade de estoque sozinha.
// Enquanto ela só aceitava quilo, tirar três barras exigia dividir de cabeça
// ou ir até a balança, e o lançamento ficava para depois.

// A unidade de manuseio do insumo, quando o cadastro tem uma: "barra" de
// 2,8 kg. O servidor só aceita o par nome/tamanho completo ou vazio, então
// aqui basta conferir que os dois vieram e que o tamanho é utilizável.
function packOf(item) {
  return item && item.pack_unit && item.pack_size > 0 ? { unit: item.pack_unit, size: item.pack_size } : null;
}

// "3 barra" trai a tela: quem le desconfia do resto do numero. Barra, pote e
// fardo pluralizam com "s", que e o que o cadastro aceita como unidade.
function packAmount(pack, quantity) {
  return `${number.format(quantity)} ${pack.unit}${quantity === 1 ? "" : "s"}`;
}

// O teclado do celular em pt-BR entrega vírgula, e `type="number"` devolve
// campo vazio quando recebe uma: o operador digitava "0,5", via o campo sumir
// e desistia do lançamento. Por isso o campo é de texto e a vírgula chega até
// aqui, em vez de ser descartada antes de alguém poder tratá-la.
function parseQuantity(value) {
  const texto = String(value ?? "").trim().replace(/\s+/g, "").replace(",", ".");
  // O separador solto no fim é o estado normal de quem está digitando "0,5";
  // recusá-lo faria a dica piscar "inválido" no meio de um número correto.
  if (!/^\d*(\.\d*)?$/.test(texto) || !/\d/.test(texto)) return NaN;
  const numero = Number(texto);
  return Number.isFinite(numero) ? numero : NaN;
}

// O estoque guarda três casas. Somar sem cortar aqui faz 0,1 + 0,2 virar
// 0,30000000000000004 e acusar falta de saldo contra um saldo de 0,3.
function roundQuantity(value) {
  return Math.round(value * 1000) / 1000;
}

// O saldo vai na própria opção: quem retira decide olhando o que existe, e
// obrigar a conferir na tela de estoque antes faria o lançamento ser adiado.
// Quando o insumo tem unidade de manuseio, ela vem primeiro — é em barra que
// a equipe enxerga o freezer, não em quilo.
function inventoryOptionLabel(item) {
  const pack = packOf(item);
  const saldo = `${number.format(item.quantity)} ${item.unit}`;
  return pack
    ? `${item.name} — ${packAmount(pack, roundQuantity(item.quantity / pack.size))} (${saldo})`
    : `${item.name} — ${saldo} em estoque`;
}

function openWithdrawal() {
  // Sem insumo cadastrado o formulário só pode ser recusado. Dizer o que falta
  // é melhor do que abrir uma lista vazia que não explica nada.
  if (!state.inventory.length) {
    toast("Cadastre um insumo antes de registrar uma retirada.", "error");
    return;
  }
  const form = document.getElementById("withdrawal-form");
  form.reset();
  document.getElementById("withdrawal-lines").innerHTML = "";
  document.getElementById("withdrawal-override").checked = false;
  document.getElementById("withdrawal-override-field").hidden = true;
  addWithdrawalLine();
  openDialog("withdrawal-modal");
  form.elements.responsible.focus();
}

function addWithdrawalLine() {
  const options = state.inventory
    .map((item) => `<option value="${item.id}">${escapeHtml(inventoryOptionLabel(item))}</option>`)
    .join("");
  document.getElementById("withdrawal-lines").insertAdjacentHTML("beforeend", `<div class="recipe-line withdrawal-line"><label>Insumo<select required data-withdrawal-item>${options}</select></label><label>Quantidade<input type="text" inputmode="decimal" autocomplete="off" placeholder="0,000" aria-label="Quantidade retirada" data-withdrawal-quantity /></label><label>Tamanho<select data-withdrawal-unit aria-label="Unidade da quantidade"></select></label><button type="button" data-remove-withdrawal title="Remover insumo"><i data-lucide="trash-2"></i></button><small class="withdrawal-hint" data-withdrawal-hint></small></div>`);
  refreshIcons();
  refreshWithdrawalPreview();
}

// A lista de tamanhos depende do insumo escolhido, então é remontada quando o
// insumo muda — e só quando ele muda: refazer o `select` a cada tecla fecharia
// a lista aberta e devolveria o foco no meio da digitação.
//
// Enquanto ninguém escolheu, o campo abre na unidade de manuseio, que é a que
// a equipe usa na prateleira. Depois de uma escolha explícita, ela manda —
// exceto quando o insumo novo não tem manuseio e só resta o estoque.
function syncWithdrawalUnit(line) {
  const item = state.inventory.find((row) => row.id === line.querySelector("[data-withdrawal-item]").value);
  const select = line.querySelector("[data-withdrawal-unit]");
  const pack = packOf(item);
  const assinatura = `${item ? item.id : ""}|${item ? item.unit : ""}|${pack ? `${pack.unit}:${pack.size}` : ""}`;
  if (select.dataset.builtFor !== assinatura) {
    const escolhido = select.dataset.chosen === "1" ? select.value : "";
    select.innerHTML = `<option value="stock">${escapeHtml(item ? item.unit : "unidade")}</option>${pack ? `<option value="pack">${escapeHtml(pack.unit)}</option>` : ""}`;
    select.value = escolhido === "pack" && !pack ? "stock" : escolhido || (pack ? "pack" : "stock");
    select.dataset.builtFor = assinatura;
  }
  return { item, pack, usingPack: select.value === "pack" && Boolean(pack) };
}

// Lê uma linha inteira: o insumo, o que foi digitado e quanto isso vale na
// unidade de estoque, que é a única que o servidor entende.
function readWithdrawalLine(line) {
  const { item, pack, usingPack } = syncWithdrawalUnit(line);
  const input = line.querySelector("[data-withdrawal-quantity]");
  const typed = parseQuantity(input.value);
  return {
    line,
    item,
    pack,
    usingPack,
    typed,
    blank: input.value.trim() === "",
    quantity: Number.isNaN(typed) ? NaN : roundQuantity(usingPack ? typed * pack.size : typed),
  };
}

function withdrawalLines() {
  return [...document.querySelectorAll("#withdrawal-lines .withdrawal-line")].map(readWithdrawalLine);
}

function readWithdrawalLines() {
  return withdrawalLines()
    .filter((linha) => linha.item && linha.quantity > 0)
    .map((linha) => ({ inventory_id: linha.item.id, quantity: linha.quantity }));
}

// A conferência de saldo é por insumo, mas o erro está numa linha. Sem marcar
// a linha, uma retirada de cinco itens obriga a reler todas para achar qual
// estourou — e a dica embaixo dela é onde a conversão fica visível sem tirar
// o operador do formulário.
function describeWithdrawalLine(linha, pedido, semSaldo, repetidos) {
  const input = linha.line.querySelector("[data-withdrawal-quantity]");
  const hint = linha.line.querySelector("[data-withdrawal-hint]");
  const invalida = !linha.blank && Number.isNaN(linha.typed);
  const estourou = Boolean(linha.item && semSaldo.has(linha.item.id));
  input.classList.toggle("over-balance", invalida || estourou);
  if (!hint) return;
  hint.className = "withdrawal-hint";
  if (!linha.item) {
    hint.textContent = "";
    return;
  }
  const item = linha.item;
  const conversao = linha.pack ? `1 ${linha.pack.unit} = ${number.format(linha.pack.size)} ${item.unit}` : `Em ${item.unit}`;
  if (invalida) {
    hint.textContent = "Quantidade inválida. Use apenas números, com vírgula para o decimal.";
    hint.className = "withdrawal-hint negative";
    return;
  }
  if (!(linha.quantity > 0)) {
    hint.textContent = `${conversao} · há ${number.format(item.quantity)} ${item.unit}`;
    return;
  }
  const total = pedido.get(item.id) ?? linha.quantity;
  const sobra = roundQuantity(item.quantity - total);
  const leitura = linha.usingPack
    ? `${packAmount(linha.pack, linha.typed)} = ${number.format(linha.quantity)} ${item.unit}`
    : `${number.format(linha.quantity)} ${item.unit}`;
  // Duas linhas do mesmo insumo somam no servidor. Dizer isso aqui evita o
  // "por que recusou, se cada linha cabia no saldo?".
  const repetido = repetidos.has(item.id) ? ` · o insumo está em mais de uma linha, e juntas pedem ${number.format(total)} ${item.unit}` : "";
  hint.textContent = sobra < 0
    ? `${leitura} · há ${number.format(item.quantity)} ${item.unit}, faltam ${number.format(Math.abs(sobra))} ${item.unit}${repetido}`
    : `${leitura} · sobram ${number.format(sobra)} ${item.unit}${repetido}`;
  if (sobra < 0) hint.className = "withdrawal-hint negative";
}

function refreshWithdrawalPreview() {
  const preview = document.getElementById("withdrawal-preview");
  if (!preview) return;
  const linhas = withdrawalLines();
  const override = document.getElementById("withdrawal-override");
  const overrideField = document.getElementById("withdrawal-override-field");

  // O mesmo insumo em duas linhas soma antes da conferência, como o servidor
  // faz: mostrar cada linha isolada aprovaria aqui uma retirada que lá seria
  // recusada por saldo.
  const pedido = new Map();
  const vistos = new Set();
  const repetidos = new Set();
  linhas.forEach((linha) => {
    if (!linha.item) return;
    if (vistos.has(linha.item.id)) repetidos.add(linha.item.id); else vistos.add(linha.item.id);
    if (!(linha.quantity > 0)) return;
    pedido.set(linha.item.id, roundQuantity((pedido.get(linha.item.id) || 0) + linha.quantity));
  });

  let cost = 0;
  const semSaldo = new Map();
  pedido.forEach((quantity, inventoryId) => {
    const item = state.inventory.find((row) => row.id === inventoryId);
    if (!item) return;
    cost += quantity * item.unit_cost;
    if (item.quantity < quantity) semSaldo.set(inventoryId, quantity);
  });

  linhas.forEach((linha) => describeWithdrawalLine(linha, pedido, semSaldo, repetidos));

  const pendentes = linhas.filter((linha) => linha.item && !(linha.quantity > 0)).length;
  if (!pedido.size) {
    override.checked = false;
    overrideField.hidden = true;
    const invalidas = linhas.some((linha) => linha.item && !linha.blank && Number.isNaN(linha.typed));
    preview.textContent = invalidas
      ? "Quantidade inválida. Use apenas números, com vírgula para o decimal."
      : "Escolha o insumo, o tamanho e quanto saiu do estoque.";
    preview.className = `modal-result full ${invalidas ? "negative" : "neutral"}`;
    return;
  }
  // Faltar saldo nao encerra o assunto: o insumo pode ter saido de verdade e o
  // cadastro estar atrasado. A recusa continua sendo o padrao, e a confirmacao
  // ao lado e o que separa o erro de digitacao do estoque desatualizado.
  overrideField.hidden = !semSaldo.size;
  if (!semSaldo.size) override.checked = false;
  if (semSaldo.size) {
    const faltas = [...semSaldo].map(([inventoryId, quantity]) => {
      const item = state.inventory.find((row) => row.id === inventoryId);
      return `${item.name} (há ${number.format(item.quantity)} ${item.unit}, a retirada pede ${number.format(quantity)} ${item.unit})`;
    });
    preview.textContent = override.checked
      ? `Sem saldo para ${faltas.join(" · ")}. Vai ser registrada assim mesmo, e esse saldo fica negativo.`
      : `Sem saldo para ${faltas.join(" · ")}. Confira a quantidade, ou confirme abaixo para registrar assim mesmo.`;
    preview.className = `modal-result full ${override.checked ? "neutral" : "negative"}`;
    return;
  }
  const resumo = `${pedido.size} ${pedido.size === 1 ? "insumo" : "insumos"} · ${currency.format(cost)} saindo do estoque.`;
  preview.textContent = pendentes
    ? `${resumo} Falta a quantidade de ${pendentes} ${pendentes === 1 ? "linha" : "linhas"}.`
    : resumo;
  preview.className = `modal-result full ${pendentes ? "negative" : "positive"}`;
}

async function submitWithdrawal(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const linhas = withdrawalLines();
  // Linha começada e não preenchida costuma ser insumo que saiu de verdade.
  // Descartá-la em silêncio devolveria ao sistema um saldo maior que o da
  // prateleira — exatamente o que este registro existe para evitar. O campo de
  // texto não tem o `required` do navegador para barrar isso, então a checagem
  // é aqui, e ela aponta a linha em vez de só recusar o formulário.
  const pendentes = linhas.filter((linha) => linha.item && !(linha.quantity > 0));
  if (pendentes.length) {
    refreshWithdrawalPreview();
    pendentes[0].line.querySelector("[data-withdrawal-quantity]").focus();
    toast("Informe a quantidade de cada insumo, ou remova a linha que sobrou.", "error");
    return;
  }
  const items = readWithdrawalLines();
  if (!items.length) return toast("Inclua ao menos um insumo na retirada.", "error");
  const payload = {
    items,
    responsible: form.get("responsible"),
    reason: form.get("reason"),
    note: form.get("note") || null,
    allow_negative: document.getElementById("withdrawal-override").checked,
  };
  await submitData("/api/inventory/withdrawals", payload, "Retirada registrada e estoque atualizado.");
}

function openPurchase(inventoryId = "") {
  const form = document.getElementById("purchase-form");
  form.reset();
  const select = document.getElementById("purchase-inventory");
  select.innerHTML = state.inventory.map((item) => `<option value="${item.id}">${escapeHtml(item.name)} (${number.format(item.quantity)} ${escapeHtml(item.unit)})</option>`).join("");
  if (inventoryId && state.inventory.some((item) => item.id === inventoryId)) select.value = inventoryId;
  applyPurchasePack(state.inventory.find((row) => row.id === select.value));
  form.elements.received_at.value = localDateTimeValue(new Date());
  form.elements.received_at.max = localDateTimeValue(new Date());
  refreshPurchasePreview();
  openDialog("purchase-modal");
}

function applyPurchasePack(item) {
  // Se o insumo diz que é contado em barra, a compra já vem em barra: quem
  // cadastrou a conversão uma vez não deveria repeti-la a cada nota.
  const form = document.getElementById("purchase-form");
  form.elements.purchase_unit.value = item?.pack_unit || item?.unit || "";
  form.elements.units_per_purchase.value = item?.pack_size || 1;
}

function refreshPurchasePreview() {
  const form = document.getElementById("purchase-form");
  const item = state.inventory.find((row) => row.id === form.elements.inventory_id.value);
  const current = document.getElementById("purchase-current");
  const preview = document.getElementById("purchase-preview");
  if (!item) {
    current.value = "";
    preview.textContent = "";
    return;
  }
  current.value = `${number.format(item.quantity)} ${item.unit} a ${currency.format(item.unit_cost)}/${item.unit}`;
  document.getElementById("purchase-content-hint").textContent = `Quanto há em cada unidade, em ${item.unit}.`;
  const purchaseQuantity = Number(form.elements.purchase_quantity.value || 0);
  const unitsPerPurchase = Number(form.elements.units_per_purchase.value || 0);
  const purchaseUnitCost = Number(form.elements.purchase_unit_cost.value || 0);
  if (![purchaseQuantity, unitsPerPurchase, purchaseUnitCost].every(value => Number.isFinite(value) && value > 0)) {
    preview.textContent = "Informe a quantidade, o conteúdo e o custo da unidade comprada.";
    preview.className = "modal-result full neutral";
    return;
  }
  const quantity = purchaseQuantity * unitsPerPurchase;
  const unitCost = purchaseUnitCost / unitsPerPurchase;
  const resultingQuantity = item.quantity + quantity;
  const resultingCost = item.quantity > 0
    ? ((item.quantity * item.unit_cost) + (quantity * unitCost)) / resultingQuantity
    : unitCost;
  const resetCost = item.quantity <= 0
    ? " Como o saldo atual não é positivo, o custo da compra passa a ser a nova referência."
    : "";
  const purchaseUnit = form.elements.purchase_unit.value.trim() || "unidade";
  preview.textContent = `${number.format(purchaseQuantity)} ${purchaseUnit}${purchaseQuantity === 1 ? "" : "s"} = ${number.format(quantity)} ${item.unit} · ${currency.format(purchaseUnitCost)} por ${purchaseUnit} · total: ${currency.format(purchaseQuantity * purchaseUnitCost)} · saldo: ${number.format(resultingQuantity)} ${item.unit} · custo médio: ${currency.format(resultingCost)}.${resetCost}`;
  preview.className = "modal-result full positive";
}

async function submitPurchase(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const inventoryId = form.get("inventory_id");
  const receivedAt = form.get("received_at");
  const payload = {
    purchase_quantity: Number(form.get("purchase_quantity")),
    purchase_unit: form.get("purchase_unit"),
    units_per_purchase: Number(form.get("units_per_purchase")),
    purchase_unit_cost: Number(form.get("purchase_unit_cost")),
    supplier: form.get("supplier") || null,
    invoice_number: form.get("invoice_number") || null,
    received_at: receivedAt || null,
    note: form.get("note") || null,
  };
  await submitData(`/api/inventory/${inventoryId}/purchases`, payload, "Compra recebida e estoque atualizado.");
}

async function submitInventory(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  const inventoryId = payload.inventory_id;
  delete payload.inventory_id;
  ["quantity", "minimum_quantity", "unit_cost"].forEach(key => { payload[key] = Number(payload[key]); });
  // Validade em branco significa "não perece", e nulo é como o cadastro grava
  // isso. Mandar string vazia faria a validação recusar o insumo inteiro.
  payload.shelf_life_days = payload.shelf_life_days === "" ? null : Number(payload.shelf_life_days);
  payload.lead_time_days = Number(payload.lead_time_days || 2);
  // Unidade de manuseio e opcional, e os dois campos andam juntos: metade
  // preenchida seria recusada pelo servidor.
  payload.pack_unit = payload.pack_unit ? payload.pack_unit : null;
  payload.pack_size = payload.pack_size === "" ? null : Number(payload.pack_size);
  if ((payload.pack_unit === null) !== (payload.pack_size === null)) {
    return toast("Informe o nome e o tamanho da unidade de manuseio, ou deixe os dois em branco.", "error");
  }
  // Custo por embalagem existe só para poupar a divisão de cabeça; o que o
  // sistema guarda é sempre o custo por unidade de estoque.
  delete payload.pack_cost;
  if (inventoryId) {
    // Saldo não entra na edição: ele só muda por contagem ou ajuste, para toda
    // diferença ficar registrada em uma movimentação.
    delete payload.quantity;
    await submitData(`/api/inventory/${inventoryId}`, payload, "Cadastro do insumo atualizado.", "PATCH");
    return;
  }
  await submitData("/api/inventory", payload, "Novo insumo adicionado ao estoque.");
}

async function submitProduct(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const recipe = [...document.querySelectorAll("#recipe-lines .recipe-line")].map(line => ({ inventory_id: line.querySelector("select").value, quantity: Number(line.querySelector("input").value) })).filter(line => line.inventory_id && line.quantity > 0);
  if (!recipe.length) return toast("Inclua ao menos um insumo na ficha técnica.", "error");
  const nutrition = {};
  ["kcal", "protein_g", "carbs_g", "fiber_g"].forEach((key) => {
    const value = form.get(key);
    if (value !== null && value !== "") nutrition[key] = Number(value);
  });
  const payload = {
    name: form.get("name"),
    category: form.get("category"),
    price: Number(form.get("price")),
    line: form.get("line") || "Classico",
    claims: String(form.get("claims") || "").split(",").map((claim) => claim.trim()).filter(Boolean),
    nutrition,
    recipe,
  };
  const productId = form.get("product_id");
  if (productId) {
    await submitData(`/api/products/${productId}`, payload, "Produto atualizado.", "PATCH");
    return;
  }
  await submitData("/api/products", { ...payload, active: true }, "Produto salvo no cardápio.");
}

async function openMedia(mediaId) {
  const record = state.media.find(item => item.id === mediaId);
  if (!record) return;
  state.selectedMediaId = mediaId;
  const dialog = document.getElementById("media-modal");
  const form = document.getElementById("media-form");
  setText("#media-kind-label", record.kind === "image" ? "Imagem" : record.kind === "spreadsheet" ? "Planilha" : "Documento");
  setText("#media-modal-title", record.title);
  form.elements.title.value = record.title || "";
  form.elements.category.value = hasSelectOption(form.elements.category, record.category) ? record.category : "Documentos";
  form.elements.tags.value = (record.tags || []).join(", ");
  form.elements.entity_type.value = record.entity_type || "";
  populateEntityTargets(record.entity_type || "", record.entity_id || "");
  const openLink = document.getElementById("media-open");
  openLink.href = "#";
  openLink.setAttribute("aria-disabled", "true");
  document.getElementById("media-detail-preview").innerHTML = '<span>Carregando arquivo...</span>';
  renderMediaContext(record.metadata || {});
  dialog.showModal();
  document.getElementById("modal-backdrop").hidden = false;
  refreshIcons();
  try {
    const url = await mediaContentUrl(record.id);
    if (state.selectedMediaId !== record.id) return;
    openLink.href = url;
    openLink.removeAttribute("aria-disabled");
    document.getElementById("media-detail-preview").innerHTML = record.kind === "image"
      ? `<img src="${url}" alt="${escapeHtml(record.title)}" />`
      : `<i data-lucide="${mediaIcon(record.kind)}"></i><span>${escapeHtml(record.original_name)}</span>`;
    refreshIcons();
  } catch (error) {
    document.getElementById("media-detail-preview").innerHTML = '<span>Nao foi possivel abrir este arquivo.</span>';
    toast(error.message || "Nao foi possivel abrir o arquivo.", "error");
  }
}

function renderMediaContext(metadata) {
  const container = document.getElementById("media-context");
  const facts = [];
  if (metadata.supplier) facts.push(`<span>Fornecedor <strong>${escapeHtml(metadata.supplier)}</strong></span>`);
  if (metadata.business_date) facts.push(`<span>Data <strong>${escapeHtml(formatDate(metadata.business_date))}</strong></span>`);
  if (typeof metadata.total === "number") facts.push(`<span>Valor <strong>${currency.format(metadata.total)}</strong></span>`);
  if (metadata.valid_until) facts.push(`<span>Validade <strong>${escapeHtml(formatDate(metadata.valid_until))}</strong></span>`);
  const attention = metadata.attention ? `<p><i data-lucide="circle-alert"></i>${escapeHtml(metadata.attention)}</p>` : "";
  container.hidden = !facts.length && !attention;
  container.innerHTML = `${facts.length ? `<div>${facts.join("")}</div>` : ""}${attention}`;
}

async function syncMedia() {
  const button = document.getElementById("sync-media");
  button.disabled = true;
  try {
    const result = await api("/api/media/sync", { method: "POST" });
    toast(result.added ? `${result.added} arquivo${result.added === 1 ? "" : "s"} sincronizado${result.added === 1 ? "" : "s"}.` : "Arquivos já estão sincronizados.", "success");
    await loadAll();
  } catch (error) {
    toast(error.message || "Não foi possível sincronizar os arquivos.", "error");
  } finally {
    button.disabled = false;
  }
}

async function uploadMedia(event) {
  const files = [...event.target.files];
  if (!files.length) return;
  const data = new FormData();
  files.forEach(file => data.append("files", file));
  try {
    const result = await api("/api/media/upload", { method: "POST", body: data });
    toast(`${result.length} arquivo${result.length === 1 ? "" : "s"} adicionado${result.length === 1 ? "" : "s"} ao acervo.`, "success");
    event.target.value = "";
    await loadAll();
  } catch (error) {
    toast(error.message || "Não foi possível enviar os arquivos.", "error");
  }
}

async function submitMedia(event) {
  event.preventDefault();
  if (!state.selectedMediaId) return;
  const form = new FormData(event.currentTarget);
  const entityType = form.get("entity_type") || null;
  const entityId = form.get("entity_id") || null;
  if ((entityType && !entityId) || (!entityType && entityId)) return toast("Preencha os dois campos do vínculo ou deixe ambos vazios.", "error");
  await submitData(`/api/media/${state.selectedMediaId}`, {
    title: form.get("title"), category: form.get("category"),
    tags: String(form.get("tags") || "").split(",").map(tag => tag.trim()).filter(Boolean),
    entity_type: entityType, entity_id: entityId,
  }, "Arquivo organizado no acervo.", "PATCH");
}

function populateEntityTargets(type, selectedId = "") {
  const select = document.getElementById("media-entity-id");
  const collections = { expense: state.expenses, product: state.products, inventory: state.inventory };
  const rows = collections[type] || [];
  select.disabled = !type;
  select.innerHTML = type
    ? `<option value="">Selecione um registro</option>${rows.map(item => `<option value="${item.id}">${escapeHtml(linkTargetLabel(type, item))}</option>`).join("")}`
    : '<option value="">Selecione o tipo acima</option>';
  select.value = selectedId;
}

async function submitData(path, payload, successMessage, method = "POST") {
  if (submitData.pending) return;
  submitData.pending = true;
  const form = document.querySelector("dialog[open] form");
  const buttons = form ? [...form.querySelectorAll('button[type="submit"]:not(:disabled)')] : [];
  buttons.forEach(button => { button.disabled = true; });
  form?.setAttribute("aria-busy", "true");
  try {
    const resultado = await api(path, { method, ...(payload == null ? {} : { body: JSON.stringify(payload) }) });
    closeModal();
    // Quando o servidor explica o que realmente aconteceu — o produto foi
    // desativado em vez de excluído, por exemplo — a explicação dele vale mais
    // que a frase otimista escrita aqui.
    toast(resultado?.detail || successMessage, "success");
    if (await loadAll() === false) {
      toast("Operação salva, mas os dados da tela não foram atualizados. Atualize a tela antes de continuar; não repita o lançamento.", "error");
    }
  } catch (error) {
    toast(error.message || "Não foi possível salvar.", "error");
  } finally {
    submitData.pending = false;
    buttons.forEach(button => { button.disabled = false; });
    form?.removeAttribute("aria-busy");
  }
}

function populateSelects() {
  populateAdjustSelect();
  populateChannelSelect();
  populateCourierSelects();
  populateCatalogSelects();
}

// Canal deixou de ser lista fixa no código: quem manda é o cadastro em Canais.
function populateChannelSelect() {
  const select = document.getElementById("sale-channel");
  if (!select) return;
  const current = select.value;
  const active = state.channels.filter((channel) => channel.active);
  select.innerHTML = active.map((channel) => `<option value="${escapeHtml(channel.name)}">${escapeHtml(channel.name)}${channel.fee_percent ? ` — ${number.format(channel.fee_percent)}% de taxa` : ""}</option>`).join("");
  if (current && active.some((channel) => channel.name === current)) select.value = current;
}

// Frete, zona, endereço e entregador só existem quando a venda é entrega.
// Mostrá-los na venda de balcão enchia o formulário de campo que não se aplica
// — e era por isso que a caixa de seleção não parecia fazer nada.
function toggleSaleDelivery() {
  const entrega = document.getElementById("sale-is-delivery").checked;
  document.querySelectorAll("#sale-form .sale-delivery-field").forEach((campo) => { campo.hidden = !entrega; });
}

// As zonas são carregadas sob demanda com o resto da operação de rua, então a
// lista pode ainda não existir quando a venda abre. Ela é buscada aqui, uma vez.
async function populateSaleZones() {
  const select = document.getElementById("sale-zone");
  if (!state.dispatch?.zones) {
    try { state.dispatch = { ...(state.dispatch || {}), zones: await api("/api/delivery/zones") }; } catch (_) { /* segue sem zona */ }
  }
  const zonas = (state.dispatch?.zones || []).filter((zona) => zona.active !== false);
  select.innerHTML = `<option value="">Sem zona definida</option>${zonas.map((zona) => `<option value="${zona.id}" data-fee="${zona.default_fee || 0}">${escapeHtml(zona.name)}</option>`).join("")}`;
}

// A zona já sabe quanto custa chegar lá. Digitar o frete de novo é a chance de
// digitá-lo diferente, e a divergência só apareceria no acerto do mês.
function applySaleZoneFee(event) {
  const escolhida = event.target.selectedOptions[0];
  const frete = Number(escolhida?.dataset.fee || 0);
  const campo = document.querySelector('#sale-form [name="delivery_fee"]');
  if (frete > 0 && Number(campo.value || 0) === 0) campo.value = frete.toFixed(2);
}

function populateAdjustSelect() {
  document.getElementById("adjust-inventory").innerHTML = state.inventory.map(item => `<option value="${item.id}">${escapeHtml(item.name)} (${number.format(item.quantity)} ${escapeHtml(item.unit)})</option>`).join("");
}

function addRecipeLine() {
  const options = state.inventory.map(item => `<option value="${item.id}">${escapeHtml(item.name)} (${escapeHtml(item.unit)})</option>`).join("");
  document.getElementById("recipe-lines").insertAdjacentHTML("beforeend", `<div class="recipe-line"><label>Insumo<select required>${options}</select></label><label>Quantidade<input type="number" min="0.001" step="0.001" required placeholder="0,000" /></label><button type="button" data-remove-recipe title="Remover insumo"><i data-lucide="trash-2"></i></button></div>`);
  refreshIcons();
}

async function api(path, options = {}) {
  if (!navigator.onLine && (options.method || "GET") !== "GET") {
    throw new Error("Sem conexao. Conecte-se antes de registrar alteracoes.");
  }
  const headers = { ...(options.headers || {}) };
  const token = await window.erpAuth?.getToken?.();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch (error) {
    throw new Error((options.method || "GET") === "GET"
      ? "Falha de conexão. Verifique sua internet e atualize os dados."
      : "A conexão foi interrompida e não foi possível confirmar o resultado. Confira o histórico antes de repetir o lançamento.");
  }
  if (!response.ok) {
    let message = "Não foi possível concluir a operação.";
    try { message = describeError((await response.json()).detail) || message; } catch (_) { /* keep default */ }
    const requestId = response.headers?.get("X-Request-ID");
    if (response.status >= 500 && requestId) message += ` Referência: ${requestId}.`;
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

// O detalhe do erro vem em duas formas. A recusa de regra de negócio manda uma
// frase pronta; a validação do FastAPI manda uma lista de objetos, e jogar essa
// lista num texto imprimia "[object Object]" — o botão parecia quebrado sem
// dizer o que faltava preencher.
function describeError(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  const linhas = (Array.isArray(detail) ? detail : [detail]).map((item) => {
    if (typeof item === "string") return item;
    const campo = Array.isArray(item?.loc)
      ? item.loc.filter((parte) => parte !== "body" && typeof parte !== "number").join(" › ")
      : "";
    const texto = item?.msg || item?.detail || "";
    if (!texto) return "";
    return campo ? `${campo}: ${texto}` : texto;
  }).filter(Boolean);
  return linhas.join(" | ");
}

async function mediaContentUrl(mediaId) {
  if (state.mediaObjectUrl) URL.revokeObjectURL(state.mediaObjectUrl);
  const headers = {};
  const token = await window.erpAuth?.getToken?.();
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`/api/media/${mediaId}/content`, { headers });
  if (!response.ok) throw new Error("Nao foi possivel abrir o arquivo.");
  state.mediaObjectUrl = URL.createObjectURL(await response.blob());
  return state.mediaObjectUrl;
}

function itemSummary(order) { return order.items.map(item => `${item.quantity}x ${item.name}`).join(", "); }
function mediaIcon(kind) { return { spreadsheet: "sheet", document: "file-text", image: "image" }[kind] || "file"; }
// ----------------------------------------------------------------------
// Integração com o iFood
// ----------------------------------------------------------------------
//
// Fica fora de loadAll de propósito. O carregamento geral já espera trinta e
// poucas respostas, e pendurar a integração nele faria uma indisponibilidade
// do iFood derrubar o painel inteiro. Aqui o estado é buscado quando a tela de
// Ajustes abre, e depois de cada ação.
//
// A ativação tem três estados porque o iFood a fez assim, e a tela mostra em
// qual deles a loja está: sem credencial, com código à espera do portal, ou
// conectada. Mostrar os três de uma vez faria a loja adivinhar o próximo passo.

const IFOOD_ROTULOS = {
  desconectado: "Desconectado",
  aguardando_autorizacao: "Aguardando o portal",
  conectado: "Conectado",
};

const IFOOD_ACOES = {
  start: (ler) => ({
    path: "/api/integrations/ifood/activation", method: "POST",
    body: { client_id: ler("client_id"), client_secret: ler("client_secret") },
    ok: "Código de ativação gerado.",
  }),
  renew: () => ({
    path: "/api/integrations/ifood/activation/renew", method: "POST",
    ok: "Código de ativação gerado.",
  }),
  confirm: (ler) => ({
    path: "/api/integrations/ifood/activation/confirm", method: "POST",
    body: { authorization_code: ler("authorization_code") },
    ok: "iFood conectado.",
  }),
  sync: () => ({ path: "/api/integrations/ifood/sync", method: "POST", ok: "Busca concluída." }),
  disconnect: () => ({ path: "/api/integrations/ifood", method: "DELETE", ok: "Integração desligada." }),
};

// A Anota Aí, onde a loja mantém o cardápio e por onde passa toda a venda.
//
// Carregada ao abrir Ajustes, como a do iFood: uma integração fora do ar não
// pode derrubar o painel inteiro, e a tela de ajustes é o único lugar onde
// alguém precisa dessa informação.
async function loadAnotaAi() {
  const corpo = document.getElementById("anotaai-body");
  if (!corpo) return;
  try {
    renderAnotaAi(await api("/api/integrations/anota-ai"));
  } catch (error) {
    setText("#anotaai-state", "Indisponível");
    corpo.innerHTML = `<p class="empty-state">${escapeHtml(error.message || "Não foi possível ler o estado da integração.")}</p>`;
  }
}

function renderAnotaAi(estado) {
  setText("#anotaai-state", estado.connected ? "Conectada" : "Desconectada");
  const corpo = document.getElementById("anotaai-body");
  if (!estado.connected) {
    corpo.innerHTML = `
      <ol class="ifood-steps">
        <li class="ifood-step">No painel da Anota Aí, abra <strong>Configurações → Integrações</strong> e copie a chave de integração.</li>
        <li class="ifood-step">Cole abaixo. Ela vai para o cofre e nunca mais aparece nesta tela.</li>
      </ol>
      <div class="form-grid">
        <label class="full">Chave de integração<input name="integration_key" type="password" autocomplete="off" placeholder="Cole aqui a chave do painel da Anota Aí" /><small class="field-hint">Não serve a chave que aparece no endereço do navegador: ela é uma sessão de 15 minutos e é recusada.</small></label>
      </div>
      <button class="primary-button" type="button" data-anotaai="connect"><i data-lucide="plug"></i><span>Conectar</span></button>`;
  } else {
    const ultima = estado.last_catalog_sync ? formatDateTime(estado.last_catalog_sync) : "nunca";
    corpo.innerHTML = `
      <dl class="ifood-summary">
        <div><dt>Situação</dt><dd>Conectada</dd></div>
        <div><dt>Última importação de cardápio</dt><dd>${escapeHtml(ultima)}</dd></div>
      </dl>
      <button class="primary-button" type="button" data-anotaai="catalog"><i data-lucide="book-open"></i><span>Conferir o cardápio</span></button>
      <button class="secondary-button" type="button" data-anotaai="disconnect"><i data-lucide="unplug"></i><span>Desconectar</span></button>`;
  }
  refreshIcons();
}

function bindAnotaAi() {
  const corpo = document.getElementById("anotaai-body");
  if (!corpo) return;
  corpo.addEventListener("click", (event) => {
    const botao = event.target.closest("[data-anotaai]");
    if (!botao || botao.disabled) return;
    ({ connect: conectarAnotaAi, catalog: abrirCardapioAnotaAi, disconnect: desconectarAnotaAi })[botao.dataset.anotaai]?.(botao);
  });
}

async function conectarAnotaAi(botao) {
  const corpo = document.getElementById("anotaai-body");
  const campo = corpo.querySelector('[name="integration_key"]');
  const chave = (campo?.value || "").trim();
  if (chave.length < 8) return toast("Cole a chave de integração do painel da Anota Aí.", "error");
  botao.disabled = true;
  try {
    const resultado = await api("/api/integrations/anota-ai/connect", { method: "POST", body: JSON.stringify({ integration_key: chave }) });
    if (campo) campo.value = "";
    toast(`Conectada. O cardápio de lá tem ${number.format(resultado.items)} item(ns) em ${number.format(resultado.categories)} categoria(s).`, "success");
    await loadAnotaAi();
  } catch (error) {
    toast(error.message || "Não foi possível conectar.", "error");
  } finally {
    botao.disabled = false;
  }
}

async function desconectarAnotaAi() {
  if (!confirm("Desconectar a Anota Aí?\n\nA chave é apagada do cofre. Os produtos já importados continuam aqui.")) return;
  try {
    await api("/api/integrations/anota-ai", { method: "DELETE" });
    toast("Anota Aí desconectada.", "success");
    await loadAnotaAi();
  } catch (error) {
    toast(error.message || "Não foi possível desconectar.", "error");
  }
}

// Relatar antes de criar. Cardápio de loja acumula item sazonal e teste
// abandonado; despejar tudo no ERP encheria o catálogo de coisa que ninguém
// vende, e cada produto inventado vira uma ficha técnica para preencher.
async function abrirCardapioAnotaAi(botao) {
  botao.disabled = true;
  try {
    const previa = await api("/api/integrations/anota-ai/catalog");
    state.anotaCatalog = previa;
    document.getElementById("anotaai-catalog-readings").innerHTML = previa.readings.map((linha) => `
      <article class="insight ${linha.type === "attention" ? "attention" : ""}"><span class="insight-icon"><i data-lucide="${linha.type === "attention" ? "triangle-alert" : "info"}"></i></span><div><strong>${escapeHtml(linha.title)}</strong><p>${escapeHtml(linha.description)}</p></div></article>`).join("");
    setText("#anotaai-catalog-summary", `${previa.matched.length} produto(s) já casam · ${previa.new.length} item(ns) vendidos lá e sem cadastro aqui · ${previa.only_here.length} só existem aqui · ${previa.addons.length} complemento(s).`);
    document.getElementById("anotaai-catalog-detail").innerHTML = previa.new.length
      ? `<p class="panel-note">Estes são vendidos na Anota Aí e não existem no seu cardápio. Cada um é um pedido que chega sem produto identificado — e por isso sem custo e sem margem:</p>
         <div class="value-rows">${previa.new.map((linha) => `<div class="ladder-row"><span>${escapeHtml(linha.name)}${linha.unavailable ? " <small class=\"muted-value\">(fora do ar lá)</small>" : ""}</span><b>${currency.format(linha.price)}</b></div>`).join("")}</div>`
      : '<p class="empty-state">Todo item do cardápio de lá já tem produto aqui.</p>';
    document.getElementById("anotaai-import-confirm").hidden = !previa.new.length;
    openDialog("anotaai-catalog-modal");
    refreshIcons();
  } catch (error) {
    toast(error.message || "Não foi possível ler o cardápio.", "error");
  } finally {
    botao.disabled = false;
  }
}

async function importarCardapioAnotaAi() {
  const botao = document.getElementById("anotaai-import-confirm");
  botao.disabled = true;
  try {
    const resultado = await api("/api/integrations/anota-ai/catalog/import", { method: "POST", body: JSON.stringify({ anota_ids: [] }) });
    closeModal();
    const recusados = resultado.rejected?.length ? ` ${resultado.rejected.length} não entraram: ${resultado.rejected.map((linha) => linha.name).join(", ")}.` : "";
    toast(`${resultado.created.length} produto(s) criados sem ficha técnica.${recusados} Preencha a ficha para o custo deixar de ser desconhecido.`, "success");
    await loadAll();
    await loadAnotaAi();
  } catch (error) {
    toast(error.message || "Não foi possível importar o cardápio.", "error");
  } finally {
    botao.disabled = false;
  }
}

async function loadIFood() {
  const corpo = document.getElementById("ifood-body");
  if (!corpo) return;
  try {
    renderIFood(await api("/api/integrations/ifood"));
  } catch (error) {
    setText("#ifood-state", "Indisponível");
    corpo.innerHTML = `<p class="ifood-warning">${escapeHtml(error.message || "Não foi possível ler o estado da integração.")}</p>`;
  }
}

function renderIFood(estado) {
  const corpo = document.getElementById("ifood-body");
  setText("#ifood-state", IFOOD_ROTULOS[estado.status] || "Desconectado");
  if (estado.status === "aguardando_autorizacao") corpo.innerHTML = ifoodAguardando(estado);
  else if (estado.connected) corpo.innerHTML = ifoodConectado(estado);
  else corpo.innerHTML = ifoodDesconectado();
  refreshIcons();
}

function ifoodDesconectado() {
  return `
    <ol class="ifood-steps">
      <li class="ifood-step"><span>No Portal do Desenvolvedor do iFood, crie um aplicativo e copie a chave e o segredo dele.</span></li>
      <li class="ifood-step"><span>Gere aqui o código de ativação e leve-o ao Portal do Parceiro, em Serviços e soluções → Integrações → Ativar aplicativo via código.</span></li>
      <li class="ifood-step"><span>Cole de volta aqui o código de autorização que o portal devolver.</span></li>
    </ol>
    <div class="form-grid two">
      <label>Chave do aplicativo<input name="client_id" autocomplete="off" placeholder="clientId" /></label>
      <label>Segredo do aplicativo<input name="client_secret" type="password" autocomplete="off" placeholder="clientSecret" /></label>
      <p class="field-hint full">A chave e o segredo ficam no servidor e servem só para renovar o acesso sozinho. Nenhum dos dois volta para esta tela.</p>
    </div>
    <div class="ifood-actions"><button class="primary-button" type="button" data-ifood="start"><i data-lucide="key-round"></i><span>Gerar código de ativação</span></button></div>`;
}

function ifoodAguardando(estado) {
  const prazo = estado.code_expires_at ? ` Ele vale até ${timeOnly(estado.code_expires_at)}.` : "";
  return `
    <p class="field-hint">No Portal do Parceiro do iFood, abra Serviços e soluções → Integrações → Ativar aplicativo via código e informe este código:</p>
    <strong class="ifood-code">${escapeHtml(estado.user_code || "")}</strong>
    <p class="field-hint">O portal devolve um código de autorização; cole-o abaixo para concluir.${prazo}${estado.verification_url ? ` Portal: ${escapeHtml(estado.verification_url)}` : ""}</p>
    <div class="form-grid two">
      <label class="full">Código de autorização devolvido pelo portal<input name="authorization_code" autocomplete="off" placeholder="Cole aqui o código do portal" /></label>
    </div>
    <div class="ifood-actions">
      <button class="primary-button" type="button" data-ifood="confirm"><i data-lucide="check"></i><span>Concluir ativação</span></button>
      <button class="secondary-button" type="button" data-ifood="renew"><i data-lucide="refresh-cw"></i><span>Gerar outro código</span></button>
    </div>`;
}

function ifoodConectado(estado) {
  const soltos = estado.unmapped_items || [];
  return `
    <dl class="ifood-summary">
      <div><dt>Loja conectada</dt><dd>${escapeHtml(estado.merchant_name || estado.merchant_id || "—")}</dd></div>
      <div><dt>Aplicativo</dt><dd>${escapeHtml(estado.client_id_hint || "—")}</dd></div>
      <div><dt>Última busca</dt><dd>${estado.last_sync_at ? formatDateTime(estado.last_sync_at) : "ainda não houve"}</dd></div>
      <div><dt>Resultado</dt><dd>${escapeHtml(estado.last_sync_summary || "—")}</dd></div>
      <div><dt>Pedidos trazidos</dt><dd>${number.format(estado.orders_imported || 0)}</dd></div>
    </dl>
    ${estado.last_error ? `<p class="ifood-warning">Última falha: ${escapeHtml(estado.last_error)}</p>` : ""}
    ${soltos.length ? `<p class="ifood-warning">Sem produto correspondente no cardápio daqui: ${escapeHtml(soltos.join(", "))}. Enquanto o item do iFood não trouxer o código do produto, esses pedidos entram com o faturamento certo e o custo desconhecido — a margem deles fica de fora das contas.</p>` : ""}
    <p class="field-hint">Deste momento em diante o pedido do iFood entra por aqui. Se o Relatório de Pedidos da Anota Aí continuar sendo importado cobrindo os mesmos dias, o mesmo pedido entra duas vezes: importe só até a data da ativação.</p>
    <div class="ifood-actions">
      <button class="primary-button" type="button" data-ifood="sync"><i data-lucide="refresh-cw"></i><span>Buscar pedidos agora</span></button>
      <button class="danger-button" type="button" data-ifood="disconnect"><i data-lucide="unplug"></i><span>Desconectar</span></button>
    </div>`;
}

function bindIFood() {
  const corpo = document.getElementById("ifood-body");
  if (!corpo) return;
  corpo.addEventListener("click", (event) => {
    const botao = event.target.closest("[data-ifood]");
    if (botao && !botao.disabled) runIFood(botao.dataset.ifood);
  });
}

async function runIFood(nome) {
  const corpo = document.getElementById("ifood-body");
  const acao = IFOOD_ACOES[nome];
  if (!acao || runIFood.pending) return;
  const pedido = acao((campo) => (corpo.querySelector(`[name="${campo}"]`)?.value || "").trim());
  const botoes = [...corpo.querySelectorAll("[data-ifood]")];
  runIFood.pending = true;
  botoes.forEach((item) => { item.disabled = true; });
  corpo.setAttribute("aria-busy", "true");
  try {
    const resultado = await api(pedido.path, {
      method: pedido.method,
      ...(pedido.body ? { body: JSON.stringify(pedido.body) } : {}),
    });
    toast(resultado?.detail || resultado?.instructions || pedido.ok, "success");
    await loadIFood();
    // Pedido novo muda estoque, custo e faturamento: a tela inteira envelheceu.
    if (resultado?.created) await loadAll();
  } catch (error) {
    toast(error.message || "Não foi possível concluir a operação.", "error");
    botoes.forEach((item) => { item.disabled = false; });
  } finally {
    runIFood.pending = false;
    corpo.removeAttribute("aria-busy");
  }
}

function linkLabel(type) { return { expense: "Despesa", product: "Produto", inventory: "Insumo", supplier: "Fornecedor" }[type] || type; }
function linkTargetLabel(type, item) { return type === "expense" ? `${item.description} · ${currency.format(item.amount)}` : item.name; }
function hasSelectOption(select, value) { return [...select.options].some(option => option.value === value); }
function formatBytes(bytes) { if (!bytes) return "0 KB"; const units = ["B", "KB", "MB"]; const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1); return `${number.format(bytes / 1024 ** index)} ${units[index]}`; }
function paymentLabel(value) { return { Credito: "Crédito", Debito: "Débito", Dinheiro: "Dinheiro", Pix: "Pix" }[value] || value; }
function formatDate(value) { return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short", year: "numeric" }).format(new Date(`${value.slice(0, 10)}T12:00:00`)); }
function formatDateTime(value) { return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function dateInputValue(date) { return localDateTimeValue(date).slice(0, 10); }

function localDateTimeValue(date) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}
function emptyRow(columns, message) { return `<tr><td colspan="${columns}" class="muted-value">${message}</td></tr>`; }
function setText(selector, value) { const element = document.querySelector(selector); if (element) element.textContent = value; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char])); }
function refreshIcons() { window.lucide?.createIcons(); }
// Enquanto a tela recarrega, as tabelas são reescritas por baixo do dedo do
// gestor: ele clica em "excluir" e o botão deixa de existir no meio do clique,
// então nada acontece e nada explica. Marcar o corpo desliga as ações de linha
// durante a recarga — o clique espera meio segundo em vez de se perder.
function setRefreshing(value) {
  const button = document.getElementById("refresh-data");
  button.disabled = value;
  button.classList.toggle("is-loading", value);
  document.body.classList.toggle("is-refreshing", value);
}
function toast(message, type) { const element = document.getElementById("toast"); element.textContent = message; element.className = `toast ${type}`; element.hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => { element.hidden = true; }, 4200); }


// ---------------------------------------------------------------------------
// Fechamento do dia, canais e linhas de marca
// ---------------------------------------------------------------------------

async function loadClosing() {
  try {
    state.salesDay = await api(`/api/sales/daily${state.closingDate ? `?day=${state.closingDate}` : ""}`);
    renderClosing();
  } catch (error) {
    toast(error.message || "Não foi possível carregar o fechamento.", "error");
  }
}

function renderClosing() {
  const day = state.salesDay;
  if (!day) return;
  const input = document.getElementById("closing-date");
  if (input && !input.value) input.value = day.date;
  const t = day.totals;

  setText("#day-revenue", currency.format(t.revenue));
  setText("#day-orders", number.format(t.orders));
  setText("#day-ticket", t.orders ? `${currency.format(t.average_ticket)} por pedido` : "nenhuma venda registrada");
  setText("#day-net", currency.format(t.net_revenue));
  setText("#day-fee", t.channel_fee ? `${currency.format(t.channel_fee)} ficaram com os canais` : "nenhuma taxa de canal no dia");
  // Sem nenhum pedido com custo lançado a margem é desconhecida, não zero.
  setText("#day-margin", t.margin_coverage <= 0 ? "—" : `${number.format(t.gross_margin)}%`);
  setText("#day-profit", t.margin_coverage <= 0
    ? "nenhum pedido do dia tem custo lançado"
    : t.margin_coverage >= 99.5
      ? `${currency.format(t.gross_profit)} de lucro bruto`
      : `sobre ${number.format(t.margin_coverage)}% do faturamento do dia`);

  const compare = document.getElementById("day-compare");
  if (compare) {
    if (day.comparison.change === null) {
      compare.textContent = `primeira ${day.weekday} com movimento`;
      compare.className = "";
    } else {
      const signal = day.comparison.change >= 0 ? "+" : "";
      compare.textContent = `${signal}${number.format(day.comparison.change)}% vs. média de ${day.weekday}`;
      compare.className = day.comparison.change >= 0 ? "positive" : "negative";
    }
  }

  renderShareList("day-channel-list", day.by_channel, "Nenhuma venda neste dia.");
  renderShareList("day-payment-list", day.by_payment, "Nenhum recebimento neste dia.");

  document.getElementById("day-product-body").innerHTML = day.by_product.length
    ? day.by_product.map((row) => `<tr><td><strong>${escapeHtml(row.name)}</strong></td><td class="align-right">${number.format(row.quantity)}</td><td class="align-right">${currency.format(row.value)}</td></tr>`).join("")
    : emptyRow(3, "Nenhum produto vendido — ou os pedidos do dia vieram sem itens detalhados.");

  document.getElementById("day-alerts").innerHTML = closingAlerts(day).map((item) => `
    <article class="insight ${item.type}"><span class="insight-icon"><i data-lucide="${item.icon}"></i></span><div><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.description)}</p></div></article>`).join("");

  setText("#day-order-count", `${day.totals.orders} pedido${day.totals.orders === 1 ? "" : "s"}`);
  document.getElementById("day-order-body").innerHTML = day.orders.length
    ? day.orders.map((order) => `<tr><td><strong>#${order.number}</strong></td><td class="muted-value">${escapeHtml(timeOnly(order.created_at))}</td><td><span class="channel-pill">${escapeHtml(order.channel)}</span></td><td>${escapeHtml(itemSummary(order) || "sem itens detalhados")}</td><td class="align-right"><strong>${currency.format(order.total)}</strong></td><td class="align-right">${orderActions(order)}</td></tr>`).join("")
    : emptyRow(6, "Nenhum pedido neste dia. Use Lançar venda para cadastrar o movimento.");
  refreshIcons();
}

// O que precisa de conferência antes de fechar o caixa, na ordem em que pesa.
function closingAlerts(day) {
  const alerts = [];
  const t = day.totals;
  if (t.loss_cost > 0) {
    alerts.push({ type: "attention", icon: "trash-2", title: "Perdas lançadas hoje", description: `${currency.format(t.loss_cost)} de insumo saíram sem virar venda. Confira se foi quebra, degustação ou erro de lançamento.` });
  }
  if (t.cancelled) {
    alerts.push({ type: "attention", icon: "ban", title: `${t.cancelled} pedido${t.cancelled === 1 ? "" : "s"} cancelado${t.cancelled === 1 ? "" : "s"}`, description: `${currency.format(t.cancelled_value)} saíram do faturamento e o insumo voltou para o estoque.` });
  }
  if (t.channel_fee > 0) {
    const share = t.revenue ? (t.channel_fee / t.revenue) * 100 : 0;
    alerts.push({ type: "info", icon: "scissors", title: "Taxa dos canais", description: `${number.format(share)}% do faturamento do dia ficou com os canais de distribuição.` });
  }
  if (day.expenses_due.length) {
    const total = day.expenses_due.reduce((sum, row) => sum + row.amount, 0);
    alerts.push({ type: "attention", icon: "calendar-clock", title: `${day.expenses_due.length} conta${day.expenses_due.length === 1 ? "" : "s"} vencendo hoje`, description: `${currency.format(total)} — ${day.expenses_due.map((row) => row.description).slice(0, 3).join(", ")}.` });
  }
  if (t.discount > 0) {
    alerts.push({ type: "info", icon: "tag", title: "Descontos concedidos", description: `${currency.format(t.discount)} em desconto nos pedidos do dia.` });
  }
  if (!alerts.length) {
    alerts.push({ type: "info", icon: "check", title: "Dia sem pendência", description: "Nenhuma perda, cancelamento ou conta vencendo. O caixa fecha com o que foi vendido." });
  }
  return alerts.slice(0, 4);
}

function renderShareList(elementId, rows, emptyMessage) {
  const container = document.getElementById(elementId);
  if (!container) return;
  const total = rows.reduce((sum, row) => sum + row.value, 0);
  container.innerHTML = rows.length ? rows.map((row) => {
    const percentage = total ? (row.value / total) * 100 : 0;
    return `<div class="channel-row"><span>${escapeHtml(row.name)}</span><span class="channel-meter"><span style="width:${percentage}%"></span></span><strong>${currency.format(row.value)}</strong></div>`;
  }).join("") : `<p class="empty-state">${escapeHtml(emptyMessage)}</p>`;
}

function renderChannelsView() {
  const performance = state.channelPerformance;
  if (performance) {
    setText("#ch-revenue", currency.format(performance.totals.revenue));
    setText("#ch-fee", currency.format(performance.totals.fee));
    const share = performance.totals.revenue ? (performance.totals.fee / performance.totals.revenue) * 100 : 0;
    setText("#ch-fee-share", performance.totals.fee ? `${number.format(share)}% do faturamento` : "nenhuma comissão no período");
    setText("#ch-net", currency.format(performance.totals.net_revenue));

    document.getElementById("channel-performance-body").innerHTML = performance.channels.length
      ? performance.channels.map((row) => {
          const margin = row.contribution_margin === null
            ? '<span class="muted-value" title="Os pedidos deste canal vieram sem custo lançado.">—</span>'
            : `<span class="status-pill ${row.contribution_margin > 0 ? "success" : "danger"}">${number.format(row.contribution_margin)}%</span>`;
          const warning = row.registered ? "" : '<span class="row-note">canal não cadastrado</span>';
          return `<tr><td><strong>${escapeHtml(row.name)}</strong>${warning}<span class="row-note">${number.format(row.share)}% das vendas</span></td><td class="align-right">${number.format(row.orders)}</td><td class="align-right">${currency.format(row.revenue)}</td><td class="align-right ${row.fee ? "negative" : "muted-value"}">${row.fee ? `-${currency.format(row.fee)}` : "—"}</td><td class="align-right"><strong>${currency.format(row.net_revenue)}</strong></td><td class="align-right muted-value">${currency.format(row.average_ticket)}</td><td>${margin}</td></tr>`;
        }).join("")
      : emptyRow(7, "Nenhuma venda por canal no período.");

    if (performance.idle_channels.length) {
      document.getElementById("channel-performance-body").insertAdjacentHTML("beforeend",
        `<tr><td colspan="7" class="muted-value">Sem movimento no período: ${escapeHtml(performance.idle_channels.join(", "))}.</td></tr>`);
    }
  }

  document.getElementById("channel-body").innerHTML = state.channels.length
    ? state.channels.map((channel) => `<tr class="${channel.active ? "" : "cancelled-row"}"><td><strong>${escapeHtml(channel.name)}</strong></td><td class="align-right">${channel.fee_percent ? `${number.format(channel.fee_percent)}%` : '<span class="muted-value">—</span>'}</td><td class="align-right">${channel.fee_fixed ? currency.format(channel.fee_fixed) : '<span class="muted-value">—</span>'}</td><td><span class="status-pill ${channel.active ? "success" : "neutral"}">${channel.active ? "Ativo" : "Inativo"}</span></td><td class="muted-value">${escapeHtml(channel.note || "—")}</td><td class="align-right"><div class="row-actions">
        <button class="row-action" type="button" data-edit-channel="${channel.id}" title="Editar canal"><i data-lucide="pencil"></i></button>
        <button class="row-action danger" type="button" data-delete-channel="${channel.id}" title="Remover canal"><i data-lucide="trash-2"></i></button>
      </div></td></tr>`).join("")
    : emptyRow(6, "Nenhum canal cadastrado.");
  refreshIcons();
}

function renderBrand() {
  const data = state.lines;
  if (!data) return;

  document.getElementById("line-grid").innerHTML = data.lines.length ? data.lines.map((line) => {
    const inativa = line.revenue === 0;
    return `<article class="line-card ${inativa ? "idle" : ""}">
      <header><span class="line-pill line-${escapeHtml(String(line.name).toLowerCase())}">${escapeHtml(line.name)}</span><span class="muted-value">${line.active_products} de ${line.products} no cardápio</span></header>
      <strong>${currency.format(line.revenue)}</strong>
      <p>${inativa ? "sem venda nos últimos 30 dias" : `${number.format(line.share)}% do faturamento`}</p>
      <div class="line-stats">
        <span>Margem<b>${inativa ? "—" : `${number.format(line.margin)}%`}</b></span>
        <span>Unidades<b>${number.format(line.units)}</b></span>
        <span>Ticket<b>${inativa ? "—" : currency.format(line.average_ticket)}</b></span>
      </div>
    </article>`;
  }).join("") : '<p class="empty-state">Nenhuma linha cadastrada ainda.</p>';

  const linhas = ["all", ...new Set(data.catalog.map((row) => row.line))];
  document.getElementById("line-filters").innerHTML = linhas.map((line) => `
    <button type="button" class="filter-chip ${state.lineFilter === line ? "active" : ""}" data-line-filter="${escapeHtml(line)}">${line === "all" ? "Todas" : escapeHtml(line)}</button>`).join("");

  const rows = data.catalog.filter((row) => state.lineFilter === "all" || row.line === state.lineFilter);
  document.getElementById("brand-catalog-body").innerHTML = rows.length ? rows.map((row) => `
    <tr class="${row.active ? "" : "cancelled-row"}"><td><strong>${escapeHtml(row.name)}</strong></td><td><span class="line-pill line-${escapeHtml(String(row.line).toLowerCase())}">${escapeHtml(row.line)}</span></td><td class="align-right">${currency.format(row.price)}</td><td class="align-right muted-value">${currency.format(row.cost)}</td><td class="align-right"><strong>${number.format(row.margin)}%</strong></td><td class="muted-value">${(row.claims || []).length ? escapeHtml(row.claims.join(", ")) : "—"}</td><td class="align-right"><div class="row-actions">
      <button class="row-action" type="button" data-dossier="${row.id}" title="Dossiê de custo"><i data-lucide="file-search"></i></button>
      <button class="row-action" type="button" data-edit-product="${row.id}" title="Editar produto"><i data-lucide="pencil"></i></button>
      ${row.active
        ? `<button class="row-action danger" type="button" data-delete-product="${row.id}" title="Tirar do cardápio"><i data-lucide="trash-2"></i></button>`
        : `<button class="row-action danger" type="button" data-purge-product="${row.id}" title="Excluir do cadastro"><i data-lucide="trash-2"></i></button>`}
    </div></td></tr>`).join("") : emptyRow(7, "Nenhum produto nesta linha.");
  refreshIcons();
}

function timeOnly(value) { return new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function todayInputValue() { const now = new Date(); return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }


// ---------------------------------------------------------------------------
// Crescimento: o que fazer para a loja crescer, com a conta aberta
// ---------------------------------------------------------------------------

async function loadPromotions() {
  try {
    const [semana, mes] = await Promise.all([
      api(`/api/promotions/week?days=${state.promotionsPeriod}&horizon=7`),
      api(`/api/promotions/month?days=${state.promotionsPeriod}&weeks=4`),
    ]);
    state.promotions = semana;
    state.promotionMonth = mes;
    renderPromotions();
    renderPromotionMonth();
  } catch (error) {
    toast(error.message || "Não foi possível carregar as promoções.", "error");
  }
}

async function loadPricing() {
  try {
    const [orientacao, referencias] = await Promise.all([
      api(`/api/pricing?days=${state.pricingPeriod}`),
      api("/api/market-prices"),
    ]);
    state.pricing = orientacao;
    state.marketPrices = referencias;
    renderPricing();
  } catch (error) {
    toast(error.message || "Não foi possível carregar os preços.", "error");
  }
}

async function loadGrowth() {
  try {
    state.growth = await api(`/api/growth?days=${state.growthPeriod}`);
    renderGrowth();
  } catch (error) {
    toast(error.message || "Não foi possível carregar as oportunidades.", "error");
  }
}

const GROWTH_TYPES = {
  combo: { label: "Combo", icon: "layers" },
  produto: { label: "Produto", icon: "cup-soda" },
  servico: { label: "Serviço", icon: "concierge-bell" },
  preco: { label: "Preço", icon: "tag" },
  canal: { label: "Canal", icon: "route" },
  marketing: { label: "Marketing", icon: "megaphone" },
};

function renderGrowth() {
  const data = state.growth;
  if (!data) return;
  const { context: ctx, totals } = data;

  setText("#growth-impact", totals.impact_monthly ? currency.format(totals.impact_monthly) : "—");
  setText("#growth-impact-note", totals.quantified
    ? `somando ${totals.quantified} sugestão${totals.quantified === 1 ? "" : "ões"} com número`
    : "nenhuma sugestão pôde ser quantificada ainda");
  setText("#growth-count", number.format(totals.count));
  setText("#growth-quantified", `${totals.count - totals.quantified} sem valor calculável`);
  setText("#growth-orders", `${number.format(ctx.orders)} pedidos`);
  // Cesta e mix só enxergam o pedido que tem itens. Sem dizer isso, a sugestão
  // parece cobrir a loja inteira quando cobre só a parte detalhada.
  setText("#growth-coverage", ctx.detail_coverage >= 99.5
    ? `todos com itens detalhados`
    : `${number.format(ctx.detail_coverage)}% com itens detalhados`);
  setText("#growth-identified", `${number.format(ctx.identified_share)}%`);
  setText("#growth-identified-note", `${number.format(ctx.identified_customers)} pedidos com nome do cliente`);

  const badge = document.getElementById("nav-growth-count");
  badge.hidden = totals.count === 0;
  badge.textContent = totals.count;

  const tipos = ["all", ...data.by_type.map((row) => row.type)];
  document.getElementById("growth-filters").innerHTML = tipos.map((tipo) => {
    const total = tipo === "all" ? totals.count : data.by_type.find((row) => row.type === tipo)?.count || 0;
    const label = tipo === "all" ? "Todas" : GROWTH_TYPES[tipo]?.label || tipo;
    return `<button type="button" class="filter-chip ${state.growthFilter === tipo ? "active" : ""}" data-growth-filter="${escapeHtml(tipo)}">${escapeHtml(label)} <b>${total}</b></button>`;
  }).join("");

  const rows = data.suggestions.filter((row) => state.growthFilter === "all" || row.type === state.growthFilter);
  document.getElementById("growth-list").innerHTML = rows.length
    ? rows.map(growthCard).join("")
    : '<p class="empty-state">Nenhuma sugestão deste tipo no período analisado.</p>';

  const blockers = document.getElementById("growth-blockers");
  document.getElementById("growth-blockers-panel").hidden = data.blockers.length === 0;
  blockers.innerHTML = data.blockers.map((row) => `
    <article class="insight attention"><span class="insight-icon"><i data-lucide="triangle-alert"></i></span><div><strong>${escapeHtml(row.title)}</strong><p>${escapeHtml(row.description)}</p></div></article>`).join("");
  refreshIcons();
}

// ------------------------------------------------------------ promoções
//
// A tela existe para uma coisa: não deixar o desconto sair sem que o gestor
// veja o piso e o ponto de virada. Card sem esses dois números vira o conselho
// genérico que o resto do sistema se recusa a dar.

const PROMO_TYPES = {
  "vale-da-semana": { label: "Vale da semana", icon: "calendar-off" },
  "estoque-em-risco": { label: "Estoque em risco", icon: "hourglass" },
  "linha-sport": { label: "Linha Sport", icon: "dumbbell" },
};

function renderPromotionMonth() {
  const data = state.promotionMonth;
  if (!data) return;

  setText("#promotions-split-note", data.split_note || "");
  document.getElementById("promotions-blocks").innerHTML = data.blocks.map((bloco) => {
    const papel = bloco.id === data.weak_block ? "vazio" : "cheio";
    const alavanca = papel === "vazio" ? "pede preço" : "pede ticket";
    return `<article class="block-card ${papel}">
      <p class="eyebrow">${escapeHtml(bloco.name)} · ${escapeHtml(alavanca)}</p>
      <strong>${bloco.average_per_open_day === null ? "—" : currency.format(bloco.average_per_open_day)}</strong>
      <small>por dia aberto</small>
      <div class="block-metrics">
        <span>Ticket<b>${bloco.average_ticket === null ? "—" : currency.format(bloco.average_ticket)}</b></span>
        <span>Do faturamento<b>${number.format(bloco.share)}%</b></span>
        <span>Dias abertos<b class="${bloco.closed_days ? "negative" : ""}">${number.format(bloco.open_days)} de ${number.format(bloco.possible_days)}</b></span>
      </div>
    </article>`;
  }).join("");

  document.getElementById("promotions-month").innerHTML = data.calendar.map((semana) => {
    const ofertas = semana.promotions.length
      ? semana.promotions.map((oferta) => promotionCard(oferta, oferta.id === semana.headline)).join("")
      : '<p class="empty-state">Nenhuma oferta pôde ser montada para esta semana com o que está cadastrado.</p>';
    return `<section class="month-week">
      <header><span class="field-label">${escapeHtml(semana.label)}</span><small>${escapeHtml(formatDate(semana.starts_on))} a ${escapeHtml(formatDate(semana.ends_on))}</small></header>
      ${ofertas}
    </section>`;
  }).join("");
  refreshIcons();
}

function renderPromotions() {
  const data = state.promotions;
  if (!data) return;
  const { coverage, channel } = data;
  // Os cartões do topo falam do mês, que é a unidade em que o gestor decide;
  // a tabela de dias abaixo continua sendo o detalhe que sustenta o corte.
  const totals = state.promotionMonth?.totals || data.totals;

  setText("#promotions-count", number.format(totals.count));
  setText("#promotions-source", `escolhidas por ${coverage.source}`);
  setText("#promotions-upside", totals.upside_monthly ? currency.format(totals.upside_monthly) : "—");
  setText("#promotions-risk", data.totals.margin_at_risk_monthly ? currency.format(data.totals.margin_at_risk_monthly) : "—");
  setText("#promotions-channel", channel.name);
  setText("#promotions-channel-note", channel.reason || "");

  const badge = document.getElementById("nav-promotions-count");
  badge.hidden = totals.count === 0;
  badge.textContent = totals.count;

  document.getElementById("promotions-week-body").innerHTML = data.week.map((dia) => {
    const fator = dia.factor === null
      ? '<span class="muted-value">sem base</span>'
      : `<span class="status-pill ${dia.is_valley ? "pending" : "success"}">${number.format(Math.round((dia.factor - 1) * 100))}%</span>`;
    // Aberturas que faltaram é a leitura que mais muda decisão: promover um dia
    // fechado é gastar margem numa porta que não abriu.
    const leitura = dia.mostly_closed
      ? `<strong class="negative">Fechou ${dia.closed_days} vez${dia.closed_days === 1 ? "" : "es"}</strong> — decidir abrir vem antes de decidir promover.`
      : dia.is_valley ? "Vale: é aqui que a promoção se paga."
      : dia.factor === null ? "Ainda sem observações suficientes."
      : "Dia cheio: descontar aqui compra venda que já viria.";
    return `<tr><td><strong>${escapeHtml(dia.name)}</strong><small class="muted-value"> ${escapeHtml(formatDate(dia.date))}</small></td><td class="align-right">${dia.average_revenue === null ? '<span class="muted-value">—</span>' : currency.format(dia.average_revenue)}</td><td class="align-right">${fator}</td><td class="align-right ${dia.mostly_closed ? "negative" : "muted-value"}">${number.format(dia.open_days)}</td><td>${leitura}</td></tr>`;
  }).join("");

  document.getElementById("promotions-list").innerHTML = data.promotions.length
    ? data.promotions.map(promotionCard).join("")
    : '<p class="empty-state">Nenhuma promoção sugerida para esta semana. Veja abaixo o que precisa ser decidido antes — o módulo prefere não sugerir a sugerir desconto sem base.</p>';

  document.getElementById("promotions-anchors-body").innerHTML = data.anchors.length
    ? data.anchors.map((linha) => `<tr><td><strong>${escapeHtml(linha.name)}</strong>${linha.note ? `<small class="muted-value"> ${escapeHtml(linha.note)}</small>` : ""}</td><td>${escapeHtml(linha.kind)}</td><td class="muted-value">${escapeHtml(linha.weekday_names.join(", "))}</td><td class="muted-value">${escapeHtml(linha.time_of_day)}</td><td class="align-right">${linha.people ? number.format(linha.people) : '<span class="muted-value">—</span>'}</td><td class="align-right"><div class="row-actions"><button class="row-action" type="button" data-edit-anchor="${linha.id}" title="Editar âncora"><i data-lucide="pencil"></i></button></div></td></tr>`).join("")
    : emptyRow(6, "Nenhuma âncora cadastrada. Sem ela o módulo não sugere ação esportiva — ele não inventa quando o bairro treina.");

  const blockers = document.getElementById("promotions-blockers");
  document.getElementById("promotions-blockers-panel").hidden = data.blockers.length === 0;
  blockers.innerHTML = data.blockers.map((row) => `
    <article class="insight attention"><span class="insight-icon"><i data-lucide="triangle-alert"></i></span><div><strong>${escapeHtml(row.title)}</strong><p>${escapeHtml(row.description)}</p></div></article>`).join("");
  refreshIcons();
}

function promotionCard(row, titular = false) {
  const tipo = PROMO_TYPES[row.kind] || { label: row.kind, icon: "megaphone" };
  const confianca = { alta: "success", media: "pending", baixa: "neutral" }[row.confidence] || "neutral";
  // O veredito de preço viaja junto da oferta: promoção não conserta preço
  // errado, e descontar um item que já está barato demais é perder duas vezes.
  const preco = row.pricing && row.pricing.verdict !== "na-faixa"
    ? `<p class="growth-premise ${row.pricing.verdict === "abaixo-do-custo" || row.pricing.verdict === "espaco-para-subir" ? "warning" : ""}"><i data-lucide="tag"></i><span><b>Preço-base: ${escapeHtml(row.pricing.action)}.</b> ${escapeHtml(row.pricing.headline)}${row.pricing.suggested_price ? ` Sugerido: ${currency.format(row.pricing.suggested_price)}.` : ""}</span></p>`
    : "";
  const marca = titular ? '<span class="status-pill success" title="Maior ganho entre as ofertas da semana">rode esta se só der para uma</span>' : "";
  // O ponto de virada é o motivo de a tela existir: sem ele o card seria só um
  // desconto bonito.
  const virada = row.breakeven && row.breakeven.units !== null
    ? `<div class="promo-breakeven"><span class="field-label">Ponto de virada</span><strong>+${number.format(row.breakeven.units)} unidade${row.breakeven.units === 1 ? "" : "s"} nesta ${escapeHtml(row.weekday)}</strong><p>${escapeHtml(row.breakeven.note)}</p></div>`
    : '<div class="promo-breakeven none"><span class="field-label">Ponto de virada</span><p>Sem volume medido por produto neste dia o ponto de virada não pode ser calculado. Importe os itens da Anota Aí para ele aparecer.</p></div>';
  const impacto = row.upside_monthly
    ? `<span class="growth-impact"><b>+${currency.format(row.upside_monthly)}</b><small>por mês</small></span>`
    : '<span class="growth-impact none"><b>sem número</b><small>premissa sem base</small></span>';
  return `<article class="growth-card promo-card" data-promo-kind="${escapeHtml(row.kind)}">
    <div class="growth-rank">${row.rank}</div>
    <div class="growth-body">
      <div class="growth-top">
        <span class="growth-type"><i data-lucide="${tipo.icon}"></i>${escapeHtml(row.goal || tipo.label)} · ${escapeHtml(row.block_name || row.weekday)}</span>
        ${marca}
        <span class="status-pill ${confianca}" title="${escapeHtml(row.confidence_note)}">${escapeHtml(row.source)}</span>
      </div>
      <h3>${escapeHtml(row.title)}</h3>
      <div class="promo-price">
        <span class="promo-from">${currency.format(row.price.from)}</span>
        <i data-lucide="arrow-right"></i>
        <span class="promo-to">${currency.format(row.price.to)}</span>
        <span class="status-pill pending">-${number.format(row.price.discount_percent)}%</span>
      </div>
      <p class="promo-floor"><i data-lucide="shield"></i><span>Piso: custo com quebra de ${currency.format(row.floor.cost_with_shrinkage)}${row.floor.min_price ? `, preço mínimo de ${currency.format(row.floor.min_price)}` : ""}. O desconto máximo antes de zerar a sobra é ${number.format(row.floor.max_discount_percent)}%.</span></p>
      ${preco}
      ${virada}
      <p class="growth-evidence"><i data-lucide="search"></i><span>${escapeHtml(row.evidence)}</span></p>
      <p class="growth-action"><i data-lucide="arrow-right"></i><span>${escapeHtml(row.why_this_day)}</span></p>
      ${row.channel ? `<p class="growth-premise"><i data-lucide="route"></i><span>Publicar em ${escapeHtml(row.channel)}. ${escapeHtml(row.channel_note || "")}</span></p>` : ""}
      ${row.upside_note ? `<p class="growth-premise"><i data-lucide="scale"></i><span>${escapeHtml(row.upside_note)}</span></p>` : ""}
    </div>
    ${impacto}
  </article>`;
}

// ---------------------------------------------------------------- preços
//
// A tela não recomenda preço a partir do custo e finge que isso é leitura de
// mercado. Ela separa as três âncoras e diz qual está faltando.

const PRICING_VERDICTS = {
  "abaixo-do-custo": { icon: "triangle-alert", css: "danger" },
  "canal-no-prejuizo": { icon: "triangle-alert", css: "danger" },
  "espaco-para-subir": { icon: "trending-up", css: "success" },
  "acima-do-mercado": { icon: "gem", css: "pending" },
  "na-faixa": { icon: "check", css: "neutral" },
  "sem-referencia": { icon: "help-circle", css: "neutral" },
};

function renderPricing() {
  const data = state.pricing;
  if (!data) return;
  const { totals, coverage, store_market: mercado } = data;

  setText("#pricing-room", number.format(totals.room_to_raise));
  setText("#pricing-room-note", totals.monthly_upside
    ? `até ${currency.format(totals.monthly_upside)} por mês, no volume medido`
    : "ganho não projetável sem volume por produto");
  setText("#pricing-below", number.format(totals.below_floor));
  setText("#pricing-ticket", mercado.average_ticket ? currency.format(mercado.average_ticket) : "—");
  setText("#pricing-ticket-note", mercado.orders ? `${number.format(mercado.orders)} pedidos no período` : "sem pedidos no período");
  setText("#pricing-references", number.format(coverage.reference_count));
  setText("#pricing-competitors", coverage.competitors.length
    ? coverage.competitors.slice(0, 3).join(", ")
    : "nenhum concorrente cadastrado");

  const badge = document.getElementById("nav-pricing-count");
  badge.hidden = totals.below_floor + totals.room_to_raise === 0;
  badge.textContent = totals.below_floor + totals.room_to_raise;

  document.getElementById("pricing-list").innerHTML = data.products.length
    ? data.products.map(pricingCard).join("")
    : '<p class="empty-state">Nenhum produto ativo com ficha técnica para precificar.</p>';

  document.getElementById("pricing-market-body").innerHTML = state.marketPrices.length
    ? state.marketPrices.map((linha) => {
        const produto = state.products.find((p) => p.id === linha.product_id);
        const alvo = produto ? escapeHtml(produto.name) : linha.category ? `${escapeHtml(linha.category)} <em>(categoria)</em>` : '<span class="muted-value">—</span>';
        return `<tr><td><strong>${escapeHtml(linha.competitor)}</strong></td><td>${escapeHtml(linha.description)}</td><td>${alvo}</td><td class="muted-value">${escapeHtml(linha.channel || "—")}</td><td class="align-right"><strong>${currency.format(linha.price)}</strong></td><td class="muted-value">${linha.observed_on ? escapeHtml(formatDate(linha.observed_on)) : "—"}</td><td class="align-right"><div class="row-actions"><button class="row-action" type="button" data-edit-market-price="${linha.id}" title="Editar referência"><i data-lucide="pencil"></i></button></div></td></tr>`;
      }).join("")
    : emptyRow(7, "Nenhuma referência cadastrada. Sem elas o preço só pode ser julgado por custo e margem-alvo.");

  document.getElementById("pricing-store-body").innerHTML = mercado.channels.length
    ? mercado.channels.map((canal) => `<tr><td><strong>${escapeHtml(canal.name)}</strong></td><td class="align-right muted-value">${number.format(canal.orders)}</td><td class="align-right"><strong>${currency.format(canal.average_ticket)}</strong></td></tr>`).join("")
    : emptyRow(3, "Sem pedidos no período.");
  document.getElementById("pricing-bands").innerHTML = mercado.price_bands.length
    ? mercado.price_bands.map((faixa) => `<span><b>${number.format(faixa.share)}%</b>${escapeHtml(faixa.band)}</span>`).join("")
    : "";

  document.getElementById("pricing-blockers-panel").hidden = data.blockers.length === 0;
  document.getElementById("pricing-blockers").innerHTML = data.blockers.map((row) => `
    <article class="insight attention"><span class="insight-icon"><i data-lucide="triangle-alert"></i></span><div><strong>${escapeHtml(row.title)}</strong><p>${escapeHtml(row.description)}</p></div></article>`).join("");
  refreshIcons();
}

function pricingCard(row) {
  const v = row.verdict;
  const estilo = PRICING_VERDICTS[v.id] || { icon: "tag", css: "neutral" };
  const mercado = row.market.samples
    ? `<p class="growth-evidence"><i data-lucide="tags"></i><span>Mercado: mediana de ${currency.format(row.market.median)} entre ${currency.format(row.market.min)} e ${currency.format(row.market.max)}, em ${row.market.samples} referência(s) por ${escapeHtml(row.market.match)}${row.market.stale_share >= 50 ? " — a maioria com mais de 120 dias" : ""}.</span></p>`
    : '<p class="growth-evidence"><i data-lucide="tags"></i><span>Mercado: sem referência cadastrada para este produto.</span></p>';
  const canais = row.channels.map((canal) => `<span>${escapeHtml(canal.name)}<b class="${canal.contribution > 0 ? "" : "negative"}">${currency.format(canal.contribution)}</b></span>`).join("");
  const sugerido = v.suggested_price
    ? `<span class="growth-impact"><b>${currency.format(v.suggested_price)}</b><small>preço sugerido</small></span>`
    : `<span class="growth-impact none"><b>${number.format(row.margin)}%</b><small>margem atual</small></span>`;
  return `<article class="growth-card" data-pricing-verdict="${escapeHtml(v.id)}">
    <div class="growth-rank"><i data-lucide="${estilo.icon}"></i></div>
    <div class="growth-body">
      <div class="growth-top">
        <span class="growth-type">${escapeHtml(row.name)}</span>
        <span class="status-pill ${estilo.css}">${escapeHtml(v.action)}</span>
      </div>
      <h3>${escapeHtml(v.headline)}</h3>
      <p class="growth-action"><i data-lucide="arrow-right"></i><span>${escapeHtml(v.reason)}</span></p>
      ${mercado}
      <p class="growth-premise"><i data-lucide="shield"></i><span>Piso: ${currency.format(row.cost.with_shrinkage)} de custo com quebra (ficha ${currency.format(row.cost.recipe)}). Escada: ${row.ladder.filter((l) => l.price).map((l) => `${l.margin}% = ${currency.format(l.price)}`).join(" · ")}.</span></p>
      <div class="growth-metrics">${canais}</div>
    </div>
    ${sugerido}
  </article>`;
}

function growthCard(row) {
  const tipo = GROWTH_TYPES[row.type] || { label: row.type, icon: "lightbulb" };
  const confidence = { alta: "success", media: "pending", baixa: "neutral" }[row.confidence] || "neutral";
  const impact = row.impact_monthly !== null
    ? `<span class="growth-impact"><b>+${currency.format(row.impact_monthly)}</b><small>por mês</small></span>`
    : `<span class="growth-impact none"><b>sem valor</b><small>calculável</small></span>`;
  const metrics = (row.metrics || []).length
    ? `<div class="growth-metrics">${row.metrics.map((m) => `<span>${escapeHtml(m.label)}<b>${escapeHtml(m.value)}</b></span>`).join("")}</div>`
    : "";
  return `<article class="growth-card" data-growth-type="${escapeHtml(row.type)}">
    <div class="growth-rank">${row.rank}</div>
    <div class="growth-body">
      <div class="growth-top">
        <span class="growth-type"><i data-lucide="${tipo.icon}"></i>${escapeHtml(tipo.label)}</span>
        <span class="status-pill ${confidence}" title="${escapeHtml(row.confidence_note)}">confiança ${escapeHtml(row.confidence)}</span>
      </div>
      <h3>${escapeHtml(row.title)}</h3>
      <p class="growth-evidence"><i data-lucide="search"></i><span>${escapeHtml(row.evidence)}</span></p>
      <p class="growth-action"><i data-lucide="arrow-right"></i><span>${escapeHtml(row.action)}</span></p>
      ${metrics}
      ${row.impact_note ? `<p class="growth-premise"><i data-lucide="scale"></i><span>${escapeHtml(row.impact_note)}</span></p>` : ""}
    </div>
    ${impact}
  </article>`;
}


// ---------------------------------------------------------------------------
// Planejamento: quanto preparar e quando pedir
// ---------------------------------------------------------------------------

async function loadPlanning() {
  try {
    state.planning = await api(`/api/planning?days=90&horizon=7&service_level=${state.planningService}`);
    renderPlanning();
  } catch (error) {
    toast(error.message || "Não foi possível carregar o planejamento.", "error");
  }
}

function renderPlanning() {
  const plan = state.planning;
  if (!plan) return;

  setText("#pl-production", currency.format(plan.production_total));
  setText("#pl-production-day", plan.production.length
    ? `${plan.production.length} insumo(s) para ${plan.target_weekday}`
    : "nenhum insumo perecível com histórico");
  const pedir = plan.purchase.filter((row) => row.reorder_now);
  setText("#pl-purchase", currency.format(plan.purchase_total));
  setText("#pl-purchase-count", pedir.length ? `${pedir.length} item(ns) no ponto de pedido` : "nada no ponto de pedido");
  // Três tabelas vazias em sequência, e a explicação só no rodapé: o gestor
  // concluía "sem nexo" antes de chegar nela — e estava certo, porque nada no
  // topo dizia que o plano não tem base. O motivo agora vem primeiro.
  const semPlano = !plan.production.length && !plan.purchase.length;
  const aviso = document.getElementById("planning-gap");
  aviso.hidden = !semPlano || !plan.blockers.length;
  if (!aviso.hidden) {
    aviso.innerHTML = `<i data-lucide="unplug"></i><span>O plano está vazio, e não é falha de cálculo: <strong>${escapeHtml(plan.blockers[0].title)}</strong>. ${escapeHtml(plan.blockers[0].description)} O detalhe está em "O que o plano ainda não enxerga", no fim desta tela.</span>`;
  }
  setText("#pl-days", number.format(plan.open_days));
  setText("#pl-service", `${number.format(plan.service_level)}%`);
  setText("#pl-method", plan.method.summary);
  setText("#pl-nofit", plan.method.why_no_fit);
  setText("#pl-target", `${plan.target_weekday}, ${formatDate(plan.target_day)}`);
  setText("#pl-production-title", `Ficha de produção — ${plan.target_weekday}`);
  setText("#pl-purchase-total", pedir.length ? `${currency.format(plan.purchase_total)} a pedir` : "nada urgente");

  const badge = document.getElementById("nav-planning-count");
  badge.hidden = pedir.length === 0;
  badge.textContent = pedir.length;

  document.getElementById("pl-production-body").innerHTML = plan.production.length
    ? plan.production.map((row) => `<tr>
        <td><strong>${escapeHtml(row.name)}</strong><span class="row-note">validade de ${row.shelf_life_days} dia(s) · ${row.observations} observações</span></td>
        <td class="align-right"><strong>${number.format(row.prepare)} ${escapeHtml(row.unit)}</strong><span class="row-note">percentil ${number.format(row.critical_ratio)}</span></td>
        <td class="align-right muted-value">${number.format(row.median)} ${escapeHtml(row.unit)}</td>
        <td class="align-right muted-value">${number.format(row.in_stock)} ${escapeHtml(row.unit)}</td>
        <td class="align-right">${row.to_prepare > 0 ? `<strong>${number.format(row.to_prepare)} ${escapeHtml(row.unit)}</strong>` : '<span class="muted-value">já tem</span>'}</td>
        <td class="plan-why"><span>${escapeHtml(row.reading)}</span><small>${escapeHtml(row.ratio_basis)}</small></td>
      </tr>`).join("")
    : emptyRow(6, "Nenhum insumo perecível tem histórico de consumo suficiente. Cadastre a validade dos insumos e registre as vendas item a item.");

  document.getElementById("pl-purchase-body").innerHTML = plan.purchase.length
    ? plan.purchase.map((row) => {
        const situacao = row.reorder_now
          ? '<span class="status-pill pending">Pedir agora</span>'
          : `<span class="status-pill success">${row.days_left === null ? "sem consumo" : `${number.format(row.days_left)} dias`}</span>`;
        // O mínimo digitado no olho ganha origem: o histórico do próprio insumo
        // no prazo daquele fornecedor.
        const minimo = Math.abs(row.current_minimum - row.suggested_minimum) > 0.01
          ? `<span class="row-note">mínimo hoje ${number.format(row.current_minimum)} → sugerido ${number.format(row.suggested_minimum)}</span>`
          : "";
        return `<tr class="${row.reorder_now ? "" : "muted-row"}">
          <td><strong>${escapeHtml(row.name)}</strong><span class="row-note">prazo de ${row.lead_time_days} dia(s) · ${number.format(row.daily_average)} ${escapeHtml(row.unit)}/dia</span>${minimo}</td>
          <td class="align-right ${row.in_stock < 0 ? "negative" : "muted-value"}">${number.format(row.in_stock)} ${escapeHtml(row.unit)}</td>
          <td class="align-right"><strong>${number.format(row.reorder_point)} ${escapeHtml(row.unit)}</strong></td>
          <td class="align-right muted-value">${number.format(row.safety_stock)} ${escapeHtml(row.unit)}</td>
          <td class="align-right">${row.to_buy > 0 ? `<strong>${number.format(row.to_buy)} ${escapeHtml(row.unit)}</strong>` : '<span class="muted-value">—</span>'}</td>
          <td class="align-right">${row.cost > 0 ? currency.format(row.cost) : '<span class="muted-value">—</span>'}</td>
          <td>${situacao}</td>
        </tr>`;
      }).join("")
    : emptyRow(7, "Nenhum insumo tem histórico de consumo suficiente para calcular ponto de pedido.");

  document.getElementById("pl-abc-body").innerHTML = plan.abc.length
    ? plan.abc.map((row) => `<tr>
        <td><strong>${escapeHtml(row.name)}</strong></td>
        <td><span class="class-pill class-${escapeHtml(row.abc.toLowerCase())}">${escapeHtml(row.abc)}${escapeHtml(row.xyz)}</span></td>
        <td class="align-right">${currency.format(row.cost)}<span class="row-note">${number.format(row.share)}%</span></td>
        <td class="align-right muted-value">${number.format(row.cumulative)}%</td>
        <td class="align-right muted-value">${number.format(row.variation)}</td>
        <td class="muted-value">${escapeHtml(row.routine)}</td>
      </tr>`).join("")
    : emptyRow(6, "Sem consumo registrado no período.");

  document.getElementById("pl-blockers-panel").hidden = plan.blockers.length === 0;
  document.getElementById("pl-blockers").innerHTML = plan.blockers.map((row) => `
    <article class="insight attention"><span class="insight-icon"><i data-lucide="triangle-alert"></i></span><div><strong>${escapeHtml(row.title)}</strong><p>${escapeHtml(row.description)}</p></div></article>`).join("");
  refreshIcons();
}


// ---------------------------------------------------------------------------
// Entregas
// ---------------------------------------------------------------------------

async function loadDelivery() {
  try {
    const [deliveries, performance] = await Promise.all([
      api(`/api/deliveries?days=${state.deliveryPeriod}`),
      api(`/api/couriers/performance?days=${state.deliveryPeriod}`),
    ]);
    state.deliveries = deliveries;
    state.deliveryPerformance = performance;
    renderDelivery();
  } catch (error) {
    toast(error.message || "Não foi possível carregar as entregas.", "error");
  }
}

// Entregador ativo aparece na venda e no lançamento; inativo some da escolha
// mas continua no histórico.
function populateCourierSelects() {
  const ativos = state.couriers.filter((row) => row.active);
  const opcoes = ativos.map((row) => `<option value="${row.id}">${escapeHtml(row.name)}</option>`).join("");
  const venda = document.getElementById("sale-courier");
  if (venda) {
    const atual = venda.value;
    venda.innerHTML = `<option value="">Sem entregador definido</option>${opcoes}`;
    if (atual) venda.value = atual;
  }
  const turno = document.getElementById("delivery-courier");
  if (turno) {
    const atual = turno.value;
    turno.innerHTML = ativos.length ? opcoes : '<option value="">Cadastre um entregador primeiro</option>';
    if (atual) turno.value = atual;
  }
}

function renderDelivery() {
  const data = state.deliveryPerformance;
  if (!data) return;
  const t = data.totals;

  setText("#dl-count", number.format(t.deliveries));
  const turnos = data.couriers.reduce((soma, row) => soma + row.shifts, 0);
  setText("#dl-per-shift", turnos ? `${number.format(t.deliveries / turnos)} por turno, em média` : "nenhum turno lançado");
  setText("#dl-freight", currency.format(t.freight));
  setText("#dl-freight-each", t.freight_per_delivery !== null ? `${currency.format(t.freight_per_delivery)} por entrega` : "sem entregas no período");
  setText("#dl-payout", currency.format(t.payout));
  setText("#dl-cost-each", t.cost_per_delivery !== null ? `${currency.format(t.cost_per_delivery)} por entrega` : "sem entregas no período");

  const resultado = document.getElementById("dl-result");
  resultado.textContent = t.payout ? currency.format(t.result) : "—";
  resultado.className = t.result >= 0 ? "positive" : "negative";
  // Abaixo de 100%, a diferença sai da margem do produto. É a conta que decide
  // se o delivery vale a pena como está.
  setText("#dl-coverage", t.coverage !== null
    ? `o frete cobre ${number.format(t.coverage)}% do custo`
    : "lance um turno para a conta aparecer");

  const badge = document.getElementById("nav-delivery-count");
  const pendentes = data.reconciliation.orders_without_courier;
  badge.hidden = pendentes === 0;
  badge.textContent = pendentes;

  document.getElementById("dl-readings").innerHTML = data.readings.map((row) => `
    <article class="insight ${row.type}"><span class="insight-icon"><i data-lucide="${row.type === "attention" ? "triangle-alert" : row.type === "opportunity" ? "badge-dollar-sign" : "lightbulb"}"></i></span><div><strong>${escapeHtml(row.title)}</strong><p>${escapeHtml(row.description)}</p></div></article>`).join("");

  // Quem manda na lista é o cadastro; a medição preenche as colunas. Antes ela
  // vinha só do desempenho do período, e por isso o entregador recém-cadastrado
  // não aparecia em lugar nenhum — justamente quem mais precisa ser aberto, para
  // ligar a conta do aplicativo de rua ao cadastro dele.
  const medido = new Map(data.couriers.map((row) => [row.courier_id, row]));
  const zerado = {
    deliveries: 0, shifts: 0, deliveries_per_shift: 0, freight: 0, payout: 0,
    salary: 0, cost_per_delivery: null, result: 0, overrides: 0,
  };
  const linhasCourier = state.couriers.map((courier) => ({
    ...zerado,
    ...(medido.get(courier.id) || {}),
    courier_id: courier.id,
    name: courier.name,
    vehicle: courier.vehicle,
    active: courier.active !== false,
    moved: medido.has(courier.id),
  }));
  // Entregador que saiu do cadastro mas tem turno no período continua aqui: o
  // custo dele está no total, e sumir a linha faria a coluna não fechar.
  data.couriers
    .filter((row) => !state.couriers.some((courier) => courier.id === row.courier_id))
    .forEach((row) => linhasCourier.push({ ...row, moved: true }));

  document.getElementById("dl-courier-body").innerHTML = linhasCourier.length
    ? linhasCourier.map((row) => `<tr class="${row.active ? "" : "cancelled-row"}">
        <td><strong>${escapeHtml(row.name)}</strong>${row.vehicle ? `<span class="row-note">${escapeHtml(row.vehicle)}</span>` : ""}${row.moved ? "" : '<span class="row-note">sem movimento no período</span>'}${row.overrides ? `<span class="row-note">${row.overrides} turno(s) com valor à mão</span>` : ""}</td>
        <td class="align-right"><strong>${number.format(row.deliveries)}</strong></td>
        <td class="align-right muted-value">${number.format(row.shifts)}</td>
        <td class="align-right muted-value">${number.format(row.deliveries_per_shift)}</td>
        <td class="align-right">${currency.format(row.freight)}</td>
        <td class="align-right"><strong>${currency.format(row.payout)}</strong>${row.salary ? `<span class="row-note">inclui ${currency.format(row.salary)} de salário</span>` : ""}</td>
        <td class="align-right">${row.cost_per_delivery !== null ? currency.format(row.cost_per_delivery) : '<span class="muted-value">—</span>'}</td>
        <td class="align-right ${row.result >= 0 ? "positive" : "negative"}"><strong>${currency.format(row.result)}</strong></td>
        <td class="align-right"><div class="row-actions"><button class="row-action" type="button" data-edit-courier="${row.courier_id}" title="Editar entregador"><i data-lucide="pencil"></i></button></div></td>
      </tr>`).join("")
    : emptyRow(9, "Nenhum entregador cadastrado. Cadastre um para lançar turno e para liberar o aplicativo de rua.");

  setText("#dl-entry-count", `${state.deliveries.length} turno(s)`);
  document.getElementById("dl-entry-body").innerHTML = state.deliveries.length
    ? state.deliveries.map((row) => {
        const conta = row.breakdown.length
          ? row.breakdown.map((parte) => `${escapeHtml(parte.label)} = ${currency.format(parte.value)}`).join(" · ")
          : "sem remuneração definida para este entregador";
        const marca = row.overridden
          ? `<span class="row-note">valor à mão; calculado seria ${currency.format(row.calculated_payout)}</span>`
          : "";
        return `<tr>
          <td class="muted-value">${escapeHtml(formatDate(row.date))}${row.worked ? "" : '<span class="row-note">sem diária</span>'}</td>
          <td><strong>${escapeHtml(row.courier_name)}</strong>${row.note ? `<span class="row-note">${escapeHtml(row.note)}</span>` : ""}</td>
          <td class="align-right">${number.format(row.deliveries)}</td>
          <td class="align-right muted-value">${currency.format(row.freight_total)}</td>
          <td class="plan-why"><span>${escapeHtml(conta)}</span>${marca}</td>
          <td class="align-right"><strong>${currency.format(row.payout)}</strong></td>
          <td class="align-right"><div class="row-actions">
            <button class="row-action" type="button" data-edit-delivery="${row.id}" title="Corrigir turno"><i data-lucide="pencil"></i></button>
            <button class="row-action danger" type="button" data-delete-delivery="${row.id}" title="Excluir turno"><i data-lucide="trash-2"></i></button>
          </div></td>
        </tr>`;
      }).join("")
    : emptyRow(7, "Nenhum turno lançado no período.");

  const r = data.reconciliation;
  document.getElementById("dl-reconcile").innerHTML = `
    <article><p>Pedidos de entrega no ERP</p><strong>${number.format(r.delivery_orders)}</strong><small>${currency.format(r.order_freight)} de frete</small></article>
    <article><p>Entregas lançadas nos turnos</p><strong>${number.format(r.entries_deliveries)}</strong><small>somando os fechamentos</small></article>
    <article class="${r.difference === 0 ? "" : "attention"}"><p>Diferença</p><strong>${r.difference > 0 ? "+" : ""}${number.format(r.difference)}</strong><small>${r.difference === 0 ? "os dois lados batem" : "confira antes do acerto"}</small></article>
    <article class="${r.orders_without_courier ? "attention" : ""}"><p>Sem entregador marcado</p><strong>${number.format(r.orders_without_courier)}</strong><small>pedidos de entrega</small></article>`;
  refreshIcons();
}


// ---------------------------------------------------------------------------
// Listas editáveis
// ---------------------------------------------------------------------------

function catalogItems(catalogId) {
  const lista = state.catalogs.find((row) => row.id === catalogId);
  return lista ? lista.items.map((item) => item.value) : [];
}

/* Todo campo de escolha passa a vir do cadastro, não de uma lista fixa no
 * código. Valor antigo que saiu da lista continua sendo injetado pelo
 * setSelectValue, para o registro não perder o que já tinha. */
function populateCatalogSelects() {
  if (!state.catalogs.length) return;
  const alvos = [
    ["payment_methods", '#sale-form [name="payment_method"]'],
    ["payment_methods", "#import-payment"],
    ["expense_categories", '#expense-form [name="category"]'],
    ["product_lines", '#product-form [name="line"]'],
    ["media_categories", '#media-form [name="category"]'],
  ];
  alvos.forEach(([catalogId, seletor]) => {
    const select = document.querySelector(seletor);
    if (!select) return;
    const atual = select.value;
    const opcoes = catalogItems(catalogId);
    if (!opcoes.length) return;
    select.innerHTML = opcoes.map((valor) => `<option value="${escapeHtml(valor)}">${escapeHtml(valor)}</option>`).join("");
    if (atual && opcoes.includes(atual)) select.value = atual;
  });
  // Campo livre: a lista sugere, mas não impede um valor novo.
  [["inventory_categories", "inventory-categories"], ["courier_vehicles", "courier-vehicles"]].forEach(([catalogId, elementId]) => {
    const datalist = document.getElementById(elementId);
    if (datalist) {
      datalist.innerHTML = catalogItems(catalogId).map((valor) => `<option value="${escapeHtml(valor)}"></option>`).join("");
    }
  });
}

function renderCatalogs() {
  const grid = document.getElementById("catalog-grid");
  if (!grid) return;
  grid.innerHTML = state.catalogs.map((lista) => {
    const itens = lista.items.map((item, indice) => `
      <li class="catalog-row">
        <button type="button" class="catalog-item" data-edit-catalog="${escapeHtml(lista.id)}" data-catalog-value="${escapeHtml(item.value)}">
          <span>${escapeHtml(item.value)}</span>
          <b class="${item.usage ? "" : "unused"}">${item.usage ? `${number.format(item.usage)} em uso` : "sem uso"}</b>
          <i data-lucide="pencil"></i>
        </button>
        <span class="row-actions catalog-move">
          <button class="row-action" type="button" data-move-catalog="${escapeHtml(lista.id)}" data-move-index="${indice}" data-move-step="-1" title="Subir" ${indice === 0 ? "disabled" : ""}><i data-lucide="chevron-up"></i></button>
          <button class="row-action" type="button" data-move-catalog="${escapeHtml(lista.id)}" data-move-index="${indice}" data-move-step="1" title="Descer" ${indice === lista.items.length - 1 ? "disabled" : ""}><i data-lucide="chevron-down"></i></button>
        </span>
      </li>`).join("");
    // Valor que ficou nos registros mas saiu da lista: some da escolha, não da
    // tela — é ele que explica um relatório antigo com categoria inexistente.
    const orfaos = lista.orphans.length
      ? `<p class="catalog-orphans"><i data-lucide="ghost"></i>Fora da lista, ainda em uso: ${lista.orphans.map((row) => `${escapeHtml(row.value)} (${row.usage})`).join(", ")}</p>`
      : "";
    return `<article class="panel catalog-card">
      <div class="panel-header"><div><p class="eyebrow">${escapeHtml(lista.feeds)}</p><h2>${escapeHtml(lista.name)}</h2></div>
      <button class="icon-button" type="button" data-add-catalog="${escapeHtml(lista.id)}" title="Adicionar item"><i data-lucide="plus"></i></button></div>
      <p class="panel-note">${escapeHtml(lista.description)}</p>
      <ul class="catalog-list">${itens}</ul>
      ${orfaos}
    </article>`;
  }).join("");
  refreshIcons();
}


// ---------------------------------------------------------------------------
// Histórico: a trilha que sustenta a edição
// ---------------------------------------------------------------------------

// Nomes internos das entidades traduzidos para a linguagem da loja. Não é
// catálogo editável: são identificadores de código, não dado de negócio.
const HISTORY_ENTITIES = {
  order: { label: "Pedido", icon: "receipt-text" },
  product: { label: "Produto", icon: "cup-soda" },
  inventory: { label: "Insumo", icon: "package-search" },
  purchase: { label: "Compra", icon: "shopping-cart" },
  movement: { label: "Movimentação", icon: "clipboard-list" },
  expense: { label: "Despesa", icon: "wallet-cards" },
  channel: { label: "Canal", icon: "route" },
  courier: { label: "Entregador", icon: "bike" },
  delivery: { label: "Turno de entrega", icon: "package-check" },
  catalog: { label: "Lista", icon: "sliders-horizontal" },
};

const HISTORY_ACTIONS = {
  criado: "success", editado: "neutral", excluido: "danger",
  cancelado: "danger", inativado: "pending", contagem: "neutral",
};

async function loadHistory() {
  try {
    state.history = await api(`/api/audit?limit=${state.historyLimit}`);
    renderHistory();
  } catch (error) {
    toast(error.message || "Não foi possível carregar o histórico.", "error");
  }
}

function renderHistory() {
  const container = document.getElementById("history-body");
  if (!container) return;

  const tipos = [...new Set(state.history.map((row) => row.entity))];
  document.getElementById("history-filters").innerHTML = ["all", ...tipos].map((tipo) => {
    const total = tipo === "all" ? state.history.length : state.history.filter((row) => row.entity === tipo).length;
    const rotulo = tipo === "all" ? "Tudo" : HISTORY_ENTITIES[tipo]?.label || tipo;
    return `<button type="button" class="filter-chip ${state.historyFilter === tipo ? "active" : ""}" data-history-filter="${escapeHtml(tipo)}">${escapeHtml(rotulo)} <b>${total}</b></button>`;
  }).join("");

  const busca = document.getElementById("history-search")?.value.trim().toLocaleLowerCase("pt-BR") || "";
  const linhas = state.history.filter((row) => {
    const tipoOk = state.historyFilter === "all" || row.entity === state.historyFilter;
    const texto = `${row.summary} ${HISTORY_ENTITIES[row.entity]?.label || row.entity} ${row.action}`.toLocaleLowerCase("pt-BR");
    return tipoOk && texto.includes(busca);
  });

  container.innerHTML = linhas.length ? linhas.map((row) => {
    const entidade = HISTORY_ENTITIES[row.entity] || { label: row.entity, icon: "file" };
    const css = HISTORY_ACTIONS[row.action] || "neutral";
    return `<tr>
      <td class="muted-value">${escapeHtml(formatDateTime(row.at))}</td>
      <td><span class="history-entity"><i data-lucide="${entidade.icon}"></i>${escapeHtml(entidade.label)}</span></td>
      <td><span class="status-pill ${css}">${escapeHtml(row.action)}</span></td>
      <td>${escapeHtml(row.summary)}</td>
      <td class="history-changes">${renderChanges(row.changes)}</td>
    </tr>`;
  }).join("") : emptyRow(5, busca || state.historyFilter !== "all"
    ? "Nenhuma alteração corresponde ao filtro."
    : "Nenhuma alteração registrada ainda.");
  refreshIcons();
}

// O diff só guarda o que mudou de fato, então cada linha aqui é uma diferença
// real — não um despejo do registro inteiro.
function renderChanges(changes) {
  const campos = Object.entries(changes || {});
  if (!campos.length) return '<span class="muted-value">—</span>';
  return campos.map(([campo, valor]) => {
    if (valor === null || typeof valor !== "object" || !("de" in valor)) {
      return `<span class="change-line"><b>${escapeHtml(fieldLabel(campo))}</b> ${escapeHtml(String(valor))}</span>`;
    }
    return `<span class="change-line"><b>${escapeHtml(fieldLabel(campo))}</b><em>${escapeHtml(changeValue(valor.de))}</em><i data-lucide="move-right"></i><strong>${escapeHtml(changeValue(valor.para))}</strong></span>`;
  }).join("");
}

function changeValue(value) {
  if (value === null || value === undefined || value === "") return "vazio";
  if (typeof value === "boolean") return value ? "sim" : "não";
  if (typeof value === "number") return number.format(value);
  return String(value);
}

function fieldLabel(field) {
  return {
    total: "total", discount: "desconto", channel: "canal", payment_method: "pagamento",
    created_at: "data", itens: "itens", amount: "valor", status: "situação",
    due_date: "vencimento", description: "descrição", category: "categoria",
    unit_cost: "custo", minimum_quantity: "mínimo", quantity_delta: "quantidade",
    reason: "motivo", price: "preço", line: "linha", name: "nome", active: "ativo",
    "quantidade comprada": "quantidade comprada", "conteúdo por unidade": "conteúdo por unidade", "custo da compra": "custo da compra", "custo da unidade comprada": "custo da unidade comprada", "custo médio": "custo médio",
    fornecedor: "fornecedor", documento: "documento",
    fee_percent: "taxa %", fee_fixed: "taxa fixa", fee_per_delivery: "por entrega",
    commission_percent: "comissão", daily_rate: "diária", monthly_salary: "salário",
    deliveries: "entregas", freight_total: "frete", worked: "diária contada",
    bonus: "bônus", deduction: "desconto", payout_override: "valor à mão",
    delivery_fee: "frete", is_delivery: "é entrega", valor: "valor",
    registros_migrados: "registros migrados", recurring: "fixa",
  }[field] || field;
}

// ------------------------------------------------------- operação de rua
//
// Zonas, quem está na rua agora e o que o território ensina. Carregado ao
// abrir a tela de Entregas, e não no `loadAll`: aquele já espera trinta e
// quatro consultas antes de mostrar qualquer coisa, e quatro a mais fariam o
// painel inteiro demorar por causa de uma tela que nem sempre é aberta. O
// precedente é o do iFood, que já carrega assim.

async function loadDispatch() {
  if (loadDispatch.pending) return;
  loadDispatch.pending = true;
  try {
    const [board, territory, zones, runs, pending, shifts] = await Promise.all([
      api(`/api/delivery/board?days=${state.deliveryPeriod}`),
      api(`/api/delivery/territory?days=90&horizon=7`),
      api("/api/delivery/zones"),
      api(`/api/delivery/runs?days=${state.deliveryPeriod}`),
      api(`/api/delivery/pending?days=${state.deliveryPeriod}`),
      api(`/api/delivery/shifts?days=${state.deliveryPeriod}`),
    ]);
    state.dispatch = { board, territory, zones, runs, pending, shifts };
    renderDispatch();
  } catch (error) {
    toast(error.message || "Não foi possível carregar a operação de rua.", "error");
  } finally {
    loadDispatch.pending = false;
  }
}

function renderDispatch() {
  const dados = state.dispatch;
  if (!dados) return;
  renderOnRoad(dados.board.now);
  renderZones(dados.board.zones, dados.zones);
  renderTerritory(dados.territory);
  renderMenuByZone(dados.territory.menu || []);
  renderRuns(dados.runs);
  renderPendingSales(dados.pending);
  renderShiftLog(dados.shifts);
  // As leituras do painel de rua entram junto das do acerto: as duas
  // respondem a mesma pergunta — o que precisa de decisão hoje.
  const leituras = document.getElementById("dl-readings");
  if (leituras && dados.board.readings?.length) {
    leituras.insertAdjacentHTML("beforeend", dados.board.readings.map(insightCard).join(""));
  }
  refreshIcons();
}

function renderOnRoad(agora) {
  setText("#dl-now-chip", agora.checked_in
    ? `${agora.checked_in} em turno · ${agora.on_route} a caminho`
    : "ninguém em turno");
  document.getElementById("dl-now").innerHTML = agora.on_shift.length
    ? agora.on_shift.map((pessoa) => `
      <article class="now-card">
        <div class="now-head">
          <div><strong>${escapeHtml(pessoa.name)}</strong><small class="muted-value">${escapeHtml(pessoa.vehicle || "")}</small></div>
          <span class="soft-chip">${duracaoCurta(pessoa.minutes_on_shift)} em turno</span>
        </div>
        <p class="now-line">${pessoa.deliveries} entrega(s) · ${currency.format(pessoa.freight)} de frete</p>
        ${pessoa.on_route.length
          ? pessoa.on_route.map((linha) => `
            <div class="now-route ${linha.late ? "late" : ""}">
              <span><strong>${escapeHtml(linha.zone_name || "Sem zona")}</strong>${linha.customer_name ? ` · ${escapeHtml(linha.customer_name)}` : ""}</span>
              <b>${linha.minutes} min</b>
            </div>`).join("")
          : `<p class="now-line muted-value">Na loja, sem entrega a caminho.</p>`}
      </article>`).join("")
    : `<p class="empty-note">Ninguém fez check-in. O entregador abre <code>/entregador</code> no celular dele e toca em “Fazer check-in”.</p>`;
}

function renderZones(medidas, cadastro) {
  const porZona = new Map(medidas.map((linha) => [linha.zone_id, linha]));
  const linhas = cadastro.map((zona) => {
    const medida = porZona.get(zona.id) || { deliveries: 0, share: 0, per_day: 0, customers: 0, ticket: null, freight: 0, minutes_median: null, thin: true, over_target: false };
    const tempo = medida.minutes_median === null
      ? '<span class="muted-value">—</span>'
      : `<strong class="${medida.over_target ? "negative" : ""}">${medida.minutes_median} min</strong>${zona.target_minutes ? `<small class="muted-value">/${zona.target_minutes}</small>` : ""}`;
    // Zona criada na rua chega com frete zero e sem tempo alvo: ela vale, mas
    // precisa de decisão da loja antes de virar base de acerto. A marca é o
    // que impede que ela fique para sempre valendo frete nenhum.
    const marca = !zona.active
      ? '<span class="soft-chip">inativa</span>'
      : zona.proposed ? '<span class="soft-chip attention">criada na rua · confirme o frete</span>'
      : medida.thin && medida.deliveries ? '<span class="soft-chip">amostra fraca</span>' : "";
    return `<tr><td><strong>${escapeHtml(zona.name)}</strong> ${marca}${zona.note ? `<small class="muted-value"> ${escapeHtml(zona.note)}</small>` : ""}</td><td class="align-right">${number.format(medida.deliveries)}</td><td class="align-right">${medida.deliveries ? `${number.format(medida.share)}%` : '<span class="muted-value">—</span>'}</td><td class="align-right muted-value">${number.format(medida.per_day)}</td><td class="align-right">${number.format(medida.customers)}</td><td class="align-right">${medida.ticket === null ? '<span class="muted-value">—</span>' : currency.format(medida.ticket)}</td><td class="align-right">${currency.format(medida.freight)}</td><td class="align-right">${tempo}</td><td class="align-right"><div class="row-actions"><button class="row-action" type="button" data-edit-zone="${zona.id}" title="Editar zona"><i data-lucide="pencil"></i></button></div></td></tr>`;
  });
  // Entrega sem zona é dado que não entra em leitura nenhuma; ela precisa
  // aparecer aqui para alguém corrigir, e não sumir por não ter cadastro.
  const orfas = porZona.get("sem-zona");
  if (orfas) {
    linhas.push(`<tr><td><strong class="negative">Sem zona</strong><small class="muted-value"> corrija cada entrega na lista abaixo</small></td><td class="align-right">${number.format(orfas.deliveries)}</td><td class="align-right">${number.format(orfas.share)}%</td><td class="align-right muted-value">${number.format(orfas.per_day)}</td><td class="align-right">${number.format(orfas.customers)}</td><td class="align-right muted-value">—</td><td class="align-right">${currency.format(orfas.freight)}</td><td class="align-right muted-value">—</td><td></td></tr>`);
  }
  document.getElementById("dl-zone-body").innerHTML = linhas.length
    ? linhas.join("")
    : emptyRow(9, "Nenhuma zona cadastrada. Sem ela o entregador não tem o que escolher, e a pergunta “onde mais pedem” continua sem resposta.");
}

function renderTerritory(territorio) {
  const base = territorio.basis;
  setText("#dl-territory-basis", territorio.status === "sem-dados"
    ? "sem entregas"
    : `${number.format(base.deliveries)} entregas · ${number.format(base.days_with_delivery)} dia(s) com movimento`);
  const nota = document.getElementById("dl-territory-note");
  if (territorio.note) nota.textContent = territorio.note;

  document.getElementById("dl-territory").innerHTML = territorio.zones.length
    ? territorio.zones.map((zona) => {
      const dias = zona.forecast.slice(0, 7).map((dia) => `<div class="zone-day"><small>${escapeHtml(dia.weekday.slice(0, 3))}</small><b>${number.format(dia.deliveries)}</b></div>`).join("");
      return `
        <article class="zone-card ${zona.thin ? "thin" : ""}">
          <div class="zone-head"><strong>${escapeHtml(zona.name)}</strong>${zona.thin ? '<span class="soft-chip">amostra fraca</span>' : ""}</div>
          <p class="zone-line">${number.format(zona.daily_average)} entrega(s) por dia com movimento · tendência ${number.format(zona.trend)}× · ${zona.active_days} dia(s) medido(s)</p>
          ${zona.best_weekday ? `<p class="zone-line muted-value">Dia mais forte: ${escapeHtml(zona.best_weekday.name)}, com ${zona.best_weekday.deliveries} entrega(s).</p>` : ""}
          <div class="zone-days">${dias}</div>
          <p class="zone-line"><strong>${number.format(zona.forecast_total)}</strong> entregas previstas nos próximos 7 dias.</p>
          ${zona.top_products.length ? `<p class="zone-line muted-value">Mais pedido: ${zona.top_products.map((linha) => `${escapeHtml(linha.name)} (${linha.units})`).join(" · ")}</p>` : ""}
        </article>`;
    }).join("")
    : `<p class="empty-note">${escapeHtml(territorio.note || "Sem entregas registradas no período.")}</p>`;
}

function renderMenuByZone(sugestoes) {
  document.getElementById("dl-menu").innerHTML = sugestoes.length
    ? sugestoes.map((linha) => `
      <article class="insight opportunity"><span class="insight-icon"><i data-lucide="map-pinned"></i></span><div><strong>${escapeHtml(linha.title)}</strong><p>${escapeHtml(linha.description)}</p><p class="muted-value">${escapeHtml(linha.action)}</p></div></article>`).join("")
    : `<p class="empty-note">Ainda não há entregas suficientes para separar preferência de bairro de coincidência. A tela se preenche sozinha conforme os check-outs acontecem.</p>`;
}

function renderRuns(entregas) {
  setText("#dl-runs-count", `${number.format(entregas.length)} no período`);
  document.getElementById("dl-runs-body").innerHTML = entregas.length
    ? entregas.map((linha) => {
      const tempo = linha.open
        ? `<strong class="negative">em rota · ${linha.minutes} min</strong>`
        : linha.failed ? '<span class="negative">não entregue</span>' : `${linha.minutes} min`;
      const itens = (linha.items || []).map((item) => `${item.quantity}× ${item.name}`).join(", ");
      return `<tr><td>${formatDateTime(linha.departed_at)}</td><td>${escapeHtml(courierName(linha.courier_id))}</td><td><strong>${escapeHtml(linha.zone_name || "Sem zona")}</strong>${linha.reference ? `<small class="muted-value"> ${escapeHtml(linha.reference)}</small>` : ""}</td><td class="muted-value">${escapeHtml(itens || `${linha.units} item(ns)`)}</td><td>${escapeHtml(linha.customer_name || "—")}</td><td class="align-right">${currency.format(linha.freight)}</td><td class="align-right">${tempo}</td><td class="align-right"><div class="row-actions"><button class="row-action" type="button" data-edit-run="${linha.id}" title="Corrigir entrega"><i data-lucide="pencil"></i></button><button class="row-action danger" type="button" data-delete-run="${linha.id}" title="Excluir entrega"><i data-lucide="trash-2"></i></button></div></td></tr>`;
    }).join("")
    : emptyRow(8, "Nenhuma entrega registrada. Elas aparecem aqui conforme os entregadores registram no aplicativo de rua.");
}

// A venda que veio da rua e ainda não é faturamento. O painel só existe quando
// há algo a conferir: um quadro vazio todo dia ensina a ignorar o quadro.
function renderPendingSales(pendentes) {
  const painel = document.getElementById("dl-pending-panel");
  painel.hidden = !pendentes.length;
  setText("#dl-pending-chip", `${pendentes.length} aguardando`);
  document.getElementById("dl-pending-body").innerHTML = pendentes.map((linha) => {
    const itens = (linha.items || []).map((item) => `${item.quantity}× ${item.name}`).join(", ");
    const recebeu = linha.declared_total === null || linha.declared_total === undefined
      ? '<span class="muted-value">não informado</span>'
      : `<strong>${currency.format(linha.declared_total)}</strong>`;
    // Divergência entre o que a rua recebeu e o que o cardápio calcularia é a
    // razão de a conferência existir; marcá-la evita ter de conferir de cabeça.
    const difere = linha.declared_total !== null && linha.declared_total !== undefined
      && Math.abs(linha.declared_total - linha.suggested_total) >= 0.01;
    return `<tr>
      <td class="muted-value">${escapeHtml(formatDateTime(linha.occurred_at))}</td>
      <td>${escapeHtml(linha.courier_name)}</td>
      <td>${escapeHtml(linha.zone_name || "—")}</td>
      <td class="muted-value">${escapeHtml(itens || `${linha.units} item(ns)`)}</td>
      <td>${escapeHtml(linha.customer_name || "—")}</td>
      <td class="align-right">${recebeu}</td>
      <td class="align-right ${difere ? "negative" : "muted-value"}">${currency.format(linha.suggested_total)}</td>
      <td class="muted-value">${escapeHtml(linha.payment_method || "—")}</td>
      <td class="align-right"><div class="row-actions"><button class="row-action" type="button" data-check-pending="${linha.id}" title="Conferir e lançar"><i data-lucide="check"></i></button></div></td>
    </tr>`;
  }).join("");
}

function renderShiftLog(turnos) {
  setText("#dl-shifts-chip", `${turnos.length} turno(s)`);
  document.getElementById("dl-shifts-body").innerHTML = turnos.length
    ? turnos.map((linha) => `<tr>
        <td class="muted-value">${escapeHtml(formatDate(linha.date))}</td>
        <td><strong>${escapeHtml(linha.courier_name)}</strong></td>
        <td>${escapeHtml(hourOf(linha.checkin_at))}</td>
        <td>${linha.open ? '<strong class="positive">em turno</strong>' : escapeHtml(hourOf(linha.checkout_at))}</td>
        <td class="align-right ${linha.auto_closed ? "muted-value" : ""}">${duracaoCurta(linha.minutes)}</td>
        <td class="align-right">${number.format(linha.deliveries)}</td>
        <td class="align-right">${currency.format(linha.freight)}</td>
        <td>${linha.auto_closed ? `<span class="row-note">fechado pelo sistema: ${escapeHtml(linha.auto_closed)}${linha.left_on_route ? ` · ${linha.left_on_route} ficou(ram) em rota` : ""}</span>` : ""}</td>
      </tr>`).join("")
    : emptyRow(8, "Nenhum check-in no período.");
}

function hourOf(value) {
  return value ? new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "--:--";
}

function courierName(courierId) {
  return (state.couriers.find((linha) => linha.id === courierId) || {}).name || "Entregador removido";
}

function duracaoCurta(minutos) {
  return minutos < 60 ? `${minutos} min` : `${Math.floor(minutos / 60)}h${String(minutos % 60).padStart(2, "0")}`;
}
