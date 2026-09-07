"""Integração com o iFood, exercitada sem rede.

O que estes testes protegem não é o formato da chamada HTTP — é o que acontece
com o dinheiro e com a prateleira quando um pedido chega. Pedido do iFood que
entra sem baixar insumo deixa o estoque mentindo; pedido que entra duas vezes
inventa faturamento; evento confirmado antes de gravar perde a venda. Cada um
desses tem um teste aqui.

A rede é substituída por um transporte falso que responde como a API do iFood.
Nenhum teste toca a internet.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from app.repositories.secrets import FileVault
from app.repositories.store import DemoStore
from app.schemas import IFoodActivationConfirm, IFoodActivationStart
from app.services import ERPService
from app.services.ifood import (
    INTEGRATION_ID,
    INTEGRATIONS,
    SECRET_FIELDS,
    SECRET_NAME,
    STATUS_CONNECTED,
    STATUS_WAITING,
    IFoodClient,
    IFoodError,
    IFoodService,
)

CLIENT_ID = "cliente-de-teste-0001"
CLIENT_SECRET = "segredo-de-teste-0001"


class FakeIFood:
    """Responde como a API do iFood e guarda o que foi pedido."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.headers: list[dict[str, str]] = []
        self.forms: list[dict[str, str]] = []
        self.acknowledged: list[str] = []
        self.events: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.merchants = [{"id": "loja-1", "name": "Açaí do Teste"}]
        self.user_code_response = {
            "userCode": "ABCD-1234",
            "authorizationCodeVerifier": "verificador-secreto",
            "verificationUrl": "https://portal.ifood.com.br/apps/code",
            "verificationUrlComplete": "https://portal.ifood.com.br/apps/code?c=ABCD-1234",
            "expiresIn": 600,
        }
        self.token_response = {
            "accessToken": "token-de-acesso", "refreshToken": "token-de-renovacao",
            "expiresIn": 21600, "type": "bearer",
        }
        self.fail_with: dict[str, tuple[int, dict]] = {}

    def __call__(self, method, url, *, headers, body, timeout):
        path = urlsplit(url).path
        self.calls.append((method, path))
        self.headers.append(dict(headers))
        if path in self.fail_with:
            return self.fail_with[path]

        if path.endswith("/oauth/userCode"):
            self.forms.append(self._form(body))
            return 200, dict(self.user_code_response)
        if path.endswith("/oauth/token"):
            self.forms.append(self._form(body))
            return 200, dict(self.token_response)
        if path.endswith("/merchant/v1.0/merchants"):
            return 200, list(self.merchants)
        if path.endswith("events:polling"):
            return (200, list(self.events)) if self.events else (204, None)
        if path.endswith("/events/acknowledgment"):
            self.acknowledged.extend(item["id"] for item in _decode(body))
            return 202, None
        if "/order/v1.0/orders/" in path:
            return 200, dict(self.orders[path.rsplit("/", 1)[-1]])
        raise AssertionError(f"caminho não previsto: {path}")

    @staticmethod
    def _form(body: bytes) -> dict[str, str]:
        return {key: value[0] for key, value in parse_qs(body.decode("utf-8")).items()}

    # -- ajuda dos testes ----------------------------------------------

    def place(self, order_id: str, items: list[dict], *, amount: float, display: str = "1234",
              delivery_fee: float = 0.0, benefits: float = 0.0, order_type: str = "DELIVERY") -> None:
        self.orders[order_id] = {
            "id": order_id, "displayId": display, "orderType": order_type,
            "salesChannel": "IFOOD", "createdAt": _now_iso(), "items": items,
            "total": {"subTotal": amount - delivery_fee + benefits, "deliveryFee": delivery_fee,
                      "benefits": benefits, "orderAmount": amount},
            "payments": {"methods": [{"method": "CREDIT"}]},
            "customer": {"name": "Cliente de Teste"},
        }

    def emit(self, event_id: str, order_id: str, code: str = "CFM") -> None:
        self.events.append({"id": event_id, "code": code, "fullCode": code,
                            "orderId": order_id, "merchantId": "loja-1", "createdAt": _now_iso()})


