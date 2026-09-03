"""Importa o relatorio de pedidos da Anota Ai para o ERP.

O relatorio traz pedido a pedido, mas nao traz os itens vendidos. O faturamento
e fato conhecido e entra inteiro; o custo fica marcado como desconhecido, e nao
como zero, porque zero significaria "saiu de graca" e inflaria a margem.

Se um dia o painel exportar os itens, o custo desses pedidos deixa de ser
desconhecido: cada item passa pela ficha tecnica e o custo e calculado
retroativamente, sem refazer a importacao.

Uso:
    python scripts/import_anota_ai.py "Relatório de Pedidos.xlsx"
    python scripts/import_anota_ai.py "Relatório de Pedidos.xlsx" --apply
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from app.config import get_settings
from app.repositories.store import Store, build_store

FUSO_LOJA = timezone(timedelta(hours=-3))
ORIGEM = "anota-ai"

# Colunas do relatorio, tal como o painel exporta.
COLUNA_NUMERO = "Número do Pedido"
COLUNA_ORIGEM = "Origem"
COLUNA_PAGAMENTO = "Condição de pagamento"
COLUNA_TOTAL = "Valor total"
COLUNA_DATA = "Data"
COLUNA_STATUS = "Status"
COLUNA_ENTREGA = "Retirada"
COLUNA_FRETE = "Valor do frete"
COLUNA_ENTREGADOR = "Entregador"
# O painel exporta esta coluna como "Subtotal (Total - Valor do frete)". O
# nome completo muda entre versoes, entao a busca e por prefixo.
COLUNA_SUBTOTAL = "Subtotal"
COLUNA_DESCONTO = "Descontos"

# Valor da coluna "Retirada" que marca o pedido como entrega. O resto e balcao
# ou retirada no estabelecimento, que nao gera acerto com entregador.
ENTREGA = "delivery"

# So o que virou dinheiro. Negado, cancelado e expirado ficam de fora para nao
# inflar o historico que o modelo vai aprender.
STATUS_VALIDOS = {"finalizado"}

# O ERP tem um conjunto proprio de canais; o relatorio usa outro vocabulario.
#
# O link proprio fica separado de proposito. No relatorio real ele e o segundo
# maior canal da loja e nao paga comissao nenhuma — jogar isso dentro de um
# "Delivery" generico esconde justamente o canal onde a promocao da semana
# deveria ser publicada, e faz o marketplace parecer maior do que e.
CANAIS = {
    "pdv": "Balcao",
    "ifood": "iFood",
    "whatsapp": "WhatsApp",
    "site": "Site",
    "compartilhamento do menu": "Link proprio",
    "compartilhamento do produto": "Link proprio",
}

# Canal que o relatorio revela e o cadastro ainda nao tem. Entra com taxa zero
# porque canal proprio nao cobra comissao; o marketplace o gestor cadastra com
# a taxa real, e ai a conta de margem passa a valer.
TAXA_PADRAO_DO_CANAL = {"Balcao": 0.0, "Site": 0.0, "Link proprio": 0.0, "WhatsApp": 0.0, "iFood": 27.0}


def _data(valor: Any) -> Any:
    if isinstance(valor, datetime):
        return valor.date()
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(valor).strip(), formato).date()
        except (ValueError, TypeError):
            continue
    return None


def _coluna(cabecalho: list[str], prefixo: str) -> str | None:
    """Acha a coluna pelo comeco do nome.

    O painel exporta "Subtotal (Total - Valor do frete)" hoje e pode exportar
    so "Subtotal" amanha. Casar pelo prefixo evita que a importacao quebre — ou
    pior, ignore a coluna em silencio — quando o rotulo mudar.
    """

    for nome in cabecalho:
        if nome.strip().lower().startswith(prefixo.strip().lower()):
            return nome
    return None


def _numero(valor: Any, padrao: float = 0.0) -> float:
    try:
        return round(float(valor or 0), 2)
    except (TypeError, ValueError):
        return padrao


def ler_relatorio(caminho: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Le a planilha e devolve os pedidos aproveitaveis e o porque das exclusoes."""

    workbook = load_workbook(caminho, read_only=True, data_only=True)
    aba = workbook[workbook.sheetnames[0]]
    linhas = list(aba.iter_rows(values_only=True))
    workbook.close()

    if not linhas:
        return [], {"planilha_vazia": 1}

    cabecalho = [str(c).strip() if c is not None else "" for c in linhas[0]]
    faltando = [c for c in (COLUNA_NUMERO, COLUNA_TOTAL, COLUNA_DATA, COLUNA_STATUS) if c not in cabecalho]
    if faltando:
        raise ValueError(f"O relatório não tem as colunas esperadas: {', '.join(faltando)}.")

    coluna_subtotal = _coluna(cabecalho, COLUNA_SUBTOTAL)
    coluna_desconto = _coluna(cabecalho, COLUNA_DESCONTO)

    pedidos: list[dict[str, Any]] = []
    descartes: dict[str, int] = {}
    for linha in linhas[1:]:
        if not linha or linha[0] is None:
            continue
        registro = dict(zip(cabecalho, linha))

        status = str(registro.get(COLUNA_STATUS) or "").strip().lower()
        if status not in STATUS_VALIDOS:
            descartes[f"status:{status or 'vazio'}"] = descartes.get(f"status:{status or 'vazio'}", 0) + 1
            continue

        dia = _data(registro.get(COLUNA_DATA))
        if not dia:
            descartes["data_invalida"] = descartes.get("data_invalida", 0) + 1
            continue

        try:
            total = round(float(registro.get(COLUNA_TOTAL) or 0), 2)
        except (TypeError, ValueError):
            descartes["valor_invalido"] = descartes.get("valor_invalido", 0) + 1
            continue
        if total <= 0:
            descartes["valor_zerado_ou_negativo"] = descartes.get("valor_zerado_ou_negativo", 0) + 1
            continue

        numero = str(registro.get(COLUNA_NUMERO)).strip()
        origem = str(registro.get(COLUNA_ORIGEM) or "").strip().lower()
        retirada = str(registro.get(COLUNA_ENTREGA) or "").strip()
        e_entrega = retirada.strip().lower() == ENTREGA
        try:
            frete = round(float(registro.get(COLUNA_FRETE) or 0), 2)
        except (TypeError, ValueError):
            frete = 0.0
        entregador = str(registro.get(COLUNA_ENTREGADOR) or "").strip()

        # O frete declarado pode ser maior que o total recebido. Acontece no
        # marketplace: o subsidio come o preco do produto e o subtotal do
        # relatorio chega negativo. Manter o frete cheio faria a receita de
        # produto ficar negativa e envenenaria toda margem que a leia, entao o
        # frete e limitado ao que de fato entrou — e a diferenca fica gravada,
        # nao apagada.
        frete_relatado = frete
        if frete > total:
            frete = total
        subtotal_relatado = _numero(registro.get(coluna_subtotal), 0.0) if coluna_subtotal else None
        desconto = _numero(registro.get(coluna_desconto), 0.0) if coluna_desconto else 0.0
        # O relatorio nao informa a hora; meio-dia evita que o pedido escorregue
        # de dia ao converter para UTC e estrague a leitura por dia da semana.
        momento = datetime.combine(dia, datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=12)

        pedidos.append({
            "id": f"{ORIGEM}-{numero}-{dia.isoformat()}",
            "number": numero,
            "created_at": momento.isoformat(),
            "channel": CANAIS.get(origem, "Delivery"),
            "source": ORIGEM,
            "source_channel": str(registro.get(COLUNA_ORIGEM) or "").strip(),
            "delivery": retirada,
            # A maioria dos pedidos e balcao. O sinalizador vem da coluna
            # Retirada, e nao do frete, porque entrega com frete gratis existe.
            "is_delivery": e_entrega,
            "delivery_fee": frete if e_entrega else 0.0,
            # O que o relatorio dizia, para a conferencia continuar possivel
            # depois do ajuste acima.
            "reported_delivery_fee": frete_relatado if e_entrega else 0.0,
            "reported_subtotal": subtotal_relatado,
            "revenue_conflict": bool(e_entrega and frete_relatado > total),
            "courier_id": _id_entregador(entregador) if (e_entrega and entregador) else None,
            "courier_name": entregador or None,
            "payment_method": str(registro.get(COLUNA_PAGAMENTO) or "").strip() or "Não informado",
            "customer_name": None,
            # O relatorio informa o desconto concedido. Ele e o unico registro
            # de promocao ja aplicada que a loja tem, e sem ele nao da para
            # medir depois se promocao funcionou.
            "discount": desconto,
            "total": total,
            # Desconhecido, nao zero: o relatorio nao traz os itens vendidos.
            "cost": None,
            "status": "Concluido",
            "items": [],
        })

    return pedidos, descartes


