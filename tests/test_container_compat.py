"""A diferença entre o Python daqui e o do container.

O ambiente local roda 3.14, onde anotações são preguiçosas (PEP 649). A imagem
roda **3.12**, que avalia a anotação na hora da definição. Um `def list` numa
classe faz as anotações `list[...]` seguintes resolverem para o método em vez
do builtin — e isso passa verde aqui e derruba o container. Foi exatamente
assim que o primeiro deploy caiu, em `app/services/media.py`.

Este teste força a avaliação de toda anotação de todo módulo, reproduzindo o
que o 3.12 faz sozinho. A varredura era um passo manual antes do deploy, e era
incompleta: percorria pacotes, e `app/repositories/` não tinha `__init__.py`,
então `store.py` — que tem o padrão `def list` em seis classes — nunca foi
verificado. Aqui a descoberta é por arquivo, não por pacote, para que nenhum
diretório novo escape em silêncio.
"""

import importlib
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
PACOTES = ("app", "etl", "scripts")


def modulos() -> list[str]:
    """Todo módulo importável dos pacotes do projeto, achado pelo arquivo."""

    achados: list[str] = []
    for pacote in PACOTES:
        for arquivo in sorted((RAIZ / pacote).rglob("*.py")):
            if "__pycache__" in arquivo.parts:
                continue
            partes = arquivo.relative_to(RAIZ).with_suffix("").parts
            if partes[-1] == "__init__":
                partes = partes[:-1]
            achados.append(".".join(partes))
    return achados


@pytest.fixture(scope="module", autouse=True)
def raiz_no_caminho():
    """Sem isto o import pega a cópia instalada no `.venv`, não o código daqui."""

    if str(RAIZ) not in sys.path:
        sys.path.insert(0, str(RAIZ))


def test_a_varredura_alcanca_os_arquivos_que_ja_escaparam():
    """Guarda do próprio guarda: a lista tem de conter o que ela já perdeu."""

    encontrados = set(modulos())

    assert "app.repositories.store" in encontrados, "o arquivo com `def list` em seis classes"
    assert "app.repositories.secrets" in encontrados
    assert "app.services.ifood" in encontrados
    assert len(encontrados) > 30


@pytest.mark.parametrize("nome", modulos())
def test_as_anotacoes_avaliam_como_no_container(nome):
    modulo = importlib.import_module(nome)

    if sys.version_info < (3, 14):
        # Aqui a avaliação já aconteceu na importação: chegar até esta linha
        # é a prova, e é exatamente o que o container faz.
        return

    import annotationlib
    import inspect

    alvos = [modulo]
    for _, obj in inspect.getmembers(modulo):
        if getattr(obj, "__module__", None) != nome:
            continue
        if inspect.isclass(obj):
            alvos.append(obj)
            alvos += [membro for _, membro in inspect.getmembers(obj, callable)]
        elif inspect.isfunction(obj):
            alvos.append(obj)

    for alvo in alvos:
        try:
            annotationlib.get_annotations(alvo, format=annotationlib.Format.VALUE)
        except Exception as error:  # noqa: BLE001 - qualquer falha aqui quebra o container
            rotulo = getattr(alvo, "__qualname__", repr(alvo))
            pytest.fail(f"{nome}.{rotulo}: {type(error).__name__}: {error}")
