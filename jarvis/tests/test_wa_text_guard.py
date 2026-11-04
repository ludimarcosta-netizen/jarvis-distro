import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "wa_text_guard", Path(__file__).resolve().parents[1] / "wa_text_guard.py"
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

WA = '"C:/Py/python.exe" "C:/hermes/skills/productivity/whatsapp-evolution/wa.py"'


def send(text, to="Beltrana Quatro"):
    return {"command": f'{WA} enviar --para "{to}" --texto "{text}" --repetir'}


class WaTextGuardTest(unittest.TestCase):
    def setUp(self):
        guard._turns.clear()

    def turn(self, user, previous=""):
        history = [{"role": "assistant", "content": previous}] if previous else []
        guard.capture(session_id="s", user_message=user, conversation_history=history)

    def check(self, args, tool="terminal"):
        return guard.guard(tool_name=tool, args=args, session_id="s")

    def test_invented_text_is_blocked(self):
        # Incident 19/10: "Vc não enviou" -> agent wrote its own message to Beltrana.
        self.turn("Vc não enviou")
        self.assertEqual(self.check(send("Olá Beltrana, segue o documento assinado."))["action"], "block")

    def test_dictated_text_passes(self):
        self.turn("manda pra Beltrana: Bom dia, chego às 15h!")
        self.assertIsNone(self.check(send("Bom dia, chego às 15h")))

    def test_proposed_text_passes_only_after_confirmation(self):
        proposal = 'Posso enviar à Beltrana: "Bom dia, tudo certo"?'
        self.turn("pode enviar", proposal)
        self.assertIsNone(self.check(send("Bom dia, tudo certo")))
        self.turn("e o documento?", proposal)
        self.assertEqual(self.check(send("Bom dia, tudo certo"))["action"], "block")

    def test_direct_api_and_module_bypass_are_blocked(self):
        self.turn("Bom dia")
        for command in (
            'curl -X POST http://127.0.0.1:8090/message/sendText/x -d "{}"',
            'python -c "import wa; wa.enviar(\'x\', \'Bom dia\')"',
        ):
            self.assertEqual(self.check({"command": command})["action"], "block")
        self.assertEqual(self.check({"code": "from wa import enviar"}, "execute_code")["action"], "block")

    def test_reading_commands_are_untouched(self):
        self.turn("Vc não enviou")
        self.assertIsNone(self.check({"command": f"{WA} mensagens --horas 24 --conversa Beltrana"}))
        self.assertIsNone(self.check({"command": f"{WA} status"}))

    def test_send_without_readable_text_is_blocked(self):
        self.turn("Beltrana")
        self.assertEqual(self.check({"command": f"{WA} enviar --para Beltrana"})["action"], "block")


if __name__ == "__main__":
    unittest.main()
