# Mapeamento de sistema web (SaaS público/prefeitura) via CDP

Use quando o pedido for "analise/mapeie o módulo X do sistema Y" numa aba do
`rea-chrome` (CDP `http://127.0.0.1:9333`) e o entregável for um relatório `.md`.

## Ordem de trabalho

1. `list_browser_targets` (filtro `allowed_origins`) e anotar o `target_id`.
2. **Conferir o login antes de qualquer análise profunda.** Sinais de que a aba
   não está autenticada: botões "Entrar"/"Entrar com certificado" visíveis,
   `jwt: null` nas respostas do gateway, 401 nas rotas de inbox ou listagem.
   Sem sessão, só a camada pública fica visível. Avise o senhor logo no início
   e pergunte se ele quer entrar (como cidadão e/ou servidor) antes de seguir.
   Não descubra isso só no fim.
3. `inspect_web_page` (acessibilidade, estrutura de JSON, chaves de storage) e
   depois `analyze_web_bundle` (scripts e source maps). A saída grande vai para
   o spillover: faça o parse com `execute_code`, sem repetir a chamada.
4. **Estático do JS legado:** baixe os scripts servidos com `curl --max-time 20`
   para `…\cache\scratch\<sistema>_js`. Rode `analyze_javascript_application`
   (`input_path` obrigatório, `format: "directory"`, `detail: "summary"`).
   Se a leitura em `/mnt/c/...` falhar com ENOMEM/io num arquivo grande, copie
   a pasta para o home do WSL (`wsl -d Ubuntu -- bash -lc 'cp -r /mnt/c/.../x ~/rea_x'`),
   passe `/home/<user>/rea_x` com `max_heap_mb: 2048` e apague a cópia no fim.
   Antes de dar o JS ao REA, redija credenciais fixas embutidas (ex.:
   `Authorization: "<hex>"`) nas cópias locais. Em seguida extraia por regex o
   inventário RPC: `chamada=<acao>`, `*.php`, `pg=<rota>`. Em PHP/jQuery sem
   módulos, o grafo do REA fica raso e a regex rende mais.
5. **Navegação somente leitura com captura de estrutura:** use o
   `scripts/cdp_readonly_probe.js` (Node 22+, WebSocket nativo; sem `npm
   install`). Ele navega só dentro da origem permitida, roda uma expressão de
   leitura do DOM e devolve as respostas XHR/Fetch apenas como estrutura
   (chaves e tipos, sem valores). Abrir abas e modais com GET é permitido.
   Submeter formulários, fazer login ou gravar qualquer coisa não é. Ao
   terminar, volte a aba para a URL original.
   Em paralelo, rode um `observe_web_session` passivo para ter Evidence REA da
   mesma janela de tempo. Dispare-o **antes** ou junto com o probe (mesmo
   bloco de chamadas): se começar depois, registra só a navegação final
   (`attach_limited`).
   **Telas de formulário/assistente com variantes** (assunto, tipo, `passo`):
   varra cada variante por URL GET com o probe e monte uma tabela
   variante × campos visíveis × obrigatórios × anexos exigidos. Formulários
   dinâmicos têm centenas de elementos ocultos; conte só os visíveis.
   Navegar a aba descarta o que estiver digitado e não enviado: pergunte ao
   senhor antes da varredura se a aba tem preenchimento em andamento, e
   mencione o risco no resumo final.
6. `get_evidence_bundle` (`detail: "summary"`) para listar os `ev_…` e citá-los.
   No fim, chame `close_binary`.

Armadilhas:
- Rode o Node do probe em primeiro plano com o caminho Windows explícito
  (`cd C:/Users/.../scratch && node probe.js ...`). Em segundo plano, a saída
  pode chegar vazia (`stdin is not a tty`).
- SPA moderna com gateway separado (ex.: `app-gateway.*`): capture o host
  inteiro, não só a origem da página. As chamadas REST com JWT ficam ali.
- `inspect_web_page` numa tela de formulário grande devolve 1 MB ou mais no
  spillover; extraia o necessário e apague o arquivo ao terminar, pois pode
  conter dados pessoais.
- Credencial fixa no JS público (cabeçalho igual para todo visitante) é
  achado de segurança: cite o arquivo, redija o valor como [REDACTED] e
  pergunte ao fornecedor se o endpoint exige sessão.
- Redija na URL os parâmetros com cara de token, hash, código, CPF ou e-mail
  antes de imprimir. IDs de serviço viram `X` no relatório.

## Formato do relatório (preferência do senhor __JARVIS_USER__)

Legenda **[O]** observado · **[I]** inferência (do código) · **[D]** desconhecido
(regra do servidor), com `ev_…` ou rótulos `CDP-Ln` em cada afirmação.
Seções, nesta ordem:

1. Visão geral
2. Mapa de telas e rotas (árvore em bloco de código)
3. Fichas de funcionalidade (quem usa, campos, regras, status acionado ou não)
4. Ciclo de vida em Mermaid `stateDiagram-v2`, com o papel em cada transição
5. Catálogo de API (tabela: endpoint | método | parâmetros | estrutura | auth | evidência)
6. Integrações externas
7. Segurança/LGPD (rastreadores antes do consentimento, enriquecimento de
   dados, IDOR em RPC `chamada=`)
8. Oportunidades e perguntas ao fornecedor

Para análise de **uma tela específica** (ex.: passo de assistente), use a
variante enxuta: 1. finalidade e papel no fluxo; 2. tabela de parâmetros da
URL; 3. campos/controles/status (com tabela de variantes); 4. passo anterior e
próximo (Mermaid `flowchart`); 5. API observada + endpoints só no código;
6. validações JS (regra | onde | efeito); 7. segurança/LGPD/limitações.

Apêndices: A, Evidence IDs; B, lacunas (respondida/parcial/não respondida) e o
que falta para completar.

Na conversa, entregue um resumo de cerca de 10 linhas mais o caminho do
arquivo. Se a cobertura ficou limitada, diga logo na primeira linha por quê
(ex.: sem login).
