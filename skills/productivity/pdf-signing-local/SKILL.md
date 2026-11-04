---
name: pdf-signing-local
description: Use when signing local PDF files (Downloads, Desktop, specific folders). Direct ICP-Brasil signing via Windows store certificates without WhatsApp.
---

# PDF Signing Local

Assinatura digital ICP-Brasil direta para arquivos PDF locais armazenados no computador (Downloads, pastas locais, rede), utilizando certificados do repositório Windows (CurrentUser\MY) via CNG/CryptoAPI.

> **Importante:** Esta skill é para documentos locais e NUNCA aciona o fluxo do WhatsApp nem envia mensagens para terceiros.

## Runtime & Ambiente

- **Python dedicado:** `__JARVIS_HERMES_HOME_BS__\runtime\pdf-signing\Scripts\python.exe`
- **Script:** `__JARVIS_HERMES_HOME_BS__\skills\productivity\pdf-signing-local\scripts\sign_local.py`
- **Certificado Padrão:** Certificado pessoal de `__JARVIS_USER_LOWER__` (__JARVIS_USER_FULLNAME__) ou detectado pelo texto do documento.

## Como Executar

Para assinar qualquer documento local, execute via terminal com um único comando:

```powershell
& "__JARVIS_HERMES_HOME_BS__\runtime\pdf-signing\Scripts\python.exe" "__JARVIS_HERMES_HOME_BS__\skills\productivity\pdf-signing-local\scripts\sign_local.py" "CAMINHO_OU_NOME_DO_ARQUIVO" [--signer __JARVIS_USER_LOWER__] [--output "CAMINHO_SAIDA.pdf"] --json
```

### Exemplos de uso:

1. **Arquivo pelo nome ou caminho na pasta Downloads:**
   ```powershell
   & "__JARVIS_HERMES_HOME_BS__\runtime\pdf-signing\Scripts\python.exe" "__JARVIS_HERMES_HOME_BS__\skills\productivity\pdf-signing-local\scripts\sign_local.py" "Declaração_Ivania.pdf" --json
   ```

2. **Arquivo com caminho absoluto:**
   ```powershell
   & "__JARVIS_HERMES_HOME_BS__\runtime\pdf-signing\Scripts\python.exe" "__JARVIS_HERMES_HOME_BS__\skills\productivity\pdf-signing-local\scripts\sign_local.py" "__JARVIS_DOWNLOADS_BS__\Contrato.pdf" --output "__JARVIS_DOWNLOADS_BS__\Contrato_assinado.pdf" --json
   ```

## Recursos Automáticos

- **Resolução inteligente:** se apenas o nome for passado, busca automaticamente em `__JARVIS_DOWNLOADS_BS__` e `~/Downloads`.
- **Detecção de signatário:** lê o texto do documento e identifica se o signatário é __JARVIS_USER__ ou outro conhecido, posicionando a caixa de assinatura visual automaticamente sobre o campo correto.
- **Validação criptográfica:** assina de forma incremental com carimbo ICP-Brasil e valida a cadeia.

## Pitfalls

- Nunca chamar `pdf_engine.list_signing_certificates()`: para inventariar certificados ICP-Brasil válidos com chave privada no Windows Store (`CurrentUser/My`), importe `from windows_signer import inventory` e filtre por `has_private_key` e `valid_now`. O identificador para os placements deve ser o `fingerprint` (SHA-256).
- Não usar `analyze.py` do fluxo WhatsApp para extrair texto de arquivos locais: use PyMuPDF (`fitz`) diretamente com normalização Unicode (`unicodedata.normalize`) e regex para casar variações do nome do signatário sem acentos.
- Pedidos com menção a caminhos locais (`D:/`, `.pdf`, Downloads) nunca devem ser interceptados por rotas de chat/WhatsApp (`pdf_bridge.py`), que tratam apenas de mensagens recebidas via Evolution API.
