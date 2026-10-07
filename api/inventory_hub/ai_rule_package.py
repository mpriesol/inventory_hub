"""Explicit offline rule-pack validation/export; never contacts Hub or Upgates."""
import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from inventory_hub.services.ai_rule_package import PackageError, compile_draft, export_package, load_packages, safe_validation_error, validate_packages


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "export", "draft"))
    parser.add_argument("--package-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Private output file; draft/export require an explicit path")
    parser.add_argument("--current-book", type=Path, help="Current RuleBook JSON to preserve unrelated scopes")
    parser.add_argument("--shop", default="biketrek")
    parser.add_argument("--supplier", default="")
    parser.add_argument("--brand", default="")
    parser.add_argument("--profile", default="general")
    parser.add_argument("--product", default="", help="Exact runtime parent product code; required with confirmed knowledge")
    parser.add_argument("--confirmed-knowledge", action="append", default=[], help="Knowledge ID justified by confirmed product facts; repeat as needed")
    args = parser.parse_args(argv)
    if args.action != "validate" and not args.output:
        parser.error("--output is required; no private source text is printed implicitly")
    try:
        packages = load_packages(args.package_dir)
        if args.action == "validate":
            result = validate_packages(packages)
        elif args.action == "export":
            result = export_package(packages)
        else:
            current = json.loads(args.current_book.read_text()) if args.current_book else None
            result = compile_draft(packages, shop=args.shop, supplier=args.supplier, brand=args.brand,
                                   profile=args.profile, product=args.product, knowledge_ids=args.confirmed_knowledge, current_book=current)
        data = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            # Avoid replacing source files or another unreviewed draft by mistake.
            with args.output.open("x", encoding="utf-8") as output:
                output.write(data)
            print(json.dumps({"status": "written", "published": False, "output": str(args.output)}))
        else:
            print(data, end="")
        return 0
    except ValidationError as error:
        print(json.dumps({"error": "package_schema", "message": safe_validation_error(error)}), file=sys.stderr)
        return 2
    except (PackageError, OSError, ValueError) as error:
        print(json.dumps({"error": getattr(error, "code", "package_io_error"), "message": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
