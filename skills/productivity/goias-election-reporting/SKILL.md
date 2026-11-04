---
name: goias-election-reporting
description: Use when generating audited DOCX election reports for Goiás.
version: 1.0.0
author: __JARVIS_USER__; Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [eleicoes, goias, docx, relatorio, tse]
---

# Goiás Election Reporting Standards

This skill establishes strict guidelines and executable standards for generating comprehensive political and electoral reports for Goiás elections.

## Core Rules for Electoral Data Accuracy

1. **Never Invent or Guess Party Affiliations:** Always cross-reference candidates with official TSE records (DivulgaCandContas) or trusted journalistic sources before listing.
2. **Methodology and Limitations Disclaimer:** Always include an explicit methodology section stating the percentage of totalized sections, source of data, and disclaimer regarding official TSE diplomação.
3. **Completeness Standard:** When requested to list elected officials, NEVER summarize or truncate arbitrarily. List ALL 17 Federal Deputies and ALL 41 State Deputies with complete vote counts and percentages.
4. **Analytical Rigor:** Calculate summary metrics:
   - Effective number of parties (fragmentation)
   - Re-election rate / Turnover rate
   - Vote distribution ratio (ratio between top-voted and lowest-voted elected candidate)
   - Party caucus totals (Bancadas) across majoritarian and proportional seats.

## DOCX Output Workflow

When generating a DOCX report:
1. Formulate a structured JSON spec matching the schema expected by `productivity/docx/scripts/docx_create.py`.
2. Ensure clear hierarchy: Executive Summary, Methodology, Majoritarian Results (Governor, Senate), Proportional Results (Federal Deputies, State Deputies), and Consolidated Party Caucus Analysis.
3. Include styled tables (`Table Grid`) with explicit column headers and complete rows.
4. Render the DOCX file via Python script execution using native `python-docx` dependencies.
