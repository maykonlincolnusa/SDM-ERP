"""A tela da retirada: escolher o tamanho e digitar a quantidade.

O servidor da retirada já tinha rede de proteção (`test_withdrawals.py`). A tela
não tinha, e era nela que o lançamento morria: o campo só aceitava a unidade de
estoque, então tirar três barras exigia dividir 8,4 por 2,8 de cabeça; a
vírgula do teclado brasileiro esvaziava o campo de `type="number"`; e a linha
começada sem quantidade era descartada em silêncio, devolvendo ao sistema um
saldo maior que o da prateleira — exatamente o que este registro existe para
evitar.

Os testes com Node executam os helpers de produção, sem navegador. Os demais
conferem a ligação entre HTML, CSS e JS.
"""

import re
from pathlib import Path

import pytest

from test_frontend_resilience import run_js

ESTATICO = Path(__file__).resolve().parents[1] / "app" / "static"
APP_JS = (ESTATICO / "app.js").read_text(encoding="utf-8")
CSS = (ESTATICO / "styles.css").read_text(encoding="utf-8")
HTML = (ESTATICO / "index.html").read_text(encoding="utf-8")


# Um estoque de teste com as duas situações que a linha precisa distinguir: o
# insumo que a equipe conta em barra e o que ela conta na própria unidade.
ITENS = """
  global.state = { inventory: [
    { id: "acai", name: "Base de açaí", unit: "kg", quantity: 8.4, unit_cost: 22.5, pack_unit: "barra", pack_size: 2.8 },
    { id: "granola", name: "Granola", unit: "kg", quantity: 1.4, unit_cost: 27 },
  ] };
"""

# O mínimo de DOM para os helpers rodarem sem navegador, mais formatadores que
# não dependem do ICU do Node: as asserções falam do número, não do idioma.
DOM = """
  global.escapeHtml = (valor) => String(valor ?? "");
  global.number = { format: (valor) => String(valor) };
  global.currency = { format: (valor) => "R$ " + valor.toFixed(2) };
  function campo(valor) { return { value: valor, classList: { marcas: new Set(), toggle(nome, ligado) { if (ligado) this.marcas.add(nome); else this.marcas.delete(nome); }, tem(nome) { return this.marcas.has(nome); } } }; }
  function linhaFalsa(inventoryId, digitado, unidade) {
    const partes = {
      "[data-withdrawal-item]": { value: inventoryId },
      "[data-withdrawal-quantity]": campo(digitado),
      "[data-withdrawal-unit]": { value: unidade ? unidade.value : "", innerHTML: "", dataset: unidade && unidade.chosen ? { chosen: "1" } : {} },
      "[data-withdrawal-hint]": { textContent: "", className: "" },
    };
    return { querySelector: (seletor) => partes[seletor], partes };
  }
  function montarTela(linhas) {
    const preview = { textContent: "", className: "" };
    const override = { checked: false };
    const overrideField = { hidden: true };
    const elementos = { "withdrawal-preview": preview, "withdrawal-override": override, "withdrawal-override-field": overrideField };
    global.document = { querySelectorAll: () => linhas, getElementById: (id) => elementos[id] };
    return { preview, override, overrideField };
  }
"""

ESTOQUE = ITENS + DOM


# ----------------------------------------------------------------------
# A quantidade digitada
# ----------------------------------------------------------------------


def test_a_virgula_do_teclado_brasileiro_e_um_numero_valido():
    """`type="number"` devolvia campo vazio para "0,5", e o operador desistia."""

    run_js(["parseQuantity"], """
      assert.equal(parseQuantity("0,5"), 0.5);
      assert.equal(parseQuantity("0.5"), 0.5);
      assert.equal(parseQuantity(" 3 "), 3);
      assert.equal(parseQuantity(",5"), 0.5);
      // Separador solto e o estado normal de quem esta digitando "0,5": recusa-lo
      // faria a dica piscar "invalido" no meio de um numero certo.
      assert.equal(parseQuantity("0,"), 0);
      assert.ok(Number.isNaN(parseQuantity("")));
      assert.ok(Number.isNaN(parseQuantity("meio quilo")));
      assert.ok(Number.isNaN(parseQuantity("-2")));
      assert.ok(Number.isNaN(parseQuantity("1,2,3")));
      assert.ok(Number.isNaN(parseQuantity("1.234,5")));
    """)


