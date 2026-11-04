# Validação — 15/10/2026

Implementação local: `scripts/evolution.py` restringe busca a mensagem/conversa exatas; `workflow.py` mantém SQLite, fingerprints, prévias e autorização humana específica de 15 minutos; `pdf_engine.py` assina incrementalmente e valida todas as assinaturas; `windows_signer.py`/`windows_certificate.ps1` acessam a chave no Windows sem exportação; `receive.py` recebe eventos novos; `analyze.py` usa Gemini do Jarvis sem ferramentas; `main.py` expõe a skill.

## Resultados confirmados

- 24 testes `unittest` passaram com PDFs/certificados sintéticos e API/chamada nativa Windows simuladas. Cobrem duas assinaturas, preservação de anterior, campos existentes, confiança inválida, adulteração, aprovação ausente/expirada/inválida, duplicatas, entrega com citação e timeout sem repetição, eventos novos, ambiguidade, cache de análise, falha de modelo, bloqueio de URLs locais e distinção de indisponibilidade/revogação.
- Ruff lint e formatação passaram. Sintaxe Python passou por AST e os dois scripts PowerShell passaram pelo parser nativo. `compileall` no sandbox falhou ao renomear `.pyc` por restrição de filesystem; importação/execução da suíte fora dessa restrição passou. Python não possui etapa de build nesta skill; não foi introduzida configuração de verificação estática de tipos.
- PNG da prévia e renderização do PDF sintético com duas assinaturas foram inspecionados visualmente: áreas dentro da página, sem sobreposição e sem corte. Artefatos sintéticos em `tmp/pdf-signing-qa/`; nenhuma chave privada foi gravada.
- Evolution local respondeu `WhatsApp: open` fora do sandbox. Leitura de configuração, sem consultar conversas: `readMessages=false` e `readStatus=false`. Nenhuma configuração alterada.
- Certificados correspondentes aos dois aliases encontrados em `CurrentUser/My`: válidos, RSA, `HasPrivateKey=true`; propriedade de provedor indica CSP `Microsoft Enhanced Cryptographic Provider v1.0`. Só metadados e certificados públicos foram lidos.
- Inicialmente, cadeia offline de ambos retornou `RevocationStatusUnknown`, `OfflineRevocation`. A checagem online posterior passou para ambos, sem flags de erro. `refresh-validation` obteve nove downloads CRL públicos, com cinco conteúdos únicos. Com eles, o preflight estrito do pyHanko passou para os dois certificados reais, sem adquirir/usar chaves privadas. O fluxo mantém o bloqueio se os dados expirarem ou não comprovarem validade.

## Ainda não validado / não ativado

- Nenhuma assinatura real, exportação de chave, leitura de senha ou envio real foi realizado. `RSA.SignData` com o CSP real não foi exercitado; eventual incompatibilidade SHA-256 precisa ser tratada sem exportação silenciosa.
- CRLs precisam permanecer atuais; repetir `refresh-validation` quando necessário. Consultas online foram autorizadas nesta etapa e enviaram somente pedidos de cadeia/revogação públicos aos emissores. Nenhuma raiz foi adicionada.
- Download e envio real de PDF pela Evolution e comportamento de citação foram testados apenas por contratos simulados, não por conversa real.
- Monitor/webhook não ativado nem configurado na Evolution. Ainda falta comprovar manutenção de indicador de não lida e notificações no celular; configuração de recibo sozinha não demonstra esse efeito.
- Análise Gemini real, janela humana de aprovação, avisos Windows e avisos de voz não foram exercitados. Aviso de voz não está integrado. A fila gera avisos JSON e suporta aviso Windows genérico opt-in.
- Catálogo nativo `tools.skills_tool.skills_list(category='productivity')` confirmou `pdf-signing discovered: True`. Apareceu aviso preexistente de toolset desconhecido `hermes-homeassistant`, fora do escopo. Não foi alterada configuração global para forçar carregamento.

## Operação e persistência

Não houve alteração da stack Docker/Next/banco do aplicativo. Dependências da skill estão em `runtime/pdf-signing/`, separadas do ambiente Hermes. Estado em `state/pdf-signing/`: SQLite é suficiente para fila local pessoal; PostgreSQL do WhatsApp permanece intacto. Backup com monitor parado: copiar juntos banco e arquivos, preservar permissões e restaurar juntos. Não versionar estado/documentos; a raiz operacional Hermes não é repositório Git. A skill inclui `.gitignore` para cache Python.

Reprodução: Python isolado com `-m unittest discover -s skills/productivity/pdf-signing/tests -v`; Ruff com `check --no-cache` e `format --no-cache --check` sobre a skill. Configuração `monitor.example.json` mantém monitor e avisos desativados, sem modelo selecionado.
