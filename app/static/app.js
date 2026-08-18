const state = {
  dashboard: null,
  products: [],
  inventory: [],
  orders: [],
  expenses: [],
  media: [],
  mediaSummary: null,
  consumption: null,
  forecast: null,
  mediaFilter: "all",
  selectedMediaId: null,
  mediaObjectUrl: null,
  cart: new Map(),
  period: 7,
};

const currency = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const number = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 });
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
  const requestedView = window.location.hash.slice(1);
  if (document.getElementById(requestedView)) activateView(requestedView, false);
  document.getElementById("refresh-data").addEventListener("click", loadAll);
  document.getElementById("period-select").addEventListener("change", async (event) => {
    state.period = Number(event.target.value);
    await loadDashboard();
  });
  document.getElementById("order-search").addEventListener("input", renderOrders);
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

async function loadAll() {
  setRefreshing(true);
  try {
    const [config, dashboard, consumption, forecast, products, inventory, orders, expenses, mediaSummary, media] = await Promise.all([
      api("/api/config"),
      api(`/api/dashboard?days=${state.period}`),
      api(`/api/inventory/consumption?days=${state.period}`),
      api("/api/forecast?horizon=7"),
      api("/api/products"),
      api("/api/inventory"),
      api("/api/orders"),
      api("/api/expenses"),
      api("/api/media/summary"),
      api("/api/media"),
    ]);
    state.dashboard = dashboard;
    state.consumption = consumption;
    state.forecast = forecast;
    state.products = products;
    state.inventory = inventory;
    state.orders = orders;
    state.expenses = expenses;
    state.mediaSummary = mediaSummary;
    state.media = media;
    document.getElementById("storage-mode").textContent = config.storage === "firebase" ? "Firebase conectado" : "Modo demonstrativo";
    renderAll();
  } catch (error) {
    toast(error.message || "Não foi possível carregar os dados.", "error");
  } finally {
    setRefreshing(false);
  }
}

