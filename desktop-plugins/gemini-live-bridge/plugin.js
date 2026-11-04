/**
 * Gemini Live Bridge — status-bar minibar + backend-proxy voice session
 * Save at: ~/.hermes/desktop-plugins/gemini-live-bridge/plugin.js
 *
 * The renderer NEVER talks to Google. Mic is captured here, PCM chunks are
 * POSTed to the local backend, which owns the upstream Gemini Live WebSocket
 * (ephemeral token, server-side key). Playback: poll the backend for new
 * model-audio chunks and play them via WebAudio.
 *
 * Jarvis fork: /poll also carries `delegation` (delegate_to_hermes running on
 * Hermes); the panel shows the live Hermes turn — running request and result.
 */

import { atom, Button, host, icons, Popover, PopoverContent, PopoverTrigger, STATUSBAR_AREAS, Tip, useValue } from '@hermes/plugin-sdk'
import { useEffect, useId } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'gemini-live-bridge'
const musicOpen = atom(false)
const musicPlayer = atom({ src: '', error: '' })

const CSS = `
/* Transparent conversation beside the portrait. */
.jarvis-captions{display:none;position:fixed;z-index:85;inset:54% 4% 160px;grid-template-columns:minmax(0,1fr) clamp(180px,19vw,320px) minmax(0,1fr);grid-template-rows:minmax(0,1fr);gap:32px;pointer-events:none;font-family:"SF Pro Display","Segoe UI",sans-serif;color:#f7f8f8}
:root[data-jarvis-face] .jarvis-captions{display:grid}
.jarvis-caption{min-width:0;min-height:0;align-self:start;max-height:100%;display:flex;flex-direction:column;gap:12px;background:none;border:0;pointer-events:auto}
.jarvis-caption[hidden]{display:none}
.jarvis-caption--you{grid-column:1;text-align:right}
.jarvis-caption--jarvis{grid-column:3}
.jarvis-caption-label{color:#d0d6e0;font-size:12px;font-weight:500;letter-spacing:.16em;text-transform:uppercase;text-shadow:0 1px 6px #010102}
.jarvis-caption--jarvis .jarvis-caption-label{color:#828fff}
.jarvis-caption-text{margin:0;min-height:0;overflow:auto;overscroll-behavior:contain;scrollbar-width:thin;font-size:clamp(18px,1.5vw,28px);font-weight:400;line-height:1.5;letter-spacing:-.2px;white-space:pre-wrap;overflow-wrap:anywhere;text-shadow:0 2px 5px #010102,0 0 16px #010102}
.jarvis-caption[data-active=true] .jarvis-caption-label::after{content:' ·';color:#828fff}
@media(max-width:760px){.jarvis-captions{inset:18% 16px 300px;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:24px}.jarvis-caption--jarvis{grid-column:2}.jarvis-caption-text{font-size:18px}}
@media(max-height:600px){.jarvis-captions{top:18%;bottom:180px}.jarvis-caption-text{font-size:18px}}
/* Older desktop shells still mount voice controls inside the message form. */
:root[data-jarvis-face] form:has(>input[aria-label="Mensagem para o Jarvis"]):not(.jarvis-face-composer){display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;box-sizing:border-box;width:100%;max-width:min(840px,calc(100vw - 32px));padding:16px;border-radius:16px;background:var(--ui-bg-chrome);border:1px solid var(--ui-stroke-secondary)}
:root[data-jarvis-face] form:has(>input[aria-label="Mensagem para o Jarvis"]):not(.jarvis-face-composer)>input{grid-column:1/-1;box-sizing:border-box;width:100%;min-width:0;min-height:48px;padding:8px 0 12px;border:0;border-bottom:1px solid var(--ui-stroke-secondary);border-radius:0;font-family:inherit;font-size:14px;line-height:24px;background:transparent;color:var(--ui-text-primary)}
:root[data-jarvis-face] form:has(>input[aria-label="Mensagem para o Jarvis"]):not(.jarvis-face-composer)>div{min-width:0;width:100%}
:root[data-jarvis-face] form:has(>input[aria-label="Mensagem para o Jarvis"]):not(.jarvis-face-composer)>button{align-self:end;min-height:36px;border-radius:8px;padding:8px 16px;font-family:inherit;font-size:12px}
:root[data-jarvis-face] .jarvis-face-console{box-sizing:border-box;width:100%;max-width:min(840px,calc(100vw - 32px))}
:root[data-jarvis-face] .jarvis-face-composer{display:grid;grid-template-columns:minmax(0,1fr) auto}
:root[data-jarvis-face] .jarvis-face-composer input{box-sizing:border-box;width:100%;min-width:0;min-height:48px;font-size:14px;line-height:24px}

/* Keep the galaxy visible until the composer is in use. */
:root[data-jarvis-face] :is(.jarvis-face-console,form:has(>input[aria-label="Mensagem para o Jarvis"]):not(.jarvis-face-composer)){background:color-mix(in srgb,var(--ui-bg-chrome) 26%,transparent);border-color:color-mix(in srgb,var(--ui-stroke-secondary) 55%,transparent);opacity:.64;backdrop-filter:blur(10px);transition:background-color 220ms ease,border-color 220ms ease,opacity 220ms ease}
:root[data-jarvis-face] :is(.jarvis-face-console,form:has(>input[aria-label="Mensagem para o Jarvis"]):not(.jarvis-face-composer)):is(:focus-within,:has(input:not(:placeholder-shown))){background:color-mix(in srgb,var(--ui-bg-chrome) 94%,transparent);border-color:var(--ui-stroke-secondary);opacity:1}
@media(prefers-reduced-motion:reduce){:root[data-jarvis-face] :is(.jarvis-face-console,form:has(>input[aria-label="Mensagem para o Jarvis"]):not(.jarvis-face-composer)){transition:none}}

.jarvis-voice-controls{display:flex;align-items:center;gap:8px;flex:1;min-width:0;flex-wrap:wrap;font-family:inherit}
.jarvis-voice-status{flex:1 1 130px;min-width:0}
.jarvis-voice-status>span{font-family:inherit;font-size:12px;font-weight:600;letter-spacing:.08em;color:var(--ui-text-primary)}
.jarvis-voice-status>p{margin:3px 0 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:inherit;font-size:12px;color:var(--ui-text-secondary)}
.jarvis-voice-controls .jarvis-control{min-height:36px;height:auto;border-radius:8px;padding:8px 12px;font-family:inherit;font-size:12px;line-height:18px;color:var(--ui-text-secondary);border:1px solid var(--ui-stroke-secondary);background:transparent;flex-shrink:0}
.jarvis-voice-controls .jarvis-control:hover{background:var(--ui-bg-elevated);color:var(--ui-text-primary)}
.jarvis-voice-controls .jarvis-control[aria-pressed=true]{color:var(--ui-accent);background:color-mix(in srgb,var(--ui-accent) 10%,transparent)}
.jarvis-voice-controls .jarvis-control--primary{color:var(--ui-bg-chrome);background:var(--ui-accent);border-color:transparent;font-weight:600}
.jarvis-voice-controls .jarvis-control--primary:hover{background:var(--ui-accent);filter:brightness(1.1);color:var(--ui-bg-chrome)}
.jarvis-voice-controls button:disabled{opacity:.45}
/* Face and app mount the same contribution; its portal must sit above the face. */
:root[data-jarvis-face] [data-radix-popper-content-wrapper]:has(>.jarvis-voice-tools),
:root:has([aria-label="Face do Jarvis"]) [data-radix-popper-content-wrapper]:has(>.jarvis-voice-tools){z-index:120!important;pointer-events:auto}
.jarvis-voice-tools{box-sizing:border-box;display:flex;flex-direction:column;width:480px;max-width:calc(100vw - 32px);max-height:min(72vh,680px);overflow:hidden;padding:0!important;border:1px solid var(--ui-stroke-secondary);border-radius:12px;background:var(--ui-bg-elevated);color:var(--ui-text-primary);font-family:inherit;box-shadow:0 16px 48px color-mix(in srgb,var(--ui-bg-chrome) 50%,transparent)}
.jarvis-tools-header{display:flex;align-items:center;gap:12px;padding:16px;border-bottom:1px solid var(--ui-stroke-secondary);flex-shrink:0}
.jarvis-tools-header h2{flex:1;min-width:0;margin:0;font-size:14px;font-weight:600}
.jarvis-tools-header button,.jarvis-tool-body button{min-height:36px;border:1px solid var(--ui-stroke-secondary);border-radius:8px;padding:8px 12px;background:transparent;color:var(--ui-text-primary);font:inherit;font-size:12px;cursor:pointer}
.jarvis-tools-list{overflow:auto;padding:8px;min-height:0}
.jarvis-tools-list>button{display:flex;align-items:center;justify-content:space-between;gap:16px;width:100%;min-height:64px;padding:12px;text-align:left;border:0;border-radius:8px;background:transparent;color:var(--ui-text-primary);cursor:pointer;font:inherit}
.jarvis-tools-list>button:hover,.jarvis-tools-header button:hover,.jarvis-tool-body button:hover{background:color-mix(in srgb,var(--ui-accent) 10%,transparent)}
.jarvis-tools-list>button>span:first-child{display:flex;flex-direction:column;gap:4px;min-width:0}
.jarvis-tools-list strong{font-size:13px;font-weight:600}
.jarvis-tools-list>button>span:first-child>span{font-size:12px;line-height:1.5;color:var(--ui-text-secondary)}
.jarvis-tool-count{flex-shrink:0;font-size:12px;color:var(--ui-text-secondary)}
.jarvis-tool-body{min-height:0;overflow:auto;padding:16px;overflow-wrap:anywhere}
.jarvis-tool-content{position:static;width:100%;max-width:none;max-height:none;overflow:visible;color:var(--ui-text-primary);font-size:13px;line-height:1.6}
.jarvis-tool-content>div:first-child:has(>h2){display:none}
.jarvis-tool-body form{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.jarvis-tool-body input:not([type=checkbox]),.jarvis-tool-body textarea,.jarvis-tool-body select{box-sizing:border-box;min-width:0;width:100%;max-width:100%;min-height:36px;padding:8px;border:1px solid var(--ui-stroke-secondary);border-radius:8px;background:var(--ui-bg-input);color:var(--ui-text-primary);font:inherit;font-size:13px}
.jarvis-tool-body label{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px}
.jarvis-tool-body form>label{width:100%}
.jarvis-tool-body label input[type=number],.jarvis-tool-body label input[type=time]{max-width:160px;margin-left:auto}
.jarvis-tool-body input[type=checkbox]{width:16px;height:16px;flex-shrink:0}
.jarvis-tool-body :is(p,h3,ul){margin:8px 0}
.jarvis-tool-body>details{border:0;padding:0;background:transparent}
.jarvis-tool-body>details>summary{display:none}
.jarvis-tool-body .voice-identity-card{background:transparent;border-color:var(--ui-stroke-secondary)}
.jarvis-tool-body .jarvis-tool-voices{box-sizing:border-box;width:100%;max-width:none;max-height:none;overflow:visible;background:transparent}
.jarvis-tool-voices .voice-identity-shell{padding:0}
.jarvis-tool-voices .voice-identity-header{position:static;background:transparent}
.jarvis-tool-body button:disabled{opacity:.5;cursor:default}
.jarvis-tool-body audio{width:100%;max-width:100%}
.jarvis-tool-body pre{max-width:100%;overflow:auto}
.jarvis-file-preview{min-width:0}
.jarvis-file-preview h3{margin:16px 0 8px;font-size:14px;font-weight:600}
.jarvis-file-preview pre{max-height:none!important;margin-top:12px;padding:12px;border:1px solid var(--ui-stroke-secondary);border-radius:8px;background:var(--ui-bg-input);white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.6}
[data-radix-popper-content-wrapper]:has(>.jarvis-music-player){position:fixed!important;inset:0!important;transform:none!important;z-index:110!important;pointer-events:none}
.jarvis-music-player{box-sizing:border-box;position:fixed;pointer-events:auto;right:24px;bottom:176px;width:480px;max-width:calc(100vw - 32px);max-height:calc(100dvh - 200px);overflow:auto;padding:16px;border:1px solid #23252a;border-radius:12px;background:#0f1011;color:#f7f8f8;font:13px/1.5 "SF Pro Display","Segoe UI",sans-serif;box-shadow:0 16px 48px #010102}
.jarvis-music-player header{display:flex;align-items:center;gap:16px;margin-bottom:12px}
.jarvis-music-player h2{flex:1;margin:0;font-size:14px;font-weight:600}
.jarvis-music-player button{min-height:36px;padding:8px 12px;border:1px solid #23252a;border-radius:8px;background:transparent;color:#d0d6e0;font:inherit;cursor:pointer}
.jarvis-music-player iframe{display:block;width:100%;min-height:200px;aspect-ratio:16/9;border:0;border-radius:8px;background:#010102}
.jarvis-music-player p{margin:12px 0;color:#8a8f98}
.jarvis-music-player a{color:#828fff;text-decoration:underline}
.jarvis-music-player :is(button,a):focus-visible{outline:2px solid #828fff;outline-offset:2px}
@media(max-height:640px){.jarvis-music-player{bottom:16px;max-height:calc(100dvh - 32px)}}
.jarvis-voice-controls button:focus-visible,.jarvis-voice-tools :is(button,summary,input):focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
@media(max-width:540px){.jarvis-voice-status{flex-basis:100%}.jarvis-voice-controls{gap:6px}.jarvis-voice-controls .jarvis-control{padding:8px}}

.gemini-live-bar{display:flex;align-items:center;gap:2px;height:100%;color:var(--ui-text-tertiary)}
.gemini-live-bar .gemini-live-name{max-width:96px;overflow:hidden;text-overflow:ellipsis;text-align:left}
.gemini-live-dot{display:inline-block;width:7px;height:7px;border-radius:9999px;background:var(--ui-text-quaternary);flex-shrink:0}
.gemini-live-dot[data-active=true]{background:var(--ui-accent)}
.gemini-live-panel{width:320px;max-width:calc(100vw - 24px);height:auto;overflow:visible}
.gemini-live-status{display:flex;align-items:center;gap:6px;padding:6px 4px;font-size:11px;color:var(--ui-text-secondary)}
.gemini-live-error{font-size:11px;line-height:15px;color:var(--ui-text-secondary);padding:6px 4px;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;max-height:76px;overflow-y:auto}
.gemini-live-transport{display:flex;align-items:center;gap:4px;padding:2px 2px 0}
.gemini-live-transport-space{flex:1}
.gemini-live-flag{font-size:10px;line-height:18px;padding:0 7px;border:1px solid var(--ui-stroke-secondary,var(--border,#333));border-radius:9999px;background:transparent;color:var(--ui-text-secondary,var(--foreground,#ddd));cursor:pointer;white-space:nowrap}
.gemini-live-flag:disabled{opacity:.5;cursor:default}
.gemini-live-usage{font-size:10px;color:var(--ui-text-quaternary,var(--muted-foreground,#999));font-variant-numeric:tabular-nums}
.gemini-live-thinkrow{display:flex;flex-direction:column;align-items:flex-start;gap:3px;padding:2px 2px;margin-top:2px}
.gemini-live-thinkhead{display:flex;align-items:center;gap:6px}
.gemini-live-thinklabel{font-size:10px;color:var(--ui-text-quaternary,var(--muted-foreground,#999))}
.gemini-live-thinkbtn{width:auto;min-width:0;max-width:none;padding:1px 9px;text-align:center;flex:0 0 auto}
.gemini-live-thinklist{position:static;display:flex;flex-direction:column;gap:2px;min-width:118px;background:var(--ui-background,var(--card,#1a1a1a));border:1px solid var(--ui-stroke-secondary,var(--border,#333));border-radius:6px;padding:3px}
.gemini-live-picker{position:relative;max-width:200px}
.gemini-live-model{display:block;width:100%;text-align:right;font-size:10px;color:var(--ui-text-secondary);background:transparent;border:1px solid var(--ui-stroke-secondary);border-radius:4px;padding:1px 5px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:inherit}
.gemini-live-picker-list{position:absolute;right:0;bottom:calc(100% + 4px);z-index:50;min-width:230px;max-height:132px;overflow-y:auto;background:var(--ui-background,var(--card,#1a1a1a));border:1px solid var(--ui-stroke-secondary,var(--border,#333));border-radius:6px;box-shadow:0 8px 24px rgba(0,0,0,.35);padding:3px}
.gemini-live-picker-item{font-size:10px;line-height:22px;padding:0 8px;color:var(--ui-text-secondary,var(--foreground,#ddd));white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer;border-radius:4px}
.gemini-live-picker-item:hover{background:var(--ui-hover,rgba(128,128,128,.15))}
.gemini-live-picker-item--sel{color:var(--ui-accent,var(--accent,#8ab4f8))}
.gemini-live-meter{display:flex;align-items:flex-end;gap:2px;height:16px;padding:0 4px;margin:5px 0 3px}
.gemini-live-meter>span{width:4px;background:var(--ui-accent);border-radius:1px;opacity:.8;min-height:2px}
.gemini-live-deleg{display:flex;align-items:flex-start;gap:6px;font-size:11px;line-height:15px;padding:4px 4px;color:var(--ui-text-secondary);white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
.gemini-live-deleg>span:first-child{flex:0 0 auto}
.gemini-live-deleg[data-status=running]>span:first-child{color:var(--ui-accent)}
.gemini-live-deleg[data-status=done]>span:first-child{color:var(--ui-text-secondary)}
.gemini-live-deleg[data-status=error]>span:first-child{color:#e06c75}
.gemini-live-log{max-height:102px;overflow-y:auto;font-family:var(--font-mono,monospace);font-size:10px;line-height:14px;color:var(--ui-text-quaternary);padding:4px;margin-top:6px;border-top:1px solid var(--ui-stroke-secondary);white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
.gemini-live-log>div:hover{color:var(--ui-text-secondary)}
.voice-identity-panel{width:460px;max-width:calc(100vw - 24px);max-height:82vh;overflow:auto;padding:0!important;font-size:12px;color:#d0d6e0;background:#0f1011}
.voice-identity-shell{display:flex;flex-direction:column;gap:12px;padding:14px}
.voice-identity-header{position:sticky;top:0;z-index:2;padding:8px 0 12px;background:#0f1011;border-bottom:1px solid #23252a}
.voice-identity-header,.voice-identity-row,.voice-identity-section-head,.voice-identity-actions{display:flex;align-items:center;gap:8px}
.voice-identity-header,.voice-identity-section-head{justify-content:space-between}
.voice-identity-title{font-size:14px;font-weight:700;color:var(--ui-text-primary);letter-spacing:.01em}
.voice-identity-subtitle{margin-top:2px;font-size:11px;color:var(--ui-text-quaternary,var(--ui-text-secondary))}
.voice-identity-badge{display:inline-flex;align-items:center;min-height:20px;padding:0 7px;border:1px solid var(--ui-stroke-secondary);border-radius:999px;font-size:10px;white-space:nowrap;color:var(--ui-text-secondary)}
.voice-identity-badge[data-active=true]{border-color:var(--ui-accent);color:var(--ui-accent)}
.voice-identity-card{border:1px solid #23252a;border-radius:12px;padding:12px;background:#141516}
.voice-identity-card--pending{border-color:var(--ui-accent)}
.voice-identity-label{font-size:11px;font-weight:600;color:var(--ui-text-primary)}
.voice-identity-status{margin:7px 0 0;line-height:16px;color:var(--ui-text-secondary);white-space:pre-wrap;overflow-wrap:anywhere}
.voice-identity-muted{margin:5px 0 0;font-size:11px;line-height:15px;color:var(--ui-text-quaternary,var(--ui-text-secondary));white-space:pre-wrap;overflow-wrap:anywhere}
.voice-identity-panel input[type=text]{box-sizing:border-box;width:100%;padding:7px 8px;margin:7px 0 5px;background:var(--ui-background);color:var(--ui-text-primary);border:1px solid var(--ui-stroke-secondary);border-radius:6px}
.voice-identity-panel button,.voice-identity-panel select{margin:0;padding:6px 9px;border:1px solid var(--ui-stroke-secondary);border-radius:6px;background:var(--ui-background);color:var(--ui-text-primary);font:inherit}
.voice-identity-panel button{cursor:pointer}
.voice-identity-panel button:disabled,.voice-identity-panel select:disabled{opacity:.55;cursor:default}
.voice-identity-select{min-width:185px;margin-left:auto!important}
.voice-identity-primary{border-color:var(--ui-accent)!important;color:var(--ui-accent)!important}
.voice-identity-danger{color:#e06c75!important}
.voice-identity-actions{margin-top:9px;flex-wrap:wrap}
.voice-identity-progress{height:7px;margin-top:9px;border-radius:999px;overflow:hidden;background:var(--ui-stroke-secondary)}
.voice-identity-progress>span{display:block;height:100%;min-width:0;border-radius:inherit;background:var(--ui-accent);transition:width .2s ease}
.voice-identity-progress-row{display:flex;justify-content:space-between;gap:10px;margin-top:7px;font-size:11px}
.voice-identity-section{display:flex;flex-direction:column;gap:7px}
.voice-identity-profile,.voice-identity-candidate{border:1px solid #34343a;border-radius:12px;padding:12px;background:#141516}
.voice-identity-candidate[data-suggested=true]{border-color:#5e6ad2}
.voice-identity-candidate-title{font-size:13px;font-weight:600;color:#f7f8f8}
.voice-identity-candidate-meta{font-size:11px;color:#8a8f98}
.voice-identity-candidate-note{margin:8px 0;line-height:1.45;color:#d0d6e0}
.voice-identity-panel button:focus-visible,.voice-identity-panel input:focus-visible,.voice-identity-panel select:focus-visible{outline:2px solid #5e6ad2;outline-offset:2px}
.voice-identity-panel details{border:0}
.voice-identity-panel summary{cursor:pointer;color:var(--ui-text-primary);font-weight:600}
.voice-identity-check{display:flex;align-items:flex-start;gap:6px;margin-top:7px;line-height:15px}
.voice-identity-check input{margin-top:2px}
.voice-identity-help{padding-top:3px;border-top:1px solid var(--ui-stroke-secondary)}
.voice-identity-help summary{font-size:11px;color:var(--ui-text-secondary)}
.voice-identity-panel audio{width:100%;margin-top:2px}
`

