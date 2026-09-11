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

import json
import logging
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Protocol

from app.repositories.secrets import SecretVault
from app.repositories.store import Store
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
        conectado = bool((self.vault.read(SECRET_NAME) or {}).get("integration_key"))
        return {
            "connected": conectado,
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

    @staticmethod
    def _agora() -> datetime:
        return datetime.now(timezone.utc).astimezone(FUSO_LOJA)


def _dinheiro(valor: Any) -> float:
    try:
        return round(max(0.0, float(valor or 0)), 2)
    except (TypeError, ValueError):
        return 0.0
