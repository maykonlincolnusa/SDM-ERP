"""Cache de leitura com o tempo de vida de uma requisicao.

O problema medido em producao: `/api/promotions/month` levava 16 segundos de
mediana e 28 no pior caso. A causa nao era uma consulta lenta — era a mesma
consulta repetida. Uma requisicao dessa rota varria `orders` **seis vezes** e
fazia 19 varreduras de colecao no total, porque cada servico que ela encadeia
(promocao, previsao, preco, custo, ERP) le o historico inteiro por conta
propria. Contra mil pedidos no Firestore, cada varredura e uma viagem de rede
que traz tudo.

A correcao nao mexe em nenhum servico. Ela poe um dicionario na frente do
armazenamento e o esvazia quando a requisicao acaba:

- **Nada de TTL, nada compartilhado entre requisicoes.** Cache com prazo entre
  requisicoes traria leitura velha depois de uma gravacao, e num ERP isso e
  pior do que ser lento. O ganho vem de dentro da mesma requisicao, onde a
  releitura nunca deveria ter acontecido.
- **Gravar limpa o cache.** Um servico que grava e depois le precisa ver o que
  gravou. A limpeza e da colecao inteira e nao do documento, porque `list`
  guarda a colecao toda.
- **A transacao passa por fora.** Ela tem consistencia propria do Firestore, e
  servi-la de um dicionario quebraria a garantia que ela existe para dar.

Cada requisicao passa a enxergar um retrato estavel da colecao, o que tambem
corrige um efeito que ninguem tinha notado: hoje um relatorio longo pode ler
`orders` no inicio e de novo no fim, e receber conteudos diferentes se uma
venda entrar no meio.
"""

from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from typing import Any

# Vazio significa "fora de requisicao": script, teste, tarefa de fundo. Nesses
# casos o cache nao liga sozinho, e a leitura continua indo ao banco.
_ESCOPO: ContextVar[dict[str, list[dict[str, Any]]] | None] = ContextVar("leitura_da_requisicao", default=None)


def abrir_escopo() -> dict[str, list[dict[str, Any]]]:
    """Comeca um retrato novo. Chamado uma vez por requisicao."""

    escopo: dict[str, list[dict[str, Any]]] = {}
    _ESCOPO.set(escopo)
    return escopo


def fechar_escopo() -> None:
    _ESCOPO.set(None)


def escopo_atual() -> dict[str, list[dict[str, Any]]] | None:
    return _ESCOPO.get()


def guardar(escopo: dict[str, list[dict[str, Any]]], collection: str, linhas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    escopo[collection] = linhas
    return copiar(linhas)


def copiar(linhas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Devolve sempre objetos novos, como a leitura sem cache devolvia.

    Servir o mesmo dicionario duas vezes mudaria o comportamento em silencio:
    o codigo do ERP le uma colecao, mexe no que leu e grava — e a segunda
    leitura passaria a enxergar a alteracao da primeira antes de ela existir no
    banco. A copia custa milissegundos contra a viagem de rede que ela evita, e
    compra a garantia de que ligar o cache nao muda resultado nenhum.
    """

    return deepcopy(linhas)


def invalidar(collection: str | None = None) -> None:
    """Descarta o que foi lido, porque acabou de ser gravado.

    Sem colecao, descarta tudo: e o caso da transacao, que pode ter mexido em
    varias de uma vez e nao diz em quais.
    """

    escopo = _ESCOPO.get()
    if escopo is None:
        return
    if collection is None:
        escopo.clear()
    else:
        escopo.pop(collection, None)
