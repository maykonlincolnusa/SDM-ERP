"""A ligação entre HTML, JS e CSS.

Não existe navegador nos testes, e por isso a tela era a única parte do sistema
sem rede de proteção. Estes testes não provam que a tela *parece* certa — provam
que ela está *ligada*: que o id procurado pelo JS existe no HTML, que o botão
abre um modal que existe, que a classe usada no template tem estilo, e que
nenhum arquivo chama uma função que mora no escopo fechado do outro.

O último caso não é hipotético. `openDialog` vivia dentro da IIFE de manage.js e
era chamado de app.js, onde não existia: o botão "Registrar compra" quebrava com
ReferenceError e nenhum teste via. É esse tipo de erro que esta suíte pega.
"""

import re
from pathlib import Path

import pytest

ESTATICO = Path(__file__).resolve().parent.parent / "app" / "static"
HTML = (ESTATICO / "index.html").read_text(encoding="utf-8")
APP_JS = (ESTATICO / "app.js").read_text(encoding="utf-8")
MANAGE_JS = (ESTATICO / "manage.js").read_text(encoding="utf-8")
CSS = (ESTATICO / "styles.css").read_text(encoding="utf-8")

IDS_DO_HTML = set(re.findall(r'id="([^"]+)"', HTML))
CLASSES_DO_CSS = set(re.findall(r"\.([a-zA-Z][\w-]*)", CSS))

# Classes que existem sem estilo de propósito: são gancho de seletor para o JS,
# não aparência. Manter a lista curta e explícita é o que impede o teste de
# virar carimbo — cada nome aqui é uma decisão, não uma exceção esquecida.
CLASSES_EXTERNAS = {
    "lucide",
    "data-form",             # marca os formulários que o JS intercepta no submit
    "expense-repeat-field",  # alvo do toggle de despesa recorrente
    "insight-item",
}

FONTES = {"app.js": APP_JS, "manage.js": MANAGE_JS}


def _ids_procurados(js: str) -> set[str]:
    achados = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    achados |= set(re.findall(r'(?:setText|querySelector|querySelectorAll)\("#([a-zA-Z0-9_-]+)"', js))
    return achados


def _classes_usadas(js: str) -> set[str]:
    """Só as listas de classe totalmente estáticas.

    Onde há interpolação, a classe é escolhida em runtime e tentar recortar a
    parte fixa produz lixo de ternário — `?`, `>=`, `0` — que faria o teste
    gritar sem apontar nada. Meia checagem confiável vale mais do que uma
    completa que ninguém acredita.
    """

    usadas: set[str] = set()
    blocos = re.findall(r'class="([^"$]*)"', js)
    blocos += re.findall(r'className = "([^"$]*)"', js)
    for bloco in blocos:
        usadas.update(parte for parte in bloco.split() if parte)
    return usadas


# ----------------------------------------------------------------------
# HTML x JS
# ----------------------------------------------------------------------


@pytest.mark.parametrize("nome", sorted(FONTES))
def test_todo_id_procurado_pelo_js_existe_no_html(nome):
    faltando = sorted(_ids_procurados(FONTES[nome]) - IDS_DO_HTML)

    assert not faltando, f"{nome} procura ids que não existem no index.html: {faltando}"


def test_todo_botao_de_modal_aponta_para_um_dialog_existente():
    """`data-modal="x"` abre `#x-modal`: sem o dialog, o clique não faz nada."""

    alvos = set(re.findall(r'data-modal="([^"]+)"', HTML))
    faltando = sorted(alvo for alvo in alvos if f"{alvo}-modal" not in IDS_DO_HTML)

    assert not faltando, f"Botões apontam para modais inexistentes: {faltando}"


def test_todo_item_de_menu_aponta_para_uma_tela_existente():
    alvos = set(re.findall(r'data-view-target="([^"]+)"', HTML))
    faltando = sorted(alvo for alvo in alvos if alvo not in IDS_DO_HTML)

    assert not faltando, f"Menu aponta para telas inexistentes: {faltando}"


