# Acai Gestao

ERP web para a operacao de uma loja de acai. O projeto combina API FastAPI, painel web, Firebase opcional e uma camada de engenharia de dados para manter planilhas, comprovantes e documentos organizados e rastreaveis.

## Estrutura

```text
app/                 API, regras do ERP, repositorios e interface web
etl/                 extracao, transformacao e agregacao por dominio
scripts/             comandos operacionais de ETL e publicacao
tests/               cobertura de API, midia e pipeline de dados
data/raw/            fontes originais, separadas por dominio
data/bronze/         copias por checksum, metadados e linhas extraidas
data/silver/         registros normalizados por dominio
data/gold/           indicadores prontos para gestao
data/runtime/        estado demo, biblioteca de midia e manifestos de execucao
docs/                arquitetura e contratos de dados
```

`data/raw`, `data/bronze`, `data/silver`, `data/gold` e `data/runtime` sao locais e ficam fora do Git e da imagem Docker.

## Executar localmente

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Abra `http://127.0.0.1:8000`. Sem configuracao adicional, a aplicacao usa o modo demonstrativo e grava o estado em `data/runtime/demo_store.json`.

## PWA

Em navegadores compativeis, use o icone de instalacao no topo para adicionar o ERP ao celular ou computador como aplicativo. O service worker guarda apenas a interface e os icones para abertura resiliente; dados de vendas, estoque, financeiro e arquivos continuam sendo buscados ao vivo e nunca ficam em cache offline.

## Firebase e Google Cloud

1. No console Firebase do projeto `sdm-2959c`, habilite Cloud Firestore em modo de producao e confirme o bucket `sdm-2959c.firebasestorage.app`.
2. Instale as dependencias com `pip install .`.
3. Copie `.env.firebase.example` para `.env.firebase`. Esse arquivo local ativa o Firebase sem alterar ou expor o `.env` existente.
4. Em desenvolvimento local, configure Application Default Credentials com `gcloud auth application-default login`, ou defina `FIREBASE_SERVICE_ACCOUNT_PATH` para uma chave fora do controle de versao. Em Cloud Run, use a conta de servico do runtime com permissoes para Firestore e Firebase Storage.
5. Valide Firestore e Firebase Storage sem gravar dados: `./.venv/Scripts/python.exe scripts/verify_firestore.py`. Reinicie a API apos a credencial ser criada; o indicador da loja deve mudar de modo demonstrativo para Firebase conectado.
6. Confira a migracao sem escrever com `./.venv/Scripts/python.exe scripts/seed_firestore.py`; depois use `--apply`. O comando preserva documentos existentes, a menos que `--overwrite` seja informado. Arquivos devem ser enviados pela tela Arquivos para que os binarios tambem cheguem ao Storage.
7. No Firebase Authentication, habilite o provedor Email/Password, crie a conta do proprietario e execute `./.venv/Scripts/python.exe scripts/grant_erp_admin.py --email dono@exemplo.com`. A claim entra no proximo login.
8. Ative `AUTH_REQUIRED=true` antes de publicar. O aplicativo recusa iniciar em producao com Firebase sem autenticacao e valida a claim `erp_admin` em todas as rotas operacionais.
9. Publique as regras restritivas com `firebase deploy --only firestore:rules,storage`. O arquivo `.firebaserc` ja aponta esse comando para `sdm-2959c`. O acesso direto ao Firebase exige a claim customizada `erp_admin=true`; a API usa o Admin SDK no servidor.

O ERP usa as colecoes `products`, `inventory`, `orders`, `expenses`, `inventory_movements` e `media`. Os binarios da secao Arquivos ficam no Firebase Storage em producao e somente os metadados ficam no Firestore.

## Pipeline de Dados

Coloque novas fontes na zona correspondente dentro de `data/raw` e execute:

```powershell
.\.venv\Scripts\python.exe scripts\run_etl.py
```

O ETL calcula SHA-256, cria uma copia imutavel em bronze, normaliza apenas fatos elegiveis em silver e gera os agregados de gestao em gold. A repeticao com o mesmo arquivo e idempotente; o manifesto fica em `data/runtime/etl/manifest.json`.

Fontes atuais:

- `raw/pricing/current`: ficha tecnica e precificacao ativa. Somente esta pasta alimenta o catalogo de margem em gold.
- `raw/pricing/reference`: historico e modelo de consulta; e preservado, mas nao substitui a precificacao ativa.
- `raw/finance`: receitas e despesas transacionais. Resumos, formulas, funcionarios sem pagamento e controle pessoal ficam fora dos fatos empresariais.
- `raw/market_research`: observacoes competitivas, com texto bruto preservado e normalizacoes sinalizadas para revisao.
- `raw/receipts` e `raw/legal`: documentos e imagens catalogados com metadados, hash e nivel de acesso. Eles exigem revisao humana antes de criar compras, estoque ou despesas.

Saidas principais:

- `silver/pricing/<hash>.jsonl`, `silver/finance/<hash>.jsonl` e `silver/market/<hash>.jsonl`
- `silver/document_catalog.jsonl`
- `gold/pricing_catalog.jsonl` e `gold/pricing_summary.json`
- `gold/daily_revenue.jsonl`, `gold/expenses.jsonl` e `gold/finance_summary.json`
- `gold/market_summary.json` e `gold/data_quality.json`

Revise `data/gold/data_quality.json` antes de publicar informacoes no ERP. Ele concentra precos sem taxas, nomes que precisam de confirmacao, abas excluidas e outras excecoes de qualidade.

## Publicar Precificacao no ERP

Depois de revisar a saida do ETL, publique a planilha ativa com o mesmo contrato de transformacao:

```powershell
.\.venv\Scripts\python.exe scripts\import_pricing.py "data/raw/pricing/current/Precificação 2026.xlsx"
```

O importador preserva quantidade e estoque minimo ja cadastrados. Por seguranca, produtos cujo valor sugerido nao informa taxas nao sao publicados sem aprovacao explicita. Use `--allow-price-without-fees` somente apos revisar o alerta no relatorio de qualidade.

## Documentos e Imagens

A tela **Arquivos** sincroniza os itens aceitos em `data/raw` e os novos arquivos em `data/raw/inbox`. A sincronizacao deduplica por hash e guarda classificacao, etiquetas, vinculos e metadados de origem. Fotos de comprovantes nao geram lancamentos automaticos; essa separacao evita duplicidade entre compra, estoque e contas a pagar.

Em Cloud Run, envie os arquivos pela tela ou execute a sincronizacao em uma maquina com as credenciais Firebase. O container nao deve depender de arquivos do computador local.

## Testes

```powershell
.\.venv\Scripts\python.exe -m pytest
```
