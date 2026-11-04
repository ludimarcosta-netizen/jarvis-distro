---
name: whatsapp-automation
description: "Plano B WhatsApp: NUNCA use direto; só se Evolution falhar."
version: 2.1.1
author: JARVIS (Hermes)
license: MIT
metadata:
  hermes:
    tags: [whatsapp, automation, uia, pywinauto, windows, messaging]
    related_skills: []
---

# WhatsApp Automation (Plano B - Interface Gráfica)

> ⚠️ **REGRA DE ROUTING: ESTA SKILL É UM PLANO B DE EMERGÊNCIA.**
> **NUNCA selecione ou invoque esta skill como primeira opção.**
> Qualquer pedido de leitura, resumo ou envio de WhatsApp DEVE usar a skill `whatsapp-evolution`.
> Você só pode acionar esta skill se executar `wa.py status` da skill `whatsapp-evolution` e o retorno NÃO for `open`.
> Caso precise usar esta skill após falha confirmada da Evolution API, informe explicitamente ao senhor que está utilizando o Plano B por tela.

Envio automático e verificado de mensagens no WhatsApp Web (PWA/Desktop) no Windows 11.

## REGRA FUNDAMENTAL DE EXECUÇÃO
- O script `wa_uia_final.py` está salvo nesta skill. Use o arquivo existente; não o recrie em `cache/scratch/` nem gere código Python inline.
- Se o arquivo ou uma dependência estiver ausente, relate o erro. Não tente reconstruir a automação durante um pedido de envio.
- Após uma falha de confirmação, não repita o envio automaticamente: a mensagem pode já ter sido enviada.
- O script não deve imprimir destinatário ou conteúdo da mensagem. A confirmação exige uma bolha nova com texto exato; prévia da lista não confirma.

## Como Executar

Execute diretamente com o Python 3.12 que possui `pywinauto`:
```bash
"__JARVIS_HERMES_HOME_FS__/hermes-agent/venv/Scripts/python.exe" "__JARVIS_HERMES_HOME_FS__/skills/productivity/whatsapp-automation/wa_uia_final.py" "<contato>" "<mensagem>"
```

### Parâmetros
- `<contato>`: Nome do contato ou grupo cadastrado no WhatsApp.
- `<mensagem>`: Texto exato solicitado pelo usuário.

## Códigos de Retorno
- **Exit code 0**: envio confirmado por uma bolha nova na conversa.
- **Exit code 2**: falha antes do acionamento do envio.
- **Exit code 3**: envio acionado sem confirmação; verifique a conversa e não repita automaticamente.

## Pré-requisitos
1. O aplicativo WhatsApp (ou WhatsApp Web PWA) precisa estar aberto no Windows.
2. A sessão do Windows deve estar desbloqueada (o script não opera se a tela de bloqueio estiver ativa).
