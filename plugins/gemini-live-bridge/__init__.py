"""gemini-live-bridge — Hermes unified agent+desktop package marker.

The plugin's real backend is the dashboard API (``dashboard/plugin_api.py``,
mounted by the web server at ``/api/plugins/gemini-live-bridge/``); the voice
minibar UI is ``desktop/plugin.js``. This package marker exists so the desktop
installer recognizes the repository as a unified agent+desktop package and so
the agent install lands the ``dashboard/`` half (plus ``desktop/`` for
materialization) into the plugins root. It registers no agent tools.

Jarvis fork: adds the delegate_to_hermes function (Gemini Live toolCall ->
one quiet Hermes turn in the "Jarvis Voice" session -> toolResponse).
"""


def register(ctx):
    """Route typed Desktop PDF requests using native middleware, without new tools."""
    import importlib.util
    from pathlib import Path

    for name, filename in (
        ("jarvis_pdf_agent_gate", "pdf_agent_gate.py"),
        ("jarvis_wa_text_guard", "wa_text_guard.py"),  # nenhum texto não ditado sai pelo WhatsApp
        ("jarvis_vigia_reply_context", "vigia_reply_context.py"),  # "instala N" chega com a proposta
        ("jarvis_meeting_context", "voice_meeting_context.py"),  # dados locais, sem rodadas de descoberta
    ):
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parents[2] / "jarvis" / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.register(ctx)
