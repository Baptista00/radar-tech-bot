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

A aplicação no DevCenter já foi criada. O módulo OAuth abaixo permite autorizar sua própria conta e renovar tokens, mas **a conexão entre esse cofre e o buscador fica para a próxima etapa**. O adaptador atual ainda usa a opção legada `MERCADO_LIVRE_ACCESS_TOKEN`; não copie tokens novos para essa variável. A função `get_valid_access_token()` será o ponto de integração, evitando tokens fixos. **Ter um token não garante que a plataforma libere a busca de anúncios de terceiros**; a permissão para o recurso e uso pretendidos precisa ser confirmada com o Mercado Livre.

Sem esse acesso, a busca registra “acesso pendente”, guarda o erro e o painel continua disponível. HTTP 401, 403, 404 e 429 são tratados; o ciclo para após o erro e aguarda o próximo agendamento, sem insistir em páginas alternativas. Se a plataforma não autorizar a busca de terceiros, será necessário escolher outra fonte permitida implementando `ProductSource.search`.

Referências oficiais consultadas:

- [Itens e buscas](https://developers.mercadolivre.com.br/en_us/public-and-private-resources/items-and-searches)
- [Permissões funcionais](https://developers.mercadolivre.com.br/pt_br/permissoes-funcionais/)
- [Telegram Bot API](https://core.telegram.org/bots/api)

São aceitos produtos novos e ativos em BRL, com preço válido, título relacionado a tech e link normal do Mercado Livre. Metadados ausentes não são completados por suposição. Avaliação do vendedor não vira avaliação do produto; `base_price` não vira preço antigo. URLs de produto perdem query string e fragmento, eliminando rastreamento e parâmetros de afiliado. A primeira fonte cobre itens reconhecidos por palavras do título; gadgets fora dessa lista podem ser descartados até calibrarmos o filtro.

## Autorizar sua conta do Mercado Livre

Fluxo oficial Authorization Code com PKCE `S256` e `state` aleatório. Sem cookies, scraping ou acesso automático ao navegador. Os comandos OAuth não iniciam o bot, não buscam produtos e não publicam no Telegram. Mantenha `AUTO_PUBLISH=false`.

No DevCenter, mantenha Authorization Code, Refresh Token e PKCE habilitados. As variáveis `MELI_CLIENT_ID`, `MELI_CLIENT_SECRET` e `MELI_REDIRECT_URI` já configuradas manualmente no seu `.env` serão carregadas **quando você executar a CLI**. A Redirect URI deve ser HTTPS, fixa, sem query/fragmento, exatamente como cadastrada. O módulo não lê `.env` no import e não modifica esse arquivo.

Primeiro, no PowerShell da pasta do projeto, execute:

```powershell
.\.venv\Scripts\python.exe -m services.mercado_livre_auth authorize
```

O terminal mostra somente a URL oficial de autorização, sem client secret, verifier ou tokens. Abra essa URL no navegador e autorize com sua própria conta. Cada chamada de `authorize` substitui a tentativa anterior; a sessão local vale por 10 minutos.

Depois da autorização, copie a URL **completa** da barra do navegador, incluindo `code` e `state`. Mesmo se a página de destino não abrir, a URL recebida pode ser usada para finalizar manualmente. Ela é sensível: não envie por chat, não cole em logs ou no README.

Modo recomendado, com entrada oculta e sem colocar a callback no histórico do shell:

```powershell
.\.venv\Scripts\python.exe -m services.mercado_livre_auth exchange
```

Cole a URL no prompt oculto e pressione Enter. O comando valida o destino, o prazo e o `state` antes de fazer o POST. O resultado exibido é somente uma confirmação segura. Se o terminal não permitir entrada oculta, o comando para sem mostrar a URL; use um PowerShell local interativo.

Também existe a forma solicitada com argumento, mas a URL pode ficar no histórico do shell e na lista de processos; prefira o prompt acima:

```powershell
.\.venv\Scripts\python.exe -m services.mercado_livre_auth exchange --callback-url "URL_COMPLETA_RECEBIDA_APOS_AUTORIZAR"
```

O POST usa exclusivamente `https://api.mercadolibre.com/oauth/token`. Client secret, authorization code, verifier e refresh token vão no corpo form-encoded, nunca na URL. Há timeout, redirecionamentos são recusados e erros exibem descrições controladas, sem respostas brutas do provedor.

### Onde ficam os tokens

O arquivo `data/meli_oauth/credentials.bin` guarda tokens, validade e a tentativa PKCE pendente. A pasta inteira está no `.gitignore`, inclusive lock e temporários. Não precisa criar o arquivo manualmente.

- **Windows:** conteúdo criptografado com DPAPI, vinculado à conta Windows; normalmente exige também o mesmo computador. Falha na proteção interrompe a operação, sem salvar texto aberto. Não é um backup portátil: ao trocar de usuário/computador, refaça a autorização.
- **Linux/macOS:** arquivo local com permissão `600`, dentro de diretório `700`, pertencentes ao usuário atual. Nesses sistemas o conteúdo depende da proteção de permissões/disco, sem criptografia própria. Permissões abertas são recusadas.
- Nenhum token entra no SQLite, `.env`, `.env.example` ou logs. `MELI_ACCESS_TOKEN` e `MELI_REFRESH_TOKEN` ficam vazios no exemplo e **não são lidos como fallback**; isso impede ressuscitar refresh tokens antigos. Não preencha esses campos para este fluxo.

Code verifier e state saem do cofre quando a callback é consumida. Tentativas vencidas são rejeitadas e removidas ao finalizar; uma nova autorização substitui a pendente. Uma callback aceita não pode ser reutilizada.

### Renovação automática

`services.mercado_livre_auth.get_valid_access_token()` devolve o token guardado enquanto válido e renova quando entra na margem de 60 segundos da expiração informada pelo provedor. Não há agendador separado: a renovação acontece na chamada da função. Para futura integração, chame-a antes de uma requisição autenticada e nunca imprima seu retorno. Há também `force_refresh=True` para tratar revogação antecipada após um 401, quando a fonte for integrada.

Para verificar a credencial ou renovar se necessário sem exibir seu valor:

```powershell
.\.venv\Scripts\python.exe -m services.mercado_livre_auth refresh
```

Um lock impede renovações concorrentes. O refresh anterior é marcado como consumido **antes** do POST; access token e refresh token novos são gravados juntos por substituição atômica. Não há repetição automática da troca. Timeout, resposta inválida, interrupção ou falha ao salvar podem deixar o resultado incerto: nesse caso, repita `authorize` e `exchange`, sem reutilizar code, refresh token ou cópias antigas do cofre. Nunca restaure um backup de tokens consumidos.

A autorização OAuth foi concluída pelo usuário. A confirmação de `/users/me` e o acesso à busca são validações separadas; autenticar a conta não comprova permissão para buscar produtos.

### Validar autenticação e diagnosticar proxy

No PowerShell normal do VS Code, aberto na pasta do projeto, execute:

```powershell
.\.venv\Scripts\python.exe -m services.mercado_livre_auth verify
```

O comando usa `get_valid_access_token()` e consulta somente `GET https://api.mercadolibre.com/users/me`. Não inicia o bot, não busca produtos, não altera `AUTO_PUBLISH` e não publica no Telegram. Se o token estiver vencendo, o helper renova e grava o novo par no cofre. Se `/users/me` retornar 401, há no máximo uma renovação forçada e uma nova consulta; não existe loop de tentativas.

A saída contém somente `http_status` e `authenticated`, por exemplo, **em caso de sucesso**:

```json
{"http_status": 200, "authenticated": true}
```

Nenhum ID, nome, email, token, header ou JSON da conta é exibido. Logs e debug de headers ficam desativados durante esse diagnóstico. Código de saída `0` indica conta reconhecida; `1` indica autenticação não confirmada. `http_status: null` significa que nenhuma resposta HTTP de `/users/me` foi obtida, inclusive em falhas de conexão, TLS, configuração ou cofre. Uma resposta HTTP anterior, se houver, permanece no resultado quando a tentativa seguinte falha. Isso não comprova que a credencial esteja inválida.

OAuth, refresh, `/users/me` e o adaptador Mercado Livre usam uma `requests.Session` própria com `trust_env=False`. Assim, não herdam `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` ou `NO_PROXY` da sessão de execução. As variáveis do sistema não são modificadas; o transporte do Telegram permanece como estava.

A verificação TLS continua habilitada (`verify=True`), com os certificados padrão do Requests. Também não são herdados `.netrc`, `REQUESTS_CA_BUNDLE` ou `CURL_CA_BUNDLE`. Timeout permanece em 5 segundos para conexão e 20 segundos para leitura; redirecionamentos continuam bloqueados. Não há repetição de POST OAuth como tentativa de contornar uma falha de proxy.

Se o ambiente de execução do Codex impuser restrições de rede/proxy, execute `verify` manualmente no terminal normal. A alteração foi testada com HTTP simulado, sem executar essa chamada real dentro da sessão restrita.

Referências: [OAuth do Mercado Livre](https://developers.mercadolivre.com.br/autenticacao-e-autorizacao), [PKCE — RFC 7636](https://www.rfc-editor.org/rfc/rfc7636.html), [proteção DPAPI do Windows](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata).

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
| `MELI_CLIENT_ID` | placeholder no exemplo | Identificador da aplicação OAuth no DevCenter. |
| `MELI_CLIENT_SECRET` | placeholder no exemplo | Segredo da aplicação; usado somente no corpo do POST OAuth. |
| `MELI_REDIRECT_URI` | placeholder no exemplo | URL HTTPS fixa registrada no DevCenter. |
| `MELI_ACCESS_TOKEN` / `MELI_REFRESH_TOKEN` | vazios | Reservados no exemplo; este fluxo usa exclusivamente o cofre local. |

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
services/mercado_livre_auth.py OAuth PKCE, callback manual e renovação de tokens
services/mercado_livre_http.py sessão HTTP própria, sem proxy herdado e com TLS validado
services/meli_oauth_store.py cofre local protegido e gravação atômica
services/offer_filter.py  cálculo do score
services/offer_formatter.py mensagem final
tests/test_mvp.py         testes sem rede nem leitura de .env
tests/test_mercado_livre_auth.py testes OAuth, erros, rotação e cofre com dados sintéticos
tests/test_mercado_livre_http.py testes de isolamento de proxy e validação TLS, sem rede
data/radartech.db         banco local, criado no primeiro uso e ignorado pelo Git
data/meli_oauth/          cofre OAuth local e lock; ignorados pelo Git
teste_telegram.py        teste original preservado; envia mensagem real se executado
```

Tabelas: `products`, `price_history`, `search_runs`, `offer_evaluations`, `publications`, `simulations`, `system_state` e `recent_errors`. Preços são **centavos inteiros**, datas no banco são UTC e campos não informados são `NULL`. Produtos são únicos por `(source, external_id)`; cada busca gera uma observação do preço e uma avaliação. Publicações confirmadas guardam o ID da mensagem. Termos mais publicados aparecem após pelo menos cinco publicações, para termos repetidos ao menos duas vezes.

O banco não guarda tokens. Logs usam o terminal, com início/fim de busca, contagens, prévias, publicações confirmadas e erros seguros. Respostas brutas de APIs e tracebacks de requests não são registrados. `.env`, `.vscode/`, `.venv/` e dados locais estão ignorados.

## Verificação local

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Testes usam SQLite e cofres temporários, dados sintéticos exclusivamente de teste e transportes simulados. Bloqueiam rede real e carregamento do `.env`. Cobrem autorização, dados ausentes, score, normalização de links, cota diária/fuso, intervalo, repetição, queda de preço, concorrência, recuperação de interrupções e segurança dos logs. Para OAuth, incluem vetor público de PKCE, state, callback inválida/repetida/expirada, corpo do POST, rotação de refresh token, erros HTTP, timeouts, falhas de gravação, entrada oculta e proteção do cofre. No Windows, o teste local de DPAPI usa somente dados sintéticos.

Para o usuário executar somente uma coleta em simulação, depois de ter acesso autorizado à fonte:

```powershell
.\.venv\Scripts\python.exe main.py --dry-run --once
```

Nenhuma publicação automática real nem comando ao bot foi executado durante a implementação. O teste Telegram antigo foi preservado, mas não foi executado nesta etapa.