def test_a_soma_das_linhas_e_cortada_na_precisao_do_estoque():
    run_js(["roundQuantity"], """
      assert.equal(roundQuantity(0.1 + 0.2), 0.3);
      assert.equal(roundQuantity(3 * 2.8), 8.4);
      assert.equal(roundQuantity(1.23456), 1.235);
    """)


# ----------------------------------------------------------------------
# O tamanho
# ----------------------------------------------------------------------


def test_a_linha_abre_no_tamanho_que_a_equipe_usa_na_prateleira():
    """Quem tira três barras não deveria precisar dividir 8,4 por 2,8."""

    run_js(["syncWithdrawalUnit", "packOf"], ESTOQUE + """
      const linha = linhaFalsa("acai", "");
      const resultado = syncWithdrawalUnit(linha);
      assert.equal(linha.partes["[data-withdrawal-unit]"].value, "pack");
      assert.equal(resultado.usingPack, true);
      assert.match(linha.partes["[data-withdrawal-unit]"].innerHTML, /barra/);
      // O insumo sem unidade de manuseio nao ganha uma opcao inventada.
      const simples = linhaFalsa("granola", "");
      assert.equal(syncWithdrawalUnit(simples).usingPack, false);
      assert.equal(simples.partes["[data-withdrawal-unit]"].value, "stock");
      assert.doesNotMatch(simples.partes["[data-withdrawal-unit]"].innerHTML, /barra/);
    """)


def test_a_unidade_de_manuseio_e_escrita_no_plural():
    """"3 barra" trai a tela: quem lê desconfia do resto do número."""

    run_js(["packAmount", "inventoryOptionLabel", "packOf", "roundQuantity"], ESTOQUE + """
      assert.equal(packAmount({ unit: "barra", size: 2.8 }, 1), "1 barra");
      assert.equal(packAmount({ unit: "barra", size: 2.8 }, 3), "3 barras");
      // A opcao mostra as duas leituras: a da prateleira e a do estoque.
      assert.ok(inventoryOptionLabel(state.inventory[0]).includes("3 barras (8.4 kg)"));
      assert.ok(inventoryOptionLabel(state.inventory[1]).includes("1.4 kg em estoque"));
    """)


def test_o_tamanho_escolhido_de_proposito_sobrevive_a_troca_de_insumo():
    run_js(["syncWithdrawalUnit", "packOf"], ESTOQUE + """
      // Escolheu quilo de proposito: trocar o insumo nao pode devolver a barra.
      const linha = linhaFalsa("acai", "2", { value: "stock", chosen: true });
      assert.equal(syncWithdrawalUnit(linha).usingPack, false);
      // Escolheu barra, e o insumo novo nao tem barra: so resta o estoque.
      const outra = linhaFalsa("granola", "2", { value: "pack", chosen: true });
      assert.equal(syncWithdrawalUnit(outra).usingPack, false);
      assert.equal(outra.partes["[data-withdrawal-unit]"].value, "stock");
    """)


def test_a_lista_de_tamanhos_so_e_remontada_quando_o_insumo_muda():
    """Refazer o `select` a cada tecla fecharia a lista aberta pelo operador."""

    run_js(["syncWithdrawalUnit", "packOf"], ESTOQUE + """
      const linha = linhaFalsa("acai", "");
      syncWithdrawalUnit(linha);
      const unidade = linha.partes["[data-withdrawal-unit]"];
      unidade.innerHTML = "MARCA";
      syncWithdrawalUnit(linha);
      assert.equal(unidade.innerHTML, "MARCA");
      linha.partes["[data-withdrawal-item]"].value = "granola";
      syncWithdrawalUnit(linha);
      assert.notEqual(unidade.innerHTML, "MARCA");
    """)


