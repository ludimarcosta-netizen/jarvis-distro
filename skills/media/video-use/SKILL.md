---
name: video-use
description: "Edita vídeo: corta, legenda, insere texto/animação/logo. Edit any video by conversation. Transcribe, cut, color grade, generate overlay animations, insert text (titles, lower thirds, badges, typed text, callouts, logos, watermarks), generate AI B-roll images, punch-in zooms, transitions, sound effects, word-highlight captions — for talking heads, montages, tutorials, travel, interviews. No presets, no menus. Ask questions, confirm the plan, execute, iterate, persist. Production-correctness rules are hard; everything else is artistic freedom."
---

# Video Use

## Principle

1. **LLM reasons from raw transcript + on-demand visuals.** The only derived artifact that earns its keep is a packed phrase-level transcript (`takes_packed.md`). Everything else — filler tagging, retake detection, shot classification, emphasis scoring — you derive at decision time.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user, then edit.
5. **Artistic freedom is the default.** Every specific value, preset, font, color, duration, pitch structure, and technique in this document is a *worked example* from one proven video — not a mandate. Read them to understand what's possible and why each worked. Then make your own taste calls based on what the material actually is and what the user actually wants. **The only things you MUST do are in the Hard Rules section below.** Everything else is yours.
6. **Invent freely.** If the material calls for a technique not described here — split-screen, picture-in-picture, lower-third identity cards, reaction cuts, speed ramps, freeze frames, crossfades, match cuts, L-cuts, J-cuts, speed ramps over breath, whatever — build it. The helpers are ffmpeg and PIL. They can do anything the format supports. Do not wait for permission.
7. **Verify your own output before showing it to the user.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

These are the things where deviation produces silent failures or broken output. They are not taste, they are correctness. Memorize them.

