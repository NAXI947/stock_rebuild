"""Verify a GitHub build run and its immutable image manifest for manual release."""

import argparse
import json
from pathlib import Path
import re


BUILD_PATH = ".github/workflows/verify-build.yml"
IMAGE_FIELDS = {"BACKEND_IMAGE": "backend", "WEB_IMAGE": "web"}
MANIFEST_FIELDS = ("BUILD_RUN_ID", "BUILD_COMMIT", *IMAGE_FIELDS)


def verify_run(run, repository, run_id):
    origin = run.get("repository") or {}
    head_repository = run.get("head_repository") or {}
    path, _, ref = (run.get("path") or "").partition("@")
    repository = repository.lower()
    if (
        not re.fullmatch(r"[1-9][0-9]*", run_id)
        or str(run.get("id")) != run_id
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("event") != "push"
        or run.get("head_branch") != "main"
        or path != BUILD_PATH
        or ref not in ("", "main", "refs/heads/main")
        or origin.get("full_name", "").lower() != repository
        or head_repository.get("full_name", "").lower() != repository
        or not re.fullmatch(r"[a-f0-9]{40}", run.get("head_sha") or "")
        or not re.fullmatch(r"[1-9][0-9]*", str(run.get("run_attempt")))
    ):
        raise ValueError("Selected run must be a successful main push build from this repository")


def verify_manifest(manifest, run, repository):
    values = {}
    for line in manifest.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in MANIFEST_FIELDS or key in values:
            raise ValueError("Invalid or duplicate release manifest field")
        values[key] = value

    if values.keys() != set(MANIFEST_FIELDS):
        raise ValueError("Release manifest fields are incomplete")
    if values["BUILD_RUN_ID"] != str(run["id"]) or values["BUILD_COMMIT"] != run["head_sha"]:
        raise ValueError("Release manifest does not match the selected build")

    owner, _ = repository.lower().split("/", 1)
    for key, component in IMAGE_FIELDS.items():
        prefix = re.escape(f"ghcr.io/{owner}/stock-v4-{component}@sha256:")
        if not re.fullmatch(prefix + r"[a-f0-9]{64}", values[key]):
            raise ValueError(f"Invalid image digest in {key}")

    return "\n".join(f"{key}={values[key]}" for key in MANIFEST_FIELDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_run", type=Path)
    parser.add_argument("repository")
    parser.add_argument("run_id")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()

    try:
        run = json.loads(args.build_run.read_text(encoding="utf-8"))
        verify_run(run, args.repository, args.run_id)
        if args.manifest is None:
            print(run["run_attempt"])
        else:
            manifest = args.manifest.read_text(encoding="utf-8")
            print(verify_manifest(manifest, run, args.repository))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
