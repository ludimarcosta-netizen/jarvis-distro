#!/usr/bin/env python3
"""
oficios.py - Ferramenta CLI de alta velocidade para consulta e registro de ofícios.
Arquivo de dados: __JARVIS_USERPROFILE_FS__/Documents/JARVIS_Records/oficios/oficios.json
"""

import sys
import json
from pathlib import Path

DB_PATH = Path("__JARVIS_USERPROFILE_FS__/Documents/JARVIS_Records/oficios/oficios.json")

def load_db():
    if not DB_PATH.exists():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"next_number": 1, "records": []}
        save_db(data)
        return data
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(data):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def proximo():
    data = load_db()
    print(data.get("next_number", 1))

def registrar(assunto, numero=None):
    data = load_db()
    if numero is None:
        numero = data.get("next_number", 1)
        data["next_number"] = numero + 1
    else:
        numero = int(numero)
        if numero >= data.get("next_number", 1):
            data["next_number"] = numero + 1
    
    # Adiciona registro
    record = {"number": numero, "subject": assunto}
    data["records"].append(record)
    save_db(data)
    print(f"Ofício nº {numero} registrado com sucesso: {assunto}")

def listar(limite=5):
    data = load_db()
    records = data.get("records", [])
    print(f"Próximo número disponível: {data.get('next_number', 1)}\n")
    print(f"Últimos {min(limite, len(records))} registros:")
    for r in records[-limite:]:
        print(f"  - Nº {r['number']}: {r['subject']}")

def main():
    if len(sys.argv) < 2:
        print("Uso: python oficios.py [proximo | registrar <assunto> [numero] | listar]")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "proximo":
        proximo()
    elif cmd == "registrar":
        if len(sys.argv) < 3:
            print("Erro: informe o assunto para registrar.")
            sys.exit(1)
        assunto = sys.argv[2]
        numero = sys.argv[3] if len(sys.argv) > 3 else None
        registrar(assunto, numero)
    elif cmd == "listar":
        limite = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        listar(limite)
    else:
        print(f"Comando desconhecido: {cmd}")
        sys.exit(1)

if __name__ == "__main__":
    main()
