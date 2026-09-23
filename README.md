# Bling Finance Bot v7 — Telegram + SQLite + Relatórios Gerenciais

Bot financeiro em Python para Telegram integrado à API v3 do Bling. A versão v7 deixa de reconstruir anos de histórico a cada consulta: usa **SQLite persistente** em `/app/data/financeiro.db`, sincronização incremental e uma camada central de relatórios reutilizável.

O bot usa **long polling**, portanto não precisa expor porta HTTP nem domínio no EasyPanel.

## O que esta versão resolve

- Primeira carga automática: somente os últimos `CASH_BOOTSTRAP_DAYS` dias (padrão 90).
- Uso normal: somente os últimos `CASH_SYNC_DAYS` dias (padrão 7) são relidos no Bling.
- Histórico antigo fica salvo no SQLite e **não é reconsultado automaticamente**.
- Se você alterar algo antigo no Bling, atualize só aquele intervalo com `/sincronizar INICIO FIM`.
- Não existe mais varredura automática desde 2000 nem milhares de consultas individuais `/caixas/{id}`.
- Categorias, fornecedor, CPF/CNPJ, histórico, conta financeira, débito/crédito e valor ficam persistidos no banco local quando a API os retorna.
- Relatórios consultam o SQLite, não a API linha por linha.
- Cálculos financeiros são feitos no backend Python/SQL; a camada de linguagem natural somente identifica intenção, período e filtros.

## Banco local

Arquivo persistente:

```text
/app/data/financeiro.db
```

Principais tabelas:

```text
cash_accounts
cash_movements
categories
category_classifications
sync_state
sync_periods
app_settings
```

### Sincronização

No uso diário:

```text
Telegram
   ↓
verifica janela recente
   ↓
GET /caixas (7 dias por padrão)
   ↓
substitui somente esse período no SQLite
   ↓
relatórios leem o SQLite
```

Se um lançamento dos últimos 7 dias foi editado ou excluído no Bling, a janela recente é substituída e o SQLite fica consistente.

Para histórico antigo:

```text
/sincronizar 2025-01-01 2025-12-31
```

Esse período é buscado uma única vez e fica salvo. Se você nunca alterar 2025, não precisa consultar 2025 novamente.

## Saldos de Caixas e Bancos

A API usada neste projeto fornece os lançamentos de Caixas e Bancos, mas o saldo atual exibido no painel do Bling não é tratado como um campo confiável do catálogo de contas. Por isso a v7 usa **saldo-base calibrado uma única vez**.

Fluxo recomendado:

1. O bot sincroniza os últimos 90 dias.
2. Use `/contas` e confirme quais contas devem ficar ativas.
3. Use `/calibrar`.
4. O bot pede os saldos atuais que aparecem no Bling, por exemplo:

```text
Bling Conta=1209,28; Caixa=734,88; Infinity Bank=377,14; Inter=0,74
```

5. O SQLite grava um saldo-base.
6. Daí em diante:

```text
saldo atual = saldo-base + créditos posteriores - débitos posteriores
```

Assim não é necessário importar todos os lançamentos desde a criação da empresa apenas para manter o saldo atual.

Se uma nova conta for criada, desativada ou houver mudança estrutural nas contas, use `/contas`, ajuste com `/ativar_conta` ou `/desativar_conta` e calibre novamente.

## Relatórios gerenciais

O comando `/relatorios` abre o menu com os 15 relatórios pedidos:

1. Para onde vão cada R$ 100 gastos
2. Ranking de categorias
3. Ranking de fornecedores
4. Histórico de fornecedor
5. Despesas recorrentes / assinaturas
6. Fixas x variáveis
7. Evolução mensal
8. Variação por categoria
9. Pró-labore, retiradas e sócios
10. Despesas administrativas
11. Despesas operacionais
12. DRE gerencial
13. Custo por dia / hora (OPEX)
14. Pequenas despesas acumuladas
15. Gastos fora do padrão

Também existe um **Resumo financeiro** quando a pergunta é ampla.

### Linguagem natural

O bot possui dois níveis de interpretação:

1. **Parser local determinístico** — funciona sem serviço externo e cobre os períodos, filtros e relatórios documentados.
2. **Interpretação por IA opcional** — se `OPENAI_API_KEY` estiver configurada, a pergunta é normalizada pela Responses API com saída estruturada antes de chegar ao parser local. A IA recebe somente a pergunta do usuário e a data atual; lançamentos, saldos e totais financeiros não são enviados. Toda soma, média, margem, ranking e comparação continua sendo calculada pelo backend SQLite. Se a IA estiver indisponível, o bot cai automaticamente para o parser local.

Você pode simplesmente escrever no Telegram:

