"""A integração com a Anota Aí: o cardápio que a loja mantém lá.

Toda venda da loja passa pela Anota Aí, e o relatório exportado de pedidos não
traz os itens vendidos. Essa ausência é a raiz do que o painel não responde:
sem saber quais copos saíram, a ficha técnica não baixa insumo, o custo não
fecha e a previsão não tem o que aprender.

O cardápio é a ponta que resolve isso na origem, e é a única parte da API cuja
resposta já está documentada — por isso ela vem primeiro. `Transport` é
injetável, então estes testes exercitam a tradução da resposta real sem tocar
na internet.
"""

import json

import pytest

from app.repositories.secrets import FileVault
from app.repositories.store import DemoStore
from app.services import ERPService
from app.services.anotaai import (
    MENU_PATH,
    SECRET_NAME,
    AnotaAiClient,
    AnotaAiError,
    AnotaAiService,
)

CHAVE = "chave-de-integracao-da-loja"

# A forma exata que a Anota Aí devolve, conferida sondando a API em 18/08/2026.
CARDAPIO = {
    "success": True,
    "message": "ok",
    "categories": [
        {
            "title": "Açaí",
            "id": "cat-1",
            "is_additional": False,
            "itens": [
                # Casa com o cardápio do ERP pelo nome.
                {"id": "a1", "title": "Açaí 500 ml", "price": 22.0, "out": False, "external_id": None},
                # Não existe no ERP: é o item que chega em pedido sem custo.
                {"id": "a2", "title": "Açaí Ninho com Nutella 700 ml", "price": 32.0, "out": False, "external_id": None},
                # Fora do ar no cardápio deles.
                {"id": "a3", "title": "Açaí Sazonal Pitaya", "price": 28.0, "out": True, "external_id": None},
            ],
        },
        {
            "title": "Complementos",
            "id": "cat-2",
            "is_additional": True,
            "itens": [{"id": "c1", "title": "Granola", "price": 2.0, "out": False, "external_id": None}],
        },
    ],
}


def _transporte(resposta, status=200, registro=None):
    def transporte(method, url, *, headers, body, timeout):
        if registro is not None:
            registro.append({"method": method, "url": url, "headers": headers})
        return status, resposta

    return transporte


@pytest.fixture
def servico(tmp_path) -> AnotaAiService:
    store = DemoStore(tmp_path / "store.json")
    return AnotaAiService(
        store, ERPService(store), FileVault(tmp_path / "cofre.json"),
        AnotaAiClient(_transporte(CARDAPIO)),
    )


# ----------------------------------------------------------------------
# A conexao
# ----------------------------------------------------------------------


def test_a_chave_vai_direto_no_cabecalho_sem_bearer(tmp_path):
    """Com "Bearer" a Anota Aí devolve 401 — foi medido contra a API real."""

    registro = []
    cliente = AnotaAiClient(_transporte(CARDAPIO, registro=registro))

    cliente.catalog(CHAVE)

    assert registro[0]["headers"]["Authorization"] == CHAVE
    assert "Bearer" not in registro[0]["headers"]["Authorization"]
    assert registro[0]["url"].endswith(MENU_PATH)


def test_conectar_confirma_a_chave_na_hora(servico):
    """Chave errada guardada em silêncio vira "não traz nada" três dias depois."""

    resultado = servico.connect(CHAVE)

    assert resultado["connected"] is True
    assert resultado["categories"] == 2
    assert resultado["items"] == 4
    assert servico.status()["connected"] is True


def test_a_chave_nunca_volta_para_a_tela(servico):
    servico.connect(CHAVE)

    estado = servico.status()

    assert CHAVE not in json.dumps(estado)
    assert "integration_key" not in estado


def test_chave_recusada_explica_onde_achar_a_certa(tmp_path):
    """Colar a do endereço do navegador é o erro comum: ela é sessão de 15 min."""

    store = DemoStore(tmp_path / "store.json")
    servico = AnotaAiService(
        store, ERPService(store), FileVault(tmp_path / "cofre.json"),
        AnotaAiClient(_transporte({"err": "Failed to authenticate token."}, status=401)),
    )

    with pytest.raises(AnotaAiError, match="Configurações → Integrações"):
        servico.connect(CHAVE)

    assert servico.status()["connected"] is False


def test_endereco_nao_reconhecido_sugere_liberacao_pelo_suporte(tmp_path):
    store = DemoStore(tmp_path / "store.json")
    servico = AnotaAiService(
        store, ERPService(store), FileVault(tmp_path / "cofre.json"),
        AnotaAiClient(_transporte({}, status=404)),
    )

    with pytest.raises(AnotaAiError, match="liberada para a loja"):
        servico.connect(CHAVE)


