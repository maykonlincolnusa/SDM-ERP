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

## Operacao no dia a dia

### Edicao de tudo que e lancado

Todo registro do ERP pode ser corrigido, e a correcao mexe no saldo junto. Essa
e a regra que sustenta o resto: registro certo com prateleira errada e pior do
que nao ter edicao.

- **Pedido** — `PATCH /api/orders/{id}` refaz a baixa de estoque: devolve o que
  a versao antiga consumiu e aplica a nova. O pedido guarda `stock_usage`, o que
  realmente saiu, entao o estorno e exato mesmo que a ficha tecnica tenha mudado
  depois. `POST /api/orders/{id}/cancel` tira o valor do faturamento, devolve o
  insumo e mantem o pedido no historico. `DELETE` so aceita pedido ja cancelado.
- **Pedido com data retroativa** — `occurred_at` permite lancar o movimento de
  ontem. `registered_at` guarda quando foi digitado; a diferenca entre os dois
  denuncia lancamento feito muito depois do fato.
- **Estoque** — `PATCH /api/inventory/{id}` edita o cadastro, mas nao o saldo.
  Saldo muda por `POST /api/inventory/{id}/count` (contagem fisica: informe o
  que existe, o sistema calcula a diferenca), por `POST
  /api/inventory/{id}/purchases` (compra recebida: quantidade, unidade comprada,
  conteudo por unidade, custo, fornecedor, documento e data) ou por ajuste. A
  compra converte, por exemplo, `2 caixas x 5 kg` para 10 kg e recalcula o custo
  medio ponderado; o historico continua mostrando as duas caixas. Ajuste fica
  reservado para perda, quebra, degustacao e outros acertos. Assim toda
  diferenca tem um movimento explicando de onde veio. Insumo usado em receita
  ou como porcao de complemento nao pode ser excluido.
- **Consumo** — `PATCH`/`DELETE /api/inventory/movements/{id}` corrige ou desfaz
  um lancamento manual, acertando o saldo. Baixa de venda e estorno de
  cancelamento sao recusados: eles existem porque um pedido existe, e so mudam
  pelo pedido.
- **Despesa que se repete** — marcar `recurring` deixou de ser um rotulo e
  passou a abrir uma serie: o sistema lanca sozinho cada mes que vence, de
  `starts_on` ate o mes corrente. E idempotente (o id do lancamento e a serie
  mais o mes), nunca materializa mes futuro, e o mes apagado nao volta na
  proxima geracao. `GET /api/expenses/recurring` lista as regras,
  `PATCH /api/expenses/recurring/{id}` muda o que ainda sera lancado — o passado
  nao se reescreve — e `DELETE` para de repetir mantendo o historico.
  `GET /api/expenses/fixed-cost` devolve o custo fixo do mes pelas regras: e ele
  que a projecao de lucro usa, em vez de uma media do passado.
- **Despesa, produto, complemento e canal** — `PATCH` e `DELETE` proprios.
  Produto com venda no historico sai em duas etapas: o primeiro `DELETE` apenas
  tira do cardapio, e `DELETE /api/products/{id}?permanent=true` remove do
  cadastro. O pedido antigo sobrevive porque cada linha guarda nome, preco e
  custo do dia da venda; o que se perde e o dossie e a previsao daquele produto,
  e por isso a segunda etapa e explicita. Complemento e canal com venda sao
  inativados.
- **Rastro** — toda alteracao grava em `audit_log` com o que mudou, de que valor
  para qual. `GET /api/audit?entity=&entity_id=`.

### Lucro operacional

`GET /api/finance/operating?days=30` fecha a equacao operacional: faturamento
menos taxa de canal, custo das receitas efetivamente vendidas, perdas de
estoque, custo dos entregadores e despesas operacionais no periodo. Compra de
insumo sai do caixa, mas nao entra duas vezes no lucro: ela vira custo quando o
insumo e vendido. A tela Financeiro tambem mostra unidades, receita, custo e
margem por produto.

Pedido importado sem itens nao recebe custo estimado. Enquanto houver esse tipo
de venda, o resultado aparece como **nao fechado**, com o valor cuja composicao
ainda falta — um lucro parcialmente calculado poderia parecer melhor do que e.

