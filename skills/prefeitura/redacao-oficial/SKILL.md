---
name: redacao-oficial
description: Redige ofício, despacho e portaria no padrão oficial.
version: 1.0.0
author: __JARVIS_USER_FULLNAME__; Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [prefeitura, redacao-oficial, oficio, portaria, despacho, decreto, pt-br]
    category: prefeitura
    related_skills: [licitacoes-e-contratos, docx, pdf]
---

# Redação Oficial Skill

Redige expedientes e atos da Prefeitura Municipal de __JARVIS_CIDADE__ (GO) no padrão ofício do
Manual de Redação da Presidência da República (3ª ed., 2018) e na técnica legislativa da
LC 95/1998. Não decide o mérito administrativo nem cita norma sem conferir o texto vigente.

## When to Use

- "Qual meu próximo número de ofício?", "reserva/registra número de ofício", "salva como ofício...".
- "Faz um ofício para a Secretaria de Saúde pedindo…", "responde esse ofício", "redige um despacho".
- "Monta uma portaria nomeando / designando fiscal / instituindo comissão", "minuta de decreto".
- Revisar um texto oficial que o senhor __JARVIS_USER__ colou ou anexou.

Não usar para: edital e contrato de licitação (skill `licitacoes-e-contratos`), mensagem informal
(WhatsApp, e-mail pessoal).

## Prerequisites

- Controle e Registro de Ofícios:
  - Script e base canônica: `__JARVIS_USERPROFILE_FS__/Documents/JARVIS_Records/oficios/oficios.py` e `oficios.json`.
  - Consultar próximo número: execute `python "__JARVIS_USERPROFILE_FS__/Documents/JARVIS_Records/oficios/oficios.py" proximo` ou leia `next_number` direto em `__JARVIS_USERPROFILE_FS__/Documents/JARVIS_Records/oficios/oficios.json` via `read_file`. Nunca use buscas de disco (Get-ChildItem/dir), pastas de download ou busca em sessões antigas para isso.
  - Registrar ofício: execute `python "__JARVIS_USERPROFILE_FS__/Documents/JARVIS_Records/oficios/oficios.py" registrar "<Assunto>"`. O número é sequencial e gravado imediatamente. Se o senhor pedir vários sem assunto, pergunte os assuntos antes de registrar.
  - Pós-registro no Segundo Cérebro: registre o fato via `mcp__cerebro__cerebro_registrar` (kind="fato", text="Ofício nº <N> registrado com o assunto '<Assunto>'. Próximo número disponível: <N+1>.", source="Registro de ofícios do Gabinete (AAAA-MM-DD)", agent="Jarvis").
- Modelos em `templates/oficio.md`, `templates/despacho.md` e `templates/portaria.md` desta skill:
  carregue com `skill_view` (arquivo da skill) antes de redigir.
- Para gerar .docx/.pdf, use as skills `docx` ou `pdf` depois do texto aprovado.
- Dados que faltarem (número do expediente, destinatário, cargo, fundamento legal) são perguntados,
  nunca inventados.

## Quick Reference

| Item | Regra |
|---|---|
| Tratamento | "Vossa Excelência" para quem o Manual lista: prefeito e vice, presidente da Câmara Municipal, governador, secretários de Estado, deputados, senadores, juízes e desembargadores. Vereadores e secretários municipais seguem o costume/regimento local: na dúvida, pergunte. Demais autoridades: "Vossa Senhoria". |
| Vocativo | "Senhor Prefeito,", "Senhora Secretária,", "Senhor Presidente,". **Nunca** "Excelentíssimo", "Ilustríssimo", "Digníssimo", "DD." (diretriz do senhor __JARVIS_USER__ e do Manual). |
| Fecho | "Respeitosamente," para autoridade superior; "Atenciosamente," para mesma hierarquia ou inferior. |
| Identificação | Número/ano/sigla: "Ofício nº 45/2026/GAB". Local e data por extenso: "__JARVIS_CIDADE__, 30 de setembro de 2026." |
| Assunto | "Assunto: <resumo em poucas palavras>." |
| Texto | Direto: introdução com o motivo, desenvolvimento, conclusão com o pedido. Parágrafos numerados quando houver mais de um (exceto vocativo e fecho). |
| Signatário | Nome e cargo abaixo da assinatura; sem "à disposição" nem agradecimentos protocolares. |
| Atos (portaria/decreto) | Epígrafe em maiúsculas com data por extenso; ementa; preâmbulo com a competência (Lei Orgânica); "RESOLVE:"/"DECRETA:"; artigos "Art. 1º" a "Art. 9º" ordinais, depois "Art. 10." cardinais; cláusula de vigência. |

## Procedure

1. Identifique tipo (ofício, despacho, portaria, decreto), remetente, destinatário e objetivo.
   Se faltar um deles, pergunte em uma frase só.
2. Carregue o modelo correspondente em `templates/`. A data do expediente é a de hoje (data atual
   do sistema, fuso America/Sao_Paulo), por extenso, salvo pedido em contrário; o número fica
   `[__]` quando o senhor não informar.
3. Redija com linguagem impessoal, frases curtas, sem gerúndio de enrolação ("estaremos enviando"),
   sem jargão ("outrossim", "vimos por meio deste"). Uma ideia por parágrafo.
4. Para portaria/decreto, cite o dispositivo que dá competência. Se não souber o artigo da Lei
   Orgânica ou da lei municipal, deixe `[art. __ da Lei Orgânica]` marcado para o senhor preencher.
   Nunca invente número de lei, artigo ou data.
5. Revise contra a Quick Reference: tratamento, vocativo, fecho, numeração, datas.
6. Entregue o texto final e, em uma linha, o que ficou pendente de preenchimento.

## Pitfalls

- Consulta ou registro de número de ofício: não faça varredura em `D:\`, `__JARVIS_DOWNLOADS_BS__` nem busque em sessões do Cérebro ou arquivos avulsos. A fonte canônica e o controle são exclusivos do script `__JARVIS_USERPROFILE_FS__/Documents/JARVIS_Records/oficios/oficios.py` e do banco `oficios.json`.
- Não acrescente prazo, exigência ou conteúdo que o senhor não pediu. Em "relatório de setembro até
  10 de outubro", setembro é o período e 10 de outubro é o prazo de entrega. Se a frase for ambígua,
  pergunte em vez de escolher.
- Pronome de tratamento concorda na 3ª pessoa: "Vossa Senhoria **sabe**", "seu pedido", não "vosso".
- "Vossa" é para quem se fala; "Sua Excelência" é para quem se fala **sobre**.
- Data em ato normativo: "DE 30 DE SETEMBRO DE 2026" na epígrafe; no texto, "30 de setembro de 2026".
- Não use "Excelentíssimo Senhor" nem no endereçamento: basta "Ao Senhor" + nome + cargo.
- Resposta por voz: dite só o resumo e ofereça gerar o documento; texto oficial não se lê inteiro em voz alta.

## Verification

- Nenhuma ocorrência de "Excelentíssimo", "Ilustríssimo", "Digníssimo" ou "DD." (use `search_files`
  no arquivo gerado ou confira no texto).
- Todo fundamento legal citado foi fornecido pelo senhor ou conferido na fonte oficial com
  `web_extract`; o que não foi conferido está marcado com `[ ]`.
- Número, data e signatário presentes.