def test_sem_conexao_a_previa_diz_o_que_falta(servico):
    with pytest.raises(AnotaAiError, match="ainda não está conectada"):
        servico.catalog_preview()


def test_desconectar_apaga_a_chave(servico):
    servico.connect(CHAVE)

    servico.disconnect()

    assert servico.status()["connected"] is False
    with pytest.raises(AnotaAiError, match="ainda não está conectada"):
        servico.catalog_preview()


# ----------------------------------------------------------------------
# A previa: relatar antes de criar
# ----------------------------------------------------------------------


def test_a_previa_separa_o_que_casa_do_que_falta(servico):
    servico.connect(CHAVE)

    previa = servico.catalog_preview()

    casados = {linha["name"] for linha in previa["matched"]}
    novos = {linha["name"] for linha in previa["new"]}
    assert "Açaí 500 ml" in casados
    assert "Açaí Ninho com Nutella 700 ml" in novos
    # Complemento não é produto: criá-lo como copo vendável seria inventar item.
    assert [linha["name"] for linha in previa["addons"]] == ["Granola"]


def test_a_previa_marca_quem_casou_so_pelo_nome(servico):
    """Casar por nome funciona até alguém renomear o item lá."""

    servico.connect(CHAVE)

    previa = servico.catalog_preview()

    acai = next(linha for linha in previa["matched"] if linha["name"] == "Açaí 500 ml")
    assert acai["linked_by"] == "nome"
    assert any("identificador" in leitura["title"] for leitura in previa["readings"])


def test_a_previa_aponta_preco_divergente(servico):
    """Quem cobra do cliente é a Anota Aí; divergir é margem calculada errada."""

    servico.connect(CHAVE)
    produto = next(p for p in servico.store.list("products") if p["name"] == "Açaí 500 ml")
    produto["price"] = 19.0
    servico.store.put("products", produto["id"], produto)

    previa = servico.catalog_preview()

    acai = next(linha for linha in previa["matched"] if linha["name"] == "Açaí 500 ml")
    assert acai["price_differs"] is True
    assert any("preço diferente" in leitura["title"] for leitura in previa["readings"])


def test_a_previa_mostra_o_que_existe_so_aqui(servico):
    """Produto que some do cardápio de lá para de vender sem ninguém notar."""

    servico.connect(CHAVE)

    previa = servico.catalog_preview()

    assert any(linha["name"] == "Açaí 300 ml" for linha in previa["only_here"])


def test_a_previa_nao_grava_nada(servico):
    servico.connect(CHAVE)
    antes = len(servico.store.list("products"))

    servico.catalog_preview()

    assert len(servico.store.list("products")) == antes


# ----------------------------------------------------------------------
# A importacao: o cadastro entra, a ficha fica
# ----------------------------------------------------------------------


def test_importar_cria_o_produto_sem_ficha_tecnica(servico):
    """Inventar receita seria inventar custo, e custo inventado se espalha."""

    servico.connect(CHAVE)

    resultado = servico.import_catalog(["a2"])

    assert len(resultado["created"]) == 1
    criado = next(p for p in servico.store.list("products") if p["name"] == "Açaí Ninho com Nutella 700 ml")
    assert criado["recipe"] == []
    assert criado["price"] == 32.0
    assert resultado["pending_recipe"] == 1


def test_o_item_fora_do_ar_entra_desativado(servico):
    servico.connect(CHAVE)

    servico.import_catalog(["a3"])

    sazonal = next(p for p in servico.store.list("products") if p["name"] == "Açaí Sazonal Pitaya")
    assert sazonal["active"] is False


def test_importar_sem_escolha_traz_todos_os_que_faltam(servico):
    servico.connect(CHAVE)

    resultado = servico.import_catalog([])

    assert len(resultado["created"]) == 2


def test_um_item_recusado_nao_derruba_os_outros(tmp_path):
    """Preço zero é item de vitrine no cardápio deles, não produto vendável."""

    cardapio = {
        "success": True,
        "categories": [{
            "title": "Açaí", "id": "c", "is_additional": False,
            "itens": [
                {"id": "z1", "title": "A partir de", "price": 0, "out": False},
                {"id": "z2", "title": "Copo Novo 900 ml", "price": 36.0, "out": False},
            ],
        }],
    }
    store = DemoStore(tmp_path / "store.json")
    servico = AnotaAiService(
        store, ERPService(store), FileVault(tmp_path / "cofre.json"),
        AnotaAiClient(_transporte(cardapio)),
    )
    servico.connect(CHAVE)

    resultado = servico.import_catalog([])

    assert [linha["name"] for linha in resultado["created"]] == ["Copo Novo 900 ml"]
    assert resultado["rejected"][0]["name"] == "A partir de"
    assert "sem preço" in resultado["rejected"][0]["reason"]