function b64FromInt16(pcm) {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength)
  let bin = ''
  const CHUNK = 0x8000
  for (let i = 0; i < bytes.length; i += CHUNK) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK))
  return btoa(bin)
}

function pcm16At16k(input, sourceRate) {
  const targetRate = 16000
  const outputLength = sourceRate === targetRate
    ? input.length
    : Math.max(1, Math.round(input.length * targetRate / sourceRate))
  const pcm = new Int16Array(outputLength)
  for (let i = 0; i < outputLength; i++) {
    const sourceIndex = sourceRate === targetRate ? i : i * sourceRate / targetRate
    const left = Math.min(input.length - 1, Math.floor(sourceIndex))
    const right = Math.min(input.length - 1, left + 1)
    const mix = sourceIndex - left
    const sample = input[left] + (input[right] - input[left]) * mix
    const s = Math.max(-1, Math.min(1, sample))
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff
  }
  return pcm
}

function createMedia(ctx) {
  const empty = () => ({ open: false, busy: false, query: '', results: [], sessions: [], volume: '50', error: '', notice: '', fallback: '' })
  const state = atom(empty())
  let session = ''
  let generation = 0
  const clear = () => { generation++; session = ''; state.set(empty()) }
  const toggle = () => {
    if (state.get().open) { clear(); document.getElementById?.('jarvis-media-toggle')?.focus() }
    else state.set({ ...state.get(), open: true })
  }
  const action = async (name, ref, command) => {
    if (state.get().busy) return
    const started = generation
    const query = state.get().query
    const level = name === 'media.volume' ? Number(state.get().volume) : undefined
    if (name === 'media.volume' && !(Number.isInteger(level) && level >= 0 && level <= 100)) {
      state.set({ ...state.get(), error: 'Volume deve ser um número inteiro de 0 a 100.' })
      return
    }
    state.set({ ...state.get(), busy: true, error: '', notice: '' })
    try {
      if (!session) {
        const result = await ctx.rest('/workspace', { method: 'POST', body: { action: 'start' }, timeoutMs: 5000 })
        if (started !== generation) return
        if (!result.ok) throw new Error(result.error || 'Sessão local indisponível.')
        session = result.session
      }
      const result = await ctx.rest('/workspace', { method: 'POST', body: { action: name, session, query, ref, command, level }, timeoutMs: 15000 })
      if (started !== generation) return
      if (!result.ok) throw new Error(result.error || 'O aplicativo não confirmou o comando.')
      const next = { ...state.get(), notice: result.message || '' }
      if (name === 'videos.search') {
        next.results = result.results || []
        next.fallback = typeof result.fallback === 'string' && result.fallback.startsWith('https://www.youtube.com/results?search_query=') ? result.fallback : ''
        next.notice ||= next.results.length ? 'Escolha um vídeo para abrir.' : 'Nenhum vídeo disponível. Use a busca no navegador.'
      }
      if (name === 'media.status') {
        next.sessions = result.sessions || []
        next.notice = next.sessions.length ? 'Escolha o aplicativo e o comando.' : 'Nenhuma sessão de mídia disponível. Abra um reprodutor.'
      }
      state.set(next)
    } catch (error) {
      if (started === generation) state.set({ ...state.get(), error: error.message || 'Falha no controle de mídia.' })
    } finally {
      if (started === generation) state.set({ ...state.get(), busy: false })
    }
  }
  ctx.onDispose(clear)
  if (typeof host !== 'undefined') {
    for (const scope of [host.state?.profile, host.state?.connectionId, host.state?.focusedSessionId]) {
      if (scope?.listen) ctx.onDispose(scope.listen(clear))
    }
  }
  return { state, toggle, action, query: query => state.set({ ...state.get(), query }),
    volume: volume => state.set({ ...state.get(), volume }) }
}

function createMusic(ctx) {
  const state = atom({ busy: false, playing: false, error: '' })
  let guest = null
  let timer = null
  let deadline = null
  let checking = false
  const stop = (error = '') => {
    clearInterval(timer)
    clearTimeout(deadline)
    timer = deadline = null
    guest?.remove()
    guest = null
    state.set({ busy: false, playing: false, error })
  }
  const toggle = () => {
    if (state.get().busy) return
    if (guest) { stop(); return }
    if (!window.hermesDesktop || !document.body) {
      stop('Música disponível no navegador interno do Hermes Desktop.')
      return
    }
    state.set({ busy: true, playing: false, error: '' })
    const view = document.createElement('webview')
    guest = view
    view.setAttribute('partition', 'persist:hermes-preview')
    view.setAttribute('webpreferences', 'nodeIntegration=no,contextIsolation=yes,sandbox=yes,backgroundThrottling=no,autoplayPolicy=no-user-gesture-required')
    view.setAttribute('aria-hidden', 'true')
    view.setAttribute('tabindex', '-1')
    view.style.cssText = 'position:fixed;left:-10000px;top:0;width:640px;height:360px;pointer-events:none;'
    const check = async () => {
      if (guest !== view || checking) return
      checking = true
      try {
        const playing = await view.executeJavaScript(`(() => {
          const v = document.querySelector('video');
          if (!v) return false;
          v.muted = false;
          if (!v.volume) v.volume = 0.5;
          if (v.paused) v.play().catch(() => {});
          return !v.paused && v.readyState >= 2;
        })()`, true)
        if (guest === view && playing === true) {
          clearInterval(timer)
          clearTimeout(deadline)
          state.set({ busy: false, playing: true, error: '' })
        }
      } catch {
        // Navigation can destroy the initial execution context; retry until deadline.
      } finally { checking = false }
    }
    view.addEventListener('dom-ready', () => {
      if (guest !== view) return
      clearInterval(timer)
      timer = setInterval(() => void check(), 500)
      void check()
    })
    view.addEventListener('did-fail-load', event => {
      if (guest === view && event.isMainFrame !== false && event.errorCode !== -3) stop('Não consegui carregar a música no navegador interno.')
    })
    view.addEventListener('render-process-gone', () => {
      if (guest === view) stop('O navegador interno interrompeu a música.')
    })
    deadline = setTimeout(() => { if (guest === view) stop('A música não iniciou no navegador interno. Tente novamente.') }, 30000)
    view.src = 'https://www.youtube.com/watch?v=tRsQsTMvPNg'
    document.body.append(view)
  }
  ctx.onDispose(() => stop())
  return { state, toggle }
}

function AmbientMusicButton({ live }) {
  const state = useValue(live.music.state)
  return jsxs('div', { children: [
    jsx(Button, { type: 'button', className: 'jarvis-control', disabled: state.busy,
      'aria-label': state.playing ? 'Parar música ambiente' : 'Tocar música ambiente', 'aria-pressed': state.playing,
      onClick: () => void live.music.toggle(), children: state.busy ? 'Aguarde…' : state.playing ? '■ Parar' : '▶ Play' }),
    state.error && jsx('p', { role: 'alert', children: state.error })
  ] })
}

function MediaPanel({ live, embedded = false, onClose }) {
  const media = live.media
  const state = useValue(media.state)
  return jsxs('div', { className: 'shrink-0', children: [
    !embedded && jsx(Button, { id: 'jarvis-media-toggle', type: 'button', variant: 'outline', 'aria-expanded': state.open,
      'aria-controls': 'jarvis-media-panel', onClick: media.toggle,
      className: 'h-7 border-cyan-500/40 px-2 font-mono text-xs text-cyan-300', children: 'Mídia' }),
    state.open && jsxs('section', { id: 'jarvis-media-panel', 'aria-label': 'Mídia',
      className: embedded ? 'jarvis-tool-content' : 'fixed bottom-24 right-6 z-[10002] max-h-[75vh] w-[440px] max-w-[calc(100vw-48px)] overflow-auto rounded-lg border border-cyan-500/40 bg-slate-950 p-4 text-slate-100 shadow-lg',
      onKeyDown: event => { if (!embedded && event.key === 'Escape') media.toggle() }, children: [
        jsxs('div', { className: 'mb-3 flex justify-between', children: [
          jsx('h2', { children: 'Mídia e vídeos' }), jsx('button', { type: 'button', onClick: onClose || media.toggle, children: 'Fechar' })
        ] }),
        jsxs('form', { className: 'flex gap-2', onSubmit: event => { event.preventDefault(); void media.action('videos.search') }, children: [
          jsx('input', { type: 'search', value: state.query, maxLength: 180, autoFocus: true, 'aria-label': 'Buscar vídeo ou música',
            className: 'min-w-0 flex-1 rounded border border-cyan-500/40 bg-transparent px-2 py-1', onChange: event => media.query(event.target.value) }),
          jsx(Button, { type: 'submit', disabled: state.busy || !state.query.trim(), children: 'Buscar vídeo' }),
          jsx(Button, { type: 'button', variant: 'outline', disabled: state.busy || !state.query.trim(),
            onClick: () => void media.action('spotify.play'), children: 'Tocar no Spotify' })
        ] }),
        state.error && jsx('p', { role: 'alert', className: 'mt-2 text-sm', children: state.error }),
        jsx('p', { role: 'status', className: 'my-2 text-xs text-slate-400', children: state.busy ? 'Aguarde…' : state.notice }),
        jsx('ul', { children: state.results.map(row => jsxs('li', { className: 'my-2 rounded border border-cyan-500/20 p-2', children: [
          jsx('p', { className: 'break-words text-sm', children: row.title }),
          jsx('p', { className: 'text-xs text-slate-400', children: `${row.channel} ${row.duration || ''}` }),
          jsx('button', { type: 'button', disabled: state.busy, className: 'mt-2 text-xs text-cyan-300', onClick: () => void media.action('videos.open', row.ref), children: 'Abrir este vídeo' })
        ] }, row.ref)) }),
        state.fallback && jsx('a', { href: state.fallback, target: '_blank', rel: 'noopener noreferrer', className: 'text-xs text-cyan-300', children: 'Busca no navegador' }),
        jsx('h3', { className: 'mt-4 text-sm', children: 'Reprodutores do Windows' }),
        jsx('button', { type: 'button', disabled: state.busy, className: 'my-2 text-xs text-cyan-300', onClick: () => void media.action('media.status'), children: 'Atualizar sessões' }),
        state.sessions.length > 0 && jsxs('label', { className: 'my-2 flex items-center gap-2 text-xs', children: ['Volume do aplicativo (0–100)',
          jsx('input', { type: 'number', min: 0, max: 100, step: 1, value: state.volume, disabled: state.busy, 'aria-label': 'Volume do aplicativo',
            className: 'w-16 rounded border border-cyan-500/40 bg-transparent px-1', onChange: event => media.volume(event.target.value) })] }),
        jsx('ul', { children: state.sessions.map(row => jsxs('li', { className: 'my-2 rounded border border-cyan-500/20 p-2', children: [
          jsx('p', { className: 'break-words text-xs', children: `${row.app} · ${row.status}` }),
          jsxs('div', { className: 'mt-2 flex flex-wrap gap-3 text-xs text-cyan-300', children: [
            ['play', 'Play'], ['pause', 'Pausar'], ['previous', 'Anterior'], ['next', 'Próxima']
          ].map(([command, label]) => jsx('button', { type: 'button', disabled: state.busy,
            onClick: () => void media.action('media.command', row.ref, command), children: label }, command)).concat(
            jsx('button', { type: 'button', disabled: state.busy, onClick: () => void media.action('media.volume', row.ref), children: 'Aplicar volume' }, 'volume')) })
        ] }, row.ref)) }),
        jsx('p', { className: 'mt-3 text-xs text-slate-400', children: 'Play e pausa consultam o estado do aplicativo. Próxima/anterior confirmam apenas a aceitação do comando. O volume muda só este aplicativo no mixer do Windows e exige áudio ativo. Fechar este painel mantém a música.' })
      ] })
  ] })
}