```text
Quanto gastei com software este ano?
Me mostre as despesas por categoria de 2025.
Quais foram meus maiores fornecedores este ano?
Quanto gastei com OpenAI nos últimos 12 meses?
Compare os gastos com combustível de 2025 e 2026.
Qual foi meu custo operacional médio por mês em 2026?
Quais despesas aumentaram muito este mês?
Quanto estou gastando por dia para manter a empresa?
Me mostre a DRE de janeiro até setembro de 2026.
Quais pequenas despesas mais consumiram dinheiro este ano?
Para onde foram cada R$ 100 que gastei este mês?
O que mais aumentou de agosto para setembro?
Faça um resumo financeiro de 2026.
```

Períodos reconhecidos:

- hoje;
- ontem;
- esta semana;
- semana passada;
- este mês;
- mês passado;
- últimos 30 dias;
- últimos 3, 6 ou 12 meses;
- este ano;
- ano passado;
- ano específico;
- mês específico;
- intervalo de datas `DD/MM/AAAA` ou `AAAA-MM-DD`;
- intervalos de meses, como janeiro até setembro de 2026;
- comparações entre anos ou meses.

Quando a pergunta pede comparação, o backend calcula período atual, período de referência, diferença em R$ e diferença percentual. A DRE comparativa mostra as principais linhas dos dois períodos.

### Filtros

A camada de relatórios suporta, conforme o tipo de relatório:

- categoria;
- categoria principal com inclusão automática das subcategorias;
- fornecedor;
- CPF/CNPJ;
- conta financeira;
- débito/crédito;
- valor mínimo;
- valor máximo;
- período.

## Categorias e classificação gerencial

As categorias financeiras do Bling são sincronizadas para o SQLite. A primeira classificação é sugerida automaticamente apenas para tornar os relatórios úteis desde o início; o valor fica **persistido e editável**.

Listar:

```text
/categorias
```

Editar:

```text
/classificar "Software" comportamento=fixed grupo=administrative dre=operating_expenses opex=sim
```

Valores aceitos:

```text
comportamento:
fixed | variable | direct | administrative | other

grupo:
administrative | operational | commercial | marketing | financial | partners | taxes | revenue | other

DRE:
gross_revenue | deductions | direct_costs | operating_expenses |
other_income | other_expenses | ignore | auto
```

Essa configuração é usada pelos relatórios de fixas/variáveis, administrativo, operacional, OPEX e DRE.

## DRE gerencial

A DRE é baseada nos lançamentos categorizados do SQLite e usa esta estrutura:

```text
Receita Bruta
(-) Deduções / Impostos
= Receita Líquida
(-) Custos diretos
= Lucro Bruto
(-) Despesas Operacionais
= Resultado Operacional
(+) Outras receitas
(-) Outras despesas
= Resultado / Lucro Líquido
```

Mostra também margem bruta, operacional e líquida.

Categorias ainda com `dre=auto` entram por uma regra conservadora e o relatório avisa que precisam ser revisadas. O mapeamento definitivo fica no banco e pode ser alterado por `/classificar`.

## OPEX e custo por dia/hora

Padrões iniciais:

```text
21 dias úteis/mês
8 horas/dia
```

Consultar configurações:

```text
/configurar
```

Alterar:

```text
/configurar dias_uteis=21 horas_dia=8 anomalia_pct=30 anomalia_min=100 pequenas=100
```

## Comandos principais

```text
/start
/menu

/saldos
/posicao
/posicao 2026-09-22 2026-12-31

/pagar
/receber
/fluxo
/fluxo 2026-01-01 2026-12-31

/relatorios
/dre 2026
/fornecedores 2026
/recorrentes 2026
/opex 2026
/anomalias este mês

/sincronizar
/sincronizar 2025-01-01 2025-12-31
/status_sync

/contas
/ativar_conta Infinity Bank
/desativar_conta Pag Bank
/calibrar

/categorias
/classificar "Software" comportamento=fixed grupo=administrative dre=operating_expenses opex=sim
/configurar

/autorizar
/status_bling
```

## OAuth pelo Telegram

O fluxo inicial pode ser feito sem terminal:

1. envie `/autorizar`;
2. toque em **Abrir autorização do Bling**;
3. autorize o aplicativo;
4. copie a URL completa que apareceu no navegador após o redirecionamento;
5. cole essa URL no Telegram;
6. o bot valida o `state`, extrai o `code`, troca por tokens e salva em `/app/data/bling_tokens.json`.

O `refresh_token` é renovado automaticamente e gravado no volume persistente.

`oauth_setup.py` continua existindo apenas como fallback administrativo.

## Segurança

- Somente IDs de `ALLOWED_USERS` acessam comandos e callbacks.
- Tokens ficam fora do Git.
- O token do Bling fica em `/app/data/bling_tokens.json`.
- SQLite fica em `/app/data/financeiro.db`.
- OAuth usa `state` por sessão.
- Renovação de token usa lock assíncrono.
- O container roda como usuário não-root.
- Mantenha **1 réplica** do bot em polling. Duas réplicas com o mesmo token do Telegram geram `409 Conflict` em `getUpdates`.

