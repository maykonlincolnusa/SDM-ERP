"""Integração com a Anota Aí: o cardápio que a loja já mantém lá.

Toda a venda da loja passa pela Anota Aí — balcão, iFood e WhatsApp —, e é lá
que o cardápio é mantido. O ERP até hoje recebia esse movimento por planilha
exportada à mão, e a planilha de pedidos **não traz os itens vendidos**. Essa
ausência é a raiz de tudo o que o painel não consegue responder: sem saber
quais copos saíram, a ficha técnica não baixa insumo, o custo não fecha, a
margem por produto não existe e a previsão não tem o que aprender.

Este módulo trata da ponta que resolve isso na origem: **o cadastro**.

Puxar o cardápio não é encanamento descartável. A loja quer sair da Anota Aí, e
uma integração de pedidos seria cano para uma casa que vai ser demolida — mas o
cardápio é o oposto: trazê-lo popula o catálogo próprio do ERP, que é
justamente o que precisa existir para a saída acontecer. E ele carrega o
`external_id`, o campo que liga o item de lá ao produto daqui. Com essa ponte,
o pedido passa a chegar com produto identificado, e o custo se calcula sozinho.

Três decisões de desenho
------------------------

**A rede fica num ponto só.** `Transport` é injetável, então o teste exercita a
tradução da resposta sem tocar na internet — o mesmo arranjo da integração do
iFood, que já provou que funciona.

**A chave nunca volta para a tela.** Ela entra uma vez, vai para o cofre
(Secret Manager em produção) e de lá só sai para o cabeçalho da chamada. O que
o painel mostra é se está conectado e quando foi a última sincronização.

**Nada é criado sem o gestor mandar.** A sincronização primeiro *relata*: o que
casou, o que existe lá e não aqui, o que existe aqui e não lá. Criar produto no
ERP é um segundo passo, explícito. Cardápio de loja tem item sazonal, teste
abandonado e nome duplicado; despejar tudo dentro do ERP sem conferência
encheria o catálogo de coisa que ninguém vende.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Protocol

from app.repositories.secrets import SecretVault
from app.repositories.store import Store
from app.schemas import OrderCreate, OrderLine
from app.services.erp import FUSO_LOJA, ERPService

logger = logging.getLogger("uvicorn.error")

# Hosts confirmados vivos em 18/08/2026 e reconferidos em 11/09/2026. Os nomes
# `api-orders`, `api-order` e `api-pedidos` nao resolvem DNS: nao existem.
MENU_HOST = "https://api-menu.anota.ai"
PARTNER_HOST = "https://api-parceiros.anota.ai"

# O caminho do cardapio. A resposta tem a forma
# {success, message, categories[{title, id, itens[...], is_additional}]}.
MENU_PATH = "/partnerauth/v2/nm-category/rest/simple-item/export/v2"

# A fila de avisos de pedido novo. Nao e historico: ela resolve do momento da
# ativacao em diante, e o passado continua vindo da planilha exportada.
PING_LIST_PATH = "/partnerauth/ping/list"
PING_GET_PATH = "/partnerauth/ping/get/{order_id}"

SECRET_NAME = "anotaai-credentials"
STATE_ID = "anotaai"

# Canal de venda. Todo pedido da loja passa pela Anota Ai, entao a origem real
# (balcao, iFood, WhatsApp) vem dentro do pedido e nao e este nome.
INTEGRATION_ID = "anota-ai"


ORDER_LINKS = "anotaai_order_links"

# O aplicativo do entregador consulta a própria fila periodicamente. Limitar a
# busca evita que vários celulares abertos façam uma chamada à Anota Aí a cada
# renderização, sem deixar um pedido novo esperando alguém abrir o painel.
DISPATCH_SYNC_INTERVAL_SECONDS = 20

# Todo pedido da loja passa pela Anota Ai, entao "Anota Ai" nao e canal: e o
# encanamento. O canal e de onde o cliente veio, e ele vem dentro do pedido.
CANAL_PADRAO = "Balcao"
CANAIS_CONHECIDOS = (
    ("iFood", ("ifood",)),
    ("WhatsApp", ("whats", "wpp")),
    ("Delivery", ("menu", "link", "site", "cardapio", "delivery")),
    ("Balcao", ("pdv", "balcao", "caixa", "loja")),
)

# Onde procurar cada valor na resposta de `/ping/get`.
#
# A forma dessa resposta nunca pode ser conferida: a chave de integracao da loja
# nao chegou a funcionar, e sem ela nao ha resposta real para ler. Entao o
# tradutor procura em vez de assumir — e o que ele nao acha, ele declara. Assim
# que a primeira chamada de verdade acontecer, `probe` mostra a estrutura e
# estas listas viram um caminho so, escrito sobre o fato.
CAMINHOS_TOTAL = ("total", "valorTotal", "valor_total", "orderTotal", "amount",
                  "payment.total", "values.total", "price.total", "totals.total")
CAMINHOS_FRETE = ("deliveryFee", "delivery_fee", "taxaEntrega", "frete", "delivery.fee",
                  "delivery.price", "values.deliveryFee", "payment.deliveryFee")
CAMINHOS_DESCONTO = ("discount", "desconto", "values.discount", "payment.discount")
CAMINHOS_ITENS = ("itens", "items", "produtos", "products", "cart.items", "order.items")
CAMINHOS_NUMERO = ("displayId", "display_id", "number", "numero", "orderNumber", "code", "shortId")
CAMINHOS_DATA = ("createdAt", "created_at", "date", "data", "orderDate", "dataPedido")
CAMINHOS_CLIENTE = ("customer.name", "client.name", "cliente.nome", "customerName", "nome")
CAMINHOS_PAGAMENTO = ("payment.method", "paymentMethod", "formaPagamento", "payment.name", "pagamento")
CAMINHOS_ENDERECO = ("delivery.address", "address", "endereco", "customer.address", "deliveryAddress")
CAMINHOS_TIPO = ("type", "tipo", "orderType", "deliveryType", "isDelivery", "delivery.type", "retirada")
CAMINHOS_CANAL = ("salesChannel", "origin", "origem", "channel", "canal", "from", "source")


def _ler(dados: Any, *caminhos: str) -> Any:
    """O primeiro caminho que existir, entre varios plausiveis.

    Os caminhos aceitam ponto para descer um nivel. Procurar em vez de assumir
    e o que permite escrever o tradutor antes de ver a resposta — e o que ele
    nao acha volta como "nao entendi", nunca como zero.
    """

    for caminho in caminhos:
        atual: Any = dados
        for parte in caminho.split("."):
            if not isinstance(atual, dict) or parte not in atual:
                atual = None
                break
            atual = atual[parte]
        if atual not in (None, "", [], {}):
            return atual
    return None


def _inteiro(valor: Any) -> int:
    try:
        return max(0, int(float(valor or 0)))
    except (TypeError, ValueError):
        return 0


class AnotaAiError(ValueError):
    """Falha que a tela precisa mostrar como texto, não como erro 500."""


class Transport(Protocol):
    """A ida à rede, isolada num ponto só para o teste poder substituí-la."""

    def __call__(
        self, method: str, url: str, *, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> tuple[int, Any]: ...


def _urllib_transport(
    method: str, url: str, *, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as error:  # resposta da Anota Ai, nao falha de rede
        return error.code, _decode(error.read())
    except urllib.error.URLError as error:
        raise AnotaAiError("Não foi possível falar com a Anota Aí. Verifique a conexão e tente de novo.") from error
    except TimeoutError as error:
        raise AnotaAiError("A Anota Aí demorou demais para responder. Tente de novo em instantes.") from error


def _decode(raw: bytes | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _minutos_vencido(chave: str) -> int | None:
    """Ha quantos minutos este JWT venceu, ou None se nao for um JWT.

    Nao valida assinatura nem confia no conteudo: le `exp` so para separar
    "token vencido" de "token errado", que a API responde com a mesma frase.
    """

    partes = chave.split(".")
    if len(partes) != 3:
        return None
    try:
        corpo = partes[1]
        dados = json.loads(base64.urlsafe_b64decode(corpo + "=" * (-len(corpo) % 4)))
        expira = float(dados["exp"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None
    return int((datetime.now(timezone.utc).timestamp() - expira) / 60)


def _fold(value: Any) -> str:
    """Sem acento, sem pontuação e em minúsculas, para casar nome com nome."""

    texto = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


class AnotaAiClient:
    """As chamadas HTTP, e só elas."""

    def __init__(self, transport: Transport | None = None, timeout: float = 25.0):
        self.transport = transport or _urllib_transport
        self.timeout = timeout

    def catalog(self, key: str) -> dict[str, Any]:
        return self._get(MENU_HOST, MENU_PATH, key)

    def order_queue(self, key: str) -> dict[str, Any]:
        return self._get(PARTNER_HOST, PING_LIST_PATH, key)

    def order(self, key: str, order_id: str) -> dict[str, Any]:
        caminho = PING_GET_PATH.format(order_id=urllib.parse.quote(str(order_id), safe=""))
        return self._get(PARTNER_HOST, caminho, key)

    # ------------------------------------------------------------------

    def _get(self, host: str, path: str, key: str) -> dict[str, Any]:
        status, payload = self.transport(
            "GET", f"{host}{path}",
            # A chave vai direto no cabeçalho, sem "Bearer" — é assim que a
            # Anota Aí autentica, e com "Bearer" ela devolve 401.
            headers={"Authorization": key, "Accept": "application/json"},
            body=None, timeout=self.timeout,
        )
        return self._ensure(status, payload, path)

    @staticmethod
    def _ensure(status: int, payload: Any, path: str) -> dict[str, Any]:
        if status == 403:
            raise AnotaAiError("A Anota Aí não recebeu a chave. Refaça a conexão em Ajustes.")
        if status == 401:
            raise AnotaAiError(
                "A Anota Aí recusou a chave. A chave de integração fica no painel deles, "
                "em Configurações → Integrações — não serve a que aparece no endereço do navegador."
            )
        if status == 429:
            raise AnotaAiError("A Anota Aí limitou as consultas. Aguarde um minuto e tente de novo.")
        if status == 404:
            raise AnotaAiError(
                "A Anota Aí não reconheceu este endereço. Pode ser que a integração ainda não "
                "esteja liberada para a loja — o suporte deles precisa habilitar."
            )
        if status >= 400:
            detalhe = ""
            if isinstance(payload, dict):
                detalhe = str(payload.get("err") or payload.get("message") or "")
            logger.warning("anotaai: %s respondeu %s", path, status)
            raise AnotaAiError(f"A Anota Aí recusou a chamada ({status}). {detalhe}".strip())
        if not isinstance(payload, dict):
            raise AnotaAiError("A Anota Aí devolveu uma resposta que não dá para interpretar.")
        return payload


class AnotaAiService:
    def __init__(self, store: Store, erp: ERPService, vault: SecretVault, client: AnotaAiClient | None = None):
        self.store = store
        self.erp = erp
        self.vault = vault
        self.client = client or AnotaAiClient()
        # A tela de rua pode estar aberta no celular de mais de um entregador.
        # Só uma consulta à fila externa deve sair por processo; quem chegar
        # enquanto ela está em andamento recebe a fila local imediatamente.
        self._dispatch_sync_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Conexao
    # ------------------------------------------------------------------

    def _key(self) -> str:
        chave = (self.vault.read(SECRET_NAME) or {}).get("integration_key")
        if not chave:
            raise AnotaAiError("A Anota Aí ainda não está conectada. Informe a chave de integração em Ajustes.")
        return str(chave)

    def _state(self) -> dict[str, Any]:
        return self.store.get("integrations", STATE_ID) or {"id": STATE_ID}

    def _remember(self, **campos: Any) -> dict[str, Any]:
        estado = {**self._state(), **campos}
        self.store.put("integrations", STATE_ID, estado)
        return estado

    def status(self) -> dict[str, Any]:
        """O que a tela mostra. A chave nunca aparece aqui."""

        estado = self._state()
        chave = (self.vault.read(SECRET_NAME) or {}).get("integration_key")
        conectado = bool(chave)
        # Uma chave guardada nao e uma chave viva. Se o que a loja colou foi um
        # token de sessao de 15 minutos, ele morreu no cofre — e a tela dizer
        # "conectado" transforma isso em "a sincronizacao nao traz nada" sem
        # ninguem entender por que.
        vencido = _minutos_vencido(chave) if conectado else None
        return {
            "connected": conectado,
            "key_expired": bool(vencido is not None and vencido > 0),
            "key_expired_minutes": vencido if (vencido is not None and vencido > 0) else None,
            "last_order_sync": estado.get("last_order_sync"),
            "last_order_result": estado.get("last_order_result"),
            "last_catalog_sync": estado.get("last_catalog_sync"),
            "last_catalog_result": estado.get("last_catalog_result"),
            "last_error": estado.get("last_error"),
        }

    def connect(self, integration_key: str) -> dict[str, Any]:
        """Guarda a chave e confirma na hora que ela funciona.

        Confirmar agora e nao depois importa: uma chave errada guardada em
        silencio vira "a sincronizacao nao traz nada" tres dias depois, e ai
        ninguem lembra que o problema foi a colagem.
        """

        chave = (integration_key or "").strip()
        if len(chave) < 8:
            raise AnotaAiError("A chave de integração parece curta demais. Copie o valor inteiro do painel.")

        # Token vencido e recusado com a mesma frase de token errado, e a
        # diferenca entre as duas coisas e enorme: uma se resolve copiando de
        # novo, a outra so com o suporte da Anota Ai. Quando o valor colado e um
        # JWT, da para dizer qual das duas antes de gastar a chamada.
        vencido = _minutos_vencido(chave)
        if vencido is not None and vencido > 0:
            raise AnotaAiError(
                f"Este token expirou há {vencido} minuto(s) — ele vale só 15 minutos a partir do "
                "momento em que aparece no painel. Copie um novo e cole aqui em seguida, sem "
                "fechar a tela no meio."
            )

        resposta = self.client.catalog(chave)
        if not resposta.get("success", True):
            raise AnotaAiError(str(resposta.get("message") or "A Anota Aí recusou a chave."))

        self.vault.write(SECRET_NAME, {"integration_key": chave})
        categorias = resposta.get("categories") or []
        self._remember(connected_at=self._agora().isoformat(), last_error=None)
        self.erp._audit(self.store, "integration", STATE_ID, "conectada", "Anota Aí conectada pela chave de integração")
        return {
            "connected": True,
            "categories": len(categorias),
            "items": sum(len(linha.get("itens") or []) for linha in categorias),
        }

    def disconnect(self) -> dict[str, Any]:
        self.vault.delete(SECRET_NAME)
        self._remember(connected_at=None)
        self.erp._audit(self.store, "integration", STATE_ID, "desconectada", "Anota Aí desconectada")
        return {"connected": False}

    # ------------------------------------------------------------------
    # O cardastro: o cardapio que a loja mantem la
    # ------------------------------------------------------------------

    def catalog_preview(self) -> dict[str, Any]:
        """Compara o cardápio da Anota Aí com o do ERP, sem gravar nada.

        Relatar antes de criar é o ponto inteiro. Cardápio de loja acumula item
        sazonal, teste abandonado e nome repetido; despejar tudo dentro do ERP
        sem conferência encheria o catálogo de coisa que ninguém vende — e cada
        produto inventado aqui vira uma ficha técnica que alguém vai ter de
        preencher ou apagar depois.
        """

        resposta = self.client.catalog(self._key())
        categorias = [linha for linha in (resposta.get("categories") or []) if isinstance(linha, dict)]

        produtos = self.store.list("products")
        por_external = {str(p["id"]): p for p in produtos}
        por_nome = {_fold(p.get("name")): p for p in produtos}

        casados: list[dict[str, Any]] = []
        novos: list[dict[str, Any]] = []
        complementos: list[dict[str, Any]] = []
        vistos: set[str] = set()

        for categoria in categorias:
            for item in categoria.get("itens") or []:
                if not isinstance(item, dict):
                    continue
                linha = {
                    "anota_id": str(item.get("id") or ""),
                    "name": str(item.get("title") or "").strip(),
                    "price": _dinheiro(item.get("price")),
                    "category": str(categoria.get("title") or "").strip(),
                    "external_id": str(item.get("external_id") or "") or None,
                    # `out` marca o item fora do ar no cardapio deles.
                    "unavailable": bool(item.get("out")),
                }
                if not linha["name"]:
                    continue
                # Complemento nao e produto: ele entra no ERP como adicional, e
                # misturar os dois criaria "Granola" como copo vendavel.
                if categoria.get("is_additional"):
                    complementos.append(linha)
                    continue

                # O `external_id` e a ponte oficial; o nome e a ponte que sobra
                # enquanto ninguem preencheu o external_id la.
                produto = por_external.get(linha["external_id"] or "") or por_nome.get(_fold(linha["name"]))
                if produto:
                    linha["product_id"] = produto["id"]
                    linha["product_name"] = produto["name"]
                    linha["product_price"] = produto.get("price")
                    linha["price_differs"] = abs((produto.get("price") or 0) - linha["price"]) >= 0.01
                    linha["linked_by"] = "external_id" if linha["external_id"] in por_external else "nome"
                    casados.append(linha)
                    vistos.add(produto["id"])
                else:
                    novos.append(linha)

        # Produto que existe aqui e nao la: ou saiu do cardapio deles, ou tem
        # nome diferente. Os dois casos sao trabalho de cadastro, e sumir com
        # eles esconderia o produto que para de vender sem ninguem notar.
        so_no_erp = [
            {"product_id": p["id"], "name": p["name"], "price": p.get("price")}
            for p in produtos
            if p["id"] not in vistos and p.get("active") is not False
        ]

        return {
            "categories": len(categorias),
            "matched": sorted(casados, key=lambda linha: linha["name"]),
            "new": sorted(novos, key=lambda linha: linha["name"]),
            "addons": sorted(complementos, key=lambda linha: linha["name"]),
            "only_here": sorted(so_no_erp, key=lambda linha: str(linha["name"])),
            "readings": self._leituras(casados, novos, so_no_erp),
        }

    def _leituras(self, casados, novos, so_no_erp) -> list[dict[str, str]]:
        leituras: list[dict[str, str]] = []
        sem_ponte = [linha for linha in casados if linha["linked_by"] == "nome"]
        if sem_ponte:
            leituras.append({
                "type": "info",
                "title": f"{len(sem_ponte)} produto(s) casaram pelo nome, não pelo identificador",
                "description": (
                    "Funciona, mas quebra no dia em que alguém renomear o item lá. O campo "
                    "'id externo' no cardápio da Anota Aí é a ligação que não depende do nome."
                ),
            })
        divergentes = [linha for linha in casados if linha.get("price_differs")]
        if divergentes:
            leituras.append({
                "type": "attention",
                "title": f"{len(divergentes)} produto(s) com preço diferente entre os dois",
                "description": (
                    "Quem cobra do cliente é a Anota Aí. Enquanto os preços divergirem, a margem "
                    "calculada aqui não é a margem que a loja está praticando."
                ),
            })
        if novos:
            leituras.append({
                "type": "opportunity",
                "title": f"{len(novos)} item(ns) vendidos lá e sem cadastro aqui",
                "description": (
                    "Cada um deles é um pedido que chega sem produto identificado, e por isso sem "
                    "custo e sem margem. Criar o cadastro é o que fecha essa conta."
                ),
            })
        if so_no_erp:
            leituras.append({
                "type": "info",
                "title": f"{len(so_no_erp)} produto(s) existem aqui e não aparecem no cardápio de lá",
                "description": "Ou saíram do cardápio, ou têm nome diferente nos dois lugares.",
            })
        return leituras

    def import_catalog(self, product_ids: list[str] | None = None) -> dict[str, Any]:
        """Cria no ERP os itens escolhidos do cardápio da Anota Aí.

        O produto nasce com nome, preço e categoria — e **sem ficha técnica**.
        Inventar receita seria inventar custo, e custo inventado é pior que
        custo ausente: ele se espalha por margem, preço sugerido e promoção com
        cara de número calculado. A ficha fica para a loja preencher, e até lá
        o produto aparece como "sem custo" em vez de mentir.
        """

        previa = self.catalog_preview()
        escolhidos = set(product_ids or [])
        candidatos = [
            linha for linha in previa["new"]
            if not escolhidos or linha["anota_id"] in escolhidos
        ]
        if not candidatos:
            raise AnotaAiError("Nenhum item novo para importar.")

        from app.schemas import ProductCreate

        criados: list[dict[str, Any]] = []
        recusados: list[dict[str, str]] = []
        for linha in candidatos:
            # Preco zero e item de vitrine no cardapio deles — combo montado,
            # secao, "a partir de". Ele nao e um produto vendavel, e criar com
            # preco inventado poluiria a margem de todo o resto.
            if linha["price"] <= 0:
                recusados.append({"name": linha["name"], "reason": "sem preço no cardápio da Anota Aí"})
                continue
            try:
                produto = self.erp.create_product(ProductCreate(
                    name=linha["name"][:80],
                    price=linha["price"],
                    category=(linha["category"] or "Importado")[:40].ljust(2, "."),
                    line="Classico",
                    recipe=[],
                    active=not linha["unavailable"],
                ))
            except ValueError as erro:
                # Nome repetido e o caso comum: o produto ja existe com outro
                # nome normalizado. Recusar um nao pode derrubar os outros.
                recusados.append({"name": linha["name"], "reason": str(erro)})
                continue
            criados.append({"product_id": produto["id"], "name": produto["name"], "anota_id": linha["anota_id"]})

        self._remember(last_catalog_sync=self._agora().isoformat(), last_catalog_result={"created": len(criados)})
        self.erp._audit(
            self.store, "integration", STATE_ID, "cardápio importado",
            f"{len(criados)} produto(s) criados a partir do cardápio da Anota Aí",
            {"produtos": [linha["name"] for linha in criados][:20]},
        )
        return {
            "created": criados,
            "rejected": recusados,
            "pending_recipe": len(criados),
            "detail": (
                f"{len(criados)} produto(s) criados sem ficha técnica. Enquanto a ficha não for "
                "preenchida, eles aparecem sem custo — que é a verdade, e não um número inventado."
            ),
        }

    # ------------------------------------------------------------------
    # O pedido: da fila da Anota Ai ate a mao do entregador
    #
    # A fila `/ping/list` e aviso de pedido novo, nao historico: ela resolve do
    # momento da ativacao em diante, e o passado continua vindo da planilha.
    # Os dois caminhos convivem de proposito, e o id e o que os concilia —
    # `anota-ai-{numero}-{data}` e o mesmo esquema que o importador usa, entao
    # o pedido que ja entrou pela API e pulado quando a planilha da semana
    # chegar. Nada do que existia foi retirado.
    #
    # A forma da resposta de `/ping/get` nunca pode ser conferida: a chave de
    # integracao da loja nao chegou a funcionar, e sem ela nao ha resposta real
    # para ler. Entao o tradutor aqui **procura** o valor em varios caminhos
    # plausiveis em vez de assumir um — e, quando nao acha, diz o que nao
    # entendeu e guarda a resposta crua, em vez de inventar. E por isso que
    # existe o `probe`: na primeira chamada de verdade ele mostra a estrutura,
    # e a traducao passa a ser escrita sobre o fato.
    # ------------------------------------------------------------------

    def probe(self) -> dict[str, Any]:
        """Mostra a forma crua de um aviso da fila e de um pedido.

        Serve uma vez: o suficiente para a traducao deixar de ser hipotese.
        """

        chave = self._key()
        fila = self.client.order_queue(chave)
        avisos = ((fila.get("info") or {}).get("docs") or [])
        if not avisos:
            return {
                "queue_shape": sorted((fila.get("info") or {}).keys()),
                "orders_waiting": 0,
                "detail": "A fila está vazia agora. Faça um pedido de teste na Anota Aí e chame de novo.",
            }
        primeiro = avisos[0]
        pedido_id = str(primeiro.get("_id") or primeiro.get("id") or "")
        detalhe = self.client.order(chave, pedido_id) if pedido_id else {}
        return {
            "orders_waiting": len(avisos),
            "queue_item": primeiro,
            "order_keys": sorted(detalhe.keys()),
            # A resposta inteira, para a traducao ser escrita sobre ela. E dado
            # de pedido da propria loja, nao credencial.
            "order_sample": detalhe,
        }

    def sync(self, limit: int = 40) -> dict[str, Any]:
        """Traz os pedidos novos da Anota Aí e os põe na fila do entregador."""

        chave = self._key()
        fila = self.client.order_queue(chave)
        avisos = ((fila.get("info") or {}).get("docs") or [])
        resumo: dict[str, Any] = {
            "queued": len(avisos), "created": 0, "skipped": 0,
            "without_items": 0, "unreadable": [],
        }

        for aviso in avisos[:limit]:
            pedido_id = str(aviso.get("_id") or aviso.get("id") or "")
            if not pedido_id or self.store.get(ORDER_LINKS, pedido_id):
                resumo["skipped"] += 1
                continue
            try:
                detalhe = self.client.order(chave, pedido_id)
            except AnotaAiError as erro:
                resumo["unreadable"].append({"id": pedido_id, "reason": str(erro)})
                continue
            self._registrar(pedido_id, detalhe, resumo)

        self._remember(last_order_sync=self._agora().isoformat(), last_order_result=resumo)
        return resumo

    def sync_for_dispatch(self) -> dict[str, Any] | None:
        """Atualiza a fila que o entregador vê, sem tornar a rua dependente da API.

        A tela do entregador é consultada automaticamente; se a Anota Aí estiver
        indisponível, ela continua mostrando os pedidos já recebidos. A falha é
        guardada para aparecer nos Ajustes, onde a loja pode renovar a chave.
        """

        if not self._dispatch_sync_lock.acquire(blocking=False):
            return None
        try:
            return self._sync_for_dispatch()
        finally:
            self._dispatch_sync_lock.release()

    def _sync_for_dispatch(self) -> dict[str, Any] | None:
        """Executa a sincronização já reservada por `sync_for_dispatch`."""

        estado = self.status()
        if not estado["connected"] or estado["key_expired"]:
            return None

        ultimo = estado.get("last_order_sync")
        if ultimo:
            try:
                momento = datetime.fromisoformat(str(ultimo))
                if momento.tzinfo is None:
                    momento = momento.replace(tzinfo=FUSO_LOJA)
                if (self._agora() - momento.astimezone(FUSO_LOJA)).total_seconds() < DISPATCH_SYNC_INTERVAL_SECONDS:
                    return None
            except (TypeError, ValueError):
                # Um estado antigo ou editado não deve impedir a próxima busca.
                pass

        try:
            resultado = self.sync()
        except AnotaAiError as erro:
            self._remember(last_error=str(erro))
            logger.warning("anotaai: não foi possível atualizar a fila do entregador: %s", erro)
            return None
        self._remember(last_error=None)
        return resultado

    def _registrar(self, pedido_id: str, detalhe: dict[str, Any], resumo: dict[str, Any]) -> None:
        total = _dinheiro(_ler(detalhe, *CAMINHOS_TOTAL))
        if total <= 0:
            # Sem o valor, o pedido nao pode virar venda: um pedido de R$ 0
            # entraria no faturamento como desconto que nunca existiu.
            resumo["unreadable"].append({
                "id": pedido_id,
                "reason": "Não encontrei o valor do pedido nesta resposta.",
                "keys": sorted(detalhe.keys()),
            })
            return

        momento = self._momento(_ler(detalhe, *CAMINHOS_DATA)) or self._agora()
        numero = str(_ler(detalhe, *CAMINHOS_NUMERO) or pedido_id[:8])
        identificador = f"{INTEGRATION_ID}-{numero}-{momento.date().isoformat()}"
        if self.store.get("orders", identificador):
            resumo["skipped"] += 1
            self.store.put(ORDER_LINKS, pedido_id, {"id": pedido_id, "order_id": identificador})
            return

        frete = _dinheiro(_ler(detalhe, *CAMINHOS_FRETE))
        linhas, faltando = self._traduzir_itens(detalhe)
        entrega = self._e_entrega(detalhe)

        pedido = {
            "id": identificador,
            "number": numero,
            "created_at": momento.isoformat(),
            "registered_at": self._agora().isoformat(),
            # O mesmo `source` do importador de planilha: os dois caminhos
            # descrevem o mesmo pedido, e separá-los dividiria o faturamento da
            # Anota Aí em dois canais que ninguém pediu.
            "source": INTEGRATION_ID,
            "source_channel": str(_ler(detalhe, *CAMINHOS_CANAL) or "Anota Aí"),
            "anota_order_id": pedido_id,
            "channel": self._canal_do_pedido(detalhe),
            "payment_method": str(_ler(detalhe, *CAMINHOS_PAGAMENTO) or "Anota Aí")[:30],
            "customer_name": (str(_ler(detalhe, *CAMINHOS_CLIENTE) or "").strip() or None),
            "discount": _dinheiro(_ler(detalhe, *CAMINHOS_DESCONTO)),
            "is_delivery": entrega,
            # Limitado ao total pela mesma razao do importador: subsidio de
            # marketplace pode deixar o frete maior que o recebido, e isso
            # geraria receita de produto negativa.
            "delivery_fee": min(frete, total) if entrega else 0.0,
            "courier_id": None,
            # O destino, que e o que faz o mapa do entregador funcionar.
            "delivery_address": self._endereco(detalhe),
            "delivery_zone_id": None,
            "total": total,
            "cost": None,
            "channel_fee": 0.0,
            "status": "Concluido",
            "items": linhas,
            "unmapped_items": faltando,
        }

        if linhas and not faltando:
            # Com todo item identificado, o pedido entra pela porta normal: a
            # ficha tecnica baixa o insumo e o custo deixa de ser desconhecido.
            try:
                criado = self.erp.create_order(OrderCreate(
                    items=[OrderLine(product_id=linha["product_id"], quantity=linha["quantity"]) for linha in linhas],
                    channel=pedido["channel"],
                    payment_method=pedido["payment_method"],
                    customer_name=pedido["customer_name"],
                    is_delivery=entrega,
                    delivery_fee=pedido["delivery_fee"],
                    delivery_address=pedido["delivery_address"],
                    occurred_at=momento,
                ))
                catalogo = criado["total"]
                criado.update({
                    **{k: v for k, v in pedido.items() if k in (
                        "source", "source_channel", "anota_order_id", "number", "unmapped_items")},
                    "total": total,
                    "catalog_total": catalogo,
                    "revenue_conflict": abs(catalogo - total) >= 0.01,
                })
                self.store.delete("orders", criado["id"])
                criado["id"] = identificador
                self.store.put("orders", identificador, criado)
            except ValueError as erro:
                resumo["unreadable"].append({"id": pedido_id, "reason": str(erro)})
                return
        else:
            # Sem produto identificado nao ha ficha tecnica, e um custo zero
            # mentiria margem de 100%. O pedido entra com o faturamento certo e
            # o custo declarado desconhecido, como o importador ja faz.
            self.store.put("orders", identificador, pedido)
            resumo["without_items"] += 1

        self.store.put(ORDER_LINKS, pedido_id, {
            "id": pedido_id, "order_id": identificador, "linked_at": self._agora().isoformat(),
        })
        resumo["created"] += 1

    # ------------------------------------------------------------------

    def _traduzir_itens(self, detalhe: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        """Liga cada item do pedido a um produto do ERP.

        A ponte oficial e o `external_id` do cardapio; o nome e a que sobra
        enquanto ninguem preencheu o external_id la. Nome que nao casa nao vira
        palpite: ele volta na lista de pendencias e o pedido fica sem custo,
        porque meio custo apresentado como custo inteiro e pior que nenhum.
        """

        brutos = _ler(detalhe, *CAMINHOS_ITENS) or []
        if not isinstance(brutos, list):
            return [], []

        produtos = {linha["id"]: linha for linha in self.store.list("products")}
        por_nome = {_fold(linha["name"]): linha for linha in produtos.values()}

        linhas: list[dict[str, Any]] = []
        faltando: list[str] = []
        for bruto in brutos:
            if not isinstance(bruto, dict):
                continue
            nome = str(_ler(bruto, "title", "name", "nome", "produto", "description") or "").strip()
            quantidade = _inteiro(_ler(bruto, "quantity", "qtd", "quantidade", "amount", "qty"))
            if not nome or quantidade <= 0:
                continue
            externo = str(_ler(bruto, "external_id", "externalId", "externalCode", "sku") or "")
            produto = produtos.get(externo) or por_nome.get(_fold(nome))
            if produto:
                linhas.append({"product_id": produto["id"], "name": produto["name"], "quantity": quantidade})
            else:
                faltando.append(nome)
        return linhas, faltando

    def _e_entrega(self, detalhe: dict[str, Any]) -> bool:
        """Entrega ou retirada. Frete positivo reforca, como no importador."""

        bruto = _ler(detalhe, *CAMINHOS_TIPO)
        if isinstance(bruto, bool):
            return bruto
        texto = _fold(bruto)
        if texto:
            if any(p in texto for p in ("retirada", "retirado", "takeout", "balcao", "pickup", "mesa")):
                return False
            if any(p in texto for p in ("delivery", "entrega")):
                return True
        return _dinheiro(_ler(detalhe, *CAMINHOS_FRETE)) > 0

    def _endereco(self, detalhe: dict[str, Any]) -> str | None:
        """O endereco em uma linha, para o mapa do entregador abrir direto."""

        bruto = _ler(detalhe, *CAMINHOS_ENDERECO)
        if isinstance(bruto, str):
            return bruto.strip()[:160] or None
        if isinstance(bruto, dict):
            partes = [
                str(bruto.get(campo) or "").strip()
                for campo in ("street", "logradouro", "rua", "address", "number", "numero",
                              "complement", "complemento", "neighborhood", "bairro", "district")
            ]
            junto = ", ".join(parte for parte in partes if parte)
            return junto[:160] or None
        return None

    def _canal_do_pedido(self, detalhe: dict[str, Any]) -> str:
        """A origem real — balcao, iFood, WhatsApp — que vem dentro do pedido.

        Todo pedido da loja passa pela Anota Ai, entao "Anota Ai" nao e canal:
        e o encanamento. O canal e de onde o cliente veio, e o cadastro precisa
        te-lo para a comissao sair certa.
        """

        bruto = _fold(_ler(detalhe, *CAMINHOS_CANAL))
        for nome, marcas in CANAIS_CONHECIDOS:
            if any(marca in bruto for marca in marcas):
                self._garantir_canal(nome)
                return nome
        self._garantir_canal(CANAL_PADRAO)
        return CANAL_PADRAO

    def _garantir_canal(self, nome: str) -> None:
        if any(linha["name"] == nome for linha in self.erp.channels()):
            return
        identificador = f"channel-{_fold(nome).replace(' ', '-')}"
        self.store.put("channels", identificador, {
            "id": identificador, "name": nome, "fee_percent": 0.0, "fee_fixed": 0.0,
            "active": True, "note": "Criado ao receber o primeiro pedido da Anota Aí.",
        })

    def _momento(self, valor: Any) -> datetime | None:
        if not valor:
            return None
        texto = str(valor).replace("Z", "+00:00")
        for formato in (None, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                momento = datetime.fromisoformat(texto) if formato is None else datetime.strptime(texto, formato)
            except (TypeError, ValueError):
                continue
            if momento.tzinfo is None:
                momento = momento.replace(tzinfo=FUSO_LOJA)
            # Relogio adiantado do outro lado nao pode virar pedido no futuro.
            return min(momento.astimezone(FUSO_LOJA), self._agora())
        return None

    # ------------------------------------------------------------------

    @staticmethod
    def _agora() -> datetime:
        return datetime.now(timezone.utc).astimezone(FUSO_LOJA)


def _dinheiro(valor: Any) -> float:
    try:
        return round(max(0.0, float(valor or 0)), 2)
    except (TypeError, ValueError):
        return 0.0
