# Bot Financeiro Bling + Telegram

Bot assíncrono em Python para consultar a **API v3 do Bling** e entregar relatórios financeiros pelo Telegram usando botões inline e períodos livres.

O projeto usa **long polling** do Telegram, portanto não precisa expor porta HTTP nem configurar domínio no EasyPanel.

## Funcionalidades

- Contas a pagar por vencimento: Hoje, Esta Semana, Este Mês e Este Ano.
- Contas a receber por vencimento: Hoje, Esta Semana, Este Mês e Este Ano.
- Fluxo de caixa projetado: `A Receber - A Pagar`.
- Período livre pelo comando:

```text
/fluxo YYYY-MM-DD YYYY-MM-DD
```

Exemplo:

```text
/fluxo 2026-09-01 2026-09-30
```

- Paginação integral da API, com `limite=100` e avanço de `pagina` até o fim dos resultados.
- Títulos parcialmente pagos/recebidos usam o campo `saldo` do detalhe da conta, em vez do valor original.
- Intervalos maiores que o limite aceito pela API são divididos automaticamente em blocos menores.
- OAuth 2.0 com renovação automática de `access_token` e persistência do `refresh_token` rotativo.
- Tokens gravados de forma atômica em `/app/data/bling_tokens.json`.
- Lock assíncrono para impedir duas renovações simultâneas no mesmo processo.
- Renovação preventiva 5 minutos antes do vencimento.
- Retry automático após HTTP 401, 429 e erros 5xx transitórios.
- Limitação interna de chamadas para permanecer abaixo do limite de 3 requisições/segundo do Bling.
- Acesso ao bot restrito aos IDs configurados em `ALLOWED_USERS`.
- Docker com usuário não-root.

## Estrutura

```text
.
├── bot.py
├── bling.py
├── config.py
├── oauth_setup.py
├── requirements.txt
├── Dockerfile
├── .env.example
├── .gitignore
├── .dockerignore
├── data/
│   └── .gitkeep
└── README.md
```

## Como o cálculo financeiro funciona

O bot trata como pendentes as situações:

- `1` — Em aberto
- `3` — Parcial

Para uma conta totalmente em aberto, o valor pendente é o `valor` do título. Para uma conta parcial, o bot consulta o endpoint individual da conta e usa `saldo`, evitando contabilizar como pendente uma parte que já foi paga/recebida.

O filtro principal é a **data de vencimento**.

Assim, para um período de 01/09 a 30/09:

```text
A receber = saldo dos títulos a receber pendentes com vencimento no período
A pagar   = saldo dos títulos a pagar pendentes com vencimento no período
Fluxo     = A receber - A pagar
```

Isso representa um **fluxo projetado de obrigações e recebimentos pendentes**, e não o extrato de caixa já realizado.

## 1. Criar o bot do Telegram

1. Abra o Telegram e converse com `@BotFather`.
2. Execute `/newbot`.
3. Escolha nome e username.
4. Copie o token fornecido.
5. Descubra o seu ID numérico do Telegram.
6. Coloque somente os IDs autorizados em `ALLOWED_USERS`.

Exemplo:

```env
TELEGRAM_BOT_TOKEN=123456789:token_real_do_bot
ALLOWED_USERS=123456789,987654321
```

Nunca envie o token do bot para o GitHub.

## 2. Criar o aplicativo no Bling

No painel/desenvolvedor do Bling, crie um aplicativo OAuth e habilite os escopos necessários para leitura de:

- Contas a Receber
- Contas a Pagar

No cadastro do aplicativo, configure também a URL de redirecionamento solicitada pelo Bling.

Após salvar o aplicativo, copie:

- `client_id`
- `client_secret`

Configure:

```env
BLING_CLIENT_ID=seu_client_id_real
BLING_CLIENT_SECRET=seu_client_secret_real
```

A API usa OAuth 2.0 Authorization Code. O primeiro `authorization_code` é trocado por `access_token` e `refresh_token`. O projeto solicita tokens JWT com o header `enable-jwt: 1` e mantém esse header nas renovações e consultas seguintes.

Documentação oficial:

- https://developer.bling.com.br/aplicativos
- https://developer.bling.com.br/migracao-jwt
- https://developer.bling.com.br/limites

## 3. Variáveis de ambiente

Copie o modelo:

```bash
cp .env.example .env
```

Preencha o `.env`:

```env
TELEGRAM_BOT_TOKEN=TOKEN_REAL_DO_TELEGRAM
BLING_CLIENT_ID=CLIENT_ID_REAL_DO_BLING
BLING_CLIENT_SECRET=CLIENT_SECRET_REAL_DO_BLING
ALLOWED_USERS=123456789
BLING_TOKEN_FILE=/app/data/bling_tokens.json
TZ=America/Sao_Paulo
LOG_LEVEL=INFO
```

