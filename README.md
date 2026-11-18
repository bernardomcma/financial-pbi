# financial-pbi

> **Personal Finance ETL Pipeline & BI System** — Python, PostgreSQL, Google Sheets, Telegram/Discord bots, Ollama AI insights, and Power BI (47 DAX measures).

## English Overview

`financial-pbi` is an end-to-end personal finance data pipeline and decision-support system built with **Python**, **PostgreSQL**, **Google Sheets**, **Telegram/Discord bots**, and **Power BI**.

### Key Capabilities

- **Automated ETL & Normalization:** Consolidates cash flow across credit card bills, Pix installments, subscriptions, debit expenses, and income from Google Sheets into PostgreSQL (`main.py`, `bootstrap_pg.py`).
- **Multi-Asset Portfolio Valuation:** Computes net-worth snapshots for **CDI** (Bacen historical series), **Bitcoin** (satoshi conversion), **Cryptocurrencies** (CoinGecko), and **B3 ETFs & Stocks** (Yahoo Finance).
- **Interactive Bots & Mini App:** Telegram and Discord bots (`run_bots.py`, `bots/`, `ui/`) to log transactions, manage credit card bills, and trigger pipeline runs.
- **Local AI Financial Insights:** Generates automated financial summaries and smart transaction parsing via **Ollama** (`ai_financial_insights.py`).
- **Power BI Integration:** SQL layer (`pbi-postgres-sql/`) feeding a Power BI semantic model with 47 DAX measures.

> Below is the full technical documentation in Portuguese (`PT-BR`).

---

## Documentação (PT-BR)

Pipeline de finanças pessoais com integração ao Google Sheets e PostgreSQL para:

- consolidar fluxo de caixa,
- processar receitas e despesas parceladas,
- calcular snapshots de patrimônio (CDI, BTC, criptos, ETFs e ações).

## Visão Rápida