def test_importar_sem_novidade_e_recusado(servico):
    servico.connect(CHAVE)
    servico.import_catalog([])

    with pytest.raises(AnotaAiError, match="Nenhum item novo"):
        servico.import_catalog([])


# ----------------------------------------------------------------------
# O pedido: da fila da Anota Ai ate a mao do entregador
#
# A forma da resposta de `/ping/get` nunca pode ser conferida — a chave de
# integracao da loja nao chegou a funcionar. Entao o tradutor procura o valor em
# varios caminhos plausiveis, e estes testes exercitam formas diferentes de
# resposta para provar que a busca funciona e que o que nao e encontrado volta
# como "nao entendi", nunca como zero.
# ----------------------------------------------------------------------

FILA = {"success": True, "info": {"docs": [{"_id": "ped-1", "check": False}], "count": 1}}


def _fila_e_pedido(pedido, fila=None):
    """Responde a fila num caminho e o detalhe no outro."""

    def transporte(method, url, *, headers, body, timeout):
        return 200, (pedido if "/ping/get/" in url else (fila or FILA))

    return transporte


def _servico_com(tmp_path, pedido, fila=None) -> AnotaAiService:
    store = DemoStore(tmp_path / "store.json")
    servico = AnotaAiService(
        store, ERPService(store), FileVault(tmp_path / "cofre.json"),
        AnotaAiClient(_fila_e_pedido(pedido, fila)),
    )
    servico.vault.write("anotaai-credentials", {"integration_key": CHAVE})
    return servico


def test_o_pedido_de_entrega_chega_e_fica_pronto_para_o_entregador(tmp_path):
    """É o que a loja precisa: o pedido lançado lá aparece na fila da rua."""

    servico = _servico_com(tmp_path, {
        "displayId": "8801", "total": 37.0, "deliveryFee": 7.0, "type": "DELIVERY",
        "customer": {"name": "Ana Souza"},
        "delivery": {"address": {"street": "Rua das Flores", "number": "120", "neighborhood": "Centro"}},
        "itens": [{"title": "Açaí 500 ml", "quantity": 1}],
    })

    resumo = servico.sync()

    assert resumo["created"] == 1
    pedido = next(linha for linha in servico.store.list("orders") if linha.get("anota_order_id") == "ped-1")
    assert pedido["is_delivery"] is True
    assert pedido["delivery_fee"] == 7.0
    assert pedido["total"] == 37.0
    assert pedido["customer_name"] == "Ana Souza"
    # O endereço é o que faz o mapa do entregador abrir direto.
    assert "Rua das Flores" in pedido["delivery_address"]
    assert "Centro" in pedido["delivery_address"]


def test_o_pedido_com_item_identificado_baixa_a_ficha_tecnica(tmp_path):
    servico = _servico_com(tmp_path, {
        "displayId": "8802", "total": 22.0, "type": "DELIVERY",
        "itens": [{"title": "Açaí 500 ml", "quantity": 1}],
    })
    antes = next(i for i in servico.erp.inventory() if i["id"] == "acai")["quantity"]

    servico.sync()

    assert next(i for i in servico.erp.inventory() if i["id"] == "acai")["quantity"] < antes


def test_item_que_nao_casa_deixa_o_pedido_sem_custo_em_vez_de_chutar(tmp_path):
    """Meio custo apresentado como custo inteiro é pior que custo nenhum."""

    servico = _servico_com(tmp_path, {
        "displayId": "8803", "total": 30.0, "type": "DELIVERY",
        "itens": [{"title": "Açaí Ninho com Nutella", "quantity": 1}],
    })

    resumo = servico.sync()

    assert resumo["without_items"] == 1
    pedido = next(linha for linha in servico.store.list("orders") if linha.get("anota_order_id") == "ped-1")
    assert pedido["cost"] is None
    assert pedido["unmapped_items"] == ["Açaí Ninho com Nutella"]