## Escopos necessários no Bling

Habilite leitura para:

- Contas a Receber;
- Contas a Pagar;
- Caixas e Bancos;
- Contas Contábeis;
- Categorias de Receitas e Despesas.

Se adicionar um escopo depois de já ter autorizado o app, salve a alteração e execute `/autorizar` novamente.

## Variáveis de ambiente

```env
TELEGRAM_BOT_TOKEN=TOKEN_REAL
BLING_CLIENT_ID=CLIENT_ID_REAL
BLING_CLIENT_SECRET=CLIENT_SECRET_REAL
ALLOWED_USERS=123456789

BLING_TOKEN_FILE=/app/data/bling_tokens.json
CASH_DB_FILE=/app/data/financeiro.db

CASH_BOOTSTRAP_DAYS=90
CASH_SYNC_DAYS=7
CASH_ACCOUNT_DISCOVERY_DAYS=90
CASH_HISTORY_START=2000-01-01

TZ=America/Sao_Paulo
LOG_LEVEL=INFO

# Opcional: melhora a interpretação de perguntas livres.
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.6-luna
OPENAI_BASE_URL=https://api.openai.com/v1
```

`CASH_HISTORY_START` fica como limite administrativo para futuras operações de histórico, mas **não provoca varredura automática**.

## EasyPanel

Crie um **Aplicativo** usando o `Dockerfile` do projeto.

### Volume obrigatório

Monte um volume persistente em:

```text
/app/data
```

Ele guarda:

```text
/app/data/bling_tokens.json
/app/data/financeiro.db
/app/data/financeiro.db-wal
/app/data/financeiro.db-shm
```

Sem esse volume, deploys podem apagar os tokens e o histórico local.

### Réplicas

Use:

```text
1 réplica
```

O Telegram long polling aceita somente uma instância consumindo `getUpdates` com o mesmo token.

### Porta

Nenhuma porta precisa ser publicada.

## Primeira instalação recomendada

1. Faça deploy.
2. `/autorizar`.
3. `/sincronizar` — primeira carga de 90 dias.
4. `/contas` — confira quais contas ficaram ativas.
5. Ajuste com `/ativar_conta` e `/desativar_conta` se necessário.
6. `/calibrar` — informe os saldos atuais do painel do Bling uma única vez.
7. Importe os anos que deseja analisar, uma vez por período, por exemplo:

```text
/sincronizar 2022-01-01 2022-12-31
/sincronizar 2023-01-01 2023-12-31
/sincronizar 2024-01-01 2024-12-31
/sincronizar 2025-01-01 2025-12-31
```

8. `/categorias` e revise classificações gerenciais.
9. Comece a perguntar em linguagem natural.

Depois disso, o uso comum só atualiza a janela recente.

## Estrutura do projeto

```text
.
├── bot.py
├── bling.py
├── config.py
├── finance_db.py
├── oauth_setup.py
├── services/
│   ├── __init__.py
│   ├── ai_interpreter.py
│   └── report_service.py
├── reports/
│   ├── base.py
│   ├── common.py
│   ├── formatting.py
│   ├── category_report.py
│   ├── supplier_report.py
│   ├── recurring_report.py
│   ├── fixed_variable_report.py
│   ├── monthly_evolution.py
│   ├── category_variation.py
│   ├── partner_report.py
│   ├── administrative_expenses.py
│   ├── operational_expenses.py
│   ├── dre_report.py
│   ├── opex_report.py
│   ├── small_expenses.py
│   ├── anomaly_report.py
│   ├── expense_search.py
│   └── overview_report.py
├── tests/
├── requirements.txt
├── Dockerfile
├── .env.example
└── README.md
```

## Endpoints do Bling utilizados

```text
POST /oauth/token
GET  /Api/v3/contas/receber
GET  /Api/v3/contas/receber/{id}
GET  /Api/v3/contas/pagar
GET  /Api/v3/contas/pagar/{id}
GET  /Api/v3/contas-contabeis
GET  /Api/v3/categorias/receitas-despesas
GET  /Api/v3/caixas
```

Base:

```text
https://api.bling.com.br/Api/v3
```

A API do Bling limita filtros de período a no máximo um ano e usa paginação. O cliente divide períodos longos em blocos compatíveis e usa `limite=100` por página. Isso ocorre apenas quando um período realmente precisa ser sincronizado.

## Testes

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

A versão entregue foi validada com `compileall`, 17 testes unitários e smoke tests cobrindo os 15 relatórios, SQLite, classificação, calibração de saldo, períodos em linguagem natural e comparações.
