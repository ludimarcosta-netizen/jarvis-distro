# Identificação local de vozes — Jarvis

## Ativação

Reabra o Hermes Desktop para carregar as alterações do plugin e do backend. Se a
escuta ambiente já estiver rodando, reinicie também o processo do Jarvis pelo
controle que você já usa. Não é necessário reiniciar o Windows.

1. Abra **Vozes**, ao lado de **Ativar voz**.
2. Selecione **Permitir confirmação por voz validada** e clique **Gravar nova voz**.
   Para um perfil antigo, abra seu nome e use **Refazer cadastro desta voz**.
   Fale sozinho por 30–60 segundos, em frases completas. O sistema precisa reunir
   pelo menos 20 segundos úteis em quatro ou mais trechos consistentes, dentro de
   90 segundos. Ruído, silêncio e trechos curtos não contam.
   Pausas naturais são preservadas no processamento: frases curtas da mesma pessoa
   são reunidas dentro de cada bloco antes de extrair sua identificação vocal.
   Blocos com vozes misturadas não são aceitos. O painel informa quando um bloco
   não contém fala suficiente. Se o dispositivo parar, são feitas até duas
   reconexões, preservando os segundos já aceitos dentro do prazo do cadastro.
3. Ouça a gravação reunida, informe seu nome, marque **Esta é minha voz** e o consentimento.
4. Para outras pessoas, cadastre o nome sem marcar proprietário. Amostras com
   vozes misturadas devem ser descartadas.
5. Depois de cadastrar sua voz, fale novamente em dois trechos Live distintos para liberar a confirmação vocal
   naquela sessão. Enquanto isso, a confirmação é pela tela.

O cadastro não é automático. O painel oferece revisão a partir das 18h, no horário
local, quando há amostras pendentes. Não há tarefa agendada nem notificação quando
o aplicativo está fechado. **Não registrar** exclui a amostra; desconhecidos também
expiram em 24h. A limpeza ocorre enquanto o serviço está rodando ou no próximo acesso.
O limite é de 30 vozes temporárias. Prévias automáticas têm até 10 segundos e não
podem virar cadastros permanentes; o cadastro guiado reúne 20 segundos ou mais.
Os perfis antigos permanecem visíveis como estimativas, mas não autorizam pedidos.
Vozes futuras podem gerar outra amostra após uma exclusão.

## Fluxo e desempenho

Na conversa normal, o áudio segue diretamente ao Gemini. Durante o cadastro guiado,
o backend suspende o envio dos trechos do cadastro ao Gemini. Uma cópia entra em fila limitada de um
processo Python 3.11 separado: segmentação pyannote ONNX, embeddings WeSpeaker,
agrupamento e comparação local via sherpa-onnx. São usadas duas threads CPU por
modelo; a GPU continua disponível para a transcrição ambiente existente.

O Live usa blocos de aproximadamente 5 segundos. Portanto, a identificação é uma
estimativa atrasada, não uma etiqueta perfeitamente sincronizada com cada palavra.
O teste com áudio público de 12 segundos levou aproximadamente 1 segundo de
inferência nesta máquina. Isso não mede o atraso final da conversa. A confirmação
vocal acrescenta o tempo da frase, do bloco e da transcrição local; a conversa
normal não espera pela inferência. Sob carga, cópias de áudio podem ser descartadas.

Não há nova API paga de reconhecimento nem envio de perfis vocais para terceiros.
O Gemini continua recebendo o áudio como antes e pode receber o nome estimado do
locutor. Custos e funcionamento do Gemini existente permanecem os mesmos.

## Confirmações e limites

- Em qualquer modo, **todo pedido delegado ao Hermes** exige aprovação. Uma frase
  aleatória, vinculada ao pedido e à sessão, vence em 90 segundos e vale uma vez.
- A frase precisa ser reconhecida no trecho da voz cadastrada como proprietário.
  Áudio antigo, de outra sessão ou durante a reprodução do assistente é rejeitado.
  Outras vozes podem conversar, mas não aprovar ações. Sem cadastro válido ou nos
  modos Observar/Desligado, é necessária aprovação na tela da sessão Windows.
- Pedidos com termos explícitos de exclusão, pagamento, senhas, publicação ou
  execução exigem clique na tela. Essa regra por palavras não identifica todas as
  ações perigosas. As aprovações nativas de ferramentas do Hermes continuam sendo
  necessárias; esta integração confirma o pedido inicial, não cada ferramenta.