def test_o_tradutor_acha_o_valor_em_outro_formato_de_resposta(tmp_path):
    """Os nomes dos campos são hipótese até a primeira chamada real."""

    servico = _servico_com(tmp_path, {
        "numero": "8804", "values": {"total": 45.0, "deliveryFee": 8.0},
        "tipo": "Entrega", "cliente": {"nome": "João"},
        "produtos": [{"nome": "Açaí 500 ml", "quantidade": 2}],
    })

    resumo = servico.sync()

    assert resumo["created"] == 1
    pedido = next(linha for linha in servico.store.list("orders") if linha.get("anota_order_id") == "ped-1")
    assert pedido["total"] == 45.0
    assert pedido["delivery_fee"] == 8.0
    assert pedido["is_delivery"] is True


def test_pedido_sem_valor_reconhecivel_nao_vira_venda(tmp_path):
    """Um pedido de R$ 0 entraria no faturamento como desconto que não houve."""

    servico = _servico_com(tmp_path, {"algumCampoDesconhecido": {"x": 1}})

    resumo = servico.sync()

    assert resumo["created"] == 0
    assert resumo["unreadable"][0]["id"] == "ped-1"
    assert "valor do pedido" in resumo["unreadable"][0]["reason"]
    # A loja de demonstração já tem pedidos: o que não pode existir é um vindo
    # desta sincronização.
    assert not [linha for linha in servico.store.list("orders") if linha.get("anota_order_id")]


def test_retirada_nao_entra_como_entrega(tmp_path):
    servico = _servico_com(tmp_path, {
        "displayId": "8805", "total": 20.0, "type": "TAKEOUT",
        "itens": [{"title": "Açaí 500 ml", "quantity": 1}],
    })

    servico.sync()

    pedido = next(linha for linha in servico.store.list("orders") if linha.get("anota_order_id") == "ped-1")
    assert pedido["is_delivery"] is False
    assert pedido["delivery_fee"] == 0.0


def test_o_mesmo_pedido_nao_entra_duas_vezes(tmp_path):
    servico = _servico_com(tmp_path, {
        "displayId": "8806", "total": 20.0, "type": "DELIVERY",
        "itens": [{"title": "Açaí 500 ml", "quantity": 1}],
    })
    servico.sync()

    segundo = servico.sync()

    assert segundo["created"] == 0
    assert segundo["skipped"] == 1


def test_o_id_conversa_com_o_importador_de_planilha(tmp_path):
    """Os dois caminhos descrevem o mesmo pedido: o id é o que os concilia.

    O importador pula pedido cujo id já existe, então o que entrou pela API não
    é duplicado quando a planilha da semana chegar.
    """

    from datetime import datetime

    servico = _servico_com(tmp_path, {
        "displayId": "8807", "total": 20.0, "type": "DELIVERY",
        "createdAt": "2026-09-10T15:00:00",
        "itens": [{"title": "Açaí 500 ml", "quantity": 1}],
    })

    servico.sync()

    assert servico.store.get("orders", "anota-ai-8807-2026-09-10") is not None


def test_a_origem_real_vira_canal_e_nao_some_dentro_da_anota_ai(tmp_path):
    """Todo pedido passa pela Anota Aí: ela é o encanamento, não o canal."""

    servico = _servico_com(tmp_path, {
        "displayId": "8808", "total": 20.0, "type": "DELIVERY", "salesChannel": "IFOOD",
        "itens": [{"title": "Açaí 500 ml", "quantity": 1}],
    })

    servico.sync()

    pedido = next(linha for linha in servico.store.list("orders") if linha.get("anota_order_id") == "ped-1")
    assert pedido["channel"] == "iFood"


def test_a_sondagem_mostra_a_forma_crua_do_pedido(tmp_path):
    """Uma chamada de verdade, e a tradução deixa de ser hipótese."""

    servico = _servico_com(tmp_path, {"displayId": "9", "total": 10.0, "campoNovo": "x"})

    forma = servico.probe()

    assert forma["orders_waiting"] == 1
    assert "campoNovo" in forma["order_keys"]
    assert forma["order_sample"]["displayId"] == "9"


def test_a_sondagem_com_fila_vazia_diz_o_que_fazer(tmp_path):
    servico = _servico_com(tmp_path, {}, fila={"success": True, "info": {"docs": [], "count": 0}})

    forma = servico.probe()

    assert forma["orders_waiting"] == 0
    assert "pedido de teste" in forma["detail"]


