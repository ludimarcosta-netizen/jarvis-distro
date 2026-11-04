---
name: zernio
description: "Publica e agenda posts em redes sociais via Zernio."
version: 1.0.0
author: __JARVIS_USER__ (Jarvis), Claude Code
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [redes-sociais, instagram, linkedin, tiktok, zernio, postagem]
    related_skills: []
---

# Zernio — redes sociais por API

Substitui o MCP `zernio` (OAuth quebrado: issuer com e sem barra). Chave `ZERNIO_KEY` no `.env` do Hermes.

```bash
PY="__JARVIS_HERMES_HOME_FS__/hermes-agent/venv/Scripts/python.exe"
ZR="__JARVIS_HERMES_HOME_FS__/skills/social-media/zernio/zr.py"
"$PY" "$ZR" contas
"$PY" "$ZR" post --texto "..." --conta ID --agendar 2026-10-06T09:00
"$PY" "$ZR" post --texto "..." --conta ID --rascunho
"$PY" "$ZR" status POST_ID
```

## Regras
- Publicar de verdade (sem `--agendar`/`--rascunho`) só com pedido explícito do senhor __JARVIS_USER__. Na dúvida, use `--rascunho`.
- Mostre o texto final e as contas antes de publicar/agendar.