### Dia da loja

Toda agregacao diaria — painel, fechamento, consumo e previsao — usa o dia no
fuso da loja (UTC-3), nao em UTC. Venda das 21h pertence ao dia em que a loja
trabalhou; em UTC ela cairia na barra do dia seguinte e o grafico nao bateria
com o dinheiro na gaveta. "Ultimos 7 dias" tambem significa sete dias fechados:
antes a barra mais antiga contava so o pedaco do dia posterior ao horario em que
a tela era aberta.

### Fechamento do dia

`GET /api/sales/daily?day=AAAA-MM-DD` fecha um dia so, no fuso da loja: quanto
entrou, por canal e por forma de pagamento, o que sobrou depois das taxas, as
perdas lancadas, os cancelamentos e as contas que vencem no dia. A comparacao e
com a media do mesmo dia da semana, que e a referencia que a loja usa de verdade.

### Canais de distribuicao

Canal deixou de ser lista fixa no codigo e virou cadastro com comissao
percentual e taxa fixa. Cada pedido grava `channel_fee`, e
`GET /api/channels/performance` mostra quanto cada canal deixa depois da propria
taxa — que e o numero que responde "vale a pena vender por aqui", diferente do
faturamento por canal.

### Custo fixo da loja

Aluguel, folha, luz, MEI, mensalidade da plataforma. Nao ha de onde importar
isso: a planilha `Financeiro 2026.xlsx` tem os nomes certos e os valores em
branco, e o relatorio da Anota Ai so traz venda. O custo fixo esta no contrato e
no boleto, e o unico caminho honesto e digita-lo — o que da para fazer e digitar
**uma vez**.

`POST /api/expenses/recurring` recebe a lista inteira de uma vez e cria uma
despesa que se repete para cada linha preenchida. A tela (botao **Custo fixo**,
em Financeiro) ja vem com as contas de uma loja de acai preenchidas como rotulo;
o gestor so escreve os valores. `starts_on` define o primeiro mes cobrado —
apontar para o mes de abertura coloca o historico inteiro em dia numa tacada.
Linha que ja existe como despesa ativa e pulada em vez de duplicada, entao
reabrir a tela para acrescentar a conta que faltava e seguro.

`past_paid` (marcado por padrao) grava como pagos os meses ja vencidos, que e o
caso de quem esta colocando o historico em dia; o mes corrente fica pendente de
qualquer forma. Quem esta cadastrando conta em atraso desmarca.

**Entregador conta uma vez so.** Despesa na categoria Entregas e retirada da DRE
quando ha fechamento de turno no periodo, porque o que a loja pagou ao entregador
ja entrou por `delivery_cost`; o valor retirado aparece em `blockers` e em
`courier_expenses_excluded`. Sem nenhum fechamento lancado, a despesa fica — ela
e o unico registro do custo.

### Complementos e cruzamento com o estoque

Acai com banana, com calda de chocolate, no copo de 500 ml. A ficha tecnica
responde pelo copo, mas o que o cliente pede por cima muda a cada pedido — e
enquanto isso nao era cadastrado, a banana e a calda saiam da prateleira sem
nenhum lancamento e o pedido ficava registrado por menos do que custou.

`GET`/`POST`/`PATCH`/`DELETE /api/addons` cadastram cada complemento com tres
numeros: de qual insumo ele sai, quanto sai por porcao (na unidade do estoque) e
quanto e cobrado. O cadastro devolve tambem o custo da porcao pelo estoque de
hoje e quantas porcoes o saldo ainda aguenta.

Na venda, cada item de `POST /api/orders` aceita `addons: [{addon_id,
quantity}]`, onde `quantity` sao porcoes por unidade do produto — dois copos com
calda dupla baixam quatro doses. O pedido grava o complemento com a porcao, o
preco e o custo do dia, entao editar ou cancelar devolve exatamente o que saiu,
mesmo que o cadastro mude depois.

