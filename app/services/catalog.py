"""Listas editaveis que alimentam os campos de escolha do sistema.

Forma de pagamento, categoria de despesa, linha da marca, categoria de arquivo:
todas eram listas fixas dentro do HTML. Isso funciona ate o dia em que a loja
passa a aceitar vale-refeicao, ou quer separar "manutencao" de "outros" — e ai
a unica saida e pedir para o desenvolvedor mexer no codigo.

Cada lista aqui declara a **coleção e o campo que ela alimenta**. E isso que
permite as duas operacoes que uma lista solta nao permitiria:

- **Renomear migra os registros.** Trocar "Fixo" por "Custo fixo" sem atualizar
  as despesas deixaria toda a base apontando para um valor que a lista nao
  oferece mais — inconsistencia silenciosa, do tipo que so aparece meses depois
  num relatorio que nao fecha.
- **Excluir e recusado enquanto houver uso.** O registro antigo continuaria
  valido, mas orfao. A recusa vem com a contagem e a sugestao de renomear, que
  e quase sempre o que a pessoa realmente queria fazer.
"""

from __future__ import annotations

from typing import Any

from app.repositories.store import Store
from app.services.erp import ERPService

# Cada catalogo diz o que alimenta. Sem esse vinculo, renomear seria uma
# operacao cega e excluir seria destrutivo sem aviso.
CATALOGS: dict[str, dict[str, Any]] = {
    "payment_methods": {
        "name": "Formas de pagamento",
        "description": "Aparecem na tela de venda.",
        "collection": "orders",
        "field": "payment_method",
        "defaults": ["Pix", "Credito", "Debito", "Dinheiro", "Voucher", "Outro"],
    },
    "expense_categories": {
        "name": "Categorias de despesa",
        "description": "Organizam o financeiro. As fixas entram no rateio do dossiê de custo.",
        "collection": "expenses",
        "field": "category",
        "defaults": ["Insumos", "Fixo", "Equipe", "Marketing", "Taxas", "Outros"],
    },
    "product_lines": {
        "name": "Linhas da marca",
        "description": "Separam o resultado de cada linha em Marca e linhas.",
        "collection": "products",
        "field": "line",
        "defaults": ["Classico", "Sport", "Kids", "Sazonal"],
    },
    "inventory_categories": {
        "name": "Categorias de insumo",
        "description": "Agrupam o estoque. Categoria com 'embalagem' no nome é tratada como embalagem no dossiê.",
        "collection": "inventory",
        "field": "category",
        "defaults": ["Polpas", "Frutas", "Complementos", "Embalagens", "Limpeza", "Insumos"],
    },
    "media_categories": {
        "name": "Categorias de arquivo",
        "description": "Classificam documentos e imagens.",
        "collection": "media",
        "field": "category",
        "defaults": [
            "Financeiro", "Precificação", "Compras", "Contratos",
            "Planilhas", "Documentos", "Marketing", "Operação",
        ],
    },
    "courier_vehicles": {
        "name": "Veículos de entrega",
        "description": "Sugestões no cadastro do entregador.",
        "collection": "couriers",
        "field": "vehicle",
        "defaults": ["Moto", "Bicicleta", "Carro", "A pé"],
    },
}


