# Radar Tech

Base do bot de ofertas tech com SQLite, score explicável e administração pela conversa privada no Telegram. Modo inicial: **simulação**. Não há afiliados, scraping, cookies de conta ou canal de logs.

**Estado das fontes:** Mercado Livre aguarda investigação/suporte para o HTTP 403 de itens de terceiros. Shopee aguarda aprovação; seu adaptador e o da Amazon são apenas estruturas sem consultas reais. A validação interna atual usa fixtures. **Manter `AUTO_PUBLISH=false`.**

## Validar sem internet com fixtures

```powershell
.\.venv\Scripts\python.exe main.py --dry-run --fixture
```

Executa um ciclo e encerra, sem carregar `.env`, sem usar credenciais, sem importar a integração Mercado Livre e sem construir transporte Telegram. Usa os padrões seguros de `Config`, força `AUTO_PUBLISH=false` mesmo quando o ambiente diz `true`, lê `data/fixtures/offers.json` e grava no SQLite **`data/radartech-fixture.db`**, separado do banco real. O banco e seu lock continuam ignorados pelo Git; o JSON de cenários é versionável.

São 26 registros simulados, 25 IDs únicos `FIXTURE-*` e links `https://example.com/...`: preços bons/comuns, histórico em queda, desconto sem referência, SSD suspeito, categorias tech, produto irrelevante, repetição e campos ausentes. A saída identifica a simulação, normaliza candidatos, salva preços válidos, aplica relevância, score, categorias ativas, ranking e antispam. Preço ausente é rejeitado; item irrelevante pode alimentar histórico, mas não entra no ranking.

A primeira execução carrega observações **sintéticas** anteriores, incluindo R$299 → R$279 → R$249 → R$199. A carga tem uma âncora persistente e não se repete. Novos ciclos acrescentam observações atuais normalmente. Simulações obedecem ao intervalo global de 30 minutos e às demais travas: várias ofertas podem estar aprovadas, mas só uma simulação ser reservada no ciclo. Nenhuma linha é criada em `publications`.

`--fixture` também força segurança sem `--dry-run`. Não combina com `--admin-only`, `--search-now`, `--search-limit` ou `--item-ids`. Este comando não inicia scheduler nem painel, portanto não exige bot configurado.

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
| `/pause` | Bloqueia novas buscas automáticas, publicações e simulações. Ciclo já iniciado pode concluir coleta; envio já em andamento pode concluir. |
| `/resume` | Retoma o modo configurado. Não altera `AUTO_PUBLISH`. |
| `/buscar` | Enfileira consulta e envia resumo privado ao concluir; recusa execução duplicada. |
| `/config` | Mostra somente modo, threshold, limites, repost, intervalo, categorias ativas e fonte atual. Nunca credenciais. |
| `/fixture` | Executa simulação local completa e responde ao admin com resumo. Usa `data/radartech-admin-fixture.db`, separado do scheduler e do banco real; nunca publica no canal. |

O painel usa long polling e exige que este processo permaneça aberto. Com processo desligado, o bot não responde `/status`. Outro processo de polling ou um webhook existente pode causar HTTP 409; o app informa o conflito e não remove webhooks automaticamente.

`/config` e `/fixture` exigem o mesmo admin numérico e conversa privada dos demais comandos. `/status` mostra fonte, Auto publish ON/OFF, pausa, buscas, analisados, aprovados, publicações e erros. No painel, respostas administrativas usam Telegram; os testes desta etapa simulam esse transporte. A CLI `--fixture` não envia nem respostas privadas.

## Scheduler interno

`SEARCH_INTERVAL_MINUTES=30`: um único worker por engine, sem serviço externo. Executa quando o prazo vence e agenda o próximo ciclo a partir do término. O próximo prazo e o estado de pausa sobrevivem ao reinício. `runtime_lock` continua permitindo somente uma instância por banco; comandos concorrentes não abrem duas buscas no mesmo motor.

Com `AUTO_PUBLISH=false`, pode coletar, registrar histórico e simular, sem publicar. Com automação pausada, não inicia buscas automáticas e `/status` não anuncia próximo horário; `/resume` retoma sem mudar o modo seguro. `/buscar` é uma solicitação manual explícita: pode coletar durante pausa, mas a reserva de publicação/simulação permanece bloqueada. `/fixture` usa seu próprio banco de demonstração e permanece exclusivamente simulado.