`GET /api/inventory/crossing?days=30` faz o cruzamento: por insumo, quanto saiu
pela ficha tecnica e quanto saiu por complemento, com custo, saldo e duracao; por
complemento, porcoes, receita, custo e taxa de adesao. E a conta que separa duas
decisoes diferentes — mexer na receita ou mexer no preco do complemento — e que
mostra qual delas esta comendo a margem.

A previsao usa a mesma adesao observada: `GET /api/forecast` projeta as porcoes
de cada complemento junto com as unidades do produto que as carrega, soma o
insumo delas na compra sugerida e leva receita e custo para a projecao de lucro
operacional.

### Dossie de custo

`GET /api/products/{id}/dossier` abre a conta inteira de um produto: custo
insumo a insumo com embalagem separada dos ingredientes, quanto sobra em cada
canal depois da comissao, rateio das despesas fixas por unidade vendida, preco
para cada margem-alvo e ponto de equilibrio. Sem volume no periodo, o rateio
volta nulo em vez de dividir por um denominador inventado.

O custo fixo do rateio e a **media dos meses fechados** (ate doze), nunca a soma
de tudo que ja foi lancado: somar trataria o aluguel de cada mes como se fosse do
mesmo mes, e a estrutura cresceria a cada mes de uso do sistema. O mes corrente
fica de fora porque quase sempre ainda tem conta por lancar; ele so entra quando
nao ha nenhum mes fechado, e ai o dossie devolve `partial: true` e avisa na tela.
`expense_basis` diz de quais meses o numero saiu.

### Simulador de produto novo

`POST /api/products/simulate` faz a conta antes de o produto existir. Aceita
insumo que a loja ainda nao compra (`extra_cost`), porque exigir o cadastro
antes de saber se o produto se paga inverte a ordem da decisao.

### Marca e linhas

Produto tem `line` (Classico, Sport, Kids, Sazonal), `claims` e informacao
nutricional. `GET /api/products/lines` mede cada linha em separado — faturamento,
margem, ticket e participacao. Uma linha nova comeca pequena; somada ao cardapio
inteiro, ela e julgada pela media da loja e morre antes de mostrar o que faz.

Identidade visual, tom de voz e parcerias sao trabalho de marca e acontecem fora
do sistema. O que o ERP entrega e o lastro numerico dessa decisao.

### Historico

A tela **Historico** le a trilha de auditoria (`/api/audit`): quem mexeu em que,
quando, e de qual valor para qual. Com tudo editavel, e ela que mantem a
correcao auditavel — sem isso, abrir edicao em um ERP seria trocar rigidez por
buraco no controle.

O diff guarda so o que mudou de fato, entao cada linha e uma diferenca real, e
nao um despejo do registro inteiro. Filtra por tipo de registro e por texto.

### Listas editaveis

Forma de pagamento, categoria de despesa, linha da marca, categoria de arquivo
e de insumo, veiculo de entrega: todas eram listas fixas dentro do HTML. Agora
sao cadastro, na tela **Ajustes** (`/api/catalogs`).

Cada lista declara **a colecao e o campo que ela alimenta**, e e esse vinculo
que permite as duas operacoes que uma lista solta nao permitiria:

- **Renomear migra os registros.** Trocar "Fixo" por "Custo fixo" sem atualizar
  as despesas deixaria a base apontando para um valor que a lista nao oferece
  mais — inconsistencia silenciosa, do tipo que so aparece meses depois num
  relatorio que nao fecha.
- **Excluir e recusado enquanto houver uso.** A recusa vem com a contagem e
  aponta o caminho que quase sempre era o desejado: renomear.

Valor que ficou nos registros mas saiu da lista aparece como **orfao** na tela.
Ele nao some: e ele que explica por que um relatorio antigo mostra uma
categoria que ninguem consegue mais escolher.

Categoria de insumo e veiculo de entrega usam `datalist` em vez de `select` —
a lista sugere, mas nao impede um valor novo, porque esses campos recebem
valores vindos de importacao.

A ordem dos itens define como as opcoes aparecem no campo de escolha, e e
ajustada por setas em vez de arrastar: no celular — que e onde a loja usa o
sistema — arrastar erra mais do que acerta.

