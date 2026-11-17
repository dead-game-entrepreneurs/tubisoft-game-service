#!/usr/bin/env python3
"""Extract private 187 Ride or Die USA inputs from an owned PS2 disc image.

These are analysis inputs, not distributable assets. Outputs belong under work/.
The USA release boots SLUS-21116 directly; there is no GR2-style .SOZ unpacking.
"""
import argparse
import hashlib
from pathlib import Path
import struct
import subprocess


FILES = ("SYSTEM.CNF", "SLUS_211.16", "CONFPS2.PAR", "CONFPS2.STR",
         "SCEADNAS.BIN", "MODULES/DNAS280.IMG")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso", type=Path, help="Path to your 187 Ride or Die USA ISO")
    parser.add_argument("--output", type=Path, default=Path("work/187/disc"))
    args = parser.parse_args()
    if not args.iso.is_file():
        parser.error("Disc image does not exist")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    subprocess.run(["7z", "x", "-aos", f"-o{args.output.resolve()}",
                    str(args.iso.resolve()), *FILES], check=True)
    for name in FILES:
        path = args.output / name
        if not path.is_file():
            raise SystemExit(f"Missing expected disc file: {name}")
        path.chmod(0o600)
        print(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}")
    executable = (args.output / "SLUS_211.16").read_bytes()
    if (len(executable) < 52 or executable[:6] != b"\x7fELF\x01\x01"
            or struct.unpack_from("<H", executable, 18)[0] != 8
            or struct.unpack_from("<I", executable, 24)[0] != 0x100008):
        raise SystemExit("Unexpected USA executable format or entry point")
    if b"SLUS_211.16" not in (args.output / "SYSTEM.CNF").read_bytes():
        raise SystemExit("Unexpected USA boot configuration")
    print("USA ELF verified: import with Ghidra r5900:LE:32:default; entry 0x00100008.")


if __name__ == "__main__":
    main()
