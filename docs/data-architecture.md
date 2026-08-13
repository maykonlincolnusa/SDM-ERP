# Arquitetura de Dados

## Camadas

| Camada | Objetivo | Regra de governanca |
| --- | --- | --- |
| `data/raw` | Fontes originais por dominio | Nunca transformar, sobrescrever ou usar como fato diretamente |
| `data/bronze` | Copia por SHA-256, metadados e linhas de planilha | Reproduzivel e imutavel por conteudo |
| `data/silver` | Registros normalizados e diagnosticos por fonte | Preservar origem, valor bruto relevante e flags de qualidade |
| `data/gold` | Catalogos e indicadores para decisao | Derivar somente da camada silver |
| `data/runtime` | Estado demo, biblioteca de midia e manifestos | Nao e fonte analitica |

## Contratos por Dominio

### Precificacao

- Entrada: `raw/pricing/current` e `raw/pricing/reference`.
- Silver: ingredientes e produtos por hash de arquivo.
- Gold: somente `pricing/current` alimenta `pricing_catalog.jsonl` e `pricing_summary.json`.
- Regra: `VALOR SUGERIDO c/ TAXAS` tem prioridade. Quando ele nao existe, o produto recebe `suggested_price_includes_fees=false` e um alerta de revisao.
- Regra: unidades e custos da receita sao preservados; conversoes de caixa, peca, kg, ml e L precisam de validacao operacional antes de consolidar estoque fisico.

### Financeiro

- Entrada: `raw/finance`.
- Silver: `revenue_payment` e `expense`, apenas com data valida e valor positivo.
- Gold: `daily_revenue.jsonl`, `expenses.jsonl` e `finance_summary.json`.
- Regra: linhas de total, formulas de painel, controle pessoal e placeholders de funcionarios nao entram no caixa empresarial.
- Regra: uma aba mensal pode registrar apenas o dia do mes; o ETL compoe a data a partir do mes e ano no titulo da aba.

### Pesquisa de Mercado

- Entrada: `raw/market_research`.
- Silver: observacoes com localidade, produto, base, frutas, padrao social e colocacao.
- Gold: `market_summary.json` agrupa variacoes de caixa e acento pelo valor normalizado, preservando as formas originais.
- Regra: possiveis erros de nome geram `alias_review` e sugestao de revisao. O ETL nao os corrige silenciosamente.

### Documentos e Midia

- Entrada: `raw/receipts`, `raw/legal` e demais arquivos suportados.
- Silver: catalogo de fontes com hash, tipo MIME, dimensoes ou paginas, dominio e nivel de acesso.
- Regra: o contrato e restrito; comprovantes e fotos ficam em revisao ate que uma pessoa confirme fornecedor, data, quantidade, pagamento e unidade.
- Regra: um comprovante revisado deve se vincular a fornecedor, compra, itens de estoque, movimento e conta a pagar sem criar a mesma despesa duas vezes.

## Idempotencia e Rastreabilidade

1. O hash SHA-256 identifica o conteudo da fonte, mesmo se ela aparecer em mais de um caminho.
2. O manifesto em `runtime/etl/manifest.json` guarda caminhos, camada de destino, versao da transformacao e status.
3. Uma nova versao de transformacao reprocessa somente planilhas afetadas; arquivos de origem permanecem intactos.
4. Saidas JSON e JSONL sao deterministicas e escritas atomicamente apenas quando o conteudo muda.
5. Uma falha de fonte e isolada: ela aparece no relatorio da execucao sem bloquear as demais fontes saudaveis.

## Operacao

1. Salve o arquivo original no dominio correto de `data/raw`.
2. Execute `scripts/run_etl.py`.
3. Revise `gold/data_quality.json` e os indicadores resultantes.
4. Corrija a fonte ou registre a decisao de negocio para cada alerta relevante.
5. Publique somente dados aprovados no ERP/Firebase.