### Exclusao de arquivo

`DELETE /api/media/{id}` tira o arquivo do acervo e apaga a copia do sistema. O
original em `data/raw` **nunca e tocado**: o ERP catalogou o arquivo, nao virou
dono dele. Para o arquivo sincronizado, o checksum entra numa lista de
dispensados — sem isso a proxima sincronizacao traria tudo de volta e a
exclusao pareceria nao ter funcionado.

### Correcao de pedido importado

Pedido vindo da Anota Ai nao traz os itens vendidos, entao a tela de venda nao
serve para ele: ela exige produtos. A tela propria corrige o que a origem
informou — valor, canal, pagamento, data, cliente — e tambem **entrega, frete e
entregador**, que e o que permite acertar as 390 entregas importadas com a
pessoa que as fez.

Na tela **Financeiro**, o botao **Importar Anota AI** recebe o `.xlsx` exportado
pela plataforma e chama `POST /api/imports/anota-ai`. A importacao deduplica por
numero e data do pedido; reenvie o mesmo relatorio sem receio de dobrar o
faturamento. Sincronizacao direta por API depende das credenciais e do acesso
contratado na conta Anota AI.

### Entregas e entregadores

Balcao e a maioria das vendas, entao a entrega e marcada de forma **explicita**
no pedido (`is_delivery`), e nao deduzida do frete: entrega com frete gratis
existe, e um frete de zero nao poderia distingui-la de uma venda no balcao.

O pedido ganhou `is_delivery`, `delivery_fee` e `courier_id`. O frete entra no
total porque e dinheiro que a loja recebe, mas fica gravado a parte — o painel
mostra `product_revenue` separado de `delivery_fee`, porque somar os dois faria
o delivery parecer crescimento de venda quando foi so mais gente pedindo em casa.

**Cadastro do entregador** (`/api/couriers`) com quatro componentes de
remuneracao que **se somam** em vez de excluir um ao outro: valor por entrega,
comissao sobre o frete, diaria e salario mensal. Loja de bairro combina diaria
com valor por entrega; forcar a escolha de um modelo unico faria o resto ser
lancado "no bonus", que e exatamente onde o controle se perde. Salario mensal
entra rateado no custo do periodo, senao a entrega pareceria mais barata do que e.

**Lancamento por turno** (`/api/deliveries`), nao por entrega: e assim que o
acerto acontece no balcao no fim do dia. Cada turno registra entregas, frete
arrecadado, se contou diaria, bonus e desconto — e devolve a memoria de calculo
aberta, porque acerto de entregador e conversa, e conversa sem a conta a vista
vira desconfianca. Pagar diferente do combinado e permitido, mas **exige o
motivo escrito**: e o unico ponto em que o numero deixa de sair da regra.

`GET /api/deliveries/suggest` preenche o turno com os pedidos de entrega
daquele dia. Digitar de memoria no fim do expediente e onde o numero se perde.

**Acompanhamento** (`/api/couriers/performance`) responde a pergunta que
importa: **a entrega se paga?** Frete arrecadado contra o que foi pago, custo
por entrega, entregas por turno e resultado por pessoa. Abaixo de 100% de
cobertura, a diferenca sai da margem do produto.

A tela tambem cruza os turnos lancados com os pedidos de entrega do ERP.
Divergencia quase nunca e problema de pessoa — quase sempre e pedido que entrou
sem entregador marcado. Mas ela fica visivel, senao o acerto do fim do mes vira
palavra contra palavra.

O importador da Anota Ai passou a trazer `Valor do frete`, `Entregador` e a
coluna `Retirada`, e cadastra os entregadores encontrados sem remuneracao
definida — zero aparece na tela como "sem remuneracao definida", enquanto um
chute passaria por combinado. O identificador normaliza acento, caixa e espaco,
entao a mesma pessoa grafada de formas diferentes no painel vira um cadastro so.

### Planejamento de producao e compra

`GET /api/planning?days=90&horizon=7&service_level=0.9` responde as duas
perguntas que a previsao de faturamento nao responde: **quanto preparar amanha**
e **quando pedir mais**.

