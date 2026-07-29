# Leads Maps v2

Aplicação Flask + SQLite, em PT-BR, para prospectar negócios públicos pela Google Places API, enriquecer e-mails e acompanhar o funil comercial.

## Recursos v2

- busca explícita por **segmento, cidade, UF e localização**;
- radar/progresso em streaming com fase, contadores e cancelamento; fallback HTML funciona sem JavaScript;
- alvo conta somente lead novo com e-mail real ou upgrade de um registro sem e-mail; duplicados não consomem o alvo;
- variações automáticas limitadas, deduplicação de candidatos, parada no alvo e teto rígido de API;
- qualidade de e-mail `alta`, `media` ou `baixa`, confiança numérica, alinhamento de domínio e consulta MX com timeout;
- CRM com status `novo`, `para_contatar`, `contatado`, `respondeu`, `interessado`, `reuniao`, `cliente` e `descartado`, além de notas, responsável, tags e follow-up;
- filtros avançados e ordenação com leads que têm e-mail primeiro;
- deduplicação por `place_id`, e-mail/telefone/domínio normalizados ou nome+endereço;
- CSV neutralizado contra fórmulas e XLSX formatado; exportação selecionada, filtrada ou ainda não exportada;
- painel diário persistente de requisições e custo **estimado**;
- backup SQLite online diário/lazy e manual em `backups/`, com retenção de 30 arquivos.
- sincronização manual e confirmada de contatos com listas do Brevo, sem disparo automático de campanhas.

As migrações são aditivas e idempotentes: bancos existentes são atualizados sem apagar leads.

## Instalação local

```bash
python -m pip install -r requirements.txt
copy .env.example .env
python app.py
```

Abra `http://127.0.0.1:5000`. Nunca versione `.env`, chaves ou senhas.

## Configuração

Variáveis principais (veja `.env.example`):

- `GOOGLE_MAPS_API_KEY`: chave restrita à Places API;
- `LEADS_DB_PATH`: caminho absoluto do banco em volume persistente;
- `APP_USERNAME`, `APP_PASSWORD`, `APP_SECRET_KEY`: autenticação HTTP Basic;
- `APP_PUBLIC_ACCESS=1`: acesso público explícito (CSRF, limites e cabeçalhos continuam ativos);
- `APP_SECURE_COOKIES=1`: obrigatório atrás de HTTPS;
- `API_DAILY_REQUEST_CEILING`: máximo diário persistido de requisições;
- `API_MAX_REQUESTS_PER_SEARCH`: teto adicional por busca;
- `GOOGLE_PLACES_TEXT_SEARCH_RATE` e `GOOGLE_PLACES_DETAILS_RATE`: tarifas unitárias usadas somente para estimativa;
- `EMAIL_MX_CHECK=0`: desativa consulta MX limitada, se necessário.
- `BREVO_API_KEY`: chave da API Brevo, usada somente no backend;
- `BREVO_SYNC_PASSWORD`: senha exclusiva para desbloquear a sincronização na interface pública.

Confirme preços e cobrança reais no Google Cloud; o painel exibe estimativas, não faturas.

## EasyPanel / produção

Monte um volume persistente (por exemplo `/data`) e defina `LEADS_DB_PATH=/data/leads.db`. Use HTTPS, cookies seguros e autenticação, ou aceite conscientemente o risco de `APP_PUBLIC_ACCESS=1`.

Comando de inicialização recomendado, mantendo **um worker** para a carga SQLite/streaming:

```bash
gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 300 app:app
```

O endpoint público de saúde é `GET /health`. Não use proxy buffering no endpoint `/buscar`; a resposta envia `X-Accel-Buffering: no`.

## Segurança

A aplicação mantém autenticação fail-closed em produção, rate limiting, CSRF em todo POST, escape de HTML, URLs externas restritas a HTTP(S), bloqueios SSRF/redirect/proxy do coletor de e-mail, neutralização CSV/XLSX, limite de upload e SQLite `WAL` + `busy_timeout`. Downloads GET legados são não mutantes; somente exportação POST com CSRF marca `exported_at`. A UI de backup nunca recebe caminhos do usuário.

## Integração com o Brevo

1. Crie uma chave de API no Brevo e ao menos uma lista de contatos.
2. Defina `BREVO_API_KEY` e uma senha forte e exclusiva em `BREVO_SYNC_PASSWORD` no ambiente do servidor.
3. Abra **Brevo** na aplicação e desbloqueie a integração.
4. Em **Leads**, selecione até 50 registros e clique em **Preparar envio ao Brevo**.
5. Confira a quantidade com e-mail válido, escolha a lista e confirme.

A autorização expira após 15 minutos e é invalidada automaticamente quando `BREVO_SYNC_PASSWORD` é alterada. A operação é idempotente por e-mail (`updateEnabled=true`): contatos existentes são atualizados em vez de duplicados. Leads sem e-mail válido são ignorados; falhas específicas de um contato não interrompem o lote, enquanto indisponibilidade, HTTP 5xx ou limite 429 interrompem os contatos restantes com segurança. A aplicação registra o identificador remoto, a lista, a última tentativa e o último erro seguro. A chave nunca é enviada ao navegador nem incluída nos logs.

Esta primeira etapa somente cria/atualiza o contato e o associa a uma lista. Ela **não dispara campanhas**, não marca consentimento e não reinscreve deliberadamente contatos descadastrados. Antes de qualquer campanha, valide a base legal, a identificação do remetente, o opt-out e as supressões conforme a LGPD e as políticas do Brevo.

## Verificação

```bash
python -m pytest -q
ruff check .
python -m compileall -q .
node --check static/search-v2.js
git diff --check
```

## Uso responsável

Colete apenas dados comerciais públicos e respeite os termos da Google Maps Platform, LGPD, opt-out e políticas de contato. Revise as regras de armazenamento da Places API antes de uso comercial.
