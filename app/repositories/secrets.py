"""Onde credencial de terceiro fica guardada.

Chave de API, segredo de aplicativo e token de renovação não são dado de
negócio: eles não pertencem ao Firestore ao lado de pedidos e insumos. No
Firestore ficariam em texto claro, legíveis por qualquer coisa que alcance o
banco, e apareceriam em qualquer exportação ou backup dele.

A separação segue o mesmo desenho da camada de armazenamento: um protocolo
estreito com duas implementações, escolhidas no boot. Em produção é o Secret
Manager do Google, que versiona, controla acesso por IAM e registra quem leu.
Localmente é um arquivo fora do repositório — não porque seja seguro, mas
porque desenvolvimento não tem cofre e fingir que tem seria pior.

O valor guardado é um dicionário serializado em JSON: uma integração inteira
cabe num segredo só, e gravar tudo de uma vez evita o estado meio-atualizado
que dois segredos separados permitiriam.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from app.config import Settings

logger = logging.getLogger(__name__)

# Leitura do Secret Manager é cobrada por acesso, e a sincronização do iFood
# pode rodar de meio em meio minuto. Um cache curto por instância derruba isso
# a quase nada sem segurar credencial velha por tempo relevante — o token do
# iFood vale seis horas e é renovado dez minutos antes de vencer.
CACHE_TTL = timedelta(seconds=60)


class SecretVault(Protocol):
    mode: str

    def read(self, name: str) -> dict[str, Any] | None: ...

    def write(self, name: str, value: dict[str, Any]) -> None: ...

    def delete(self, name: str) -> None: ...


class FileVault:
    """Cofre de desenvolvimento: um arquivo JSON fora do banco e fora do Git.

    Não pretende ser seguro. Pretende ser *separado*: mantém o segredo fora do
    documento de negócio, para que o código que roda local exercite exatamente
    o mesmo caminho que o de produção.
    """

    mode = "file"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as fonte:
                return json.load(fonte)
        except (ValueError, OSError):
            logger.warning("cofre local ilegível em %s; tratando como vazio", self.path)
            return {}

    def _write_all(self, dados: dict[str, Any]) -> None:
        temporario = self.path.with_suffix(".tmp")
        with temporario.open("w", encoding="utf-8") as alvo:
            json.dump(dados, alvo, ensure_ascii=False, indent=2)
        temporario.replace(self.path)

    def read(self, name: str) -> dict[str, Any] | None:
        with self.lock:
            valor = self._read_all().get(name)
            return dict(valor) if isinstance(valor, dict) else None

    def write(self, name: str, value: dict[str, Any]) -> None:
        with self.lock:
            dados = self._read_all()
            dados[name] = value
            self._write_all(dados)

    def delete(self, name: str) -> None:
        with self.lock:
            dados = self._read_all()
            if dados.pop(name, None) is not None:
                self._write_all(dados)


class SecretManagerVault:
    """Secret Manager do Google Cloud.

    Cada gravação cria uma versão nova e **destrói as anteriores**. Isso não é
    detalhe de limpeza: versão ativa é cobrada por mês, e o token do iFood se
    renova a cada seis horas — deixar o histórico acumular transformaria
    centavos em conta recorrente crescente, num projeto com orçamento de dez
    reais. O histórico de quem leu e quem gravou fica no audit log, que é onde
    ele serve para alguma coisa.
    """

    mode = "secret-manager"

    def __init__(self, project_id: str, prefix: str = "erp"):
        try:
            from google.cloud import secretmanager
        except ImportError as error:  # pragma: no cover - dependência de deploy
            raise RuntimeError("google-cloud-secret-manager é necessário no modo Secret Manager") from error
        self.client = secretmanager.SecretManagerServiceClient()
        self.secretmanager = secretmanager
        self.project_id = project_id
        self.prefix = prefix
        self.lock = threading.RLock()
        self._cache: dict[str, tuple[datetime, dict[str, Any] | None]] = {}

    def _secret_id(self, name: str) -> str:
        return f"{self.prefix}-{name}"

    def _secret_path(self, name: str) -> str:
        return f"projects/{self.project_id}/secrets/{self._secret_id(name)}"

    def read(self, name: str) -> dict[str, Any] | None:
        with self.lock:
            guardado = self._cache.get(name)
            if guardado and guardado[0] > datetime.now(timezone.utc):
                return dict(guardado[1]) if guardado[1] else None

        from google.api_core import exceptions

        try:
            resposta = self.client.access_secret_version(
                request={"name": f"{self._secret_path(name)}/versions/latest"}
            )
            valor = json.loads(resposta.payload.data.decode("utf-8"))
            valor = valor if isinstance(valor, dict) else None
        except exceptions.NotFound:
            valor = None
        except (ValueError, UnicodeDecodeError):
            logger.warning("segredo %s não é JSON; tratando como ausente", self._secret_id(name))
            valor = None

        with self.lock:
            self._cache[name] = (datetime.now(timezone.utc) + CACHE_TTL, valor)
        return dict(valor) if valor else None

    def write(self, name: str, value: dict[str, Any]) -> None:
        from google.api_core import exceptions

        caminho = self._secret_path(name)
        conteudo = {"payload": {"data": json.dumps(value, ensure_ascii=False).encode("utf-8")}}
        try:
            nova = self.client.add_secret_version(request={"parent": caminho, **conteudo})
        except exceptions.NotFound:
            # Criar exige `secretmanager.secrets.create`, que só existe no papel
            # de administrador. O caminho recomendado é o segredo já existir e a
            # conta do serviço ter apenas acesso e gestão de versões — então
            # esta criação é conveniência de primeira execução, não o normal.
            self.client.create_secret(request={
                "parent": f"projects/{self.project_id}",
                "secret_id": self._secret_id(name),
                # Réplica automática: a loja é uma só e não há requisito de
                # residência que justifique escolher regiões à mão.
                "secret": {"replication": {"automatic": {}}},
            })
            nova = self.client.add_secret_version(request={"parent": caminho, **conteudo})
        self._destroy_previous(caminho, nova.name)
        with self.lock:
            self._cache[name] = (datetime.now(timezone.utc) + CACHE_TTL, dict(value))

    def _destroy_previous(self, caminho: str, atual: str) -> None:
        from google.api_core import exceptions

        for versao in self.client.list_secret_versions(request={"parent": caminho}):
            if versao.name == atual:
                continue
            if versao.state == self.secretmanager.SecretVersion.State.DESTROYED:
                continue
            try:
                self.client.destroy_secret_version(request={"name": versao.name})
            except (exceptions.FailedPrecondition, exceptions.NotFound):
                # Já destruída por outra instância; o efeito desejado existe.
                continue

    def delete(self, name: str) -> None:
        """Destrói as versões em vez de apagar o segredo.

        É a semântica certa — o que se quer é esquecer a credencial, não
        remover o recipiente — e mantém a conta do serviço fora do papel de
        administrador, que seria necessário para apagar o segredo em si.
        Segredo sem versão ativa não é cobrado.
        """

        from google.api_core import exceptions

        try:
            self._destroy_previous(self._secret_path(name), atual="")
        except exceptions.NotFound:
            pass
        with self.lock:
            self._cache.pop(name, None)


def build_secret_vault(settings: Settings, store_mode: str) -> SecretVault:
    """Escolhe o cofre pelo mesmo critério que escolheu o banco.

    Amarrar a decisão ao modo do armazenamento é o que garante a propriedade
    que interessa: sempre que os dados de negócio estiverem no Firestore, o
    segredo estará no Secret Manager — nunca ao lado deles.
    """

    if settings.secrets_backend == "file":
        return FileVault(settings.secrets_file_path)
    if settings.secrets_backend == "secret-manager" or store_mode == "firebase":
        if not settings.firebase_project_id:
            raise ValueError("O cofre de segredos em nuvem requer FIREBASE_PROJECT_ID.")
        # Sem recuo para o arquivo aqui, de propósito. No Cloud Run o disco é
        # efêmero: o recuo silencioso gravaria a credencial em texto claro num
        # arquivo que some no próximo início a frio — falha pior que a falha,
        # porque parece funcionar. Construir o cliente não chama a API, então
        # um projeto sem o Secret Manager habilitado falha na primeira
        # ativação, com mensagem, e não no boot do serviço inteiro.
        return SecretManagerVault(settings.firebase_project_id)
    return FileVault(settings.secrets_file_path)
