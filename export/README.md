# Exportação

Gera o conteúdo do repositório a partir de uma instância pessoal do Jarvis, sem dados pessoais.

```powershell
python export\exportar.py --verificar   # só lista o que barraria
python export\exportar.py               # grava (recria só as pastas de "geradas" do manifesto)
python -m pytest export\tests
```

Requer Python 3.11+, sem dependências externas.

## Arquivos

| Arquivo | Onde fica | Conteúdo |
|---|---|---|
| `export/manifesto.toml` | repositório | o que copiar (lista de permitidos), exclusões, padrões de segredo |
| `exportacao_regras.toml` | **fora do repositório**, na instância de origem (`%LOCALAPPDATA%\hermes\jarvis\`) | valores pessoais → marcadores, padrões proibidos, catálogo de pessoas |

As regras ficam fora porque contêm justamente os dados que não podem ir para o repositório.

## Como protege

1. Só entra arquivo listado no manifesto; `.env`, tokens, bancos, áudios, vozes e caches ficam de fora mesmo se casarem.
2. Valores pessoais viram marcadores `__JARVIS_*__`; pessoas do catálogo do Cerebro viram nomes fictícios estáveis.
3. Depois das trocas, qualquer sobra pessoal (nome, cidade, caminho, telefone, CPF, e-mail, pessoa do catálogo) ou segredo **barra a exportação** com `arquivo:linha`.

Limite: pessoa que não está no catálogo do Cerebro não é reconhecida. Revisar o diff antes de cada commit.

## Ajustes de código

`export/ajustes.py` troca trechos que não basta parametrizar (ex.: o filtro de hotwords supunha cidade "X do Céu"). O trecho de origem já está com marcadores, então o arquivo não tem dado pessoal. Se o original mudar e o trecho sumir, a exportação barra.

`cortar_em` num bloco `[[copiar]]` leva só o texto antes do marcador (o `SOUL.md` vai para `templates/` sem o bloco do Cerebro).

## Marcadores que o instalador preenche

| Marcador | Valor |
|---|---|
| `__JARVIS_HERMES_HOME_FS__` / `_BS__` / `_BS2__` | pasta do Hermes (HERMES_HOME) |
| `__JARVIS_USER__` / `_LOWER__` / `_UPPER__` | primeiro nome do usuário (normal, minúsculo, maiúsculo) |
| `__JARVIS_USER_FULLNAME__` / `_FULLNAME_LOWER__` | nome completo (normal, minúsculo) |
| `__JARVIS_USERPROFILE_FS__` / `_BS__` / `_BS2__` | pasta do usuário com `/`, `\` ou `\\` |
| `__JARVIS_CEREBRO_FS__` / `_BS__` / `_BS2__` | pasta do cofre Cerebro |
| `__JARVIS_DOWNLOADS_FS__` / `_BS__` / `_BS2__` | pasta de downloads preferida |
| `__JARVIS_CIDADE__`, `_ASCII__`, `_FOLD__`, `_UPPER__` | cidade/órgão (com acento, sem acento, sem acento minúsculo, maiúsculo) |
| `__JARVIS_CIDADE_CURTA__`, `_CURTA_FOLD__` | nome curto da cidade |
| `__JARVIS_CIDADE_URL__` | cidade codificada para URL (`%20`, `%C3%A3`…) |
| `__JARVIS_USER_REGEX__`, `__JARVIS_CIDADE_REGEX__`, `__JARVIS_CIDADE_CURTA_REGEX__` | nome/cidade **como expressão regular** (usados em `transcript_hygiene.py` e `session_harvest.py`): o instalador gera um padrão que aceita com e sem acento, escapado para regex |
| `__JARVIS_ORG_DOMAIN__` | domínio de e-mail do órgão |
| `__JARVIS_GITHUB_USER__` | usuário do GitHub |

`__JARVIS_TOKEN__` **não** é marcador de instalação: é preenchido em tempo de execução por `jarvis/web_app.py`. O instalador não deve tocá-lo.

O campo de tarefas do dono nas atas é renomeado para `tasks_owner` na exportação (sem marcador).