1. **Subtitles are applied LAST in the filter chain**, after every overlay. Otherwise overlays hide captions. Silent failure.
2. **Per-segment extract → lossless `-c copy` concat**, not single-pass filtergraph. Otherwise you double-encode every segment when overlays are added.
3. **30ms audio fades at every segment boundary** (`afade=t=in:st=0:d=0.03,afade=t=out:st={dur-0.03}:d=0.03`). Otherwise audible pops at every cut.
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift the overlay's frame 0 to its window start. Otherwise you see the middle of the animation during the overlay window.
5. **Master SRT uses output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`. Otherwise captions misalign after segment concat.
6. **Never cut inside a word.** Snap every cut edge to a word boundary from the Scribe transcript.
7. **Pad every cut edge.** Working window: 30–200ms. Scribe timestamps drift 50–100ms — padding absorbs the drift. Tighter for fast-paced, looser for cinematic.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode (loses sub-second gap data). Never normalized fillers (loses editorial signal).
9. **Cache transcripts per source.** Never re-transcribe unless the source file itself changed.
10. **Parallel sub-agents for multiple animations.** Never sequential. Spawn N at once via the `Agent` tool; total wall time ≈ slowest one.
11. **Strategy confirmation before execution.** Never touch the cut until the user has approved the plain-English plan.
12. **All session outputs in `<videos_dir>/edit/`.** Never write inside the `video-use/` project directory.

Everything else in this document is a worked example. Deviate whenever the material calls for it.

## Directory layout

The skill lives in `video-use/`. User footage lives wherever they put it. All session outputs go into `<videos_dir>/edit/`.

```
<videos_dir>/
├── <source files, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached raw Scribe JSON
    ├── brand.json               ← visual identity (colours, fonts, logo, image style)
    ├── plan.md                  ← timed plan of B-roll/effects/text, approved by the user
    ├── timeline.json            ← output start/end of each range (written by render.py)
    ├── broll/ + broll_plan.json ← generated stills (+ .json sidecars with the prompt)
    ├── sfx/                     ← sound effects (synthesized or the user's)
    ├── captions.ass             ← word-highlight captions
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← per-segment extracts with grade + fades
    ├── master.srt               ← output-timeline subtitles
    ├── downloads/               ← yt-dlp outputs
    ├── verify/                  ← debug frames / timeline PNGs
    ├── preview.mp4
    └── final.mp4
```

## Setup

First-time install lives in `install.md` (clone, deps, ffmpeg, skill registration, API key). Don't re-run it every session; on cold start just verify:

- `ELEVENLABS_API_KEY` resolves — either in the environment or in `.env` at the video-use repo root. If missing, ask the user to paste one and write it to `.env` (never to the user's `<videos_dir>`).
- `ffmpeg` + `ffprobe` on PATH.
- Python deps installed (`uv sync` or `pip install -e .` inside the repo).
- Node.js + npm available if the session needs HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- `yt-dlp`, HyperFrames, Remotion, Manim installed only on first use.
- First-use animation setup happens inside the slot directory, never at the video-use repo root. HyperFrames can be invoked with `npx --yes hyperframes ...`; Remotion can be scaffolded with `npx create-video@latest` or installed as a project-local dependency before using its `remotion render` command.
- This skill vendors `skills/manim-video/`. Read its SKILL.md when building a Manim slot.

Helpers (`helpers/transcribe.py`, `helpers/render.py`, etc.) live alongside this SKILL.md. Resolve their paths relative to the directory containing this file — the skill is typically symlinked at `~/.claude/skills/video-use/` or `~/.codex/skills/video-use/`.

**Jarvis (Windows):** installed at `__JARVIS_HERMES_HOME_BS__\skills\media\video-use`, with its own venv. Run every helper with `__JARVIS_HERMES_HOME_BS__\skills\media\video-use\.venv\Scripts\python.exe -X utf8 helpers\<name>.py` (never the system/Jarvis Python; `-X utf8` or the console crashes on "→"). ffmpeg/ffprobe are on PATH. Talk to the user in Brazilian Portuguese.
Transcription here is **local Whisper** (`large-v3-turbo` on the GPU, via `helpers\whisper_local.py` run by the Jarvis venv), chosen by the user instead of ElevenLabs: do **not** ask for an `ELEVENLABS_API_KEY` — `transcribe.py`/`transcribe_batch.py` use Whisper automatically when no key exists. Pass `--language pt` for Portuguese footage. Same JSON shape as Scribe, but: no diarization (every word is `speaker_0` — ask the user who speaks when it matters), no audio events, and fillers ("é…", "hum") are often dropped, so find hesitations from gaps in `takes_packed.md` and `timeline_view` waveforms rather than from filler tokens. Ignore the "use hosted Scribe" advice below.
Other Jarvis differences:
- **Sub-agents:** wherever this file says "spawn via the `Agent` tool", use Jarvis's `delegate_task` (several tasks in one call run in parallel). For a single simple text insert, skip the sub-agent and run `text_overlay.py` yourself.
- **Inserts first:** for titles, name cards, badges, typed text, callouts and logos use `helpers\text_overlay.py` + the EDL overlay fields (see "Text inserts" below) before writing custom PIL/ffmpeg code. For B-roll, effects and animated captions follow "Retention edit" below with the helpers listed there — don't hand-roll them.
- **ffmpeg here has no fontconfig config:** `drawtext` without `fontfile=` crashes (segfault). Always pass `fontfile='C\:/Windows/Fonts/arialbd.ttf'` — or use `text_overlay.py`. Subtitles are fine: libass finds fonts by family name through DirectWrite.
- **Vertex images cost the prefeitura's GCP project.** `gen_image.py --provider vertex` only on explicit request; default is free Pollinations.
- **Fonts** (macOS paths below don't exist here): `C:\Windows\Fonts\segoeuib.ttf` (Segoe UI Bold, default), `segoeui.ttf`, `arialbd.ttf`, `bahnschrift.ttf` (condensed, good for lower thirds), `consolab.ttf` (monospace, terminal look). Subtitle `force_style` asks for Helvetica, which Windows lacks; pass a `FontName=Arial` or `FontName=Segoe UI` style if the look matters.
- **Manim is not installed** in the venv. Use it only if the slot really needs it, after `uv pip install manim` inside the repo (needs a LaTeX install for equations). HyperFrames/Remotion run on the installed Node 24, but their first render downloads a headless Chromium — tell the user it takes a few minutes.

## Helpers

- **`transcribe.py <video>`** — single-file Scribe call. `--num-speakers N` optional. Cached.
- **`transcribe_batch.py <videos_dir>`** — 4-worker parallel transcription. Use for multi-take.
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrase-level, break on silence ≥ 0.5s).
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform PNG. On-demand visual drill-down. **Not a scan tool** — use it at decision points, not constantly.
- **`render.py <edl.json> -o <out>`** — per-segment extract → concat → overlays (PTS-shifted, positioned, faded) → subtitles LAST. `--preview` for 720p fast. `--build-subtitles` to generate master.srt inline. Overlays may be video (`.mov`/`.mp4`/`.webm`) or still images (`.png`/`.jpg`).
- **`text_overlay.py <style> "<text>" -o <out.mov>`** — transparent text insert (`title`, `lower_third`, `typewriter`, `callout`, `badge`). `--subtext`, `--match <rendered base>` for size/fps, `--brand`, `--font`, `--color/--accent/--box`, `--position top|center|bottom|X,Y`, `--duration`. Output is full-frame QuickTime RLE with alpha.
- **`gen_image.py "<prompt>" -o <png>`** / **`--plan <json>`** — B-roll stills from prompts, cached, parallel. `--provider pollinations` (free, default) or `vertex` (paid). `--brand` appends the project's image style.
- **`broll.py <image> -o <clip>`** — still → moving clip: `--mode full` (Ken Burns, replaces the footage, .mp4) or `--mode card` (framed picture-in-picture over the speaker, .mov alpha).
- **`transition.py swipe|flash -o <mov>`** — overlay that covers a cut at its midpoint (no re-encode across segments).
- **`sfx.py <whoosh|pop|click|impact|riser|ding|all>`** — synthesized effects; prints each one's `attack` for frame-accurate sync.
- **`captions.py <edl.json> -o captions.ass`** — word-highlight captions (spoken word in the accent colour).
- **`brand.py`** — loads `<edit>/brand.json` (colours, fonts, logo, image style) for all of the above.
- **`grade.py <in> -o <out>`** — ffmpeg filter chain grade. Presets + `--filter '<raw>'` for custom.

For animations, create `<edit>/animations/slot_<id>/` with `Bash` and spawn a sub-agent via the `Agent` tool.

## The process

1. **Inventory.** `ffprobe` every source. `transcribe_batch.py` on the directory. `pack_transcripts.py` to produce `takes_packed.md`. Sample one or two `timeline_view`s for a visual first impression.
2. **Pre-scan for problems.** One pass over `takes_packed.md` to note verbal slips, obvious mis-speaks, or phrasings to avoid. Plain list, feed into the editor brief.
3. **Converse.** Describe what you see in plain English. Ask questions *shaped by the material*. Collect: content type, target length/aspect, aesthetic/brand direction, pacing feel, must-preserve moments, must-cut moments, animation and grade preferences, subtitle needs. Do not use a fixed checklist — the right questions are different every time.
4. **Propose strategy.** 4–8 sentences: shape, take choices, cut direction, animation plan, grade direction, subtitle style, length estimate. **Wait for confirmation.**
5. **Execute.** Produce `edl.json` via the editor sub-agent brief. Drill into `timeline_view` at ambiguous moments. Build animations in parallel sub-agents. Apply grade per-segment. Compose via `render.py`.
6. **Preview.** `render.py --preview`.
7. **Self-eval (before showing the user).** Run `timeline_view` on the **rendered output** (not the sources) at every cut boundary (±1.5s window). Check each image for:
   - Visual discontinuity / flash / jump at the cut
   - Waveform spike at the boundary (audio pop that slipped past the 30ms fade)
   - Subtitle hidden behind an overlay (Rule 1 violation)
   - Overlay misaligned or showing wrong frames (Rule 4 violation)

   Also sample: first 2s, last 2s, and 2–3 mid-points — check grade consistency, subtitle readability, overall coherence. Run `ffprobe` on the output to verify duration matches the EDL expectation.

   Measure the audio, don't assume it: `ffmpeg -i out.mp4 -af ebur128=peak=true -f null -` for integrated loudness and true peak, plus RMS per section (dialogue, music-only, end card). An end card 15 dB under the dialogue, or effects louder than speech, is a bug. You cannot listen: say so, and report the numbers.

   For anything the user will publish (launch, promo, ad), also spawn one **critic sub-agent** with the rendered file, the EDL, and any reference videos the user gave. Brief it to roast, not to praise: a verdict, ranked problems with timecodes and evidence (frames, levels), and the 5 fixes to do first. Fresh eyes catch what the author stopped seeing — cut-off payoff lines, 0.5s memes, unreadable 28px text at phone size.

   If anything fails: fix → re-render → re-eval. **Cap at 3 self-eval passes** — if issues remain after 3, flag them to the user rather than looping forever. Only present the preview once the self-eval passes.
8. **Iterate + persist.** Natural-language feedback, re-plan, re-render. Never re-transcribe. Final render on confirmation. Append to `project.md`.

## Cut craft (techniques)

- **Audio-first.** Candidate cuts from word boundaries and silence gaps.
- **Preserve peaks.** Laughs, punchlines, emphasis beats. Extend past punchlines to include reactions — the laugh IS the beat.
- **Speaker handoffs** benefit from air between utterances. Common values: 400–600ms. Less for fast-paced, more for cinematic. Taste call.
- **Audio events as signals.** `(laughs)`, `(sighs)`, `(applause)` mark beats. Extend past them.
- **Silence gaps are cut candidates.** Silences ≥400ms are usually the cleanest. 150–400ms phrase boundaries are usable with a visual check. <150ms is unsafe (mid-phrase).
- **Example cut padding** (the launch video shipped with this): 50ms before the first kept word, 80ms after the last. Tighter for montage energy, looser for documentary. Stay in the 30–200ms working window (Hard Rule 7).
- **Never reason audio and video independently.** Every cut must work on both tracks.

## The packed transcript (primary reading view)

`pack_transcripts.py` reads all `transcripts/*.json` and produces one markdown file where each take is a list of phrase-level lines, each prefixed with its `[start-end]` time range. Phrases break on any silence ≥ 0.5s OR speaker change. This is the artifact the editor sub-agent reads to pick cuts — it gives word-boundary precision from text alone at 1/10 the tokens of raw JSON.

Example line:
```
## C0103  (duration: 43.0s, 8 phrases)
  [002.52-005.36] S0 Ninety percent of what a web agent does is completely wasted.
  [006.08-006.74] S0 We fixed this.
```

## Editor sub-agent brief (for multi-take selection)

When the task is "pick the best take of each beat across many clips," spawn a dedicated sub-agent with a brief shaped like this. The structure is load-bearing; the pitch-shape example is not.

```
You are editing a <type> video. Pick the best take of each beat and 
assemble them chronologically by beat, not by source clip order.

INPUTS:
  - takes_packed.md (time-annotated phrase-level transcripts of all takes)
  - Product/narrative context: <2 sentences from the user>
  - Speaker(s): <name, role, delivery style note>
  - Expected structure: <pick an archetype or invent one>
  - Verbal slips to avoid: <list from the pre-scan pass>
  - Target runtime: <seconds>

Common structural archetypes (pick, adapt, or invent):
  - Tech launch / demo:   HOOK → PROBLEM → SOLUTION → BENEFIT → EXAMPLE → CTA
  - Tutorial:             INTRO → SETUP → STEPS → GOTCHAS → RECAP
  - Interview:            (QUESTION → ANSWER → FOLLOWUP) repeat
  - Travel / event:       ARRIVAL → HIGHLIGHTS → QUIET MOMENTS → DEPARTURE
  - Documentary:          THESIS → EVIDENCE → COUNTERPOINT → CONCLUSION
  - Music / performance:  INTRO → VERSE → CHORUS → BRIDGE → OUTRO
  - Or invent your own.

RULES:
  - Start/end times must fall on word boundaries from the transcript.
  - Pad cut boundaries (working window 30–200ms).
  - Prefer silences ≥ 400ms as cut targets.
  - Unavoidable slips are kept if no better take exists. Note them in "reason".
  - If over budget, revise: drop a beat or trim tails. Report total and self-correct.

OUTPUT (JSON array, no prose):
  [{"source": "C0103", "start": 2.42, "end": 6.85, "beat": "HOOK",
    "quote": "...", "reason": "..."}, ...]

Return the final EDL and a one-line total runtime check.
```

## Color grade (when requested)

Your job is to **reason about the image**, not apply a preset. Look at a frame (via `timeline_view`), decide what's wrong, adjust one thing, look again.

Mental model is ASC CDL. Per channel: `out = (in * slope + offset) ** power`, then global saturation. `slope` → highlights, `offset` → shadows, `power` → midtones.

**Example filter chains** (`grade.py` has `--list-presets`; use them as starting points or mix your own):

- **`warm_cinematic`** — retro/technical, subtle teal/orange split, desaturated. Shipped in a real launch video. Safe for talking heads.
- **`neutral_punch`** — minimal corrective: contrast bump + gentle S-curve. No hue shifts.
- **`none`** — straight copy. Default when the user hasn't asked.

For anything else — portraiture, nature, product, music video, documentary — invent your own chain. `grade.py --filter '<raw ffmpeg>'` accepts any filter string.

Hard rules: apply **per-segment during extraction** (not post-concat, which re-encodes twice). Never go aggressive without testing skin tones.

## Subtitles (when requested)

Subtitles have three dimensions worth reasoning about: **chunking** (1/2/3/sentence per line), **case** (UPPER/Title/Natural), and **placement** (margin from bottom). The right combo depends on content.

**Worked styles** — pick, adapt, or invent:

**`bold-overlay`** — short-form tech launch, fast-paced social. ~2-word chunks, UPPERCASE, break on punctuation and pauses ≥ 0.3s, grow to 3 words rather than flash a cue < 0.35s (`chunk_words` in `render.py`), Helvetica 18 Bold, white-on-outline, `MarginV=35`. `render.py` ships with this as `SUB_FORCE_STYLE`.

```
FontName=Helvetica,FontSize=18,Bold=1,
PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,
BorderStyle=1,Outline=2,Shadow=0,
Alignment=2,MarginV=35
```

**`natural-sentence`** (if you invent this mode) — narrative, documentary, education. 4–7 word chunks, sentence case, break on natural pauses, `MarginV=60–80`, larger font for readability, slightly wider max-width. No shipped force_style — design one if you need it.

**`word-highlight`** — the retention-edit look: same chunking, the word being spoken turns the brand accent colour and grows 12%, chunks pop in, short pauses hold the line instead of blinking. Built by `captions.py` as an `.ass` file sized to the output (`--match` a rendered base); set `"subtitles": "captions.ass"` and render.py uses the file's own styles. Fonts are picked by family name (`brand.json` `fonts.caption_family`, default Arial Black).

Invent a third style if neither fits. Hard rules: subtitles LAST (Rule 1), output-timeline offsets (Rule 5).

## Text inserts and overlays (when requested)

First decide what the insert is, because it decides the file format:

- **On top of the footage** (title over the speaker, name card, typed command, callout, logo/watermark): the overlay MUST have an alpha channel — `.mov` (QuickTime RLE `qtrle` or ProRes 4444), `.webm` VP9 with `yuva420p`, or a transparent `.png`. An `.mp4` (H.264) has no alpha: it covers the whole frame and hides the speaker. This is the silent failure to avoid.
- **Replacing the footage** for a moment (full-screen card, diagram, chapter screen): an opaque `.mp4` at the output size is fine.

The quick path for text is `text_overlay.py`. One command per insert, then one EDL overlay entry:

```
python helpers/text_overlay.py lower_third "Ana Souza" --subtext "Diretora de Produto" --match edit/base_preview.mp4 --duration 4 -o edit/animations/slot_1/render.mov
```
```json
{"file": "animations/slot_1/render.mov", "start_in_output": 3.2, "duration": 4.0}
```

- Size must match the **rendered** video, not the source (render.py outputs 1920 wide, or 1080x1920 portrait). `--match` on a rendered `base_preview.mp4`/`base.mp4` gets it right; otherwise `--size`.
- Time it from the transcript: a name card lands when the person starts talking; a title lands on the word it names (see payoff timing below). Keep `duration` ≥ the text's reading time + 1s.
- Portrait output: the helper keeps bottom elements above the TikTok/Reels UI band; with `--position X,Y` you own that.
- Need something the four styles don't cover (word-by-word kinetic type, animated charts)? Build it (PIL frames → `qtrle` with `-pix_fmt argb`, or HyperFrames/Remotion), keeping the alpha rule above.

**EDL overlay fields** — `file`, `start_in_output`, `duration` are required. Optional:

- `x`, `y` — position of the overlay's top-left; numbers or ffmpeg expressions of `W`/`H` (video) and `w`/`h` (overlay): `"(W-w)/2"` centers, `"W-w-40"` = 40px from the right. Default `0`,`0`. Use them for small overlays (a logo, a PNG sticker); full-frame overlays need none.
- `width` — scale the overlay to this many px wide (keeps aspect). Typical logo: 8–12% of the video width.
- `fade_in`, `fade_out` — seconds; fades the overlay's alpha (works on opaque mp4 cards too).

Verify every insert in the self-eval: `timeline_view` on the render inside the insert's window — the speaker must still be visible around the text, the text readable, and no subtitle hidden behind it.

## Retention edit: B-roll, effects, animated captions (when requested)

The "high-level edit that holds the viewer" (YouTube/Reels/Shorts). The model can do it — what makes it fail is improvising the look and placing things at random. So: **identity first, a timed plan second, assets third.**

**1. Identity — `<edit>/brand.json`** (schema in `helpers/brand.py`). Ask for the user's colours, logo and fonts, or propose a palette from the material and confirm it. Every generator takes `--brand`, so cards, captions, badges, transitions and generated images share one look.

**2. Plan — `<edit>/plan.md`, approved before building anything** (extends Hard Rule 11). After the cut is decided, read `takes_packed.md` + `timeline.json` (written by every render: each range's output start/end, i.e. the cut times) and write one row per event:

```
| out time | type        | what                                              | why                         |
|----------|-------------|---------------------------------------------------|-----------------------------|
| 0.0–3.0  | badge       | "45" / "NÚMERO"                                   | identifies the candidate    |
| 3.0      | zoom 1.15   | range 2 punch-in                                  | hides the jump cut          |
| 6.2–8.7  | broll card  | gen: "children in a bright classroom raising hands" | he says "educação"          |
| 9.0      | swipe+whoosh| topic change: education → health                  | marks the new chapter       |
| all      | captions    | word-highlight, accent #FFC400                    | sound-off viewers           |
```

Rules of thumb (taste, adjust to the material):
- **B-roll on concrete words** — places, objects, numbers, actions the speaker names — not on abstractions. ~one every 5–10 s in a fast edit, 2–4 s each. `full` when the image IS the point; `card` when the speaker's face matters.
- **Punch-ins alternate** at jump cuts inside one shot (1.0 → 1.12–1.18 → 1.0): the size change hides the jump. Never two consecutive ranges at the same zoom across a cut of the same shot.
- **Transitions only at topic changes**, never on every cut. Swipe/flash + whoosh.
- **SFX tied to visible events**, at most one every ~3 s (see "Music and sound effects").
- **One new thing at a time**: don't land a card, a badge and a transition in the same second. Keep the face clear during the key/emotional lines.
- **Text never goes in generated images** (generators garble letters) — put it in with `text_overlay.py`.

**3. Assets** (after approval; independent assets in parallel via sub-agents/`delegate_task` or one `--plan` call):
- Stills: write `edit/broll_plan.json` and run `gen_image.py --plan edit/broll_plan.json --brand edit/brand.json`. **Look at every image before using it** (open it / `vision_analyze`): AI artifacts — warped hands and faces, fake signs, extra limbs — are the giveaway. Regenerate with a new `--seed` or a more concrete prompt; a so-so image survives better as a small `card` than full-screen.
- Provider: `pollinations` (default, free, medium quality, may ignore details). `vertex` gives much better images (~8 s each) but is **paid, billed to the Google Cloud project of the Jarvis service account (the prefeitura's)**: use it only when the user asks for higher quality, and say how many images it will generate (~US$0.04 each). AI video clips (Veo) are not wired in.
- **Higgsfield (paid, __JARVIS_USER__'s credits — only when he asks for top quality or for AI video clips):** best stills and the only AI *video* clips available here. Use the `higgsfield` skill script with `HF_OUT` pointing at the edit folder so files land next to the project: `set HF_OUT=<edit>\broll` then `python <hermes>\skills\creative\higgsfield\hf.py imagem --prompt "..." --proporcao 16:9` (stills; then `broll.py` as usual) or `hf.py video --prompt "..." --duracao 5` (a ready 5–10 s clip; use it directly as B-roll, `--match`-scale it if the size differs). Image→video needs a **public** image URL, so for local images prefer text→video. Say how many items you will generate before running; each one spends credits. Prompts in English, concrete (subject, style, light, framing).
- Clips: `broll.py` per image (`--match edit/base_preview.mp4` so the size matches), `transition.py` per transition, `sfx.py all --out-dir edit/sfx` once, `captions.py` after the cut is final.
- User-supplied photos, screenshots, logos and footage go through the same tools — prefer them to generated images when they exist.

**4. EDL + render**: `zoom` per range, overlays (B-roll, cards, text, transitions centered on cuts), `sfx`, `"subtitles": "captions.ass"`. Render `--preview`, then self-eval as usual — plus a frame inside every B-roll/card/transition window and a listen-free audio check (levels at each effect, see Self-eval).

## Animations (when requested)

Animations match the content and the brand. **Get the palette, font, and visual language from the conversation** — never assume a default. If the user hasn't told you, propose a palette in the strategy phase and wait for confirmation before building anything.

**Tool options:**

Pick the engine per animation slot. Do not default to Remotion just because the animation is web-adjacent.

- **HyperFrames** — Browser-native HTML/CSS/GSAP video compositions: product UI motion, website-to-video or mockup-to-video captures, kinetic typography, landing-page/storyboard promos, data-driven UI states, transparent WebM overlays, and clips that need deterministic frame capture plus HyperFrames lint/validate/render checks. Best when the animation should be authored and verified like a web composition instead of a React component tree.
- **Remotion** — React/CSS compositions with component state, reusable React primitives, or an existing Remotion brand system. Best when the user specifically asks for React/Remotion or when React composition is the simpler authoring model.
- **Manim** — formal diagrams, state machines, equation derivations, graph morphs. Read `skills/manim-video/SKILL.md` and its references for depth.
- **PIL + PNG sequence + ffmpeg** — simple overlay cards: counters, typewriter text, single bar reveals, progressive draws. Fast to iterate, any aesthetic you want. The launch video used this.

For HyperFrames slots, scaffold the slot inside `edit/animations/slot_<id>/` with `npx --yes hyperframes init . --example blank --non-interactive --skip-skills`, build the HTML composition there, run the HyperFrames checks that fit the slot (`lint`, `validate`, and a draft render when practical), then produce the final overlay video with `npx --yes hyperframes render . -o render.mp4` or `--format webm -o render.webm` when alpha is required. Point the EDL overlay `file` at the actual rendered path.

For Remotion slots, keep the Remotion project isolated inside the same slot directory, scaffold with `npx create-video@latest` or install Remotion locally there, render the composition to `render.mp4` with the project-local `remotion render` command, and verify duration and dimensions with `ffprobe`.

None is mandatory. Invent hybrids if useful (e.g., PIL background with a HyperFrames or Remotion layer on top).

**Duration rules of thumb, context-dependent:**

- **Sync-to-narration explanations.** A viewer needs to parse the content at 1×. Rough floor 3s, typical 5–7s for simple cards, 8–14s for complex diagrams. The launch video shipped at 5–7s per simple card.
- **Beat-synced accents** (music video, fast montage). 0.5–2s is fine — they're visual accents, not information. The "readable at 1×" rule becomes *"recognizable at 1×"*, not *"fully parseable."*
- **Hold the final frame ≥ 1s** before the cut (universal).
- **Over voiceover:** total duration ≥ `narration_length + 1s` (universal).
- **Never parallel-reveal independent elements** — the eye can't track two new things at once. One thing, pause, next thing.

**Animation payoff timing (rule for sync-to-narration):** get the payoff word's timestamp. Start the overlay `reveal_duration` seconds earlier so the landing frame coincides with the spoken payoff word. Without this sync the animation feels disconnected.

**Easing** (universal — never `linear`, it looks robotic):

```python
def ease_out_cubic(t):    return 1 - (1 - t) ** 3
def ease_in_out_cubic(t):
    if t < 0.5: return 4 * t ** 3
    return 1 - (-2 * t + 2) ** 3 / 2
```

`ease_out_cubic` for single reveals (slow landing). `ease_in_out_cubic` for continuous draws.

**Typing text anchor trick:** center on the FULL string's width, not the partial-string width — otherwise text slides left during reveal.

**Example palette** (the launch video — one aesthetic among infinite):
- Background `(10, 10, 10)` near-black
- Accent `#FF5A00` / `(255, 90, 0)` orange
- Labels `(110, 110, 110)` dim gray
- Font: Menlo Bold at `/System/Library/Fonts/Menlo.ttc` (index 1)
- ≤ 2 accent colors, ~40% empty space, minimal chrome
- Result: terminal / retro tech feel

This is one style. If the brand is warm and serif, use that. If it's colorful and playful, use that. If the user handed you a style guide, follow it. If they didn't, propose one and confirm.

**Fonts fail silently.** A web font that didn't load renders in a fallback face with no error — the video ships in "almost Arial". In HyperFrames/Remotion, await the font load and then assert it: `if (!document.fonts.check('700 76px "Inter"')) throw new Error(...)`. In PIL, pass an explicit font path; never rely on the default.

**Worked example — "show the edit" hook** (a launch video for this tool). Instead of a title card, the first 3s visualize the editing itself: each transcript word pops in on its Scribe timestamp, with bars under it drawn from the real audio envelope; a filler ("ummm") grows letter by letter while it is spoken, turns orange and is cut out on screen at the same frame the audio cuts, and the next line lands immediately. It works because the picture is *driven by the same data as the sound* — one composition (Remotion) reads frame-exact word/envelope JSON produced in Python, so nothing can drift. Use the idea whenever the story is "we removed something": make the removal visible.

**Parallel sub-agent brief** — each animation is one sub-agent spawned via the `Agent` tool. Each prompt is self-contained (sub-agents have no parent context). Include:

1. One-sentence goal: *"Build ONE animation: [spec]. Nothing else."*
2. Absolute output path (`<edit>/animations/slot_<id>/render.mp4`)
3. Exact technical spec: resolution, fps, codec, pix_fmt, CRF, duration
4. Style palette as concrete values (RGB tuples, hex, or reference to a design system)
5. Font path with index
6. Frame-by-frame timeline (what happens when, with easing)
7. Anti-list ("no chrome, no extras, no titles unless specified")
8. Code pattern reference (copy helpers inline, don't import across slots)
9. Deliverable checklist (script, render, verify duration via ffprobe, report)
10. **"Do not ask questions. If anything is ambiguous, pick the most obvious interpretation and proceed."**

One sub-agent = one file (unique filenames, parallel agents don't overwrite each other).

## Music and sound effects (when requested)

Sound is where generated videos sound cheap. Worked rules from launch edits:

- **Fewer effects.** Every effect is tied to something visible (a cut, a landing, a click). ~20 stock whooshes/risers/impacts in 18s reads as generic; ~8 reads as designed.
- **Hit on the frame.** Most effects have an attack (silence or a build before the transient). Measure it (first sample above ~-30 dBFS of the peak) and start the file `attack` seconds *before* the visible contact frame.
- **Duck music under speech** (roughly -12 to -15 dB relative to its music-only level), and ramp it out before a stinger or end card instead of letting its own tail decay under your CTA.
- **Master once:** mix to PCM, then two-pass loudnorm (-14 LUFS, true peak ≤ -1 dBTP) on the final mix. Then measure per section (see Self-eval).
- **Music taste is the user's call.** Generated music defaults to "hype"; offer two contrasting beds and let the user listen. Don't claim a mix sounds good — you can only measure it.
- **In the EDL:** `"sfx": [{"file": "sfx/whoosh.wav", "at": 8.63, "gain_db": -8}]` — `at` is output seconds = visible event − the effect's `attack` (printed by `sfx.py`). render.py mixes them under the dialogue (dialogue level untouched) before loudnorm. Starting levels: whoosh -8 dB, pop/click -10 dB, impact -6 dB; then measure.

## Output spec

Match the source unless the user asked for something specific. Common targets: `1920×1080@24` cinematic, `1920×1080@30` screen content, `1080×1920@30` vertical social, `3840×2160@24` 4K cinema, `1080×1080@30` square. `render.py` defaults the scale to 1080p from any source; pass `--filter` or edit the extract command for other targets. Worth asking the user which delivery format matters.

## EDL format

```json
{
  "version": 1,
  "sources": {"C0103": "/abs/path/C0103.MP4", "C0108": "/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "HOOK", "quote": "...", "reason": "Cleanest delivery, stops before slip at 38.46."},
    {"source": "C0108", "start": 14.30, "end": 28.90, "zoom": 1.15, "zoom_center": [0.5, 0.4],
     "beat": "SOLUTION", "quote": "...", "reason": "Only take without the false start."}
  ],
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0},
    {"file": "edit/animations/slot_2/render.mov", "start_in_output": 12.4, "duration": 4.0},
    {"file": "edit/logo.png", "start_in_output": 0.0, "duration": 87.4,
     "x": "W-w-40", "y": "40", "width": 180, "fade_in": 0.5, "fade_out": 0.5}
  ],
  "sfx": [{"file": "sfx/whoosh.wav", "at": 4.06, "gain_db": -8}],
  "subtitles": "edit/master.srt",
  "total_duration_s": 87.4
}
```

`grade` is a preset name or raw ffmpeg filter. `zoom` (1 < z ≤ 2) punches in on that range, `zoom_center` places the crop (fractions; default `[0.5, 0.4]` keeps a face). `overlays` are rendered animation clips or still images, optionally positioned/scaled/faded (see "Text inserts and overlays"). `sfx` are effects mixed under the dialogue. `subtitles` (`.srt` house style, or `.ass` with its own styles) is optional and applied LAST. Each render writes `timeline.json` (output start/end of every range).

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Strategy:** one paragraph describing the approach
**Decisions:** take choices, cuts, grades, animations + why
**Reasoning log:** one-line rationale for non-obvious decisions
**Outstanding:** deferred items
```

On startup, read `project.md` if it exists and summarize the last session in one sentence before asking whether to continue.

## Anti-patterns

Things that consistently fail regardless of style:

- **Hierarchical pre-computed codec formats** with USABILITY / tone tags / shot layers. Over-engineering. Derive from the transcript at decision time.
- **Hand-tuned moment-scoring functions.** The LLM picks better than any heuristic you'll write.
- **Whisper SRT / phrase-level output.** Loses sub-second gap data. Always word-level verbatim.
- **Running Whisper locally on CPU.** Slow and it normalizes fillers. Use hosted Scribe.
- **Burning subtitles into base before compositing overlays.** Overlays hide them. (Hard Rule 1.)
- **Single-pass filtergraph when you have overlays.** Double re-encodes. Use per-segment extract → concat.
- **Linear animation easing.** Looks robotic. Always cubic.
- **An opaque .mp4 for text that should sit over the footage.** It blacks out the frame. Overlays meant to be "on top" need alpha (.mov qtrle/ProRes 4444, VP9 .webm, .png).
- **Unverified web fonts.** A failed load silently falls back to a system face. Assert the font loaded before rendering.
- **Stock SFX on every transition.** Tie each effect to a visible event; cap the count.
- **Hard audio cuts at segment boundaries.** Audible pops. (Hard Rule 3.)
- **Typing text centered on the partial string.** Text slides left as it grows.
- **Sequential sub-agents for multiple animations.** Always parallel.
- **Editing before confirming the strategy.** Never.
- **Re-transcribing cached sources.** Immutable outputs of immutable inputs.
- **Assuming what kind of video it is.** Look first, ask second, edit last.
