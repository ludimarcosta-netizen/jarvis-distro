---
name: abrir
description: "Abre na hora um programa instalado no PC (1 comando)."
version: 1.0.0
author: __JARVIS_USER__ (Jarvis), Claude Code
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [abrir, programa, aplicativo, launcher, windows, atalho]
    related_skills: [computer-use]
---

# Abrir programa

Para pedidos de **só abrir** um programa instalado ("abre o Spotify", "abre o fone", "abre o Photoshop"),
rode UM comando e responda. Não procure o programa com PowerShell, não confira processos, não improvise.

```bash
"__JARVIS_HERMES_HOME_FS__/hermes-agent/venv/Scripts/python.exe" "__JARVIS_HERMES_HOME_FS__/skills/productivity/abrir/abrir.py" "<nome como o senhor falou>"
```

- Exit 0: abriu; repita a mensagem impressa ("Abri Spotify.").
- Exit 2 com "Encontrei mais de um": pergunte qual e rode de novo com o nome exato.
- Exit 2 com "Não encontrei": diga isso. Não tente outro jeito sem o senhor pedir.

Atalho falado ("quando eu falar *folha*, abre o Word"):

```bash
"$PY" ".../abrir.py" --ensinar "folha" "Word"
```

Para abrir um **arquivo local direto** (documento `.docx`, `.pdf`, `.xlsx`, etc.):
```bash
cmd.exe /c start "" "C:/caminho/para/arquivo.ext"
```

Só procurar no YouTube, Google, Maps ou Spotify (abre a página de resultados):

```bash
"$PY" ".../abrir.py" --buscar youtube "receita de pão"     # youtube | google | maps | spotify
```

Para buscar **e colocar para tocar** no Spotify (localiza e clica no botão verde ou ativa a reprodução):

- Preferência do usuário: Pedidos genéricos de música ("bota pra tocar", "toca uma música") devem tocar **sertanejo** no **Spotify** (nunca YouTube, a menos que o senhor peça).
- No Windows, abrir a URI do Spotify não inicia o áudio sozinho: é necessário focar a janela do Spotify (`AppActivate('Spotify')` via PowerShell ou `pyautogui`) e enviar `Ctrl+L` -> termo -> `Enter` -> `Space` para acionar a reprodução.

```bash
"$PY" ".../abrir.py" --tocar spotify "sertanejo"
```

Pedido com mais passos (abrir **e** tocar, buscar, escrever, enviar) começa com este comando e só depois
segue com outras ferramentas. Sites não são desta skill.

A voz do Jarvis usa o mesmo módulo direto (função `abrir_programa`), sem passar pelo Hermes.