def _slug(valor: str, padrao: str) -> str:
    normalizado = unicodedata.normalize("NFKD", valor).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", normalizado.lower()).strip("-") or padrao


def _id_entregador(nome: str) -> str:
    return f"courier-{_slug(nome, 'entregador')}"


def cadastrar_canais(pedidos: list[dict[str, Any]], store: Store) -> int:
    """Cria no cadastro o canal que o relatorio revelou.

    Sem isso o canal existe nos pedidos e nao existe na tela de Canais, e a
    taxa dele fica implicitamente zero sem ninguem ter decidido isso. Canal
    proprio entra com zero de verdade; marketplace entra com a taxa de tabela,
    que o gestor corrige com o contrato dele na mao.
    """

    existentes = {str(canal.get("name", "")).strip().lower() for canal in store.list("channels")}
    criados = 0
    for nome in sorted({pedido["channel"] for pedido in pedidos}):
        if nome.strip().lower() in existentes:
            continue
        taxa = TAXA_PADRAO_DO_CANAL.get(nome, 0.0)
        identificador = _slug(nome, "canal")
        store.put("channels", identificador, {
            "id": identificador, "name": nome, "fee_percent": taxa, "fee_fixed": 0.0, "active": True,
            "note": "Criado pela importacao da Anota Aí. Confira a taxa com o contrato do canal.",
        })
        criados += 1
    return criados