def test_toda_tela_tem_item_de_menu():
    """Tela sem item de menu é código morto: ninguém chega nela."""

    telas = set(re.findall(r'<section class="view" id="([^"]+)"', HTML))
    alvos = set(re.findall(r'data-view-target="([^"]+)"', HTML))
    orfas = sorted(telas - alvos)

    assert not orfas, f"Telas sem entrada no menu: {orfas}"


# ----------------------------------------------------------------------
# Escopo: o erro que já aconteceu
# ----------------------------------------------------------------------


def test_app_js_nao_chama_funcao_privada_de_manage_js():
    """manage.js roda dentro de uma IIFE; o que ele define não existe em app.js.

    Foi assim que o botão "Registrar compra" quebrou: app.js chamava openDialog,
    que só existia no escopo fechado de manage.js.
    """

    privadas = set(re.findall(r"^  function ([a-zA-Z_$][\w$]*)\s*\(", MANAGE_JS, re.MULTILINE))
    privadas |= set(re.findall(r"^  async function ([a-zA-Z_$][\w$]*)\s*\(", MANAGE_JS, re.MULTILINE))
    globais_de_app = set(re.findall(r"^(?:async )?function ([a-zA-Z_$][\w$]*)\s*\(", APP_JS, re.MULTILINE))

    vazadas = sorted(
        nome for nome in privadas
        if nome not in globais_de_app and re.search(rf"(?<![.\w]){re.escape(nome)}\s*\(", APP_JS)
    )

    assert not vazadas, (
        "app.js chama funções que só existem dentro da IIFE de manage.js "
        f"e vai quebrar com ReferenceError: {vazadas}"
    )


def test_manage_js_so_depende_de_globais_que_app_js_realmente_define():
    """O cabeçalho de manage.js declara a dependência; ela precisa ser verdade."""

    for nome in ("state", "api", "toast", "loadAll", "escapeHtml", "currency", "number", "refreshIcons", "setText"):
        assert re.search(rf"^(?:const|let|var|function|async function) {re.escape(nome)}\b", APP_JS, re.MULTILINE), (
            f"manage.js depende de {nome}, que app.js não define no escopo global"
        )


# ----------------------------------------------------------------------
# Ações por delegação
# ----------------------------------------------------------------------


def test_toda_acao_registrada_e_emitida_em_algum_lugar():
    """Ação em ACTIONS que ninguém emite é código morto; o contrário é botão inerte."""

    acoes = set(re.findall(r'^    "([a-z-]+)": \(id\)', MANAGE_JS, re.MULTILINE))
    emitidas = set()
    for fonte in (APP_JS, MANAGE_JS, HTML):
        emitidas |= set(re.findall(r"data-([a-z-]+)=", fonte))

    orfas = sorted(acoes - emitidas)

    assert not orfas, f"Ações registradas que nenhum botão emite: {orfas}"


def test_todo_botao_de_acao_tem_handler_registrado():
    """Botão emitido sem handler é botão inerte: clica e não acontece nada."""

    acoes = set(re.findall(r'^    "([a-z-]+)": \(id\)', MANAGE_JS, re.MULTILINE))
    # Alguns data-* têm ramo próprio antes do laço de ACTIONS, porque precisam
    # de mais de um atributo para resolver o alvo.
    com_ramo_proprio = set(re.findall(r'closest\("\[data-([a-z-]+)\]"\)', MANAGE_JS))
    emitidas = set(re.findall(r'data-(edit-[a-z-]+|delete-[a-z-]+|undo-[a-z-]+)="\$\{', APP_JS))
    sem_handler = sorted(emitidas - acoes - com_ramo_proprio)

    assert not sem_handler, f"Botões emitidos sem handler em ACTIONS: {sem_handler}"


# ----------------------------------------------------------------------
# CSS
# ----------------------------------------------------------------------


@pytest.mark.parametrize("nome", sorted(FONTES))
def test_toda_classe_usada_no_js_tem_estilo(nome):
    """Classe sem estilo é elemento invisível ou sem forma — o bug que não dá erro."""

    faltando = sorted(_classes_usadas(FONTES[nome]) - CLASSES_DO_CSS - CLASSES_EXTERNAS)

    assert not faltando, f"{nome} usa classes que não existem no styles.css: {faltando}"


