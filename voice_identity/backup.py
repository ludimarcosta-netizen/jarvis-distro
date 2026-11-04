"""Consistent encrypted profile backup, excluding temporary conversation samples."""
import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent / "data" / "voices.sqlite3"
    if not source.exists():
        parser.error("Banco ainda não criado")
    if args.destination.exists():
        parser.error("Escolha um arquivo novo; backups existentes não são sobrescritos")
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(args.destination)) as dst:
        src.backup(dst)
        dst.execute("PRAGMA secure_delete=ON")
        dst.execute("DELETE FROM candidates")
        dst.commit()
        dst.execute("VACUUM")
    print("Backup criptografado de perfis concluído; amostras temporárias excluídas.")


if __name__ == "__main__":
    main()