class CatalogService:
    def __init__(self, store: Store):
        self.store = store
        self.erp = ERPService(store)

    # ------------------------------------------------------------------

    def all(self) -> list[dict[str, Any]]:
        return [self.get(identifier) for identifier in CATALOGS]

    def get(self, catalog_id: str) -> dict[str, Any]:
        definicao = self._definicao(catalog_id)
        documento = self.store.get("catalogs", catalog_id)
        if not documento:
            documento = {"id": catalog_id, "items": list(definicao["defaults"])}
            self.store.put("catalogs", catalog_id, documento)
        uso = self._uso(definicao)
        return {
            "id": catalog_id,
            "name": definicao["name"],
            "description": definicao["description"],
            "feeds": f"{definicao['collection']}.{definicao['field']}",
            "items": [
                {"value": valor, "usage": uso.get(valor, 0)}
                for valor in documento.get("items", [])
            ],
            # Valor que existe nos registros mas saiu da lista. Nao some da tela:
            # ele explica por que um relatorio antigo mostra uma categoria que
            # ninguem consegue mais escolher.
            "orphans": [
                {"value": valor, "usage": total}
                for valor, total in sorted(uso.items())
                if valor not in documento.get("items", [])
            ],
        }

    def add(self, catalog_id: str, value: str) -> dict[str, Any]:
        definicao = self._definicao(catalog_id)
        valor = self._limpar(value)
        documento = self.store.get("catalogs", catalog_id) or {"id": catalog_id, "items": list(definicao["defaults"])}
        if valor in documento["items"]:
            raise ValueError(f"'{valor}' já está na lista.")
        documento["items"].append(valor)
        self.store.put("catalogs", catalog_id, documento)
        self.erp._audit(self.store, "catalog", catalog_id, "criado", f"{definicao['name']}: '{valor}' adicionado")
        return self.get(catalog_id)

    def rename(self, catalog_id: str, value: str, new_value: str) -> dict[str, Any]:
        """Renomeia e leva junto os registros que usam o valor antigo."""

        definicao = self._definicao(catalog_id)
        novo = self._limpar(new_value)
        documento = self.store.get("catalogs", catalog_id) or {"id": catalog_id, "items": list(definicao["defaults"])}
        if value not in documento["items"]:
            raise ValueError(f"'{value}' não está na lista.")
        if novo != value and novo in documento["items"]:
            raise ValueError(f"'{novo}' já está na lista.")

        documento["items"] = [novo if item == value else item for item in documento["items"]]
        self.store.put("catalogs", catalog_id, documento)

        migrados = 0
        for registro in self.store.list(definicao["collection"]):
            if registro.get(definicao["field"]) != value:
                continue
            registro[definicao["field"]] = novo
            self.store.put(definicao["collection"], registro["id"], registro)
            migrados += 1

        self.erp._audit(
            self.store, "catalog", catalog_id, "editado",
            f"{definicao['name']}: '{value}' virou '{novo}'"
            + (f", com {migrados} registro(s) atualizado(s)" if migrados else ""),
            {"valor": {"de": value, "para": novo}, "registros_migrados": migrados},
        )
        return {**self.get(catalog_id), "migrated": migrados}

    def remove(self, catalog_id: str, value: str) -> dict[str, Any]:
        """Recusa enquanto houver registro usando o valor.

        Excluir mesmo assim deixaria o registro antigo valido e orfao. A recusa
        traz a contagem e aponta o caminho que quase sempre era o desejado:
        renomear.
        """

        definicao = self._definicao(catalog_id)
        documento = self.store.get("catalogs", catalog_id) or {"id": catalog_id, "items": list(definicao["defaults"])}
        if value not in documento["items"]:
            raise ValueError(f"'{value}' não está na lista.")
        uso = self._uso(definicao).get(value, 0)
        if uso:
            raise ValueError(
                f"'{value}' está em uso por {uso} registro(s) e não pode ser excluído. "
                f"Renomeie o item — os registros acompanham — ou troque esses registros de categoria antes."
            )
        if len(documento["items"]) <= 1:
            raise ValueError("A lista precisa manter pelo menos uma opção.")
        documento["items"] = [item for item in documento["items"] if item != value]
        self.store.put("catalogs", catalog_id, documento)
        self.erp._audit(self.store, "catalog", catalog_id, "excluido", f"{definicao['name']}: '{value}' removido")
        return self.get(catalog_id)

    def reorder(self, catalog_id: str, items: list[str]) -> dict[str, Any]:
        """Reordena sem inventar nem perder item: a lista tem de ser a mesma."""

        definicao = self._definicao(catalog_id)
        documento = self.store.get("catalogs", catalog_id) or {"id": catalog_id, "items": list(definicao["defaults"])}
        if sorted(items) != sorted(documento["items"]):
            raise ValueError("A reordenação precisa conter exatamente os mesmos itens da lista.")
        documento["items"] = items
        self.store.put("catalogs", catalog_id, documento)
        return self.get(catalog_id)

    # ------------------------------------------------------------------

    @staticmethod
    def _definicao(catalog_id: str) -> dict[str, Any]:
        definicao = CATALOGS.get(catalog_id)
        if not definicao:
            raise ValueError("Lista não encontrada.")
        return definicao

    @staticmethod
    def _limpar(value: str) -> str:
        valor = " ".join(str(value or "").split())
        if not valor:
            raise ValueError("Informe um nome para o item.")
        if len(valor) > 40:
            raise ValueError("O nome do item pode ter no máximo 40 caracteres.")
        return valor

    def _uso(self, definicao: dict[str, Any]) -> dict[str, int]:
        contagem: dict[str, int] = {}
        for registro in self.store.list(definicao["collection"]):
            valor = registro.get(definicao["field"])
            if valor:
                contagem[valor] = contagem.get(valor, 0) + 1
        return contagem