def test_toda_classe_do_html_tem_estilo():
    usadas: set[str] = set()
    for bloco in re.findall(r'class="([^"]*)"', HTML):
        usadas.update(parte for parte in bloco.split() if parte)
    faltando = sorted(usadas - CLASSES_DO_CSS - CLASSES_EXTERNAS)

    assert not faltando, f"index.html usa classes sem estilo: {faltando}"


# ----------------------------------------------------------------------
# Conversoes que existem para poupar conta de cabeca
# ----------------------------------------------------------------------


def test_o_cadastro_de_insumo_pede_o_custo_nas_duas_leituras():
    """Quem compra sabe o preco da barra; a ficha tecnica precisa do quilo."""

    for campo in ("inventory-pack-unit", "inventory-pack-size", "inventory-pack-cost", "inventory-unit-cost"):
        assert campo in IDS_DO_HTML, f"o cadastro de insumo perdeu o campo {campo}"
    assert 'name="pack_cost"' in HTML
    # O custo por embalagem e so da tela: o servidor guarda o custo por unidade.
    assert "delete payload.pack_cost;" in APP_JS


def test_a_conversao_do_insumo_e_refeita_quando_qualquer_ponta_muda():
    for gatilho in ('refreshInventoryPack("pack")', 'refreshInventoryPack("unit")'):
        assert gatilho in APP_JS, f"falta o gatilho {gatilho}"
    # Editar um insumo tem de devolver a conversao, senao ela se perde na edicao.
    assert "form.elements.pack_unit.value = item.pack_unit" in MANAGE_JS
    assert "refreshInventoryPack();" in MANAGE_JS


# ----------------------------------------------------------------------
# A entrega, do balcao ao mapa
# ----------------------------------------------------------------------


def test_os_campos_de_entrega_so_aparecem_quando_a_venda_e_entrega():
    """O CSS ja previa `[hidden]`, mas nada ligava o gatilho.

    Os campos de frete, zona, endereco e entregador ficavam visiveis na venda de
    balcao, onde nao significam nada — e a caixa "Sai para entrega" parecia nao
    comandar coisa alguma.
    """

    assert ".sale-delivery-field[hidden]" in CSS
    assert 'document.getElementById("sale-is-delivery").addEventListener("change", toggleSaleDelivery)' in APP_JS
    assert "function toggleSaleDelivery()" in APP_JS
    # E o formulario tem os quatro campos que o gatilho esconde.
    for campo in ("delivery_fee", "delivery_zone_id", "delivery_address", "courier_id"):
        assert f'name="{campo}"' in HTML, f"o formulario de venda perdeu {campo}"


def test_a_zona_preenche_o_frete_da_venda():
    """Digitar o frete de novo e a chance de digita-lo diferente."""

    assert "function applySaleZoneFee" in APP_JS
    assert 'document.getElementById("sale-zone").addEventListener("change", applySaleZoneFee)' in APP_JS


def test_o_entregador_tem_um_caminho_para_o_mapa():
    courier_js = (ESTATICO / "courier.js").read_text(encoding="utf-8")
    assert "function linkDoMapa" in courier_js
    # O mapa e o aplicativo que ele ja tem no bolso: link, nao biblioteca.
    assert "google.com/maps/search/" in courier_js
    assert 'rel="noopener noreferrer"' in courier_js
    assert ".field-map" in CSS


# ----------------------------------------------------------------------
# Service worker
# ----------------------------------------------------------------------


def test_o_shell_do_service_worker_lista_arquivos_que_existem():
    sw = (ESTATICO / "service-worker.js").read_text(encoding="utf-8")
    listados = re.findall(r'"(/static/[^"]+)"', sw)
    faltando = [caminho for caminho in listados if not (ESTATICO / caminho.removeprefix("/static/")).exists()]

    assert not faltando, f"O service worker tenta cachear arquivos que não existem: {faltando}"
