import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["JARVIS_CEREBRO_DIR"] = ""

import face_data
import storage


class FaceDataTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(tmp.name, "t.db")

    def add(self, text, **kw):
        return storage.add_pendencia(text, db_path=self.db, **kw)

    def test_lists_only_open_with_trimmed_fields(self):
        keep = self.add("Publicar edital", prazo="2026-10-10")
        done = self.add("Já feita")
        storage.concluir_pendencia(done, db_path=self.db)
        rows = face_data.list_open_pendencias(self.db)
        self.assertEqual([r["id"] for r in rows], [keep])
        self.assertEqual(set(rows[0]), {"id", "descricao", "responsavel", "prazo", "created_at", "ata_titulo"})

    def test_finish_is_idempotent_and_rejects_unknown(self):
        pid = self.add("Ligar para a Câmara")
        self.assertTrue(face_data.finish_pendencia(pid, self.db))
        self.assertFalse(face_data.finish_pendencia(pid, self.db))
        self.assertFalse(face_data.finish_pendencia(9999, self.db))
        self.assertEqual(face_data.list_open_pendencias(self.db), [])

    def test_system_checks_reflect_probe_and_open_count(self):
        self.add("A")
        checks = {c["id"]: c for c in face_data.system_checks(self.db, probe=lambda port: False, home=Path(self.db).parent)}
        self.assertEqual(checks["web"]["status"], "off")
        self.assertEqual(checks["cerebro"]["status"], "off")
        self.assertEqual((checks["pendencias"]["status"], checks["pendencias"]["detail"]), ("warn", "1"))
        up = {c["id"]: c for c in face_data.system_checks(self.db, probe=lambda port: True, home=Path(self.db).parent)}
        self.assertEqual(up["web"]["status"], "warn")

    def test_capabilities_distinguish_available_pending_and_unavailable(self):
        with tempfile.TemporaryDirectory() as home:
            down = {c["id"]: c for c in face_data.capability_checks(lambda port: False, Path(home))}
            reader = Path(home) / "runtime/pdf-signing/Scripts/python.exe"
            reader.parent.mkdir(parents=True)
            reader.write_bytes(b"")
            token = Path(home) / "jarvis/database/.spotify_token.json"
            token.parent.mkdir(parents=True)
            token.write_text("{}", encoding="utf-8")
            face_data.integration_checks.cache_clear()  # Nova instalação simulada; não reutilizar a verificação anterior.
            up = {c["id"]: c for c in face_data.capability_checks(lambda port: port == face_data.EVOLUTION_PORT, Path(home))}
        self.assertEqual((down["whatsapp"]["status"], down["pdf"]["status"], down["spotify-faixa"]["status"]), ("off", "off", "off"))
        self.assertEqual((up["whatsapp"]["status"], up["pdf"]["status"], up["spotify-faixa"]["status"]), ("warn", "warn", "warn"))
        self.assertEqual(up["acoes-locais"]["status"], "off")
        self.assertTrue(all(set(c) == {"id", "label", "status", "detail"} for c in up.values()))

    def test_unreadable_db_degrades_to_off(self):
        with tempfile.TemporaryDirectory() as folder:  # uma pasta no lugar do arquivo: o SQLite não abre
            checks = {c["id"]: c for c in face_data.system_checks(folder, probe=lambda port: True, home=Path(self.db).parent)}
        self.assertEqual(checks["pendencias"]["status"], "off")


if __name__ == "__main__":
    unittest.main()
