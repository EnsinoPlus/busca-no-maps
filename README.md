# Leads Maps — Coleta de negócios via Google Places API

Ferramenta para buscar negócios (advogados, contadores, etc.) por localização e
salvar nome, endereço, telefone, site e avaliação num banco local.

## 1. Conseguir a chave da API (obrigatório)

1. Acesse https://console.cloud.google.com/
2. Crie um projeto (ou use um existente).
3. Ative a **Places API** em "APIs e Serviços" > "Biblioteca".
4. Cadastre uma forma de pagamento (o Google exige, mas há cota gratuita mensal —
   consulte os preços atuais em https://mapsplatform.google.com/pricing/).
5. Em "Credenciais", crie uma **API Key** e restrinja o uso dela apenas à Places API
   (evita cobrança indevida se a chave vazar).

## 2. Instalar

```bash
pip install -r requirements.txt
export GOOGLE_MAPS_API_KEY="sua_chave_aqui"
```

No Windows (PowerShell): `$env:GOOGLE_MAPS_API_KEY="sua_chave_aqui"`

## 3. Rodar

Busca simples:
```bash
python main.py "advogado trabalhista Mossoró RN"
```

Múltiplos termos:
```bash
python main.py "advogado trabalhista Mossoró RN" "contador Mossoró RN"
```

A partir de um arquivo de termos (um por linha):
```bash
python main.py --file termos.txt --csv
```

## 4. Onde ficam os dados

- `leads.db`: banco SQLite local, sem duplicados (chave = place_id do Google).
- `leads_export.csv`: gerado se usar `--csv`.

Para abrir o `.db` visualmente, use um programa como o DB Browser for SQLite
(gratuito).

## Observações importantes

- **Custo**: cada busca de texto + detalhes de cada resultado consome cota da API.
  Monitore o uso no Console do Google Cloud.
- **Termos de uso do Google**: os dados retornados pela Places API têm restrições
  de armazenamento — não é permitido guardar indefinidamente todos os campos por
  tempo ilimitado nem redistribuir os dados brutos como um banco próprio de forma
  comercial. Para uso interno de prospecção (o caso aqui), isso não costuma ser
  problema, mas vale ler os termos completos:
  https://cloud.google.com/maps-platform/terms
- Esta ferramenta busca **apenas negócios/locais públicos** (nome do
  estabelecimento, endereço comercial, telefone comercial). Ela não foi feita
  para localizar dados de pessoas físicas — isso está fora do escopo pretendido
  e também fora do que a Places API expõe.
