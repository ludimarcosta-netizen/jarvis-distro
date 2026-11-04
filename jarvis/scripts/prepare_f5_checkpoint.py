"""Create an FP16 inference checkpoint without loading PyTorch or all weights into RAM."""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import numpy as np


def convert(source: Path, target: Path) -> None:
    if source.resolve() == target.resolve():
        raise ValueError("Preserve o checkpoint original e escolha outro destino")
    with source.open("rb") as original:
        size = struct.unpack("<Q", original.read(8))[0]
        header = json.loads(original.read(size))
        tensors = sorted(((key, value) for key, value in header.items() if key != "__metadata__"),
                         key=lambda item: item[1]["data_offsets"][0])
        result = {"__metadata__": header["__metadata__"]} if "__metadata__" in header else {}
        offset = 0
        for key, value in tensors:
            start, end = value["data_offsets"]
            length = (end - start) // 2 if value["dtype"] == "F32" else end - start
            result[key] = {**value, "dtype": "F16" if value["dtype"] == "F32" else value["dtype"],
                           "data_offsets": [offset, offset + length]}
            offset += length
        encoded = json.dumps(result, separators=(",", ":")).encode()
        encoded += b" " * (-len(encoded) % 8)
        temporary = target.with_suffix(".tmp")
        with temporary.open("wb") as output:
            output.write(struct.pack("<Q", len(encoded)))
            output.write(encoded)
            for _, value in tensors:
                start, end = value["data_offsets"]
                original.seek(8 + size + start)
                data = original.read(end - start)
                if value["dtype"] == "F32":
                    data = np.frombuffer(data, dtype="<f4").astype("<f2").tobytes()
                output.write(data)
        temporary.replace(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    convert(args.source, args.target)
    print(json.dumps({"checkpoint": str(args.target), "bytes": args.target.stat().st_size}))