Para o bot usar dados locais no scheduler, configure manualmente `OFFER_SOURCE=fixture` antes de iniciar o painel habitual. Nesse caso o motor usa `radartech-fixture.db` e força publicação desligada. O exemplo recomenda fixture; instalações existentes sem essa opção preservam `mercado_livre` como fonte. Nenhuma configuração local foi alterada automaticamente.

## Consulta de anúncios em modo seguro

**Consulta por IDs de anúncios implementada; descoberta global por palavra-chave ainda pendente.** A documentação oficial consultada em 08/10/2026 não confirmou uma busca global atual de anúncios de terceiros por `q` para esta aplicação. A tabela de [Itens e buscas](https://developers.mercadolivre.com.br/pt_br/itens-e-buscas) trata de buscas por vendedor e da migração para bulk. Anúncios da própria conta não substituem descoberta do marketplace. Falta confirmar um recurso oficial habilitado e documentado para essa finalidade; ausência de documentação não prova que todo acesso seja impossível. Não foi inventado endpoint substituto nem implementado scraping.

Entrada disponível: **item IDs reais de anúncios MLB**, sem exigir catálogo ou `buy_box_winner`. Não confundir ID do anúncio com ID de catálogo da página `/p/MLB...`. O termo filtra localmente essa lista; não executa busca global. Sem IDs, o comando encerra com erro explicativo e sem consultar a API, em vez de apresentar zero como sucesso.

No **PowerShell normal do VS Code**, com outra instância encerrada, informe até cinco IDs públicos de anúncios relacionados a mouse gamer:

```powershell
$env:MELI_ITEM_IDS = Read-Host 'Informe ate 5 IDs reais de anuncios MLB, separados por virgulas'
.\.venv\Scripts\python.exe main.py --dry-run --search-now --search-limit 5
```

A primeira linha solicita somente IDs públicos, sem alterar `.env`, OAuth ou credenciais. Formato: `MLB` seguido dos dígitos do anúncio, sem hífen. Existe também `--item-ids` para passar a lista separada por vírgulas; tem prioridade sobre `MELI_ITEM_IDS`. Para `/buscar`, a lista precisa estar no ambiente do processo que inicia o bot ou ser configurada manualmente pelo usuário.

O comando executa um ciclo e encerra: **força `AUTO_PUBLISH=false`**, sem polling nem mensagens ao Telegram. Com `--search-limit 5`, consulta até cinco IDs únicos e filtra somente por `mouse gamer`, independentemente de configurações antigas. A opção aceita de 1 a 10 e exige `--search-now`. Sem ela, usa os três termos iniciais e até dez IDs da lista, com cache; não descobre trinta anúncios novos. `--dry-run --once` usa os termos configurados. O lock continua impedindo instâncias simultâneas.

Pipeline:

1. `GET /items/bulk?ids=...&attributes=body...`: detalhes oficiais com seleção de campos. Prioriza `id`, `status_code`, `body`; aceita `body.id` e o `code` legado na migração. ID somente na raiz é suficiente. Sucesso individual exige 200 ou 206 e detalhe válido, independentemente do HTTP global. Envelope incompatível, item ausente no lote ou campos essenciais faltantes permitem um único `GET /items/{item_id}` oficial por item no ciclo, na mesma sessão segura. O fallback não repete GETs nem usa scraping.
2. Validar ID, site MLB, estado ativo, compra imediata, condição nova e permalink correspondente ao item.
3. `GET /items/{item_id}/sale_price?context=channel_marketplace`: preço atual em `amount`, moeda e referência promocional em `regular_amount`. Segue a [API oficial de preços](https://developers.mercadolivre.com.br/devcenter/api-de-precos), sem depender dos preços de `/items`, anunciados para descontinuação. Nunca usa `base_price` como preço anterior.
4. Relevância por termo/título/domínio e palavras negativas. Para mouse, exclui mousepad isolado, peças, adesivos, cabos e resultados sem relação identificável.
5. `GET /products/{catalog_product_id}` somente quando falta marca ou imagem. Catálogo não troca preço, título, URL ou item pelo vencedor. `buy_box_winner=null` não elimina o anúncio. Recurso documentado em [Buscador de produtos](https://developers.mercadolivre.com.br/buscador-de-produtos).
6. SQLite por item, histórico, Radar Score V2, avaliações e ranking.

Com cinco IDs: até **um bulk, cinco fallbacks individuais, cinco preços e cinco enriquecimentos opcionais**, antes dos retries dos endpoints que os permitem. Preço e catálogo só são consultados após validação básica. Cache evita repetir consulta de item, preço ou catálogo no ciclo, inclusive fallback que falhou. Sem paralelismo, paginação, cookies, afiliados ou descoberta adicional. O adaptador antigo de catálogo fica apenas para regressão; não é a fonte do aplicativo.

Dados restritos de terceiros (`sold_quantity`, estoque exato, reputação e estrelas) ficam `NULL` nesta fonte. Contagens públicas referenciais não viram quantidades exatas. Frete só entra quando a API informa booleano. Desconto exige `regular_amount > amount > 0`; referência promocional oficial não é observação do histórico local. Sem preço oficial ou permalink correto, há rejeição com motivo. Link mantém seleção do item quando necessária e remove afiliados/rastreamento. Não inventa URL.

Saída: `RADAR TECH — DRY RUN`, termos, IDs de entrada, itens válidos/relevantes, scores acima do threshold e erros. A origem explica que os IDs foram fornecidos. **TODAS AS OFERTAS ELEGÍVEIS** inclui scores abaixo de 70, preço real, item ID, campos opcionais, aprovação e motivo da decisão. `TOP OFERTAS` mostra até dez. Diagnóstico dos primeiros dez registros: ID, endpoint, HTTP status, nomes/tipos de campos conhecidos e motivo. Nenhum JSON completo ou dado privado.

Incompatibilidade do bulk registra ID esperado, chaves conhecidas na raiz, presença/tipo de `body`, `status_code`, `code` e HTTP global. Chaves livres são representadas por `<outras>` e status inválidos só mostram o tipo, nunca o valor. O diagnóstico distingue fallback, recuperação e rejeição; não inclui tokens, headers ou o JSON recebido.

Cada preço válido é salvo, inclusive se relevância reprovar; só relevantes entram no ranking. Um item gera no máximo uma observação e avaliação no ciclo. `SERIA PUBLICADA` exige threshold, economia observada e antispam liberado. Dry-run grava somente `simulations`, nunca `publications`. Não promete menor preço dos últimos dias.

`/buscar` usa o mesmo motor. Admin recebe confirmação inicial e resumo privado ao concluir; pedidos concorrentes não duplicam execução. Com `AUTO_PUBLISH=false`, nada é publicado no canal. A CLI acima não envia sequer respostas administrativas.

## Transporte, limites e erros da fonte

`get_valid_access_token()` fornece o token e renova quando necessário. Sessão com `trust_env=False`, `verify=True`, timeout 5/20 segundos e redirecionamentos bloqueados. Sem token na URL ou headers de autorização nos logs. Sucesso em `/users/me` não garante acesso a anúncios de terceiros.

`MELI_REQUEST_DELAY_SECONDS=1.0`: pausa mínima entre resposta e próximo GET, configurável de 1 a 60 segundos. `REQUEST_INTERVAL_SECONDS=2` acrescenta pausa entre filtros. Chamadas sempre sequenciais.

Até três tentativas por GET. HTTP 429 respeita `Retry-After` em segundos ou HTTP-date e backoff de 5/10 segundos com jitter positivo de até 25%; usa o maior prazo. Esperas canceláveis. Após a terceira falha, encerra ciclo, preserva resultados anteriores e grava no SQLite cooldown de pelo menos 60 segundos ou prazo maior do servidor. Nova CLI ou `/buscar` respeita esse prazo. Log inclui somente endpoint sem query, tentativa, prazo interpretado e headers públicos numéricos de lista permitida.

401 permite uma renovação forçada por ciclo, dentro do orçamento de GETs; não multiplica retries nem repete POST OAuth automaticamente. A fonte de anúncios tenta novamente após 5xx, até três GETs com espera de 5/10 segundos; o fallback individual tem orçamento estrito de um GET, inclusive em 401/429/5xx. Falha global de acesso ao bulk, autenticação não recuperável, rate limit, serviço indisponível, TLS/timeout ou resposta bulk completamente inválida encerram com erro seguro. Recusas individuais do bulk e 403/404 de detalhe/preço rejeitam somente o item e permitem os demais. 403/404 em enriquecimento mantém oferta; 403 desativa novas consultas de catálogo no ciclo. Erro individual em bulk HTTP 200 usa `status_code` (ou `code` legado); 429 interno interrompe e impõe cooldown, sem loop de reconsulta. JSON/envelope incompatível não vira sucesso silencioso. Falha em `sale_price` nunca é preenchida com preço antigo de `/items`.

Saída `0`: consulta concluída, mesmo sem aprovados. `1`: falha, fonte não configurada ou resultado parcial. 403 pode indicar falta de permissão para terceiros; sem fallback por scraping ou recurso privado. Acesso real a itens/preços precisa de validação manual fora da sessão restrita. **Descoberta global permanece pendente: ETAPA 2 ainda não cumpre integralmente o objetivo de encontrar anúncios automaticamente por termo.**

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

`services.mercado_livre_auth.get_valid_access_token()` devolve o token guardado enquanto válido e renova quando entra na margem de 60 segundos da expiração informada pelo provedor. Não há agendador separado: a renovação acontece na chamada da função. A fonte de catálogo já a chama antes de uma requisição autenticada, sem imprimir o retorno. Há também `force_refresh=True` para tratar revogação antecipada após um 401, com limite de uma renovação forçada por ciclo da fonte.

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

### Diagnóstico isolado de HTTP 403 em anúncios

**Estado atual do checkpoint:** a validação manual confirmou OAuth válido, aplicação e conta ativas, grant correspondente e scopes `read`, `write` e `offline_access`. A consulta de item de terceiro continua retornando **HTTP 403 `access_denied`**. A causa específica ainda não foi comprovada; possível restrição ou política de acesso permanece pendente de investigação/suporte do Mercado Livre. Não alterar parser, score ou pipeline sem nova evidência. **Manter `AUTO_PUBLISH=false` até validação futura; nenhuma oferta real foi publicada.**

No PowerShell normal do VS Code:

```powershell
.\.venv\Scripts\python.exe -m services.mercado_livre_auth diagnose
```

Este comando mantém publicação desativada no diagnóstico (`AUTO_PUBLISH=false`), não inicia o pipeline nem acessa Telegram, SQLite ou preços. Não altera `.env`, grants, permissões ou IPs. Usa a configuração e o cofre OAuth existentes; `get_valid_access_token()` pode renovar tokens antes da coleta, como já faz o helper. Não execute chamadas reais dentro de uma sessão Codex com rede restrita.

Coleta documentada, somente por GET:

1. `/users/me`: HTTP, presença de ID (sem mostrar o número), site e estados oficiais de validação, quando fornecidos.
2. `/applications/{APP_ID}`: atividade, site, sandbox, certificação, bloqueio e scopes, somente quando retornados. Dados privados podem exigir token da conta que criou a aplicação. Campo ausente não vira `false`.
3. `/applications/{APP_ID}/grants`: compara `user_id` e `app_id` internamente e mostra somente o grant correspondente, nomes de scopes e data/estado retornados. Se a primeira página não permitir localizar o grant, tenta uma vez o recurso oficial `/users/{USER_ID}/applications`. Falha de consulta ou página incompleta não prova grant ausente. Exibe presença de `read`, `offline_access` e `write`; falta de `write` não é tratada como impedimento de leitura.
4. `/items/MLB7390921738`: exatamente um GET autenticado, sem projeção de campos. Em erro, preserva `error`, `code`, `message`, `blocked_by`, `path`, `cause`, `request_id` e os headers permitidos `x-request-id`, `retry-after`, `x-content-missing`. Sucesso 200/206 é confirmado sem exibir os dados do anúncio.
5. O mesmo item: exatamente um GET sem credencial, em sessão nova, sem cookies herdados. Resultado é observação de diagnóstico; não muda o acesso em produção nem prova uma regra universal do marketplace.
6. `/users/{USER_ID}/items/search?limit=1`: se houver anúncio próprio, consulta uma vez `/items/{OWN_ITEM_ID}` e compara os HTTPs. Não repete o GET se o item próprio já for o ID testado. Consulta recusada não é confundida com conta sem anúncios.

Sem retries no diagnóstico, com um segundo entre GETs. HTTP 429 suspende as consultas restantes; não há tentativa de contornar o limite. TLS permanece validado, `trust_env=False`, timeout 5/20 segundos e redirecionamentos bloqueados. Segredos conhecidos e padrões de credenciais são ocultados inclusive dentro de mensagens/cause; chaves privadas são descartadas. Texto oficial seguro é preservado, sem paráfrase. Nenhum JSON completo de conta/aplicação/item, header de autorização ou cookie é impresso. O relatório fica apenas na saída do terminal; não é salvo no banco ou em arquivo pelo comando.

A classificação A–G usa evidência explícita. `access_denied` genérico fica **G) INCONCLUSIVO**. Item próprio 200 e terceiro 403, isoladamente, não provam bloqueio geral a terceiros. `PolicyAgent` identifica **F) POLICYAGENT / POLÍTICA DO MERCADO LIVRE**, mas não revela qual política interna negou acesso. Com OAuth válido, aplicação ativa e grant correspondente com `read`, a orientação é suporte oficial com data UTC, endpoint, mensagem/code e request ID seguros, sem novas alterações no pipeline. Saída `0`: detalhe do item autenticado confirmado; `1`: acesso não confirmado ou coleta inconclusiva. Saída `0` não confirma que todos os outros endpoints estejam liberados.

Conferências manuais indicadas no relatório: DevCenter > **Meus aplicativos** > aplicação > **Administrar permissões** para visualizar/exportar grants; edição da aplicação para unidades e permissões; menu da aplicação > **Gerenciar intervalos de IP**, caso essa opção esteja habilitada, somente para consulta. A documentação atual também exige separar aplicações Mercado Livre/Mercado Pago e orienta verificar scopes `urn:mp:`; sua presença é mostrada para investigação, sem atribuir automaticamente a causa do 403.

Referências oficiais: [aplicações e grants](https://developers.mercadolivre.com.br/pt_br/produto-consulta-de-usuarios/gerencie-seu-aplicativo), [conta e estados](https://developers.mercadolivre.com.br/pt_br/consulta-de-usuarios), [causas de HTTP 403](https://developers.mercadolivre.com.br/pt_br/autenticacao-e-autorizacao/erro-403), [gestão de IPs](https://developers.mercadolivre.com.br/pt_br/tendencias/gerenciar-ips-de-um-aplicativo), [busca de itens próprios](https://developers.mercadolivre.com.br/pt_br/itens-e-buscas).

## Configuração e limites

| Variável | Padrão | Uso |
| --- | --- | --- |
| `TELEGRAM_ADMIN_ID` | vazio | Apenas `/whoami` fica liberado até cadastrar o admin. |
| `AUTO_PUBLISH` | `false` | Simula sem enviar ofertas. Publicação real exige também admin configurado. |
| `MIN_OFFER_SCORE` | `70` | Score mínimo, além de evidência de economia. |
| `MAX_POSTS_PER_DAY` | `12` | Limite por dia no fuso configurado. |
| `MIN_MINUTES_BETWEEN_POSTS` | `30` | Intervalo global, inclusive para quedas significativas. |
| `REPOST_AFTER_HOURS` | `72` | Janela sem repetir o mesmo item/fonte. |
| `REPOST_PRICE_DROP_PERCENT` | `10` | Queda de pelo menos 10% frente ao último preço publicado para exceção antes de 72h; mantém intervalo global e cota diária. |
| `SEARCH_INTERVAL_MINUTES` | `30` | Intervalo depois do fim de cada ciclo automático. |
| `OFFER_SOURCE` | `mercado_livre`; exemplo: `fixture` | Seleção de fonte. `shopee` e `amazon` falham com pendência explícita, sem rede. |
| `ACTIVE_CATEGORIES` | todas | Categorias locais separadas por vírgulas; desativadas não entram no ranking. |
| `SEARCH_TERMS` | lista tech do exemplo | Termos separados por vírgulas. |
| `SEARCH_TERMS_PER_CYCLE` | `3` | Rodízio persistente dos termos; evita consultar tudo de uma vez. |
| `SEARCH_LIMIT_PER_TERM` | `10` | Máximo de IDs configurados por ciclo; valores maiores são limitados a 10. |
| `REQUEST_INTERVAL_SECONDS` | `2` | Pausa adicional entre termos. |
| `MELI_REQUEST_DELAY_SECONDS` | `1.0` | Pausa entre a resposta e o próximo GET do Mercado Livre; de 1 a 60 segundos. |
| `MELI_ITEM_IDS` | vazio | IDs públicos de anúncios MLB separados por vírgulas; sem descoberta global. |
| `NEGATIVE_KEYWORDS` | lista do exemplo | Expressões negativas para filtro local. |
| `HISTORY_DAYS` | `30` | Janela usada na comparação local de preços. |
| `KNOWN_BRANDS` | lista do exemplo | Marcas reconhecidas, separadas por vírgulas; bônus pequeno de 5 pontos. |
| `POST_TIMEZONE` | `America/Sao_Paulo` | Virada da cota diária e horários no painel. `tzdata` fornece o fuso no Windows. |
| `MELI_CLIENT_ID` | placeholder no exemplo | Identificador da aplicação OAuth no DevCenter. |
| `MELI_CLIENT_SECRET` | placeholder no exemplo | Segredo da aplicação; usado somente no corpo do POST OAuth. |
| `MELI_REDIRECT_URI` | placeholder no exemplo | URL HTTPS fixa registrada no DevCenter. |
| `MELI_ACCESS_TOKEN` / `MELI_REFRESH_TOKEN` | vazios | Reservados no exemplo; este fluxo usa exclusivamente o cofre local. |

O ciclo publica ou simula as melhores ofertas elegíveis primeiro. As restantes ficam registradas nas avaliações; não existe fila de envio de preços antigos. São reavaliadas caso apareçam em uma busca posterior. A ordem dos termos continua após reiniciar.

Os nomes legados `REPUBLISH_AFTER_HOURS` e `SIGNIFICANT_PRICE_DROP_PERCENT` continuam aceitos. Quando ambos os nomes estiverem definidos, `REPOST_AFTER_HOURS` e `REPOST_PRICE_DROP_PERCENT` têm prioridade. Categorias disponíveis: `mouse`, `teclado`, `headset`, `ssd`, `ram`, `monitor`, `notebook`, `gpu`, `cpu`, `microfone`, `webcam`, `controle`, `power_bank`, `hub`, `carregador`, `setup`, `other`. Ícones e classificação são determinísticos, sem IA externa.

Pausas, histórico, publicações e limites sobrevivem a reinicializações. Simulações têm histórico separado: obedecem aos limites para mostrar um comportamento realista, mas não consomem as cotas de envio real nem aparecem em `/ultimas` ou na média de publicações. Uma mesma oferta não gera repetidamente uma prévia a cada busca.

Publicações passam por uma reserva em transação SQLite antes do envio. Timeout ou resposta sem confirmação pode significar que a mensagem já chegou ao canal: nesse caso, o app não repete o POST, marca a tentativa como `unknown` e pausa novas publicações. Confira o canal antes de `/resume`; aquele produto continua bloqueado até revisão manual do registro no banco. Uma interrupção entre envio e gravação é tratada da mesma forma no próximo início. Cotas consideram envios pendentes/sem confirmação para evitar excesso. Esta proteção prefere não reenviar a prometer entrega “exatamente uma vez”, que a Bot API não oferece neste fluxo.

## Como o score funciona

Score de 0 a 100: **soma de pontos**, sem reescalar pelos campos disponíveis. Ausência de dados não aumenta artificialmente o score.

| Faixa | Classificação |
| --- | --- |
| 0–39 | Fraco |
| 40–59 | Regular |
| 60–69 | Interessante |
| 70–84 | Boa oferta |
| 85–100 | Oferta excelente |

Os pesos do Score V2 foram preservados e calibrados por cenários de teste: desconto e histórico concentram até 60 pontos; relevância é também condição de aprovação. Bons preços com evidência superam produtos comuns. Marca sozinha soma 5, SSD suspeito recebe penalidade, e desconto anunciado sem `original_price` não vira economia calculada. A faixa aparece junto do score na mensagem e no ranking.

| Critério | Máximo | Pontos |
| --- | --- | --- |
| Desconto | 35 | >=40%: 35; >=30%: 30; >=20%: 22; >=10%: 12; abaixo disso: 0. |
| Histórico | 25 | Exige 3 observações anteriores do mesmo item. Queda contra menor valor: >=20%: 25; >=10%: 20; >=5%: 10. Preço atual fora da referência. |
| Frete | 10 | `shipping.free_shipping=true`: 10; false: 0. |
| Reputação | 10 | Apenas dado confiável: GREEN soma 10. Fonte atual de terceiros deixa desconhecido. |
| Vendidos | 10 | Apenas contagem confiável: >=1.000: 10; >=500: 8; >=100: 6; >=50: 4; >=10: 2. Fonte atual de terceiros deixa desconhecido. |
| Marca | 5 | Marca informada nos atributos e em `KNOWN_BRANDS`: 5; desconhecida: 0. |

Desconto usa exclusivamente `(original_price - price) / original_price * 100`, com duas casas decimais, quando ambos existem e o original é maior. Campos ausentes não são usados. Avaliação não pontua porque a fonte não fornece uma avaliação confiável do produto. **Sem desconto positivo ou queda positiva no histórico, não há aprovação**, mesmo alterando o threshold. Marca sozinha soma apenas 5 e não aprova. Relevância soma até 10; irrelevância impede aprovação. Título suspeito perde até 20; publicação recente perde 10; preço abaixo de 10% da referência histórica suficiente perde 25. Resultado limitado entre 0 e 100. Antispam continua como trava adicional. A fórmula é heurística inicial; score não promete menor preço de mercado.

Na fonte atual de terceiros, sem histórico suficiente ou dados confiáveis de reputação/vendas, máximo 60 (35 desconto + 10 frete + 5 marca + 10 relevância). Threshold 70 exige mais evidência; não foi reduzido automaticamente. É normal haver poucos ou nenhum aprovado com `MIN_OFFER_SCORE=70`; o relatório mantém visíveis todos os scores para avaliação. Não ajusta o threshold nem inventa dados para produzir aprovados.

A frase **“Menor preço observado pelo Radar nos últimos X dias do histórico local”** exige X ≥ 3, ao menos três observações anteriores e cobertura de cada dia UTC anterior dentro da janela configurada, sem lacunas. O preço atual precisa ser menor ou igual a todos os preços anteriores na janela. Observações futuras, de outro item/fonte ou do próprio instante atual não contam. Muitas coletas no mesmo dia não comprovam cobertura diária. Isso descreve apenas observações locais, nunca o mínimo de todo o mercado. Três preços anteriores permitem calcular queda, mas não comprovam uma janela de 30 dias.

Postagens usam texto simples (sem `parse_mode` HTML/Markdown), título limitado, ícone da categoria, preço, desconto calculado quando válido, faixa do score e frete grátis somente quando confirmado. Campos ausentes são omitidos. Nenhuma alegação de custo-benefício, avaliação ou vendas é inventada. Mensagens de fixture identificam explicitamente dados fictícios.

## Arquitetura e dados

```text
OfferSource → OfferBatch[OfferCandidate] → normalização/relevância
           → deduplicação por fonte/item → SQLite/histórico → score/threshold
           → ranking → reserva com antispam → formatter → OfferPublisher
```

`OfferSource` define `begin_cycle()` e `search(term, limit)`. `OfferBatch` mantém candidatos, diagnósticos, erros e resultados parciais. Preços de candidatos são centavos inteiros; ausências permanecem `None`. `Product` representa dados já normalizados. O engine não importa módulos de Mercado Livre e aceita fontes novas pelo mesmo contrato. `MercadoLivreAdapter` apenas traduz o envelope legado: transporte, OAuth, bulk, fallback, sale_price e tratamento do 403 permanecem intactos. `FixtureSource` é funcional; `ShopeeSource` e `AmazonSource` são estruturas sem integração real. `OfflinePublisher` não possui cliente HTTP e recusa qualquer envio; o banco também impede reservar publicação real de fixture.

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
services/mercado_livre.py consulta por IDs, cache, transporte e rate limit
services/mercado_livre_items.py anúncio, permalink e preço oficial
services/offer_relevance.py relevância local e palavras negativas
services/mercado_livre_auth.py OAuth PKCE, callback manual e renovação de tokens
services/mercado_livre_http.py sessão HTTP própria, sem proxy herdado e com TLS validado
services/meli_oauth_store.py cofre local protegido e gravação atômica
services/offer_filter.py  cálculo do score
services/offer_formatter.py mensagem final
services/offer_source.py  protocolo, lote neutro e ponte para fonte existente
services/offer_normalizer.py candidatos genéricos para produtos válidos
services/categories.py   categorias e ícones locais
services/fixture_source.py fonte local sem credenciais
services/fixture_runner.py simulação isolada e histórico sintético idempotente
services/publisher.py     protocolo de saída e transporte offline
data/fixtures/offers.json cenários inteiramente simulados e versionáveis
tests/test_fixture.py     fonte, CLI, arquitetura, segurança e admin
tests/test_offer_core.py  faixas, calibração, histórico, antispam, formatter e scheduler
tests/test_mvp.py         testes sem rede nem leitura de .env
tests/test_mercado_livre_auth.py testes OAuth, erros, rotação e cofre com dados sintéticos
tests/test_mercado_livre_http.py testes de isolamento de proxy e validação TLS, sem rede
tests/test_mercado_livre_catalog.py regressão do adaptador anterior
tests/test_mercado_livre_rate_limit.py retry, pausas, cooldown e cancelamento
tests/test_mercado_livre_items.py anúncios, preços, score V2, cache, SQLite, CLI e painel
data/radartech.db         banco local, criado no primeiro uso e ignorado pelo Git
data/meli_oauth/          cofre OAuth local e lock; ignorados pelo Git
teste_telegram.py        teste original preservado; envia mensagem real se executado
```

Tabelas: `products`, `price_history`, `search_runs`, `offer_evaluations`, `publications`, `simulations`, `system_state` e `recent_errors`. Preços são **centavos inteiros**, datas no banco são UTC e campos não informados são `NULL`. A migração aditiva para schema 3 acrescenta identidade separada de item/catálogo, categoria oficial e origem do preço, além das contagens de busca, preservando dados anteriores. Acontece quando o usuário iniciar o programa; a implementação não abre o banco real.

Ofertas novas usam `external_id=item:<item_id>` e unicidade por item/fonte. `catalog_product_id` é separado e pode aparecer em vários anúncios. O `product_id` conceitual legado permanece nos registros antigos. Histórico antigo é preservado, sem transferir preços ao último vencedor: não sabemos qual item produziu cada preço anterior. Item novo começa histórico próprio. Duplicados no ciclo não criam observações extras; coleta em outro instante registra preço mesmo sem mudança. Simulações e publicações continuam separadas, com regra de 72 horas e exceção por queda significativa. Termos mais publicados exigem ao menos cinco publicações reais e duas por termo.

O antispam também consulta registros legados que identificam o mesmo item. Publicação recente ou envio sem confirmação continua bloqueando a identidade nova. Preço legado de catálogo não abre exceção por queda, pois o vencedor anterior pode ter sido outro anúncio.

O banco não guarda tokens. Logs usam o terminal, com início/fim de busca, contagens, prévias, publicações confirmadas e erros seguros. Respostas brutas de APIs e tracebacks de requests não são registrados. `.env`, `.vscode/`, `.venv/` e dados locais estão ignorados.

## Verificação local

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Testes usam SQLite e cofres temporários, dados sintéticos exclusivamente de teste e transportes simulados. Bloqueiam rede real e carregamento do `.env`. Cobrem autorização, dados ausentes, score, normalização de links, cota diária/fuso, intervalo, repetição, queda de preço, concorrência, recuperação de interrupções e segurança dos logs. Para OAuth, incluem vetor público de PKCE, state, callback inválida/repetida/expirada, corpo do POST, rotação de refresh token, erros HTTP, timeouts, falhas de gravação, entrada oculta e proteção do cofre. No Windows, o teste local de DPAPI usa somente dados sintéticos.

Os testes de rate limit simulam HTTP 429, `Retry-After` numérico e HTTP-date, backoff com jitter, cancelamento, teto de tentativas, deduplicação de detalhes, pausa configurável, preservação de resultados parciais e ausência de envio ao Telegram. Não usam esperas de rede reais.

Após fornecer IDs públicos em `MELI_ITEM_IDS`, primeira coleta: filtro `mouse gamer`, até 5 anúncios:

```powershell
.\.venv\Scripts\python.exe main.py --dry-run --search-now --search-limit 5
```

Nenhuma publicação automática real nem comando ao bot foi executado durante a implementação. O teste Telegram antigo foi preservado, mas não foi executado nesta etapa.
