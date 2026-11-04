---
name: jarvis-meetings
description: Atas, pendências e falas das reuniões gravadas pelo Jarvis.
version: 1.0.0
author: __JARVIS_USER__ (Jarvis 2.0), Hermes Agent
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [Meetings, Minutes, Memory, Productivity, Voice, Jarvis]
    related_skills: [weekly-review-planning, obsidian, notion]
---

# Jarvis Meetings — local meeting archive

The Jarvis sentinel (always-listening ambient engine) transcribes everything it hears
(room microphone + Windows loopback for Meet/Teams/Zoom) into a local SQLite database
and distills each closed conversation block into a structured meeting summary (ata).
Tasks for __JARVIS_USER__ and commitments become tracked PENDÊNCIAS (open/concluded) in the
same database. This archive is the ONLY home for meetings, atas and pendências —
never copy them into the permanent memory. Use this skill to answer questions about
past meetings, decisions, tasks, commitments and what is still pending.

## When to Use

- "O que foi decidido na reunião com a equipe?" / "What did we decide about X?"
- "Quais tarefas fiquei de fazer?" / "What did I commit to this week?"
- "Resuma minha reunião de ontem com a Marina"
- "Encerra a reunião" / "Gera a ata agora" (closes the meeting in progress)
- "Essa reunião juntou duas" / "quem estava na reunião?" → point to the review page
- "O que está pendente?" / "Me lembra de cobrar X até sexta" / "Isso já foi feito"
- Any question about what was said/decided in a recent conversation

Don't use for: facts about __JARVIS_USER__ himself (family, preferences, profile) — those live
in the permanent memory (memory tool). Everything about meetings lives here.

## The archive

- Database: `__JARVIS_HERMES_HOME_BS__\jarvis\transcripts.db` (local, private)
- `transcripts` — raw speech chunks (`source` = `mic` for the room, `system` for video-call participants)
- `meeting_summaries` — structured atas: title, summary, key_decisions, action_items, topics, participants
- `meetings` — one row per meeting: title, `started_at`/`ended_at`, `participants` (confirmed by __JARVIS_USER__),
  `status` (`open` / `prompting` = asking "encerrar?" / `closed`), `ata_status`
- `transcripts.meeting_id` and `meeting_summaries.meeting_id` link speech and ata to the meeting
- `meeting_markers` — explicit "Jarvis, iniciando/encerrando reunião" boundaries
- `pendencias` — tasks/commitments with `status` (`aberta`/`concluida`), `responsavel`, `prazo`, `ata_id`
- After 2 min of silence the app asks "encerrar a reunião?"; no answer in 5 min closes it
  (system-audio-only blocks close without asking; isolated noise is discarded)
- Review page (this PC only): http://127.0.0.1:17850 — meetings by year/month/day, transcript,
  participants with suggestions, "Enviar para outra reunião" (split) and "Juntar" (merge).
  When the user says two meetings got joined or wants to fix participants, send him there.

## Commands

Run these with the terminal tool (they are read-only unless `minutes` is used):

```bash
# COMPROMISSOS DO DIA (fast path, ~2 s): Google Agenda of today + open pendências sorted by deadline (--dias N adds next N days)
"__JARVIS_HERMES_HOME_FS__/jarvis/.venv311/Scripts/python.exe" "__JARVIS_HERMES_HOME_FS__/jarvis/agenda.py"

# PENDÊNCIAS: list open ones (--todas includes concluded)
"__JARVIS_HERMES_HOME_FS__/jarvis/.venv311/Scripts/python.exe" "__JARVIS_HERMES_HOME_FS__/jarvis/jarvis.py" pendencias

# Register a new pendência (writes)
"__JARVIS_HERMES_HOME_FS__/jarvis/.venv311/Scripts/python.exe" "__JARVIS_HERMES_HOME_FS__/jarvis/jarvis.py" pendencia-add "Publicar edital" --prazo 2026-10-05 --responsavel __JARVIS_USER__

# Mark pendência #ID as done (writes) — only after the user confirms it was done
"__JARVIS_HERMES_HOME_FS__/jarvis/.venv311/Scripts/python.exe" "__JARVIS_HERMES_HOME_FS__/jarvis/jarvis.py" pendencia-ok 3

# Condensed recent context (raw window, model-summarized)
"__JARVIS_HERMES_HOME_FS__/jarvis/.venv311/Scripts/python.exe" "__JARVIS_HERMES_HOME_FS__/jarvis/jarvis.py" context --minutes 90
```

## Procedure

1. **Find the meeting.** `search "<keywords in the user's language>" --days N` first — it
   prints matching atas (decisions + action items) and then matching raw speech lines.
   Prefer the user's own words for keywords (usually Portuguese).
2. **If the meeting is still in progress** (no ata yet), run `minutes` to close it and get the ata,
   then search again.
3. **Compose the answer** from the ata fields; quote raw lines only when the user asks
   for exactly what was said. Timestamps in the DB are UTC — MANDATORY: subtract 3 hours
   (Brasília, UTC-3) BEFORE showing any time to the user. Never present raw DB times.
   The user corrected this mistake once (2026-09-29). Verify offset with `date '+%z'`.
4. **Follow-ups, cobranças and deadlines** go to `pendencia-add` — NEVER to the memory
   tool. When the user says something is done, run `pendencia-ok <id>`. Meeting facts
   (who, what was agreed, values, contracts) stay in the atas; do not duplicate them
   in memory.

## Notes

- Multiple searches with different keywords beat one long query; the archive is local
  SQLite and answers instantly.
- Always check real system date (`python -c "import datetime; print(datetime.date.today())"`) before setting deadline dates like "hoje" — never rely on static prompt headers which can be stale.
- Terminal commands on Windows bash must use forward slashes and direct python execution (avoid PowerShell `&` syntax).
- The sentinel must be running for NEW meetings to be captured
  (`jarvis\start_jarvis.cmd`).
- Privacy: everything stays on this machine; the extraction model runs in the cloud
  with the user's Gemini key, so avoid dumping the full archive into other tools.