def test_a_quantidade_em_barra_chega_ao_servidor_em_quilo():
    """O servidor só entende a unidade de estoque; a conversão é da tela."""

    run_js(["readWithdrawalLines", "withdrawalLines", "readWithdrawalLine", "syncWithdrawalUnit", "packOf", "parseQuantity", "roundQuantity"], ESTOQUE + """
      const barras = linhaFalsa("acai", "3");
      const quilos = linhaFalsa("granola", "0,5");
      montarTela([barras, quilos]);
      assert.deepEqual(readWithdrawalLines(), [
        { inventory_id: "acai", quantity: 8.4 },
        { inventory_id: "granola", quantity: 0.5 },
      ]);
    """)


# ----------------------------------------------------------------------
# A conferencia antes de gravar
# ----------------------------------------------------------------------


def test_a_previa_mostra_a_conversao_e_o_que_sobra_na_propria_linha():
    run_js(["refreshWithdrawalPreview", "describeWithdrawalLine", "packAmount", "withdrawalLines", "readWithdrawalLine", "syncWithdrawalUnit", "packOf", "parseQuantity", "roundQuantity"], ESTOQUE + """
      const linha = linhaFalsa("acai", "2");
      const tela = montarTela([linha]);
      refreshWithdrawalPreview();
      const dica = linha.partes["[data-withdrawal-hint]"].textContent;
      assert.match(dica, /2 barras = 5.6 kg/);
      assert.match(dica, /sobram 2.8 kg/);
      assert.match(tela.preview.textContent, /1 insumo/);
      assert.equal(tela.preview.className, "modal-result full positive");
      assert.equal(tela.overrideField.hidden, true);
    """)


def test_a_linha_sem_saldo_e_apontada_em_vez_de_so_recusada():
    run_js(["refreshWithdrawalPreview", "describeWithdrawalLine", "packAmount", "withdrawalLines", "readWithdrawalLine", "syncWithdrawalUnit", "packOf", "parseQuantity", "roundQuantity"], ESTOQUE + """
      const boa = linhaFalsa("acai", "1");
      const estourada = linhaFalsa("granola", "9");
      const tela = montarTela([boa, estourada]);
      refreshWithdrawalPreview();
      assert.equal(estourada.partes["[data-withdrawal-quantity]"].classList.tem("over-balance"), true);
      assert.equal(boa.partes["[data-withdrawal-quantity]"].classList.tem("over-balance"), false);
      assert.match(estourada.partes["[data-withdrawal-hint]"].textContent, /faltam 7.6 kg/);
      // A confirmacao de saldo negativo so aparece quando ha saldo estourado.
      assert.equal(tela.overrideField.hidden, false);
      assert.match(tela.preview.textContent, /Sem saldo para Granola/);
    """)


def test_o_mesmo_insumo_em_duas_linhas_soma_antes_da_conferencia():
    """Duas linhas de 1 kg com saldo de 1,4 kg pedem 2 kg, como no servidor."""

    run_js(["refreshWithdrawalPreview", "describeWithdrawalLine", "packAmount", "withdrawalLines", "readWithdrawalLine", "syncWithdrawalUnit", "packOf", "parseQuantity", "roundQuantity"], ESTOQUE + """
      const primeira = linhaFalsa("granola", "1");
      const segunda = linhaFalsa("granola", "1");
      const tela = montarTela([primeira, segunda]);
      refreshWithdrawalPreview();
      assert.equal(tela.overrideField.hidden, false);
      assert.match(primeira.partes["[data-withdrawal-hint]"].textContent, /mais de uma linha/);
      assert.match(tela.preview.textContent, /a retirada pede 2/);
    """)


def test_quantidade_invalida_diz_o_que_fazer_em_vez_de_sumir():
    run_js(["refreshWithdrawalPreview", "describeWithdrawalLine", "packAmount", "withdrawalLines", "readWithdrawalLine", "syncWithdrawalUnit", "packOf", "parseQuantity", "roundQuantity"], ESTOQUE + """
      const linha = linhaFalsa("granola", "meio quilo");
      const tela = montarTela([linha]);
      refreshWithdrawalPreview();
      assert.match(linha.partes["[data-withdrawal-hint]"].textContent, /inválida/);
      assert.equal(linha.partes["[data-withdrawal-quantity]"].classList.tem("over-balance"), true);
      assert.equal(tela.preview.className, "modal-result full negative");
    """)


