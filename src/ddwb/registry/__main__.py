"""uv run python -m ddwb.registry "Summit County, OH"            ranked candidates
   uv run python -m ddwb.registry Springfield --state IL --kind township
   uv run python -m ddwb.registry --id nces_leaid 0630660         by an identifier (scheme '-' for any)
   uv run python -m ddwb.registry --host pylusd.org               by a website host
   uv run python -m ddwb.registry --card bf62a1a7cef59            a body's card
   uv run python -m ddwb.registry --export OUT_DIR                write a release folder
Add --json for JSON, --dir to read another registry folder (default: DDWB_REGISTRY_DIR, else data/tables).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import export_release, format_result, registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ddwb.registry", description="Find a public body in the registry.")
    parser.add_argument("query", nargs="*", help="free text: 'Summit County, OH', 'City of Cortez Colorado', 'Springfield IL'")
    parser.add_argument("--name")
    parser.add_argument("--state")
    parser.add_argument("--kind", help="county, city, town, township, village, school district, library, fire, water, transit, ...")
    parser.add_argument("--county")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--id", nargs=2, metavar=("SCHEME", "VALUE"))
    parser.add_argument("--host")
    parser.add_argument("--card", metavar="BODY_ID")
    parser.add_argument("--export", metavar="OUT_DIR")
    parser.add_argument("--dir", help="registry folder to read")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.export:
        manifest = export_release(args.export, source=args.dir)
        print(json.dumps(manifest, indent=2) if args.json else
              f"wrote {len(manifest['files'])} files, {manifest['total_bytes'] / 1e6:.1f} MB, to {args.export}; "
              f"people check: {manifest['people_check']['result']}")
        return 0

    started = time.time()
    reg = registry(args.dir)
    reg.index()
    loaded = time.time() - started
    if args.id:
        result = {"query": f"id {args.id[0]}={args.id[1]}",
                  "candidates": reg.by_id(None if args.id[0] in ("-", "*", "any") else args.id[0], args.id[1])}
    elif args.host:
        result = {"query": f"host {args.host}", "candidates": reg.by_host(args.host)}
    elif args.card:
        result = reg.body_card(args.card)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result else 1
    else:
        text = " ".join(args.query) or None
        if not text and not args.name:
            parser.error("give a query, --name, --id, --host, --card or --export")
        result = reg.resolve(text, name=args.name, state=args.state, kind=args.kind, county=args.county, limit=args.limit)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_result(result))
        print(f"(registry loaded in {loaded:.2f} s from {reg.root})", file=sys.stderr)
    return 0 if result.get("candidates") else 1


if __name__ == "__main__":
    sys.exit(main())
