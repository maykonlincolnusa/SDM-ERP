# Revisão funcional e de resiliência — 07/09/2026

Revisão local de estoque, retiradas, fabricação, compras, cadastros, comunicação HTTP e arquitetura de carregamento. Não houve implantação nem teste de carga no ambiente de produção.

## Correções

- Campos de quantidade com largura, contraste e foco próprios; no celular, seleção de insumo e quantidade ocupam linhas separadas. O componente atende retiradas, fichas técnicas e fabricação.
- Quantidades exibidas com até três casas decimais: `0,001` não aparece mais como zero. Valores monetários continuam com seu formatador próprio.
- Soma das linhas de retirada e fabricação arredondada para a precisão do estoque, evitando falso saldo insuficiente em `0,1 + 0,2` contra saldo `0,3`.
- Limpar a quantidade da retirada revoga e oculta a autorização anterior para saldo negativo.
- Prévias de compra recusam valores não positivos ou não finitos; fabricação recusa rendimento inválido e sinaliza consumo do próprio item produzido.
- Bloqueio compartilhado de envio simultâneo nos formulários que usam `submitData` e nas edições que usam `send`, com botão desabilitado e `aria-busy`. Falha libera os controles e preserva o formulário.
- Falha de conexão durante escrita informa que o resultado é desconhecido e orienta conferir o histórico. Não há repetição automática de gravações.
- Gravação confirmada seguida de falha de atualização tem mensagem distinta, evitando sugerir que o lançamento precisa ser repetido.
- Respostas HTTP 204 são aceitas sem tentar interpretar um corpo JSON inexistente.
- Datas de despesa e observação de preço usam o dia local, evitando avanço de data à noite por UTC.
- Requisições recebem `X-Request-ID` e `Server-Timing`; logs incluem rota parametrizada, status, duração e referência. Falhas inesperadas registram tipo da exceção e localização no código sem texto da exceção, corpo, token ou parâmetros de consulta. Erros 500 mostram a referência na tela.
- Versão do cache do aplicativo atualizada para distribuir as mudanças.

## Limites e pontos de evolução

| Ponto | Evidência no código | Implicação |
| --- | --- | --- |
| Carga geral acoplada | `loadAll` aguarda 34 consultas com `Promise.all` e só depois atualiza o estado | Uma falha impede a atualização geral; uma gravação volta a carregar relatórios de todas as áreas. Evolução: carregamento por tela, limites de concorrência e estado de atualização por seção. |
| Leituras sem paginação | `FirestoreStore.list` percorre a coleção; relatórios leem listas inteiras | Custo e latência tendem a crescer com o histórico. Requer medição com dados representativos antes de definir paginação, índices e agregações. |
| Repetição entre dispositivos | Bloqueio de envio existe no navegador, sem chave de idempotência persistida nas retiradas | Não garante deduplicação após recarga, perda de resposta ou envio por outro dispositivo. Evolução exige contrato de idempotência no servidor. |
| Escala do modo demonstrativo | `DemoStore` usa arquivo JSON e trava da instância | Não deve ser tratado como armazenamento distribuído; múltiplos processos não compartilham a trava. |
| Saúde versus prontidão | `/health` informa modo e estado do processo sem testar acesso ao banco | Não prova que o armazenamento está disponível. Uma verificação de prontidão precisa de política de frequência e tempo limite. |
| Observabilidade | Logs e identificadores locais; sem painel, retenção ou alertas provisionados nesta revisão | Não equivale a monitoramento de produção completo. |
| Elasticidade econômica | Precificação combina custo, margem e referências; não foi encontrado modelo explícito de elasticidade preço-demanda | Recomendações de preço não comprovam quanto a demanda mudará. Estimar isso exige histórico adequado e controle de outros fatores. |

## Validação

Foram acrescentados testes que executam os helpers JavaScript com atrasos e falhas controladas, testes dos logs e respostas de erro e um teste de navegador com API real e loja demonstrativa temporária. O teste de navegador bloqueia acesso externo e verifica baixa de `0,001` no estoque. Node.js e Playwright/Chromium são necessários para essas respectivas verificações; testes opcionais indicam ausência das ferramentas com `skip`.

Os testes locais de transação não constituem teste de carga nem homologação em Firestore real.
