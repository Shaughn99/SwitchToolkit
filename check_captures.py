"""
check_captures.py - audit collected show tech-support files.

A STIG scanner rejects a capture over a single absent section, and a
truncated capture still looks like valid output, so the fastest way to
tell a bad file from a bad scan is to look at which sections the file
actually contains.

    python check_captures.py <folder> [--required "show inventory,show version"]

Exits 0 when every file has all required sections, 1 otherwise, so it
can gate a scan in a script.
"""

import argparse
import os
import sys

from upgrade_engine import (TECH_REQUIRED_SECTIONS, missing_tech_sections,
                            tech_support_sections)


def audit(path, required):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return {
        "path": path,
        "bytes": len(text),
        "sections": len(tech_support_sections(text)),
        "missing": missing_tech_sections(text, required),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", help="folder of captures (searched recursively)")
    parser.add_argument("--required", default=",".join(TECH_REQUIRED_SECTIONS),
                        help="comma separated section names to require")
    args = parser.parse_args()

    required = tuple(s.strip() for s in args.required.split(",") if s.strip())

    files = []
    for dirpath, _dirs, names in os.walk(args.folder):
        files += [os.path.join(dirpath, n) for n in sorted(names) if n.endswith(".txt")]

    if not files:
        print(f"No .txt captures found under {args.folder}")
        return 1

    print(f"Checking {len(files)} capture(s) for: {', '.join(required)}\n")

    incomplete = []
    for path in files:
        try:
            r = audit(path, required)
        except OSError as e:
            print(f"  [unreadable] {path}: {e}")
            incomplete.append(path)
            continue

        name = os.path.relpath(path, args.folder)
        if r["missing"]:
            incomplete.append(path)
            print(f"  INCOMPLETE  {name}")
            print(f"              {r['bytes']:,} bytes, {r['sections']} sections, "
                  f"missing: {', '.join(r['missing'])}")
        else:
            print(f"  ok          {name}  ({r['bytes']:,} bytes, {r['sections']} sections)")

    print()
    if incomplete:
        print(f"{len(incomplete)} of {len(files)} capture(s) are incomplete and need "
              f"re-collecting:")
        for path in incomplete:
            print(f"  {os.path.relpath(path, args.folder)}")
        return 1

    print(f"All {len(files)} capture(s) contain every required section.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
