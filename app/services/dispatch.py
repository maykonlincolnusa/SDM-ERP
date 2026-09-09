"""A operacao de rua: quem esta trabalhando, o que saiu e para onde.

`delivery.py` responde *a entrega se paga?* — o frete cobre o que o entregador
recebe. Este modulo responde as duas perguntas que ficaram de fora, e que o
fechamento por turno nunca teve como responder:

- **De onde vem o pedido?** O sistema inteiro nao guardava endereco nenhum.
  Nem o pedido, nem o relatorio da Anota Ai, nem o que a integracao do iFood
  aproveita. "Onde mais pedem" era uma pergunta sem dado por tras.
- **Quanto tempo leva?** Sem saida e chegada nao ha tempo de entrega, e sem
  tempo de entrega "demorou" continua sendo impressao contra impressao.

A decisao de modelagem que sustenta o resto
-------------------------------------------

O registro e por entrega, e nao por turno — o contrario do que `delivery.py`
faz de proposito. A diferenca nao e de opiniao, e de quem digita: aquele e um
formulario que a loja preenche no fim do expediente, de memoria; este e um
botao que o proprio entregador toca no celular no momento em que sai e no
momento em que chega. Registro por entrega digitado depois e abandonado na
primeira semana cheia; registro por entrega feito na hora custa dois toques.

Para custar dois toques mesmo, a entrega nasce do pedido que ja esta no ERP:
produto, quantidade e frete vem de la, e o entregador so confirma a zona. E
ate a zona o sistema tenta adivinhar, pelo que aquele cliente pediu da ultima
vez.

O fechamento do turno nao foi substituido: ele passou a ser **calculado**.
Quando o entregador faz check-out, o lancamento de `delivery_entries` nasce
com as entregas e o frete somados do que foi registrado na rua. A maquina de
pagamento que ja existia continua inteira, sem ninguem digitar duas vezes o
mesmo fato — e a segunda digitacao e sempre a que diverge.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import fmean, median
from typing import Any
from uuid import uuid4

from app.repositories.store import Store
from app.schemas import (
    CourierCheckIn,
    CourierCheckOut,
    DeliveryRunCreate,
    DeliveryRunFinish,
    DeliveryRunUpdate,
    DeliveryZoneCreate,
    DeliveryZoneUpdate,
    PendingOrderConfirm,
    PendingOrderReject,
)
from app.services.erp import FUSO_LOJA, ERPService, is_active_order, is_delivery_order

ZONES = "delivery_zones"
SHIFTS = "courier_shifts"
RUNS = "delivery_runs"
PENDING = "pending_orders"

# Canal das vendas que nascem na rua. Separado do balcao de proposito: elas tem
# origem, taxa e comportamento proprios, e misturá-las com o balcao apagaria a
# unica pergunta que essa origem responde — quanto a rua traz sozinha.
CANAL_RUA = "Entrega direta"

# Ate onde o entregador pode retroagir. Cobre o "esqueci de lancar ontem" e a
# semana inteira sem abrir a porta para mexer em mes ja fechado e pago; data
# mais antiga que isso passa pela loja, que tem a tela de correcao.
DIAS_PARA_RETROAGIR = 7

# Turno esquecido aberto e o caso normal, nao a excecao: o entregador sai
# correndo e fecha o aplicativo. Depois disso o painel diria "3 em rua" com a
# loja fechada, e um painel que mente uma vez deixa de ser consultado.
HORAS_MAXIMAS_DE_TURNO = 16

# Abaixo disso a media por zona e uma coincidencia com casas decimais. O numero
# aparece na tela mesmo assim, mas marcado como amostra fraca — esconder faria
# a zona nova parecer inexistente em vez de recente.
ENTREGAS_MINIMAS_POR_ZONA = 5

# Pares de produto abaixo disso dentro de uma zona sao acaso. E mais alto que o
# minimo global de cesta porque a zona ja e um recorte do historico: exigir o
# mesmo numero faria toda zona pequena gerar sugestao.
OCORRENCIAS_MINIMAS_DO_PAR = 3
ENTREGAS_MINIMAS_PARA_CESTA = 15

DIAS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def _money(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _num(value: float, casas: int = 1) -> str:
    """Numero com virgula decimal.

    As frases daqui sao lidas no balcao, nao num relatorio em ingles: "5.0x a
    media" faz o gestor parar para reler, e a leitura que faz parar de ler e a
    que deixa de ser usada.
    """

    texto = f"{value:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return texto.rstrip("0").rstrip(",") if "," in texto else texto


class SemEntregadorEscolhido(ValueError):
    """A conta e da loja e ainda nao disse por quem esta lancando.

    Nao e erro de permissao nem de cadastro: e uma pergunta pendente, e a tela
    responde com uma lista em vez de uma mensagem de recusa.
    """


class DispatchService:
    """Zonas, turnos e entregas — e o que eles ensinam sobre o territorio."""

    def __init__(self, store: Store):
        self.store = store
        self.erp = ERPService(store)

    # ------------------------------------------------------------------
    # Zonas
    # ------------------------------------------------------------------

    def zones(self, include_inactive: bool = True) -> list[dict[str, Any]]:
        linhas = self.store.list(ZONES)
        if not include_inactive:
            linhas = [linha for linha in linhas if linha.get("active", True)]
        return sorted(linhas, key=lambda row: (not row.get("active", True), str(row.get("name", ""))))

    def create_zone(self, payload: DeliveryZoneCreate) -> dict[str, Any]:
        zone = payload.model_dump()
        zone["name"] = zone["name"].strip()
        zone["id"] = self._identifier("zona", zone["name"])
        # Duas zonas com o mesmo nome sao o problema que o cadastro existe para
        # evitar; deixar passar aqui recriaria o "Centro" e o "centro".
        if any(self._fold(linha.get("name", "")) == self._fold(zone["name"]) for linha in self.store.list(ZONES)):
            raise ValueError(f"Já existe uma zona chamada {zone['name']}.")
        zone["created_at"] = self._agora().isoformat()
        self.store.put(ZONES, zone["id"], zone)
        self.erp._audit(self.store, "zone", zone["id"], "criada", f"Zona de entrega {zone['name']} cadastrada")
        return zone

    def propose_zone(self, courier: dict[str, Any], payload: FieldZoneCreate) -> dict[str, Any]:
        """Zona criada da rua, quando nenhuma das cadastradas serve.

        O entregador nao pode ficar parado na calcada esperando o balcao
        cadastrar um bairro. Ela vale desde o primeiro segundo, com frete zero,
        e fica marcada como proposta ate a loja confirmar, renomear ou fundir —
        que e o que separa "a lista nasce da rua" de "a lista vira Centro,
        centro e Ctr".

        Nome que ja existe nao vira zona nova: devolve a que existe. Na rua, o
        que o entregador quer e seguir, e recusar por duplicidade seria o mesmo
        beco de antes com outra mensagem.
        """

        nome = payload.name.strip()
        existente = next(
            (linha for linha in self.store.list(ZONES) if self._fold(linha.get("name", "")) == self._fold(nome)),
            None,
        )
        if existente:
            return existente

        zone = self.create_zone(DeliveryZoneCreate(name=nome, proposed=True))
        self.erp._audit(
            self.store, "zone", zone["id"], "proposta na rua",
            f"{courier['name']} criou a zona {zone['name']} durante uma entrega",
            {"frete": "a definir pela loja"},
        )
        return zone

    def update_zone(self, zone_id: str, payload: DeliveryZoneUpdate) -> dict[str, Any]:
        zone = self.store.get(ZONES, zone_id)
        if not zone:
            raise ValueError("Zona não encontrada.")
        mudancas = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None or k == "note"}
        if "name" in mudancas:
            mudancas["name"] = str(mudancas["name"]).strip()
            conflito = any(
                linha["id"] != zone_id and self._fold(linha.get("name", "")) == self._fold(mudancas["name"])
                for linha in self.store.list(ZONES)
            )
            if conflito:
                raise ValueError(f"Já existe uma zona chamada {mudancas['name']}.")
        antes = {k: zone.get(k) for k in mudancas}
        zone.update(mudancas)
        zone["updated_at"] = self._agora().isoformat()
        self.store.put(ZONES, zone_id, zone)
        self.erp._audit(
            self.store, "zone", zone_id, "editada",
            f"Zona {zone['name']} atualizada", self.erp._diff(antes, mudancas),
        )
        return zone

    def delete_zone(self, zone_id: str) -> dict[str, str]:
        """Zona com entrega vira inativa, nunca some.

        Apagar deixaria o historico de entregas apontando para lugar nenhum, e
        e justamente esse historico que responde onde a loja vende.
        """

        zone = self.store.get(ZONES, zone_id)
        if not zone:
            raise ValueError("Zona não encontrada.")
        if any(run.get("zone_id") == zone_id for run in self.store.list(RUNS)):
            zone["active"] = False
            self.store.put(ZONES, zone_id, zone)
            self.erp._audit(self.store, "zone", zone_id, "inativada", f"Zona {zone['name']} desativada")
            return {"id": zone_id, "status": "inativada", "detail": "A zona tem entregas registradas, então foi desativada em vez de excluída."}
        self.store.delete(ZONES, zone_id)
        self.erp._audit(self.store, "zone", zone_id, "excluida", f"Zona {zone['name']} removida")
        return {"id": zone_id, "status": "excluido", "detail": "Zona removida."}

    # ------------------------------------------------------------------
    # Quem e o entregador desta sessao
    # ------------------------------------------------------------------

    def couriers_for_picker(self) -> list[dict[str, Any]]:
        """Quem a loja pode escolher para lançar no lugar. Só os ativos."""

        return sorted(
            (linha for linha in self.store.list("couriers") if linha.get("active", True)),
            key=lambda linha: str(linha.get("name", "")),
        )

    def courier_for(self, user: dict[str, Any], fallback_id: str | None = None, pode_escolher: bool = False) -> dict[str, Any]:
        """Resolve o cadastro do entregador a partir da conta autenticada.

        O vinculo e por uid, e o e-mail e a ponte da primeira vez: o gestor
        cadastra o entregador com o e-mail da conta, e o primeiro acesso
        carimba o uid. Depois disso trocar o e-mail no cadastro nao desloga
        ninguem, o que importa quando a conta de uma pessoa e o unico caminho
        ate o turno dela.
        """

        uid = str(user.get("uid") or "")
        email = self._fold(user.get("email") or "")
        entregadores = self.store.list("couriers")

        por_uid = next((linha for linha in entregadores if linha.get("uid") == uid), None) if uid else None
        if por_uid:
            return por_uid

        por_email = next(
            (linha for linha in entregadores if email and self._fold(linha.get("email") or "") == email),
            None,
        )
        if por_email:
            if uid and por_email.get("uid") != uid:
                por_email["uid"] = uid
                self.store.put("couriers", por_email["id"], por_email)
            return por_email

        # Sem autenticacao (modo demonstrativo) nao ha e-mail para casar. Com
        # autenticacao, quem manda o identificador e a loja: o gestor abre a
        # tela de rua para fechar o turno de quem ficou sem bateria, e precisa
        # dizer por quem esta lancando.
        if fallback_id:
            escolhido = self.store.get("couriers", fallback_id)
            if escolhido:
                return escolhido

        # Quem tem direito de escolher e ainda nao escolheu recebe a pergunta, e
        # nao uma recusa. O gestor nao e um entregador e nao deveria virar um:
        # ele diz por quem esta lancando. Recusar aqui foi o beco sem saida que
        # ele encontrou ao abrir o proprio link.
        if pode_escolher or user.get("erp_admin") is True:
            raise SemEntregadorEscolhido(
                "Você entrou como loja. Escolha por qual entregador quer lançar."
            )

        raise ValueError(
            "Esta conta ainda não está ligada a um entregador. Peça para a loja cadastrar você "
            "com este e-mail em Entregadores."
        )

    # ------------------------------------------------------------------
    # Turno: chegada e saida
    # ------------------------------------------------------------------

    def open_shift(self, courier_id: str) -> dict[str, Any] | None:
        """O turno aberto do entregador, se ele existir e ainda fizer sentido.

        Turno esquecido aberto de ontem nao e turno: e um aplicativo que ficou
        na tela. Ele e fechado sozinho na leitura, para o painel nunca mostrar
        alguem na rua depois de a loja fechar.
        """

        abertos = [
            linha for linha in self.store.list(SHIFTS)
            if linha.get("courier_id") == courier_id and not linha.get("checkout_at")
        ]
        if not abertos:
            return None
        abertos.sort(key=lambda row: str(row.get("checkin_at", "")), reverse=True)
        atual = abertos[0]
        for esquecido in abertos[1:]:
            self._encerrar(esquecido, motivo="turno anterior ainda aberto")
        if self._horas(atual["checkin_at"]) > HORAS_MAXIMAS_DE_TURNO:
            self._encerrar(atual, motivo=f"passou de {HORAS_MAXIMAS_DE_TURNO}h aberto")
            return None
        return atual

    def check_in(self, courier: dict[str, Any], payload: CourierCheckIn) -> dict[str, Any]:
        aberto = self.open_shift(courier["id"])
        if aberto:
            # Tocar duas vezes no botao nao pode abrir dois turnos: o segundo
            # roubaria as entregas do primeiro e o acerto sairia partido em dois.
            raise ValueError("Você já fez check-in. O turno está aberto desde " + self._hora(aberto["checkin_at"]) + ".")
        if courier.get("active") is False:
            raise ValueError("Este entregador está inativo no cadastro da loja.")

        agora = self._agora()
        shift = {
            "id": str(uuid4()),
            "courier_id": courier["id"],
            "date": agora.date().isoformat(),
            "checkin_at": agora.isoformat(),
            "checkout_at": None,
            "note": self.erp._optional_text(payload.note),
            "created_at": agora.isoformat(),
        }
        self.store.put(SHIFTS, shift["id"], shift)
        self.erp._audit(
            self.store, "shift", shift["id"], "check-in",
            f"{courier['name']} chegou às {self._hora(shift['checkin_at'])}",
        )
        return self._decorar_turno(shift, courier)

    def check_out(self, courier: dict[str, Any], payload: CourierCheckOut) -> dict[str, Any]:
        """Fecha o turno e monta o acerto a partir do que foi registrado.

        Entregas e frete nao sao perguntados: eles ja foram registrados um a
        um durante o turno. Perguntar de novo criaria dois numeros para o mesmo
        fato, e o acerto passaria a depender de qual deles alguem olhou.
        """

        shift = self.open_shift(courier["id"])
        if not shift:
            raise ValueError("Não há turno aberto para fechar. Faça o check-in primeiro.")

        entregas = self._runs_do_turno(shift["id"])
        em_rota = [run for run in entregas if not run.get("finished_at")]
        if em_rota:
            raise ValueError(
                f"{len(em_rota)} entrega(s) ainda estão em rota. Marque a chegada de cada uma antes de encerrar o turno."
            )

        agora = self._agora()
        shift["checkout_at"] = agora.isoformat()
        shift["closing_note"] = self.erp._optional_text(payload.note)
        entregues = [run for run in entregas if not run.get("failed")]
        shift["deliveries"] = len(entregues)
        shift["freight_total"] = round(sum(run.get("freight", 0) for run in entregues), 2)
        shift["failed"] = len(entregas) - len(entregues)
        self.store.put(SHIFTS, shift["id"], shift)

        lancamento = self._lancar_turno(shift, courier)
        self.erp._audit(
            self.store, "shift", shift["id"], "check-out",
            f"{courier['name']} saiu às {self._hora(shift['checkout_at'])} · "
            f"{shift['deliveries']} entrega(s) · {_money(shift['freight_total'])} de frete",
        )
        return {**self._decorar_turno(shift, courier), "entry": lancamento}

    def _lancar_turno(self, shift: dict[str, Any], courier: dict[str, Any]) -> dict[str, Any] | None:
        """Grava o fechamento em `delivery_entries`, onde o pagamento ja mora.

        Reaproveitar a colecao existente e o que mantem a remuneracao, a
        conferencia com os pedidos e o custo da entrega no resultado
        funcionando sem nenhuma mudanca. O lancamento fica marcado como vindo
        da rua, para o balcao saber que ele nao foi digitado a mao.
        """

        from app.services.delivery import DeliveryService

        existente = next(
            (linha for linha in self.store.list("delivery_entries") if linha.get("shift_id") == shift["id"]),
            None,
        )
        entry = existente or {"id": str(uuid4()), "created_at": self._agora().isoformat()}
        entry.update({
            "courier_id": shift["courier_id"],
            "date": shift["date"],
            "deliveries": shift["deliveries"],
            "freight_total": shift["freight_total"],
            "worked": True,
            "bonus": entry.get("bonus", 0),
            "deduction": entry.get("deduction", 0),
            "payout_override": entry.get("payout_override"),
            "note": entry.get("note") or shift.get("closing_note"),
            "shift_id": shift["id"],
            "source": "check-out",
        })
        self.store.put("delivery_entries", entry["id"], entry)
        return DeliveryService(self.store)._decorar(entry, courier)

    # ------------------------------------------------------------------
    # Entregas
    # ------------------------------------------------------------------

    def queue(self, courier_id: str, somente_cadastros: bool = False) -> dict[str, Any]:
        """O que ha para levar agora, com tudo o que a loja ja sabe preenchido.

        Esta e a peca que faz o registro custar dois toques. O pedido ja tem
        produto, quantidade e frete; a zona e o unico dado novo, e mesmo ela
        vem sugerida pelo que aquele cliente pediu da ultima vez.
        """

        hoje = self._agora().date()
        produtos = {linha["id"]: linha for linha in self.store.list("products")}
        ja_em_rota = {run.get("order_id") for run in self.store.list(RUNS) if run.get("order_id")}
        historico = self._zona_por_cliente()

        fila = []
        for order in [] if somente_cadastros else self.store.list("orders"):
            if not is_active_order(order) or not is_delivery_order(order):
                continue
            if self.erp._local_day(order["created_at"]) != hoje:
                continue
            if order["id"] in ja_em_rota:
                continue
            # Pedido ja marcado para outro entregador nao e desta fila.
            if order.get("courier_id") and order["courier_id"] != courier_id:
                continue
            cliente = order.get("customer_name")
            sugerida = historico.get(self._fold(cliente or "")) if cliente else None
            fila.append({
                "order_id": order["id"],
                "number": order.get("number"),
                "customer_name": cliente,
                "created_at": order["created_at"],
                "minutes_waiting": max(0, int((self._agora() - self.erp._timestamp(order["created_at"]).astimezone(FUSO_LOJA)).total_seconds() // 60)),
                "total": order.get("total"),
                "freight": order.get("delivery_fee") or 0,
                "payment_method": order.get("payment_method"),
                "items": [
                    {
                        "product_id": item["product_id"],
                        "name": (produtos.get(item["product_id"]) or {}).get("name", "Produto removido"),
                        "quantity": item["quantity"],
                    }
                    for item in order.get("items") or []
                ],
                "units": sum(item["quantity"] for item in order.get("items") or []),
                # O destino, quando o balcao o informou na venda. A zona vem
                # escolhida e o endereco vem escrito: e a diferenca entre o
                # entregador sair sabendo para onde vai e sair perguntando.
                "zone_id": order.get("delivery_zone_id"),
                "address": order.get("delivery_address"),
                # A zona que este cliente usou da ultima vez. Sugestao, nunca
                # escolha: endereco de entrega muda, e gravar sozinho poluiria
                # exatamente o dado que a zona existe para manter limpo. So vale
                # quando a venda nao trouxe a zona.
                "suggested_zone_id": order.get("delivery_zone_id") or sugerida,
            })
        fila.sort(key=lambda row: row["created_at"])
        return {
            "date": hoje.isoformat(),
            "orders": fila,
            "zones": self.zones(include_inactive=False),
            # O cardapio vai junto por causa da entrega que nao tem pedido no
            # sistema — a encomenda combinada por telefone. Sem ele o
            # entregador teria de descrever o que levou por escrito, e o
            # estoque continuaria cego para o que saiu.
            "products": [
                {"id": linha["id"], "name": linha["name"], "price": linha.get("price")}
                for linha in sorted(produtos.values(), key=lambda row: str(row.get("name", "")))
                if linha.get("active", True)
            ],
        }

    def register_run(self, courier: dict[str, Any], payload: DeliveryRunCreate) -> dict[str, Any]:
        agora = self._agora()
        quando = self._momento(payload.occurred_at) or agora
        if quando > agora:
            raise ValueError("Não dá para registrar uma entrega no futuro.")
        limite = agora - timedelta(days=DIAS_PARA_RETROAGIR)
        if quando < limite:
            raise ValueError(
                f"Só dá para lançar entrega dos últimos {DIAS_PARA_RETROAGIR} dias. "
                "Para uma data mais antiga, peça para a loja registrar."
            )
        retroativa = quando.date() != agora.date()

        # O turno e a unidade de pagamento, entao a entrega de hoje continua
        # exigindo check-in. A de outro dia nao: obrigar a reabrir um turno
        # encerrado para lancar o que ficou esquecido faria o esquecimento ser
        # a opcao mais barata, e o registro perdido de vez.
        shift = self._turno_do_dia(courier["id"], quando.date()) if retroativa else self.open_shift(courier["id"])
        if not shift and not retroativa:
            raise ValueError("Faça o check-in antes de registrar uma entrega.")

        zone = self.store.get(ZONES, payload.zone_id)
        if not zone:
            raise ValueError("Zona não encontrada.")

        order = None
        if payload.order_id:
            order = self.store.get("orders", payload.order_id)
            if not order:
                raise ValueError("Pedido não encontrado.")
            if not is_active_order(order):
                raise ValueError("Este pedido foi cancelado.")
            duplicada = next(
                (run for run in self.store.list(RUNS) if run.get("order_id") == payload.order_id),
                None,
            )
            if duplicada:
                # Dois entregadores tocando no mesmo pedido da fila e o acidente
                # normal numa noite cheia; contar duas vezes inflaria a entrega
                # de um deles e o acerto sairia errado para os dois.
                raise ValueError("Este pedido já foi pego por outra entrega.")

        produtos = {linha["id"]: linha for linha in self.store.list("products")}
        itens = self._itens_da_entrega(payload, order, produtos)
        frete = payload.freight
        if frete is None:
            frete = (order or {}).get("delivery_fee")
        if frete is None:
            frete = zone.get("default_fee", 0)

        run = {
            "id": str(uuid4()),
            # Entrega de dia sem turno registrado fica sem turno em vez de ser
            # empurrada para o de hoje: o acerto de hoje nao pode receber o
            # trabalho de ontem.
            "shift_id": shift["id"] if shift else None,
            "courier_id": courier["id"],
            "date": quando.date().isoformat(),
            "zone_id": zone["id"],
            "zone_name": zone["name"],
            "order_id": payload.order_id,
            "customer_name": self.erp._optional_text(payload.customer_name) or (order or {}).get("customer_name"),
            "reference": self.erp._optional_text(payload.reference),
            "items": itens,
            "units": sum(item["quantity"] for item in itens),
            "freight": round(float(frete), 2),
            # Valor do pedido, quando ele existe. E o que permite ler
            # faturamento por zona sem recalcular ficha tecnica nenhuma.
            "order_total": (order or {}).get("total"),
            "departed_at": quando.isoformat(),
            # A entrega de outro dia ja chegou: ninguem vai tocar em "Cheguei"
            # amanha. Ela nasce encerrada, e com duracao **desconhecida** em
            # vez de calculada — a diferenca entre a saida de ontem e o relogio
            # de agora seriam horas, e essa mentira entraria na mediana de
            # tempo de todas as zonas.
            "finished_at": quando.isoformat() if retroativa else None,
            "minutes": None,
            "failed": False,
            "backdated": retroativa,
            "address": self.erp._optional_text(
                (self.store.get("orders", payload.order_id) or {}).get("delivery_address") if payload.order_id else None
            ),
            "note": self.erp._optional_text(payload.note),
            "created_at": agora.isoformat(),
        }
        self.store.put(RUNS, run["id"], run)

        # Entrega sem pedido no sistema traz uma venda junto. Ela nao vira
        # faturamento agora: fica a conferir, e a loja e que a promove.
        if not payload.order_id and run["items"]:
            run["pending_order_id"] = self._abrir_pendente(run, courier, payload)["id"]
            self.store.put(RUNS, run["id"], run)

        # Entrega lancada com atraso num turno ja fechado refaz o acerto
        # daquele dia. `_lancar_turno` procura pelo `shift_id` e atualiza em
        # vez de duplicar, entao o pagamento se corrige sozinho — sem isso o
        # entregador so descobriria a falta no fim do mes.
        if retroativa and shift and self._acerto_do_turno(shift["id"]):
            self._resettlement(shift, courier)

        # O pedido passa a ter dono e endereco. Sem isso a conferencia do
        # acerto continuaria acusando "pedido sem entregador".
        if order is not None:
            order["courier_id"] = courier["id"]
            order["zone_id"] = zone["id"]
            order["zone_name"] = zone["name"]
            self.store.put("orders", order["id"], order)

        self.erp._audit(
            self.store, "run", run["id"], "saiu",
            f"{courier['name']} saiu para {zone['name']} com {run['units']} unidade(s)",
        )
        return self._decorar_entrega(run)

    def finish_run(self, courier: dict[str, Any], run_id: str, payload: DeliveryRunFinish) -> dict[str, Any]:
        run = self.store.get(RUNS, run_id)
        if not run:
            raise ValueError("Entrega não encontrada.")
        if run["courier_id"] != courier["id"]:
            raise ValueError("Esta entrega é de outro entregador.")
        if run.get("finished_at"):
            raise ValueError("Esta entrega já foi encerrada.")

        agora = self._agora()
        run["finished_at"] = agora.isoformat()
        run["failed"] = payload.failed
        if payload.note:
            run["note"] = self.erp._optional_text(payload.note)
        run["minutes"] = self._minutos(run["departed_at"], run["finished_at"])
        self.store.put(RUNS, run_id, run)
        self.erp._audit(
            self.store, "run", run_id, "não entregue" if payload.failed else "entregue",
            f"{courier['name']} · {run['zone_name']} · {run['minutes']} min",
        )
        return self._decorar_entrega(run)

    def update_run(self, run_id: str, payload: DeliveryRunUpdate) -> dict[str, Any]:
        run = self.store.get(RUNS, run_id)
        if not run:
            raise ValueError("Entrega não encontrada.")
        mudancas = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None or k in ("note", "reference")}
        if "zone_id" in mudancas:
            zone = self.store.get(ZONES, mudancas["zone_id"])
            if not zone:
                raise ValueError("Zona não encontrada.")
            mudancas["zone_name"] = zone["name"]
        antes = {k: run.get(k) for k in mudancas}
        run.update(mudancas)
        run["updated_at"] = self._agora().isoformat()
        self.store.put(RUNS, run_id, run)
        self.erp._audit(
            self.store, "run", run_id, "corrigida",
            f"Entrega em {run['zone_name']} corrigida", self.erp._diff(antes, mudancas),
        )
        return self._decorar_entrega(run)

    def delete_run(self, run_id: str) -> dict[str, str]:
        run = self.store.get(RUNS, run_id)
        if not run:
            raise ValueError("Entrega não encontrada.")
        self.store.delete(RUNS, run_id)
        self.erp._audit(self.store, "run", run_id, "excluida", f"Entrega em {run.get('zone_name', 'zona')} removida")
        return {"id": run_id, "status": "excluido"}

    def runs(self, days: int = 30, courier_id: str | None = None, zone_id: str | None = None) -> list[dict[str, Any]]:
        inicio = self._agora().date() - timedelta(days=max(1, min(days, 365)) - 1)
        linhas = []
        for run in self.store.list(RUNS):
            dia = self._dia(run.get("date"))
            if not dia or dia < inicio:
                continue
            if courier_id and run.get("courier_id") != courier_id:
                continue
            if zone_id and run.get("zone_id") != zone_id:
                continue
            linhas.append(self._decorar_entrega(run))
        linhas.sort(key=lambda row: str(row.get("departed_at", "")), reverse=True)
        return linhas

    # ------------------------------------------------------------------
    # A tela do entregador
    # ------------------------------------------------------------------

    def field_state(self, courier: dict[str, Any]) -> dict[str, Any]:
        """Tudo o que o aplicativo de rua precisa, numa chamada so.

        O celular do entregador esta na calcada, com sinal ruim. Trinta e
        quatro requisicoes como o painel da loja faz seria uma tela branca; e
        uma tela branca na calcada e um registro que nao acontece.
        """

        shift = self.open_shift(courier["id"])
        entregas = self._runs_do_turno(shift["id"]) if shift else []
        entregues = [run for run in entregas if run.get("finished_at") and not run.get("failed")]
        return {
            "courier": {
                "id": courier["id"],
                "name": courier["name"],
                "vehicle": courier.get("vehicle"),
            },
            "shift": self._decorar_turno(shift, courier) if shift else None,
            "runs": [self._decorar_entrega(run) for run in sorted(entregas, key=lambda row: str(row.get("departed_at", "")), reverse=True)],
            "on_route": [self._decorar_entrega(run) for run in entregas if not run.get("finished_at")],
            "totals": {
                "deliveries": len(entregues),
                "failed": len([run for run in entregas if run.get("failed")]),
                "freight": round(sum(run.get("freight", 0) for run in entregues), 2),
                "units": sum(run.get("units", 0) for run in entregues),
                "minutes_median": self._mediana([run["minutes"] for run in entregues if run.get("minutes") is not None]),
            },
            # Fora de turno nao ha fila, mas zona e cardapio continuam vindo: e
            # exatamente nesse estado que se lanca a entrega esquecida de
            # ontem, e sem o cardapio o formulario abriria sem o que escolher.
            "queue": self.queue(courier["id"]) if shift else self.queue(courier["id"], somente_cadastros=True),
        }

    # ------------------------------------------------------------------
    # A venda que veio da rua
    #
    # O entregador registra a encomenda que a loja nunca lancou — a combinada
    # por telefone, a que o balco nao teve tempo de digitar. Sem isso o
    # faturamento daquela venda simplesmente nao existe no sistema, e o
    # registro de rua fica limitado a logistica.
    #
    # Ela nasce **fora** de `orders`, numa colecao propria. A razao e concreta:
    # `is_active_order` e lista negra ("tudo que nao esta cancelado conta"), e
    # `forecast.py` e `planning.py` tem filtros proprios com a mesma forma. Um
    # pedido "a conferir" dentro de `orders` viraria faturamento em tres
    # lugares no mesmo instante em que fosse gravado. Fora dela, nada existente
    # o enxerga — e a confirmacao e que o promove a pedido de verdade, pela
    # porta normal, com ficha tecnica baixando estoque.
    # ------------------------------------------------------------------

    def _abrir_pendente(self, run: dict[str, Any], courier: dict[str, Any], payload: DeliveryRunCreate) -> dict[str, Any]:
        produtos = {linha["id"]: linha for linha in self.store.list("products")}
        # O frete entra na conta porque o pedido tambem o soma no total: o
        # cliente paga uma coisa so. Comparar um valor com frete contra outro
        # sem frete acusaria divergencia em toda venda.
        sugerido = round(sum(
            (produtos.get(item["product_id"]) or {}).get("price", 0) * item["quantity"]
            for item in run["items"]
        ) + run.get("freight", 0), 2)
        pendente = {
            "id": str(uuid4()),
            "run_id": run["id"],
            "courier_id": courier["id"],
            "courier_name": courier["name"],
            "date": run["date"],
            "occurred_at": run["departed_at"],
            "items": run["items"],
            "units": run["units"],
            "customer_name": run.get("customer_name"),
            "zone_id": run.get("zone_id"),
            "zone_name": run.get("zone_name"),
            "reference": run.get("reference"),
            "freight": run.get("freight", 0),
            # O que a rua declara e o que o cardapio calcularia. Os dois ficam
            # guardados: divergencia entre eles e justamente o que a loja
            # precisa ver na hora de conferir.
            "declared_total": payload.declared_total,
            "suggested_total": sugerido,
            "payment_method": self.erp._optional_text(payload.payment_method),
            "status": "pendente",
            "created_at": self._agora().isoformat(),
        }
        self.store.put(PENDING, pendente["id"], pendente)
        self.erp._audit(
            self.store, "pending-order", pendente["id"], "trazida da rua",
            f"{courier['name']} registrou uma venda sem pedido em {run.get('zone_name')} · "
            f"{_money(payload.declared_total if payload.declared_total is not None else sugerido)}",
        )
        return pendente

    def _garantir_canal_da_rua(self) -> str:
        """Cria o canal da venda de rua na primeira confirmacao, se faltar.

        `create_order` recusa canal que nao esteja no cadastro, e o cadastro e
        semeado com quatro nomes fixos que nao incluem este. Obrigar a loja a
        adivinhar o nome exato antes de confirmar a primeira venda faria a
        funcionalidade morrer numa mensagem de erro. A taxa nasce em zero
        porque entrega direta nao paga comissao a marketplace nenhum.
        """

        if any(linha["name"] == CANAL_RUA for linha in self.erp.channels()):
            return CANAL_RUA
        canal = {
            "id": "channel-entrega-direta",
            "name": CANAL_RUA,
            "fee_percent": 0.0,
            "fee_fixed": 0.0,
            "active": True,
            "note": "Venda que o entregador trouxe da rua e a loja confirmou. Sem comissão de marketplace.",
        }
        self.store.put("channels", canal["id"], canal)
        self.erp._audit(
            self.store, "channel", canal["id"], "criado",
            f"Canal {CANAL_RUA} criado na primeira venda confirmada da rua",
        )
        return CANAL_RUA

    def pending_orders(self, days: int = 30, include_done: bool = False) -> list[dict[str, Any]]:
        inicio = self._agora().date() - timedelta(days=max(1, min(days, 365)) - 1)
        linhas = []
        for pendente in self.store.list(PENDING):
            dia = self._dia(pendente.get("date"))
            if not dia or dia < inicio:
                continue
            if not include_done and pendente.get("status") != "pendente":
                continue
            linhas.append(pendente)
        linhas.sort(key=lambda row: str(row.get("occurred_at", "")), reverse=True)
        return linhas

    def confirm_pending(self, pending_id: str, payload: PendingOrderConfirm) -> dict[str, Any]:
        """Promove a venda da rua a pedido de verdade.

        Daqui em diante ela e um pedido como qualquer outro: a ficha tecnica
        baixa o insumo, o custo entra no resultado, a previsao aprende com ela.
        E o unico ponto do sistema em que isso acontece, e por isso ele e um
        ato explicito de alguem da loja.
        """

        from app.schemas import OrderCreate, OrderLine

        pendente = self.store.get(PENDING, pending_id)
        if not pendente:
            raise ValueError("Venda não encontrada.")
        if pendente.get("status") != "pendente":
            raise ValueError(f"Esta venda já foi {pendente.get('status')}.")
        if not pendente.get("items"):
            raise ValueError("A venda não tem itens para lançar.")

        momento = self._momento(pendente.get("occurred_at")) or self._agora()
        canal = payload.channel or self._garantir_canal_da_rua()
        pedido = self.erp.create_order(OrderCreate(
            items=[OrderLine(product_id=item["product_id"], quantity=item["quantity"]) for item in pendente["items"]],
            channel=canal,
            payment_method=payload.payment_method or pendente.get("payment_method") or "Dinheiro",
            customer_name=pendente.get("customer_name"),
            is_delivery=True,
            delivery_fee=pendente.get("freight", 0),
            courier_id=pendente["courier_id"],
            occurred_at=momento,
        ))

        # O cardapio calcula um total; a rua recebeu outro. Quem manda e o que
        # entrou no caixa — a mesma regra que a integracao do iFood aplica, e
        # pela mesma razao: o dinheiro e um fato, o preco de tabela e uma
        # expectativa. A diferenca fica gravada em vez de ser alisada.
        cobrado = payload.total if payload.total is not None else pendente.get("declared_total")
        catalogo = pedido["total"]
        if cobrado is not None and abs(cobrado - catalogo) >= 0.01:
            pedido.update({
                "total": round(cobrado, 2),
                "catalog_total": catalogo,
                "revenue_conflict": True,
            })
            self.store.put("orders", pedido["id"], pedido)

        pendente.update({
            "status": "confirmada",
            "order_id": pedido["id"],
            "confirmed_at": self._agora().isoformat(),
            "confirmed_total": pedido["total"],
            "note": self.erp._optional_text(payload.note),
        })
        self.store.put(PENDING, pending_id, pendente)

        # A entrega passa a apontar para o pedido, e o pedido para a zona: sem
        # isso a venda ficaria fora da leitura de territorio que a rua gerou.
        run = self.store.get(RUNS, pendente["run_id"])
        if run:
            run["order_id"] = pedido["id"]
            run["order_total"] = pedido["total"]
            self.store.put(RUNS, run["id"], run)

        self.erp._audit(
            self.store, "pending-order", pending_id, "confirmada",
            f"Venda de {pendente['courier_name']} confirmada como pedido {pedido.get('number', pedido['id'])} · {_money(pedido['total'])}",
        )
        return {**pendente, "order": pedido}

    def reject_pending(self, pending_id: str, payload: PendingOrderReject) -> dict[str, Any]:
        pendente = self.store.get(PENDING, pending_id)
        if not pendente:
            raise ValueError("Venda não encontrada.")
        if pendente.get("status") != "pendente":
            raise ValueError(f"Esta venda já foi {pendente.get('status')}.")
        pendente.update({
            "status": "recusada",
            "rejected_at": self._agora().isoformat(),
            "reason": payload.reason.strip(),
        })
        self.store.put(PENDING, pending_id, pendente)
        # A entrega em si nao some: ela rodou a rua e conta no acerto do
        # entregador mesmo quando a venda nao vira faturamento.
        self.erp._audit(
            self.store, "pending-order", pending_id, "recusada",
            f"Venda de {pendente['courier_name']} recusada: {payload.reason.strip()}",
        )
        return pendente

    # ------------------------------------------------------------------
    # Historico
    # ------------------------------------------------------------------

    def field_history(self, courier: dict[str, Any], days: int = 30) -> dict[str, Any]:
        """Os turnos e as entregas do proprio entregador, com hora.

        A tela de rua mostrava so o turno aberto: turno fechado e entrega de
        ontem sumiam da vista. O dado sempre esteve gravado — `checkin_at` e
        `checkout_at` guardam a hora desde o primeiro dia —, faltava alguem
        exibir. Sem isso o entregador nao tem como conferir o proprio acerto,
        e conferir o proprio acerto e a razao de o registro ser feito.
        """

        days = max(1, min(days, 365))
        inicio = self._agora().date() - timedelta(days=days - 1)

        por_turno: dict[str, list[dict[str, Any]]] = defaultdict(list)
        soltas: list[dict[str, Any]] = []
        for run in self.store.list(RUNS):
            if run.get("courier_id") != courier["id"]:
                continue
            dia = self._dia(run.get("date"))
            if not dia or dia < inicio:
                continue
            if run.get("shift_id"):
                por_turno[run["shift_id"]].append(run)
            else:
                soltas.append(run)

        turnos = []
        for shift in self.store.list(SHIFTS):
            if shift.get("courier_id") != courier["id"]:
                continue
            dia = self._dia(shift.get("date"))
            if not dia or dia < inicio:
                continue
            entregas = por_turno.pop(shift["id"], [])
            concluidas = [run for run in entregas if run.get("finished_at") and not run.get("failed")]
            minutos = [run["minutes"] for run in concluidas if run.get("minutes") is not None]
            turnos.append({
                "id": shift["id"],
                "date": shift["date"],
                "checkin_at": shift.get("checkin_at"),
                "checkout_at": shift.get("checkout_at"),
                "open": not shift.get("checkout_at"),
                "auto_closed": shift.get("auto_closed"),
                "minutes": self._minutos(shift.get("checkin_at"), shift.get("checkout_at") or self._agora().isoformat()),
                "deliveries": len(concluidas),
                "failed": len([run for run in entregas if run.get("failed")]),
                "freight": round(sum(run.get("freight", 0) for run in concluidas), 2),
                "minutes_median": self._mediana(minutos),
                "runs": [self._decorar_entrega(run) for run in sorted(entregas, key=lambda row: str(row.get("departed_at", "")))],
            })
        turnos.sort(key=lambda linha: str(linha["date"]), reverse=True)

        # Entrega lancada para um dia sem turno registrado. Ela existe, conta
        # no territorio e precisa aparecer — escondida, viraria trabalho feito
        # e nao pago.
        avulsas = sorted(soltas + [run for grupo in por_turno.values() for run in grupo],
                         key=lambda row: str(row.get("departed_at", "")), reverse=True)
        entregues = [run for turno in turnos for run in turno["runs"] if not run.get("failed") and run.get("finished_at")]
        return {
            "period_days": days,
            "shifts": turnos,
            "loose_runs": [self._decorar_entrega(run) for run in avulsas],
            "totals": {
                "shifts": len(turnos),
                "deliveries": len(entregues) + len([run for run in avulsas if not run.get("failed")]),
                "freight": round(
                    sum(run.get("freight", 0) for run in entregues)
                    + sum(run.get("freight", 0) for run in avulsas if not run.get("failed")), 2),
                "hours": round(sum(turno["minutes"] for turno in turnos) / 60, 1),
            },
        }

    def shifts(self, days: int = 30, courier_id: str | None = None) -> list[dict[str, Any]]:
        """O ponto de todo mundo, para a loja: quem entrou, quando e por quanto tempo."""

        days = max(1, min(days, 365))
        inicio = self._agora().date() - timedelta(days=days - 1)
        entregadores = {linha["id"]: linha for linha in self.store.list("couriers")}
        por_turno: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for run in self.store.list(RUNS):
            if run.get("shift_id"):
                por_turno[run["shift_id"]].append(run)

        linhas = []
        for shift in self.store.list(SHIFTS):
            dia = self._dia(shift.get("date"))
            if not dia or dia < inicio:
                continue
            if courier_id and shift.get("courier_id") != courier_id:
                continue
            entregas = por_turno.get(shift["id"], [])
            concluidas = [run for run in entregas if run.get("finished_at") and not run.get("failed")]
            linhas.append({
                "id": shift["id"],
                "courier_id": shift["courier_id"],
                "courier_name": (entregadores.get(shift["courier_id"]) or {}).get("name", "Entregador removido"),
                "date": shift["date"],
                "checkin_at": shift.get("checkin_at"),
                "checkout_at": shift.get("checkout_at"),
                "open": not shift.get("checkout_at"),
                # Turno que o sistema fechou sozinho tem duracao inventada; a
                # tela precisa dizer isso em vez de mostrar zero minuto como
                # se fosse medida.
                "auto_closed": shift.get("auto_closed"),
                "left_on_route": shift.get("left_on_route", 0),
                "minutes": self._minutos(shift.get("checkin_at"), shift.get("checkout_at") or self._agora().isoformat()),
                "deliveries": len(concluidas),
                "freight": round(sum(run.get("freight", 0) for run in concluidas), 2),
            })
        linhas.sort(key=lambda linha: (str(linha["date"]), str(linha["checkin_at"])), reverse=True)
        return linhas

    # ------------------------------------------------------------------
    # O painel da loja
    # ------------------------------------------------------------------

    def board(self, days: int = 30) -> dict[str, Any]:
        """Acompanhamento: quem esta na rua agora e o que o periodo mostra."""

        days = max(1, min(days, 365))
        hoje = self._agora().date()
        inicio = hoje - timedelta(days=days - 1)
        entregadores = {linha["id"]: linha for linha in self.store.list("couriers")}
        zonas = {linha["id"]: linha for linha in self.store.list(ZONES)}

        entregas = [
            run for run in self.store.list(RUNS)
            if (dia := self._dia(run.get("date"))) and inicio <= dia <= hoje
        ]

        presenca = self._presenca(entregadores)
        por_zona = self._por_zona(entregas, zonas, days)
        totais = self._totais(entregas)
        return {
            "period_days": days,
            "now": presenca,
            "zones": por_zona,
            "couriers": self._por_entregador(entregas, entregadores),
            "series": self._serie(entregas, hoje, days),
            "hours": self._por_hora(entregas),
            "totals": totais,
            "readings": self._leituras(presenca, por_zona, totais),
        }

    def _presenca(self, entregadores: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Quem esta em turno neste momento, e ha quanto tempo."""

        agora = self._agora()
        zonas = {linha["id"]: linha for linha in self.store.list(ZONES)}
        # Uma varredura das entregas serve a todos os turnos: chamar
        # `_runs_do_turno` por entregador releria a colecao inteira a cada um.
        por_turno: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for run in self.store.list(RUNS):
            por_turno[run.get("shift_id")].append(run)

        em_turno = []
        for courier_id, courier in entregadores.items():
            shift = self.open_shift(courier_id)
            if not shift:
                continue
            entregas = por_turno.get(shift["id"], [])
            em_rota = [run for run in entregas if not run.get("finished_at")]
            entregues = [run for run in entregas if run.get("finished_at") and not run.get("failed")]
            em_turno.append({
                "courier_id": courier_id,
                "name": courier["name"],
                "vehicle": courier.get("vehicle"),
                "checkin_at": shift["checkin_at"],
                "minutes_on_shift": self._minutos(shift["checkin_at"], agora.isoformat()),
                "deliveries": len(entregues),
                "freight": round(sum(run.get("freight", 0) for run in entregues), 2),
                "on_route": [
                    {
                        "id": run["id"],
                        "zone_name": run.get("zone_name"),
                        "customer_name": run.get("customer_name"),
                        "units": run.get("units", 0),
                        "minutes": self._minutos(run["departed_at"], agora.isoformat()),
                        # Passou do combinado para aquela zona. E o unico
                        # numero desta tela que pede uma acao agora.
                        "late": self._atrasada(run, zonas, agora),
                    }
                    for run in sorted(em_rota, key=lambda row: str(row.get("departed_at", "")))
                ],
            })
        em_turno.sort(key=lambda row: row["checkin_at"])
        return {
            "on_shift": em_turno,
            "on_route": sum(len(linha["on_route"]) for linha in em_turno),
            "checked_in": len(em_turno),
        }

    def _atrasada(self, run: dict[str, Any], zonas: dict[str, Any], agora: datetime) -> bool:
        """Passou do tempo combinado para aquela zona.

        Zona sem tempo combinado nunca atrasa: um limite inventado aqui faria a
        tela apontar problema onde a loja nao definiu expectativa nenhuma.
        """

        alvo = (zonas.get(run.get("zone_id")) or {}).get("target_minutes")
        return bool(alvo) and self._minutos(run["departed_at"], agora.isoformat()) > alvo

    def _por_zona(self, entregas: list[dict[str, Any]], zonas: dict[str, Any], days: int) -> list[dict[str, Any]]:
        linhas: dict[str, dict[str, Any]] = {}
        for run in entregas:
            if run.get("failed"):
                continue
            zone_id = run.get("zone_id") or "sem-zona"
            linha = linhas.setdefault(zone_id, {
                "zone_id": zone_id,
                "name": run.get("zone_name") or "Sem zona",
                "deliveries": 0, "units": 0, "freight": 0.0, "revenue": 0.0,
                "minutes": [], "customers": set(), "days": set(),
            })
            linha["deliveries"] += 1
            linha["units"] += run.get("units", 0)
            linha["freight"] += run.get("freight", 0)
            linha["revenue"] += run.get("order_total") or 0
            if run.get("minutes") is not None:
                linha["minutes"].append(run["minutes"])
            if run.get("customer_name"):
                linha["customers"].add(self._fold(run["customer_name"]))
            linha["days"].add(run.get("date"))

        total_entregas = sum(linha["deliveries"] for linha in linhas.values())
        resultado = []
        for linha in linhas.values():
            zona = zonas.get(linha["zone_id"], {})
            alvo = zona.get("target_minutes")
            mediana = self._mediana(linha["minutes"])
            resultado.append({
                "zone_id": linha["zone_id"],
                "name": linha["name"],
                "deliveries": linha["deliveries"],
                "units": linha["units"],
                "freight": round(linha["freight"], 2),
                "revenue": round(linha["revenue"], 2),
                "share": round(linha["deliveries"] / total_entregas * 100, 1) if total_entregas else 0.0,
                "per_day": round(linha["deliveries"] / days, 2),
                "customers": len(linha["customers"]),
                "ticket": round(linha["revenue"] / linha["deliveries"], 2) if linha["deliveries"] and linha["revenue"] else None,
                "minutes_median": mediana,
                "target_minutes": alvo,
                # Acima do combinado, medido na mediana e nao na media: uma
                # entrega perdida numa madrugada nao pode condenar a zona.
                "over_target": bool(alvo and mediana is not None and mediana > alvo),
                # Amostra pequena continua aparecendo, mas marcada: esconder
                # faria a zona nova parecer inexistente em vez de recente.
                "thin": linha["deliveries"] < ENTREGAS_MINIMAS_POR_ZONA,
            })
        resultado.sort(key=lambda row: row["deliveries"], reverse=True)
        return resultado

    def _por_entregador(self, entregas: list[dict[str, Any]], entregadores: dict[str, Any]) -> list[dict[str, Any]]:
        linhas: dict[str, dict[str, Any]] = {}
        for run in entregas:
            courier_id = run.get("courier_id")
            linha = linhas.setdefault(courier_id, {
                "courier_id": courier_id,
                "name": (entregadores.get(courier_id) or {}).get("name", "Entregador removido"),
                "deliveries": 0, "failed": 0, "units": 0, "freight": 0.0,
                "minutes": [], "zones": set(), "shifts": set(),
            })
            if run.get("failed"):
                linha["failed"] += 1
            else:
                linha["deliveries"] += 1
                linha["units"] += run.get("units", 0)
                linha["freight"] += run.get("freight", 0)
                if run.get("minutes") is not None:
                    linha["minutes"].append(run["minutes"])
            linha["zones"].add(run.get("zone_name"))
            linha["shifts"].add(run.get("shift_id"))

        resultado = []
        for linha in linhas.values():
            turnos = len(linha["shifts"])
            resultado.append({
                "courier_id": linha["courier_id"],
                "name": linha["name"],
                "deliveries": linha["deliveries"],
                "failed": linha["failed"],
                "units": linha["units"],
                "freight": round(linha["freight"], 2),
                "zones": len(linha["zones"]),
                "shifts": turnos,
                "per_shift": round(linha["deliveries"] / turnos, 1) if turnos else 0.0,
                "minutes_median": self._mediana(linha["minutes"]),
            })
        resultado.sort(key=lambda row: row["deliveries"], reverse=True)
        return resultado

    def _serie(self, entregas: list[dict[str, Any]], hoje: date, days: int) -> list[dict[str, Any]]:
        por_dia: dict[str, dict[str, float]] = defaultdict(lambda: {"deliveries": 0, "freight": 0.0})
        for run in entregas:
            if run.get("failed"):
                continue
            por_dia[run["date"]]["deliveries"] += 1
            por_dia[run["date"]]["freight"] += run.get("freight", 0)
        janela = min(days, 30)
        serie = []
        for offset in range(janela - 1, -1, -1):
            dia = (hoje - timedelta(days=offset)).isoformat()
            linha = por_dia.get(dia, {"deliveries": 0, "freight": 0.0})
            serie.append({"date": dia, "deliveries": int(linha["deliveries"]), "freight": round(linha["freight"], 2)})
        return serie

    def _por_hora(self, entregas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Em que hora a rua fica cheia. E o que decide escala, nao o total do dia."""

        contagem: Counter[int] = Counter()
        for run in entregas:
            if run.get("failed"):
                continue
            momento = self._momento(run.get("departed_at"))
            if momento:
                contagem[momento.hour] += 1
        if not contagem:
            return []
        pico = max(contagem.values())
        return [
            {"hour": hora, "deliveries": contagem.get(hora, 0), "share": round(contagem.get(hora, 0) / pico * 100, 1)}
            for hora in range(min(contagem), max(contagem) + 1)
        ]

    def _totais(self, entregas: list[dict[str, Any]]) -> dict[str, Any]:
        entregues = [run for run in entregas if not run.get("failed")]
        minutos = [run["minutes"] for run in entregues if run.get("minutes") is not None]
        return {
            "deliveries": len(entregues),
            "failed": len(entregas) - len(entregues),
            "units": sum(run.get("units", 0) for run in entregues),
            "freight": round(sum(run.get("freight", 0) for run in entregues), 2),
            "revenue": round(sum(run.get("order_total") or 0 for run in entregues), 2),
            "minutes_median": self._mediana(minutos),
            "minutes_worst": max(minutos) if minutos else None,
        }

    def _leituras(self, presenca: dict[str, Any], zonas: list[dict[str, Any]], totais: dict[str, Any]) -> list[dict[str, str]]:
        """As frases que pedem uma decisao agora, e so elas.

        O painel ja mostra os numeros. Estas leituras existem para o caso em
        que olhar a tabela inteira nao e possivel — sabado a noite, loja cheia
        — e o gestor precisa saber se tem alguma coisa fora do lugar.
        """

        leituras: list[dict[str, str]] = []

        if not totais["deliveries"] and not presenca["checked_in"]:
            leituras.append({
                "type": "info",
                "title": "Nenhuma entrega registrada no período",
                "description": (
                    "Cadastre as zonas em Entrega e peça para os entregadores fazerem check-in pelo aplicativo de rua. "
                    "É o check-out deles que passa a montar o acerto do turno sozinho."
                ),
            })
            return leituras

        atrasadas = [
            linha for pessoa in presenca["on_shift"] for linha in pessoa["on_route"] if linha["late"]
        ]
        if atrasadas:
            pior = max(atrasadas, key=lambda linha: linha["minutes"])
            leituras.append({
                "type": "attention",
                "title": f"{len(atrasadas)} entrega(s) passaram do tempo combinado",
                "description": (
                    f"A mais antiga está há {pior['minutes']} min a caminho de {pior['zone_name']}. "
                    "Vale avisar o cliente antes de ele ligar."
                ),
            })

        # Zona que passa do combinado de forma sistematica nao e dia ruim: ou o
        # tempo prometido nao cabe na distancia, ou o frete nao paga a viagem.
        lentas = [linha for linha in zonas if linha["over_target"] and not linha["thin"]]
        if lentas:
            pior = max(lentas, key=lambda linha: linha["minutes_median"] - linha["target_minutes"])
            leituras.append({
                "type": "attention",
                "title": f"{pior['name']} demora mais do que o combinado",
                "description": (
                    f"A mediana ali é de {pior['minutes_median']} min contra {pior['target_minutes']} min combinados, "
                    f"em {pior['deliveries']} entregas. Ou o tempo prometido não cabe na distância, ou o frete não paga a viagem."
                ),
            })

        if totais["failed"]:
            leituras.append({
                "type": "attention",
                "title": f"{totais['failed']} entrega(s) não chegaram ao cliente",
                "description": (
                    "Entrega que voltou consumiu a rua igual. Cada uma tem o motivo registrado pelo entregador, "
                    "e elas ficam fora da média de tempo para não mascarar o atendimento que funcionou."
                ),
            })

        concentradas = [linha for linha in zonas if linha["share"] >= 40 and linha["deliveries"] >= ENTREGAS_MINIMAS_POR_ZONA]
        if concentradas:
            linha = concentradas[0]
            leituras.append({
                "type": "opportunity",
                "title": f"{_num(linha['share'])}% das entregas saem para {linha['name']}",
                "description": (
                    f"São {linha['deliveries']} entregas e {linha['customers']} cliente(s) diferentes. "
                    "Concentração desse tamanho torna a zona o lugar certo para testar promoção e ampliar linha — "
                    "e também o risco: perder aquele bairro é perder quase metade do delivery."
                ),
            })

        sem_zona = next((linha for linha in zonas if linha["zone_id"] == "sem-zona"), None)
        if sem_zona:
            leituras.append({
                "type": "attention",
                "title": f"{sem_zona['deliveries']} entrega(s) sem zona",
                "description": "Sem a zona marcada, elas não entram em nenhuma leitura de território. Corrija cada uma na lista de entregas.",
            })

        return leituras

    # ------------------------------------------------------------------
    # O que a rua ensina
    #
    # Nada aqui e caixa-preta, pela mesma razao que `forecast.py` recusa numpy
    # e sklearn: a previsao de uma zona e o produto de tres numeros que
    # aparecem na tela — a media diaria, o peso daquele dia da semana e a
    # tendencia das ultimas semanas. O gestor precisa poder discordar de um
    # deles, e para discordar ele precisa ver qual.
    #
    # A conta de "este bairro pede mais X" e lift, a mesma medida que
    # `growth.py` usa para combo: quantas vezes mais o produto aparece naquela
    # zona do que apareceria se a zona nao mudasse nada. Lift alto com amostra
    # pequena e coincidencia, entao todo numero carrega quantas entregas ele
    # tem por tras — e a tela mostra isso, em vez de esconder a fraqueza atras
    # de uma casa decimal.
    # ------------------------------------------------------------------

    def territory(self, days: int = 90, horizon: int = 7) -> dict[str, Any]:
        days = max(7, min(days, 365))
        horizon = max(1, min(horizon, 30))
        hoje = self._agora().date()
        inicio = hoje - timedelta(days=days - 1)

        entregas = [
            run for run in self.store.list(RUNS)
            if not run.get("failed") and (dia := self._dia(run.get("date"))) and inicio <= dia <= hoje
        ]
        zonas = {linha["id"]: linha for linha in self.store.list(ZONES)}
        produtos = {linha["id"]: linha for linha in self.store.list("products")}

        # Dias em que a loja de fato entregou. Dividir pelo calendario faria
        # toda media cair na proporcao dos dias fechados — e esta loja nao abre
        # todo dia.
        dias_com_entrega = {run["date"] for run in entregas}
        base = {
            "deliveries": len(entregas),
            "days_in_window": days,
            "days_with_delivery": len(dias_com_entrega),
            "first_day": min(dias_com_entrega) if dias_com_entrega else None,
            "zones_seen": len({run.get("zone_id") for run in entregas}),
        }

        if not entregas:
            return {
                "period_days": days,
                "horizon": horizon,
                "basis": base,
                "zones": [],
                "menu": [],
                "status": "sem-dados",
                "note": (
                    "Nenhuma entrega registrada ainda. Cada check-out do entregador alimenta esta tela: "
                    "a partir da segunda semana ela começa a apontar zona, dia e produto."
                ),
            }

        peso_semanal = self._peso_do_dia(entregas)
        linhas = [
            self._aprender_zona(zone_id, entregas, zonas, produtos, peso_semanal, hoje, days, horizon)
            for zone_id in {run.get("zone_id") for run in entregas}
        ]
        linhas.sort(key=lambda row: row["deliveries"], reverse=True)

        return {
            "period_days": days,
            "horizon": horizon,
            "basis": base,
            "weekday_weight": [
                {"weekday": indice, "name": DIAS[indice], "weight": round(peso_semanal.get(indice, 1.0), 2)}
                for indice in range(7)
            ],
            "zones": linhas,
            "menu": self._cardapio_por_zona(entregas, produtos, linhas),
            "status": "medido" if len(entregas) >= ENTREGAS_MINIMAS_PARA_CESTA else "amostra-fraca",
            "note": (
                ""
                if len(entregas) >= ENTREGAS_MINIMAS_PARA_CESTA
                else f"São {len(entregas)} entregas registradas — abaixo das {ENTREGAS_MINIMAS_PARA_CESTA} "
                "necessárias para separar padrão de coincidência. Os números já aparecem, marcados como amostra fraca."
            ),
        }

    def _peso_do_dia(self, entregas: list[dict[str, Any]]) -> dict[int, float]:
        """Quanto cada dia da semana pesa em relacao ao dia medio.

        A loja nao abre todo dia, entao o peso e calculado sobre os dias em que
        houve movimento. Dividir pelo calendario transformaria porta fechada em
        "segunda fraca", que e uma conclusao diferente e errada.
        """

        por_dia: dict[str, int] = defaultdict(int)
        for run in entregas:
            por_dia[run["date"]] += 1
        if not por_dia:
            return {}

        por_semana: dict[int, list[int]] = defaultdict(list)
        for dia, quantas in por_dia.items():
            data = self._dia(dia)
            if data:
                por_semana[data.weekday()].append(quantas)

        media_geral = fmean(por_dia.values())
        if not media_geral:
            return {}
        return {
            indice: fmean(valores) / media_geral
            for indice, valores in por_semana.items()
            if valores
        }

    def _aprender_zona(
        self,
        zone_id: str,
        entregas: list[dict[str, Any]],
        zonas: dict[str, Any],
        produtos: dict[str, Any],
        peso_semanal: dict[int, float],
        hoje: date,
        days: int,
        horizon: int,
    ) -> dict[str, Any]:
        da_zona = [run for run in entregas if run.get("zone_id") == zone_id]
        dias_ativos = {run["date"] for run in da_zona}
        por_dia: dict[str, int] = defaultdict(int)
        for run in da_zona:
            por_dia[run["date"]] += 1

        media = fmean(por_dia.values()) if por_dia else 0.0
        tendencia = self._tendencia(por_dia, hoje)
        previsao = []
        for offset in range(1, horizon + 1):
            dia = hoje + timedelta(days=offset)
            peso = peso_semanal.get(dia.weekday(), 1.0)
            previsao.append({
                "date": dia.isoformat(),
                "weekday": DIAS[dia.weekday()],
                "deliveries": round(media * peso * tendencia, 1),
            })

        # Dispersao observada, e nao intervalo teorico: e o tamanho do chute
        # medido no proprio historico da zona.
        valores = sorted(por_dia.values())
        espalhamento = (valores[-1] - valores[0]) if len(valores) > 1 else 0

        contagem: Counter[str] = Counter()
        for run in da_zona:
            for item in run.get("items") or []:
                contagem[item["product_id"]] += item["quantity"]

        zona = zonas.get(zone_id, {})
        minutos = [run["minutes"] for run in da_zona if run.get("minutes") is not None]
        return {
            "zone_id": zone_id,
            "name": zona.get("name") or (da_zona[0].get("zone_name") if da_zona else "Sem zona"),
            "deliveries": len(da_zona),
            "active_days": len(dias_ativos),
            "units": sum(run.get("units", 0) for run in da_zona),
            "revenue": round(sum(run.get("order_total") or 0 for run in da_zona), 2),
            "freight": round(sum(run.get("freight", 0) for run in da_zona), 2),
            "customers": len({self._fold(run["customer_name"]) for run in da_zona if run.get("customer_name")}),
            "daily_average": round(media, 2),
            "trend": round(tendencia, 2),
            "spread": espalhamento,
            "minutes_median": self._mediana(minutos),
            "target_minutes": zona.get("target_minutes"),
            "forecast": previsao,
            "forecast_total": round(sum(linha["deliveries"] for linha in previsao), 1),
            "best_weekday": self._melhor_dia(da_zona),
            "top_products": [
                {
                    "product_id": product_id,
                    "name": (produtos.get(product_id) or {}).get("name", "Produto removido"),
                    "units": unidades,
                }
                for product_id, unidades in contagem.most_common(5)
            ],
            "thin": len(da_zona) < ENTREGAS_MINIMAS_POR_ZONA,
        }

    def _tendencia(self, por_dia: dict[str, int], hoje: date) -> float:
        """As duas ultimas semanas contra as duas anteriores.

        Limitada entre 0,5 e 1,5 de proposito: uma semana atipica nao pode
        projetar o dobro do movimento para o mes inteiro.
        """

        recente, anterior = [], []
        for dia, quantas in por_dia.items():
            data = self._dia(dia)
            if not data:
                continue
            distancia = (hoje - data).days
            if distancia < 14:
                recente.append(quantas)
            elif distancia < 28:
                anterior.append(quantas)
        if not recente or not anterior:
            return 1.0
        base = fmean(anterior)
        return max(0.5, min(1.5, fmean(recente) / base)) if base else 1.0

    def _melhor_dia(self, entregas: list[dict[str, Any]]) -> dict[str, Any] | None:
        por_semana: dict[int, int] = defaultdict(int)
        for run in entregas:
            data = self._dia(run.get("date"))
            if data:
                por_semana[data.weekday()] += 1
        if not por_semana:
            return None
        indice = max(por_semana, key=lambda chave: por_semana[chave])
        return {"weekday": indice, "name": DIAS[indice], "deliveries": por_semana[indice]}

    def _cardapio_por_zona(
        self, entregas: list[dict[str, Any]], produtos: dict[str, Any], linhas: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """O produto que uma zona pede muito mais que o resto da cidade.

        Esta e a leitura que o cardapio unico esconde: um produto pode ser
        mediano no total da loja e ser o mais pedido de um bairro. Lift alto
        ali significa que aquele publico ja escolheu — e ampliar a linha, ou
        levar a promocao para la, parte de escolha feita e nao de palpite.

        O combo geral, valendo para a loja inteira, continua em Crescimento:
        aqui a pergunta e sempre "onde", nunca "o que" sozinho.
        """

        if len(entregas) < ENTREGAS_MINIMAS_PARA_CESTA:
            return []

        total_unidades: Counter[str] = Counter()
        por_zona: dict[str, Counter[str]] = defaultdict(Counter)
        unidades_da_zona: Counter[str] = Counter()
        for run in entregas:
            zone_id = run.get("zone_id") or "sem-zona"
            for item in run.get("items") or []:
                total_unidades[item["product_id"]] += item["quantity"]
                por_zona[zone_id][item["product_id"]] += item["quantity"]
                unidades_da_zona[zone_id] += item["quantity"]

        geral = sum(total_unidades.values())
        if not geral:
            return []
        nomes = {linha["zone_id"]: linha["name"] for linha in linhas}
        fracos = {linha["zone_id"] for linha in linhas if linha["thin"]}

        sugestoes: list[dict[str, Any]] = []
        for zone_id, contagem in por_zona.items():
            if zone_id in fracos:
                continue
            na_zona = unidades_da_zona[zone_id]
            for product_id, unidades in contagem.most_common(6):
                if unidades < OCORRENCIAS_MINIMAS_DO_PAR:
                    continue
                produto = produtos.get(product_id)
                if not produto:
                    continue
                # Lift: a fatia do produto naquela zona contra a fatia dele na
                # loja inteira. Acima de 1 a zona pede mais que a media.
                fatia_zona = unidades / na_zona if na_zona else 0
                fatia_geral = total_unidades[product_id] / geral
                lift = fatia_zona / fatia_geral if fatia_geral else 0
                if lift < 1.3:
                    continue
                sugestoes.append({
                    "zone_id": zone_id,
                    "zone_name": nomes.get(zone_id, "Sem zona"),
                    "product_id": product_id,
                    "product_name": produto["name"],
                    "units": unidades,
                    "share": round(fatia_zona * 100, 1),
                    "store_share": round(fatia_geral * 100, 1),
                    "lift": round(lift, 2),
                    "title": f"{produto['name']} puxa em {nomes.get(zone_id, 'Sem zona')}",
                    "description": (
                        f"{unidades} unidade(s) entregues ali — {_num(fatia_zona * 100)}% do que sai para a zona, "
                        f"contra {_num(fatia_geral * 100)}% na loja inteira. É {_num(lift)}× a média."
                    ),
                    "action": (
                        f"Vale testar variação de {produto['name']} ou levar a promoção da semana para {nomes.get(zone_id, 'a zona')} "
                        "antes de mexer no cardápio inteiro: a preferência já existe e o teste é barato."
                    ),
                })
        sugestoes.sort(key=lambda row: (row["lift"], row["units"]), reverse=True)
        return sugestoes[:10]

    # ------------------------------------------------------------------
    # Auxiliares
    # ------------------------------------------------------------------

    def _itens_da_entrega(
        self, payload: DeliveryRunCreate, order: dict[str, Any] | None, produtos: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if payload.items:
            linhas = []
            for item in payload.items:
                produto = produtos.get(item.product_id)
                if not produto:
                    raise ValueError("Produto não encontrado no cardápio.")
                linhas.append({"product_id": item.product_id, "name": produto["name"], "quantity": item.quantity})
            return linhas
        return [
            {
                "product_id": item["product_id"],
                "name": (produtos.get(item["product_id"]) or {}).get("name", "Produto removido"),
                "quantity": item["quantity"],
            }
            for item in (order or {}).get("items") or []
        ]

    def _zona_por_cliente(self) -> dict[str, str]:
        """A ultima zona de cada cliente, para a fila abrir quase preenchida."""

        ultima: dict[str, tuple[str, str]] = {}
        for run in self.store.list(RUNS):
            nome = self._fold(run.get("customer_name") or "")
            if not nome or not run.get("zone_id"):
                continue
            quando = str(run.get("departed_at", ""))
            if nome not in ultima or quando > ultima[nome][0]:
                ultima[nome] = (quando, run["zone_id"])
        return {nome: zona for nome, (_, zona) in ultima.items()}

    def _runs_do_turno(self, shift_id: str) -> list[dict[str, Any]]:
        return [run for run in self.store.list(RUNS) if run.get("shift_id") == shift_id]

    def _acerto_do_turno(self, shift_id: str) -> dict[str, Any] | None:
        return next(
            (linha for linha in self.store.list("delivery_entries") if linha.get("shift_id") == shift_id),
            None,
        )

    def _resettlement(self, shift: dict[str, Any], courier: dict[str, Any]) -> None:
        """Recalcula um turno ja fechado a partir das entregas que ele tem agora."""

        entregas = [
            run for run in self._runs_do_turno(shift["id"])
            if run.get("finished_at") and not run.get("failed")
        ]
        shift["deliveries"] = len(entregas)
        shift["freight_total"] = round(sum(run.get("freight", 0) for run in entregas), 2)
        self.store.put(SHIFTS, shift["id"], shift)
        self._lancar_turno(shift, courier)
        self.erp._audit(
            self.store, "shift", shift["id"], "acerto refeito",
            f"Turno de {courier['name']} em {shift['date']} recalculado com entrega lançada depois",
            {"entregas": shift["deliveries"], "frete": shift["freight_total"]},
        )

    def _turno_do_dia(self, courier_id: str, dia: date) -> dict[str, Any] | None:
        """O turno daquele entregador naquela data, se ele existir.

        A entrega lancada com atraso se encaixa no turno do proprio dia — e nao
        no de hoje, que pagaria o trabalho errado. Quando o turno ja foi
        fechado e acertado, o lancamento e refeito na volta: `_lancar_turno`
        procura pelo `shift_id` e atualiza em vez de duplicar, entao a entrega
        atrasada corrige o acerto sozinha.
        """

        candidatos = [
            linha for linha in self.store.list(SHIFTS)
            if linha.get("courier_id") == courier_id and self._dia(linha.get("date")) == dia
        ]
        if not candidatos:
            return None
        candidatos.sort(key=lambda linha: str(linha.get("checkin_at", "")))
        return candidatos[0]

    def _encerrar(self, shift: dict[str, Any], motivo: str) -> None:
        """Fecha o turno que ficou aberto, e paga o que foi entregue nele.

        O fechamento automatico existe para o painel nao mentir. Mas parar por
        ai criaria um problema pior que o painel errado: as entregas ficaram
        registradas e o acerto nunca nasceria, entao o entregador que esqueceu
        de tocar no botao perderia o dia inteiro de trabalho. O lancamento sai
        marcado com o motivo, para o balcao conferir em vez de descobrir na
        reclamacao.

        A hora da saida e a do check-in porque ninguem sabe quando ele foi
        embora, e inventar um horario seria pior que registrar um turno de
        duracao zero — este pelo menos se identifica como o que e.
        """

        entregas = [run for run in self._runs_do_turno(shift["id"]) if not run.get("failed")]
        concluidas = [run for run in entregas if run.get("finished_at")]
        shift["checkout_at"] = shift.get("checkin_at")
        shift["auto_closed"] = motivo
        shift["deliveries"] = len(concluidas)
        shift["freight_total"] = round(sum(run.get("freight", 0) for run in concluidas), 2)
        # Entrega que ficou em rota nao entra no acerto: ninguem confirmou que
        # ela chegou. Ela continua na lista para o balcao encerrar ou excluir,
        # e o numero aqui e o que faz alguem procurar por ela.
        shift["left_on_route"] = len(entregas) - len(concluidas)
        self.store.put(SHIFTS, shift["id"], shift)

        courier = self.store.get("couriers", shift["courier_id"])
        if courier and shift["deliveries"]:
            self._lancar_turno(shift, courier)
        self.erp._audit(
            self.store, "shift", shift["id"], "fechado automaticamente",
            f"Turno de {(courier or {}).get('name', 'entregador')} em {shift['date']} fechado: {motivo}",
            {"entregas": shift["deliveries"], "deixadas em rota": shift["left_on_route"]},
        )

    def _decorar_turno(self, shift: dict[str, Any], courier: dict[str, Any] | None) -> dict[str, Any]:
        fim = shift.get("checkout_at") or self._agora().isoformat()
        return {
            **shift,
            "courier_name": (courier or {}).get("name", "Entregador removido"),
            "open": not shift.get("checkout_at"),
            "minutes": self._minutos(shift["checkin_at"], fim),
        }

    def _decorar_entrega(self, run: dict[str, Any]) -> dict[str, Any]:
        agora = self._agora().isoformat()
        return {
            **run,
            "open": not run.get("finished_at"),
            "minutes": run.get("minutes") if run.get("finished_at") else self._minutos(run["departed_at"], agora),
        }

    @staticmethod
    def _agora() -> datetime:
        return datetime.now(FUSO_LOJA)

    @staticmethod
    def _momento(value: Any) -> datetime | None:
        """Momento no fuso da loja, aceitando ISO com ou sem fuso.

        O turno e gravado com fuso; o campo `date` do pedido antigo nem sempre.
        Assumir o fuso da loja no que vem sem ele mantem a conta de minutos
        honesta em vez de estourar uma excecao na tela do entregador.
        """

        if not value:
            return None
        try:
            momento = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=FUSO_LOJA)
        return momento.astimezone(FUSO_LOJA)

    @classmethod
    def _minutos(cls, inicio: Any, fim: Any) -> int:
        comeco, termino = cls._momento(inicio), cls._momento(fim)
        if not comeco or not termino:
            return 0
        return max(0, int((termino - comeco).total_seconds() // 60))

    @classmethod
    def _horas(cls, inicio: Any) -> float:
        return cls._minutos(inicio, cls._agora().isoformat()) / 60

    @classmethod
    def _hora(cls, value: Any) -> str:
        momento = cls._momento(value)
        return momento.strftime("%H:%M") if momento else "--:--"

    @staticmethod
    def _mediana(valores: list[int]) -> int | None:
        return int(round(median(valores))) if valores else None

    @staticmethod
    def _dia(value: Any) -> date | None:
        try:
            return date.fromisoformat(str(value)[:10])
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _fold(value: str) -> str:
        return unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii").strip().lower()

    @classmethod
    def _identifier(cls, prefixo: str, value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", cls._fold(value)).strip("-")
        return f"{prefixo}-{slug or uuid4().hex[:6]}"
