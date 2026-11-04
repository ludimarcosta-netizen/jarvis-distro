# Jarvis

Assistente pessoal sobre o [Hermes Agent](https://github.com/NousResearch/hermes-agent) (licença MIT, Nous Research): voz, face desktop, reuniões com ata, memória compartilhada e integrações.

## Instalar (Windows 10/11)

```powershell
powershell -ExecutionPolicy Bypass -File .\instalar.ps1
```

1. O script instala o Hermes na versão testada (pasta `%LOCALAPPDATA%\hermes`).
2. Abre o menu do Jarvis: primeiro o seu perfil (nome, cidade, pastas), depois a lista de recursos.
3. Marque o que quiser (`3 5 9`), digite `i` para instalar. Cada recurso instala, pergunta o que precisa e testa.
4. No fim aparece o diagnóstico com o que ficou pendente. `d` no menu mostra de novo.

Dá para parar a qualquer hora: rodar o `instalar.ps1` de novo retoma de onde parou ou liga recursos novos.

## Recursos

| Recurso | Precisa de |
|---|---|
| IA principal (obrigatório) | conta ou chave de um provedor de IA |
| Face desktop | alguns minutos para compilar |
| Voz ao vivo | chave grátis do Google AI Studio; GPU NVIDIA recomendada |
| Reuniões | Voz ao vivo; cadastrar as vozes depois |
| Cerebro | uma pasta para o cofre de notas |
| Telegram | bot criado no @BotFather |
| WhatsApp | Docker Desktop e o celular para ler o QR code |
| Assinatura de PDF | certificado A1 importado no Windows ou token A3 |
| Skills da prefeitura | número do próximo ofício |
| Google | credencial OAuth "App para computador" do Google Cloud |
| Spotify | app em developer.spotify.com e conta Premium |
| Navegador controlado | Google Chrome |
| Antigravity | conta Google (o instalador baixa o `agy`) |
| Home Assistant | endereço e token de longa duração |
| Lembrete diário | Telegram |

## Onde ficam as coisas

- Instalação: `%LOCALAPPDATA%\hermes` (`jarvis\`, `skills\`, `plugins\`, `hermes-agent\`).
- Chaves e tokens: só em `%LOCALAPPDATA%\hermes\.env`, digitados sem aparecer na tela.
- Progresso e log do instalador: `%LOCALAPPDATA%\hermes\jarvis-instalador\`.

## Atualizar o Hermes

O Jarvis aplica alterações no Hermes num ramo local `jarvis`. Por isso `hermes update` fica bloqueado; para atualizar de propósito use `HERMES_ALLOW_UPDATE=1 hermes update` e resolva conflitos se o Hermes mexeu nos mesmos arquivos.

## Para quem mantém

- `export/`: gera este repositório a partir de uma instância real, sem dados pessoais (ver `export/README.md`).
- `installer/`: menu e etapas (Python só com biblioteca padrão). Testes: `python -m unittest discover -s installer/tests`.

## Licenças

Avisos de terceiros em [NOTICE.md](NOTICE.md).
