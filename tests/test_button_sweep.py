"""Todo botão responde, em todas as telas e em todos os modais.

A tela tinha rede de proteção para a ligação (`test_frontend_wiring.py`: o id
existe, a classe tem estilo, a ação tem handler) e para alguns caminhos
isolados (`test_frontend_resilience.py`). O que faltava era o óbvio: alguém
clicar em tudo.

É uma classe de defeito que nenhum teste de serviço vê e que a leitura do
código também não pega — o botão abre um modal que existe, com um handler
registrado, e mesmo assim não acontece nada porque o estado que a tela mostra
não foi relido depois da gravação. Foi exatamente esse o caso das zonas de
entrega: a zona era criada no banco e a tabela continuava a de antes.

Os testes aqui abrem um Chromium de verdade contra a API de verdade, com uma
loja demonstrativa temporária, e recusam toda confirmação — nada é apagado.
"""

import os
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


@pytest.fixture(scope="module")
def ambiente(tmp_path_factory):
    """App, navegador e uma loja com movimento — montados uma vez só.

    Um navegador por teste multiplicaria por cinco o tempo da suíte sem
    encontrar nada a mais: o que cada teste faz é independente e nenhum deles
    grava por cima do outro.
    """

    playwright = pytest.importorskip("playwright.sync_api")
    tmp = tmp_path_factory.mktemp("sweep")
    anterior = {chave: os.environ.get(chave) for chave in (
        "DATA_MODE", "DEMO_DATA_PATH", "AUTH_REQUIRED", "DATA_RAW_PATH",
        "MEDIA_INBOX_PATH", "MEDIA_LIBRARY_PATH", "MEDIA_SYNC_LEGACY_FILES")}
    os.environ.update({
        "DATA_MODE": "demo", "DEMO_DATA_PATH": str(tmp / "store.json"),
        "AUTH_REQUIRED": "false", "DATA_RAW_PATH": str(tmp / "raw"),
        "MEDIA_INBOX_PATH": str(tmp / "inbox"), "MEDIA_LIBRARY_PATH": str(tmp / "library"),
        "MEDIA_SYNC_LEGACY_FILES": "false",
    })
    get_settings.cache_clear()
    try:
        with TestClient(app) as client, playwright.sync_playwright() as p:
            zona = client.post("/api/delivery/zones", json={
                "name": "Centro", "default_fee": 6, "target_minutes": 25}).json()
            entregador = client.post("/api/couriers", json={
                "name": "João", "fee_per_delivery": 4.5, "email": "joao@loja.com"}).json()
            client.post("/api/orders", json={
                "items": [{"product_id": "acai-500", "quantity": 1}], "is_delivery": True,
                "delivery_fee": 6, "customer_name": "Ana", "channel": "Delivery"})
            # O aplicativo de rua envia a posição ao bater o ponto. O cenário
            # de navegador precisa fazer o mesmo, para não depender da antiga
            # brecha que deixava abrir turno sem a loja marcada ou sem GPS.
            client.put("/api/delivery/store-location", json={
                "latitude": -22.9632676, "longitude": -43.406644, "radius_m": 150,
            }).raise_for_status()
            client.post("/api/field/check-in", json={
                "courier_id": entregador["id"],
                "latitude": -22.9632676, "longitude": -43.406644, "accuracy": 10,
            }).raise_for_status()
            corrida = client.post(f"/api/field/runs?courier_id={entregador['id']}", json={
                "zone_id": zona["id"], "items": [{"product_id": "acai-500", "quantity": 1}],
                "customer_name": "Ana"}).json()
            client.post(f"/api/field/runs/{corrida['id']}/finish?courier_id={entregador['id']}", json={})

            try:
                browser = p.chromium.launch()
            except playwright.Error as erro:
                pytest.skip(f"Chromium indisponível: {erro}")

            def serve(route):
                pedido = route.request
                url = urlsplit(pedido.url)
                if url.hostname != "testserver":
                    route.fulfill(status=200, body="", content_type="text/css")
                    return
                resposta = client.request(
                    pedido.method, url.path + (f"?{url.query}" if url.query else ""),
                    content=pedido.post_data_buffer,
                    headers={"content-type": pedido.headers.get("content-type", "")})
                route.fulfill(status=resposta.status_code, body=resposta.content,
                              content_type=resposta.headers.get("content-type", "application/octet-stream"))

            page = browser.new_page(viewport={"width": 1440, "height": 1000}, service_workers="block")
            erros: list[str] = []
            page.on("pageerror", lambda erro: erros.append(str(erro)))
            page.on("console", lambda msg: erros.append(f"console.error: {msg.text}") if msg.type == "error" else None)
            # Nada de destrutivo por padrão: toda confirmação é recusada. Um
            # segundo handler não resolveria — o Playwright chama todos, e o
            # primeiro a responder decide; por isso a escolha vira um sinal.
            confirmar = {"aceitar": False}
            page.on("dialog", lambda dialogo: dialogo.accept() if confirmar["aceitar"] else dialogo.dismiss())
            page.route("**/*", serve)
            page.goto("http://testserver/")
            page.wait_for_function("state.products.length > 0", timeout=90000)
            page.wait_for_timeout(800)

            yield {"page": page, "browser": browser, "serve": serve, "erros": erros,
                   "courier_id": entregador["id"], "zone_id": zona["id"], "client": client,
                   "confirmar": confirmar}
            browser.close()
    finally:
        for chave, valor in anterior.items():
            if valor is None:
                os.environ.pop(chave, None)
            else:
                os.environ[chave] = valor
        get_settings.cache_clear()


