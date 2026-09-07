"""Camada de armazenamento: onde o dado de negócio e o segredo ficam guardados.

Existe como pacote de verdade, e não como diretório solto, porque a varredura
que roda antes do deploy percorre pacotes — sem o `__init__.py` ela pulava
`store.py` e `secrets.py` em silêncio, justamente os arquivos com o padrão que
o Python 3.12 do container avalia de forma diferente do 3.14 local.
"""
