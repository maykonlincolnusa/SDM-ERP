"""Integração com o iFood: ativação por código e entrada do pedido no ERP.

Por que uma integração de verdade aqui, e não mais um importador de planilha:
o relatório exportado chega sem itens e sem hora, então ele sabe o faturamento
e não sabe o custo. O pedido que vem pela API chega item a item, no instante em
que acontece — é o que permite a ficha técnica baixar o insumo e dizer a margem
daquela venda. O iFood também é o único canal que sobrevive à saída da Anota
Aí, então o encanamento construído aqui não é descartável.

O caminho de ativação é o de "aplicativo distribuído": a loja não digita
segredo nenhum numa tela do iFood. O ERP pede um código, a loja leva esse
código ao Portal do Parceiro em Serviços e soluções → Integrações → "Ativar
aplicativo via código", e o portal devolve um código de autorização que ela
cola de volta aqui. Só então existe token.

Nada do que autentica sai deste servidor: `status()` devolve o estado da
conexão e nunca a chave, o segredo ou o token.
"""

from __future__ import annotations

import gzip
import json
import logging
import unicodedata
import zlib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlencode

from app.repositories.secrets import SecretVault
from app.repositories.store import Store
from app.schemas import IFoodActivationConfirm, IFoodActivationStart, OrderCreate, OrderLine
from app.services.erp import ERPService

logger = logging.getLogger(__name__)

API_HOST = "https://merchant-api.ifood.com.br"

# Documento único de estado da integração e a ponte pedido-do-iFood → pedido-do-ERP.
INTEGRATIONS = "integrations"
INTEGRATION_ID = "ifood"
ORDER_LINKS = "ifood_orders"

# Nome do segredo no cofre. Tudo o que autentica cabe num documento só, e
# gravar de uma vez evita o estado meio-atualizado que dois segredos separados
# permitiriam — token novo com refresh antigo é uma conexão morta.
SECRET_NAME = "ifood-credentials"

# Campos que jamais podem voltar para o documento de negócio. A lista existe
# porque o estado é remontado com `{**anterior, ...}`: sem a peneira, um
# segredo gravado por uma versão antiga do código sobreviveria para sempre.
SECRET_FIELDS = frozenset({
    "client_secret", "access_token", "refresh_token", "token_expires_at",
    "authorization_code_verifier",
})

CHANNEL = "iFood"

# O token vale seis horas. Renovar com folga evita a corrida entre "ainda vale"
# e "expirou no meio da chamada", que apareceria como erro intermitente.
TOKEN_MARGIN = timedelta(minutes=10)

# O iFood aceita uma chamada de polling a cada 30 segundos por token. Chamar
# antes disso devolve 429 e, pior, atrasa o próximo pedido de verdade.
POLL_INTERVAL = timedelta(seconds=30)

# Eventos que mudam alguma coisa aqui. O pedido nasce no ERP quando a loja o
# confirma — não quando ele apenas chega. Confirmar é o momento em que a loja
# se compromete a produzir; baixar estoque antes disso descontaria insumo de
# pedido que ainda pode ser recusado. CONCLUDED entra como rede de segurança
# para quem confirma pelo aplicativo do iFood e nunca nos manda o CFM.
CONFIRM_CODES = {"CFM", "CONFIRMED", "CON", "CONCLUDED"}
CANCEL_CODES = {"CAN", "CANCELLED", "CANCELED"}

STATUS_DISCONNECTED = "desconectado"
STATUS_WAITING = "aguardando_autorizacao"
STATUS_CONNECTED = "conectado"


class IFoodError(ValueError):
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
            return response.status, _decode(response.read(), response.headers.get("Content-Encoding"))
    except urllib.error.HTTPError as error:  # resposta do iFood, não falha de rede
        return error.code, _decode(error.read(), error.headers.get("Content-Encoding"))
    except urllib.error.URLError as error:
        raise IFoodError("Não foi possível falar com o iFood. Verifique a conexão e tente de novo.") from error
    except TimeoutError as error:
        raise IFoodError("O iFood demorou demais para responder. Tente de novo em instantes.") from error