def _fechar(page):
    """Fecha diálogo e cortina.

    A cortina esquecida visível intercepta todo clique seguinte, e o sintoma
    — "o menu parou de responder" — não aponta para ela.
    """

    page.evaluate("document.querySelectorAll('dialog[open]').forEach(d => d.close());"
                  "const b = document.getElementById('modal-backdrop'); if (b) b.hidden = true;")
    page.wait_for_timeout(120)


def _erros(ambiente) -> list[str]:
    encontrados = list(ambiente["erros"])
    ambiente["erros"].clear()
    return encontrados


def test_todo_item_do_menu_abre_a_tela(ambiente):
    page = ambiente["page"]
    _fechar(page)
    telas = page.eval_on_selector_all("[data-view-target]", "els => els.map(e => e.dataset.viewTarget)")
    assert len(telas) >= 15, "o menu encolheu; o teste precisa ser revisto"

    mortas, quebradas = [], []
    for tela in telas:
        page.click(f'[data-view-target="{tela}"]')
        page.wait_for_timeout(450)
        if not page.eval_on_selector(f"#{tela}", "e => e.classList.contains('active')"):
            mortas.append(tela)
        quebradas += [f"{tela}: {erro}" for erro in _erros(ambiente)]

    assert not mortas, f"Itens de menu que não abrem a tela: {mortas}"
    assert not quebradas, f"Telas que quebram ao abrir: {quebradas}"


def test_todo_botao_de_modal_abre_o_dialogo(ambiente):
    """Botão que abre um modal existente e mesmo assim não mostra nada."""

    page = ambiente["page"]
    modais = page.eval_on_selector_all("[data-modal]", "els => [...new Set(els.map(e => e.dataset.modal))]")
    assert len(modais) >= 15

    inertes, quebrados = [], []
    for nome in modais:
        _fechar(page)
        botao = page.locator(f'[data-modal="{nome}"]').first
        secao = botao.evaluate("e => (e.closest('.view') || {}).id || ''")
        if secao:
            page.click(f'[data-view-target="{secao}"]')
            page.wait_for_timeout(300)
        # Botao dentro do menu "Mais" so existe com o menu aberto. Ele continua
        # sendo alcancavel — um clique a mais —, entao a varredura abre o menu
        # em vez de acusar o botao de inerte.
        botao.evaluate("e => e.closest('details')?.setAttribute('open', '')")
        try:
            botao.click(timeout=5000)
        except Exception as erro:
            inertes.append(f"{nome} (não clicável: {type(erro).__name__})")
            continue
        page.wait_for_timeout(400)
        if not page.eval_on_selector(f"#{nome}-modal", "e => e.open"):
            inertes.append(nome)
        quebrados += [f"{nome}: {erro}" for erro in _erros(ambiente)]

    _fechar(page)
    assert not inertes, f"Botões que não abrem o próprio modal: {inertes}"
    assert not quebrados, f"Modais que quebram ao abrir: {quebrados}"