O `.env` está no `.gitignore` e **não deve ser commitado**.

## 4. Primeiro OAuth do Bling

O projeto inclui `oauth_setup.py` justamente para criar o primeiro arquivo de tokens sem precisar editar JSON manualmente.

### Opção recomendada: executar dentro do container já publicado no EasyPanel

Primeiro faça o deploy do serviço com as variáveis de ambiente e o volume `/app/data` já configurados. O bot pode iniciar sem o token do Bling; as consultas financeiras apenas ficarão indisponíveis até concluir esta etapa.

No terminal/shell do serviço no EasyPanel execute:

```bash
python oauth_setup.py
```

O script mostrará uma URL semelhante a:

```text
https://www.bling.com.br/Api/v3/oauth/authorize?response_type=code&client_id=...&state=...
```

Faça o seguinte sem demora:

1. Abra a URL exibida.
2. Entre no Bling e autorize o aplicativo.
3. O Bling redirecionará para a URL cadastrada no aplicativo.
4. Copie a **URL completa** que aparecer na barra do navegador, incluindo `code=` e `state=`.
5. Cole essa URL no terminal onde `oauth_setup.py` está aguardando.

O Bling informa que o `authorization_code` expira rapidamente (aproximadamente 1 minuto), então a troca deve ser feita logo após a autorização.

Se tudo estiver correto, o script gravará:

```text
/app/data/bling_tokens.json
```

Depois disso, reinicie o serviço ou simplesmente utilize o bot; o cliente também consegue carregar o arquivo de forma preguiçosa na primeira consulta.

### Fazer o bootstrap localmente com Docker

Também é possível:

```bash
docker build -t bling-finance-bot .
mkdir -p data

docker run --rm -it \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  bling-finance-bot \
  python oauth_setup.py
```

Depois será necessário transferir com segurança o `bling_tokens.json` gerado para o volume persistente do servidor. Para evitar esse passo, prefira executar `oauth_setup.py` diretamente no container do EasyPanel.

## 5. Rodar localmente sem Docker

Requer Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

No Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Se estiver rodando sem Docker, altere temporariamente no `.env`:

```env
BLING_TOKEN_FILE=./data/bling_tokens.json
```

Faça o OAuth inicial:

```bash
python oauth_setup.py
```

Depois inicie:

```bash
python bot.py
```

## 6. Testar o bot

No Telegram:

```text
/start
```

ou:

```text
/menu
```

Comandos disponíveis:

```text
/start      abre o menu principal
/menu       abre o menu principal
/pagar      períodos de contas a pagar
/receber    períodos de contas a receber
/fluxo      períodos de fluxo por botões
/fluxo 2026-01-01 2026-12-31
```

Teste primeiro períodos curtos, por exemplo "Hoje". Depois teste mês e ano.

## 7. Subir para o GitHub

Dentro da pasta do projeto:

```bash
git init
git add .
git commit -m "feat: bot financeiro Bling Telegram"
git branch -M main
git remote add origin git@github.com:SEU_USUARIO/SEU_REPOSITORIO.git
git push -u origin main
```

Antes do push confirme:

```bash
git status
```

O arquivo `.env` e `data/bling_tokens.json` **não podem aparecer no commit**.

## 8. Deploy no EasyPanel

Crie um **App Service**.

### Source

Escolha GitHub/Git e aponte para o repositório. Se o projeto estiver na raiz, use Build Path `/`.

### Build

Selecione `Dockerfile` e use:

```text
Dockerfile
```

O EasyPanel também detecta automaticamente um Dockerfile no Build Path em configurações compatíveis.

### Environment

Cadastre no painel, sem colocar secrets no Dockerfile:

```env
TELEGRAM_BOT_TOKEN=...
BLING_CLIENT_ID=...
BLING_CLIENT_SECRET=...
ALLOWED_USERS=123456789
BLING_TOKEN_FILE=/app/data/bling_tokens.json
TZ=America/Sao_Paulo
LOG_LEVEL=INFO
```

### Storage — etapa obrigatória

Na seção **Storage** do App, crie um mount do tipo **Volume** e monte em:

```text
/app/data
```

Esse volume é obrigatório porque o Bling entrega um novo `refresh_token` durante a renovação. O código substitui atomicamente `/app/data/bling_tokens.json` pelo token novo.

Sem volume persistente, um rebuild/redeploy pode apagar o arquivo e deixar o bot sem credencial válida.

A documentação do EasyPanel alerta que alterações no filesystem do container podem ser perdidas quando o serviço é recriado; para dados persistentes deve ser usado um mount do tipo Volume.

Referência:

- https://easypanel.io/docs/services/app
- https://easypanel.io/docs/builders

### Porta e domínio

Não são necessários para o bot, pois ele usa Telegram long polling.

### Replicas

Mantenha **1 réplica**.