def test_apagar_a_quantidade_revoga_a_autorizacao_de_saldo_negativo():
    """Autorização esquecida em tela vazia gravaria a próxima retirada no negativo."""

    run_js(["refreshWithdrawalPreview", "describeWithdrawalLine", "packAmount", "withdrawalLines", "readWithdrawalLine", "syncWithdrawalUnit", "packOf", "parseQuantity", "roundQuantity"], ESTOQUE + """
      const linha = linhaFalsa("granola", "");
      const tela = montarTela([linha]);
      tela.override.checked = true;
      tela.overrideField.hidden = false;
      refreshWithdrawalPreview();
      assert.equal(tela.override.checked, false);
      assert.equal(tela.overrideField.hidden, true);
    """)


def test_a_soma_fracionada_nao_acusa_falta_de_saldo_que_nao_existe():
    """0,1 + 0,2 em ponto flutuante passa de 0,3 e recusaria uma retirada válida."""

    saldo_exato = """
      global.state = { inventory: [{ id: "item", name: "Insumo", unit: "kg", quantity: 0.3, unit_cost: 10 }] };
    """
    run_js(["refreshWithdrawalPreview", "describeWithdrawalLine", "packAmount", "withdrawalLines", "readWithdrawalLine", "syncWithdrawalUnit", "packOf", "parseQuantity", "roundQuantity"], saldo_exato + DOM + """
      const tela = montarTela([linhaFalsa("item", "0,1"), linhaFalsa("item", "0,2")]);
      refreshWithdrawalPreview();
      assert.equal(tela.overrideField.hidden, true);
      assert.equal(tela.preview.className, "modal-result full positive");
    """)


def test_a_autorizacao_de_saldo_negativo_e_revogada_quando_a_falta_some():
    run_js(["refreshWithdrawalPreview", "describeWithdrawalLine", "packAmount", "withdrawalLines", "readWithdrawalLine", "syncWithdrawalUnit", "packOf", "parseQuantity", "roundQuantity"], ESTOQUE + """
      const linha = linhaFalsa("granola", "9");
      const tela = montarTela([linha]);
      refreshWithdrawalPreview();
      tela.override.checked = true;
      linha.partes["[data-withdrawal-quantity]"].value = "1";
      refreshWithdrawalPreview();
      assert.equal(tela.override.checked, false);
      assert.equal(tela.overrideField.hidden, true);
    """)


# ----------------------------------------------------------------------
# O envio
# ----------------------------------------------------------------------


def test_linha_sem_quantidade_barra_o_envio_em_vez_de_ser_descartada():
    """Descartar em silêncio devolveria um saldo maior que o da prateleira."""

    run_js(["submitWithdrawal", "withdrawalLines", "readWithdrawalLines", "readWithdrawalLine", "syncWithdrawalUnit", "packOf", "parseQuantity", "roundQuantity"], ESTOQUE + """
      const preenchida = linhaFalsa("acai", "1");
      const vazia = linhaFalsa("granola", "");
      montarTela([preenchida, vazia]);
      let focada = 0, enviado = null;
      vazia.partes["[data-withdrawal-quantity]"].focus = () => { focada++; };
      const mensagens = [];
      global.refreshWithdrawalPreview = () => {};
      global.toast = (texto, tipo) => mensagens.push({ texto, tipo });
      global.submitData = async (caminho, payload) => { enviado = payload; };
      global.FormData = class { get(campo) { return { responsible: "Maria", reason: "Produção da manhã", note: "" }[campo]; } };
      await submitWithdrawal({ preventDefault() {}, currentTarget: {} });
      assert.equal(enviado, null);
      assert.equal(focada, 1);
      assert.equal(mensagens.at(-1).tipo, "error");
    """)


