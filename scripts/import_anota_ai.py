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

# Valor da coluna "Retirada" que marca o pedido como entrega. O resto e balcao
# ou retirada no estabelecimento, que nao gera acerto com entregador.
ENTREGA = "delivery"

# So o que virou dinheiro. Negado, cancelado e expirado ficam de fora para nao
# inflar o historico que o modelo vai aprender.
STATUS_VALIDOS = {"finalizado"}

# O ERP tem um conjunto proprio de canais; o relatorio usa outro vocabulario.
CANAIS = {
    "pdv": "Balcao",
    "ifood": "iFood",
    "whatsapp": "WhatsApp",
    "site": "Delivery",
    "compartilhamento do menu": "Delivery",
    "compartilhamento do produto": "Delivery",
}


def _data(valor: Any) -> Any:
    if isinstance(valor, datetime):
        return valor.date()
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(valor).strip(), formato).date()
        except (ValueError, TypeError):
            continue
    return None


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
            "courier_id": _id_entregador(entregador) if (e_entrega and entregador) else None,
            "courier_name": entregador or None,
            "payment_method": str(registro.get(COLUNA_PAGAMENTO) or "").strip() or "Não informado",
            "customer_name": None,
            "discount": 0,
            "total": total,
            # Desconhecido, nao zero: o relatorio nao traz os itens vendidos.
            "cost": None,
            "status": "Concluido",
            "items": [],
        })

    return pedidos, descartes


def _id_entregador(nome: str) -> str:
    normalizado = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalizado.lower()).strip("-")
    return f"courier-{slug or 'entregador'}"


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
    entregadores = cadastrar_entregadores(pedidos, store)
    criados = ignorados = 0
    for pedido in pedidos:
        if not sobrescrever and store.get("orders", pedido["id"]):
            ignorados += 1
            continue
        gravavel = {k: v for k, v in pedido.items() if k != "courier_name"}
        store.put("orders", gravavel["id"], gravavel)
        criados += 1
    return {"criados": criados, "ignorados": ignorados, "entregadores_criados": entregadores}


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