def test_toda_acao_de_linha_abre_o_dialogo(ambiente):
    """O lápis de cada tabela: clicar tem de abrir a edição, não nada."""

    page = ambiente["page"]
    _fechar(page)
    acoes = page.eval_on_selector_all(
        "[data-edit-order],[data-edit-expense],[data-edit-inventory],[data-edit-product],"
        "[data-edit-courier],[data-edit-delivery],[data-edit-channel],[data-edit-recipe],"
        "[data-edit-zone],[data-edit-run],[data-edit-market-price],[data-edit-addon],"
        "[data-edit-anchor],[data-edit-movement]",
        "els => els.map(e => ({attr: Object.keys(e.dataset)[0], view: (e.closest('.view')||{}).id}))")
    assert acoes, "nenhuma ação de linha na tela; a loja de teste ficou sem dados"

    # Ação que nunca renderiza não aparece nesta lista, e um laço sobre o que
    # foi encontrado a ignora em silêncio — foi assim que o lápis do entregador
    # ficou ausente da tela sem nada acusar. O conjunto esperado é explícito.
    encontradas = {acao["attr"] for acao in acoes}
    obrigatorias = {"editOrder", "editExpense", "editInventory", "editProduct",
                    "editCourier", "editChannel", "editZone", "editRun"}
    assert not obrigatorias - encontradas, (
        f"Ações que a tela deveria oferecer e não renderizou: {sorted(obrigatorias - encontradas)}"
    )

    inertes, vistos = [], set()
    for acao in acoes:
        chave = acao["attr"]
        if chave in vistos:
            continue
        vistos.add(chave)
        _fechar(page)
        if acao["view"]:
            page.click(f'[data-view-target="{acao["view"]}"]')
            page.wait_for_timeout(350)
        seletor = "[data-" + "".join(f"-{letra.lower()}" if letra.isupper() else letra for letra in chave) + "]"
        try:
            page.locator(seletor).first.click(timeout=4000)
            page.wait_for_timeout(350)
            if not page.locator("dialog[open]").count():
                inertes.append(chave)
        except Exception as erro:
            inertes.append(f"{chave} ({type(erro).__name__})")

    _fechar(page)
    assert not inertes, f"Ações de linha que não abrem nada: {inertes}"


def test_entregador_recem_cadastrado_pode_ser_aberto(ambiente):
    """A tabela de entregadores vinha só do desempenho do período.

    Quem tinha acabado de ser cadastrado não aparecia em lugar nenhum até ter
    um turno fechado — e é exatamente ali, logo depois do cadastro, que alguém
    precisa abrir a ficha para ligar a conta do aplicativo de rua ao cadastro.
    Um entregador inalcançável é uma funcionalidade inalcançável.
    """

    page = ambiente["page"]
    _fechar(page)
    page.click('[data-view-target="delivery"]')
    page.wait_for_timeout(500)

    linhas = page.locator("#dl-courier-body [data-edit-courier]")
    assert linhas.count(), "nenhum entregador na tabela, mesmo havendo cadastro"

    linhas.first.click()
    page.wait_for_timeout(350)
    assert page.locator("#courier-modal[open]").count()
    # O campo do e-mail é o vínculo com a conta de rua; sem ele o cadastro não
    # serve para liberar ninguém.
    assert page.locator('#courier-form [name="email"]').count()
    _fechar(page)
    assert not _erros(ambiente)


def test_gravar_uma_zona_aparece_na_tela_sem_trocar_de_aba(ambiente):
    """A regressão que motivou este arquivo.

    A zona era criada no banco, o modal fechava, o aviso de sucesso aparecia —
    e a tabela continuava a de antes, porque a operação de rua é carregada sob
    demanda e o `loadAll` da gravação não a relê. Para quem clicou, o botão não
    tinha feito nada.
    """

    page = ambiente["page"]
    _fechar(page)
    page.click('[data-view-target="delivery"]')
    page.wait_for_timeout(400)
    page.click('[data-modal="zone"]')
    page.wait_for_timeout(300)
    page.fill('#zone-form [name="name"]', "Bairro do Teste")
    page.fill('#zone-form [name="default_fee"]', "7.5")
    page.click('#zone-form button[type="submit"]')
    page.wait_for_function("submitData.pending === false", timeout=60000)
    page.wait_for_timeout(1200)

    assert "Bairro do Teste" in page.locator("#dl-zone-body").inner_text()
    assert not _erros(ambiente)