async function loadDashboard() {
  try {
    const [dashboard, consumption] = await Promise.all([
      api(`/api/dashboard?days=${state.period}`),
      api(`/api/inventory/consumption?days=${state.period}`),
    ]);
    state.dashboard = dashboard;
    state.consumption = consumption;
    renderDashboard();
    renderConsumption();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderAll() {
  renderDashboard();
  renderOrders();
  renderInventory();
  renderConsumption();
  renderForecast();
  renderFinance();
  renderProducts();
  renderMedia();
  populateSelects();
  refreshIcons();
}

function renderDashboard() {
  const dashboard = state.dashboard;
  if (!dashboard) return;
  const { metrics } = dashboard;
  setText('[data-metric="revenue"]', currency.format(metrics.revenue));
  setText('[data-metric="orders"]', number.format(metrics.orders));
  setText('[data-metric="ticket"]', currency.format(metrics.average_ticket));
  setText('[data-metric="margin"]', `${number.format(metrics.gross_margin)}%`);
  // Pedido importado nao traz custo. Sem esse aviso, a margem parece valer para
  // o periodo inteiro quando na verdade cobre so parte do faturamento.
  const cobertura = metrics.margin_coverage ?? 100;
  setText('[data-metric="gross-profit"]', cobertura >= 99.5
    ? `${currency.format(metrics.gross_profit)} de lucro bruto`
    : `sobre ${number.format(cobertura)}% do faturamento — ${currency.format(metrics.revenue_without_cost)} sem custo lançado`);
  const change = document.querySelector('[data-metric="revenue-change"]');
  const signal = metrics.revenue_change >= 0 ? "+" : "";
  change.textContent = `${signal}${number.format(metrics.revenue_change)}% vs. período anterior`;
  change.className = metrics.revenue_change >= 0 ? "positive" : "negative";
  setText("#chart-total", currency.format(metrics.revenue));
  renderChart(dashboard.sales_series);
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

function renderInsights(insights) {
  const icons = { attention: "triangle-alert", opportunity: "badge-dollar-sign", info: "lightbulb" };
  document.getElementById("insight-list").innerHTML = insights.map((item) => `
    <article class="insight ${item.type}"><span class="insight-icon"><i data-lucide="${icons[item.type] || "lightbulb"}"></i></span><div><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.description)}</p></div></article>
  `).join("");
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
  document.getElementById("orders-body").innerHTML = orders.length ? orders.map(order => `
    <tr><td><strong>#${order.number}</strong></td><td>${escapeHtml(formatDateTime(order.created_at))}</td><td><span class="channel-pill">${escapeHtml(order.channel)}</span></td><td>${escapeHtml(itemSummary(order))}</td><td>${escapeHtml(paymentLabel(order.payment_method))}</td><td class="align-right"><strong>${currency.format(order.total)}</strong></td></tr>
  `).join("") : emptyRow(6, "Nenhum pedido encontrado.");
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
    return `<tr><td><strong>${escapeHtml(item.name)}</strong></td><td>${escapeHtml(item.category)}</td><td><span class="stock-level"><i class="stock-dot ${dot}"></i><span class="stock-amount">${number.format(item.quantity)} ${escapeHtml(item.unit)}</span></span></td><td class="muted-value">${minimum}</td><td>${currency.format(item.unit_cost)}</td><td><span class="status-pill ${status.css}">${status.label}</span></td></tr>`;
  }).join("") : emptyRow(6, "Nenhum insumo encontrado.");
  refreshIcons();
}

function renderConsumption() {
  const data = state.consumption;
  if (!data) return;
  const { totals } = data;
  setText("#consumption-total", currency.format(totals.total_cost));
  setText("#consumption-daily", `${currency.format(totals.daily_cost)} por dia, em média`);
  setText("#consumption-sale", currency.format(totals.sale_cost));
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
    return `<tr><td><strong>${escapeHtml(item.name)}</strong></td><td class="align-right">${number.format(item.sold)} ${escapeHtml(item.unit)}</td><td class="align-right">${lost}</td><td class="align-right">${currency.format(item.cost)}</td><td class="align-right muted-value">${number.format(item.quantity)} ${escapeHtml(item.unit)}</td><td>${duration}</td></tr>`;
  }).join("") : emptyRow(6, "Nenhuma saída de insumo no período.");

  document.getElementById("movement-body").innerHTML = data.history.length ? data.history.map((row) => {
    const labels = { venda: "Venda", perda: "Baixa", entrada: "Entrada" };
    const css = { venda: "success", perda: "pending", entrada: "neutral" };
    const signal = row.quantity_delta > 0 ? "+" : "";
    return `<tr><td class="muted-value">${escapeHtml(formatDateTime(row.created_at))}</td><td><strong>${escapeHtml(row.name)}</strong></td><td><span class="status-pill ${css[row.kind]}">${labels[row.kind]}</span></td><td>${escapeHtml(row.reason)}</td><td class="align-right"><strong>${signal}${number.format(row.quantity_delta)} ${escapeHtml(row.unit)}</strong></td></tr>`;
  }).join("") : emptyRow(5, "Nenhuma movimentação registrada.");
  refreshIcons();
}