def _decode(body: bytes) -> list[dict]:
    import json

    return json.loads(body.decode("utf-8"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def loja(tmp_path):
    store = DemoStore(tmp_path / "store.json")
    erp = ERPService(store)
    fake = FakeIFood()
    cofre = FileVault(tmp_path / "secrets.json")
    service = IFoodService(store, erp, cofre, IFoodClient(transport=fake, host="https://ifood.test"))
    return store, erp, service, fake


def conectar(service: IFoodService) -> None:
    service.start_activation(IFoodActivationStart(client_id=CLIENT_ID, client_secret=CLIENT_SECRET))
    service.confirm_activation(IFoodActivationConfirm(authorization_code="codigo-do-portal"))


# ----------------------------------------------------------------------
# Ativação
# ----------------------------------------------------------------------


def test_ativacao_gera_o_codigo_que_a_loja_leva_ao_portal(loja):
    """Sem esse código não existe integração: é ele que liga o app à loja."""

    _, _, service, fake = loja

    resultado = service.start_activation(
        IFoodActivationStart(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    )

    assert resultado["user_code"] == "ABCD-1234"
    assert resultado["verification_url"].startswith("https://portal.ifood.com.br")
    assert "Ativar aplicativo via código" in resultado["instructions"]
    assert "ABCD-1234" in resultado["instructions"]
    # O iFood recebe a chave do aplicativo, e só ela.
    assert fake.forms[0] == {"clientId": CLIENT_ID}
    assert service.status()["status"] == STATUS_WAITING


def test_a_tela_nunca_recebe_chave_segredo_nem_token(loja):
    """Estado da conexão é uma coisa; credencial é outra e não sai daqui."""

    _, _, service, _ = loja
    conectar(service)

    import json

    estado = service.status()

    serializado = json.dumps(estado, ensure_ascii=False)
    for segredo in (CLIENT_ID, CLIENT_SECRET, "token-de-acesso", "token-de-renovacao", "verificador-secreto"):
        assert segredo not in serializado, f"{segredo} vazou para a tela"
    assert estado["connected"] is True
    assert estado["client_id_hint"] == "clie…0001", "o bastante para reconhecer, não para usar"


def test_confirmacao_troca_o_codigo_por_token_e_identifica_a_loja(loja):
    """O merchantId separa esta loja das outras que a mesma chave enxerga."""

    store, _, service, fake = loja
    service.start_activation(IFoodActivationStart(client_id=CLIENT_ID, client_secret=CLIENT_SECRET))

    estado = service.confirm_activation(IFoodActivationConfirm(authorization_code="codigo-do-portal"))

    assert estado["status"] == STATUS_CONNECTED
    assert estado["merchant_id"] == "loja-1"
    assert estado["merchant_name"] == "Açaí do Teste"
    troca = fake.forms[-1]
    assert troca["grantType"] == "authorization_code"
    assert troca["authorizationCode"] == "codigo-do-portal"
    assert troca["authorizationCodeVerifier"] == "verificador-secreto"
    # O canal precisa existir para o pedido do iFood ter onde cair.
    assert any(row["name"] == "iFood" for row in store.list("channels"))


def test_confirmar_sem_gerar_codigo_e_recusado(loja):
    _, _, service, _ = loja

    with pytest.raises(IFoodError, match="Gere um código"):
        service.confirm_activation(IFoodActivationConfirm(authorization_code="qualquer"))


def test_codigo_vencido_nao_vira_conexao(loja):
    """Código de ativação expira em minutos; aceitar depois seria mentir."""

    store, _, service, _ = loja
    service.start_activation(IFoodActivationStart(client_id=CLIENT_ID, client_secret=CLIENT_SECRET))
    estado = store.get(INTEGRATIONS, INTEGRATION_ID)
    estado["code_expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    store.put(INTEGRATIONS, INTEGRATION_ID, estado)

    with pytest.raises(IFoodError, match="expirou"):
        service.confirm_activation(IFoodActivationConfirm(authorization_code="codigo-do-portal"))
    assert service.status()["status"] == "desconectado", "código vencido não fica pendurado na tela"


def test_credencial_recusada_vira_recado_e_nao_erro_500(loja):
    _, _, service, fake = loja
    fake.fail_with["/authentication/v1.0/oauth/userCode"] = (401, {"message": "invalid client"})

    with pytest.raises(IFoodError, match="recusou as credenciais"):
        service.start_activation(IFoodActivationStart(client_id=CLIENT_ID, client_secret=CLIENT_SECRET))


def test_sincronizar_desconectado_ensina_o_proximo_passo(loja):
    _, _, service, _ = loja

    with pytest.raises(IFoodError, match="ainda não está conectado"):
        service.sync()


# ----------------------------------------------------------------------
# O que só aparece contra a API de verdade
# ----------------------------------------------------------------------


def test_resposta_comprimida_e_lida_como_json():
    """O iFood responde em gzip mesmo sem o pedido aceitar compressão.

    Medido contra a API real em 07/09/2026: `Content-Encoding: gzip` numa
    requisição que não anunciou `Accept-Encoding`. Sem desfazer isso, toda
    resposta virava texto ilegível e a integração parecia fora do ar.
    """

    import gzip
    import json as _json

    from app.services.ifood import _decode

    corpo = gzip.compress(_json.dumps({"userCode": "ABCD-1234"}).encode("utf-8"))

    assert _decode(corpo, "gzip") == {"userCode": "ABCD-1234"}
    assert _decode(corpo, None) == {"userCode": "ABCD-1234"}, "o cabeçalho pode faltar; os bytes não mentem"
    assert _decode(b"", "gzip") is None
    assert _decode(b"nao e json", None) is None


def test_a_recusa_aninhada_do_ifood_vira_frase_legivel():
    """Corpo real do iFood: {"error": {"code": ..., "message": ...}}."""

    from app.services.ifood import _error_message

    assert _error_message({"error": {"code": "BadRequest", "message": "clientId is invalid"}}) == "clientId is invalid"
    assert _error_message({"message": "plano simples"}) == "plano simples"
    assert _error_message({"error": "texto direto"}) == "texto direto"
    assert _error_message(None) == ""


def test_o_erro_do_ifood_chega_ao_operador_em_vez_do_dicionario(loja):
    _, _, service, fake = loja
    fake.fail_with["/authentication/v1.0/oauth/userCode"] = (
        400, {"error": {"code": "BadRequest", "message": "clientId is invalid"}}
    )

    with pytest.raises(IFoodError) as erro:
        service.start_activation(IFoodActivationStart(client_id=CLIENT_ID, client_secret=CLIENT_SECRET))

    assert "clientId is invalid" in str(erro.value)
    assert "{" not in str(erro.value), "dicionário impresso na tela não explica nada"


# ----------------------------------------------------------------------
# Entrada do pedido
# ----------------------------------------------------------------------


def test_pedido_confirmado_baixa_a_ficha_tecnica_e_guarda_o_valor_do_ifood(loja):
    """O ponto inteiro da integração: custo pela ficha, receita pelo iFood.

    O marketplace cobra mais caro que o balcão. O estoque e o custo têm de vir
    da ficha técnica, porque é ela que sabe o que saiu da prateleira; o
    faturamento tem de vir do iFood, porque é o dinheiro que de fato entrou.
    """

    store, erp, service, fake = loja
    conectar(service)
    saldo = next(row for row in erp.inventory() if row["id"] == "acai")["quantity"]
    fake.place("pedido-1", [{"name": "Açaí 500 ml", "externalCode": "acai-500", "quantity": 2}],
               amount=58.0, delivery_fee=6.0)
    fake.emit("evento-1", "pedido-1")

    resumo = service.sync()

    assert resumo["created"] == 1 and resumo["with_recipe"] == 1
    pedido = next(row for row in erp.orders() if row.get("ifood_order_id") == "pedido-1")
    assert pedido["channel"] == "iFood"
    assert pedido["total"] == 58.0, "o que o cliente pagou no iFood"
    assert pedido["delivery_fee"] == 6.0
    assert pedido["catalog_total"] == 44.0, "o que o cardápio da loja cobraria"
    assert pedido["revenue_conflict"] is True
    assert pedido["cost"] > 0, "a ficha técnica sabe o custo"
    assert pedido["channel_fee"] == round(58.0 * 0.27, 2)
    # Dois copos de 500 ml consomem 0,92 kg de açaí.
    atual = next(row for row in erp.inventory() if row["id"] == "acai")["quantity"]
    assert atual == pytest.approx(saldo - 0.92)


def test_item_sem_produto_mapeado_entra_com_custo_desconhecido(loja):
    """Custo zero mentiria margem de 100%; desconhecido é a verdade."""

    store, erp, service, fake = loja
    conectar(service)
    saldo = next(row for row in erp.inventory() if row["id"] == "acai")["quantity"]
    fake.place("pedido-2", [{"name": "Combo que a loja não cadastrou", "quantity": 1}], amount=35.0)
    fake.emit("evento-2", "pedido-2")

    resumo = service.sync()

    assert resumo["created"] == 1 and resumo["without_recipe"] == 1
    pedido = next(row for row in erp.orders() if row.get("ifood_order_id") == "pedido-2")
    assert pedido["cost"] is None
    assert pedido["total"] == 35.0
    assert pedido["items"] == []
    assert pedido["unmapped_items"] == ["Combo que a loja não cadastrou"]
    assert next(row for row in erp.inventory() if row["id"] == "acai")["quantity"] == saldo
    # A tela precisa saber o que mapear para o custo aparecer.
    assert service.status()["unmapped_items"] == ["Combo que a loja não cadastrou"]


def test_pedido_com_um_item_solto_nao_baixa_estoque_pela_metade(loja):
    """Baixar só o que mapeou daria custo de meio pedido — pior que nenhum."""

    _, erp, service, fake = loja
    conectar(service)
    saldo = next(row for row in erp.inventory() if row["id"] == "acai")["quantity"]
    fake.place("pedido-3", [
        {"name": "Açaí 500 ml", "externalCode": "acai-500", "quantity": 1},
        {"name": "Item fora do cardápio", "quantity": 1},
    ], amount=40.0)
    fake.emit("evento-3", "pedido-3")

    service.sync()

    pedido = next(row for row in erp.orders() if row.get("ifood_order_id") == "pedido-3")
    assert pedido["cost"] is None and pedido["items"] == []
    assert next(row for row in erp.inventory() if row["id"] == "acai")["quantity"] == saldo


def test_o_nome_do_produto_serve_de_ponte_quando_falta_o_codigo(loja):
    """Cardápio antigo não tem externalCode; o nome evita perder o custo."""

    _, erp, service, fake = loja
    conectar(service)
    fake.place("pedido-4", [{"name": "açai 500 ML", "quantity": 1}], amount=25.0)
    fake.emit("evento-4", "pedido-4")

    service.sync()

    pedido = next(row for row in erp.orders() if row.get("ifood_order_id") == "pedido-4")
    assert pedido["cost"] > 0
    assert pedido["items"][0]["product_id"] == "acai-500"


def test_o_mesmo_evento_duas_vezes_nao_cria_dois_pedidos(loja):
    """Reenvio é normal no iFood; faturamento dobrado não pode ser."""

    _, erp, service, fake = loja
    conectar(service)
    fake.place("pedido-5", [{"name": "Açaí 500 ml", "externalCode": "acai-500", "quantity": 1}], amount=29.0)
    fake.emit("evento-5", "pedido-5")
    service.sync()

    fake.events.clear()
    fake.emit("evento-5-bis", "pedido-5", code="CON")
    resumo = service.sync(force=True)

    assert resumo["created"] == 0 and resumo["duplicated"] == 1
    assert len([row for row in erp.orders() if row.get("ifood_order_id") == "pedido-5"]) == 1


def test_cancelamento_no_ifood_devolve_o_insumo(loja):
    """Cancelar sem estornar deixaria a prateleira com um copo a menos."""

    _, erp, service, fake = loja
    conectar(service)
    saldo = next(row for row in erp.inventory() if row["id"] == "acai")["quantity"]
    fake.place("pedido-6", [{"name": "Açaí 500 ml", "externalCode": "acai-500", "quantity": 1}], amount=29.0)
    fake.emit("evento-6", "pedido-6")
    service.sync()

    fake.events.clear()
    fake.emit("evento-6-can", "pedido-6", code="CAN")
    resumo = service.sync(force=True)

    assert resumo["cancelled"] == 1
    pedido = next(row for row in erp.orders() if row.get("ifood_order_id") == "pedido-6")
    assert pedido["status"] == "Cancelado"
    assert next(row for row in erp.inventory() if row["id"] == "acai")["quantity"] == pytest.approx(saldo)


def test_pedido_apenas_recebido_nao_baixa_estoque(loja):
    """PLACED ainda pode ser recusado; descontar insumo ali seria adiantado."""

    _, erp, service, fake = loja
    conectar(service)
    saldo = next(row for row in erp.inventory() if row["id"] == "acai")["quantity"]
    fake.place("pedido-7", [{"name": "Açaí 500 ml", "externalCode": "acai-500", "quantity": 1}], amount=29.0)
    fake.emit("evento-7", "pedido-7", code="PLC")

    resumo = service.sync()

    assert resumo["created"] == 0 and resumo["ignored"] == 1
    assert next(row for row in erp.inventory() if row["id"] == "acai")["quantity"] == saldo
    # Ignorar não é deixar na fila: o evento é confirmado e some.
    assert fake.acknowledged == ["evento-7"]


def test_o_aviso_do_item_solto_some_quando_a_loja_mapeia(loja):
    """Aviso que não some vira ruído e para de ser lido."""

    _, _, service, fake = loja
    conectar(service)
    fake.place("pedido-a", [{"name": "Combo sem cadastro", "quantity": 1}], amount=30.0)
    fake.emit("evento-a", "pedido-a")
    service.sync()
    assert service.status()["unmapped_items"] == ["Combo sem cadastro"]

    # Uma consulta sem pedido nenhum não prova que alguém mapeou o item.
    fake.events.clear()
    service.sync(force=True)
    assert service.status()["unmapped_items"] == ["Combo sem cadastro"]

    fake.place("pedido-b", [{"name": "Açaí 500 ml", "externalCode": "acai-500", "quantity": 1}], amount=30.0)
    fake.emit("evento-b", "pedido-b")
    service.sync(force=True)

    assert service.status()["unmapped_items"] == []


# ----------------------------------------------------------------------
# A fila
# ----------------------------------------------------------------------


def test_o_evento_so_e_confirmado_depois_de_o_pedido_estar_gravado(loja):
    """Confirmar antes de gravar apaga o pedido da fila e perde a venda."""

    _, _, service, fake = loja
    conectar(service)
    fake.place("pedido-8", [{"name": "Açaí 500 ml", "externalCode": "acai-500", "quantity": 1}], amount=29.0)
    fake.emit("evento-8", "pedido-8")

    service.sync()

    ordem = [caminho for _, caminho in fake.calls]
    gravou = ordem.index("/order/v1.0/orders/pedido-8")
    confirmou = ordem.index("/events/v1.0/events/acknowledgment")
    assert gravou < confirmou
    assert fake.acknowledged == ["evento-8"]


def test_pedido_que_falha_fica_na_fila_para_a_proxima_consulta(loja):
    """Perder um pedido é pior que recebê-lo duas vezes."""

    _, erp, service, fake = loja
    conectar(service)
    fake.place("pedido-9", [{"name": "Açaí 500 ml", "externalCode": "acai-500", "quantity": 1}], amount=29.0)
    fake.emit("evento-9", "pedido-9")
    fake.emit("evento-10", "pedido-inexistente")

    resumo = service.sync()

    assert resumo["created"] == 1 and resumo["failed"] == 1
    assert fake.acknowledged == ["evento-9"], "o que falhou não foi confirmado"


def test_sem_pedido_novo_a_consulta_nao_confirma_nada(loja):
    _, _, service, fake = loja
    conectar(service)

    resumo = service.sync()

    assert resumo["events"] == 0 and resumo["created"] == 0
    assert resumo["detail"] == "Nenhum pedido novo."
    assert "/events/v1.0/events/acknowledgment" not in [caminho for _, caminho in fake.calls]


def test_consulta_repetida_antes_de_meio_minuto_e_segurada(loja):
    """O iFood limita a uma consulta por 30 s; insistir atrasa o pedido real."""

    _, _, service, fake = loja
    conectar(service)
    service.sync()
    consultas = [caminho for _, caminho in fake.calls].count("/events/v1.0/events:polling")

    resumo = service.sync()

    assert resumo["skipped"] is True
    assert [caminho for _, caminho in fake.calls].count("/events/v1.0/events:polling") == consultas


def test_a_consulta_leva_o_identificador_da_loja(loja):
    """Sem o filtro, a chave que enxerga várias lojas traria pedido alheio."""

    _, _, service, fake = loja
    conectar(service)

    service.sync()

    consulta = next(cabecalho for (_, caminho), cabecalho in zip(fake.calls, fake.headers)
                    if caminho.endswith("events:polling"))
    assert consulta["x-polling-merchants"] == "loja-1"
    assert consulta["Authorization"] == "Bearer token-de-acesso"


# ----------------------------------------------------------------------
# Token
# ----------------------------------------------------------------------


def test_token_vencido_se_renova_sozinho(loja):
    """Seis horas de validade não podem virar erro na cara do operador."""

    _, _, service, fake = loja
    conectar(service)
    credenciais = service.vault.read(SECRET_NAME)
    credenciais["token_expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    service.vault.write(SECRET_NAME, credenciais)
    fake.token_response = {**fake.token_response, "accessToken": "token-renovado"}

    service.sync()

    assert fake.forms[-1]["grantType"] == "refresh_token"
    assert fake.forms[-1]["refreshToken"] == "token-de-renovacao"
    assert service.vault.read(SECRET_NAME)["access_token"] == "token-renovado"


def test_token_valido_nao_e_renovado_a_toa(loja):
    _, _, service, fake = loja
    conectar(service)
    trocas = [caminho for _, caminho in fake.calls].count("/authentication/v1.0/oauth/token")

    service.sync()

    assert [caminho for _, caminho in fake.calls].count("/authentication/v1.0/oauth/token") == trocas


def test_desconectar_esquece_a_credencial_e_mantem_o_historico(loja):
    """Desligar a integração não pode apagar a venda que já aconteceu."""

    store, erp, service, fake = loja
    conectar(service)
    fake.place("pedido-11", [{"name": "Açaí 500 ml", "externalCode": "acai-500", "quantity": 1}], amount=29.0)
    fake.emit("evento-11", "pedido-11")
    service.sync()

    estado = service.disconnect()

    assert estado["status"] == "desconectado"
    assert store.get(INTEGRATIONS, INTEGRATION_ID) is None
    assert any(row.get("ifood_order_id") == "pedido-11" for row in erp.orders())


def test_falha_do_ifood_fica_registrada_para_a_tela_explicar(loja):
    """Sem isso a tela só diria "não deu"; o operador precisa saber o quê."""

    _, _, service, fake = loja
    conectar(service)
    fake.fail_with["/events/v1.0/events:polling"] = (429, {"message": "too many requests"})

    with pytest.raises(IFoodError, match="limitou as consultas"):
        service.sync()

    assert "limitou as consultas" in service.status()["last_error"]


# ----------------------------------------------------------------------
# Onde o segredo mora
# ----------------------------------------------------------------------


def test_o_segredo_nunca_encosta_no_documento_de_negocio(loja):
    """A separação é o ponto: dado de negócio e credencial em bancos distintos.

    No Firestore o documento fica em texto claro, legível por qualquer coisa
    que alcance o banco e presente em toda exportação dele. Chave, segredo e
    tokens vão para o cofre; o documento guarda só o estado da conexão.
    """

    store, _, service, _ = loja
    conectar(service)

    documento = store.get(INTEGRATIONS, INTEGRATION_ID)
    vazados = sorted(SECRET_FIELDS.intersection(documento))
    assert not vazados, f"campo que autentica gravado junto do negócio: {vazados}"
    assert CLIENT_SECRET not in json_do(documento)

    cofre = service.vault.read(SECRET_NAME)
    assert cofre["client_secret"] == CLIENT_SECRET
    assert cofre["refresh_token"] == "token-de-renovacao"
    # O documento continua sabendo com qual aplicativo está falando.
    assert documento["client_id"] == CLIENT_ID
    assert documento["status"] == STATUS_CONNECTED


def test_o_verificador_e_esquecido_depois_de_usado(loja):
    """Ele serve uma vez; guardá-lo é manter exposto um segredo sem função."""

    _, _, service, _ = loja
    service.start_activation(IFoodActivationStart(client_id=CLIENT_ID, client_secret=CLIENT_SECRET))
    assert service.vault.read(SECRET_NAME)["authorization_code_verifier"] == "verificador-secreto"

    service.confirm_activation(IFoodActivationConfirm(authorization_code="codigo-do-portal"))

    assert service.vault.read(SECRET_NAME)["authorization_code_verifier"] is None


def test_segredo_gravado_por_versao_antiga_e_expurgado(loja):
    """A integração já existiu gravando segredo no documento; sobra tem de sair.

    O estado é remontado com `{**anterior, ...}`, então sem a peneira um campo
    gravado por uma versão antiga do código sobreviveria para sempre.
    """

    store, _, service, _ = loja
    store.put(INTEGRATIONS, INTEGRATION_ID, {
        "id": INTEGRATION_ID, "status": "desconectado", "client_id": "antigo",
        "client_secret": "segredo-que-ficou-para-tras",
        "refresh_token": "token-que-ficou-para-tras",
    })

    service.start_activation(IFoodActivationStart(client_id=CLIENT_ID, client_secret=CLIENT_SECRET))

    documento = store.get(INTEGRATIONS, INTEGRATION_ID)
    assert not SECRET_FIELDS.intersection(documento)
    assert "segredo-que-ficou-para-tras" not in json_do(documento)


def test_desconectar_esvazia_o_cofre(loja):
    """Sobra de credencial é exatamente o que não pode ficar para trás."""

    _, _, service, _ = loja
    conectar(service)

    service.disconnect()

    assert service.vault.read(SECRET_NAME) is None


def test_trocar_de_aplicativo_descarta_o_token_do_anterior(loja):
    """Token antigo pertence a outro aplicativo e não renova mais."""

    _, _, service, _ = loja
    conectar(service)

    service.start_activation(IFoodActivationStart(client_id="outro-cliente-0002",
                                                  client_secret="outro-segredo-0002"))

    cofre = service.vault.read(SECRET_NAME)
    assert cofre.get("refresh_token") is None
    assert cofre["client_secret"] == "outro-segredo-0002"


def test_cofre_local_guarda_le_e_apaga(tmp_path):
    """O cofre de desenvolvimento não é seguro, mas precisa ser correto."""

    from app.repositories.secrets import FileVault

    cofre = FileVault(tmp_path / "cofre" / "secrets.json")

    assert cofre.read("ausente") is None
    cofre.write("ifood", {"client_secret": "x"})
    assert cofre.read("ifood") == {"client_secret": "x"}
    cofre.write("ifood", {"client_secret": "y", "refresh_token": "z"})
    assert cofre.read("ifood") == {"client_secret": "y", "refresh_token": "z"}
    cofre.write("outro", {"a": 1})
    cofre.delete("ifood")
    assert cofre.read("ifood") is None
    assert cofre.read("outro") == {"a": 1}, "apagar um não pode levar o vizinho"
    cofre.delete("ifood")  # apagar duas vezes não pode estourar


def test_cofre_local_ilegivel_nao_derruba_a_aplicacao(tmp_path):
    """Arquivo corrompido vira cofre vazio, e a loja refaz a ativação."""

    from app.repositories.secrets import FileVault

    caminho = tmp_path / "secrets.json"
    caminho.write_text("{isto nao e json", encoding="utf-8")

    assert FileVault(caminho).read("ifood") is None


def test_com_firestore_o_cofre_nunca_e_arquivo():
    """A propriedade que interessa: dado no Firestore, segredo no Secret Manager.

    Um recuo silencioso para arquivo no Cloud Run gravaria a credencial em
    texto claro num disco efêmero — falha pior que a falha, porque parece
    funcionar.
    """

    from app.config import Settings
    from app.repositories.secrets import build_secret_vault

    sem_projeto = Settings(data_mode="demo", firebase_project_id=None)
    with pytest.raises(ValueError, match="FIREBASE_PROJECT_ID"):
        build_secret_vault(sem_projeto, store_mode="firebase")

    local = build_secret_vault(Settings(data_mode="demo"), store_mode="demo")
    assert local.mode == "file"


def json_do(valor) -> str:
    import json

    return json.dumps(valor, ensure_ascii=False, default=str)


# ----------------------------------------------------------------------
# A camada HTTP
# ----------------------------------------------------------------------
#
# O serviço pode estar certo e a rota errada. Aqui o que se prova é o contrato:
# o caminho existe, a recusa de domínio vira 422 com a frase que o operador vai
# ler, e a rota de estado não devolve credencial nenhuma.


@pytest.fixture
def http(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    for chave, valor in {
        "DATA_MODE": "demo", "DEMO_DATA_PATH": str(tmp_path / "store.json"),
        "AUTH_REQUIRED": "false", "DATA_RAW_PATH": str(tmp_path / "raw"),
        "MEDIA_INBOX_PATH": str(tmp_path / "inbox"),
        "MEDIA_LIBRARY_PATH": str(tmp_path / "library"), "MEDIA_SYNC_LEGACY_FILES": "false",
    }.items():
        monkeypatch.setenv(chave, valor)
    get_settings.cache_clear()
    fake = FakeIFood()
    with TestClient(app) as cliente:
        cliente.app.state.ifood.client = IFoodClient(transport=fake, host="https://ifood.test")
        yield cliente, fake
    get_settings.cache_clear()


def test_a_rota_de_estado_responde_desconectado_sem_credencial(http):
    cliente, _ = http

    resposta = cliente.get("/api/integrations/ifood")

    assert resposta.status_code == 200
    assert resposta.json()["status"] == "desconectado"
    assert resposta.json()["connected"] is False


def test_a_ativacao_pela_rota_devolve_o_codigo_e_esconde_o_segredo(http):
    cliente, _ = http

    gerado = cliente.post("/api/integrations/ifood/activation",
                          json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})

    assert gerado.status_code == 200
    assert gerado.json()["user_code"] == "ABCD-1234"
    estado = cliente.get("/api/integrations/ifood").json()
    assert estado["status"] == STATUS_WAITING
    assert CLIENT_SECRET not in resposta_em_texto(estado)


def test_ativacao_completa_e_busca_de_pedidos_pela_rota(http):
    cliente, fake = http
    cliente.post("/api/integrations/ifood/activation",
                 json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    cliente.post("/api/integrations/ifood/activation/confirm", json={"authorization_code": "codigo"})
    fake.place("pedido-http", [{"name": "Açaí 500 ml", "externalCode": "acai-500", "quantity": 1}], amount=31.0)
    fake.emit("evento-http", "pedido-http")

    resposta = cliente.post("/api/integrations/ifood/sync")

    assert resposta.status_code == 200
    assert resposta.json()["created"] == 1
    pedido = next(row for row in cliente.get("/api/orders").json() if row.get("ifood_order_id") == "pedido-http")
    assert pedido["total"] == 31.0 and pedido["cost"] > 0


def test_sincronizar_sem_conexao_vira_422_com_a_frase_do_operador(http):
    cliente, _ = http

    resposta = cliente.post("/api/integrations/ifood/sync")

    assert resposta.status_code == 422
    assert "não está conectado" in resposta.json()["detail"]


def test_a_rota_recusa_chave_curta_demais_para_ser_verdadeira(http):
    cliente, _ = http

    resposta = cliente.post("/api/integrations/ifood/activation",
                            json={"client_id": "abc", "client_secret": "x"})

    assert resposta.status_code == 422


def test_desconectar_pela_rota(http):
    cliente, _ = http
    cliente.post("/api/integrations/ifood/activation",
                 json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    cliente.post("/api/integrations/ifood/activation/confirm", json={"authorization_code": "codigo"})

    resposta = cliente.delete("/api/integrations/ifood")

    assert resposta.status_code == 200
    assert resposta.json()["status"] == "desconectado"


def resposta_em_texto(payload) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


# ----------------------------------------------------------------------
# A tela
# ----------------------------------------------------------------------


def test_a_ativacao_inteira_funciona_no_navegador(tmp_path, monkeypatch):
    """A tela da ativação é nova, e nenhum teste de serviço prova que ela liga.

    O que se percorre aqui é o caminho do operador: abrir Ajustes, informar a
    chave, ver o código que ele vai levar ao portal, colar o código de
    autorização e buscar o primeiro pedido — com a API real e uma loja
    temporária. O navegador não fala com a internet: o transporte do iFood é o
    mesmo dublê dos testes acima e todo pedido externo é bloqueado.
    """

    playwright = pytest.importorskip("playwright.sync_api")
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    for chave, valor in {
        "DATA_MODE": "demo", "DEMO_DATA_PATH": str(tmp_path / "store.json"),
        "AUTH_REQUIRED": "false", "DATA_RAW_PATH": str(tmp_path / "raw"),
        "MEDIA_INBOX_PATH": str(tmp_path / "inbox"),
        "MEDIA_LIBRARY_PATH": str(tmp_path / "library"), "MEDIA_SYNC_LEGACY_FILES": "false",
    }.items():
        monkeypatch.setenv(chave, valor)
    get_settings.cache_clear()
    fake = FakeIFood()
    try:
        with TestClient(app) as client, playwright.sync_playwright() as p:
            client.app.state.ifood.client = IFoodClient(transport=fake, host="https://ifood.test")
            try:
                browser = p.chromium.launch()
            except playwright.Error as error:
                pytest.skip(f"Chromium indisponível: {error}")
            page = browser.new_page(viewport={"width": 1280, "height": 900}, service_workers="block")
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                request = route.request
                url = urlsplit(request.url)
                if url.hostname != "testserver":
                    route.fulfill(status=200, body="", content_type="text/css")
                    return
                response = client.request(
                    request.method, url.path + (f"?{url.query}" if url.query else ""),
                    content=request.post_data_buffer,
                    headers={"content-type": request.headers.get("content-type", "")},
                )
                route.fulfill(status=response.status_code, body=response.content,
                              content_type=response.headers.get("content-type", "application/octet-stream"))

            page.route("**/*", serve)
            page.goto("http://testserver/#settings")
            page.wait_for_function("state.inventory.length > 0", timeout=90000)
            page.wait_for_selector('#ifood-body [data-ifood="start"]', timeout=90000)
            assert page.text_content("#ifood-state") == "Desconectado"

            page.fill('#ifood-body [name="client_id"]', CLIENT_ID)
            page.fill('#ifood-body [name="client_secret"]', CLIENT_SECRET)
            page.click('#ifood-body [data-ifood="start"]')
            page.wait_for_selector(".ifood-code", timeout=90000)
            assert page.text_content(".ifood-code") == "ABCD-1234", "o código precisa estar legível na tela"
            assert page.text_content("#ifood-state") == "Aguardando o portal"

            page.fill('#ifood-body [name="authorization_code"]', "codigo-do-portal")
            page.click('#ifood-body [data-ifood="confirm"]')
            page.wait_for_selector('#ifood-body [data-ifood="sync"]', timeout=90000)
            assert page.text_content("#ifood-state") == "Conectado"
            # O segredo não pode reaparecer na tela por nenhum caminho.
            assert CLIENT_SECRET not in page.content()

            fake.place("pedido-tela", [{"name": "Açaí 500 ml", "externalCode": "acai-500", "quantity": 1}],
                       amount=33.0)
            fake.emit("evento-tela", "pedido-tela")
            page.click('#ifood-body [data-ifood="sync"]')
            page.wait_for_function(
                "!document.getElementById('ifood-body').hasAttribute('aria-busy')", timeout=90000)

            pedido = next(row for row in client.get("/api/orders").json()
                          if row.get("ifood_order_id") == "pedido-tela")
            assert pedido["total"] == 33.0 and pedido["cost"] > 0
            assert not errors, errors
            browser.close()
    finally:
        get_settings.cache_clear()