def _decode(raw: bytes | None, encoding: str | None = None) -> Any:
    """Desfaz a compressão antes de ler o JSON.

    O iFood responde com `Content-Encoding: gzip` mesmo quando o pedido não
    anuncia que aceita compressão. Ler os bytes crus fazia toda resposta —
    inclusive a de sucesso — virar texto ilegível, e a integração inteira
    pareceria fora do ar por um motivo que nada tem a ver com o iFood.
    """

    if not raw:
        return None
    rotulo = (encoding or "").lower()
    if rotulo == "gzip" or raw.startswith(bytes.fromhex("1f8b")):
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            return None
    elif rotulo == "deflate":
        for janela in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                raw = zlib.decompress(raw, janela)
                break
            except zlib.error:
                continue
        else:
            return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _error_message(payload: Any) -> str:
    """A recusa do iFood vem aninhada: `{"error": {"code", "message"}}`.

    Procurar só no primeiro nível devolvia o dicionário inteiro impresso na
    cara do operador, em vez da frase que explica o que ele digitou errado.
    """

    if not isinstance(payload, dict):
        return ""
    interno = payload.get("error")
    if isinstance(interno, dict):
        return str(interno.get("message") or interno.get("code") or "")
    for chave in ("message", "error", "error_description", "details"):
        valor = payload.get(chave)
        if isinstance(valor, str) and valor:
            return valor
    return ""


class IFoodClient:
    """Fala com a API do iFood e nada mais: não conhece estoque nem pedido do ERP."""

    def __init__(self, transport: Transport | None = None, host: str = API_HOST, timeout: float = 20.0):
        self.transport = transport or _urllib_transport
        self.host = host.rstrip("/")
        self.timeout = timeout

    # -- autenticação --------------------------------------------------

    def user_code(self, client_id: str) -> dict[str, Any]:
        """Pede o código que a loja vai digitar no Portal do Parceiro."""

        return self._form("/authentication/v1.0/oauth/userCode", {"clientId": client_id})

    def token_from_authorization_code(
        self, client_id: str, client_secret: str, authorization_code: str, verifier: str
    ) -> dict[str, Any]:
        return self._form("/authentication/v1.0/oauth/token", {
            "grantType": "authorization_code",
            "clientId": client_id,
            "clientSecret": client_secret,
            "authorizationCode": authorization_code,
            "authorizationCodeVerifier": verifier,
        })

    def token_from_refresh(self, client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
        return self._form("/authentication/v1.0/oauth/token", {
            "grantType": "refresh_token",
            "clientId": client_id,
            "clientSecret": client_secret,
            "refreshToken": refresh_token,
        })

    # -- loja, eventos e pedidos ---------------------------------------

    def merchants(self, token: str) -> list[dict[str, Any]]:
        payload = self._get("/merchant/v1.0/merchants", token)
        return payload if isinstance(payload, list) else []

    def poll(self, token: str, merchant_id: str | None = None) -> list[dict[str, Any]]:
        headers = {"x-polling-merchants": merchant_id} if merchant_id else {}
        payload = self._get("/events/v1.0/events:polling", token, headers=headers)
        # 204 é o caso comum: nenhum pedido novo desde a última consulta.
        return payload if isinstance(payload, list) else []

    def acknowledge(self, token: str, event_ids: list[str]) -> None:
        if not event_ids:
            return
        self._json("/events/v1.0/events/acknowledgment", token, [{"id": item} for item in event_ids])

    def order(self, token: str, order_id: str) -> dict[str, Any]:
        payload = self._get(f"/order/v1.0/orders/{order_id}", token)
        if not isinstance(payload, dict):
            raise IFoodError(f"O iFood não devolveu os dados do pedido {order_id}.")
        return payload

    # -- transporte -----------------------------------------------------

    def _form(self, path: str, fields: dict[str, str]) -> dict[str, Any]:
        status, payload = self.transport(
            "POST", f"{self.host}{path}",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            body=urlencode(fields).encode("utf-8"), timeout=self.timeout,
        )
        return self._ensure(status, payload, path)

    def _get(self, path: str, token: str, headers: dict[str, str] | None = None) -> Any:
        status, payload = self.transport(
            "GET", f"{self.host}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", **(headers or {})},
            body=None, timeout=self.timeout,
        )
        if status == 204:
            return None
        self._ensure(status, payload, path, allow_list=True)
        return payload

    def _json(self, path: str, token: str, body: Any) -> Any:
        status, payload = self.transport(
            "POST", f"{self.host}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=json.dumps(body).encode("utf-8"), timeout=self.timeout,
        )
        if status in (200, 202, 204):
            return payload
        return self._ensure(status, payload, path, allow_list=True)

    @staticmethod
    def _ensure(status: int, payload: Any, path: str, allow_list: bool = False) -> dict[str, Any]:
        if status == 429:
            raise IFoodError("O iFood limitou as consultas. Aguarde meio minuto e tente de novo.")
        if status in (401, 403):
            raise IFoodError("O iFood recusou as credenciais. Refaça a ativação para gerar um código novo.")
        if status >= 400:
            # A mensagem do iFood é útil e não carrega segredo: o token viaja
            # no cabeçalho, nunca no corpo da resposta.
            detail = _error_message(payload)
            logger.warning("ifood: %s respondeu %s", path, status)
            raise IFoodError(f"O iFood recusou a chamada ({status}). {detail}".strip())
        if allow_list:
            return payload if isinstance(payload, dict) else {}
        if not isinstance(payload, dict):
            raise IFoodError("O iFood devolveu uma resposta que não dá para interpretar.")
        return payload