def test_corrigir_a_entrega_registrada_na_rua_aparece_na_tela(ambiente):
    page = ambiente["page"]
    _fechar(page)
    page.click('[data-view-target="delivery"]')
    page.wait_for_timeout(400)
    page.locator("[data-edit-run]").first.click()
    page.wait_for_timeout(350)
    assert page.locator("#run-fix-modal[open]").count()

    page.fill('#run-fix-form [name="reference"]', "portao azul da praca")
    page.click('#run-fix-form button[type="submit"]')
    page.wait_for_function("submitData.pending === false", timeout=60000)
    page.wait_for_timeout(1200)

    assert "portao azul da praca" in page.locator("#dl-runs-body").inner_text()
    assert not _erros(ambiente)


def test_a_tela_do_entregador_responde_a_todos_os_botoes(ambiente):
    """O aplicativo de rua é outra página; ele não é coberto pelo resto."""

    rua = ambiente["browser"].new_page(viewport={"width": 390, "height": 844}, service_workers="block")
    erros: list[str] = []
    rua.on("pageerror", lambda erro: erros.append(str(erro)))
    rua.on("console", lambda msg: erros.append(f"console.error: {msg.text}") if msg.type == "error" else None)
    rua.on("dialog", lambda dialogo: dialogo.dismiss())
    rua.route("**/*", ambiente["serve"])
    rua.goto(f"http://testserver/entregador?courier_id={ambiente['courier_id']}")
    rua.wait_for_selector("#field-open:not([hidden])", timeout=60000)
    rua.wait_for_timeout(400)

    inertes = []
    for identificador in ("field-refresh", "field-manual", "field-checkout"):
        try:
            rua.click(f"#{identificador}", timeout=5000)
            rua.wait_for_timeout(400)
            rua.evaluate("document.querySelectorAll('dialog[open]').forEach(d => d.close())")
        except Exception as erro:
            inertes.append(f"{identificador} ({type(erro).__name__})")

    # "Entrega sem pedido no sistema" abre o modal com o cardápio dentro: sem
    # ele o entregador não tem como registrar a encomenda combinada por telefone.
    rua.click("#field-manual")
    rua.wait_for_timeout(400)
    aberto = rua.locator("#run-modal[open]").count()
    produtos = rua.locator("#run-items select option").count()
    zonas = rua.locator("#run-zone option").count()
    rua.close()

    assert not inertes, f"Botões inertes no aplicativo de rua: {inertes}"
    assert not erros, f"A tela de rua quebrou: {erros}"
    assert aberto, "o botão de entrega avulsa não abriu o formulário"
    assert produtos, "o formulário de entrega avulsa abriu sem cardápio"
    assert zonas, "o formulário de entrega avulsa abriu sem zonas"


def test_excluir_a_entrega_registrada_na_rua(ambiente):
    """O caminho destrutivo, que a varredura recusa de propósito.

    Toda confirmação é dispensada nos outros testes para não apagar dados no
    meio da varredura — e "recusei a confirmação" não prova que aceitar
    funciona. Este cria o próprio alvo e o apaga de verdade, então não depende
    da ordem em que os testes rodam nem estraga o que os outros usam.
    """

    client, page = ambiente["client"], ambiente["page"]
    corrida = client.post(f"/api/field/runs?courier_id={ambiente['courier_id']}", json={
        "zone_id": ambiente["zone_id"],
        "items": [{"product_id": "acai-300", "quantity": 1}],
        "reference": "entrega para apagar",
    }).json()
    client.post(f"/api/field/runs/{corrida['id']}/finish?courier_id={ambiente['courier_id']}", json={})
    antes = len(client.get("/api/delivery/runs").json())

    _fechar(page)
    # Só neste teste a confirmação é aceita, e o sinal volta a falso no fim
    # para nenhum teste seguinte apagar nada sem querer.
    ambiente["confirmar"]["aceitar"] = True
    try:
        page.click('[data-view-target="delivery"]')
        page.wait_for_timeout(700)
        alvo = page.locator(f'[data-delete-run="{corrida["id"]}"]')
        assert alvo.count(), "a entrega criada não apareceu com botão de excluir"
        alvo.first.click()
        page.wait_for_function("submitData.pending === false", timeout=60000)
        page.wait_for_timeout(1000)
    finally:
        ambiente["confirmar"]["aceitar"] = False

    assert len(client.get("/api/delivery/runs").json()) == antes - 1
    assert not _erros(ambiente)