function createMemory(ctx) {
  const empty = () => ({ open: false, busy: false, query: '', inactive: false, results: [], selected: null, ref: '', text: '', reason: '', prepared: null, error: '', notice: '' })
  const state = atom(empty())
  let session = ''
  let generation = 0
  const clear = () => { generation++; session = ''; state.set(empty()) }
  const update = fields => state.set({ ...state.get(), ...fields, prepared: null })
  const toggle = () => {
    if (state.get().open) { clear(); document.getElementById?.('jarvis-memory-toggle')?.focus() }
    else update({ open: true })
  }
  const action = async (name, ref, change) => {
    if (state.get().busy) return
    const before = state.get()
    const started = generation
    if (name === 'memory.confirm' && !before.prepared) return
    state.set({ ...before, busy: true, error: '', notice: '' })
    try {
      if (!session) {
        const opened = await ctx.rest('/workspace', { method: 'POST', body: { action: 'start' }, timeoutMs: 5000 })
        if (started !== generation) return
        if (!opened.ok) throw new Error(opened.error || 'Sessão local indisponível.')
        session = opened.session
      }
      const result = await ctx.rest('/workspace', { method: 'POST', body: {
        action: name, session, query: before.query, include_inactive: before.inactive, ref: ref || before.ref,
        change, text: before.text, reason: before.reason, ticket: before.prepared?.ticket
      }, timeoutMs: 20000 })
      if (started !== generation) return
      if (!result.ok) throw new Error(result.error || 'Operação de memória não confirmada.')
      const next = { ...state.get(), notice: result.notice || result.message || '' }
      if (name === 'memory.search') {
        Object.assign(next, { results: result.results || [], selected: null, ref: '', text: '', reason: '', prepared: null })
        if (result.partial) next.notice += ' Consulta de notas desativadas parcial; refine a busca.'
        else if (!next.results.length) next.notice += ' Nenhuma nota encontrada.'
      } else if (name === 'memory.select') {
        Object.assign(next, { selected: result, ref, text: result.text || '', reason: '', prepared: null })
      } else if (name === 'memory.prepare') next.prepared = result
      else if (name === 'memory.confirm') {
        Object.assign(next, { results: [], selected: null, ref: '', text: '', reason: '', prepared: null })
      }
      state.set(next)
    } catch (error) {
      if (started === generation) state.set({ ...state.get(),
        ...(name === 'memory.confirm' ? { prepared: null, selected: null, ref: '' } : {}),
        error: name === 'memory.confirm' ? 'Não consegui confirmar a revisão. Refaça a busca e confira o histórico antes de repetir.' : error.message || 'Falha na memória local.' })
    } finally {
      if (started === generation) state.set({ ...state.get(), busy: false })
    }
  }
  ctx.onDispose(clear)
  if (typeof host !== 'undefined') {
    for (const scope of [host.state?.profile, host.state?.connectionId, host.state?.focusedSessionId]) {
      if (scope?.listen) ctx.onDispose(scope.listen(clear))
    }
  }
  return { state, toggle, update, action }
}

const MEMORY_STATES = { active: 'Ativa', corrected: 'Corrigida', disabled: 'Desativada', outdated: 'Fonte mudou; revisão anterior não aplicada' }
const MEMORY_CHANGES = { correct: 'Corrigir texto', disable: 'Desativar nas consultas', restore: 'Restaurar original nas consultas' }

function MemoryPanel({ live, embedded = false, onClose }) {
  const memory = live.memory
  const state = useValue(memory.state)
  const selected = state.selected
  return jsxs('div', { className: 'shrink-0', children: [
    !embedded && jsx(Button, { id: 'jarvis-memory-toggle', type: 'button', variant: 'outline', 'aria-expanded': state.open,
      'aria-controls': 'jarvis-memory-panel', onClick: memory.toggle,
      className: 'h-7 border-cyan-500/40 px-2 font-mono text-xs text-cyan-300', children: 'Memória' }),
    state.open && jsxs('section', { id: 'jarvis-memory-panel', 'aria-label': 'Memória local',
      className: embedded ? 'jarvis-tool-content' : 'fixed bottom-24 right-6 z-[10003] max-h-[75vh] w-[520px] max-w-[calc(100vw-48px)] overflow-auto rounded-lg border border-cyan-500/40 bg-slate-950 p-4 text-slate-100 shadow-lg',
      onKeyDown: event => { if (!embedded && event.key === 'Escape') memory.toggle() }, children: [
        jsxs('div', { className: 'mb-3 flex justify-between', children: [jsx('h2', { children: 'Memória e fontes' }),
          jsx('button', { type: 'button', onClick: onClose || memory.toggle, children: 'Fechar' })] }),
        jsxs('form', { className: 'flex gap-2', onSubmit: event => { event.preventDefault(); void memory.action('memory.search') }, children: [
          jsx('input', { type: 'search', maxLength: 150, value: state.query, disabled: state.busy, autoFocus: true, 'aria-label': 'Buscar memórias',
            className: 'min-w-0 flex-1 rounded border border-cyan-500/40 bg-transparent px-2 py-1',
            onChange: event => memory.update({ query: event.target.value, selected: null, ref: '' }) }),
          jsx(Button, { type: 'submit', disabled: state.busy || !state.query.trim(), children: 'Buscar' })] }),
        jsxs('label', { className: 'my-2 flex gap-2 text-xs', children: [
          jsx('input', { type: 'checkbox', checked: state.inactive, disabled: state.busy,
            onChange: event => memory.update({ inactive: event.target.checked }) }), 'Incluir notas desativadas'] }),
        state.error && jsx('p', { role: 'alert', className: 'my-2 text-sm', children: state.error }),
        jsx('p', { role: 'status', className: 'my-2 text-xs text-slate-400', children: state.busy ? 'Aguarde…' : state.notice }),
        jsx('ul', { children: state.results.map(row => jsxs('li', { className: 'my-2 rounded border border-cyan-500/20 p-2', children: [
          jsx('button', { type: 'button', disabled: state.busy, className: 'text-left text-sm text-cyan-300',
            onClick: () => void memory.action('memory.select', row.ref), children: row.title }),
          jsx('p', { className: 'break-words text-xs text-slate-400', children: `${row.source} · ${MEMORY_STATES[row.state] || row.state}` }),
          row.date && jsx('p', { className: 'text-xs text-slate-400', children: row.date }),
          jsx('p', { className: 'mt-1 whitespace-pre-wrap break-words text-xs', children: row.excerpt })
        ] }, row.ref)) }),
        selected && jsxs('div', { className: 'mt-3 border-t border-cyan-500/20 pt-3', children: [
          jsx('h3', { className: 'break-words text-sm', children: selected.source }),
          jsx('p', { className: 'my-2 text-xs text-slate-400', children: `${MEMORY_STATES[selected.state]}. A revisão afeta esta nota inteira nas consultas. O arquivo original permanece.` }),
          jsx('textarea', { 'aria-label': 'Texto da nota para revisão', value: state.text, maxLength: 4000, rows: 8,
            readOnly: !selected.editable || state.busy, className: 'w-full rounded border border-cyan-500/30 bg-transparent p-2 text-xs',
            onChange: event => memory.update({ text: event.target.value }) }),
          !selected.editable && jsx('p', { className: 'my-2 text-xs text-slate-400', children: 'Correção textual indisponível: nota longa ou com dados protegidos. Use o editor local.' }),
          jsx('input', { type: 'text', 'aria-label': 'Motivo da revisão', placeholder: 'Motivo da revisão', maxLength: 300,
            value: state.reason, disabled: state.busy, className: 'my-2 w-full rounded border border-cyan-500/30 bg-transparent p-2 text-xs',
            onChange: event => memory.update({ reason: event.target.value }) }),
          jsx('p', { className: 'my-2 text-xs text-slate-400', children: 'Não inclua senhas, documentos pessoais ou dados de saúde. Nenhuma sugestão é salva sem confirmação.' }),
          jsxs('div', { className: 'flex flex-wrap gap-3 text-xs text-cyan-300', children: [
            jsx('button', { type: 'button', disabled: state.busy || !selected.editable || !state.reason.trim() || !state.text.trim(),
              onClick: () => void memory.action('memory.prepare', null, 'correct'), children: 'Revisar correção' }),
            jsx('button', { type: 'button', disabled: state.busy || !state.reason.trim() || selected.state === 'disabled',
              onClick: () => void memory.action('memory.prepare', null, 'disable'), children: 'Desativar nota' }),
            jsx('button', { type: 'button', disabled: state.busy || !state.reason.trim() || selected.state === 'active',
              onClick: () => void memory.action('memory.prepare', null, 'restore'), children: 'Restaurar original' })] }),
          state.prepared && jsxs('div', { className: 'my-3 rounded border border-cyan-500/50 p-3', children: [
            jsx('h4', { children: MEMORY_CHANGES[state.prepared.change] }),
            jsx('p', { className: 'break-words text-xs', children: state.prepared.source }),
            jsx('p', { className: 'my-2 text-xs', children: state.prepared.reason }),
            state.prepared.change === 'correct' && jsx('pre', { className: 'max-h-40 overflow-auto whitespace-pre-wrap break-words text-xs', children: state.prepared.text }),
            jsx('p', { className: 'my-2 text-xs text-slate-400', children: state.prepared.notice }),
            jsx(Button, { type: 'button', disabled: state.busy, onClick: () => void memory.action('memory.confirm'), children: 'Confirmar revisão' }),
            jsx('button', { type: 'button', disabled: state.busy, className: 'ml-3 text-xs', onClick: () => memory.update({}), children: 'Voltar' })] }),
          jsx('h4', { className: 'mt-3 text-xs', children: 'Histórico de revisões' }),
          jsx('ul', { className: 'text-xs text-slate-400', children: selected.history.map(row => jsx('li', {
            className: 'my-1 break-words', children: `${row.at} · ${MEMORY_CHANGES[row.action]} · ${row.reason} · ${row.source}`
          }, row.source)) })
        ] })
      ] })
  ] })
}

