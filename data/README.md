# Camadas de Dados

`raw/` guarda fontes originais por dominio. `bronze/` guarda copias por checksum, metadados e linhas extraidas. `silver/` contem entidades normalizadas e diagnosticos. `gold/` contem agregacoes usadas pela gestao. `runtime/` e exclusivo para estado local, manifestos e biblioteca de midia.

As pastas de dados gerados e fontes confidenciais nao entram no repositorio nem na imagem Docker. Consulte `docs/data-architecture.md` para os contratos de cada camada.