- Reconhecimento de locutor **não oferece proteção contra clonagem ou replay
  direcionado**. Limiares 0,72 (identidade), 0,80 (confirmação) e margem 0,12 são
  iniciais, ainda sem calibração com sua voz e seu ambiente. Ruído e falas sobrepostas
  podem causar rejeições e erros. Use a confirmação na tela quando houver dúvida.
- Depois da confirmação do proprietário, o resultado da delegação volta ao Gemini
  para que ele responda por voz; a execução também continua visível no Hermes. Retomada de conversas Gemini anteriores foi desabilitada,
  pois elas podem conter dados compartilhados antes da correção. Isso não remove
  informações da conversa que já estava aberta: encerre-a e abra uma nova. Tampouco
  controla respostas que o Gemini produzir usando contexto já recebido.
- Desativar reconhecimento NÃO desativa o bloqueio de delegação.
  Quem controla sua sessão Windows e a interface pode alterar os cadastros/modo.

## Arquivos e operação

- `service.py`, `engine.py`, `store.py`, `confirmation.py`: serviço, inferência,
  persistência e regras de confirmação.
- `client.py`, `bridge.py`: integração sem dependências ML no processo Hermes.
- `../plugins/gemini-live-bridge/dashboard/plugin_api.py`: cópia do áudio, rotas e
  confirmação antes de delegar.
- `../desktop-plugins/gemini-live-bridge/plugin.js`: painel Vozes.
- `../jarvis/ambient_engine.py`: cópia apenas do microfone, nunca loopback.

O serviço escuta somente `127.0.0.1:17839`, com token local criptografado e rejeição
de Origin. O painel usa a autenticação já existente do dashboard. O serviço inicia
por demanda, oculto, e permanece ativo para expirar amostras. **Desativado** para
a análise de novos áudios; para encerrar o processo, identifique o PID exato de
`voice_identity/service.py` no Gerenciador de Tarefas e encerre somente esse processo.

Dependências isoladas em `.venv`; versões em `requirements.txt`. Modelos em
`models/`, origem e hashes em `models/manifest.json`. `download_models.py` baixa
pesos públicos oficiais do sherpa-onnx. A confirmação usa faster-whisper **base**
já presente no cache local Hugging Face, sem download automático em execução.
Em outra instalação, esse modelo também precisa estar disponível localmente.

## Persistência, backup e reversão

SQLite é adequado para produção **local de um usuário** neste caso: baixa
concorrência, sem servidor extra. Banco separado: `data/voices.sqlite3`, schema v1.
Nomes, embeddings e amostras são criptografados com DPAPI do Windows. Os registros
permanentes mantêm embeddings; o áudio temporário é excluído ao cadastrar.

Execute a partir da pasta Hermes, com seu usuário Windows:

```powershell
& voice_identity\.venv\Scripts\python.exe voice_identity\backup.py C:\CAMINHO\backup-vozes.sqlite3
```

Escolha um arquivo novo em uma pasta existente. O backup usa a API consistente do
SQLite e remove amostras temporárias. Para restaurar, feche o Hermes e o serviço
local, preserve o banco atual e copie o backup para `data/voices.sqlite3`.
DPAPI vincula os dados ao usuário/Windows: em outra máquina ou conta, recadastre as
vozes. Não presuma que copiar o banco permite descriptografá-lo. Apagar um perfil
não apaga backups antigos; aplique sua política de retenção aos backups.

Para reverter o código, primeiro selecione **Desativado**, feche Hermes/Jarvis e
encerre somente o helper. Restaure os três arquivos originais guardados em
`../backups/voice-identity-20260927/`: `plugin_api.py`, `plugin.js` e
`ambient_engine.py`, nos destinos listados acima. Reabra o aplicativo. Não apague
os dados do Jarvis nem o banco de transcrições existente.

## Verificação

```powershell
& voice_identity\.venv\Scripts\python.exe -m pytest voice_identity\tests -q
node --test desktop-plugins/gemini-live-bridge/lifecycle.test.mjs
& jarvis\.venv311\Scripts\python.exe -m unittest discover -s jarvis\tests -v
& voice_identity\.venv\Scripts\python.exe voice_identity\verify_service.py
```

Os testes DPAPI exigem a sessão normal do usuário Windows. `verify_service.py`
inicia o helper e verifica modelos/autenticação; não liga o microfone. Testes de
reconhecimento usam um áudio público, nunca um cadastro seu simulado. A validação
final com sua voz, outras pessoas e o microfone real ainda precisa ser feita.