![Python](https://img.shields.io/badge/Python-3.14+-blue)
![Pytest](https://img.shields.io/badge/tests-pytest-green)
![Status](https://img.shields.io/badge/status-active-success)

## Principais Funcionalidades

- Carga e normalização de múltiplas abas do Google Sheets.
- Processamento de fluxo de caixa consolidado com status (`Pago`, `Pendente`, etc.).
- Cálculo de patrimônio com:
    - CDI com série histórica Bacen,
    - BTC com conversão correta de satoshis,
    - Criptos por ticker usando CoinGecko,
    - ETFs e ações da B3 por ticker usando Yahoo Finance.
- Escrita robusta no Sheets com:
    - estratégia de swap para snapshots,
    - append em lote para histórico incremental.
- Logging estruturado e retry com backoff exponencial.
- Testes automatizados com `pytest`.

## Arquitetura

```text
financial-pbi/
├── bots/                     # Bots do Telegram e Discord
├── database/                 # Conexão e persistência em PostgreSQL
├── deploy/                   # Scripts e configurações de deploy
├── docs/                     # Documentação complementar
├── migrations/               # Migrações de schema PostgreSQL
├── models/                   # Dataclasses e contratos de dados
├── pbi-postgres-sql/         # Queries/views SQL para Power BI + PostgreSQL
├── processors/               # Regras de negócio (fluxo de caixa e patrimônio)
├── services/                 # Integrações externas (Google Sheets, cotações, IA)
├── tests/                    # Testes automatizados (pytest)
├── ui/                       # Interface web / Telegram Mini App
├── utils/                    # Logging, retry, parsing e helpers
├── ai_financial_insights.py  # Geração de insights financeiros com Ollama
├── bootstrap_pg.py           # Inicialização e carga do PostgreSQL
├── main.py                   # Entrypoint principal do pipeline ETL
├── run_bots.py               # Launcher unificado dos bots (Telegram + Discord)
├── TELEGRAMBOT_SETUP.md      # Configuração do bot e Mini App do Telegram
├── requirements.txt          # Dependências do projeto
└── requirements-lock.txt     # Lockfile de dependências
```

## Requisitos

- Python 3.14+
- Acesso ao Google Sheets API com conta de serviço
- Arquivo `credentials.json` na raiz do projeto

## Instalação

```bash
pip install -r requirements.txt
```

## Configuração

Ajuste os parâmetros em `config.py`:

- `SPREADSHEET_ID`
- `MAX_RETRIES`, `RETRY_BASE_DELAY_SECONDS`, `REQUEST_TIMEOUT_SECONDS`
- `LOG_LEVEL`, `LOG_FILE`

Importante:

- `SATOSHIS_PER_BITCOIN` está definido como `100_000_000` (valor correto).
- Cotação de ETFs e ações da B3 vem do Yahoo Finance: sem token e sem custo.
  Se a fonte ficar indisponível, a renda variável é pulada com um aviso no log e
  o restante do patrimônio (CDI, BTC, cripto) continua sendo calculado.
- `VENCIMENTO_CARTOES` define o dia de vencimento de cada cartão.
- `DIAS_FECHAMENTO_CARTOES` define quantos dias antes do vencimento a fatura costuma fechar; quando não houver fechamento informado, a rotina usa um fallback conservador.

## Execução

ETL / recálculo do Fluxo de Caixa (rodado pelo cron da VM e pelo `/run_script`):

```bash
python main.py
```

Bots (Telegram + Discord no mesmo processo):

```bash
python run_bots.py
```

Para rodar só um: `python -m bots.telegram_bot` ou `python -m bots.discord_bot`.
Configuração do Mini App do Telegram: ver `TELEGRAMBOT_SETUP.md`.

## Testes

```bash
pytest -q
```

## Contrato de Abas (Google Sheets)

### Entradas esperadas

| Aba | Finalidade |
| --- | --- |
| `FaturasPagas` | Último ciclo pago por cartão |
| `ComprasCartao` | Compras e parcelamentos no cartão |
| `PixParcelado` | Parcelamentos com entrada e parcelas |
| `Assinaturas` | Custos recorrentes (mensais ou anuais, via `Periodicidade`) |
| `DebitoAvulso` | Saídas à vista |
| `Receitas` | Entradas de caixa |
| `Investimentos` | Movimentações por classe: CDI, Cripto, ETF e Ação |

### Saídas geradas

| Aba | Conteúdo |
| --- | --- |
| `FluxoCaixaCompleto` | Movimentações consolidadas |
| `InvestimentoBTC` | Snapshot atual de BTC |
| `InvestimentoCripto` | Snapshot atual de criptos |
| `InvestimentoCDI` | Snapshot atual de CDI (`DataHora`) |
| `InvestimentoRendaVariavel` | Snapshot atual de ETFs e ações da B3 |

## O Que Mudou no Preenchimento da Planilha

- Fluxo de preenchimento manual continua praticamente o mesmo.
- Datas inválidas do tipo somente horário (ex.: `16:24:00`) agora são tratadas como vazias.
- `InvestimentoCDI` usa cabeçalho `DataHora` para preservar precisão temporal.
- Escrita incremental de investimentos é feita em lote (mais estável/performance).

## Observabilidade e Robustez

- Logs em console + arquivo (`logs/financial-pbi.log`).
- Retry padrão para chamadas externas com backoff exponencial.
- Tratamento de exceções específicas do `gspread` e requests.

## Módulos Principais

- `services/google_sheets.py`: leitura/escrita no Sheets.
- `processors/data_loader.py`: carga e normalização das abas.
- `processors/fluxo_caixa.py`: consolidação de entradas/saídas.
- `processors/patrimonio.py`: cálculo de snapshots de investimento.
- `utils/data_utils.py`: parsing de datas/números e cálculo de CDI.
- `utils/logging_config.py`: configuração de logging sem handlers duplicados.
- `utils/retry.py`: retry genérico com backoff.

## Troubleshooting

- Erro de credenciais: verifique `credentials.json` e permissões da conta de serviço.
- Erro de planilha: confirme `SPREADSHEET_ID` em `config.py`.
- Inconsistência de colunas: valide os cabeçalhos das abas de entrada.

## Licença

Uso pessoal.