def test_o_pedido_da_anota_ai_aparece_na_fila_do_entregador(tmp_path):
    """O caminho inteiro, que é o que a loja pediu.

    Pedido lançado na Anota Aí → sincronização → fila do celular do entregador,
    com produto, frete e endereço prontos. É o mesmo caminho que o pedido
    lançado no balcão já percorria; nada do que existia foi retirado.
    """

    from app.schemas import CourierCheckIn, CourierCreate
    from app.services import DeliveryService, DispatchService

    servico = _servico_com(tmp_path, {
        "displayId": "9100", "total": 29.0, "deliveryFee": 6.0, "type": "DELIVERY",
        "customer": {"name": "Ana Souza"},
        "delivery": {"address": {"street": "Rua das Flores", "number": "120"}},
        "itens": [{"title": "Açaí 500 ml", "quantity": 1}],
    })
    servico.sync()

    despacho = DispatchService(servico.store)
    entregador = DeliveryService(servico.store).create_courier(CourierCreate(name="Leonardo"))
    despacho.check_in(entregador, CourierCheckIn())

    fila = despacho.queue(entregador["id"])["orders"]

    assert len(fila) == 1
    assert fila[0]["customer_name"] == "Ana Souza"
    assert fila[0]["freight"] == 6.0
    assert "Rua das Flores" in fila[0]["address"]
    assert fila[0]["items"][0]["name"] == "Açaí 500 ml"


# ----------------------------------------------------------------------
# Token vencido nao e token errado
#
# A API responde as duas coisas com a mesma frase ("Failed to authenticate
# token."), e elas pedem acoes opostas: uma se resolve copiando de novo, a outra
# so com o suporte. O token que a Anota Ai emite vale 15 minutos — curto o
# bastante para vencer entre copiar e colar. Quando o valor e um JWT, da para
# separar os dois casos antes de gastar a chamada.
# ----------------------------------------------------------------------


def _jwt(segundos_ate_expirar: int) -> str:
    """Um token com a forma que a Anota Aí emite. A assinatura não importa aqui."""

    import base64
    import time

    def parte(dados):
        return base64.urlsafe_b64encode(json.dumps(dados).encode()).decode().rstrip("=")

    agora = int(time.time())
    cabecalho = parte({"alg": "HS256", "typ": "JWT"})
    corpo = parte({
        "idpage": "66dfb3a439b8de0012bbfd26", "iat": agora,
        "exp": agora + segundos_ate_expirar, "iss": "session-api",
    })
    return f"{cabecalho}.{corpo}.assinatura"


def _com_registro(tmp_path):
    chamadas: list[dict] = []
    store = DemoStore(tmp_path / "store.json")
    cofre = FileVault(tmp_path / "cofre.json")
    service = AnotaAiService(
        store, ERPService(store), cofre,
        AnotaAiClient(_transporte(CARDAPIO, registro=chamadas)),
    )
    return service, cofre, chamadas


def test_token_vencido_diz_que_venceu_em_vez_de_chave_errada(tmp_path):
    service, cofre, chamadas = _com_registro(tmp_path)

    with pytest.raises(AnotaAiError) as erro:
        service.connect(_jwt(-1800))

    assert "expirou" in str(erro.value)
    assert "15 minutos" in str(erro.value)
    # E nao gastou chamada: nao ha o que perguntar a uma credencial morta.
    assert chamadas == []
    assert not (cofre.read(SECRET_NAME) or {}).get("integration_key")


def test_token_dentro_da_validade_segue_para_a_api(tmp_path):
    service, cofre, chamadas = _com_registro(tmp_path)

    service.connect(_jwt(600))

    assert len(chamadas) == 1


def test_chave_comum_nao_e_confundida_com_token_vencido(tmp_path):
    """Nem toda credencial é um JWT; a que não for passa direto."""

    service, cofre, chamadas = _com_registro(tmp_path)

    service.connect("chave-fixa-do-painel-1234")

    assert len(chamadas) == 1


def test_a_tela_avisa_quando_a_chave_guardada_venceu(tmp_path):
    """Chave guardada não é chave viva, e "conectado" sozinho esconde isso."""

    service, cofre, chamadas = _com_registro(tmp_path)
    cofre.write(SECRET_NAME, {"integration_key": _jwt(-3600)})

    estado = service.status()

    assert estado["connected"] is True
    assert estado["key_expired"] is True
    assert estado["key_expired_minutes"] >= 59


def test_chave_viva_nao_e_marcada_como_vencida(tmp_path):
    service, cofre, chamadas = _com_registro(tmp_path)
    service.connect(_jwt(600))

    estado = service.status()

    assert estado["key_expired"] is False
    assert estado["key_expired_minutes"] is None
