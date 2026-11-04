---
name: jarvis-procedure-learning
description: Reusa ou salva procedimento testado de tarefa repetida.
version: 1.0.0
author: __JARVIS_USER__; Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [jarvis, procedures, skills, reuse]
    related_skills: []
---

# Jarvis Procedure Learning

This skill makes repeatable Jarvis delegations use durable executable procedures. Memory remains factual and short; full steps and scripts belong in native Hermes skills.

## When to Use

Use this workflow for every delegated task that executes tools or code and could be repeated. It applies in new Hermes sessions and new processes.

## Prerequisites

Use native Hermes tools `skills_list`, `skill_view`, and `skill_manage`. Use `terminal` to execute and verify an artifact. Procedure assets must live under the skill's `scripts/`, `references/`, or `templates/` directory.

## How to Run

Before acting, call `skills_list`, search for a procedure matching the operation, and load each plausible match fully with `skill_view`. When a match exists, check its declared prerequisites and load and execute its existing artifact. Pass current values as parameters. Never replace the artifact with inline code or a new file in `cache/scratch/`.

If a required artifact, interpreter, import, executable, credential, or service is missing, stop with a concise diagnostic. Record the observed failure in the response. Do not recreate or edit the procedure to work around an environment failure.

## Procedure

1. Define a success condition that can be observed independently of a tool's claim (exit status, exact output, test assertion, or UI evidence).
2. Run the existing procedure if one matches. If it fails, capture the diagnostic and determine whether the failure proves that the procedure's logic is incompatible. Missing files, imports, permissions, services, or UI evidence are environment/uncertain outcomes; preserve the procedure unchanged.
3. For a task with no matching procedure, solve it using available tools. If a new script is needed, place it under the candidate skill's permanent `scripts/` path. Execute it with safe test inputs and verify the success condition before recording it as learned.
4. After success only, search again for equivalent skills. Update a genuinely matching skill with `skill_manage` instead of creating a duplicate. Otherwise create one skill with a concise trigger description and the complete procedure. Include interpreter and dependencies, prerequisites, variable parameters, the exact skill-relative invocation, verification evidence, and failure diagnostics.
5. Keep scripts self-contained and parameterized. Never embed a real recipient, private message, credential, transcript, temporary path, or session-specific data.
6. Persist synchronously through `skill_manage`; wait for its successful result before reporting that learning completed. If the write is staged, rejected, or fails, report that the procedure was not saved.
7. In the post-task review, inspect the preceding session's real tool results. Report `PROCEDURE_SAVED:<name>` only after confirming the skill and artifact exist and the procedure's execution was verified. Report `PROCEDURE_REUSED:<name>` only after confirming the existing artifact was loaded and run. Otherwise report `PROCEDURE_UNCHANGED:<reason>` or `NO_PROCEDURE`.

## Pitfalls

- A skill being loaded is not proof its script was executed.
- A button click or a tool's success claim is not independent verification.
- Unconfirmed external actions must not be retried or recorded as successful procedures.
- When verifying file downloads, inspect exact filenames and sequence numbers rather than aggregate file counts in the directory.
- When automating open Chrome tabs via DevTools Console, type `allow pasting` key-by-key before pasting JS scripts to pass Chrome Self-XSS protection (see `references/chrome_devtools_injection.md`).
- Do not author a second script because an import, path, or dependency failed.
- Failure observations may be summarized generically in the procedure's diagnostic guidance; never preserve private task data or raw logs.

## Verification

For reuse, verify the invoked path is the same skill-relative artifact as before, parameter values are supplied at runtime, and the result meets the declared success condition. If that cannot be shown, report the result as unverified and leave the skill unchanged.