Este projeto usa um lock assíncrono em memória para proteger a renovação do token dentro de um único processo. Rodar várias réplicas compartilhando o mesmo refresh token pode criar corrida entre renovações e não é recomendado sem um lock distribuído.

### Primeiro deploy

1. Configure Environment.
2. Configure o Volume em `/app/data`.
3. Faça Deploy.
4. Abra o terminal do serviço.
5. Execute `python oauth_setup.py`.
6. Autorize no Bling e cole a URL de retorno.
7. Confirme que existe `/app/data/bling_tokens.json`.
8. Teste `/start` no Telegram.

## 9. Segurança

### Telegram

Todos os comandos e callbacks passam pela mesma checagem de `ALLOWED_USERS`.

Tentativas não autorizadas recebem:

```text
⛔ Acesso negado.
```

O evento é registrado no log com o ID e username do Telegram, sem registrar credenciais do Bling.

### Tokens

- `client_secret`, `access_token` e `refresh_token` nunca devem ser commitados.
- O arquivo de token é criado com permissão `0600` quando o filesystem suporta chmod.
- A gravação usa arquivo temporário + `os.replace`, reduzindo risco de corrupção em reinício durante a escrita.
- O novo refresh token é persistido antes de passar a ser usado em memória.

### JWT do Bling

O cliente envia:

```text
enable-jwt: 1
```

na obtenção, renovação e utilização dos tokens.

## 10. Paginação e limites do Bling

A documentação oficial do Bling informa:

- até 100 registros por página por padrão;
- parâmetro `pagina` para avançar;
- parâmetro `limite` para controlar a página;
- limite da conta de 3 requisições por segundo;
- limite de 120.000 requisições por dia;
- filtros por período acima de um ano retornam HTTP 400.

O cliente deste projeto:

1. solicita `limite=100`;
2. continua incrementando `pagina` enquanto vierem 100 registros;
3. faz uma requisição final para confirmar o término quando necessário;
4. mantém espaçamento mínimo entre requests;
5. trata 429 com backoff;
6. divide intervalos longos em blocos de até 365 dias inclusivos.

## 11. OAuth e renovação automática

O Bling retorna `expires_in` junto do token. O projeto grava `expires_at` como timestamp absoluto.

Antes de qualquer consulta:

```text
se agora + 5 minutos >= expires_at:
    renovar token
```

Na renovação:

1. adquire lock assíncrono;
2. usa o `refresh_token` atual;
3. recebe o novo `access_token` e o novo `refresh_token`;
4. grava o novo par no volume;
5. só então atualiza o estado em memória.

Se uma chamada normal retornar HTTP 401, o cliente força uma renovação e repete a chamada uma vez.

Se o refresh token deixar de ser válido/revogado, execute novamente:

```bash
python oauth_setup.py
```

## 12. Endpoints utilizados

```text
POST /Api/v3/oauth/token
GET  /Api/v3/contas/receber
GET  /Api/v3/contas/receber/{id}
GET  /Api/v3/contas/pagar
GET  /Api/v3/contas/pagar/{id}
```

Base atual:

```text
https://api.bling.com.br/Api/v3
```

O projeto não usa a URL antiga `https://bling.com.br/Api/v3`, cuja descontinuação foi anunciada pelo Bling.

## 13. Troubleshooting

### `Tokens do Bling ainda não foram configurados`

Execute:

```bash
python oauth_setup.py
```

no container com `/app/data` persistente já montado.

### HTTP 403

Normalmente indica que o usuário/app não concedeu o escopo necessário. Revise os escopos do aplicativo no Bling e refaça a autorização OAuth.

### HTTP 401 recorrente

O cliente tenta renovar automaticamente. Se a renovação falhar, refaça `oauth_setup.py`.

### HTTP 429

O cliente já limita a frequência e possui backoff. Se o erro indicar limite diário, aguarde a renovação da franquia da API da conta.

### Arquivo some depois do deploy

O mount persistente do EasyPanel não está configurado corretamente. O caminho dentro do container deve ser exatamente:

```text
/app/data
```

### `Permission denied` em `/app/data`

Confirme que o Volume está montado no caminho correto e que o usuário do container possui permissão de escrita no mount. O Dockerfile prepara `/app/data` para o usuário não-root `app`.

## Dependências fixadas

```text
python-telegram-bot==22.8
httpx==0.28.1
python-dotenv==1.2.3
```

## Observação sobre o conceito de fluxo

O relatório deste projeto responde à pergunta operacional:

> Quanto tenho para receber menos quanto tenho para pagar, considerando os vencimentos pendentes dentro deste período?

Ele não substitui o extrato realizado de Caixas e Bancos nem uma DRE. Se futuramente você quiser, é possível adicionar em outro módulo uma visão separada de **realizado x previsto**, preservando este relatório atual como projeção de caixa.
