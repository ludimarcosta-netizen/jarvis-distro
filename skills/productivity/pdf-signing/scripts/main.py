"""Agent-facing CLI. No command-line flag can approve a signature."""

import argparse
import json
import logging
import os
from pathlib import Path

from analyze import analyze
from evolution import Blocked, Evolution
from receive import serve
from workflow import Workflow

APPROVAL_WINDOW_MS = 240_000


def human_approval(workflow, request_id):
    import tkinter as tk
    from tkinter import messagebox

    row = workflow.get(request_id)
    plan = workflow.checked_plan(row)
    if row["state"] != "prepared":
        raise Blocked("Somente prévia preparada pode ser aprovada.")
    window = tk.Tk()
    window.title("Jarvis — autorização específica de assinatura")
    window.geometry("1000x800")
    window.attributes("-topmost", True)
    window.focus_force()
    text = tk.Text(window, height=17, wrap="word")
    text.insert("1.0", json.dumps(plan, ensure_ascii=False, indent=2))
    text.configure(state="disabled")
    text.pack(fill="x")
    canvas = tk.Canvas(window)
    scroll = tk.Scrollbar(window, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    canvas.pack(fill="both", expand=True)
    content = tk.Frame(canvas)
    canvas.create_window((0, 0), window=content, anchor="nw")
    images = []
    for name in plan["previews"]:
        image = tk.PhotoImage(file=str(workflow.folder(request_id) / name))
        images.append(image)
        tk.Label(content, image=image).pack()
    content.bind(
        "<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all"))
    )

    def approve():
        if messagebox.askyesno(
            "Confirmar autorização",
            "Autorizo assinar este PDF com os certificados e áreas exibidos e devolver o resultado à conversa original. Validade: 15 minutos.",
        ):
            workflow.approve(request_id, row["plan_hash"])
            window.destroy()

    tk.Button(
        window, text="Autorizar assinatura e devolução deste PDF", command=approve
    ).pack()
    tk.Button(window, text="Cancelar", command=window.destroy).pack()
    # Nobody at the PC (request came from Telegram): close before the caller's 300 s timeout.
    window.after(APPROVAL_WINDOW_MS, window.destroy)
    window.mainloop()
    return {"request": request_id, "state": workflow.get(request_id)["state"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    home = Path(os.environ.get("HERMES_HOME", Path(__file__).resolve().parents[4]))
    parser.add_argument(
        "--state-dir", type=Path, default=home / "state" / "pdf-signing"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("--chat", required=True)
    fetch.add_argument("--message-id", required=True)
    for name in ("prepare", "approve", "sign", "deliver", "status", "triage"):
        sub = commands.add_parser(name)
        sub.add_argument("request")
        if name == "prepare":
            sub.add_argument("--proposal", required=True, type=Path)
        if name == "triage":
            sub.add_argument("--model", required=True)
    monitor = commands.add_parser("serve")
    commands.add_parser("certificates")
    commands.add_parser("refresh-validation")
    monitor.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    for name in ("pyhanko", "pyhanko_certvalidator"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    workflow = Workflow(args.state_dir)
    try:
        if args.command == "fetch":
            result = {
                "request": workflow.ingest(Evolution(), args.chat, args.message_id)
            }
        elif args.command == "refresh-validation":
            from validation_data import refresh

            result = refresh(workflow.root / "validation")
        elif args.command == "certificates":
            from windows_signer import inventory

            result = [
                {
                    key: row[key]
                    for key in (
                        "alias",
                        "fingerprint",
                        "has_private_key",
                        "valid_now",
                        "chain_ok",
                    )
                }
                for row in inventory()
            ]
        elif args.command == "prepare":
            result = workflow.prepare(
                args.request, json.loads(args.proposal.read_text(encoding="utf-8"))
            )
        elif args.command == "approve":
            result = human_approval(workflow, args.request)
            if result["state"] == "prepared":
                raise Blocked(
                    "Janela de autorização fechada sem confirmação (cancelada ou sem resposta em 4 minutos). "
                    'Nada foi assinado. Autorização dada no chat vai ao controlador de PDFs ("pode assinar").'
                )
        elif args.command == "triage":
            result = analyze(workflow.folder(args.request), args.model)
        elif args.command == "sign":
            result = workflow.sign(args.request)
        elif args.command == "deliver":
            result = workflow.deliver(args.request, Evolution())
        elif args.command == "serve":
            serve(
                workflow,
                Evolution(),
                json.loads(args.config.read_text(encoding="utf-8")),
            )
            return
        else:
            row = workflow.get(args.request)
            result = {
                "request": row["id"],
                "state": row["state"],
                "pdf_hash": row["pdf_hash"],
                "plan_hash": row["plan_hash"],
            }
        print(json.dumps(result, ensure_ascii=False))
    except Blocked as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from None
    except (ValueError, OSError, KeyError, TypeError):
        # Avoid PDF contents, API errors or certificate identity leaking to logs.
        print(
            json.dumps(
                {
                    "error": "Operação bloqueada. Verifique estado, aprovação, dependências e provedor dos certificados."
                }
            )
        )
        raise SystemExit(2)
    finally:
        workflow.close()


if __name__ == "__main__":
    main()
