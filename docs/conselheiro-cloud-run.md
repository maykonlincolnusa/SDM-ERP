# Conselheiro no Google Cloud

O conselheiro `mcda-evidencia-v1` roda dentro da mesma API FastAPI do SDM. Ele
nao depende de um modelo generativo ou de uma chamada externa: assim, dados de
venda e de clientes permanecem no projeto Google Cloud da loja, a resposta e
reproduzivel e a explicacao continua disponivel mesmo quando nao ha internet
fora do Cloud Run.

## O que o algoritmo faz

Ele seleciona, no maximo, tres temas para a reuniao semanal. Cada item recebe
uma pontuacao de prioridade de 0 a 100, composta por:

| Criterio | Maximo | Regra |
| --- | ---: | --- |
| Protecao da operacao | 55 | Riscos observados, como saldo negativo, entram antes de crescimento. |
| Urgencia | 20 | Acoes que nao podem esperar recebem mais pontos. |
| Confianca da evidencia | 15 | Amostra alta, media ou baixa reduz a pontuacao proporcionalmente. |
| Impacto mensal ajustado | 10 | O impacto e limitado e multiplicado pela confianca; nao domina por ser uma estimativa alta. |

A separacao entre risco, decisao e execucao e intencional: a reuniao nao pode
virar tres sugestoes parecidas de marketing e ignorar estoque, margem ou dados
incompletos. A pontuacao organiza a conversa; ela nao representa chance de
sucesso, retorno garantido ou meta financeira.

## Publicacao no Cloud Run

Preencha o projeto, a regiao e os valores da configuracao web do Firebase antes
de executar. O aplicativo usa a conta de servico atribuida ao Cloud Run para
acessar Firestore e Secret Manager; nenhuma chave de conta de servico deve ser
embarcada na imagem.

```powershell
$project = "SEU_PROJECT_ID"
$region = "southamerica-east1"
$serviceAccount = "sdm-runtime@$project.iam.gserviceaccount.com"

gcloud config set project $project
gcloud services enable run.googleapis.com cloudbuild.googleapis.com firestore.googleapis.com secretmanager.googleapis.com

gcloud run deploy sdm `
  --source . `
  --region $region `
  --service-account $serviceAccount `
  --set-env-vars "APP_ENV=production,DATA_MODE=firebase,AUTH_REQUIRED=true,FIREBASE_PROJECT_ID=$project,FIREBASE_STORAGE_BUCKET=SEU_BUCKET,FIREBASE_WEB_API_KEY=SEU_WEB_API_KEY,FIREBASE_WEB_AUTH_DOMAIN=SEU_AUTH_DOMAIN,FIREBASE_WEB_APP_ID=SEU_WEB_APP_ID"
```

A conta `sdm-runtime` deve ter, no minimo, `roles/datastore.user` para o
Firestore e `roles/secretmanager.secretAccessor` para os tokens de integracoes.
Mantenha o invocador do Cloud Run restrito ou coloque-o atras de um proxy de
autenticacao: o controle de acesso da aplicacao depende de Firebase
Authentication e das claims `erp_admin` e `courier`.

Depois da publicacao, valide sem expor dados de negocio:

```powershell
Invoke-WebRequest "https://SEU_SERVICO/health" | Select-Object -Expand Content
```

O retorno esperado inclui `"status":"healthy"`. Em seguida, entre com uma
conta administradora e abra **Conselheiro**; a API `GET /api/advisor` devolve o
modelo, a pontuacao e as evidencias de cada prioridade.
