---
name: licitacoes-e-contratos
description: Orienta licitação e contrato pela Lei 14.133/2021.
version: 1.0.0
author: __JARVIS_USER_FULLNAME__; Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [prefeitura, licitacao, contratos, lei-14133, pncp, edital, dispensa, pt-br]
    category: prefeitura
    related_skills: [redacao-oficial, document-to-action-items, pdf]
---

# Licitações e Contratos Skill

Orienta a fase preparatória, a escolha da modalidade, prazos, contratação direta e gestão de
contratos da Prefeitura de __JARVIS_CIDADE__ pela Lei nº 14.133/2021. É apoio técnico: não substitui
o parecer jurídico (art. 53) nem a decisão da autoridade competente.

## When to Use

- "Que modalidade uso para comprar…", "dá para fazer dispensa?", "qual o prazo do edital?".
- "Monta o checklist da fase preparatória", "revisa esse termo de referência / ETP".
- "Qual o limite de aditivo?", "quem é o fiscal/gestor do contrato?", "precisa publicar no PNCP?".
- Pendência de edital (ex.: distrito industrial): use junto com a skill `jarvis-meetings`.
- "Faz a minuta de decisão/diligência desse processo", recurso contra aceitação de proposta ou
  amostra: siga `references/recurso-e-decisao-superior.md` (checklist + estrutura da decisão).

## Prerequisites

- Texto oficial da lei: https://www.planalto.gov.br/ccivil_03/_ato2019-2022/2021/lei/l14133.htm
  (abra com `web_extract` para conferir qualquer artigo antes de citá-lo em documento).
- Valores de dispensa (art. 75, I e II) são **atualizados todo 1º de janeiro** pelo Executivo federal
  (IPCA-E) e divulgados no PNCP (art. 182): pesquise o valor vigente com `web_search` antes de afirmá-lo.
  Nunca use valor de memória.
- Regulamentos municipais (decreto de compras, de pregão, de dispensa eletrônica) podem detalhar a
  lei; pergunte ao senhor se existe e onde está.

## Quick Reference

Artigos conferidos no texto compilado do Planalto em 30/09/2026. A lei muda: reconfira antes de
citar em documento formal.

| Tema | Regra (Lei 14.133/2021) |
|---|---|
| Modalidades (art. 28) | pregão, concorrência, concurso, leilão, diálogo competitivo. Pregão para bens e serviços comuns, inclusive serviços comuns de engenharia. |
| Fases (art. 17) | preparatória → divulgação do edital → propostas e lances → julgamento → habilitação → recursal → homologação. |
| Fase preparatória (art. 18) | compatível com o plano de contratações anual e as leis orçamentárias: necessidade fundamentada em estudo técnico preliminar (ETP), objeto em termo de referência/anteprojeto/projeto básico, orçamento estimado compatível com o mercado (art. 23). Ao final, controle prévio de legalidade pela assessoria jurídica (art. 53). |
| Prazos mínimos de propostas (art. 55, contados da divulgação do edital) | bens: 8 dias úteis (menor preço/maior desconto) ou 15 (demais); serviços comuns e obras/serviços comuns de engenharia: 10; serviços especiais e obras/serviços especiais de engenharia: 25; contratação integrada: 60; semi-integrada ou demais casos: 35; maior lance: 15; técnica e preço, melhor técnica ou conteúdo artístico: 35. |
| Contratação direta | inexigibilidade (art. 74: inviável a competição) e dispensa (art. 75). Instrução do art. 72: documento de formalização de demanda (e ETP/termo de referência quando for o caso), estimativa de despesa (art. 23), pareceres, compatibilidade orçamentária, habilitação, razão da escolha, justificativa de preço e autorização. |
| Publicidade | Edital no PNCP (art. 54). Contrato e aditivos só têm eficácia após divulgação no PNCP: 20 dias úteis da assinatura (licitação) ou 10 (contratação direta) (art. 94). |
| Gestão do contrato (art. 117) | um ou mais fiscais especialmente designados (requisitos do art. 7º) por ato formal: portaria pela skill `redacao-oficial`. |
| Alterações unilaterais (art. 125) | acréscimos ou supressões até 25% do valor inicial atualizado (obras, serviços, compras); em reforma de edifício ou equipamento, acréscimos até 50%. |
| Vigência (arts. 105-107) | a prevista no edital, com crédito orçamentário verificado a cada exercício (e previsão no PPA se passar de um exercício); serviços e fornecimentos contínuos até 5 anos, prorrogáveis sucessivamente até 10 anos. |

## Procedure

1. Entenda o objeto: o que será comprado/contratado, quantidade, valor estimado, urgência e se é
   comum ou especial. Pergunte o que faltar.
2. Decida o caminho: licitação (qual modalidade e critério de julgamento) ou contratação direta.
   Para dispensa por valor, confira o decreto vigente (Prerequisites) e o fracionamento: somar
   despesas do mesmo objeto no exercício.
3. Monte o checklist da fase preparatória (Quick Reference) e marque o que já existe.
4. Para prazos, conte em dias úteis a partir da divulgação no PNCP e confira o art. 55.
5. Cite artigos apenas depois de conferir com `web_extract` no Planalto; marque `[conferir]` quando
   não der para conferir. Se `web_extract` não extrair a página, baixe com
   `curl -sL -A "Mozilla/5.0" <url> -o l14133.htm` no scratch, decodifique em latin-1, remova tags
   e recorte do `Art. N.` ao `Art. N+1.` em Python.
5a. Processo digital já analisado (pasta com `pages.json`, relatório .docx): confronte a minuta de
   decisão que estiver nos autos com o recurso antes de aproveitá-la — minuta pronta costuma omitir
   alegações e citar parágrafo errado.
6. Entregue: caminho recomendado, checklist, prazos e riscos (fracionamento, orçamento sem pesquisa
   de preços, falta de ETP). Documentos formais seguem a skill `redacao-oficial`.

## Pitfalls

- Lei 8.666/93 e Lei do Pregão (10.520/02) estão revogadas: não use seus prazos nem modalidades
  (tomada de preços e convite não existem mais).
- Valor de dispensa muda todo ano: resposta com valor fixo de memória é erro.
- Dispensa emergencial (art. 75, VIII): só o necessário à emergência, parcelas concluídas em até 1 ano
  da ocorrência, vedadas a prorrogação e a recontratação da empresa com base nesse inciso.
- Pregão não se aplica a obras, a serviços de engenharia que não sejam comuns nem a serviços técnicos
  especializados de natureza predominantemente intelectual (art. 29, parágrafo único).
- Decisão de recurso que não responde a todas as alegações é anulável (art. 165, § 1º, II); na
  dúvida sobre fatos, converta em diligência em vez de decidir o mérito.
- Diligência nunca pode permitir trocar produto, preço ou amostra: escreva a vedação no dispositivo.
- Por voz, dê a resposta curta (caminho + prazo) e ofereça o checklist por escrito.

## Verification

- Todo artigo e valor citados foram conferidos na fonte oficial nesta conversa, ou estão marcados
  `[conferir]`.
- A recomendação diz se depende de parecer jurídico e de regulamento municipal.
- Prazos em dias úteis e contados da publicação no PNCP.
