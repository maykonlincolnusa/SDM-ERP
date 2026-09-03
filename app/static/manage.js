/* Camada de edição do SDM.
 *
 * app.js mostra o que existe; este arquivo deixa corrigir. A separação é
 * proposital: as ações destrutivas — cancelar venda, desfazer baixa, remover
 * insumo — ficam todas juntas, com a confirmação e a explicação do efeito no
 * mesmo lugar, em vez de espalhadas no meio das telas de leitura.
 *
 * Depende do estado global carregado por app.js (state, api, toast, loadAll).
 */

(function () {
  "use strict";

  // Ações de clique, resolvidas por delegação: as tabelas são reescritas a
  // cada carga e um listener por botão vazaria a cada render.
  const ACTIONS = {
    "edit-order": (id) => editOrder(id),
    "cancel-order": (id) => cancelOrder(id),
    "delete-order": (id) => deleteOrder(id),
    "edit-expense": (id) => editExpense(id),
    "pay-expense": (id) => payExpense(id),
    "delete-expense": (id) => deleteExpense(id),
    "edit-recurring": (id) => editRecurring(id),
    "stop-recurring": (id) => stopRecurring(id),
    "edit-inventory": (id) => editInventory(id),
    "delete-inventory": (id) => deleteInventory(id),
    "purchase-item": (id) => openPurchase(id),
    "count-item": (id) => openCount(id),
    "edit-movement": (id) => editMovement(id),
    "undo-withdrawal": (id) => undoWithdrawal(id),
    "edit-anchor": (id) => editSportAnchor(id),
    "edit-market-price": (id) => editMarketPrice(id),
    "edit-recipe": (id) => editProductionRecipe(id),
    "edit-product": (id) => editProduct(id),
    "delete-product": (id) => deleteProduct(id),
    "purge-product": (id) => deleteProduct(id, true),
    "edit-addon": (id) => editAddon(id),
    "delete-addon": (id) => deleteAddon(id),
    "dossier": (id) => openDossier(id),
    "edit-channel": (id) => editChannel(id),
    "delete-channel": (id) => deleteChannel(id),
    "edit-courier": (id) => editCourier(id),
    "edit-delivery": (id) => editDelivery(id),
    "delete-delivery": (id) => deleteDelivery(id),
    "add-catalog": (id) => openCatalogItem(id, null),
  };

  let dossierProductId = null;

  document.addEventListener("DOMContentLoaded", bind);

  function bind() {
    document.addEventListener("click", onClick);
    document.getElementById("count-form").addEventListener("submit", submitCount);
    document.getElementById("count-inventory").addEventListener("change", refreshCountPreview);
    document.querySelector('#count-form [name="quantity"]').addEventListener("input", refreshCountPreview);
    document.getElementById("movement-form").addEventListener("submit", submitMovement);
    document.getElementById("movement-delete").addEventListener("click", undoMovement);
    document.getElementById("sport-anchor-form").addEventListener("submit", submitSportAnchor);
    document.getElementById("production-recipe-form").addEventListener("submit", submitProductionRecipe);
    document.getElementById("production-recipe-delete").addEventListener("click", deleteProductionRecipe);
    document.getElementById("add-production-recipe-line").addEventListener("click", () => addProductionLine("production-recipe-lines"));
    document.getElementById("production-recipe-lines").addEventListener("click", (event) => {
      if (!event.target.closest("[data-remove-production]")) return;
      if (document.querySelectorAll("#production-recipe-lines .production-line").length === 1) return;
      event.target.closest(".production-line").remove();
    });
    document.getElementById("market-price-form").addEventListener("submit", submitMarketPrice);
    document.getElementById("market-price-delete").addEventListener("click", deleteMarketPrice);
    document.getElementById("sport-anchor-delete").addEventListener("click", deleteSportAnchor);
    document.getElementById("channel-form").addEventListener("submit", submitChannel);
    document.getElementById("channel-delete").addEventListener("click", () => deleteChannel(document.getElementById("channel-id").value));
    document.getElementById("addon-delete").addEventListener("click", () => deleteAddon(document.getElementById("addon-id").value));
    document.getElementById("catalog-form").addEventListener("submit", submitCatalogItem);
    document.getElementById("recurring-form").addEventListener("submit", submitRecurring);
    document.getElementById("recurring-stop").addEventListener("click", () => stopRecurring(document.getElementById("recurring-id").value));
    document.getElementById("catalog-delete").addEventListener("click", deleteCatalogItem);
    document.getElementById("import-form").addEventListener("submit", submitImportedOrder);
    document.getElementById("import-is-delivery").addEventListener("change", toggleImportDelivery);
    document.getElementById("media-delete").addEventListener("click", deleteMedia);
    document.getElementById("courier-form").addEventListener("submit", submitCourier);
    document.getElementById("courier-delete").addEventListener("click", () => deleteCourier(document.getElementById("courier-id").value));
    document.getElementById("delivery-form").addEventListener("submit", submitDelivery);
    document.getElementById("delivery-delete").addEventListener("click", () => deleteDelivery(document.getElementById("delivery-entry-id").value));
    document.getElementById("delivery-courier").addEventListener("change", refreshDeliveryPreview);
    document.getElementById("delivery-date").addEventListener("change", refreshDeliveryPreview);
    ["deliveries", "freight_total", "bonus", "deduction", "worked"].forEach((campo) => {
      document.querySelector(`#delivery-form [name="${campo}"]`).addEventListener("input", renderDeliveryMath);
    });
    // Balcão é a maioria: os campos de entrega ficam escondidos até ela ser marcada.
    document.getElementById("sale-is-delivery").addEventListener("change", toggleSaleDelivery);
    document.getElementById("simulate-form").addEventListener("submit", submitSimulation);
    document.getElementById("add-simulate-line").addEventListener("click", () => addSimulateLine());
    document.getElementById("simulate-recipe-lines").addEventListener("click", (event) => {
      if (event.target.closest("[data-remove-recipe]")) event.target.closest(".recipe-line").remove();
    });
    document.getElementById("dossier-target").addEventListener("change", () => {
      if (dossierProductId) openDossier(dossierProductId);
    });
    document.getElementById("dossier-edit").addEventListener("click", () => {
      if (dossierProductId) editProduct(dossierProductId);
    });
  }

  function onClick(event) {
    // Um cadastro novo nunca pode herdar o id do registro editado antes.
    const opener = event.target.closest("[data-modal]");
    if (opener) resetCreateMode(opener.dataset.modal);

    const historyFilter = event.target.closest("[data-history-filter]");
    if (historyFilter) {
      state.historyFilter = historyFilter.dataset.historyFilter;
      renderHistory();
      return;
    }

    const move = event.target.closest("[data-move-catalog]");
    if (move) {
      event.preventDefault();
      moveCatalogItem(move.dataset.moveCatalog, Number(move.dataset.moveIndex), Number(move.dataset.moveStep));
      return;
    }

    const catalogItem = event.target.closest("[data-edit-catalog]");
    if (catalogItem) {
      event.preventDefault();
      openCatalogItem(catalogItem.dataset.editCatalog, catalogItem.dataset.catalogValue);
      return;
    }

    const growthFilter = event.target.closest("[data-growth-filter]");
    if (growthFilter) {
      state.growthFilter = growthFilter.dataset.growthFilter;
      renderGrowth();
      return;
    }

    const lineFilter = event.target.closest("[data-line-filter]");
    if (lineFilter) {
      state.lineFilter = lineFilter.dataset.lineFilter;
      renderBrand();
      return;
    }

    for (const [name, handler] of Object.entries(ACTIONS)) {
      const trigger = event.target.closest(`[data-${name}]`);
      if (trigger) {
        event.preventDefault();
        handler(trigger.dataset[camel(name)]);
        return;
      }
    }
  }

  function camel(value) {
    return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
  }

  // -------------------------------------------------------------- pedidos

  function editOrder(orderId) {
    const order = state.orders.find((row) => row.id === orderId);
    if (!order) return;
    if (!order.items || !order.items.length) {
      editImportedOrder(order);
      return;
    }
    const form = document.getElementById("sale-form");
    state.cart.clear();
    state.cartAddons.clear();
    order.items.forEach((item) => {
      state.cart.set(item.product_id, item.quantity);
      // O complemento volta como foi vendido, não como está no cadastro hoje:
      // corrigir a quantidade não pode reescrever o que o cliente pediu.
      if (item.addons?.length) {
        state.cartAddons.set(item.product_id, new Map(item.addons.map((chosen) => [chosen.addon_id, chosen.quantity])));
      }
    });
    renderSaleProducts();
    renderCart();
    populateChannelSelect();
    form.elements.order_id.value = order.id;
    setSelectValue(form.elements.channel, order.channel);
    setSelectValue(form.elements.payment_method, order.payment_method);
    form.elements.customer_name.value = order.customer_name || "";
    form.elements.discount.value = order.discount || 0;
    form.elements.occurred_at.value = toLocalInput(order.created_at);
    form.elements.is_delivery.checked = Boolean(order.is_delivery);
    toggleSaleDelivery();
    form.elements.delivery_fee.value = order.delivery_fee || 0;
    setSelectValue(form.elements.courier_id, order.courier_id || "");
    setText("#sale-modal-title", `Editar pedido #${order.number}`);
    setText("#submit-sale span", "Salvar correção");
    openDialog("sale-modal");
  }

  function editImportedOrder(order) {
    openImportedOrder(order);
  }

  async function cancelOrder(orderId) {
    const order = state.orders.find((row) => row.id === orderId);
    if (!order) return;
    const reason = window.prompt(
      `Cancelar o pedido #${order.number}?\n\nO valor sai do faturamento e o insumo volta para o estoque. O pedido continua no histórico.\n\nMotivo:`,
      "Cancelado pela loja",
    );
    if (reason === null) return;
    await send(`/api/orders/${orderId}/cancel`, "POST", { reason: reason || "Cancelado pela loja" }, "Pedido cancelado e estoque devolvido.");
  }

  async function deleteOrder(orderId) {
    const order = state.orders.find((row) => row.id === orderId);
    if (!order) return;
    if (!confirm(`Excluir de vez o pedido #${order.number} do histórico?\n\nEle já está cancelado, então o estoque não muda. A numeração fica com um vão.`)) return;
    await send(`/api/orders/${orderId}`, "DELETE", null, "Pedido removido do histórico.");
  }

  // ------------------------------------------------------------- despesas

  function editExpense(expenseId) {
    const expense = state.expenses.find((row) => row.id === expenseId);
    if (!expense) return;
    const form = document.getElementById("expense-form");
    form.elements.expense_id.value = expense.id;
    form.elements.description.value = expense.description;
    setSelectValue(form.elements.category, expense.category);
    form.elements.amount.value = expense.amount;
    form.elements.due_date.value = String(expense.due_date).slice(0, 10);
    setSelectValue(form.elements.status, expense.status);
    form.elements.recurring.checked = Boolean(expense.recurring);
    // Ligar o interruptor aqui abriria uma segunda série para a mesma conta.
    form.elements.recurring.disabled = Boolean(expense.series_id);
    toggleExpenseRepeat();
    setText("#expense-modal-title", "Editar despesa");
    openDialog("expense-modal");
  }

  async function payExpense(expenseId) {
    await send(`/api/expenses/${expenseId}`, "PATCH", { status: "Paga" }, "Despesa marcada como paga.");
  }

  async function deleteExpense(expenseId) {
    const expense = state.expenses.find((row) => row.id === expenseId);
    if (!expense) return;
    if (!confirm(`Excluir "${expense.description}" de ${currency.format(expense.amount)}?`)) return;
    await send(`/api/expenses/${expenseId}`, "DELETE", null, "Despesa excluída.");
  }

  // -------------------------------------------------------------- estoque

  function editInventory(inventoryId) {
    const item = state.inventory.find((row) => row.id === inventoryId);
    if (!item) return;
    const form = document.getElementById("inventory-form");
    form.elements.inventory_id.value = item.id;
    form.elements.name.value = item.name;
    form.elements.category.value = item.category;
    form.elements.unit.value = item.unit;
    form.elements.quantity.value = item.quantity;
    form.elements.minimum_quantity.value = item.minimum_quantity;
    form.elements.unit_cost.value = item.unit_cost;
    form.elements.shelf_life_days.value = item.shelf_life_days ?? "";
    form.elements.lead_time_days.value = item.lead_time_days ?? 2;
    // Sem isto a edição de qualquer insumo apagaria a unidade de manuseio, e a
    // barra voltaria a ser lançada em quilo.
    form.elements.pack_unit.value = item.pack_unit ?? "";
    form.elements.pack_size.value = item.pack_size ?? "";
    form.elements.pack_cost.value = item.pack_size ? (item.unit_cost * item.pack_size).toFixed(2) : "";
    refreshInventoryPack();
    // O saldo some da edição: quem muda saldo é contagem ou ajuste, e assim
    // toda diferença de estoque tem um lançamento explicando de onde veio.
    const quantityField = document.getElementById("inventory-quantity-field");
    quantityField.hidden = true;
    form.elements.quantity.required = false;
    setText("#inventory-modal-title", "Editar insumo");
    openDialog("inventory-modal");
  }

  async function deleteInventory(inventoryId) {
    const item = state.inventory.find((row) => row.id === inventoryId);
    if (!item) return;
    if (!confirm(`Excluir o insumo "${item.name}"?\n\nO histórico de movimentação dele deixa de ter referência.`)) return;
    await send(`/api/inventory/${inventoryId}`, "DELETE", null, "Insumo excluído.");
  }

  function openCount(inventoryId) {
    const select = document.getElementById("count-inventory");
    select.innerHTML = state.inventory
      .map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`)
      .join("");
    if (inventoryId) select.value = inventoryId;
    document.querySelector('#count-form [name="quantity"]').value = "";
    document.querySelector('#count-form [name="reason"]').value = "Contagem física";
    refreshCountPreview();
    openDialog("count-modal");
  }

  function refreshCountPreview() {
    const select = document.getElementById("count-inventory");
    const item = state.inventory.find((row) => row.id === select.value);
    const currentField = document.getElementById("count-current");
    const preview = document.getElementById("count-preview");
    if (!item) {
      currentField.value = "";
      preview.textContent = "";
      return;
    }
    currentField.value = `${number.format(item.quantity)} ${item.unit}`;
    const counted = document.querySelector('#count-form [name="quantity"]').value;
    if (counted === "") {
      preview.textContent = "";
      preview.className = "modal-result full";
      return;
    }
    const difference = Number(counted) - item.quantity;
    if (Math.abs(difference) < 0.0005) {
      preview.textContent = "A contagem confere com o sistema. Nenhum lançamento será criado.";
      preview.className = "modal-result full neutral";
      return;
    }
    const value = Math.abs(difference) * item.unit_cost;
    preview.textContent = difference > 0
      ? `Sobra de ${number.format(difference)} ${item.unit} (${currency.format(value)}). Vai entrar como acerto de estoque.`
      : `Falta de ${number.format(Math.abs(difference))} ${item.unit} (${currency.format(value)}). Vai entrar como perda.`;
    preview.className = `modal-result full ${difference > 0 ? "positive" : "negative"}`;
  }

  async function submitCount(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const inventoryId = document.getElementById("count-inventory").value;
    await submitData(
      `/api/inventory/${inventoryId}/count`,
      { quantity: Number(form.get("quantity")), reason: form.get("reason") },
      "Contagem registrada.",
    );
  }

  function editMovement(movementId) {
    const movement = state.consumption?.history.find((row) => row.id === movementId);
    if (!movement) return;
    const form = document.getElementById("movement-form");
    form.elements.movement_id.value = movement.id;
    form.elements.quantity_delta.value = movement.quantity_delta;
    form.elements.reason.value = movement.reason;
    document.getElementById("movement-context").textContent =
      `${movement.name} · lançado em ${formatDateTime(movement.created_at)}. Corrigir a quantidade também acerta o saldo do estoque.`;
    setText("#movement-modal-title", "Corrigir lançamento");
    openDialog("movement-modal");
  }

  async function submitMovement(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    await submitData(
      `/api/inventory/movements/${form.get("movement_id")}`,
      { quantity_delta: Number(form.get("quantity_delta")), reason: form.get("reason") },
      "Lançamento corrigido e saldo ajustado.",
      "PATCH",
    );
  }

  // A linha da retirada não se corrige sozinha: as outras do mesmo lançamento
  // saíram juntas e voltam juntas, senão o saldo fica meio devolvido.
  async function undoWithdrawal(withdrawalId) {
    const lines = (state.consumption?.history || []).filter((row) => row.withdrawal_id === withdrawalId);
    if (!lines.length) return;
    const resumo = lines.map((row) => `- ${number.format(Math.abs(row.quantity_delta))} ${row.unit} de ${row.name}`).join("\n");
    const quem = lines[0].responsible ? ` registrada por ${lines[0].responsible}` : "";
    if (!confirm(`Desfazer a retirada${quem}?\n\n${resumo}\n\nTudo volta para o saldo do estoque e os lançamentos somem do consumo.`)) return;
    await send(`/api/inventory/withdrawals/${withdrawalId}`, "DELETE", null, "Retirada desfeita e saldo devolvido.");
  }

  // ------------------------------------------------- fichas de fabricação

  function editProductionRecipe(recipeId) {
    const ficha = state.productionRecipes.find((linha) => linha.id === recipeId);
    if (!ficha) return;
    const form = document.getElementById("production-recipe-form");
    form.reset();
    document.getElementById("production-recipe-lines").innerHTML = "";
    ficha.inputs.forEach((linha) => addProductionLine("production-recipe-lines", linha.inventory_id, linha.quantity));
    document.getElementById("production-recipe-output").innerHTML = inventoryOptions(ficha.output_inventory_id);
    form.elements.recipe_id.value = ficha.id;
    form.elements.name.value = ficha.name;
    form.elements.output_quantity.value = ficha.output_quantity;
    form.elements.note.value = ficha.note || "";
    document.getElementById("production-recipe-delete").hidden = false;
    setText("#production-recipe-modal-title", "Editar ficha de batida");
    openDialog("production-recipe-modal");
  }

  async function submitProductionRecipe(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const inputs = readProductionLines("production-recipe-lines");
    if (!inputs.length) { toast("Inclua ao menos um insumo na ficha.", "error"); return; }
    const payload = {
      name: form.get("name"),
      inputs,
      output_inventory_id: form.get("output_inventory_id"),
      output_quantity: Number(form.get("output_quantity")),
      note: form.get("note") || null,
    };
    const recipeId = form.get("recipe_id");
    if (recipeId) {
      await send(`/api/production/recipes/${recipeId}`, "PATCH", payload, "Ficha atualizada.");
      return;
    }
    await send("/api/production/recipes", "POST", payload, "Ficha cadastrada. A batida passa a vir pré-preenchida.");
  }

  async function deleteProductionRecipe() {
    const recipeId = document.getElementById("production-recipe-id").value;
    if (!recipeId) return;
    if (!confirm("Excluir esta ficha?\n\nAs fabricações já lançadas continuam no histórico; só o preenchimento automático deixa de existir.")) return;
    await send(`/api/production/recipes/${recipeId}`, "DELETE", null, "Ficha removida.");
  }

  // ------------------------------------------------ referências de mercado

  function populateMarketProducts(selecionado = "") {
    const select = document.getElementById("market-price-product");
    select.innerHTML = '<option value="">Nenhum — comparar pela categoria</option>'
      + state.products.map((produto) => `<option value="${produto.id}">${escapeHtml(produto.name)} — ${currency.format(produto.price)}</option>`).join("");
    select.value = selecionado || "";
  }

  function editMarketPrice(referenceId) {
    const referencia = state.marketPrices.find((linha) => linha.id === referenceId);
    if (!referencia) return;
    const form = document.getElementById("market-price-form");
    form.reset();
    populateMarketProducts(referencia.product_id);
    form.elements.reference_id.value = referencia.id;
    form.elements.competitor.value = referencia.competitor || "";
    form.elements.description.value = referencia.description || "";
    form.elements.price.value = referencia.price;
    form.elements.category.value = referencia.category || "";
    form.elements.channel.value = referencia.channel || "Balcao";
    form.elements.observed_on.value = (referencia.observed_on || "").slice(0, 10);
    form.elements.note.value = referencia.note || "";
    document.getElementById("market-price-delete").hidden = false;
    setText("#market-price-modal-title", "Editar referência");
    openDialog("market-price-modal");
  }

  async function submitMarketPrice(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const referenceId = form.get("reference_id");
    const payload = {
      competitor: form.get("competitor"),
      description: form.get("description"),
      price: Number(form.get("price")),
      product_id: form.get("product_id") || null,
      category: form.get("category") || null,
      channel: form.get("channel") || "Balcao",
      observed_on: form.get("observed_on") || null,
      note: form.get("note") || null,
    };
    if (!payload.product_id && !payload.category) {
      toast("Ligue a referência a um produto do cardápio ou a uma categoria.", "error");
      return;
    }
    if (referenceId) {
      await send(`/api/market-prices/${referenceId}`, "PATCH", payload, "Referência atualizada.");
      return;
    }
    await send("/api/market-prices", "POST", payload, "Referência cadastrada. O veredito de preço passa a ter a âncora de mercado.");
  }

  async function deleteMarketPrice() {
    const referenceId = document.getElementById("market-price-id").value;
    if (!referenceId) return;
    if (!confirm("Excluir esta referência?\n\nO veredito de preço do produto volta a ser calculado sem ela.")) return;
    await send(`/api/market-prices/${referenceId}`, "DELETE", null, "Referência removida.");
  }

  // --------------------------------------------------- âncoras esportivas

  function anchorWeekdays() {
    return [...document.querySelectorAll("#sport-anchor-weekdays input:checked")].map((campo) => Number(campo.value));
  }

  function editSportAnchor(anchorId) {
    const ancora = (state.promotions?.anchors || []).find((linha) => linha.id === anchorId);
    if (!ancora) return;
    const form = document.getElementById("sport-anchor-form");
    form.reset();
    form.elements.anchor_id.value = ancora.id;
    form.elements.name.value = ancora.name;
    setSelectValue(form.elements.kind, ancora.kind);
    setSelectValue(form.elements.time_of_day, ancora.time_of_day);
    form.elements.people.value = ancora.people || "";
    form.elements.note.value = ancora.note || "";
    const marcados = new Set(ancora.weekdays || []);
    document.querySelectorAll("#sport-anchor-weekdays input").forEach((campo) => {
      campo.checked = marcados.has(Number(campo.value));
    });
    document.getElementById("sport-anchor-delete").hidden = false;
    setText("#sport-anchor-modal-title", "Editar âncora");
    openDialog("sport-anchor-modal");
  }

  async function submitSportAnchor(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const anchorId = form.get("anchor_id");
    const payload = {
      name: form.get("name"),
      kind: form.get("kind"),
      weekdays: anchorWeekdays(),
      time_of_day: form.get("time_of_day"),
      people: form.get("people") ? Number(form.get("people")) : null,
      note: form.get("note") || null,
    };
    if (anchorId) {
      await send(`/api/sport-anchors/${anchorId}`, "PATCH", payload, "Âncora atualizada.");
      return;
    }
    await send("/api/sport-anchors", "POST", payload, "Âncora cadastrada. As promoções da linha Sport passam a usar esses dias.");
  }

  async function deleteSportAnchor() {
    const anchorId = document.getElementById("sport-anchor-id").value;
    if (!anchorId) return;
    if (!confirm("Excluir esta âncora?\n\nAs sugestões esportivas que dependiam dos dias dela deixam de aparecer.")) return;
    await send(`/api/sport-anchors/${anchorId}`, "DELETE", null, "Âncora removida.");
  }

  async function undoMovement() {
    const movementId = document.getElementById("movement-id").value;
    if (!movementId) return;
    if (!confirm("Desfazer este lançamento?\n\nO saldo volta ao que era antes dele e o registro é apagado.")) return;
    await send(`/api/inventory/movements/${movementId}`, "DELETE", null, "Lançamento desfeito.");
  }

  // ------------------------------------------------------------- produtos

  function editProduct(productId) {
    const product = state.products.find((row) => row.id === productId);
    if (!product) return;
    const form = document.getElementById("product-form");
    form.reset();
    form.elements.product_id.value = product.id;
    form.elements.name.value = product.name;
    form.elements.category.value = product.category;
    form.elements.price.value = product.price;
    setSelectValue(form.elements.line, product.line || "Classico");
    form.elements.claims.value = (product.claims || []).join(", ");
    const nutrition = product.nutrition || {};
    ["kcal", "protein_g", "carbs_g", "fiber_g"].forEach((key) => {
      form.elements[key].value = nutrition[key] ?? "";
    });
    const container = document.getElementById("recipe-lines");
    container.innerHTML = "";
    (product.recipe || []).forEach((line) => {
      addRecipeLine();
      const row = container.lastElementChild;
      row.querySelector("select").value = line.inventory_id;
      row.querySelector("input").value = line.quantity;
    });
    if (!container.children.length) addRecipeLine();
    setText("#product-modal-title", `Editar ${product.name}`);
    openDialog("product-modal");
  }

  async function deleteProduct(productId, permanent = false) {
    const product = state.products.find((row) => row.id === productId);
    if (!product) return;
    // Sai em duas etapas de propósito: o primeiro clique tira do balcão, o
    // segundo tira do cadastro. Assim ninguém apaga por engano o produto que
    // responde pelo faturamento do mês passado.
    const pergunta = permanent
      ? `Excluir "${product.name}" do cadastro de vez?\n\nOs pedidos antigos continuam com nome, preço e custo do dia da venda, mas o dossiê de custo e a previsão deste produto deixam de existir.`
      : `Tirar "${product.name}" do cardápio?\n\nSe ele já tem vendas, o registro é mantido no histórico e o produto apenas deixa de aparecer para venda.`;
    if (!confirm(pergunta)) return;
    const rota = permanent ? `/api/products/${productId}?permanent=true` : `/api/products/${productId}`;
    await send(rota, "DELETE", null, permanent ? "Produto excluído do cadastro." : "Produto removido do cardápio.");
  }

  // --------------------------------------------------------- complementos

  function editAddon(addonId) {
    const addon = state.addons.find((row) => row.id === addonId);
    if (!addon) return;
    const form = document.getElementById("addon-form");
    form.reset();
    populateAddonInventory();
    form.elements.addon_id.value = addon.id;
    form.elements.name.value = addon.name;
    setSelectValue(form.elements.inventory_id, addon.inventory_id);
    form.elements.quantity.value = addon.quantity;
    form.elements.price.value = addon.price;
    form.elements.category.value = addon.category || "Complemento";
    form.elements.active.checked = addon.active !== false;
    document.getElementById("addon-delete").hidden = false;
    refreshAddonPreview();
    setText("#addon-modal-title", `Editar ${addon.name}`);
    openDialog("addon-modal");
  }

  async function deleteAddon(addonId) {
    const addon = state.addons.find((row) => row.id === addonId);
    if (!addon) return;
    if (!confirm(`Remover o complemento "${addon.name}"?\n\nSe ele já foi vendido, fica apenas desativado — os pedidos antigos precisam continuar explicando o que saiu do estoque.`)) return;
    await send(`/api/addons/${addonId}`, "DELETE", null, "Complemento removido.");
  }

  // ------------------------------------------------------ despesas fixas

  function editRecurring(seriesId) {
    const serie = state.expenseSeries.find((row) => row.id === seriesId);
    if (!serie) return;
    const form = document.getElementById("recurring-form");
    form.reset();
    form.elements.series_id.value = serie.id;
    form.elements.description.value = serie.description;
    form.elements.category.value = serie.category;
    form.elements.amount.value = serie.amount;
    form.elements.day.value = serie.day;
    form.elements.repeat_until.value = serie.ends_on || "";
    document.getElementById("recurring-stop").hidden = !serie.active;
    setText(
      "#recurring-preview",
      serie.active
        ? `Já lançou ${serie.released} mês(es).${serie.next_due ? ` Próximo vencimento em ${formatDate(serie.next_due)}.` : ""}`
        : `Parou de repetir. Os ${serie.released} mês(es) lançados continuam no histórico.`,
    );
    setText("#recurring-modal-title", `Editar ${serie.description}`);
    openDialog("recurring-modal");
  }

  async function submitRecurring(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const payload = {
      description: form.get("description"),
      category: form.get("category"),
      amount: Number(form.get("amount")),
      day: Number(form.get("day")),
    };
    // Só envia o fim quando ele existe: mandar vazio apagaria o "sem fim previsto".
    if (form.get("repeat_until")) payload.repeat_until = form.get("repeat_until");
    await send(`/api/expenses/recurring/${form.get("series_id")}`, "PATCH", payload, "Regra atualizada. Vale para os próximos meses.");
  }

  async function stopRecurring(seriesId) {
    const serie = state.expenseSeries.find((row) => row.id === seriesId);
    if (!serie) return;
    if (!confirm(`Parar de lançar "${serie.description}" todo mês?\n\nOs ${serie.released} mês(es) já lançados continuam no histórico. O custo fixo da projeção cai ${currency.format(serie.amount)}.`)) return;
    await send(`/api/expenses/recurring/${seriesId}`, "DELETE", null, "Não será mais lançada nos próximos meses.");
  }

  // --------------------------------------------------------------- canais

  function editChannel(channelId) {
    const channel = state.channels.find((row) => row.id === channelId);
    if (!channel) return;
    const form = document.getElementById("channel-form");
    form.elements.channel_id.value = channel.id;
    form.elements.name.value = channel.name;
    form.elements.fee_percent.value = channel.fee_percent;
    form.elements.fee_fixed.value = channel.fee_fixed;
    form.elements.note.value = channel.note || "";
    form.elements.active.checked = Boolean(channel.active);
    document.getElementById("channel-delete").hidden = false;
    setText("#channel-modal-title", `Editar ${channel.name}`);
    openDialog("channel-modal");
  }

  async function submitChannel(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const channelId = form.get("channel_id");
    const payload = {
      name: form.get("name"),
      fee_percent: Number(form.get("fee_percent") || 0),
      fee_fixed: Number(form.get("fee_fixed") || 0),
      note: form.get("note") || null,
      active: form.get("active") === "on",
    };
    if (channelId) {
      await submitData(`/api/channels/${channelId}`, payload, "Canal atualizado.", "PATCH");
      return;
    }
    await submitData("/api/channels", payload, "Canal cadastrado.");
  }

  async function deleteChannel(channelId) {
    const channel = state.channels.find((row) => row.id === channelId);
    if (!channel) return;
    if (!confirm(`Remover o canal "${channel.name}"?\n\nSe ele já tem vendas, será apenas desativado para o histórico continuar explicável.`)) return;
    await send(`/api/channels/${channelId}`, "DELETE", null, "Canal removido.");
  }

  // ------------------------------------------------------------ simulação

  function addSimulateLine() {
    const options = state.inventory
      .map((item) => `<option value="${item.id}">${escapeHtml(item.name)} (${escapeHtml(item.unit)})</option>`)
      .join("");
    document.getElementById("simulate-recipe-lines").insertAdjacentHTML(
      "beforeend",
      `<div class="recipe-line"><label>Insumo<select>${options}</select></label><label>Quantidade<input type="number" min="0.001" step="0.001" placeholder="0,000" /></label><button type="button" data-remove-recipe title="Remover insumo"><i data-lucide="trash-2"></i></button></div>`,
    );
    refreshIcons();
  }

  async function submitSimulation(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const recipe = [...document.querySelectorAll("#simulate-recipe-lines .recipe-line")]
      .map((line) => ({ inventory_id: line.querySelector("select").value, quantity: Number(line.querySelector("input").value) }))
      .filter((line) => line.inventory_id && line.quantity > 0);
    const payload = {
      name: form.get("name") || "Produto novo",
      price: Number(form.get("price")),
      recipe,
      extra_cost: Number(form.get("extra_cost") || 0),
      extra_cost_label: form.get("extra_cost_label") || null,
      target_margin: Number(form.get("target_margin") || 0),
      expected_daily_units: Number(form.get("expected_daily_units") || 0),
    };
    if (!recipe.length && !payload.extra_cost) {
      toast("Informe a ficha técnica ou um custo estimado para simular.", "error");
      return;
    }
    try {
      renderSimulation(await api("/api/products/simulate", { method: "POST", body: JSON.stringify(payload) }));
    } catch (error) {
      toast(error.message || "Não foi possível simular.", "error");
    }
  }

  function renderSimulation(result) {
    const verdicts = {
      inviavel: { label: "Inviável", css: "danger" },
      atencao: { label: "Atenção", css: "pending" },
      apertado: { label: "Apertado", css: "pending" },
      saudavel: { label: "Saudável", css: "success" },
      forte: { label: "Forte", css: "success" },
    };
    const verdict = verdicts[result.verdict] || verdicts.saudavel;
    const projection = result.projection.daily_units > 0
      ? `<div class="simulate-block"><p class="section-label">Se vender ${number.format(result.projection.daily_units)} por dia</p>
           <div class="simulate-rows">
             <span>Faturamento no mês<b>${currency.format(result.projection.monthly_revenue)}</b></span>
             <span>Sobra no mês<b>${currency.format(result.projection.monthly_contribution)}</b></span>
           </div></div>`
      : "";
    const cover = result.projection.units_to_cover_fixed
      ? `<p class="formula-note">Sozinho, este produto pagaria toda a estrutura da loja com ${number.format(result.projection.units_to_cover_fixed)} unidades por mês. Serve de escala, não de meta.</p>`
      : "";

    document.getElementById("simulate-result").innerHTML = `
      <div class="simulate-verdict ${verdict.css}"><span class="status-pill ${verdict.css}">${verdict.label}</span><p>${escapeHtml(result.reading)}</p></div>
      <div class="simulate-block">
        <p class="section-label">A conta</p>
        <div class="simulate-rows">
          <span>Preço<b>${currency.format(result.price)}</b></span>
          <span>Custo direto<b>${currency.format(result.cost.direct)}</b></span>
          <span>Sobra por unidade<b class="${result.contribution >= 0 ? "positive" : "negative"}">${currency.format(result.contribution)}</b></span>
          <span>Margem<b>${number.format(result.gross_margin)}%</b></span>
        </div>
      </div>
      <div class="simulate-block">
        <p class="section-label">Onde o custo está</p>
        ${result.recipe.length ? result.recipe.map((row) => `<div class="cost-line"><span>${escapeHtml(row.name)}${row.estimated ? ' <em>estimado</em>' : ""}</span><span class="cost-bar"><span style="width:${row.share}%"></span></span><b>${currency.format(row.cost)}</b></div>`).join("") : '<p class="empty-state">Sem ficha técnica.</p>'}
      </div>
      <div class="simulate-block">
        <p class="section-label">Quanto sobra em cada canal</p>
        ${result.channels.map((row) => `<div class="cost-line"><span>${escapeHtml(row.name)}${row.fee ? ` <em>-${currency.format(row.fee)}</em>` : ""}</span><b class="${row.profitable ? "positive" : "negative"}">${currency.format(row.contribution)}</b></div>`).join("")}
      </div>
      <div class="simulate-block">
        <p class="section-label">Preço por margem-alvo</p>
        <div class="ladder">${result.price_ladder.map((row) => `<span><b>${number.format(row.margin)}%</b>${currency.format(row.price)}</span>`).join("")}</div>
        ${result.target.price ? `<p class="formula-note">Para os ${number.format(result.target.margin)}% que você pediu, o preço é ${currency.format(result.target.price)}.</p>` : ""}
      </div>
      ${projection}
      ${cover}
      ${result.alerts.map((alert) => `<div class="insight ${alert.type}"><span class="insight-icon"><i data-lucide="triangle-alert"></i></span><div><strong>${escapeHtml(alert.title)}</strong><p>${escapeHtml(alert.description)}</p></div></div>`).join("")}`;
    refreshIcons();
  }

  // --------------------------------------------------------------- dossiê

  async function openDossier(productId) {
    dossierProductId = productId;
    const target = Number(document.getElementById("dossier-target").value || 0);
    openDialog("dossier-modal");
    document.getElementById("dossier-body").innerHTML = '<p class="empty-state">Carregando dossiê...</p>';
    try {
      renderDossier(await api(`/api/products/${productId}/dossier?days=30&target_margin=${target}`));
    } catch (error) {
      document.getElementById("dossier-body").innerHTML = `<p class="empty-state">${escapeHtml(error.message || "Não foi possível abrir o dossiê.")}</p>`;
    }
  }

  function renderDossier(data) {
    const p = data.price_analysis;
    setText("#dossier-modal-title", data.product.name);
    setText("#dossier-line", `${data.product.line} · ${data.product.category}`);

    const nutrition = Object.entries(data.product.nutrition || {}).filter(([, value]) => value !== null && value !== undefined);
    const labels = { kcal: "kcal", protein_g: "g proteína", carbs_g: "g carboidrato", fat_g: "g gordura", fiber_g: "g fibra" };

    document.getElementById("dossier-body").innerHTML = `
      <div class="dossier-metrics">
        <article><p>Preço</p><strong>${currency.format(p.price)}</strong></article>
        <article><p>Custo da ficha</p><strong>${currency.format(data.cost.direct)}</strong><small>${number.format(data.cost.packaging_share)}% é embalagem</small></article>
        <article><p>Custo com quebra</p><strong>${currency.format(data.cost.with_shrinkage)}</strong><small>${data.cost.shrinkage > 0 ? `${currency.format(data.cost.shrinkage)} do que saiu sem vender` : "nada saiu sem vender"}</small></article>
        <article><p>Sobra por unidade</p><strong class="${p.contribution >= 0 ? "positive" : "negative"}">${currency.format(p.contribution)}</strong><small>margem de ${number.format(p.gross_margin)}%</small></article>
        <article><p>Depois da estrutura</p><strong>${p.net_margin === null ? "—" : `${number.format(p.net_margin)}%`}</strong><small>${p.net_margin === null ? "sem volume para ratear" : `${currency.format(p.profit_per_unit)} de lucro real`}</small></article>
      </div>

      <section class="dossier-section">
        <p class="section-label">Onde cada centavo do custo está</p>
        ${data.recipe.length ? data.recipe.map((row) => `
          <div class="cost-line"><span>${escapeHtml(row.name)} <em>${number.format(row.quantity)} ${escapeHtml(row.unit)}${row.packaging ? " · embalagem" : ""}${row.shrinkage_rate ? ` · ${number.format(row.shrinkage_rate)}% sai sem vender` : ""}</em></span><span class="cost-bar"><span style="width:${row.share}%"></span></span><b>${currency.format(row.cost)}</b></div>`).join("") : '<p class="empty-state">Este produto não tem ficha técnica cadastrada, então o custo não pode ser calculado.</p>'}
        <div class="cost-total"><span>Ingredientes ${currency.format(data.cost.ingredients)}</span><span>Embalagem ${currency.format(data.cost.packaging)}</span><span><b>Total ${currency.format(data.cost.direct)}</b></span></div>
        ${data.cost.shrinkage > 0 ? `<p class="formula-note">Somando o que saiu do estoque sem virar venda nos últimos ${data.cost.shrinkage_days} dias, cada unidade custa <b>${currency.format(data.cost.with_shrinkage)}</b> — ${number.format(data.cost.shrinkage_share)}% acima da ficha. É esse custo que a escada de preço e o ponto de equilíbrio usam.</p>` : ""}
      </section>

      <section class="dossier-section">
        <p class="section-label">Quanto sobra em cada canal</p>
        <div class="table-scroll"><table><thead><tr><th>Canal</th><th class="align-right">Taxa</th><th class="align-right">Recebe</th><th class="align-right">Sobra</th><th class="align-right">Margem</th><th class="align-right">Preço p/ igualar</th></tr></thead><tbody>
          ${data.channels.map((row) => `<tr class="${row.profitable ? "" : "cancelled-row"}"><td><strong>${escapeHtml(row.name)}</strong></td><td class="align-right muted-value">${row.fee ? `-${currency.format(row.fee)}` : "—"}</td><td class="align-right">${currency.format(row.net_price)}</td><td class="align-right"><b class="${row.profitable ? "positive" : "negative"}">${currency.format(row.contribution)}</b></td><td class="align-right">${row.margin === null ? "—" : `${number.format(row.margin)}%`}</td><td class="align-right muted-value">${row.price_to_match === null ? "—" : currency.format(row.price_to_match)}</td></tr>`).join("")}
        </tbody></table></div>
      </section>

      <div class="dossier-columns">
        <section class="dossier-section">
          <p class="section-label">Preço por margem-alvo</p>
          <div class="ladder">${data.price_ladder.map((row) => `<span><b>${number.format(row.margin)}%</b>${currency.format(row.price)}</span>`).join("")}</div>
          <p class="formula-note">Calculada sobre o custo com quebra (${currency.format(data.cost.with_shrinkage)}), não sobre a ficha.</p>
          ${data.target && data.target.price ? `<p class="formula-note">Para ${number.format(data.target.margin)}% o preço seria ${currency.format(data.target.price)} — ${data.target.difference >= 0 ? `${currency.format(data.target.difference)} acima` : `${currency.format(Math.abs(data.target.difference))} abaixo`} do preço de hoje.</p>` : '<p class="formula-note">Informe uma margem-alvo no rodapé para ver o preço correspondente.</p>'}
        </section>
        <section class="dossier-section">
          <p class="section-label">Estrutura e ponto de equilíbrio</p>
          <p class="formula-note">Despesas fixas do mês: <b>${currency.format(data.fixed.monthly)}</b> em ${data.fixed.expenses} lançamento${data.fixed.expenses === 1 ? "" : "s"}${data.fixed.partial ? " <b>(mês ainda aberto)</b>" : ""}. Base: ${escapeHtml(data.fixed.expense_basis)}. Rateio: ${escapeHtml(data.fixed.basis)}.</p>
          <p class="formula-note">${escapeHtml(data.breakeven.note)}</p>
        </section>
      </div>

      <section class="dossier-section">
        <p class="section-label">Giro nos últimos ${data.period_days} dias</p>
        <div class="simulate-rows">
          <span>Unidades<b>${number.format(data.sales.units)}</b></span>
          <span>Faturamento<b>${currency.format(data.sales.revenue)}</b></span>
          <span>Média diária<b>${number.format(data.sales.daily_average)}</b></span>
          <span>Do faturamento da loja<b>${number.format(data.sales.share_of_revenue)}%</b></span>
        </div>
        ${data.sales.by_channel.length ? `<p class="formula-note">Por canal: ${data.sales.by_channel.map((row) => `${escapeHtml(row.name)} ${number.format(row.units)}`).join(" · ")}.</p>` : ""}
      </section>

      ${(data.product.claims || []).length || nutrition.length ? `<section class="dossier-section">
        <p class="section-label">O que este produto promete</p>
        ${(data.product.claims || []).length ? `<div class="media-tags">${data.product.claims.map((claim) => `<span>${escapeHtml(claim)}</span>`).join("")}</div>` : ""}
        ${nutrition.length ? `<div class="simulate-rows">${nutrition.map(([key, value]) => `<span>${escapeHtml(labels[key] || key)}<b>${number.format(value)}</b></span>`).join("")}</div>` : ""}
      </section>` : ""}

      ${data.alerts.map((alert) => `<div class="insight ${alert.type}"><span class="insight-icon"><i data-lucide="${alert.type === "attention" ? "triangle-alert" : "lightbulb"}"></i></span><div><strong>${escapeHtml(alert.title)}</strong><p>${escapeHtml(alert.description)}</p></div></div>`).join("")}`;
    refreshIcons();
  }


  // -------------------------------------------------------------- entregas

  function toggleSaleDelivery() {
    const entrega = document.getElementById("sale-is-delivery").checked;
    document.querySelectorAll(".sale-delivery-field").forEach((campo) => { campo.hidden = !entrega; });
    if (!entrega) {
      const form = document.getElementById("sale-form");
      form.elements.delivery_fee.value = 0;
      form.elements.courier_id.value = "";
    }
  }

  function editCourier(courierId) {
    const courier = state.couriers.find((row) => row.id === courierId);
    if (!courier) return;
    const form = document.getElementById("courier-form");
    form.elements.courier_id.value = courier.id;
    form.elements.name.value = courier.name;
    form.elements.phone.value = courier.phone || "";
    form.elements.vehicle.value = courier.vehicle || "";
    form.elements.active.checked = Boolean(courier.active);
    ["fee_per_delivery", "commission_percent", "daily_rate", "monthly_salary"].forEach((campo) => {
      form.elements[campo].value = courier[campo] ?? 0;
    });
    form.elements.note.value = courier.note || "";
    document.getElementById("courier-delete").hidden = false;
    setText("#courier-modal-title", `Editar ${courier.name}`);
    openDialog("courier-modal");
  }

  async function submitCourier(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const courierId = form.get("courier_id");
    const payload = {
      name: form.get("name"),
      phone: form.get("phone") || null,
      vehicle: form.get("vehicle") || null,
      active: form.get("active") === "on",
      note: form.get("note") || null,
    };
    ["fee_per_delivery", "commission_percent", "daily_rate", "monthly_salary"].forEach((campo) => {
      payload[campo] = Number(form.get(campo) || 0);
    });
    if (courierId) {
      await submitData(`/api/couriers/${courierId}`, payload, "Entregador atualizado.", "PATCH");
      return;
    }
    await submitData("/api/couriers", payload, "Entregador cadastrado.");
  }

  async function deleteCourier(courierId) {
    const courier = state.couriers.find((row) => row.id === courierId);
    if (!courier) return;
    if (!confirm(`Remover "${courier.name}"?\n\nSe já tem turnos lançados, ele é apenas desativado — o histórico de custo da entrega precisa continuar com dono.`)) return;
    await send(`/api/couriers/${courierId}`, "DELETE", null, "Entregador removido.");
  }

  function openDelivery(entry) {
    const form = document.getElementById("delivery-form");
    form.reset();
    document.getElementById("delivery-entry-id").value = entry ? entry.id : "";
    document.getElementById("delivery-delete").hidden = !entry;
    populateCourierSelects();
    if (entry) {
      form.elements.courier_id.value = entry.courier_id;
      form.elements.date.value = String(entry.date).slice(0, 10);
      form.elements.deliveries.value = entry.deliveries;
      form.elements.freight_total.value = entry.freight_total;
      form.elements.bonus.value = entry.bonus || 0;
      form.elements.deduction.value = entry.deduction || 0;
      form.elements.worked.checked = entry.worked !== false;
      form.elements.payout_override.value = entry.payout_override ?? "";
      form.elements.note.value = entry.note || "";
      setText("#delivery-modal-title", `Turno de ${entry.courier_name}`);
      setText("#delivery-suggestion", "");
      renderDeliveryMath();
    } else {
      form.elements.date.value = todayInputValue();
      form.elements.worked.checked = true;
      setText("#delivery-modal-title", "Lançar turno");
      refreshDeliveryPreview();
    }
    openDialog("delivery-modal");
  }

  function editDelivery(entryId) {
    const entry = state.deliveries.find((row) => row.id === entryId);
    if (entry) openDelivery(entry);
  }

  /* Preenche o turno com o que os pedidos daquele dia já registram.
   * Digitar de memória no fim do expediente é onde o número se perde. */
  async function refreshDeliveryPreview() {
    const form = document.getElementById("delivery-form");
    const courierId = form.elements.courier_id.value;
    const dia = form.elements.date.value;
    const aviso = document.getElementById("delivery-suggestion");
    if (!courierId || !dia || document.getElementById("delivery-entry-id").value) {
      renderDeliveryMath();
      return;
    }
    try {
      const sugestao = await api(`/api/deliveries/suggest?courier_id=${encodeURIComponent(courierId)}&day=${dia}`);
      if (sugestao.deliveries || sugestao.freight_total) {
        form.elements.deliveries.value = sugestao.deliveries;
        form.elements.freight_total.value = sugestao.freight_total;
        aviso.textContent = `Preenchido com ${sugestao.deliveries} pedido(s) de entrega deste dia. ${sugestao.note}`.trim();
      } else {
        aviso.textContent = sugestao.note || "Nenhum pedido de entrega registrado neste dia para este entregador.";
      }
    } catch (_) {
      aviso.textContent = "";
    }
    renderDeliveryMath();
  }

  /* A conta aberta na tela: acerto de entregador é conversa, e conversa sem a
   * conta à vista vira desconfiança. */
  function renderDeliveryMath() {
    const form = document.getElementById("delivery-form");
    const courier = state.couriers.find((row) => row.id === form.elements.courier_id.value);
    const alvo = document.getElementById("delivery-preview");
    if (!courier) {
      alvo.textContent = "";
      return;
    }
    const entregas = Number(form.elements.deliveries.value || 0);
    const frete = Number(form.elements.freight_total.value || 0);
    const partes = [];
    if (courier.fee_per_delivery) partes.push({ label: `${entregas} × ${currency.format(courier.fee_per_delivery)}`, value: entregas * courier.fee_per_delivery });
    if (courier.commission_percent) partes.push({ label: `${number.format(courier.commission_percent)}% do frete`, value: frete * courier.commission_percent / 100 });
    if (courier.daily_rate && form.elements.worked.checked) partes.push({ label: "diária", value: courier.daily_rate });
    const bonus = Number(form.elements.bonus.value || 0);
    const desconto = Number(form.elements.deduction.value || 0);
    if (bonus) partes.push({ label: "bônus", value: bonus });
    if (desconto) partes.push({ label: "desconto", value: -desconto });
    const total = partes.reduce((soma, parte) => soma + parte.value, 0);
    alvo.className = "modal-result full neutral";
    alvo.textContent = partes.length
      ? `${partes.map((parte) => `${parte.label} = ${currency.format(parte.value)}`).join("  +  ")}   →   ${currency.format(total)}`
      : `${courier.name} está sem remuneração definida. Edite o cadastro para a conta aparecer.`;
  }

  async function submitDelivery(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const entryId = form.get("entry_id");
    const override = form.get("payout_override");
    const payload = {
      courier_id: form.get("courier_id"),
      date: form.get("date"),
      deliveries: Number(form.get("deliveries") || 0),
      freight_total: Number(form.get("freight_total") || 0),
      worked: form.get("worked") === "on",
      bonus: Number(form.get("bonus") || 0),
      deduction: Number(form.get("deduction") || 0),
      payout_override: override === "" ? null : Number(override),
      note: form.get("note") || null,
    };
    if (payload.payout_override !== null && !payload.note) {
      toast("Ao pagar valor diferente do combinado, escreva o motivo.", "error");
      return;
    }
    if (entryId) {
      await submitData(`/api/deliveries/${entryId}`, payload, "Turno corrigido.", "PATCH");
      return;
    }
    await submitData("/api/deliveries", payload, "Turno lançado.");
  }

  async function deleteDelivery(entryId) {
    const entry = state.deliveries.find((row) => row.id === entryId);
    if (!entry) return;
    if (!confirm(`Excluir o turno de ${entry.courier_name} em ${formatDate(entry.date)}?`)) return;
    await send(`/api/deliveries/${entryId}`, "DELETE", null, "Turno excluído.");
  }



  /* Reordenar por setas, nao por arrastar: no celular — que e onde a loja usa
   * o sistema — arrastar erra mais do que acerta. A ordem define como as
   * opcoes aparecem no campo de escolha. */
  async function moveCatalogItem(catalogId, indice, passo) {
    const lista = state.catalogs.find((row) => row.id === catalogId);
    if (!lista) return;
    const valores = lista.items.map((item) => item.value);
    const destino = indice + passo;
    if (destino < 0 || destino >= valores.length) return;
    [valores[indice], valores[destino]] = [valores[destino], valores[indice]];
    try {
      const atualizada = await api(`/api/catalogs/${catalogId}/order`, {
        method: "PATCH", body: JSON.stringify({ items: valores }),
      });
      state.catalogs = state.catalogs.map((row) => (row.id === catalogId ? atualizada : row));
      renderCatalogs();
      populateCatalogSelects();
    } catch (error) {
      toast(error.message || "Não foi possível reordenar a lista.", "error");
    }
  }

  // --------------------------------------------------------------- listas


  function openCatalogItem(catalogId, value) {
    const lista = state.catalogs.find((row) => row.id === catalogId);
    if (!lista) return;
    const form = document.getElementById("catalog-form");
    form.reset();
    form.elements.catalog_id.value = catalogId;
    form.elements.original.value = value || "";
    form.elements.value.value = value || "";
    setText("#catalog-modal-list", lista.name);
    setText("#catalog-modal-title", value ? `Editar "${value}"` : "Novo item");

    const item = value ? lista.items.find((row) => row.value === value) : null;
    const usos = item ? item.usage : 0;
    document.getElementById("catalog-delete").hidden = !value;
    document.getElementById("catalog-hint").textContent = !value
      ? lista.description
      : usos
        ? `${usos} registro(s) usam este item. Renomear leva todos junto; excluir não é possível enquanto houver uso.`
        : "Nenhum registro usa este item, então ele pode ser excluído com segurança.";
    openDialog("catalog-modal");
  }

  async function submitCatalogItem(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const catalogId = form.get("catalog_id");
    const original = form.get("original");
    const valor = String(form.get("value") || "").trim();
    if (!valor) return toast("Informe um nome para o item.", "error");
    if (original) {
      if (valor === original) return closeModal();
      await submitData(`/api/catalogs/${catalogId}/items`, { value: original, new_value: valor },
        "Item renomeado — os registros que o usavam acompanharam.", "PATCH");
      return;
    }
    await submitData(`/api/catalogs/${catalogId}/items`, { value: valor }, "Item adicionado à lista.");
  }

  async function deleteCatalogItem() {
    const form = document.getElementById("catalog-form");
    const catalogId = form.elements.catalog_id.value;
    const valor = form.elements.original.value;
    if (!catalogId || !valor) return;
    if (!confirm(`Excluir "${valor}" da lista?`)) return;
    await send(`/api/catalogs/${catalogId}/items/${encodeURIComponent(valor)}`, "DELETE", null, "Item removido da lista.");
  }

  // ------------------------------------------------------ pedido importado

  function toggleImportDelivery() {
    const entrega = document.getElementById("import-is-delivery").checked;
    document.querySelectorAll(".import-delivery-field").forEach((campo) => { campo.hidden = !entrega; });
  }

  /* Pedido importado não traz os itens vendidos, então a tela de venda não
   * serve: ela exige produtos. Aqui só o que a origem informou é editável —
   * incluindo frete e entregador, que o relatório da Anota Aí traz. */
  function openImportedOrder(order) {
    const form = document.getElementById("import-form");
    form.reset();
    populateCourierSelects();
    populateCatalogSelects();
    populateChannelSelect();
    const canal = document.getElementById("import-channel");
    canal.innerHTML = state.channels.filter((row) => row.active)
      .map((row) => `<option value="${escapeHtml(row.name)}">${escapeHtml(row.name)}</option>`).join("");
    const entregador = document.getElementById("import-courier");
    entregador.innerHTML = `<option value="">Sem entregador definido</option>${
      state.couriers.filter((row) => row.active).map((row) => `<option value="${row.id}">${escapeHtml(row.name)}</option>`).join("")}`;

    form.elements.order_id.value = order.id;
    form.elements.total.value = order.total;
    setSelectValue(form.elements.channel, order.channel);
    setSelectValue(form.elements.payment_method, order.payment_method);
    form.elements.occurred_at.value = toLocalInput(order.created_at);
    form.elements.customer_name.value = order.customer_name || "";
    form.elements.is_delivery.checked = Boolean(order.is_delivery);
    toggleImportDelivery();
    form.elements.delivery_fee.value = order.delivery_fee || 0;
    setSelectValue(form.elements.courier_id, order.courier_id || "");
    setText("#import-modal-title", `Corrigir pedido #${order.number}`);
    openDialog("import-modal");
  }

  async function submitImportedOrder(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const entrega = form.get("is_delivery") === "on";
    const payload = {
      total: Number(form.get("total") || 0),
      channel: form.get("channel"),
      payment_method: form.get("payment_method"),
      customer_name: form.get("customer_name") || null,
      is_delivery: entrega,
      delivery_fee: entrega ? Number(form.get("delivery_fee") || 0) : 0,
      courier_id: entrega ? (form.get("courier_id") || "") : "",
    };
    const quando = form.get("occurred_at");
    if (quando) payload.occurred_at = quando;
    await submitData(`/api/orders/${form.get("order_id")}`, payload, "Pedido corrigido.", "PATCH");
  }

  // ------------------------------------------------------------- arquivos

  async function deleteMedia() {
    const record = state.media.find((row) => row.id === state.selectedMediaId);
    if (!record) return;
    const aviso = record.source === "upload"
      ? "O arquivo sai do acervo e a cópia do sistema é apagada."
      : "O arquivo sai do acervo e não volta na próxima sincronização. O original na pasta de origem não é apagado.";
    if (!confirm(`Excluir "${record.title}"?\n\n${aviso}`)) return;
    await send(`/api/media/${record.id}`, "DELETE", null, "Arquivo removido do acervo.");
  }

  // -------------------------------------------------------------- suporte



  /* Volta o modal ao estado de cadastro novo.
   * Sem isso, abrir "Nova venda" logo depois de editar um pedido reenviaria o
   * id do pedido anterior e sobrescreveria uma venda que já estava certa. */
  function resetCreateMode(name) {
    if (name === "sale") {
      document.getElementById("sale-order-id").value = "";
      document.getElementById("sale-occurred").value = "";
      document.getElementById("sale-is-delivery").checked = false;
      toggleSaleDelivery();
      setText("#sale-modal-title", "Registrar venda");
      setText("#submit-sale span", "Concluir venda");
      populateChannelSelect();
    }
    if (name === "expense") {
      document.getElementById("expense-id").value = "";
      setText("#expense-modal-title", "Lançar despesa");
    }
    if (name === "production-recipe") {
      document.getElementById("production-recipe-form").reset();
      document.getElementById("production-recipe-id").value = "";
      document.getElementById("production-recipe-delete").hidden = true;
      document.getElementById("production-recipe-lines").innerHTML = "";
      document.getElementById("production-recipe-output").innerHTML = inventoryOptions();
      addProductionLine("production-recipe-lines");
      setText("#production-recipe-modal-title", "Nova ficha de batida");
    }
    if (name === "market-price") {
      document.getElementById("market-price-form").reset();
      document.getElementById("market-price-id").value = "";
      document.getElementById("market-price-delete").hidden = true;
      document.querySelector('#market-price-form [name="observed_on"]').value = new Date().toISOString().slice(0, 10);
      populateMarketProducts();
      setText("#market-price-modal-title", "Preço de concorrente");
    }
    if (name === "sport-anchor") {
      document.getElementById("sport-anchor-form").reset();
      document.getElementById("sport-anchor-id").value = "";
      document.querySelectorAll("#sport-anchor-weekdays input").forEach((campo) => { campo.checked = false; });
      document.getElementById("sport-anchor-delete").hidden = true;
      setText("#sport-anchor-modal-title", "Âncora esportiva");
    }
    if (name === "inventory") {
      document.getElementById("inventory-form").reset();
      document.getElementById("inventory-id").value = "";
      const quantityField = document.getElementById("inventory-quantity-field");
      quantityField.hidden = false;
      document.getElementById("inventory-form").elements.quantity.required = true;
      refreshInventoryPack();
      setText("#inventory-modal-title", "Novo insumo");
    }
    if (name === "product") {
      document.getElementById("product-id").value = "";
      setText("#product-modal-title", "Novo produto");
    }
    if (name === "addon") {
      document.getElementById("addon-form").reset();
      document.getElementById("addon-id").value = "";
      document.getElementById("addon-form").elements.active.checked = true;
      document.getElementById("addon-delete").hidden = true;
      setText("#addon-modal-title", "Novo complemento");
    }
    if (name === "channel") {
      document.getElementById("channel-form").reset();
      document.getElementById("channel-id").value = "";
      document.getElementById("channel-form").elements.active.checked = true;
      document.getElementById("channel-delete").hidden = true;
      setText("#channel-modal-title", "Novo canal");
    }
    if (name === "courier") {
      document.getElementById("courier-form").reset();
      document.getElementById("courier-id").value = "";
      document.getElementById("courier-form").elements.active.checked = true;
      document.getElementById("courier-delete").hidden = true;
      setText("#courier-modal-title", "Novo entregador");
    }
    if (name === "delivery") openDelivery(null);
    if (name === "count") openCount(state.inventory[0]?.id);
    if (name === "simulate" && !document.querySelector("#simulate-recipe-lines .recipe-line")) addSimulateLine();
  }

  async function send(path, method, payload, successMessage) {
    try {
      await api(path, { method, ...(payload ? { body: JSON.stringify(payload) } : {}) });
      closeModal();
      toast(successMessage, "success");
      await loadAll();
    } catch (error) {
      toast(error.message || "Não foi possível concluir a operação.", "error");
    }
  }

  function setSelectValue(select, value) {
    if (!select) return;
    if (![...select.options].some((option) => option.value === value)) {
      select.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`);
    }
    select.value = value;
  }

  /* datetime-local espera hora local sem fuso; o pedido chega em UTC. */
  function toLocalInput(iso) {
    const date = new Date(iso);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }

  function afterRender() {
    // Reaplica o estado de entrega: populateCourierSelects reescreve o select.
    toggleSaleDelivery();
    const countSelect = document.getElementById("count-inventory");
    if (countSelect && !countSelect.options.length && state.inventory.length) {
      countSelect.innerHTML = state.inventory.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("");
    }
  }

  window.sdmManage = { afterRender };
})();