class IFoodService:
    """A integração vista pelo ERP: ativar, sincronizar e desconectar."""

    def __init__(self, store: Store, erp: ERPService, vault: SecretVault,
                 client: IFoodClient | None = None):
        self.store = store
        self.erp = erp
        self.vault = vault
        self.client = client or IFoodClient()

    # -- estado público e segredo, separados --------------------------

    def _persist(self, estado: dict[str, Any]) -> dict[str, Any]:
        """Grava o estado da conexão sem deixar passar nada que autentique."""

        limpo = {chave: valor for chave, valor in estado.items() if chave not in SECRET_FIELDS}
        self.store.put(INTEGRATIONS, INTEGRATION_ID, limpo)
        return limpo

    def _credentials(self) -> dict[str, Any]:
        return self.vault.read(SECRET_NAME) or {}

    def _save_credentials(self, **campos: Any) -> dict[str, Any]:
        atual = {**self._credentials(), **campos}
        self.vault.write(SECRET_NAME, atual)
        return atual

    # ------------------------------------------------------------------
    # Estado
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """O que a tela pode ver. Chave, segredo e token nunca saem daqui."""

        state = self.store.get(INTEGRATIONS, INTEGRATION_ID) or {}
        situacao = state.get("status", STATUS_DISCONNECTED)
        expira = state.get("code_expires_at")
        aguardando = situacao == STATUS_WAITING and bool(expira) and self._parse(expira) > self._now()
        if situacao == STATUS_WAITING and not aguardando:
            situacao = STATUS_DISCONNECTED
        return {
            "status": situacao,
            "connected": situacao == STATUS_CONNECTED,
            "client_id_hint": self._hint(state.get("client_id")),
            "merchant_id": state.get("merchant_id"),
            "merchant_name": state.get("merchant_name"),
            "channel": CHANNEL,
            "user_code": state.get("user_code") if aguardando else None,
            "verification_url": state.get("verification_url") if aguardando else None,
            "verification_url_complete": state.get("verification_url_complete") if aguardando else None,
            "code_expires_at": expira if aguardando else None,
            "last_sync_at": state.get("last_sync_at"),
            "last_sync_summary": state.get("last_sync_summary"),
            "last_error": state.get("last_error"),
            "orders_imported": state.get("orders_imported", 0),
            "unmapped_items": state.get("unmapped_items", []),
        }

    # ------------------------------------------------------------------
    # Ativação
    # ------------------------------------------------------------------

    def start_activation(self, payload: IFoodActivationStart) -> dict[str, Any]:
        """Gera o código de ativação que a loja leva ao Portal do Parceiro.

        A chave e o segredo vêm do aplicativo criado no Portal do
        Desenvolvedor; o código gerado aqui vale poucos minutos e é o que liga
        aquele aplicativo a esta loja.
        """

        resposta = self.client.user_code(payload.client_id)
        codigo = str(resposta.get("userCode") or "").strip()
        verificador = str(resposta.get("authorizationCodeVerifier") or "").strip()
        if not codigo or not verificador:
            raise IFoodError("O iFood não devolveu o código de ativação. Confira a chave do aplicativo.")

        expira_em = int(resposta.get("expiresIn") or 600)
        agora = self._now()
        anterior = self.store.get(INTEGRATIONS, INTEGRATION_ID) or {}
        estado = {
            **anterior,
            "id": INTEGRATION_ID,
            "client_id": payload.client_id,
            "status": STATUS_WAITING,
            "user_code": codigo,
            "verification_url": resposta.get("verificationUrl") or "https://portal.ifood.com.br/apps/code",
            "verification_url_complete": resposta.get("verificationUrlComplete"),
            "code_expires_at": (agora + timedelta(seconds=expira_em)).isoformat(),
            "last_error": None,
            "created_at": anterior.get("created_at") or agora.isoformat(),
            "updated_at": agora.isoformat(),
        }
        # Trocar de aplicativo invalida o que a conexão anterior deixou: os
        # tokens antigos pertencem a outro aplicativo e não renovam mais.
        if anterior.get("client_id") and anterior["client_id"] != payload.client_id:
            estado.update({"merchant_id": None, "merchant_name": None})
            self.vault.delete(SECRET_NAME)
        self._save_credentials(client_secret=payload.client_secret,
                               authorization_code_verifier=verificador)
        self._persist(estado)
        self.erp._audit(self.store, "integracao", INTEGRATION_ID, "codigo_gerado",
                        "Código de ativação do iFood gerado")
        return {
            "status": STATUS_WAITING,
            "user_code": codigo,
            "verification_url": estado["verification_url"],
            "verification_url_complete": estado["verification_url_complete"],
            "code_expires_at": estado["code_expires_at"],
            "expires_in": expira_em,
            "instructions": (
                "No Portal do Parceiro do iFood, abra Serviços e soluções → Integrações → "
                f"Ativar aplicativo via código, informe {codigo} e cole aqui o código de "
                "autorização que o portal devolver."
            ),
        }

    def renew_activation(self) -> dict[str, Any]:
        """Gera outro código sem pedir a chave e o segredo de novo.

        O código vale poucos minutos, e é comum vencer entre abrir o portal do
        iFood e achar a tela certa. Exigir o segredo a cada tentativa
        empurraria a loja a deixá-lo anotado num papel ao lado do computador.
        """

        estado = self.store.get(INTEGRATIONS, INTEGRATION_ID) or {}
        segredo = self._credentials().get("client_secret")
        if not estado.get("client_id") or not segredo:
            raise IFoodError("Informe a chave e o segredo do aplicativo para gerar o código.")
        return self.start_activation(IFoodActivationStart(
            client_id=estado["client_id"], client_secret=segredo,
        ))

    def confirm_activation(self, payload: IFoodActivationConfirm) -> dict[str, Any]:
        """Troca o código de autorização do portal pelos tokens de acesso."""

        estado = self.store.get(INTEGRATIONS, INTEGRATION_ID) or {}
        credenciais = self._credentials()
        if estado.get("status") != STATUS_WAITING or not credenciais.get("authorization_code_verifier"):
            raise IFoodError("Gere um código de ativação antes de confirmar.")
        if self._parse(estado["code_expires_at"]) <= self._now():
            raise IFoodError("O código de ativação expirou. Gere um novo.")

        resposta = self.client.token_from_authorization_code(
            estado["client_id"], credenciais["client_secret"],
            payload.authorization_code, credenciais["authorization_code_verifier"],
        )
        credenciais = self._save_tokens(resposta)

        # A loja precisa ficar identificada: é o merchantId que filtra o
        # polling e o que separa esta loja das outras do mesmo aplicativo.
        lojas = self.client.merchants(credenciais["access_token"])
        if lojas:
            escolhida = next((row for row in lojas if row.get("id") == payload.merchant_id), lojas[0])
            estado["merchant_id"] = escolhida.get("id")
            estado["merchant_name"] = escolhida.get("name") or escolhida.get("corporateName")

        estado.update({
            "status": STATUS_CONNECTED, "user_code": None,
            "verification_url_complete": None, "code_expires_at": None, "last_error": None,
            "updated_at": self._now().isoformat(),
        })
        self._persist(estado)
        # O verificador serviu uma vez e não serve mais; guardá-lo seria
        # manter exposto um segredo sem função.
        self._save_credentials(authorization_code_verifier=None)
        self._ensure_channel()
        self.erp._audit(self.store, "integracao", INTEGRATION_ID, "conectado",
                        f"iFood conectado — loja {estado.get('merchant_name') or estado.get('merchant_id') or '—'}")
        return self.status()

    def disconnect(self) -> dict[str, Any]:
        """Esquece as credenciais. Os pedidos já importados continuam no ERP."""

        existia = bool(self.store.get(INTEGRATIONS, INTEGRATION_ID))
        # O cofre é limpo mesmo sem o documento: sobra de credencial é
        # exatamente o que não pode ficar para trás.
        self.vault.delete(SECRET_NAME)
        if existia:
            self.store.delete(INTEGRATIONS, INTEGRATION_ID)
            self.erp._audit(self.store, "integracao", INTEGRATION_ID, "desconectado",
                            "Integração com o iFood desligada")
        return self.status()

    # ------------------------------------------------------------------
    # Sincronização
    # ------------------------------------------------------------------

    def sync(self, force: bool = False) -> dict[str, Any]:
        """Lê a fila de eventos, registra o que mudou e confirma o recebimento.

        A ordem importa: o evento só é confirmado depois que o pedido está
        gravado. Se a gravação falhar, o iFood devolve o mesmo evento na
        próxima consulta — perder um pedido é pior que recebê-lo duas vezes, e
        a ponte `ifood_orders` cuida da segunda parte.
        """

        estado = self._connected_state()
        ultima = estado.get("last_poll_at")
        if not force and ultima and self._parse(ultima) + POLL_INTERVAL > self._now():
            return {**self._empty_summary(), "skipped": True,
                    "detail": "O iFood aceita uma consulta a cada 30 segundos. Aguarde."}

        token = self._valid_token(estado)
        resumo = self._empty_summary()
        confirmados: list[str] = []
        nao_mapeados: dict[str, str] = {}
        try:
            eventos = self.client.poll(token, estado.get("merchant_id"))
            for evento in eventos:
                evento_id = str(evento.get("id") or "")
                if not evento_id:
                    continue
                try:
                    self._handle_event(token, evento, resumo, nao_mapeados)
                except IFoodError:
                    raise
                except Exception:
                    # Um pedido problemático não pode travar a fila inteira: o
                    # evento fica sem confirmar e volta na próxima consulta.
                    logger.exception("ifood: falha ao processar o evento %s", evento_id)
                    resumo["failed"] += 1
                    continue
                confirmados.append(evento_id)
            self.client.acknowledge(token, confirmados)
        except IFoodError as error:
            # A tela precisa dizer o que houve, e não apenas que não deu. O
            # relógio da consulta também avança: repetir na hora só renderia
            # outro 429.
            self._remember(last_poll_at=self._now().isoformat(), last_error=str(error))
            raise

        estado = self.store.get(INTEGRATIONS, INTEGRATION_ID) or estado
        estado["last_poll_at"] = self._now().isoformat()
        resumo["acknowledged"] = len(confirmados)
        resumo["events"] = len(eventos)
        resumo["detail"] = self._describe(resumo)

        estado.update({
            "last_sync_at": self._now().isoformat(),
            "last_sync_summary": resumo["detail"],
            "last_error": None,
            "orders_imported": estado.get("orders_imported", 0) + resumo["created"],
            "updated_at": self._now().isoformat(),
        })
        # O aviso do que falta mapear só se refaz quando houve pedido para
        # conferir: uma consulta vazia não prova que a loja preencheu o código
        # do produto, e limpar o aviso ali esconderia o custo que continua
        # desconhecido. Quando entra pedido e nada falta, o aviso some sozinho.
        if resumo["created"]:
            estado["unmapped_items"] = sorted(nao_mapeados.values())[:20]
        self._persist(estado)
        return resumo

    def _remember(self, **campos: Any) -> None:
        """Anota no estado sem perder o que outra escrita já tenha gravado."""

        estado = self.store.get(INTEGRATIONS, INTEGRATION_ID)
        if not estado:
            return
        self._persist({**estado, **campos, "updated_at": self._now().isoformat()})

    # ------------------------------------------------------------------
    # Um evento
    # ------------------------------------------------------------------

    def _handle_event(
        self, token: str, evento: dict[str, Any], resumo: dict[str, Any], nao_mapeados: dict[str, str]
    ) -> None:
        codigo = str(evento.get("code") or evento.get("fullCode") or "").upper()
        pedido_id = str(evento.get("orderId") or "")
        if not pedido_id:
            resumo["ignored"] += 1
            return

        ligacao = self.store.get(ORDER_LINKS, pedido_id)
        if codigo in CANCEL_CODES:
            self._cancel(ligacao, pedido_id, resumo)
            return
        if codigo not in CONFIRM_CODES:
            resumo["ignored"] += 1
            return
        if ligacao:
            resumo["duplicated"] += 1
            return

        self._record(pedido_id, self.client.order(token, pedido_id), resumo, nao_mapeados)

    def _cancel(self, ligacao: dict[str, Any] | None, pedido_id: str, resumo: dict[str, Any]) -> None:
        if not ligacao or ligacao.get("cancelled"):
            resumo["ignored"] += 1
            return
        try:
            self.erp.cancel_order(ligacao["order_id"], "Cancelado no iFood")
        except ValueError:
            # Já cancelado ou apagado no ERP: o efeito desejado já existe.
            pass
        self.store.put(ORDER_LINKS, pedido_id, {**ligacao, "cancelled": True,
                                                "cancelled_at": self._now().isoformat()})
        resumo["cancelled"] += 1

    def _record(
        self, pedido_id: str, detalhe: dict[str, Any], resumo: dict[str, Any], nao_mapeados: dict[str, str]
    ) -> None:
        """Grava o pedido do iFood como pedido do ERP.

        Quando todo item do pedido aponta para um produto do cardápio, o pedido
        entra pela porta normal: a ficha técnica baixa o insumo e calcula o
        custo. Quando algum item não aponta, o pedido entra sem itens — com o
        faturamento certo e o custo declarado desconhecido. Inventar a ficha
        técnica do item que a loja não mapeou seria pior que admitir a falta:
        contaminaria a margem de todos os outros.
        """

        linhas, faltando = self._map_items(detalhe.get("items") or [])
        for nome in faltando:
            nao_mapeados[self._key(nome)] = nome

        valores = self._amounts(detalhe)
        momento = self._occurred(detalhe)
        entrega = str(detalhe.get("orderType") or "").upper() != "TAKEOUT"
        provenance = self._provenance(pedido_id, detalhe, valores, faltando)

        if linhas and not faltando:
            pedido = self.erp.create_order(OrderCreate(
                items=linhas, channel=CHANNEL,
                payment_method=self._payment(detalhe),
                customer_name=self._customer(detalhe),
                is_delivery=entrega,
                occurred_at=momento,
            ))
            # O estoque e o custo vêm da ficha técnica; o dinheiro vem do
            # iFood. O total que o cardápio calcularia costuma diferir do
            # cobrado no marketplace, e é o cobrado que entrou no caixa.
            catalogo = pedido["total"]
            pedido.update({
                "discount": valores["discount"], "delivery_fee": valores["delivery_fee"],
                "total": valores["total"], "channel_fee": self._fee(valores["total"]),
                **provenance, "catalog_total": catalogo,
                "revenue_conflict": abs(catalogo - valores["total"]) >= 0.01,
            })
            self.store.put("orders", pedido["id"], pedido)
            order_id = pedido["id"]
            resumo["with_recipe"] += 1
        else:
            order_id = f"{INTEGRATION_ID}-{pedido_id}"
            self.store.put("orders", order_id, {
                "id": order_id,
                "number": str(detalhe.get("displayId") or pedido_id[:8]),
                "created_at": momento.isoformat(),
                "registered_at": self._now().isoformat(),
                "channel": CHANNEL,
                "payment_method": self._payment(detalhe),
                "customer_name": self._customer(detalhe),
                "discount": valores["discount"],
                "is_delivery": entrega,
                "delivery_fee": valores["delivery_fee"],
                "courier_id": None,
                "total": valores["total"],
                # Desconhecido, não zero: sem produto identificado não há ficha
                # técnica, e um custo zero mentiria margem de 100%.
                "cost": None,
                "channel_fee": self._fee(valores["total"]),
                "status": "Concluido",
                "items": [],
                **provenance,
            })
            resumo["without_recipe"] += 1

        resumo["created"] += 1
        self.store.put(ORDER_LINKS, pedido_id, {
            "id": pedido_id, "order_id": order_id, "cancelled": False,
            "display_id": str(detalhe.get("displayId") or ""), "linked_at": self._now().isoformat(),
        })

    # ------------------------------------------------------------------
    # Tradução do pedido do iFood
    # ------------------------------------------------------------------

    def _map_items(self, itens: list[dict[str, Any]]) -> tuple[list[OrderLine], list[str]]:
        """Liga item do iFood a produto do ERP pelo código externo do cardápio.

        `externalCode` é o campo que a loja preenche no cardápio do iFood com o
        id do produto daqui. Ele é a ponte, e sem ele não há custo. O nome
        entra como segunda tentativa porque o cardápio antigo pode não trazer o
        código, mas nome é frágil de propósito — a solução é preencher o
        código.
        """

        produtos = {row["id"]: row for row in self.store.list("products") if row.get("active", True)}
        por_nome = {self._key(row["name"]): row["id"] for row in produtos.values()}

        linhas: list[OrderLine] = []
        faltando: list[str] = []
        for item in itens:
            nome = str(item.get("name") or "item sem nome")
            codigo = str(item.get("externalCode") or "").strip()
            produto_id = codigo if codigo in produtos else por_nome.get(self._key(nome))
            quantidade = item.get("quantity")
            if not produto_id or not isinstance(quantidade, (int, float)) or isinstance(quantidade, bool):
                faltando.append(nome)
                continue
            if quantidade != int(quantidade) or not 1 <= int(quantidade) <= 50:
                faltando.append(nome)
                continue
            linhas.append(OrderLine(product_id=produto_id, quantity=int(quantidade)))
        return linhas, faltando

    @staticmethod
    def _amounts(detalhe: dict[str, Any]) -> dict[str, float]:
        """Dinheiro do pedido, do jeito que o iFood o reporta.

        `orderAmount` é o que o cliente pagou; `benefits` é o desconto que já
        saiu dali. O frete fica separado porque existe para pagar a entrega —
        somá-lo ao faturamento de produto esconderia se a entrega se paga.
        """

        total = detalhe.get("total") or {}
        cobrado = _money(total.get("orderAmount"))
        frete = _money(total.get("deliveryFee"))
        desconto = _money(total.get("benefits"))
        if cobrado <= 0:
            cobrado = max(0.0, round(_money(total.get("subTotal")) + frete - desconto, 2))
        # O subsídio do marketplace pode deixar o frete maior que o recebido.
        # Limitar evita receita de produto negativa, como no importador.
        return {"total": cobrado, "delivery_fee": min(frete, cobrado), "discount": min(desconto, cobrado)}

    def _occurred(self, detalhe: dict[str, Any]) -> datetime:
        bruto = str(detalhe.get("createdAt") or "")
        agora = self._now()
        try:
            momento = datetime.fromisoformat(bruto.replace("Z", "+00:00"))
        except ValueError:
            return agora
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=timezone.utc)
        # Relógio adiantado do outro lado não pode virar erro de "data futura".
        return min(momento.astimezone(timezone.utc), agora)

    @staticmethod
    def _payment(detalhe: dict[str, Any]) -> str:
        metodos = ((detalhe.get("payments") or {}).get("methods")) or []
        if not metodos:
            return "iFood"
        primeiro = metodos[0] if isinstance(metodos[0], dict) else {}
        nome = str(primeiro.get("method") or primeiro.get("type") or "")
        traducao = {
            "CREDIT": "Credito", "DEBIT": "Debito", "PIX": "Pix", "CASH": "Dinheiro",
            "MEAL_VOUCHER": "Vale", "FOOD_VOUCHER": "Vale",
        }
        return traducao.get(nome.upper(), nome.title()[:30] or "iFood")

    @staticmethod
    def _customer(detalhe: dict[str, Any]) -> str | None:
        nome = str((detalhe.get("customer") or {}).get("name") or "").strip()
        return nome[:80] or None

    def _provenance(
        self, pedido_id: str, detalhe: dict[str, Any], valores: dict[str, float], faltando: list[str]
    ) -> dict[str, Any]:
        return {
            "source": INTEGRATION_ID,
            "source_channel": str(detalhe.get("salesChannel") or "IFOOD"),
            "ifood_order_id": pedido_id,
            "ifood_display_id": str(detalhe.get("displayId") or ""),
            "ifood_order_type": str(detalhe.get("orderType") or ""),
            "reported_total": valores["total"],
            "reported_delivery_fee": valores["delivery_fee"],
            "unmapped_items": faltando,
        }

    def _fee(self, total: float) -> float:
        canal = next((row for row in self.store.list("channels") if row["name"] == CHANNEL), None)
        return self.erp._channel_fee(canal, total) if canal else 0.0

    def _ensure_channel(self) -> None:
        """O canal precisa existir para o pedido do iFood ter onde cair."""

        if any(row["name"] == CHANNEL for row in self.store.list("channels")):
            return
        self.store.put("channels", INTEGRATION_ID, {
            "id": INTEGRATION_ID, "name": CHANNEL, "fee_percent": 27.0, "fee_fixed": 0.0,
            "note": "Criado pela integração. Confirme o percentual do seu contrato.",
            "active": True, "created_at": self._now().isoformat(),
        })

    # ------------------------------------------------------------------
    # Token
    # ------------------------------------------------------------------

    def _connected_state(self) -> dict[str, Any]:
        estado = self.store.get(INTEGRATIONS, INTEGRATION_ID) or {}
        if estado.get("status") != STATUS_CONNECTED or not self._credentials().get("refresh_token"):
            raise IFoodError("O iFood ainda não está conectado. Gere o código de ativação primeiro.")
        return estado

    def _valid_token(self, estado: dict[str, Any]) -> str:
        credenciais = self._credentials()
        expira = credenciais.get("token_expires_at")
        if credenciais.get("access_token") and expira and self._parse(expira) - TOKEN_MARGIN > self._now():
            return credenciais["access_token"]
        resposta = self.client.token_from_refresh(
            estado["client_id"], credenciais["client_secret"], credenciais["refresh_token"]
        )
        return self._save_tokens(resposta)["access_token"]

    def _save_tokens(self, resposta: dict[str, Any]) -> dict[str, Any]:
        """Guarda o par de tokens no cofre, nunca no documento da integração."""

        acesso = str(resposta.get("accessToken") or "").strip()
        if not acesso:
            raise IFoodError("O iFood não devolveu o token de acesso. Confira o código de autorização.")
        vida = int(resposta.get("expiresIn") or 21600)
        # O refresh pode vir rotacionado; quando não vem, o anterior continua
        # valendo e perdê-lo mataria a conexão sem aviso.
        return self._save_credentials(
            access_token=acesso,
            refresh_token=resposta.get("refreshToken") or self._credentials().get("refresh_token"),
            token_expires_at=(self._now() + timedelta(seconds=vida)).isoformat(),
        )

    # ------------------------------------------------------------------
    # Miudezas
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _parse(value: str) -> datetime:
        momento = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return momento if momento.tzinfo else momento.replace(tzinfo=timezone.utc)

    @staticmethod
    def _key(value: str) -> str:
        texto = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
        return " ".join(texto.lower().split())

    @staticmethod
    def _hint(client_id: str | None) -> str | None:
        """O bastante para a loja reconhecer o aplicativo, nunca o bastante para usá-lo."""

        if not client_id:
            return None
        return f"{client_id[:4]}…{client_id[-4:]}" if len(client_id) > 10 else "…"

    @staticmethod
    def _empty_summary() -> dict[str, Any]:
        return {"events": 0, "created": 0, "cancelled": 0, "duplicated": 0, "ignored": 0,
                "failed": 0, "acknowledged": 0, "with_recipe": 0, "without_recipe": 0,
                "skipped": False, "detail": ""}

    @staticmethod
    def _describe(resumo: dict[str, Any]) -> str:
        if not resumo["events"]:
            return "Nenhum pedido novo."
        partes = [f"{resumo['created']} pedido(s) registrado(s)"]
        if resumo["with_recipe"]:
            partes.append(f"{resumo['with_recipe']} com ficha técnica")
        if resumo["without_recipe"]:
            partes.append(f"{resumo['without_recipe']} sem produto mapeado")
        if resumo["cancelled"]:
            partes.append(f"{resumo['cancelled']} cancelado(s)")
        if resumo["failed"]:
            partes.append(f"{resumo['failed']} com falha")
        return ", ".join(partes) + "."


def _money(value: Any) -> float:
    try:
        numero = round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0
    # NaN e infinito não representam dinheiro e envenenariam qualquer soma.
    return numero if numero == numero and abs(numero) != float("inf") else 0.0
