# Radar Tech

Base do bot de ofertas tech com SQLite, score explicável e administração pela conversa privada no Telegram. Modo inicial: **simulação**. Não há afiliados, scraping, cookies de conta ou canal de logs.

## Iniciar no Windows

Python 3.12 ou superior. Ambiente local preparado em `.venv`, que está ignorado pelo Git.

```powershell
.\.venv\Scripts\python.exe main.py --dry-run
```

`--dry-run` força simulação, inclusive se houver `AUTO_PUBLISH=true` no ambiente. Inicia buscas e o painel privado; não envia ofertas ao canal. Para encerrar, `Ctrl+C`.

Para validar apenas os comandos privados, sem consultar fontes de ofertas:

```powershell
.\.venv\Scripts\python.exe main.py --dry-run --admin-only
```

`--admin-only` também força publicação desligada e bloqueia `/buscar` nesta sessão.

Em uma instalação nova:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --dry-run
```

Quem preferir usar o Python global pode instalar `requirements.txt` com `py -m pip install -r requirements.txt` e iniciar com `py main.py --dry-run`.

O programa usa as variáveis do ambiente e, ao ser iniciado pelo usuário, carrega o `.env` local com `python-dotenv`. Não substitui valores já definidos no ambiente e nunca escreve no `.env`. Nenhum módulo carrega esse arquivo só por ser importado.

Mantenha suas credenciais existentes. Acrescente manualmente apenas as opções que quiser configurar, usando `.env.example` como referência. Não substitua seu `.env` pelo exemplo. Os valores ausentes usam os padrões seguros abaixo.

## Descobrir e configurar administrador

1. Inicie o programa em simulação.
2. Abra a conversa **privada com o bot**, não com o canal `@RadarTechOfertas`.
3. Envie `/whoami`. A resposta contém somente seu Telegram user ID numérico.
4. Coloque esse número em `TELEGRAM_ADMIN_ID` no seu `.env` e reinicie o programa.

Até configurar o ID, apenas `/whoami` fica disponível. Os demais comandos respondem exatamente `Não autorizado.`. A autenticação compara o ID de `message.from` recebido da API com `TELEGRAM_ADMIN_ID`; nunca usa username. Comandos em grupos são recusados.

| Comando | Resultado |
| --- | --- |
| `/whoami` | Seu user ID; funciona antes de cadastrar admin. |
| `/status` | Estado do sistema e buscador, modo, pausa, última/próxima busca, contagens, simulações e últimos erros. |
| `/ultimas` | Últimas cinco publicações confirmadas, com preço e link. |
| `/stats` | Observações analisadas, produtos únicos, total publicado, média de score e termos mais publicados. |
| `/pause` | Bloqueia novas publicações e simulações; coleta e histórico continuam. Um envio já em andamento pode concluir. |
| `/resume` | Retoma o modo configurado. Não altera `AUTO_PUBLISH`. |
| `/buscar` | Coloca uma busca imediata na fila; recusa duplicar uma busca já em andamento. |

O painel usa long polling e exige que este processo permaneça aberto. Com processo desligado, o bot não responde `/status`. Outro processo de polling ou um webhook existente pode causar HTTP 409; o app informa o conflito e não remove webhooks automaticamente.

## Mercado Livre: situação desta etapa

**Busca real ainda depende de acesso adicional e não foi validada contra a sua aplicação.** O adaptador usa apenas o recurso oficial de busca do site MLB, com uma página de resultados por termo. Não há fallback para scraping nem dados fictícios em produção.

A documentação oficial apresenta chamadas autenticadas com access token e também restrições sobre recursos de itens/buscas. Falta obter, fora deste projeto, uma aplicação autorizada no Mercado Livre Developers e um access token OAuth válido com permissão para o recurso e uso pretendidos. O token deve ser informado em `MERCADO_LIVRE_ACCESS_TOKEN`. **Ter um token não garante que a plataforma libere a busca de anúncios de terceiros**; a autorização para busca geral por termo precisa ser confirmada com o Mercado Livre. Este código não acessa sua conta, não implementa login e não obtém nem renova tokens.

Sem esse acesso, a busca registra “acesso pendente”, guarda o erro e o painel continua disponível. HTTP 401, 403, 404 e 429 são tratados; o ciclo para após o erro e aguarda o próximo agendamento, sem insistir em páginas alternativas. Se a plataforma não autorizar a busca de terceiros, será necessário escolher outra fonte permitida implementando `ProductSource.search`.

Referências oficiais consultadas:

- [Itens e buscas](https://developers.mercadolivre.com.br/en_us/public-and-private-resources/items-and-searches)
- [Permissões funcionais](https://developers.mercadolivre.com.br/pt_br/permissoes-funcionais/)
- [Telegram Bot API](https://core.telegram.org/bots/api)

São aceitos produtos novos e ativos em BRL, com preço válido, título relacionado a tech e link normal do Mercado Livre. Metadados ausentes não são completados por suposição. Avaliação do vendedor não vira avaliação do produto; `base_price` não vira preço antigo. URLs de produto perdem query string e fragmento, eliminando rastreamento e parâmetros de afiliado. A primeira fonte cobre itens reconhecidos por palavras do título; gadgets fora dessa lista podem ser descartados até calibrarmos o filtro.

## Configuração e limites

| Variável | Padrão | Uso |
| --- | --- | --- |
| `TELEGRAM_ADMIN_ID` | vazio | Apenas `/whoami` fica liberado até cadastrar o admin. |
| `AUTO_PUBLISH` | `false` | Simula sem enviar ofertas. Publicação real exige também admin configurado. |
| `MIN_OFFER_SCORE` | `70` | Score mínimo, além de evidência de economia. |
| `MAX_POSTS_PER_DAY` | `12` | Limite por dia no fuso configurado. |
| `MIN_MINUTES_BETWEEN_POSTS` | `30` | Intervalo global, inclusive para quedas significativas. |
| `REPUBLISH_AFTER_HOURS` | `72` | Janela sem repetir produto. |
| `SIGNIFICANT_PRICE_DROP_PERCENT` | `10` | Queda mínima em relação ao último preço publicado para abrir exceção à janela de 72h. |
| `SEARCH_INTERVAL_MINUTES` | `60` | Intervalo depois do fim de cada ciclo. |
| `SEARCH_TERMS` | lista tech do exemplo | Termos separados por vírgulas. |
| `SEARCH_TERMS_PER_CYCLE` | `3` | Rodízio persistente dos termos; evita consultar tudo de uma vez. |
| `SEARCH_LIMIT_PER_TERM` | `20` | Uma página, até 20 resultados por termo. |
| `REQUEST_INTERVAL_SECONDS` | `2` | Pausa entre consultas da fonte. |
| `HISTORY_DAYS` | `30` | Janela usada na comparação local de preços. |
| `POST_TIMEZONE` | `America/Sao_Paulo` | Virada da cota diária e horários no painel. `tzdata` fornece o fuso no Windows. |
| `MERCADO_LIVRE_ACCESS_TOKEN` | vazio | Acesso adicional à API oficial, quando autorizado. |

O ciclo publica ou simula as melhores ofertas elegíveis primeiro. As restantes ficam registradas nas avaliações; não existe fila de envio de preços antigos. São reavaliadas caso apareçam em uma busca posterior. A ordem dos termos continua após reiniciar.

Pausas, histórico, publicações e limites sobrevivem a reinicializações. Simulações têm histórico separado: obedecem aos limites para mostrar um comportamento realista, mas não consomem as cotas de envio real nem aparecem em `/ultimas` ou na média de publicações. Uma mesma oferta não gera repetidamente uma prévia a cada busca.

Publicações passam por uma reserva em transação SQLite antes do envio. Timeout ou resposta sem confirmação pode significar que a mensagem já chegou ao canal: nesse caso, o app não repete o POST, marca a tentativa como `unknown` e pausa novas publicações. Confira o canal antes de `/resume`; aquele produto continua bloqueado até revisão manual do registro no banco. Uma interrupção entre envio e gravação é tratada da mesma forma no próximo início. Cotas consideram envios pendentes/sem confirmação para evitar excesso. Esta proteção prefere não reenviar a prometer entrega “exatamente uma vez”, que a Bot API não oferece neste fluxo.

## Como o score funciona

Score de 0 a 100: soma das notas vezes seus pesos, dividida pela soma dos pesos **disponíveis**, arredondada. Cada nota fica entre 0 e 1.

| Critério | Peso | Nota |
| --- | --- | --- |
| Desconto | 40 | Calculado só com preço atual e preço original válido; 50% ou mais recebe nota 1. |
| Histórico | 35 | Queda contra o menor preço local anterior na janela; 20% ou mais recebe nota 1. O preço atual não entra na referência. |
| Avaliação | 15 | Nota 3 ou menos recebe 0; nota 5 recebe 1. Só avaliação do produto recebida da fonte. |
| Vendidos | 5 | Escala logarítmica de 0 a 1.000 unidades; ausente é ignorado. |
| Marca | 5 | Marca informada nos atributos e presente na lista conhecida recebe 1; marca informada mas desconhecida recebe 0. |

**Sem desconto positivo ou queda positiva no histórico, não há aprovação**, mesmo com boa marca/avaliação. Não há bônus por preço absoluto: comparar diretamente um carregador com um notebook distorceria o score. Recência é aplicada como trava de publicação. A fórmula é heurística inicial; score alto não é promessa de menor preço do mercado.

As linhas de desconto e avaliação são omitidas quando faltam dados. O texto de apoio usa apenas informação observada, sem inventar atributos ou alegar custo-benefício não verificado.

## Arquitetura e dados

```text
main.py                  início, ciclo de vida e polling dos comandos
config.py                leitura e validação de configurações
database.py              esquema SQLite, persistência e limites em transação
models.py                produtos, preços em centavos e resultado do score
engine.py                agendamento, rodízio, avaliação e publicação/simulação
telegram_service.py      Bot API, timeout, mensagens e proteção do modo seguro
admin_bot.py             comandos privados e autorização pelo ID
safety.py                erros seguros e filtro de logs
runtime_lock.py          apenas uma instância local por banco
services/mercado_livre.py adaptador substituível e normalização da fonte
services/offer_filter.py  cálculo do score
services/offer_formatter.py mensagem final
tests/test_mvp.py         testes sem rede nem leitura de .env
data/radartech.db         banco local, criado no primeiro uso e ignorado pelo Git
teste_telegram.py        teste original preservado; envia mensagem real se executado
```

Tabelas: `products`, `price_history`, `search_runs`, `offer_evaluations`, `publications`, `simulations`, `system_state` e `recent_errors`. Preços são **centavos inteiros**, datas no banco são UTC e campos não informados são `NULL`. Produtos são únicos por `(source, external_id)`; cada busca gera uma observação do preço e uma avaliação. Publicações confirmadas guardam o ID da mensagem. Termos mais publicados aparecem após pelo menos cinco publicações, para termos repetidos ao menos duas vezes.

O banco não guarda tokens. Logs usam o terminal, com início/fim de busca, contagens, prévias, publicações confirmadas e erros seguros. Respostas brutas de APIs e tracebacks de requests não são registrados. `.env`, `.vscode/`, `.venv/` e dados locais estão ignorados.

## Verificação local

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Testes usam SQLite temporário, dados sintéticos exclusivamente de teste e transportes simulados. Bloqueiam rede real e carregamento do `.env`. Cobrem autorização, dados ausentes, score, normalização de links, cota diária/fuso, intervalo, repetição, queda de preço, concorrência, recuperação de interrupções e segurança dos logs.

Para o usuário executar somente uma coleta em simulação, depois de ter acesso autorizado à fonte:

```powershell
.\.venv\Scripts\python.exe main.py --dry-run --once
```

Nenhuma publicação automática real nem comando ao bot foi executado durante a implementação. O teste Telegram antigo foi preservado, mas não foi executado nesta etapa.
