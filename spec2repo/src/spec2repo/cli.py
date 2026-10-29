"""Offline contract validation; does not call models or execute generated code."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from spec2repo.domain.spec import CoreSpec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spec2repo")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-spec", help="Validate a structured specification")
    validate.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        spec = CoreSpec.model_validate_json(args.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            # Do not echo entire inputs (which may contain secrets) into error logs.
            errors = exc.errors(include_url=False, include_context=False, include_input=False)
        else:
            errors = [{"type": type(exc).__name__, "msg": "Cannot read specification as UTF-8"}]
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "spec_id": spec.id,
                "version": spec.version,
                "fingerprint": spec.fingerprint(),
                "ready_for_planning": spec.ready_for_planning,
                "blocking_question_ids": spec.blocking_question_ids,
                "requirements": len(spec.requirements),
                "acceptance_criteria": len(spec.acceptance_criteria),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if spec.ready_for_planning else 2