def test_o_envio_leva_a_quantidade_convertida_e_a_confirmacao_de_saldo():
    run_js(["submitWithdrawal", "withdrawalLines", "readWithdrawalLines", "readWithdrawalLine", "syncWithdrawalUnit", "packOf", "parseQuantity", "roundQuantity"], ESTOQUE + """
      const linha = linhaFalsa("acai", "3");
      const tela = montarTela([linha]);
      tela.override.checked = true;
      let enviado = null;
      global.refreshWithdrawalPreview = () => {};
      global.toast = () => {};
      global.submitData = async (caminho, payload) => { enviado = { caminho, payload }; };
      global.FormData = class { get(campo) { return { responsible: "Maria", reason: "Produção da manhã", note: "" }[campo]; } };
      await submitWithdrawal({ preventDefault() {}, currentTarget: {} });
      assert.equal(enviado.caminho, "/api/inventory/withdrawals");
      assert.deepEqual(enviado.payload.items, [{ inventory_id: "acai", quantity: 8.4 }]);
      assert.equal(enviado.payload.responsible, "Maria");
      assert.equal(enviado.payload.allow_negative, true);
      assert.equal(enviado.payload.note, null);
    """)


# ----------------------------------------------------------------------
# Ligacao com o HTML e o CSS
# ----------------------------------------------------------------------


def test_a_linha_da_retirada_tem_campo_de_tamanho_e_faixa_de_dica():
    linha = re.search(r'insertAdjacentHTML\("beforeend", `<div class="recipe-line withdrawal-line".*?`\);', APP_JS, re.S)
    assert linha, "a linha da retirada mudou de forma; o teste precisa ser reescrito"
    marcacao = linha.group()
    for atributo in ("data-withdrawal-item", "data-withdrawal-quantity", "data-withdrawal-unit", "data-withdrawal-hint"):
        assert atributo in marcacao, f"a linha da retirada perdeu {atributo}"
    # O campo e de texto justamente para a virgula chegar ao parseQuantity.
    assert 'type="text" inputmode="decimal"' in marcacao


def test_o_tamanho_e_a_dica_da_retirada_tem_estilo():
    for classe in (".withdrawal-line", ".withdrawal-hint", ".over-balance"):
        assert classe in CSS, f"{classe} não tem estilo, e o operador não veria a diferença"
    # No celular quantidade e tamanho dividem a mesma linha, abaixo do insumo.
    mobile = re.search(r"@media \(max-width: 480px\) \{.*?\n\}", CSS, re.S)
    assert mobile and ".withdrawal-line" in mobile.group()


def test_a_conversao_da_fabricacao_e_reescrita_quando_o_insumo_muda():
    """A dica ficava congelada e anunciava a barra do item anterior."""

    assert "describeProductionLine" in APP_JS
    assert 'addEventListener("change", () => describeProductionLine(linha))' in APP_JS


def test_o_modal_avisa_que_a_conversao_e_feita_na_tela():
    modal = re.search(r'<dialog class="modal" id="withdrawal-modal".*?</dialog>', HTML, re.S)
    assert modal and "tamanho que a equipe usa" in modal.group()


def test_desfazer_mostra_a_retirada_na_unidade_em_que_ela_foi_lancada():
    """Quem lançou "3 barras" precisa reconhecer o lançamento na confirmação."""

    manage = (ESTATICO / "manage.js").read_text(encoding="utf-8")
    resumo = re.search(r"async function undoWithdrawal.*?\n  \}", manage, re.S)
    assert resumo, "undoWithdrawal mudou de forma; o teste precisa ser reescrito"
    assert "packOf(state.inventory.find" in resumo.group()
    assert "${pack.unit}" in resumo.group()


def test_a_retirada_nao_abre_sem_insumo_cadastrado():
    """Lista vazia num campo obrigatório é um formulário que só pode ser recusado."""

    run_js(["openWithdrawal"], """
      global.state = { inventory: [] };
      const mensagens = [];
      global.toast = (texto, tipo) => mensagens.push({ texto, tipo });
      let abriu = false;
      global.openDialog = () => { abriu = true; };
      global.document = { getElementById: () => { throw new Error("não deveria montar o formulário"); } };
      openWithdrawal();
      assert.equal(abriu, false);
      assert.equal(mensagens.at(-1).tipo, "error");
      assert.match(mensagens.at(-1).texto, /Cadastre um insumo/);
    """)