def cadastrar_entregadores(pedidos: list[dict[str, Any]], store: Store) -> int:
    """Cria o entregador que aparece no relatorio, sem remuneracao definida.

    O relatorio diz quem entregou, mas nunca quanto a loja paga. Criar com zero
    e deixar o gestor preencher e melhor do que inventar um valor: zero aparece
    na tela como "sem remuneracao definida", e um chute passaria por combinado.
    """

    criados = 0
    for nome in sorted({p["courier_name"] for p in pedidos if p.get("courier_name")}):
        identificador = _id_entregador(nome)
        if store.get("couriers", identificador):
            continue
        store.put("couriers", identificador, {
            "id": identificador, "name": nome, "phone": None, "vehicle": None, "active": True,
            "fee_per_delivery": 0, "commission_percent": 0, "daily_rate": 0, "monthly_salary": 0,
            "note": "Importado da Anota Aí. Defina a remuneração combinada.",
        })
        criados += 1
    return criados


def importar(pedidos: list[dict[str, Any]], store: Store, *, sobrescrever: bool) -> dict[str, int]:
    canais = cadastrar_canais(pedidos, store)
    entregadores = cadastrar_entregadores(pedidos, store)
    criados = ignorados = 0
    for pedido in pedidos:
        if not sobrescrever and store.get("orders", pedido["id"]):
            ignorados += 1
            continue
        gravavel = {k: v for k, v in pedido.items() if k != "courier_name"}
        store.put("orders", gravavel["id"], gravavel)
        criados += 1
    return {
        "criados": criados,
        "ignorados": ignorados,
        "entregadores_criados": entregadores,
        "canais_criados": canais,
    }


# ----------------------------------------------------------------------
# Segundo relatorio: os itens vendidos
# ----------------------------------------------------------------------
#
# O relatorio de pedidos traz o faturamento e nao traz o que foi vendido. E
# essa falta que deixa o custo desconhecido, a margem por produto impossivel e
# qualquer sugestao de promocao por produto sem base. Se o painel exportar um
# segundo relatorio com os itens, ele entra aqui e enriquece os pedidos que ja
# foram importados — sem refazer a importacao e sem tocar no estoque.
#
# O estoque fica de fora de proposito: a baixa desses insumos ja aconteceu na
# prateleira meses atras. Descontar agora nao corrigiria o passado, criaria um
# rombo no saldo de hoje.

