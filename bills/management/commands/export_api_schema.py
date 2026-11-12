"""Keep the offline API schema generated from the live route definitions."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from bills.api.router import api


class Command(BaseCommand):
    help = "Export OpenAPI, or verify that its checked-in artifact is current."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--output", default="docs/api/openapi.json")

    def handle(self, *args, **options):
        content = json.dumps(api.get_openapi_schema(), indent=2, ensure_ascii=False) + "\n"
        target = Path(options["output"])
        if options["check"]:
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                raise CommandError("API schema artifact differs; run export_api_schema.")
            self.stdout.write("API schema artifact matches the live contract.")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self.stdout.write(f"Exported {target}.")
