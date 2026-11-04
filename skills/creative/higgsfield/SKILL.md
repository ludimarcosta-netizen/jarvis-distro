---
name: higgsfield
description: Gera imagem e vídeo com IA na Higgsfield (pago).
version: 1.0.0
author: __JARVIS_USER__ (Jarvis), Claude Code
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [imagem, video, geracao, higgsfield, criativo]
    related_skills: []
---

# Higgsfield — imagem e vídeo

Serviço pago por crédito. Cada geração gasta créditos da conta do senhor __JARVIS_USER__.

## Comandos

```bash
PY="__JARVIS_HERMES_HOME_FS__/hermes-agent/venv/Scripts/python.exe"
HF="__JARVIS_HERMES_HOME_FS__/skills/creative/higgsfield/hf.py"
"$PY" "$HF" imagem --prompt "..." --proporcao 16:9 --resolucao 2K --quantidade 1
"$PY" "$HF" video --prompt "..." --modelo kling --duracao 5            # texto → vídeo
"$PY" "$HF" video --prompt "..." --imagem "https://..." --modelo kling   # imagem → vídeo
"$PY" "$HF" status <request_id>                                          # retomar um pedido demorado
```

- Proporções: 1:1, 4:3, 3:4, 3:2, 2:3, 5:4, 4:5, 16:9, 9:16 (Reels/Stories), 21:9.
- Vídeo: `kling` (5 ou 10 s, padrão) ou `hailuo` (6 ou 10 s).
- `--imagem` precisa ser URL pública. Arquivo local ainda não é aceito por este script.
- Saída: um caminho de arquivo por linha (padrão `%USERPROFILE%/Pictures/Higgsfield`).
  Os links da Higgsfield expiram em cerca de 7 dias; o arquivo baixado fica.
- Exit 0 = ok; 2 = erro explicado (chave ausente, crédito, filtro de conteúdo, tempo).

## Como agir

- Gere quando o senhor pedir. Se ele não pediu explicitamente (por exemplo, a ideia surgiu de você),
  pergunte antes, porque gasta crédito.
- Escreva o prompt em inglês, detalhado (assunto, estilo, luz, enquadramento). Mantenha a intenção dele.
- Para peças com identidade visual de alguma marca, consulte antes o branding no Segundo Cérebro, se existir.
- Vídeo demora minutos: avise que está gerando. Se passar de 15 min, devolva o `request_id`.
- Entregue os caminhos dos arquivos. Não publique em lugar nenhum sem pedido (use a skill de redes
  sociais/Zernio só quando ele pedir).

## Credencial

`HF_KEY=id:segredo` no `.env` do Hermes (`%LOCALAPPDATA%/hermes/.env`). A chave é criada pelo senhor
em console.higgsfield.ai. Nunca mostre nem registre a chave.
