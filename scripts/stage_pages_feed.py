"""Stage a GitHub Pages artifact from explicitly referenced public JSON."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import tempfile

from jsonschema import Draft202012Validator, FormatChecker, ValidationError


def read_json(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Missing or unsafe JSON file: {path.name}")
    raw = path.read_bytes()

    def unique(items):
        result = dict(items)
        if len(result) != len(items):
            raise ValueError("Duplicate JSON key")
        return result

    def nonfinite(value):
        raise ValueError(f"Invalid JSON number: {value}")

    return raw, json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)


def validator(schema):
    checker = FormatChecker()

    @checker.checks("date-time", raises=ValueError)
    def timestamp(value):
        if not isinstance(value, str):
            return True
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", value):
            return False
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True

    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=checker)


def release(manifest_path, schema, check):
    raw, manifest = read_json(manifest_path)
    check.validate(manifest)
    if manifest["kind"] != "manifest":
        raise ValueError("Expected a feed manifest")
    prefix = f"releases/{manifest['release_id']}"
    root = manifest_path.parent

    def get(relative, expected):
        if relative != expected:
            raise ValueError("Manifest path does not match its release and date")
        path = root / relative
        if any(part.is_symlink() for part in (*path.parents, path) if part != root.parent):
            raise ValueError("Publication references must not use symbolic links")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Publication reference escapes the input directory")
        return read_json(path)

    schema_path = manifest["schema_path"]
    schema_raw, supplied_schema = get(schema_path, f"{prefix}/schema.json")
    if supplied_schema != schema:
        raise ValueError("Feed schema differs from trusted publisher schema")
    assets = {schema_path: schema_raw}
    for kind in ("games", "history", "reports"):
        days = set()
        for item in manifest[kind]:
            day, relative = item["day_et"], item["path"]
            if day in days:
                raise ValueError("Duplicate date in manifest index")
            days.add(day)
            content, doc = get(relative, f"{prefix}/{kind}/{day}.json")
            check.validate(doc)
            if (doc["kind"], doc["day_et"], doc["release_id"], doc["season"], doc["exported_at"]) != (
                    kind, day, manifest["release_id"], manifest["season"], manifest["exported_at"]):
                raise ValueError("Shard identity differs from manifest")
            if kind == "reports" and doc["report"]["report_date_et"] != day:
                raise ValueError("Report date differs from manifest")
            count = doc["report"]["daily"]["graded_games"] if kind == "reports" else len(doc[kind])
            if count != item["count"]:
                raise ValueError("Shard count differs from manifest")
            assets[relative] = content
    return raw, manifest, assets


def stage(source, output, schema_path, previous=None, transport=False, clean_input=False):
    if source.is_symlink():
        raise ValueError("Input directory must not be a symbolic link")
    source, output = source.resolve(), output.resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use a new output directory separate from the input")
    _, schema = read_json(schema_path)
    check = validator(schema)
    raw, current, assets = release(source / "manifest.json", schema, check)
    previous_raw = None
    if previous:
        previous_raw, old, retained = release(previous, schema, check)
        for path, content in retained.items():
            if path in assets and content != assets[path]:
                raise ValueError("Immutable release path has conflicting content")
            assets.setdefault(path, content)
        if old["release_id"] == current["release_id"]:
            if previous_raw != raw:
                raise ValueError("A release ID cannot have two different manifests")
            # Keep the existing predecessor when retrying the same handoff.
            predecessor = previous.parent / "previous-manifest.json"
            if transport and predecessor.is_file() and predecessor != previous:
                return stage(source, output, schema_path, predecessor, transport, clean_input)
            previous_raw = None
    if clean_input:
        expected = {"manifest.json", *assets}
        if previous and previous.parent.resolve() == source:
            expected.add(previous.name)
        actual = {p.relative_to(source).as_posix() for p in source.rglob("*")
                  if p.is_file() or p.is_symlink()}
        if actual != expected:
            raise ValueError("Publisher input contains files outside the manifest allowlist")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".feed-stage-", dir=output.parent) as temporary:
        staged = Path(temporary) / "feed"
        staged.mkdir()
        for relative, data in assets.items():
            path = staged / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if transport and previous_raw is not None:
            (staged / "previous-manifest.json").write_bytes(previous_raw)
        (staged / "manifest.json").write_bytes(raw)
        staged.rename(output)
    return len(assets) + 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--previous-manifest", type=Path)
    parser.add_argument("--transport", action="store_true")
    parser.add_argument("--clean-input", action="store_true")
    args = parser.parse_args()
    try:
        count = stage(args.source, args.output, args.schema, args.previous_manifest,
                      args.transport, args.clean_input)
    except ValidationError:
        parser.exit(1, "Feed staging failed: JSON does not match the trusted schema.\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"Feed staging failed: {error}\n")
    print(f"Staged {count} public JSON files.")


if __name__ == "__main__":
    main()