- **Ficha de producao** — modelo do jornaleiro. A quantidade sai do percentil
  `Cu/(Cu+Co)` da demanda observada, onde Cu e a margem que a venda perdida
  deixaria e Co e o insumo que vence. Bater a media significa faltar em metade
  dos dias; a razao entre os dois custos e que decide onde parar. Insumo barato
  que destrava margem grande (banana) recebe percentil alto; insumo caro perto
  do que rende (polpa) recebe percentil menor.
- **Ponto de pedido** — a demanda no prazo do fornecedor sai das somas moveis
  observadas daquele numero de dias. O `minimum_quantity` que hoje e digitado no
  olho ganha um valor sugerido com origem no proprio historico.
- **Curva ABC/XYZ** — ABC pelo custo consumido, XYZ pelo coeficiente de
  variacao. Diz onde vale contagem diaria e onde a mensal resolve.

O cadastro do insumo ganhou `shelf_life_days` (vazio significa "nao perece", e e
o que coloca o item na ficha de producao) e `lead_time_days`.

#### Por que nada aqui ajusta parametro

Com poucos pedidos por dia, ajustar uma normal — ou qualquer curva com
parametro — produz numero com quatro casas decimais e nenhuma sustentacao: o
modelo decora o ruido da amostra. As tres escolhas que evitam isso:

1. **Quantil empirico**, nao distribuicao ajustada. O percentil sai das
   observacoes ordenadas; se o historico nunca viu um dia acima de X, o plano
   nao inventa um.
2. **Encolhimento do fator do dia da semana** por `n / (n + k)`. Um sabado visto
   tres vezes quase nao move o plano; visto vinte, move quase tudo. Sem isso, um
   sabado atipico vira regra permanente.
3. **Convolucao empirica para o prazo de entrega**, nao aproximacao normal. A
   demanda de tres dias vem das somas moveis de tres dias que aconteceram.

Dia sem nenhum movimento e tratado como loja fechada e sai da amostra: contar
domingo fechado como "demanda zero" rebaixaria todo quantil e o plano sugeriria
menos do que a loja precisa nos dias em que ela abre.

### Sugestoes de crescimento

`GET /api/growth?days=90` devolve produtos, combos, servicos, ajustes de preco,
acoes de canal e de marketing derivados do movimento da propria loja:

- **Combos** por analise de cesta — pares que ja saem no mesmo pedido, com lift
  acima do acaso, precificados para manter margem.
- **Produtos** — tamanho ausente numa escada que ja existe, insumo parado que
  nenhuma receita puxa, linha de marca sem produto.
- **Preco** — produto abaixo da margem mediana do cardapio, produto que da
  prejuizo em um canal por causa da comissao.
- **Canais** — quanto o marketplace levou, canal cadastrado que nunca vendeu,
  concentracao excessiva em um canal so.
- **Marketing** — dia da semana vazio, base de clientes inexistente,
  dependencia de um produto so.
- **Servicos** — oferta de ultima hora quando a perda e recorrente, clube quando
  ha recorrencia identificada.

Tres regras valem para todas elas:

1. Nenhuma sugestao aparece sem a evidencia numerica que a gerou.
2. Todo impacto estimado vem com a premissa escrita ao lado. Estimativa nao e
   previsao, e o texto na tela diz isso.
3. Amostra pequena rebaixa a confianca em vez de sumir com a sugestao — e
   quando nao ha base nenhuma, a resposta e "ainda nao da", nao um combo
   generico.

A resposta traz tambem `blockers`: o que falta cadastrar para as sugestoes
melhorarem. Se metade dos pedidos vem importada sem itens, a analise de cesta so
enxerga a outra metade, e a tela diz isso em vez de deixar parecer que a
recomendacao cobre a loja inteira.

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

O ERP usa as colecoes `products`, `inventory`, `orders`, `expenses`, `inventory_movements`, `channels`, `couriers`, `delivery_entries`, `catalogs`, `audit_log`, `system`, `media` e `media_dismissed`. Os binarios da secao Arquivos ficam no Firebase Storage em producao e somente os metadados ficam no Firestore.

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