function renderConsumptionChart(series) {
  const container = document.getElementById("consumption-chart");
  if (!container) return;
  const peak = Math.max(...series.map((row) => row.sale_cost + row.loss_cost), 0);
  const top = niceCeiling(peak);
  const axis = document.getElementById("consumption-y-axis");
  if (axis) axis.innerHTML = [top, top / 2, 0].map((value) => `<span>${escapeHtml(currency.format(value))}</span>`).join("");
  container.style.setProperty("--columns", series.length);
  container.innerHTML = series.map((row) => {
    const total = row.sale_cost + row.loss_cost;
    const height = (value) => (value > 0 ? Math.max(3, Math.round((value / top) * 150)) : 0);
    const title = `${formatDate(row.date)}: ${currency.format(row.sale_cost)} em venda, ${currency.format(row.loss_cost)} em perda`;
    const stack = total > 0
      ? `<span class="bar loss" style="height:${height(row.loss_cost)}px"></span><span class="bar sale" style="height:${height(row.sale_cost)}px"></span>`
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
  const metrics = state.dashboard?.metrics;
  if (!metrics) return;
  const paid = state.expenses.filter(expense => expense.status === "Paga").reduce((sum, expense) => sum + expense.amount, 0);
  const result = metrics.gross_profit - paid;
  setText("#finance-result", currency.format(result));
  setText("#finance-result-copy", result >= 0 ? "Resultado depois das despesas marcadas como pagas." : "As despesas pagas superam a margem do período selecionado.");
  setText("#finance-revenue", currency.format(metrics.revenue));
  setText("#finance-cost", currency.format(metrics.revenue - metrics.gross_profit + paid));
  const pending = state.expenses.filter(expense => expense.status === "Pendente").sort((a, b) => a.due_date.localeCompare(b.due_date));
  setText("#pending-count", `${pending.length} pendente${pending.length === 1 ? "" : "s"}`);
  document.getElementById("due-list").innerHTML = pending.length ? pending.slice(0, 4).map(expense => {
    const date = new Date(`${expense.due_date}T12:00:00`);
    return `<article class="due-item"><span class="due-date">${String(date.getDate()).padStart(2, "0")}<br>${date.toLocaleDateString("pt-BR", { month: "short" }).replace(".", "")}</span><div><strong>${escapeHtml(expense.description)}</strong><span>${escapeHtml(expense.category)}</span></div><b>${currency.format(expense.amount)}</b></article>`;
  }).join("") : '<p class="empty-state">Nenhuma despesa pendente.</p>';
  document.getElementById("expenses-body").innerHTML = state.expenses.length ? state.expenses.map(expense => `
    <tr><td><strong>${escapeHtml(expense.description)}</strong></td><td>${escapeHtml(expense.category)}</td><td>${escapeHtml(formatDate(expense.due_date))}</td><td><span class="status-pill ${expense.status === "Paga" ? "success" : "pending"}">${escapeHtml(expense.status)}</span></td><td class="align-right"><strong>${currency.format(expense.amount)}</strong></td></tr>
  `).join("") : emptyRow(5, "Nenhuma despesa cadastrada.");
}

function renderProducts() {
  document.getElementById("product-grid").innerHTML = state.products.map(product => `
    <article class="product-card ${product.image ? "featured-product" : ""}"><span class="category">${escapeHtml(product.category)}</span><h2>${escapeHtml(product.name)}</h2><span class="price">${currency.format(product.price)}</span>${product.image ? `<img class="product-image" src="${product.image}" alt="${escapeHtml(product.name)}" />` : ""}<div class="product-footer"><span class="product-cost">Custo da receita<b>${currency.format(product.cost)}</b></span><span class="product-margin">Margem<strong>${number.format(product.margin)}%</strong></span></div></article>
  `).join("");
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
  document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.viewTarget === target));
  setMobileNavigation(false);
  if (updateHash) history.replaceState(null, "", `#${target}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function bindModals() {
  document.addEventListener("click", (event) => {
    const open = event.target.closest("[data-modal]");
    if (open) openModal(open.dataset.modal);
    if (event.target.closest("[data-close-modal]")) closeModal();
  });
  document.getElementById("modal-backdrop").addEventListener("click", closeModal);
  document.querySelectorAll("dialog").forEach(dialog => dialog.addEventListener("cancel", () => closeModal()));
}

function openModal(name) {
  const dialog = document.getElementById(`${name}-modal`);
  if (!dialog) return;
  if (name === "sale") { state.cart.clear(); renderSaleProducts(); renderCart(); }
  if (name === "adjust") populateAdjustSelect();
  if (name === "product") { document.getElementById("product-form").reset(); document.getElementById("recipe-lines").innerHTML = ""; addRecipeLine(); }
  if (name === "expense") { document.getElementById("expense-form").reset(); document.querySelector('#expense-form [name="due_date"]').value = dateInputValue(new Date()); }
  dialog.showModal();
  document.getElementById("modal-backdrop").hidden = false;
  refreshIcons();
}

function closeModal() {
  document.querySelectorAll("dialog[open]").forEach(dialog => dialog.close());
  document.getElementById("modal-backdrop").hidden = true;
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
  document.getElementById("sale-form").addEventListener("submit", submitSale);
  document.getElementById("expense-form").addEventListener("submit", submitExpense);
  document.getElementById("adjust-form").addEventListener("submit", submitAdjustment);
  document.getElementById("inventory-form").addEventListener("submit", submitInventory);
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
}

function renderSaleProducts() {
  const container = document.getElementById("sale-products");
  container.innerHTML = state.products.filter(product => product.active).map(product => {
    const quantity = state.cart.get(product.id) || 0;
    return `<article class="sale-product ${quantity ? "selected" : ""}"><div><strong>${escapeHtml(product.name)}</strong><small>${currency.format(product.price)}</small></div><div class="stepper"><button type="button" data-cart-action="subtract" data-product-id="${product.id}" title="Remover uma unidade"><i data-lucide="minus"></i></button><span>${quantity}</span><button type="button" data-cart-action="add" data-product-id="${product.id}" title="Adicionar uma unidade"><i data-lucide="plus"></i></button></div></article>`;
  }).join("");
  refreshIcons();
}

function renderCart() {
  const rows = [...state.cart.entries()].map(([id, quantity]) => ({ product: state.products.find(product => product.id === id), quantity })).filter(row => row.product);
  const total = rows.reduce((sum, row) => sum + row.product.price * row.quantity, 0);
  document.getElementById("cart-lines").innerHTML = rows.length ? rows.map(row => `<div class="cart-line"><span>${row.quantity}x ${escapeHtml(row.product.name)}</span><strong>${currency.format(row.quantity * row.product.price)}</strong></div>`).join("") : '<p class="empty-state">Selecione um produto ao lado.</p>';
  setText("#cart-total", currency.format(total));
}

async function submitSale(event) {
  event.preventDefault();
  if (!state.cart.size) return toast("Selecione pelo menos um produto.", "error");
  const form = new FormData(event.currentTarget);
  const payload = { items: [...state.cart.entries()].map(([product_id, quantity]) => ({ product_id, quantity })), channel: form.get("channel"), payment_method: form.get("payment_method"), customer_name: form.get("customer_name") || null, discount: Number(form.get("discount") || 0) };
  await submitData("/api/orders", payload, "Venda registrada e estoque atualizado.");
}

async function submitExpense(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  payload.amount = Number(payload.amount);
  await submitData("/api/expenses", payload, "Despesa lançada no financeiro.");
}

async function submitAdjustment(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const inventoryId = form.get("inventory_id");
  await submitData(`/api/inventory/${inventoryId}/adjust`, { quantity_delta: Number(form.get("quantity_delta")), reason: form.get("reason") }, "Saldo de estoque atualizado.");
}

async function submitInventory(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  ["quantity", "minimum_quantity", "unit_cost"].forEach(key => { payload[key] = Number(payload[key]); });
  await submitData("/api/inventory", payload, "Novo insumo adicionado ao estoque.");
}

async function submitProduct(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const recipe = [...document.querySelectorAll(".recipe-line")].map(line => ({ inventory_id: line.querySelector("select").value, quantity: Number(line.querySelector("input").value) })).filter(line => line.inventory_id && line.quantity > 0);
  if (!recipe.length) return toast("Inclua ao menos um insumo na ficha técnica.", "error");
  await submitData("/api/products", { name: form.get("name"), category: form.get("category"), price: Number(form.get("price")), recipe, active: true }, "Produto salvo no cardápio.");
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
  }, "Arquivo organizado no acervo.");
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

async function submitData(path, payload, successMessage) {
  try {
    await api(path, { method: "POST", body: JSON.stringify(payload) });
    closeModal();
    toast(successMessage, "success");
    await loadAll();
  } catch (error) {
    toast(error.message || "Não foi possível salvar.", "error");
  }
}

function populateSelects() {
  populateAdjustSelect();
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
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = "Não foi possível concluir a operação.";
    try { message = (await response.json()).detail || message; } catch (_) { /* keep default */ }
    throw new Error(message);
  }
  return response.json();
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
function linkLabel(type) { return { expense: "Despesa", product: "Produto", inventory: "Insumo", supplier: "Fornecedor" }[type] || type; }
function linkTargetLabel(type, item) { return type === "expense" ? `${item.description} · ${currency.format(item.amount)}` : item.name; }
function hasSelectOption(select, value) { return [...select.options].some(option => option.value === value); }
function formatBytes(bytes) { if (!bytes) return "0 KB"; const units = ["B", "KB", "MB"]; const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1); return `${number.format(bytes / 1024 ** index)} ${units[index]}`; }
function paymentLabel(value) { return { Credito: "Crédito", Debito: "Débito", Dinheiro: "Dinheiro", Pix: "Pix" }[value] || value; }
function formatDate(value) { return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short", year: "numeric" }).format(new Date(`${value.slice(0, 10)}T12:00:00`)); }
function formatDateTime(value) { return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function dateInputValue(date) { return date.toISOString().slice(0, 10); }
function emptyRow(columns, message) { return `<tr><td colspan="${columns}" class="muted-value">${message}</td></tr>`; }
function setText(selector, value) { const element = document.querySelector(selector); if (element) element.textContent = value; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char])); }
function refreshIcons() { window.lucide?.createIcons(); }
function setRefreshing(value) { const button = document.getElementById("refresh-data"); button.disabled = value; button.classList.toggle("is-loading", value); }
function toast(message, type) { const element = document.getElementById("toast"); element.textContent = message; element.className = `toast ${type}`; element.hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => { element.hidden = true; }, 4200); }
