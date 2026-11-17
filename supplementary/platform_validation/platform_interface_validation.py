"""Optional schema checks."""
from pathlib import Path
import json
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
from pglast import parse_sql
import contextlib
import io
import traceback
import sys

def main():
    stream = io.StringIO()
    success = True
    with contextlib.redirect_stdout(stream):
        try:
            root=Path(__file__).resolve().parents[1]/'platform_interfaces'
            schema=json.loads((root/'notification.schema.json').read_text())
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema,format_checker=FormatChecker()).validate(json.loads((root/'notification.sample.json').read_text()))
            print('JSON Schema and example: PASS')
            validate(yaml.safe_load((root/'api.yaml').read_text()),base_uri=(root/'api.yaml').as_uri())
            print('OpenAPI 3.1: PASS')
            print('PostgreSQL statements parsed:',len(parse_sql((root/'db.schema.sql').read_text())))
            print('SQL parsing is not database execution or an integration test.')
        except Exception:
            success = False
            traceback.print_exc(file=stream)
    report = stream.getvalue()
    Path(__file__).resolve().with_name('platform_interface_validation_output.txt').write_text(report, encoding='utf-8')
    print(report, end='')
    return 0 if success else 1

if __name__ == '__main__':
    sys.exit(main())