# Nomes que o painel pode dar a cada coluna. A busca e por prefixo e o primeiro
# que casar vence, entao o mais especifico vem antes.
COLUNAS_ITEM = {
    "pedido": ("Número do Pedido", "Numero do Pedido", "Pedido", "Número", "Numero", "Cód. Pedido"),
    "produto": ("Produto", "Item", "Nome do Produto", "Descrição", "Descricao"),
    "quantidade": ("Quantidade", "Qtd", "Qtde"),
    "valor": ("Valor total", "Valor", "Total", "Preço", "Preco", "Subtotal"),
}


def ler_relatorio_itens(caminho: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Le o relatorio de itens vendidos, seja qual for o rotulo das colunas."""

    workbook = load_workbook(caminho, read_only=True, data_only=True)
    aba = workbook[workbook.sheetnames[0]]
    linhas = list(aba.iter_rows(values_only=True))
    workbook.close()
    if not linhas:
        return [], {"planilha_vazia": 1}

    cabecalho = [str(c).strip() if c is not None else "" for c in linhas[0]]
    achadas = {
        campo: next((coluna for nome in nomes if (coluna := _coluna(cabecalho, nome))), None)
        for campo, nomes in COLUNAS_ITEM.items()
    }
    if not achadas["produto"] or not achadas["quantidade"]:
        raise ValueError(
            "Este relatório não tem coluna de produto e quantidade. "
            f"As colunas encontradas foram: {', '.join(c for c in cabecalho if c)}."
        )
    if not achadas["pedido"]:
        raise ValueError(
            "Este relatório traz os produtos mas não diz a que pedido cada um pertence, "
            "então não dá para calcular o custo pedido a pedido. Procure no painel a exportação "
            "de itens que tenha a coluna do número do pedido."
        )

    itens: list[dict[str, Any]] = []
    descartes: dict[str, int] = {}
    for linha in linhas[1:]:
        if not linha or all(valor is None for valor in linha):
            continue
        registro = dict(zip(cabecalho, linha))
        numero = str(registro.get(achadas["pedido"]) or "").strip()
        nome = str(registro.get(achadas["produto"]) or "").strip()
        quantidade = _numero(registro.get(achadas["quantidade"]), 0.0)
        if not numero or not nome or quantidade <= 0:
            descartes["linha_incompleta"] = descartes.get("linha_incompleta", 0) + 1
            continue
        itens.append({
            "number": numero,
            "name": nome,
            "quantity": quantidade,
            "line_total": _numero(registro.get(achadas["valor"]), 0.0) if achadas["valor"] else 0.0,
        })
    return itens, descartes


def aplicar_itens(itens: list[dict[str, Any]], store: Store) -> dict[str, Any]:
    """Liga cada item ao produto do ERP e calcula o custo do pedido pela ficha.

    O casamento e por nome normalizado. Nome que nao casar nao vira palpite: ele
    volta na lista de pendencias, e o pedido continua marcado como sem custo —
    porque meio custo conhecido, apresentado como custo inteiro, e pior do que
    custo nenhum.
    """

    produtos = {_slug(p["name"], ""): p for p in store.list("products")}
    insumos = {item["id"]: item for item in store.list("inventory")}
    pedidos_por_numero: dict[str, list[dict[str, Any]]] = {}
    for pedido in store.list("orders"):
        if pedido.get("source") == ORIGEM:
            pedidos_por_numero.setdefault(str(pedido.get("number")), []).append(pedido)

    linhas_por_numero: dict[str, list[dict[str, Any]]] = {}
    for item in itens:
        linhas_por_numero.setdefault(item["number"], []).append(item)

    atualizados = 0
    sem_produto: dict[str, float] = {}
    sem_pedido: set[str] = set()
    ambiguos: list[str] = []
    incompletos: list[str] = []
    for numero, linhas_do_pedido in linhas_por_numero.items():
        alvos = pedidos_por_numero.get(numero)
        if not alvos:
            sem_pedido.add(numero)
            continue
        if len(alvos) > 1:
            # O mesmo numero em dois dias diferentes: sem a data no relatorio de
            # itens nao da para saber a qual pedido o item pertence.
            ambiguos.append(numero)
            continue
        casados = [(linha, produtos.get(_slug(linha["name"], ""))) for linha in linhas_do_pedido]
        faltando = [linha["name"] for linha, produto in casados if not produto]
        if faltando:
            # Um pedido so ganha itens se todas as linhas dele casaram. Com uma
            # linha de fora o custo sairia menor do que foi, e margem inflada e
            # o erro que parece boa noticia.
            for nome in faltando:
                sem_produto[nome] = sem_produto.get(nome, 0) + 1
            incompletos.append(numero)
            continue
        pedido = alvos[0]
        linhas_pedido = []
        custo_total = 0.0
        for item, produto in casados:
            custo_unitario = sum(
                insumos.get(linha["inventory_id"], {}).get("unit_cost", 0) * linha["quantity"]
                for linha in produto.get("recipe", [])
            )
            custo_total += custo_unitario * item["quantity"]
            linhas_pedido.append({
                "product_id": produto["id"],
                "name": produto["name"],
                "quantity": item["quantity"],
                "unit_price": produto["price"],
                "unit_cost": round(custo_unitario, 4),
                "line_total": round(item["line_total"] or produto["price"] * item["quantity"], 2),
                "addons": [],
            })
        pedido["items"] = linhas_pedido
        pedido["cost"] = round(custo_total, 2)
        pedido["items_source"] = ORIGEM
        store.put("orders", pedido["id"], pedido)
        atualizados += 1

    return {
        "itens_lidos": len(itens),
        "pedidos_detalhados": atualizados,
        "pedidos_nao_encontrados": sorted(sem_pedido)[:20],
        "pedidos_ambiguos": sorted(ambiguos)[:20],
        "pedidos_incompletos": len(incompletos),
        # A lista que o gestor precisa ver: cada nome aqui e um produto da
        # Anota Ai que nao existe no cardapio do ERP com esse nome.
        "produtos_sem_cadastro": sorted(sem_produto.items(), key=lambda par: -par[1])[:30],
    }



def main() -> int:
    parser = argparse.ArgumentParser(description="Importa o relatório de pedidos da Anota Aí.")
    parser.add_argument("relatorio", type=Path)
    parser.add_argument("--apply", action="store_true", help="Grava no banco. Sem isso, apenas confere.")
    parser.add_argument("--overwrite", action="store_true", help="Regrava pedidos já importados.")
    args = parser.parse_args()

    if not args.relatorio.is_file():
        parser.error(f"Arquivo não encontrado: {args.relatorio}")

    pedidos, descartes = ler_relatorio(args.relatorio)
    dias = sorted({p["created_at"][:10] for p in pedidos})
    entregas = [p for p in pedidos if p["is_delivery"]]
    resumo = {
        "aproveitados": len(pedidos),
        "faturamento": round(sum(p["total"] for p in pedidos), 2),
        "entregas": len(entregas),
        "balcao": len(pedidos) - len(entregas),
        "frete": round(sum(p["delivery_fee"] for p in pedidos), 2),
        # O painel grava o nome digitado, entao a mesma pessoa aparece com
        # grafias diferentes. O identificador normaliza acento, caixa e espaco,
        # e e ele que junta os turnos da pessoa certa no acerto.
        "entregadores": sorted({_id_entregador(p["courier_name"]) for p in pedidos if p.get("courier_name")}),
        "nomes_no_relatorio": sorted({p["courier_name"] for p in pedidos if p.get("courier_name")}),
        "primeiro_dia": dias[0] if dias else None,
        "ultimo_dia": dias[-1] if dias else None,
        "dias_com_movimento": len(dias),
        "descartados": descartes,
    }

    if not args.apply:
        print(json.dumps({"conferencia": True, **resumo, "dica": "rode de novo com --apply para gravar"}, ensure_ascii=False, indent=2))
        return 0

    resultado = importar(pedidos, build_store(get_settings()), sobrescrever=args.overwrite)
    print(json.dumps({"conferencia": False, **resumo, **resultado}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