function createLive(ctx) {
  const toolsOpen = atom(false)
  const toolsOwner = atom(null)
  const toolsPage = atom('')
  const memory = createMemory(ctx)
  const media = createMedia(ctx)
  const music = createMusic(ctx)
  const filesUI = atom({ open: false, query: '', busy: false, results: [], preview: null, error: '', notice: '' })
  let filesSession = ''
  let filesGeneration = 0
  const setFilesQuery = query => filesUI.set({ ...filesUI.get(), query })
  const filesAction = async (action, ref) => {
    if (action === 'files.cancel') {
      if (!filesSession) return
      try {
        const result = await ctx.rest('/workspace', { method: 'POST', body: { action, session: filesSession }, timeoutMs: 4000 })
        if (!result.ok) throw new Error('Cancelamento não confirmado.')
      } catch {
        filesUI.set({ ...filesUI.get(), error: 'Não consegui confirmar o cancelamento.' })
      }
      return
    }
    if (filesUI.get().busy) return
    const generation = filesGeneration
    const state = filesUI.get()
    filesUI.set({ ...state, busy: true, error: '', notice: '' })
    try {
      if (!filesSession) {
        const started = await ctx.rest('/workspace', { method: 'POST', body: { action: 'start' }, timeoutMs: 5000 })
        if (generation !== filesGeneration) return
        if (!started.ok) throw new Error(started.error || 'Não consegui abrir a sessão local.')
        filesSession = started.session
      }
      const result = await ctx.rest('/workspace', { method: 'POST',
        body: { action, session: filesSession, query: state.query, ref }, timeoutMs: 10000 })
      if (generation !== filesGeneration) return
      if (!result.ok) throw new Error(result.error || 'Não consegui concluir a operação local.')
      const next = { ...filesUI.get() }
      if (action === 'files.search') {
        next.results = result.results || []
        next.preview = null
        next.notice = result.cancelled ? 'Busca cancelada; resultados parciais.'
          : result.partial || result.unreadable ? 'Busca parcial. Refine o nome para encontrar outros arquivos.'
          : next.results.length ? `${next.results.length} arquivo(s), mais recentes primeiro.` : 'Nenhum arquivo encontrado nas pastas permitidas.'
      } else if (action === 'files.preview') next.preview = result
      else next.notice = result.message || ''
      filesUI.set(next)
    } catch (error) {
      if (generation === filesGeneration) filesUI.set({ ...filesUI.get(), error: error.message || 'Falha no painel local.' })
    } finally {
      if (generation === filesGeneration) filesUI.set({ ...filesUI.get(), busy: false })
    }
  }
  const filesToggle = () => {
    const state = filesUI.get()
    if (state.open) {
      if (filesSession) void ctx.rest('/workspace', { method: 'POST',
        body: { action: 'files.cancel', session: filesSession }, timeoutMs: 4000 }).catch(() => {})
      filesGeneration++
      filesSession = ''
      filesUI.set({ ...state, open: false, busy: false, results: [], preview: null, error: '' })
      document.getElementById?.('jarvis-files-toggle')?.focus()
    } else filesUI.set({ ...state, open: true })
  }
  const setToolsPage = page => {
    const previous = toolsPage.get()
    if (previous === page) return
    if (filesUI.get().open) filesToggle()
    if (media.state.get().open) media.toggle()
    if (memory.state.get().open) memory.toggle()
    voiceSample.set('')
    voiceSampleId.set('')
    voiceSampleInfo.set(null)
    toolsPage.set(page)
    if (page === 'files') filesToggle()
    if (page === 'media') media.toggle()
    if (page === 'memory') memory.toggle()
    if (!page && previous) setTimeout(() => document.getElementById?.('jarvis-tool-' + previous)?.focus(), 0)
  }
  const resetTools = () => { setToolsPage(''); toolsOpen.set(false); toolsOwner.set(null) }
  ctx.onDispose(resetTools)
  if (typeof host !== 'undefined') {
    for (const scope of [host.state?.profile, host.state?.connectionId, host.state?.focusedSessionId]) {
      if (scope?.listen) ctx.onDispose(scope.listen(resetTools))
    }
  }
  ctx.onDispose(() => { if (filesUI.get().open) filesToggle() })
  if (typeof host !== 'undefined') {
    for (const scope of [host.state?.profile, host.state?.connectionId, host.state?.focusedSessionId]) {
      if (!scope?.listen) continue
      ctx.onDispose(scope.listen(() => {
        filesGeneration++
        filesSession = ''
        filesUI.set({ open: false, query: '', busy: false, results: [], preview: null, error: '', notice: '' })
      }))
    }
  }
  const status = atom('idle') // idle | connecting | live | error
  const message = atom('')
  const transcript = atom([])
  const captions = atom({ you: '', jarvis: '', active: '' })
  let userEnded = true
  let outputEnded = true
  let scheduledCaptions = []
  function caption(role, text, replace = false) {
    if (!text) return
    const previous = captions.get()
    const value = replace ? text : previous[role] + text
    if (previous[role] !== value || previous.active !== role) captions.set({ ...previous, [role]: value, active: role })
  }
  function clearCaptions() {
    scheduledCaptions = []
    if (captions.get().you || captions.get().jarvis) captions.set({ you: '', jarvis: '', active: '' })
    userEnded = outputEnded = true
  }
  const level = atom(0)
  const modelLabel = atom('…')
  const models = atom([])        // candidates from /config (short names)
  const chosen = atom('')        // selected model; '' migrates to first candidate
  const pickerOpen = atom(false)
  const thinkLevel = atom('low') // only relevant for extended-thinking
  const thinkOpen = atom(false)
  const usage = atom('') // session token meter, e.g. '23.4k'
  const delegation = atom(null)  // Hermes delegate_to_hermes turn status from /poll
  const taskHistory = atom({ tasks: [], error: '', loaded: false })
  let taskHistorySnapshot = ''
  let taskHistoryGeneration = 0
  function syncTaskHistory(value, generation) {
    if (generation !== taskHistoryGeneration || !value || !Array.isArray(value.tasks)) return
    const snapshot = JSON.stringify(value)
    if (snapshot !== taskHistorySnapshot) {
      taskHistorySnapshot = snapshot
      taskHistory.set({ ...value, loaded: true })
    }
  }
  if (typeof host !== 'undefined') {
    for (const scope of [host.state?.profile, host.state?.connectionId, host.state?.focusedSessionId]) {
      if (scope?.listen) ctx.onDispose(scope.listen(() => {
        clearCaptions()
        taskHistoryGeneration++
        proactivityGeneration++
        proactivitySnapshot = ''
        proactivity.set({ draft: null, config: null, notice: null, busy: false, dirty: false, error: '' })
        taskHistorySnapshot = ''
        taskHistory.set({ tasks: [], error: '', loaded: false })
      }))
    }
  }
  const proactivity = atom({ draft: null, config: null, notice: null, busy: false, dirty: false, error: '' })
  let proactivityGeneration = 0
  let proactivitySnapshot = ''
  function syncProactivity(value, scope, generation) {
    if (!value || scope !== taskHistoryGeneration || generation !== proactivityGeneration || proactivity.get().busy) return
    const current = proactivity.get()
    const snapshot = JSON.stringify(value)
    if (snapshot === proactivitySnapshot && !current.error) return
    proactivitySnapshot = snapshot
    proactivity.set(value.ok ? { ...current, ...value, draft: current.dirty ? current.draft : value.config, error: '' }
      : { ...current, notice: null, error: value.error || 'Proatividade indisponível.' })
  }
  function editProactivity(change) {
    proactivity.set({ ...proactivity.get(), draft: { ...proactivity.get().draft, ...change }, dirty: true, error: '' })
  }
  async function proactivityAction(action) {
    const current = proactivity.get()
    if (current.busy || (action === 'save' && !current.draft)) return
    const generation = ++proactivityGeneration
    proactivity.set({ ...current, busy: true, error: '' })
    try {
      const result = await ctx.rest('/proactivity', { method: 'POST', timeoutMs: 5000,
        body: action === 'save' ? { action, config: current.draft } : { action, id: current.notice?.id } })
      if (generation !== proactivityGeneration) return
      proactivity.set(result.ok ? { ...result, draft: result.config, dirty: false, busy: false, error: '' }
        : { ...proactivity.get(), busy: false, error: result.error || 'Não foi possível salvar.' })
    } catch {
      if (generation === proactivityGeneration) proactivity.set({ ...proactivity.get(), busy: false,
        error: 'Sem confirmação do backend. Confira o estado antes de repetir.' })
    }
  }
  const voiceIdentity = atom({ mode: 'off', profiles: [], candidates: [] })
  const voiceError = atom('')
  const voiceSample = atom('')
  const voiceSampleId = atom('')
  const voiceSampleInfo = atom(null)
  const voiceNotice = atom('')
  const enrollmentName = atom('')
  const enrollmentConsent = atom(false)
  const draftCandidateId = atom('')
  const voiceBusy = atom(false)
  const sentinel = atom(false)    // jarvis sentinel (ambient listening) alive?
  const captureMuted = atom(false)
  const captureBusy = atom(false)
  const listenOnly = atom(false)
  const listenOnlyBusy = atom(false)
  const meetingPrompt = atom(null) // "encerrar a reunião?" (2 min de silêncio) vindo da página de reuniões
  const meetingNames = atom('')
  const meetingBusy = atom(false)
  const meetingError = atom('')
  const log = atom([])
  let stream = null
  let processor = null
  let srcCtx = null
  let playCtx = null
  let playAnalyser = null
  const playbackSamples = new Uint8Array(512)
  let stopped = true
  let pollTimer = null
  let playHead = 0 // sequential audio queue clock (AudioContext time)
  const playbackSources = new Set()
  let interruptSeq = null
  let micQueue = Promise.resolve() // keeps chunk order
  let enrollmentStream = null
  let enrollmentProcessor = null
  let enrollmentCtx = null
  let enrollmentPollTimer = null
  let enrollmentDeadlineTimer = null
  let enrollmentQueue = Promise.resolve()
  let enrollmentActive = false
  let enrollmentLastFrameAt = 0
  let enrollmentGeneration = 0
  const ENROLLMENT_BATCH_SAMPLES = 16000 * 21 / 4 // 5.25s leaves room for VAD edge trimming
  let enrollmentPcm = new Int16Array(ENROLLMENT_BATCH_SAMPLES)
  let enrollmentPcmLength = 0
  let off = { i: 0, o: 0, a: 0 }   // poll offsets
  let pollBusy = false
  let pollGeneration = 0
  let delegSeen = '' // last delegation key already logged (status+request)
  let wakeSeq = null    // last wake counter seen from the sentinel channel
  let wakeTimer = null  // idle poll: watches /poll.wake_seq when no session is live
  let lastActivity = 0  // last sign of life: user speech, gemini audio, hermes turn
  let farewellSent = false
  let farewellBuffer = ''

  const SILENCE_CLOSE_MS = 30000 // total dead-air budget before auto-farewell
  // A fala inteira precisa ser uma despedida/encerramento (ap\u00f3s remover vocativos e
  // cortesias); "encerrar a sess\u00e3o do Zoom" ou "encerrar reuni\u00e3o" n\u00e3o casam.
  const FAREWELL_LEAD = /^(?:(?:jarvis|ok|entao|pronto|certo|bom|bem|obrigado|obrigada|valeu|por favor)\s+)+/
  const FAREWELL_TAIL = /(?:\s+(?:jarvis|por favor|agora|obrigado|obrigada|valeu))+$/
  const FAREWELL_CORE = new RegExp('^(?:' + [
    'tchau(?: tchau)?',
    'adeus',
    'ate (?:mais|logo|amanha|breve|a proxima)',
    'tenha um bom dia',
    '(?:pode )?(?:encerrar|encerra|finalizar|finaliza|terminar|termina|fechar|fecha|desligar|desliga)' +
      '(?: (?:a|essa|esta|nossa|o))? (?:sessao|conversa|chamada|ligacao|conexao|papo)',
    '(?:encerrar|encerra|fim)(?: de)? (?:sessao|conversa)',
    'pode (?:dormir|descansar|encerrar|sair)',
    '(?:e|era) so isso',
    'por hoje (?:e so|e tudo|chega)',
  ].join('|') + ')$')

  const addLog = msg => log.set([...log.get().slice(-40), { t: new Date().toLocaleTimeString(), msg }])

  function armActivity() { lastActivity = Date.now() }

  // Face do Jarvis: publica o estado da voz (idle/connecting/live/error, falando/ouvindo, pedido em
  // andamento e pedido aguardando confirmação) para a UI, e aceita a decisão vinda da face: o painel
  // Vozes fica embaixo da face em tela cheia e não dá para clicar nele.
  let lastVoiceKey = ''
  const mouthTimer = setInterval(() => {
    if (playCtx) {
      while (scheduledCaptions.length && scheduledCaptions[0].at <= playCtx.currentTime) {
        const cue = scheduledCaptions.shift()
        caption('jarvis', cue.text, cue.replace)
      }
      if (playHead > playCtx.currentTime) armActivity()
    }
    if (Date.now() - lastActivity >= SILENCE_CLOSE_MS) clearCaptions()
    let level = null
    if (playAnalyser && playCtx) {
      playAnalyser.getByteTimeDomainData(playbackSamples)
      let energy = 0
      for (const sample of playbackSamples) energy += ((sample - 128) / 128) ** 2
      level = playHead > playCtx.currentTime ? Math.min(1, Math.sqrt(energy / playbackSamples.length) * 5) : 0
    }
    window.dispatchEvent(new CustomEvent('jarvis:voice-level', { detail: { level } }))
  }, 50)
  ctx.onDispose(() => clearInterval(mouthTimer))
  const voiceStateTimer = setInterval(() => {
    const speaking = !!playCtx && playHead > playCtx.currentTime
    const listening = status.get() === 'live' && !speaking && level.get() > 8
    const deleg = delegation.get()
    const pend = voiceIdentity.get().pending || null
    const key = `${status.get()}|${speaking}|${listening}|${deleg?.status}|${deleg?.request}|${pend?.id}|${pend?.remaining_s}`
    if (key === lastVoiceKey) return
    lastVoiceKey = key
    window.dispatchEvent(new CustomEvent('jarvis:voice', { detail: {
      status: status.get(), speaking, listening,
      delegation: deleg ? { status: deleg.status, request: String(deleg.request || '') } : null,
      pending: pend ? { id: pend.id, digest: pend.digest, request: String(pend.request || ''), remaining_s: pend.remaining_s } : null
    } }))
  }, 150)
  ctx.onDispose(() => clearInterval(voiceStateTimer))
  window.addEventListener('jarvis:decide', event => {
    const d = event.detail
    if (d && typeof d.id === 'string' && typeof d.digest === 'string' && typeof d.allow === 'boolean') {
      void voiceAction('decision', { id: d.id, digest: d.digest, allow: d.allow })
    }
  })

  function isFarewellPhrase(text) {
    const normalized = String(text || '')
      .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
      .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().replace(/\s+/g, ' ')
    const core = normalized.replace(FAREWELL_LEAD, '').replace(FAREWELL_TAIL, '')
    return FAREWELL_CORE.test(core)
  }

  function startWakeWatch() {
    // Sentinel watch: the jarvis listener POSTs /wake on the backend's fixed
    // channel; while idle we poll cheaply and auto-open the Live session.
    if (wakeTimer) return
    void ctx.rest('/warmup', { method: 'POST', timeoutMs: 5000 }).catch(() => {})
    wakeTimer = setInterval(async () => {
      const st = status.get()
      if (enrollmentActive) return
      if (st !== 'idle' && st !== 'error') return
      try {
        const taskGeneration = taskHistoryGeneration
        const proactiveGeneration = proactivityGeneration
        const p = await ctx.rest('/poll?in_offset=0&out_offset=0&a_offset=0', { timeoutMs: 2500 })
        if (!listenOnlyBusy.get() && typeof p.listen_only === 'boolean') syncListenOnly(p.listen_only)
        syncSentinel(p.capture_paused === true ? { ...p.sentinel, muted: true } : p.sentinel)
        if (p.voice_identity) voiceIdentity.set(p.voice_identity)
        syncMeetingPrompt(p.meeting_prompt)
        syncTaskHistory(p.task_history, taskGeneration)
        syncProactivity(p.proactivity, taskGeneration, proactiveGeneration)
        if (typeof p.wake_seq === 'number' && p.wake_seq !== wakeSeq) {
          const known = wakeSeq !== null
          wakeSeq = p.wake_seq
          if (known && !enrollmentActive && !listenOnly.get()) {
            addLog(`wake word detectada — abrindo sessão`)
            void start()
          }
        }
      } catch { /* backend busy/starting — next tick retries */ }
    }, 1200)
  }

  function syncMeetingPrompt(prompt) {
    const current = meetingPrompt.get()
    if (prompt && (!current || current.meeting.id !== prompt.meeting.id)) {
      meetingNames.set((prompt.meeting.participants || []).join(', '))
      meetingError.set('')
      addLog('reunião em silêncio há 2 min — aguardando confirmação')
    }
    meetingPrompt.set(prompt || null)
  }

  async function meetingAction(action) {
    const prompt = meetingPrompt.get()
    if (!prompt || meetingBusy.get()) return
    meetingBusy.set(true)
    meetingError.set('')
    try {
      const participants = meetingNames.get().split(',').map(s => s.trim()).filter(Boolean)
      const r = await ctx.rest('/meeting-action', { method: 'POST', timeoutMs: 95000,
        body: { action, meeting_id: prompt.meeting.id, participants } })
      if (!r.ok) throw new Error(r.error || 'falha')
      if (action !== 'open_page') meetingPrompt.set(null)
    } catch (err) {
      meetingError.set('Não foi possível concluir: ' + (err && err.message ? err.message : err))
    } finally {
      meetingBusy.set(false)
    }
  }

  function playPcm24k(b64) {
    if (!playCtx || listenOnly.get()) return
    const bin = atob(b64)
    const pcm = new Int16Array(bin.length / 2)
    for (let i = 0; i < pcm.length; i++) pcm[i] = (bin.charCodeAt(2 * i) & 0xff) | ((bin.charCodeAt(2 * i + 1) & 0xff) << 8)
    const buf = playCtx.createBuffer(1, pcm.length, 24000)
    buf.getChannelData(0).set(new Float32Array(pcm.length).map((_, i) => pcm[i] / 32768))
    const src = playCtx.createBufferSource()
    src.buffer = buf
    src.connect(playAnalyser || playCtx.destination)
    playbackSources.add(src)
    src.onended = () => playbackSources.delete(src)
    // Schedule SEQUENTIALLY — chunks from /poll arrive in bursts; starting
    // them all 'now' overlaps and chops. Queue after the previous one instead.
    const now = playCtx.currentTime
    if (playHead < now + 0.02) playHead = now + 0.02 // resync after silence
    src.start(playHead)
    playHead += buf.duration
  }

  function stopPolling() {
    pollGeneration++
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  }

  function startPolling() {
    stopPolling()
    pollTimer = setInterval(async () => {
      if (stopped || pollBusy) return
      pollBusy = true
      const generation = pollGeneration
      const taskGeneration = taskHistoryGeneration
      const proactiveGeneration = proactivityGeneration
      try {
        const p = await ctx.rest(`/poll?in_offset=${off.i}&out_offset=${off.o}&a_offset=${off.a}`)
        if (stopped || generation !== pollGeneration) return
        if (typeof p.interrupt_seq === 'number' && p.interrupt_seq !== interruptSeq) {
          if (interruptSeq !== null) {
            for (const source of playbackSources) { try { source.stop?.() } catch {} }
            playbackSources.clear()
            playHead = playCtx?.currentTime || 0
            scheduledCaptions = []
          }
          interruptSeq = p.interrupt_seq
        }
        if (!listenOnlyBusy.get() && typeof p.listen_only === 'boolean') syncListenOnly(p.listen_only)
        syncSentinel(p.capture_paused === true ? { ...p.sentinel, muted: true } : p.sentinel)
        if (stopped || listenOnly.get() || captureMuted.get()) return
        if (p.voice_identity) voiceIdentity.set(p.voice_identity)
        syncMeetingPrompt(p.meeting_prompt)
        if (p.usage && p.usage.totalTokenCount != null) {
          const t = p.usage.totalTokenCount
          usage.set(t >= 1000 ? `${(t / 1000).toFixed(1)}k` : String(t))
        }
        off.i = p.in_total ?? off.i
        off.o = p.out_total ?? off.o
        off.a = p.a_total ?? off.a
        if (typeof p.wake_seq === 'number') wakeSeq = p.wake_seq
        // Silence clock: any sign of life (user mic, Gemini audio/text, a
        // running Hermes turn) pushes the auto-farewell away.
        if (level.get() > 8 || (playCtx && playHead > playCtx.currentTime)) armActivity()
        if (p.input?.length || p.output?.length || p.audio?.length) armActivity()
        if (p.interaction === 'IN_PROGRESS') armActivity()
        syncTaskHistory(p.task_history, taskGeneration)
        syncProactivity(p.proactivity, taskGeneration, proactiveGeneration)
        const d = p.delegation
        if (d && (d.status === 'running' || d.status === 'confirming')) armActivity()
        if (p.voice_identity?.enrollment?.state === 'recording' || p.voice_identity?.pending) armActivity()
        if (status.get() === 'live' && !farewellSent && Date.now() - lastActivity >= SILENCE_CLOSE_MS) {
          farewellSent = true
          addLog('30s de silêncio — retomando escuta ambiente')
          clearCaptions()
          stop()
          return
        }
        if (p.error) {
          message.set(p.error); addLog(`error: ${p.error}`)
          if (p.local_captions && /síntese F5 falhou/.test(p.error)) {
            const reply = d?.reply || p.output?.at(-1)
            if (reply) { scheduledCaptions = []; caption('jarvis', reply, true) }
          }
        }
        if (typeof p.input_partial === 'string' && p.input_partial) caption('you', p.input_partial, true)
        for (const t of p.input) {
          transcript.set([...transcript.get().slice(-80), t === '—' ? { role: 'sep', text: '' } : { role: 'you', text: t }])
          if (t === '—') {
            userEnded = true
            farewellBuffer = ''
            continue
          }
          farewellBuffer = `${farewellBuffer} ${t}`.trim().slice(-160)
          caption('you', t, userEnded || p.local_captions === true)
          userEnded = false
          outputEnded = true
          if (!farewellSent && status.get() === 'live' && isFarewellPhrase(farewellBuffer)) {
            farewellSent = true
            addLog('despedida detectada — encerrando conversa')
            stopMic()
            void ctx.rest('/farewell?explicit=true', { method: 'POST' }).catch(() => stop())
          }
        }
        for (const t of p.output) transcript.set([...transcript.get().slice(-80), { role: 'gemini', text: t }])
        if (!p.local_captions) for (const t of p.output) {
          caption('jarvis', t, outputEnded)
          outputEnded = false
          userEnded = true
        }
        for (let index = 0; index < p.audio.length; index++) {
          const offset = (p.a_total - p.audio.length) + index
          const cue = p.speech_cues?.find(cue => cue.offset === offset)
          playPcm24k(p.audio[index])
          if (cue && playCtx) {
            const at = Math.max(playCtx.currentTime, playHead - .1)
            const words = cue.text.match(/\S+\s*/g) || []
            words.forEach((word, wordIndex) => scheduledCaptions.push({
              at: at + cue.duration * wordIndex / words.length,
              text: (wordIndex === 0 && !cue.first ? ' ' : '') + word,
              replace: cue.first && wordIndex === 0
            }))
          }
        }
        // Hermes delegation status (delegate_to_hermes) — surface + log transitions
        if (d && d.status) {
          const key = `${d.status}|${d.request}`
          if (key !== delegSeen) {
            delegSeen = key
            if (d.status === 'running') addLog(`hermes ▶ ${d.request}`)
            if (d.status === 'done') addLog(`hermes ✓ (${d.elapsed ?? '?'}s) ${(d.reply || '').slice(0, 110)}`)
            // Ordinary speech stays in captions; only explicit cards reach the card layer.
            if (d.status === 'done' && /::card\{/.test(d.reply || '')) window.dispatchEvent(new CustomEvent('jarvis:reply', { detail: { request: d.request, text: d.reply || '' } }))
            if (d.status === 'done' && !p.local_captions && !p.output.length) caption('jarvis', d.reply || '', true)
            if (d.status === 'error') addLog(`hermes ✗ ${d.reply || ''}`)
          }
          delegation.set(d)
        }
        if (p.session !== 'live' && !stopped) {
          if (farewellSent) {
            stopped = true
            status.set('idle')
            message.set('')
            stopMic()
            stopPolling()
            if (playCtx) void playCtx.close()
            playCtx = null
            return
          }
          // upstream dropped — surface honestly, keep UI state
          addLog('upstream session closed')
          status.set('error')
          message.set(p.error || 'Session dropped — press start again')
          stopMic()
          stopPolling()
        }
      } catch { /* transient — next tick retries */ }
      finally { pollBusy = false }
    }, 300)
  }

  function stopMic() {
    try { processor && (processor.onaudioprocess = null, processor.disconnect()) } catch {}
    try { stream && stream.getTracks().forEach(t => t.stop()) } catch {}
    try { srcCtx && srcCtx.close() } catch {}
    processor = null; stream = null; srcCtx = null
    level.set(0)
  }

  function stopEnrollmentCapture() {
    enrollmentGeneration++
    try { enrollmentProcessor && (enrollmentProcessor.onaudioprocess = null, enrollmentProcessor.disconnect()) } catch {}
    try { enrollmentStream && enrollmentStream.getTracks().forEach(t => t.stop()) } catch {}
    try { enrollmentCtx && enrollmentCtx.close() } catch {}
    if (enrollmentPollTimer) clearInterval(enrollmentPollTimer)
    if (enrollmentDeadlineTimer) clearTimeout(enrollmentDeadlineTimer)
    enrollmentDeadlineTimer = null
    enrollmentProcessor = null; enrollmentStream = null; enrollmentCtx = null; enrollmentPollTimer = null
    enrollmentQueue = Promise.resolve()
    enrollmentPcmLength = 0
    enrollmentActive = false
  }

  function stop(closeBackend = true) {
    stopped = true
    scheduledCaptions = []
    stopMic()
    stopPolling()
    if (playCtx) void playCtx.close()
    playCtx = null
    playAnalyser = null
    status.set('idle')
    return closeBackend ? ctx.rest('/close', { method: 'POST' }).catch(() => {}) : Promise.resolve()
  }

  function syncSentinel(value) {
    if (captureBusy.get()) return
    sentinel.set(value || null)
    if (typeof value?.muted === 'boolean') {
      captureMuted.set(value.muted)
      if (value.muted) {
        stop(false)
        stopEnrollmentCapture()
      }
    }
  }

  async function setCaptureMuted(value) {
    if (captureBusy.get()) return
    captureBusy.set(true)
    if (value) {
      captureMuted.set(true)
      stop(false)
      stopEnrollmentCapture()
    }
    try {
      const result = await ctx.rest('/sentinel-mute', {
        method: 'POST', body: { muted: value }, timeoutMs: 40000
      })
      if (!result.ok) throw new Error(result.error || 'Não foi possível alterar a captação de áudio.')
      captureMuted.set(value)
      sentinel.set(result.sentinel || null)
      message.set('')
    } catch (error) {
      message.set(error.message || String(error))
    } finally {
      captureBusy.set(false)
    }
  }

  function syncListenOnly(value) {
    if (listenOnly.get() === value) return
    listenOnly.set(value)
    try { ctx.storage.set('local.listen-only', value) } catch {}
    if (value) {
      stop(false)
      playHead = 0
    }
  }

  async function setListenOnly(value) {
    if (listenOnlyBusy.get()) return
    listenOnlyBusy.set(true)
    if (value) syncListenOnly(true)
    try {
      const result = await ctx.rest('/listen-only', { method: 'POST', body: { enabled: value } })
      if (!result.ok) throw new Error(result.error || 'Não foi possível salvar Só ouvir.')
      syncListenOnly(result.listen_only)
      if (result.sentinel) sentinel.set(result.sentinel)
      message.set('')
    } catch (error) {
      message.set(error.message || String(error))
    } finally {
      listenOnlyBusy.set(false)
    }
  }

  try { if (ctx.storage.get('local.listen-only', false) === true) syncListenOnly(true) } catch {}

  async function start() {
    if (listenOnly.get() || listenOnlyBusy.get() || captureMuted.get() || captureBusy.get()) return
    if (enrollmentActive) {
      message.set('Conclua o cadastro de voz antes de iniciar a conversa.')
      return
    }
    if (status.get() === 'live' || status.get() === 'connecting') { stop(); return }
    status.set('connecting')
    micQueue = Promise.resolve()
    message.set('')
    stopped = false
    playHead = 0
    interruptSeq = null
    playbackSources.clear()
    usage.set('')
    transcript.set([])
    clearCaptions()
    delegation.set(null)
    delegSeen = ''
    farewellSent = false
    farewellBuffer = ''
    armActivity()
    off = { i: 0, o: 0, a: 0 }
    try {
      playCtx = new AudioContext({ sampleRate: 24000 })
      if (typeof playCtx.createAnalyser === 'function') {
        playAnalyser = playCtx.createAnalyser()
        playAnalyser.fftSize = playbackSamples.length
        playAnalyser.connect(playCtx.destination)
      }
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
      if (stopped || listenOnly.get() || captureMuted.get()) {
        stop(false)
        return
      }
      srcCtx = new AudioContext({ sampleRate: 16000 })
      const src = srcCtx.createMediaStreamSource(stream)
      processor = srcCtx.createScriptProcessor(4096, 1, 1)
      processor.onaudioprocess = event => {
        if (stopped || status.get() !== 'live') return
        const pcm = new Int16Array(event.inputBuffer.length)
        const input = event.inputBuffer.getChannelData(0)
        let peak = 0
        for (let i = 0; i < input.length; i++) {
          const s = Math.max(-1, Math.min(1, input[i]))
          pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff
          peak = Math.max(peak, Math.abs(s))
        }
        level.set(Math.round(peak * 100))
        const b64 = b64FromInt16(pcm)
        const playback = !!playCtx && playHead > playCtx.currentTime
        // Serialize uploads to preserve order; drop backlog if we fall behind.
        micQueue = micQueue.then(() => stopped
          ? null
          : ctx.rest('/audio', { method: 'POST', body: { data: b64, playback }, timeoutMs: 4000 }).catch(() => {})
        )
      }
      src.connect(processor)
      processor.connect(srcCtx.destination) // keeps the node pumping

      addLog('POST /start — iniciando voz do Jarvis…')
      const wanted = chosen.get()
      const body = wanted ? { model: wanted } : {}
      if ((wanted || '').includes('extended-thinking')) body.thinkingLevel = thinkLevel.get()
      body.resume = true                 // backend auto-resumes when a valid handle exists
      const r = await ctx.rest('/start', { method: 'POST', timeoutMs: 240000, body })
      if (!r.ok) {
        stopMic()
        status.set('error')
        message.set(r.error || 'Start failed')
        addLog(`start failed: ${r.error}`)
        return
      }
      if (stopped || listenOnly.get() || captureMuted.get()) {
        await ctx.rest('/close', { method: 'POST' })
        return
      }
      modelLabel.set(String(r.model || '').replace('models/', ''))
      startPolling()
      addLog(`LIVE — ${r.model} (mic streaming)`)
      status.set('live')
      armActivity() // Model preparation is not user silence; start the budget when ready.
      message.set('')
    } catch (e) {
      stopMic()
      stopPolling()
      status.set('error')
      message.set(String(e?.message || e))
      addLog(`error: ${e?.message || e}`)
    }
  }

  function setChosen(v) {
    chosen.set(v)
    try { ctx.storage.set('local.model', v) } catch {}
  }

  try { const saved = ctx.storage.get('local.model', ''); if (saved) chosen.set(saved) } catch {}

  const cancelBusy = atom(false)
  async function cancelTask() {
    if (cancelBusy.get() || delegation.get()?.status !== 'running') return
    cancelBusy.set(true)
    try {
      const result = await ctx.rest('/delegation-cancel', { method: 'POST', body: {}, timeoutMs: 15000 })
      message.set(result.ok ? (result.message || '') : (result.error || 'Cancelamento não confirmado.'))
    } catch {
      message.set('Não consegui confirmar o cancelamento. Confira a conversa Jarvis Voice.')
    } finally {
      cancelBusy.set(false)
    }
  }

  async function fetchModels() {
    try {
      const cfg = await ctx.rest('/config')
      const list = (cfg.candidates || []).map(m => String(m).replace('models/', '')).filter(m => !m.includes('translate'))
      models.set(list.length ? list : [String(cfg.model || '').replace('models/', '')])
      if ((cfg.engine === 'local' || !chosen.get()) && models.get()[0]) chosen.set(models.get()[0])
    } catch { /* keep previous list */ }
  }

  async function voiceAction(operation, body) {
    if (voiceBusy.get()) return null
    voiceBusy.set(true)
    voiceError.set('')
    voiceNotice.set(operation === 'enroll' ? 'Salvando identificação…' : '')
    try {
      const result = await ctx.rest(`/voice-identity/${operation}`, { method: 'POST', body,
        timeoutMs: operation === 'enroll' ? 35000 : operation === 'sample' ? 20000 : 6000 })
      if (!result.ok) throw new Error(result.error || 'Operação não concluída')
      if (operation === 'sample') {
        voiceSampleId.set(body.id)
        voiceSample.set(`data:audio/wav;base64,${result.audio}`)
        voiceSampleInfo.set({ offset: result.offset || 0, duration: result.duration || 0 })
      }
      if (operation === 'delete' || operation === 'enroll' || operation === 'review') {
        voiceSample.set('')
        voiceSampleId.set('')
        voiceSampleInfo.set(null)
        if (operation === 'enroll') draftCandidateId.set('')
      }
      if (operation === 'enroll') {
        voiceNotice.set(result.pending ? 'Nome salvo. A coleta continua até completar o cadastro.'
          : result.action === 'linked' ? 'Voz associada ao perfil existente.'
            : 'Cadastro vocal concluído. Veja em Vozes cadastradas.')
        const current = voiceIdentity.get()
        const candidates = result.pending
          ? (current.candidates || []).map(candidate => body.ids.includes(candidate.id)
            ? { ...candidate, label_name: body.name, label_owner: body.owner,
                seconds: result.seconds ?? candidate.seconds, chunks: result.chunks ?? candidate.chunks }
            : candidate)
          : (current.candidates || []).filter(candidate => !body.ids.includes(candidate.id))
        const profiles = result.action === 'created'
          ? [...(current.profiles || []), { id: result.id, name: body.name, owner: body.owner }]
          : current.profiles
        voiceIdentity.set({ ...current, candidates, profiles })
      }
      if (operation !== 'sample') {
        try {
          const p = await ctx.rest('/voice-identity/status', { method: 'POST', body: {}, timeoutMs: 10000 })
          if (p.voice_identity) voiceIdentity.set(p.voice_identity)
        } catch {
          // The action succeeded; a delayed panel refresh must not report it as failed.
        }
      }
      return result
    } catch (error) {
      if (operation === 'enroll') {
        try {
          const refreshed = await ctx.rest('/voice-identity/status', { method: 'POST', body: {}, timeoutMs: 10000 })
          const state = refreshed.voice_identity
          if (state) {
            voiceIdentity.set(state)
            if (!state.candidates?.some(candidate => body.ids.includes(candidate.id))
                && state.profiles?.some(profile => profile.name.toLocaleLowerCase() === String(body.name).toLocaleLowerCase())) {
              voiceNotice.set('Identificação concluída. Veja em Vozes cadastradas.')
              return { ok: true, recovered: true }
            }
          }
        } catch { /* Keep the original operation error. */ }
      }
      voiceNotice.set('')
      voiceError.set(error.message || String(error))
      return null
    }
    finally { voiceBusy.set(false) }
  }

  async function startEnrollment(replace_id = '', hint_name = '', consent = false) {
    if (enrollmentActive || voiceBusy.get() || captureMuted.get() || captureBusy.get()) return
    enrollmentActive = true
    enrollmentName.set(hint_name)
    enrollmentConsent.set(consent)
    if (status.get() === 'live' || status.get() === 'connecting') await stop()
    const generation = ++enrollmentGeneration
    voiceError.set('')
    let localStream = null
    let localCtx = null
    let localProcessor = null
    try {
      // Create/resume while the enrollment button still owns the user gesture.
      // Waiting for getUserMedia first can leave Chromium's AudioContext suspended.
      localCtx = new AudioContext()
      if (localCtx.state === 'suspended') await localCtx.resume()
      localStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
      if (captureMuted.get() || generation !== enrollmentGeneration) {
        localStream.getTracks().forEach(t => t.stop())
        void localCtx.close()
        return
      }
      const src = localCtx.createMediaStreamSource(localStream)
      localProcessor = localCtx.createScriptProcessor(4096, 1, 1)
      const opened = await voiceAction('enrollment', { replace_id })
      if (!opened || captureMuted.get() || generation !== enrollmentGeneration) {
        localStream.getTracks().forEach(t => t.stop())
        void localCtx.close()
        stopEnrollmentCapture()
        return
      }
      enrollmentStream = localStream
      enrollmentCtx = localCtx
      enrollmentProcessor = localProcessor
      enrollmentQueue = Promise.resolve()
      enrollmentPcmLength = 0
      enrollmentActive = true
      enrollmentDeadlineTimer = setTimeout(() => {
        voiceError.set('Tempo esgotado. Não foi possível concluir 20 segundos de fala limpa. Grave novamente.')
        voiceIdentity.set({ ...voiceIdentity.get(), enrollment: { state: 'error', seconds: 0 } })
        stopEnrollmentCapture()
      }, 185000)
      enrollmentLastFrameAt = Date.now()
      enrollmentProcessor.onaudioprocess = event => {
        if (!enrollmentActive || generation !== enrollmentGeneration) return
        enrollmentLastFrameAt = Date.now()
        const input = event.inputBuffer.getChannelData(0)
        const pcm = pcm16At16k(input, enrollmentCtx?.sampleRate || 16000)
        let offset = 0
        while (offset < pcm.length) {
          const take = Math.min(enrollmentPcm.length - enrollmentPcmLength, pcm.length - offset)
          enrollmentPcm.set(pcm.subarray(offset, offset + take), enrollmentPcmLength)
          enrollmentPcmLength += take
          offset += take
          if (enrollmentPcmLength !== enrollmentPcm.length) continue
          const data = b64FromInt16(enrollmentPcm)
          enrollmentPcm = new Int16Array(ENROLLMENT_BATCH_SAMPLES)
          enrollmentPcmLength = 0
          enrollmentQueue = enrollmentQueue.then(() => enrollmentActive && generation === enrollmentGeneration
            ? ctx.rest('/voice-identity/enrollment-audio', { method: 'POST', body: { data }, timeoutMs: 4000 })
              .then(result => { if (!result.ok) throw new Error(result.error || 'Falha na gravação local') })
              .catch(error => {
                if (generation !== enrollmentGeneration) return
                voiceError.set(error.message || String(error))
                // A transient transport failure must not discard already accepted speech.
              })
            : null)
        }
      }
      src.connect(enrollmentProcessor)
      enrollmentProcessor.connect(enrollmentCtx.destination)
      let pollBusy = false
      let reconnects = 0
      const receiveAudio = enrollmentProcessor.onaudioprocess
      enrollmentPollTimer = setInterval(async () => {
        if (!enrollmentActive || pollBusy || generation !== enrollmentGeneration) return
        pollBusy = true
        try {
          if (Date.now() - enrollmentLastFrameAt > 5000) {
            if (++reconnects > 2) throw new Error('O dispositivo de áudio continua indisponível após duas reconexões.')
            voiceError.set('Reconectando o microfone; a fala já aceita foi preservada.')
            enrollmentProcessor.onaudioprocess = null
            enrollmentProcessor.disconnect()
            enrollmentStream.getTracks().forEach(track => track.stop())
            void enrollmentCtx.close()
            const nextCtx = new AudioContext()
            let nextStream = null
            try {
              if (nextCtx.state === 'suspended') await nextCtx.resume()
              nextStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
              if (!enrollmentActive || generation !== enrollmentGeneration) {
                nextStream.getTracks().forEach(track => track.stop())
                void nextCtx.close()
                return
              }
              enrollmentCtx = nextCtx
              enrollmentStream = nextStream
              enrollmentProcessor = nextCtx.createScriptProcessor(4096, 1, 1)
              enrollmentPcmLength = 0
              enrollmentProcessor.onaudioprocess = receiveAudio
              nextCtx.createMediaStreamSource(nextStream).connect(enrollmentProcessor)
              enrollmentProcessor.connect(nextCtx.destination)
              enrollmentLastFrameAt = Date.now()
            } catch (error) {
              nextStream?.getTracks().forEach(track => track.stop())
              void nextCtx.close()
              throw error
            }
          }
          const p = await ctx.rest('/voice-identity/status', { method: 'POST', body: {}, timeoutMs: 4000 })
          if (generation !== enrollmentGeneration) return
          if (!p.voice_identity) return
          voiceIdentity.set(p.voice_identity)
          const state = p.voice_identity.enrollment?.state
          // Another poll may already have consumed the terminal state and closed
          // the backend enrollment session. An absent job must also release the mic.
          if (state !== 'recording' && p.voice_identity.available) stopEnrollmentCapture()
        } catch (error) {
          if (generation !== enrollmentGeneration) return
          voiceError.set(error.message || String(error))
          if (reconnects > 2) stopEnrollmentCapture()
        } finally { pollBusy = false }
      }, 500)
    } catch (error) {
      try { localProcessor && localProcessor.disconnect() } catch {}
      try { localStream && localStream.getTracks().forEach(t => t.stop()) } catch {}
      try { localCtx && localCtx.close() } catch {}
      voiceError.set(error.message || String(error))
      stopEnrollmentCapture()
    }
  }

  ctx.onDispose(() => {
    clearInterval(wakeTimer)
    stopEnrollmentCapture()
    stop()
  })
  return { captions, toolsOwner, toolsOpen, toolsPage, setToolsPage, proactivity, editProactivity, proactivityAction, taskHistory, memory, media, music, filesUI, filesAction, filesToggle, setFilesQuery, status, message, transcript, level, modelLabel, models, chosen, pickerOpen, thinkLevel, thinkOpen, usage, delegation, cancelBusy, cancelTask, sentinel, captureMuted, captureBusy, setCaptureMuted, listenOnly, listenOnlyBusy, setListenOnly, meetingPrompt, meetingNames, meetingBusy, meetingError, meetingAction, log, start, stop, toggle: start, fetchModels, setChosen, startWakeWatch, voiceIdentity, voiceError, voiceSample, voiceSampleId, voiceSampleInfo, voiceNotice, enrollmentName, enrollmentConsent, draftCandidateId, voiceBusy, voiceAction, startEnrollment }
}

function VoiceIdentityPanel({ live, embedded = false }) {
  const state = useValue(live.voiceIdentity)
  const error = useValue(live.voiceError)
  const sample = useValue(live.voiceSample)
  const sampleId = useValue(live.voiceSampleId)
  const sampleInfo = useValue(live.voiceSampleInfo)
  const notice = useValue(live.voiceNotice)
  const enrollmentName = useValue(live.enrollmentName)
  const enrollmentConsent = useValue(live.enrollmentConsent)
  const draftCandidateId = useValue(live.draftCandidateId)
  const busy = useValue(live.voiceBusy)
  const pending = state.pending
  const profiles = state.profiles || []
  const candidates = state.candidates || []
  const enrollment = state.enrollment || {}
  const recording = enrollment.state === 'recording'
  const enrollmentSeconds = Math.max(0, Number(enrollment.seconds) || 0)
  const progressSeconds = Math.min(20, enrollmentSeconds)
  const progressPercent = Math.min(100, (progressSeconds / 20) * 100)
  const statusText = error || state.error || (state.mode === 'off'
    ? 'Reconhecimento local desativado.'
    : !state.ready ? 'Preparando modelos locais…'
      : 'Locutor: ' + (state.latest?.name || 'aguardando fala'))
  const recordVoice = async (replace_id = '', hint_name = '', consent = false) => live.startEnrollment(replace_id, hint_name, consent)

  return jsxs(embedded ? 'div' : Popover, { ...(embedded ? {} : { onOpenChange: open => { if (!open) { live.voiceSample.set(''); live.voiceSampleId.set(''); live.voiceSampleInfo.set(null) } } }), children: [
    !embedded && jsx(PopoverTrigger, { asChild: true, children: jsx(Button, {
      variant: 'outline', type: 'button', 'aria-label': 'Identificação local de vozes',
      children: pending ? 'Confirmar pedido' : 'Vozes' + (candidates.length ? ' (' + candidates.length + ')' : '')
    }) }),
    jsx(embedded ? 'div' : PopoverContent, { ...(embedded ? {} : { side: 'top', align: 'end' }), className: embedded ? 'voice-identity-panel jarvis-tool-voices' : 'voice-identity-panel', children: jsxs('section', {
      className: 'voice-identity-shell', 'aria-label': 'Identificação local de vozes', children: [
        jsxs('div', { className: 'voice-identity-header', children: [
          jsxs('div', { children: [
            jsx('div', { className: 'voice-identity-title', children: 'Identificação de voz' }),
            jsx('div', { className: 'voice-identity-subtitle', children: 'Biometria processada neste computador' })
          ] }),
          jsx('span', { className: 'voice-identity-badge', 'data-active': !!state.ready,
            children: state.ready ? 'Local · pronto' : 'Local · preparando' })
        ] }),

        pending && jsxs('div', { className: 'voice-identity-card voice-identity-card--pending', children: [
          jsxs('div', { className: 'voice-identity-section-head', children: [
            jsx('span', { className: 'voice-identity-label', children: 'Pedido aguardando confirmação' }),
            jsx('span', { className: 'voice-identity-badge', 'data-active': true, children: String(pending.remaining_s) + 's' })
          ] }),
          jsx('p', { className: 'voice-identity-status', children: pending.request }),
          jsx('p', { className: 'voice-identity-muted', children: 'Este pedido exige confirmação na tela.' }),
          jsxs('div', { className: 'voice-identity-actions', children: [
            jsx('button', { type: 'button', className: 'voice-identity-primary', disabled: busy,
              onClick: () => live.voiceAction('decision', { id: pending.id, digest: pending.digest, allow: true }),
              children: 'Confirmar este pedido uma vez' }),
            jsx('button', { type: 'button', disabled: busy,
              onClick: () => live.voiceAction('decision', { id: pending.id, digest: pending.digest, allow: false }),
              children: 'Cancelar pedido' })
          ] })
        ] }),

        error && jsx('p', { className: 'voice-identity-status', role: 'alert', children: error }),
        notice && jsx('p', { className: 'voice-identity-status', role: 'status', children: notice }),

        jsxs('div', { className: 'voice-identity-section', children: [
          jsxs('div', { className: 'voice-identity-section-head', children: [
            jsx('span', { className: 'voice-identity-label', children: 'Pessoas para identificar' }),
            jsx('span', { className: 'voice-identity-badge', children: String(candidates.length) })
          ] }),
          !candidates.length && jsx('p', { className: 'voice-identity-muted',
            children: 'Nenhuma voz aguardando revisão. A escuta ambiente identifica perfis já cadastrados.' }),
          ...candidates.map(candidate => jsxs('article', {
            className: 'voice-identity-candidate', 'data-suggested': !!candidate.suggested_name,
            'aria-label': candidate.suggested_name ? `Revisar voz de ${candidate.suggested_name}` : 'Revisar voz sem cadastro',
            children: [
              jsxs('div', { className: 'voice-identity-section-head', children: [
                jsx('span', { className: 'voice-identity-candidate-title',
                  children: candidate.suggested_name ? `Pode ser ${candidate.suggested_name}`
                    : candidate.label_name ? `Identificação: ${candidate.label_name}`
                      : candidate.enrollment_ready ? 'Pronto para identificar' : 'Pessoa ainda não identificada' }),
                jsx('span', { className: 'voice-identity-badge',
                  children: `${Math.max(0, Number(candidate.seconds) || 0).toFixed(1)}s salvos` })
              ] }),
              jsx('div', { className: 'voice-identity-candidate-meta',
                children: new Date(candidate.created * 1000).toLocaleString() }),
              jsx('p', { className: 'voice-identity-candidate-note',
                children: candidate.suggested_name
                  ? (candidate.confidence >= 0.72 ? 'A biometria sugeriu esta pessoa. Confira a amostra antes de confirmar.'
                    : 'Este nome foi citado na conversa. A voz ainda não foi reconhecida com segurança.')
                  : candidate.hint_name ? `Nome citado na conversa: ${candidate.hint_name}. Ouça os trechos para confirmar quem falou.`
                    : candidate.label_name ? `Identificação salva: ${candidate.label_name}. Outros cartões com este nome são reunidos.`
                      : candidate.enrollment_ready ? 'Voz pronta. Confira o nome e o consentimento para salvar.'
                        : 'Trechos da conversa são salvos automaticamente. Identifique esta pessoa a qualquer momento.' }),
              !candidate.suggested_name && jsxs('p', { className: 'voice-identity-candidate-note', role: 'status', children: [
                `${candidate.chunks || 1} trechos salvos · ${Math.min(20, Number(candidate.seconds) || 0).toFixed(1)}/20s de fala`,
                candidate.enrollment_ready ? ' · pronto para formar perfil' : ' · mais trechos serão reunidos'
              ] }),
              jsx('p', { className: 'voice-identity-muted', children: 'O tempo acima soma trechos agrupados, não é uma gravação contínua. Ouça 12 segundos por vez.' }),
              jsxs('div', { className: 'voice-identity-actions', children: [
                jsx('button', { type: 'button', disabled: busy, onClick: () => live.voiceAction('sample', { id: candidate.id, offset: 0 }),
                  children: sampleId === candidate.id ? 'Reouvir amostra' : 'Ouvir amostra' }),
                candidate.suggested_name && jsx('button', { type: 'button', className: 'voice-identity-primary', disabled: busy,
                  onClick: () => live.voiceAction('review', { id: candidate.id, confirmed: true }),
                  children: `Sim, é ${candidate.suggested_name}` }),
                candidate.suggested_name && jsx('button', { type: 'button', disabled: busy,
                  onClick: () => live.voiceAction('review', { id: candidate.id, confirmed: false }),
                  children: 'Não é essa pessoa' }),
                !candidate.suggested_name && !candidate.enrollment_ready && !candidate.label_name && jsx('button', { type: 'button', disabled: busy || state.mode === 'off',
                  onClick: () => live.draftCandidateId.set(draftCandidateId === candidate.id ? '' : candidate.id),
                  children: draftCandidateId === candidate.id ? 'Fechar dados' : 'Identificar pessoa' }),
                jsx('button', { type: 'button', className: 'voice-identity-danger', disabled: busy,
                  onClick: () => live.voiceAction('delete', { id: candidate.id, candidate: true }),
                  children: 'Excluir amostra' })
              ] }),
              sample && sampleId === candidate.id && jsxs('div', { children: [
                jsx('audio', { controls: true, src: sample,
                  'aria-label': `Amostra vocal captada em ${new Date(candidate.created * 1000).toLocaleTimeString()}` }),
                sampleInfo && jsxs('div', { className: 'voice-identity-actions', children: [
                  jsx('span', { className: 'voice-identity-muted', children: `Trecho ${Math.floor(sampleInfo.offset / 12) + 1} de ${Math.max(1, Math.ceil(sampleInfo.duration / 12))}` }),
                  jsx('button', { type: 'button', disabled: busy || sampleInfo.offset === 0,
                    onClick: () => live.voiceAction('sample', { id: candidate.id, offset: Math.max(0, sampleInfo.offset - 12) }),
                    children: 'Trecho anterior' }),
                  jsx('button', { type: 'button', disabled: busy || sampleInfo.offset + 12 >= sampleInfo.duration,
                    onClick: () => live.voiceAction('sample', { id: candidate.id, offset: sampleInfo.offset + 12 }),
                    children: 'Próximo trecho' })
                ] })
              ] }),
              !candidate.suggested_name && (candidate.enrollment_ready || candidate.label_name || draftCandidateId === candidate.id) && jsxs('form', {
                className: 'voice-identity-enrollment-draft', onSubmit: event => {
                event.preventDefault()
                const form = new FormData(event.currentTarget)
                void live.voiceAction('enroll', { ids: [candidate.id], name: form.get('name'),
                  owner: form.get('owner') === 'on', consent: form.get('consent') === 'on' })
              }, onInvalid: event => { live.voiceError.set(event.target.name === 'consent'
                ? 'Marque o consentimento da pessoa para salvar a identificação vocal.'
                : 'Informe o nome da pessoa para salvar a identificação.') }, children: [
                jsx('p', { className: 'voice-identity-muted', children: candidate.enrollment_ready
                  ? 'Confirme quem falou. O nome será associado a um perfil existente ou criará um novo cadastro.'
                  : 'Salve o nome agora. A voz continuará reunindo trechos antes de formar um perfil.' }),
                jsx('input', { name: 'name', type: 'text', required: true, maxLength: 80,
                  defaultValue: candidate.label_name || profiles.find(p => p.id === candidate.replace_id)?.name || enrollmentName || candidate.hint_name || '',
                  placeholder: 'Nome da pessoa', 'aria-label': 'Nome da pessoa' }),
                jsxs('label', { className: 'voice-identity-check', children: [
                  jsx('input', { type: 'checkbox', name: 'owner',
                    defaultChecked: !!candidate.label_owner || !!profiles.find(p => p.id === candidate.replace_id)?.owner
                      || !!profiles.find(p => p.name.toLocaleLowerCase() ===
                        String(candidate.label_name || candidate.hint_name || '').toLocaleLowerCase())?.owner }),
                  jsx('span', { children: 'Esta é minha voz (proprietário)' })
                ] }),
                jsxs('label', { className: 'voice-identity-check', children: [
                  jsx('input', { type: 'checkbox', name: 'consent', required: true,
                    defaultChecked: !!candidate.label_name || (enrollmentConsent && !!enrollmentName) }),
                  jsx('span', { children: 'A pessoa concorda com o cadastro vocal' })
                ] }),
                jsx('p', { className: 'voice-identity-muted', children: 'Obrigatório: marque o consentimento antes de salvar.' }),
                jsx('button', { type: 'submit', className: 'voice-identity-primary',
                  disabled: busy,
                  children: candidate.enrollment_ready ? 'Concluir identificação' : 'Salvar nome da pessoa' })
              ] })
            ]
          }, candidate.id))
        ] }),

        recording && jsxs('div', { className: 'voice-identity-card', children: [
          jsxs('div', { className: 'voice-identity-section-head', children: [
            jsx('span', { className: 'voice-identity-label', children: 'Cadastro vocal' }),
            recording && jsx('span', { className: 'voice-identity-badge', 'data-active': true, children: 'Gravando' })
          ] }),
          jsx('div', { className: 'voice-identity-progress', 'aria-label': 'Progresso do cadastro vocal',
            children: jsx('span', { style: { width: progressPercent + '%' } }) }),
          jsxs('div', { className: 'voice-identity-progress-row', children: [
            jsx('span', { children: recording ? 'Fale normalmente e sem outras pessoas.' : '20 segundos de fala útil' }),
            jsx('strong', { children: Math.floor(progressSeconds) + '/20s' })
          ] }),
          jsx('p', { className: 'voice-identity-muted', role: 'status',
            children: enrollment.hint || `Trechos analisados: ${enrollment.batches_received || 0}.` })
        ] }),

        jsxs('div', { className: 'voice-identity-card', children: [
          jsxs('label', { className: 'voice-identity-row', children: [
            jsx('span', { className: 'voice-identity-label', children: 'Reconhecimento' }),
            jsx('select', {
              className: 'voice-identity-select', 'aria-label': 'Modo de reconhecimento',
              value: state.mode || 'off', disabled: busy,
              onChange: event => live.voiceAction('settings', { mode: event.target.value }),
              children: [
                jsx('option', { value: 'off', children: 'Desligado' }, 'off'),
                jsx('option', { value: 'observe', children: 'Identificar pessoas' }, 'observe')
              ]
            })
          ] }),
          jsx('p', { className: 'voice-identity-status', role: 'status', children: statusText }),
          state.mode !== 'off' && jsx('p', { className: 'voice-identity-muted',
            children: 'As vozes são identificadas para revisão. Pedidos sensíveis exigem confirmação na tela.' })
        ] }),

        jsxs('div', { className: 'voice-identity-section', children: [
          jsxs('div', { className: 'voice-identity-section-head', children: [
            jsx('span', { className: 'voice-identity-label', children: 'Vozes cadastradas' }),
            jsx('span', { className: 'voice-identity-badge', children: String(profiles.length) })
          ] }),
          !profiles.length && jsx('p', { className: 'voice-identity-muted', children: 'Nenhuma voz cadastrada.' }),
          ...profiles.map(profile => jsxs('details', { className: 'voice-identity-profile', children: [
            jsx('summary', { children: profile.name + (profile.owner ? ' · proprietário' : '') }),
            jsxs('div', { className: 'voice-identity-actions', children: [
              jsx('button', { type: 'button', disabled: busy || recording || state.mode === 'off',
                onClick: () => recordVoice(profile.id), children: 'Refazer cadastro' }),
              jsx('button', { type: 'button', className: 'voice-identity-danger', disabled: busy,
                onClick: () => live.voiceAction('delete', { id: profile.id, candidate: false }),
                children: 'Excluir perfil' })
            ] })
          ] }, profile.id))
        ] }),

        jsxs('details', { className: 'voice-identity-help', children: [
          jsx('summary', { children: 'Como funciona' }),
          jsx('p', { className: 'voice-identity-muted',
            children: 'Nome e consentimento são salvos imediatamente. Cartões identificados com o mesmo nome juntam seus trechos; com 20 segundos úteis em quatro trechos, o perfil é formado automaticamente. Os dados ficam protegidos pelo Windows até você excluí-los.' })
        ] })
      ] }) })
  ] })
}

function Minibar({ live }) {
  const status = useValue(live.status)
  const message = useValue(live.message)
  const transcript = useValue(live.transcript)
  const level = useValue(live.level)
  const modelLabel = useValue(live.modelLabel)
  const logs = useValue(live.log)
  const models = useValue(live.models)
  const chosen = useValue(live.chosen)
  const pickerOpen = useValue(live.pickerOpen)
  const thinkLevel = useValue(live.thinkLevel)
  const thinkOpen = useValue(live.thinkOpen)
  const usage = useValue(live.usage)
  const dlg = useValue(live.delegation)
  const cancelBusy = useValue(live.cancelBusy)
  const sent = useValue(live.sentinel)
  const active = status === 'live' || status === 'connecting'
  const sentinelAlive = !!sent?.alive
  const sentinelMuted = useValue(live.captureMuted)
  const captureBusy = useValue(live.captureBusy)
  // Merge streaming fragments into flowing sentences per speaker: consecutive
  // same-role fragments concatenate; '—' (turnComplete) starts a new line.
  const merged = []
  for (const row of transcript) {
    if (row.role === 'sep') { merged.push({ role: 'sep', text: '' }); continue }
    const last = merged[merged.length - 1]
    if (last && last.role === row.role) last.text += row.text
    else merged.push({ role: row.role, text: row.text })
  }
  const tail = merged.filter(r => r.role !== 'sep').slice(-2)
  return jsxs('div', { className: 'gemini-live-bar', 'data-gemini-live-status': status, children: [
    jsx('span', { className: 'gemini-live-dot', 'data-active': active, 'aria-hidden': true }),
    jsx(Popover, {
      onOpenChange: open => { if (open) void live.fetchModels(); if (!open && status === 'idle') live.stop() },
      children: [
        jsx(PopoverTrigger, { asChild: true, children:
          jsx(Button, { variant: 'ghost', size: 'micro', 'aria-label': 'Painel de voz do Jarvis', children:
            jsx('span', { className: 'gemini-live-name', children: 'Voz do Jarvis' })
          })
        }),
        jsx(PopoverContent, { side: 'top', align: 'end', className: 'gemini-live-panel', 'aria-label': 'Voz do Jarvis', children:
          jsxs('div', { children: [
            jsxs('div', { className: 'gemini-live-status', children: [
              jsx('span', { className: 'gemini-live-dot', 'data-active': active, 'aria-hidden': true }),
              jsx('span', { children: status === 'idle'
                ? (sentinelMuted ? 'Sentinela MUDO — Ctrl+Shift+M retoma'
                  : sentinelAlive ? "Sentinela no ar — say 'hey jarvis'" : "Idle — say 'hey jarvis' or press play")
                : status === 'live' ? 'Live — just talk' :
                status === 'connecting' ? 'Connecting…' : 'Error' }),
              jsx('span', { className: 'gemini-live-transport-space' }),
              jsx('span', { className: 'gemini-live-usage', title: 'Session tokens (free-tier meter)', children: usage ? `${usage} tok` : '' })
            ] }),
            active && jsx('div', { className: 'gemini-live-meter', 'aria-hidden': true, children:
              Array.from({ length: 8 }, (_, i) => jsx('span', { style: { height: `${Math.max(2, Math.min(16, (level / 100) * 16 * (1 - i * 0.09)))}px` } }, i))
            }),
            dlg && jsxs('div', { className: 'gemini-live-deleg', 'data-status': dlg.status, children: [
              jsx('span', { children: dlg.status === 'confirming' ? '🔒' : dlg.status === 'running' ? '⚙' : dlg.status === 'done' ? '✓' : dlg.status === 'cancelled' ? '⊘' : '✗' }),
              jsx('span', { children:
                dlg.status === 'confirming' ? 'Confirme o pedido no painel Vozes'
                  : dlg.status === 'running'
                  ? `Hermes executando: ${(dlg.request || '').slice(0, 110)}`
                  : dlg.status === 'done'
                    ? `Hermes (${dlg.elapsed ?? '?'}s): ${(dlg.reply || '').slice(0, 180)}`
                    : dlg.status === 'cancelled' ? `Tarefa interrompida: ${dlg.reply || ''}`
                    : `Hermes falhou: ${dlg.reply || ''}` }),
              dlg.status === 'running' && jsx('button', { type: 'button', className: 'gemini-live-flag', disabled: cancelBusy,
                onClick: () => void live.cancelTask(), children: cancelBusy ? 'Cancelando…' : 'Cancelar tarefa' })
            ] }),
            jsxs('div', { className: 'gemini-live-transport', children: [
              jsx(Tip, { label: active ? 'Stop session' : 'Start session', children: jsx(Button, {
                variant: 'ghost', size: 'icon-xs', onClick: live.toggle,
                children: jsx(active ? icons.Pause : icons.Play, { size: 12 })
              }) }),
              jsx(Tip, {
                label: sentinelMuted ? 'Retomar escuta ambiente (Ctrl+Shift+M)' : 'Pausar escuta ambiente (mudo)',
                children: jsx(Button, {
                  variant: 'ghost', size: 'icon-xs', disabled: captureBusy,
                  onClick: () => live.setCaptureMuted(!sentinelMuted),
                  children: jsx(ListeningIcon, { muted: sentinelMuted })
                })
              }),
              jsx('span', { className: 'gemini-live-transport-space' }),
              jsxs('div', { className: 'gemini-live-picker', children: [
                jsx('button', {
                  type: 'button', className: 'gemini-live-model', disabled: active,
                  title: 'Motor de voz',
                  onClick: () => live.pickerOpen.set(!pickerOpen),
                  children: status === 'live' ? modelLabel : chosen
                }),
                pickerOpen && jsx('div', { className: 'gemini-live-picker-list', role: 'listbox', children:
                  models.map(m => jsx('div', {
                    role: 'option', 'aria-selected': chosen === m,
                    className: 'gemini-live-picker-item' + (chosen === m ? ' gemini-live-picker-item--sel' : ''),
                    onClick: () => { live.setChosen(m); live.pickerOpen.set(false) },
                    children: m
                  }, m))
                })
              ] })
            ] }),
            chosen === 'gemini-3.8-live-extended-thinking' && jsxs('div', { className: 'gemini-live-thinkrow', children: [
              jsxs('div', { className: 'gemini-live-thinkhead', children: [
                jsx('span', { className: 'gemini-live-thinklabel', children: 'Thinking:' }),
                jsx('button', { type: 'button', className: 'gemini-live-model gemini-live-thinkbtn', disabled: active,
                  onClick: () => live.thinkOpen.set(!thinkOpen), children: thinkLevel })
              ] }),
              thinkOpen && jsx('div', { className: 'gemini-live-thinklist', role: 'listbox', children:
                ['low', 'high'].map(lv => jsx('div', {
                  role: 'option', 'aria-selected': thinkLevel === lv,
                  className: 'gemini-live-picker-item' + (thinkLevel === lv ? ' gemini-live-picker-item--sel' : ''),
                  onClick: () => { live.thinkLevel.set(lv); live.thinkOpen.set(false) },
                  children: lv === 'low' ? 'low — fast thinking' : 'high — deep thinking'
                }, lv))
              })
            ] }),
            jsx('div', { className: 'gemini-live-error', role: 'status', children:
              message || (tail.length ? tail.map((row, i) => jsxs('div', { children: [row.role === 'you' ? 'Você: ' : row.role === 'gemini' ? 'Jarvis: ' : '', row.text] }, i)) : '')
            }),
            jsx('div', { className: 'gemini-live-log', 'aria-label': 'diagnostics', children:
              logs.map((row, i) => jsxs('div', { children: [row.t, '  ', row.msg] }, i))
            })
          ] })
        })
      ]
    })
  ] })
}

const meetingClock = new Intl.DateTimeFormat('pt-BR', { timeZone: 'America/Sao_Paulo', hour: '2-digit', minute: '2-digit' })

function MeetingPromptPanel({ live }) {
  const prompt = useValue(live.meetingPrompt)
  const names = useValue(live.meetingNames)
  const busy = useValue(live.meetingBusy)
  const error = useValue(live.meetingError)
  if (!prompt) return null
  const m = prompt.meeting
  const span = meetingClock.format(new Date(m.started_at)) + '–' + meetingClock.format(new Date(m.ended_at))
  const secs = Math.max(0, Number(prompt.auto_close_in_s) || 0)
  const left = secs >= 60 ? Math.floor(secs / 60) + ' min' : secs + 's'
  const add = name => {
    const list = names.split(',').map(s => s.trim()).filter(Boolean)
    if (!list.some(n => n.toLowerCase() === name.toLowerCase())) list.push(name)
    live.meetingNames.set(list.join(', '))
  }
  return jsxs(Popover, { children: [
    jsx(PopoverTrigger, { asChild: true, children: jsx(Button, {
      variant: 'outline', type: 'button', 'aria-label': 'Reunião aguardando confirmação de encerramento',
      className: 'h-7 shrink-0 border-amber-500/50 bg-amber-500/15 px-3 font-mono text-xs text-amber-300',
      children: 'Encerrar reunião?' }) }),
    jsx(PopoverContent, { side: 'top', align: 'end', className: 'voice-identity-panel', children: jsxs('section', {
      className: 'voice-identity-shell', 'aria-label': 'Encerrar reunião', children: [
        jsxs('div', { className: 'voice-identity-card voice-identity-card--pending', children: [
          jsxs('div', { className: 'voice-identity-section-head', children: [
            jsx('span', { className: 'voice-identity-label', children: 'Reunião em silêncio há 2 min · ' + span }),
            jsx('span', { className: 'voice-identity-badge', 'data-active': true, children: left })
          ] }),
          jsx('p', { className: 'voice-identity-muted', children: 'Encerrar a transcrição? Sem resposta, encerra sozinha em ' + left + '.' }),
          jsx('input', { type: 'text', value: names, placeholder: 'Com quem foi? (nomes separados por vírgula)',
            'aria-label': 'Participantes da reunião',
            onChange: ev => live.meetingNames.set(ev.target.value) }),
          (prompt.suggestions || []).length > 0 && jsx('div', { className: 'voice-identity-actions', children:
            prompt.suggestions.map(s => jsx('button', { type: 'button', title: s.reason, disabled: busy,
              onClick: () => add(s.name), children: '+ ' + s.name }, s.name)) }),
          error && jsx('p', { className: 'voice-identity-status', role: 'alert', children: error }),
          jsxs('div', { className: 'voice-identity-actions', children: [
            jsx('button', { type: 'button', className: 'voice-identity-primary', disabled: busy,
              onClick: () => live.meetingAction('close'), children: busy ? 'Encerrando…' : 'Encerrar e gerar ata' }),
            jsx('button', { type: 'button', disabled: busy, onClick: () => live.meetingAction('continue'),
              children: 'Ainda estamos em reunião' }),
            jsx('button', { type: 'button', disabled: busy, onClick: () => live.meetingAction('open_page'),
              children: 'Abrir página de reuniões' })
          ] })
        ] })
      ] }) })
  ] })
}

function ListeningIcon({ muted }) {
  return jsxs('svg', { width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round',
    'aria-hidden': true, children: [
      jsx('path', { d: 'M6 8a6 6 0 0 1 12 0c0 4-4 4-4 7v2a3 3 0 0 1-6 0M10 8a2 2 0 0 1 4 0c0 2-3 2-3 4' }),
      muted && jsx('path', { d: 'M3 3l18 18' })
    ] })
}

function TaskHistoryPanel({ live, embedded = false }) {
  const state = useValue(live.taskHistory)
  const labels = { queued: 'Na fila', confirming: 'Aguardando confirmação', running: 'Em execução',
    done: 'Concluída', error: 'Falhou', cancelled: 'Não executada ou interrompida', uncertain: 'Resultado incerto' }
  return jsxs('details', { className: 'voice-identity-card', open: embedded || undefined, children: [
    jsx('summary', { children: `Tarefas recentes (${state.tasks.length})` }),
    state.error && jsx('p', { role: 'alert', children: state.error }),
    jsx('p', { className: 'voice-identity-status', children: 'Histórico local. Após reinício, confira resultados incertos na conversa Jarvis Voice antes de repetir.' }),
    state.loaded === false && jsx('p', { role: 'status', children: 'Carregando histórico…' }),
    state.loaded !== false && !state.tasks.length && !state.error && jsx('p', { children: 'Nenhuma tarefa registrada neste perfil.' }),
    jsx('div', { style: { maxHeight: 260, overflowY: 'auto' }, children: state.tasks.map(task =>
      jsxs('article', { className: 'voice-identity-card', children: [
        jsx('strong', { children: labels[task.status] || 'Estado desconhecido' }),
        jsx('p', { children: task.request }),
        task.reply && jsx('p', { style: { whiteSpace: 'pre-wrap' }, children: task.reply })
      ] }, task.id)) })
  ] })
}

function ProactivityPanel({ live, embedded = false }) {
  const state = useValue(live.proactivity)
  const draft = state.draft
  return jsxs('details', { className: 'voice-identity-card', open: embedded || state.notice ? true : undefined, children: [
    jsx('summary', { children: 'Check-ins' }),
    jsx('p', { className: 'voice-identity-status', children: 'Avisos visuais locais, somente após ativar. Respeitam Só ouvir, captura pausada, reuniões e tarefas. Horários do Windows.' }),
    state.error && jsx('p', { role: 'alert', children: state.error }),
    !draft && !state.error && jsx('p', { role: 'status', children: 'Carregando configuração…' }),
    draft && jsxs('form', { onSubmit: event => { event.preventDefault(); void live.proactivityAction('save') }, children: [
      jsxs('label', { children: [jsx('input', { type: 'checkbox', checked: draft.enabled, disabled: state.busy,
        onChange: event => live.editProactivity({ enabled: event.target.checked }) }), 'Ativar check-ins'] }),
      jsxs('label', { children: ['Intervalo em minutos', jsx('input', { type: 'number', min: 15, max: 240, required: true,
        value: draft.interval_minutes, disabled: state.busy, onChange: event => live.editProactivity({ interval_minutes: Number(event.target.value) }) })] }),
      ...['start', 'end'].map(key => jsxs('label', { children: [key === 'start' ? 'Começar às' : 'Terminar às',
        jsx('input', { type: 'time', required: true, value: draft[key], disabled: state.busy,
          onChange: event => live.editProactivity({ [key]: event.target.value }) })] }, key)),
      jsx('button', { type: 'submit', disabled: state.busy || !state.dirty, children: state.busy ? 'Salvando…' : 'Salvar check-ins' }),
      jsx('p', { role: 'status', children: state.dirty ? 'Alterações ainda não salvas.' : state.status })
    ] }),
    state.notice && jsxs('div', { className: 'voice-identity-card', children: [
      jsx('p', { children: state.notice.text }), jsx('button', { type: 'button', disabled: state.busy,
        onClick: () => void live.proactivityAction('dismiss'), children: 'Dispensar aviso' })
    ] })
  ] })
}

function FilesPanel({ live, embedded = false, onClose }) {
  const state = useValue(live.filesUI)
  return jsxs('div', { className: 'shrink-0', children: [
    !embedded && jsx(Button, { id: 'jarvis-files-toggle', type: 'button', variant: 'outline', 'aria-expanded': state.open,
      'aria-controls': 'jarvis-files-panel', onClick: live.filesToggle,
      className: 'h-7 border-cyan-500/40 px-2 font-mono text-xs text-cyan-300', children: 'Arquivos' }),
    state.open && jsxs('section', { id: 'jarvis-files-panel', 'aria-label': 'Arquivos locais',
      className: embedded ? 'jarvis-tool-content' : 'fixed bottom-24 right-6 z-[10001] max-h-[75vh] w-[440px] max-w-[calc(100vw-48px)] overflow-auto rounded-lg border border-cyan-500/40 bg-slate-950 p-4 text-slate-100 shadow-lg',
      onKeyDown: event => { if (!embedded && event.key === 'Escape') live.filesToggle() },
      children: [
        jsxs('div', { className: 'mb-3 flex items-center justify-between gap-2', children: [
          jsx('h2', { className: 'font-title text-sm font-bold', children: 'Arquivos locais' }),
          jsx('button', { type: 'button', onClick: onClose || live.filesToggle, 'aria-label': 'Fechar arquivos', children: 'Fechar' })
        ] }),
        jsx('p', { className: 'mb-3 text-xs text-slate-400', children: 'Busca por nome em Documentos, Downloads e Área de Trabalho. Sem enviar conteúdo à IA.' }),
        !state.preview && jsxs('form', { className: 'flex gap-2', onSubmit: event => { event.preventDefault(); void live.filesAction('files.search') }, children: [
          jsx('input', { type: 'search', 'aria-label': 'Nome do arquivo', value: state.query, maxLength: 150, autoFocus: true,
            className: 'min-w-0 flex-1 rounded border border-cyan-500/40 bg-transparent px-2 py-1 text-sm',
            onChange: event => live.setFilesQuery(event.target.value) }),
          jsx(Button, { type: 'submit', disabled: state.busy || state.query.trim().length < 2, children: state.busy ? 'Aguarde…' : 'Buscar' }),
          state.busy && jsx('button', { type: 'button', onClick: () => void live.filesAction('files.cancel'), children: 'Cancelar busca' })
        ] }),
        state.error && jsx('p', { role: 'alert', className: 'mt-2 text-xs text-slate-300', children: state.error }),
        !state.preview && jsx('p', { role: 'status', className: 'my-2 text-xs text-slate-400', children: state.notice }),
        !state.preview && jsx('ul', { className: 'space-y-2', children: state.results.map(row => jsxs('li', {
          className: 'rounded border border-cyan-500/20 p-2', children: [
            jsx('p', { className: 'break-words text-sm', children: row.name }),
            jsx('p', { className: 'break-words text-xs text-slate-400', children: row.location }),
            jsxs('div', { className: 'mt-2 flex gap-3 text-xs text-cyan-300', children: [
              jsx('button', { type: 'button', disabled: state.busy, onClick: () => void live.filesAction('files.preview', row.ref), children: 'Prévia' }),
              jsx('button', { type: 'button', disabled: state.busy, onClick: () => void live.filesAction('files.open', row.ref), children: 'Abrir arquivo' })
            ] })
          ] }, row.ref)) }),
        state.preview && jsxs('section', { className: 'jarvis-file-preview', 'aria-label': 'Prévia do arquivo', children: [
          jsx('button', { type: 'button', autoFocus: true, 'aria-label': 'Voltar aos arquivos encontrados',
            onClick: () => live.filesUI.set({ ...live.filesUI.get(), preview: null }), children: '← Resultados' }),
          jsx('h3', { className: 'text-sm', children: state.preview.name }),
          jsx('p', { className: 'my-2 text-xs text-slate-400', children: state.preview.notice }),
          jsx('pre', { tabIndex: 0, className: 'max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs', children: state.preview.text }),
          state.preview.truncated && jsx('p', { className: 'text-xs text-slate-400', children: 'Prévia truncada. Abra o arquivo para ler o restante.' })
        ] })
      ] })
  ] })
}

// Preserved visible-video player. AmbientMusicButton uses the internal browser instead.
async function musicPlayerSrc() {
  const getOrigin = window.hermesDesktop?.getEmbedHostOrigin
  if (getOrigin) {
    const origin = new URL(await getOrigin())
    if (origin.protocol !== 'http:' || origin.hostname !== '127.0.0.1' || !origin.port ||
        origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) {
      throw new Error('Origem local do player inválida.')
    }
    return `${origin.origin}/youtube/tRsQsTMvPNg?rel=0`
  }
  if (window.location?.protocol === 'http:' || window.location?.protocol === 'https:') {
    return 'https://www.youtube-nocookie.com/embed/tRsQsTMvPNg?playsinline=1&rel=0'
  }
  throw new Error('Player local indisponível. Abra a faixa no YouTube.')
}

function AnthropicMusic({ playerOnly = false, triggerOnly = false } = {}) {
  const open = useValue(musicOpen)
  const player = useValue(musicPlayer)
  useEffect(() => {
    if (!playerOnly || !open) return
    let active = true
    musicPlayer.set({ src: '', error: '' })
    musicPlayerSrc().then(src => {
      if (active) musicPlayer.set({ src, error: '' })
    }).catch(() => {
      if (active) musicPlayer.set({ src: '', error: 'Não consegui abrir o player local. Use o link do YouTube.' })
    })
    return () => { active = false }
  }, [open, playerOnly])
  if (playerOnly && !open) return null
  return jsxs('div', { className: 'relative shrink-0', children: [
    !playerOnly && jsx(Button, { type: 'button', variant: 'outline',
      'aria-expanded': open, 'aria-controls': 'jarvis-music-player',
      'aria-label': open ? 'Parar música da Anthropic' : 'Tocar música da Anthropic',
      className: 'jarvis-control',
      onClick: () => musicOpen.set(!open),
      children: open ? '■ Parar música' : 'Abrir player · Anthropic' }),
    !triggerOnly && open && jsx(Popover, { open: true, onOpenChange: value => { if (!value) musicOpen.set(false) }, children: jsxs(PopoverContent, { id: 'jarvis-music-player', 'aria-label': 'Música · Anthropic',
      className: 'jarvis-music-player',
      onOpenAutoFocus: event => event.preventDefault(),
      onCloseAutoFocus: event => event.preventDefault(),
      onInteractOutside: event => event.preventDefault(),
      children: [
        jsxs('header', { children: [jsx('h2', { children: 'Música · Anthropic' }),
          jsx('button', { type: 'button', 'aria-label': 'Parar e fechar música', onClick: () => musicOpen.set(false), children: 'Fechar' })] }),
        player.src && jsx('iframe', { title: 'Player de música da Anthropic no YouTube',
          src: player.src,
          allow: 'autoplay; encrypted-media; picture-in-picture; fullscreen',
          referrerPolicy: 'strict-origin-when-cross-origin', allowFullScreen: true }),
        jsx('p', { role: 'status', children: player.error || (player.src ? 'Clique em ▶ no vídeo para tocar. Fechar este cartão para a música.' : 'Preparando player…') }),
        jsx('a', { href: 'https://www.youtube.com/watch?v=tRsQsTMvPNg', target: '_blank',
          rel: 'noopener noreferrer', children: 'Abrir no YouTube ↗' })
      ] }) })
  ] })
}

const TOOL_PAGES = [
  ['files', 'Arquivos', 'Encontre, visualize e abra arquivos do computador.'],
  ['media', 'Mídia', 'Busque vídeos, toque no Spotify e controle reprodutores.'],
  ['memory', 'Memória', 'Consulte fontes e revise o que o Jarvis lembra.'],
  ['voices', 'Vozes', 'Ouça amostras e identifique pessoas com consentimento.'],
  ['tasks', 'Tarefas recentes', 'Confira pedidos, resultados e tarefas em andamento.'],
  ['checkins', 'Check-ins', 'Configure avisos e horários de acompanhamento.'],
  ['music', 'Música ambiente', 'Reproduza ou pare a faixa da Anthropic.']
]

function JarvisTools({ live }) {
  const instance = useId()
  const requestedOpen = useValue(live.toolsOpen)
  const owner = useValue(live.toolsOwner)
  const open = requestedOpen && owner === instance
  const page = useValue(live.toolsPage)
  const voices = useValue(live.voiceIdentity)
  const tasks = useValue(live.taskHistory)
  const proactivity = useValue(live.proactivity)
  const title = TOOL_PAGES.find(([id]) => id === page)?.[1] || 'Ferramentas do Jarvis'
  const back = () => live.setToolsPage('')
  const close = () => { live.setToolsPage(''); live.toolsOpen.set(false); live.toolsOwner.set(null) }
  useEffect(() => () => {
    if (live.toolsOwner.get() === instance) { live.setToolsPage(''); live.toolsOpen.set(false); live.toolsOwner.set(null) }
  }, [live, instance])
  const panels = {
    files: jsx(FilesPanel, { live, embedded: true, onClose: back }),
    media: jsx(MediaPanel, { live, embedded: true, onClose: back }),
    memory: jsx(MemoryPanel, { live, embedded: true, onClose: back }),
    voices: jsx(VoiceIdentityPanel, { live, embedded: true }),
    tasks: jsx(TaskHistoryPanel, { live, embedded: true }),
    checkins: jsx(ProactivityPanel, { live, embedded: true }),
    music: jsxs('div', { className: 'jarvis-tool-content', children: [
      jsx(AmbientMusicButton, { live })
    ] })
  }
  return jsxs(Popover, { open, onOpenChange: value => {
    if (value) { live.toolsOwner.set(instance); live.toolsOpen.set(true) }
    else if (live.toolsOwner.get() === instance) close()
  }, children: [
    jsx(PopoverTrigger, { asChild: true, children: jsx(Button, {
      type: 'button', variant: 'outline', className: 'jarvis-control', children: proactivity.notice ? 'Ferramentas · aviso' : 'Ferramentas'
    }) }),
    jsxs(PopoverContent, { side: 'top', align: 'end', sideOffset: 12, className: 'jarvis-voice-tools',
      'aria-label': 'Ferramentas do Jarvis',
      onEscapeKeyDown: event => { if (page) { event.preventDefault(); back() } },
      children: [
        jsxs('header', { className: 'jarvis-tools-header', children: [
          page && jsx('button', { type: 'button', autoFocus: true, onClick: back, children: '← Voltar' }),
          jsx('h2', { children: title }),
          jsx('button', { type: 'button', onClick: close, 'aria-label': 'Fechar ferramentas', children: 'Fechar' })
        ] }),
        page ? jsx('div', { className: 'jarvis-tool-body', children: panels[page] })
          : jsx('nav', { className: 'jarvis-tools-list', 'aria-label': 'Escolha uma ferramenta', children: TOOL_PAGES.map(([id, label, description]) =>
            jsxs('button', { id: 'jarvis-tool-' + id, type: 'button', onClick: () => live.setToolsPage(id), children: [
              jsxs('span', { children: [jsx('strong', { children: label }), jsx('span', { children: description })] }),
              jsx('span', { className: 'jarvis-tool-count', children: id === 'voices' ? String(voices.candidates?.length || 0)
                : id === 'tasks' ? String(tasks.tasks.length) : id === 'checkins' && proactivity.notice ? 'Aviso' : '›' })
            ] }, id)) })
      ] })
  ] })
}

function JarvisVoiceControls({ live }) {
  const toolsOpen = useValue(live.toolsOpen)
  const status = useValue(live.status)
  const message = useValue(live.message)
  const sentinel = useValue(live.sentinel)
  const delegation = useValue(live.delegation)
  const meetingPrompt = useValue(live.meetingPrompt)
  const listenOnly = useValue(live.listenOnly)
  const listenOnlyBusy = useValue(live.listenOnlyBusy)
  const captureMuted = useValue(live.captureMuted)
  const captureBusy = useValue(live.captureBusy)
  const active = status === 'live' || status === 'connecting'
  const label = captureMuted ? (message || 'Captação de áudio pausada') : listenOnly ? (message || (sentinel?.muted ? 'Só ouvir · escuta pausada' : 'Só ouvir · voz bloqueada'))
    : status === 'connecting' ? 'Preparando voz do Jarvis…'
    : status === 'live' ? (delegation?.status === 'confirming' ? 'Confirme o pedido no painel Vozes'
      : delegation?.status === 'running' ? 'Hermes executando' : 'Jarvis · ouvindo')
    : status === 'error' ? message
    : meetingPrompt ? 'Reunião em silêncio · confirme o encerramento'
    : sentinel?.alive ? (sentinel.muted ? 'Escuta ambiente pausada' : 'Escuta ambiente · diga Hey Jarvis')
    : 'Iniciando escuta ambiente…'
  return jsxs('div', { className: 'jarvis-voice-controls', 'data-tools-open': toolsOpen, role: 'group', 'aria-label': 'Controles de voz do Jarvis', children: [
    jsxs('div', { className: 'jarvis-voice-status', children: [
      jsx('span', { className: '', children: 'JARVIS' }),
      jsx('p', { role: 'status', title: label, className: '', children: label })
    ] }),
    jsx(Button, { type: 'button', variant: 'outline', disabled: status === 'connecting' || listenOnly || listenOnlyBusy || captureMuted || captureBusy,
      className: 'jarvis-control jarvis-control--primary',
      onClick: active ? live.stop : live.start,
      children: active ? 'Encerrar conversa' : 'Ativar voz' }),
    jsx(Button, { type: 'button', variant: 'outline', disabled: listenOnlyBusy,
      'aria-pressed': listenOnly, 'aria-label': 'Só ouvir',
      title: listenOnly ? 'Desativar Só ouvir e permitir conversas por voz' : 'Continuar escutando sem falar',
      className: 'jarvis-control',
      onClick: () => live.setListenOnly(!listenOnly),
      children: listenOnlyBusy ? 'Salvando…' : listenOnly ? 'Só ouvir · ativo' : 'Só ouvir' }),
    jsx(Button, { type: 'button', variant: 'outline', disabled: captureBusy,
      'aria-pressed': !captureMuted, 'aria-label': 'Captação de áudio',
      title: captureMuted ? 'Retomar captação de áudio' : 'Pausar captação de áudio',
      className: 'jarvis-control',
      onClick: () => live.setCaptureMuted(!captureMuted),
      children: jsxs('span', { className: 'flex items-center gap-1', children: [
        jsx(ListeningIcon, { muted: captureMuted }),
        captureBusy ? 'Aguarde…' : captureMuted ? 'Pausado' : 'Ouvindo'
      ] }) }),
    jsx(JarvisTools, { live }),
    jsx(MeetingPromptPanel, { live })
  ] })
}

function mountCaptions(ctx, live) {
  // A body-level layer avoids the composer's backdrop-filter trapping fixed positioning.
  if (!document.body) return
  const layer = document.createElement('div')
  layer.className = 'jarvis-captions'
  const columns = ['you', 'jarvis'].map(role => {
    const column = document.createElement('section')
    column.className = `jarvis-caption jarvis-caption--${role}`
    const label = document.createElement('span')
    label.className = 'jarvis-caption-label'
    label.textContent = role === 'you' ? 'Você' : 'Jarvis'
    const text = document.createElement('p')
    text.className = 'jarvis-caption-text'
    text.tabIndex = 0
    text.setAttribute('role', 'log')
    text.setAttribute('aria-label', role === 'you' ? 'Sua fala' : 'Fala do Jarvis')
    text.setAttribute('aria-live', 'off')
    column.hidden = true
    column.append(label, text)
    layer.append(column)
    return { role, column, text }
  })
  document.body.append(layer)
  let previous = null
  const timer = setInterval(() => {
    const next = live.captions.get()
    if (next === previous) return
    previous = next
    for (const { role, column, text } of columns) {
      const value = next[role]
      column.hidden = !value
      column.dataset.active = String(next.active === role)
      if (text.textContent !== value) {
        const follow = text.scrollHeight - text.scrollTop - text.clientHeight < 40
        text.textContent = value
        if (follow) text.scrollTop = text.scrollHeight
      }
    }
  }, 100)
  ctx.onDispose(() => { clearInterval(timer); layer.remove() })
}

export default {
  id: ID,
  name: 'Voz do Jarvis',
  description: 'Conversa com o Hermes, transcrição e voz locais.',
  register(ctx) {
    const style = document.createElement('style')
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())
    ctx.onDispose(() => musicOpen.set(false))
    const live = createLive(ctx)
    mountCaptions(ctx, live)
    live.startWakeWatch()
    ctx.register({ id: 'jarvis-controls', area: 'jarvis.voice.controls', render: () => jsx(JarvisVoiceControls, { live }) })
    ctx.register({ id: 'minibar', area: STATUSBAR_AREAS.right, order: 10, render: () => jsx(Minibar, { live }) })
  }
}
