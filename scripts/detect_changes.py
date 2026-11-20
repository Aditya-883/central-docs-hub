import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def compute_sha256(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_previous_hashes(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logging.warning("Could not read state file %s: %s. Starting fresh.", state_file, exc)
        return {}


def scan_json_files(inbox_dir: Path) -> dict:
    hashes = {}
    if not inbox_dir.exists():
        logging.warning("Inbox directory not found: %s", inbox_dir)
        return hashes

    for file_path in sorted(inbox_dir.rglob("*.json")):
        rel_path = file_path.relative_to(inbox_dir).as_posix()
        hashes[rel_path] = compute_sha256(file_path)
    return hashes


def write_github_output(has_changes: bool, changed_files: list[str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    try:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(f"has_changes={'true' if has_changes else 'false'}\n")
            f.write(f"changed_files={json.dumps(changed_files)}\n")
    except OSError as exc:
        logging.error("Failed to write GITHUB_OUTPUT: %s", exc)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portal", required=True)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()

    inbox_dir = Path("data/inbox") / args.portal
    state_file = Path("data/state") / f"{args.portal}-hashes.json"

    old_hashes = load_previous_hashes(state_file)
    new_hashes = scan_json_files(inbox_dir)

    changed_files = [
        path for path, digest in new_hashes.items()
        if old_hashes.get(path) != digest
    ]
    removed_files = [path for path in old_hashes if path not in new_hashes]

    has_changes = bool(changed_files or removed_files)

    if args.update:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(new_hashes, indent=2, sort_keys=True), encoding="utf-8")
        logging.info("Updated hash state: %s", state_file)
        return

    logging.info("Portal: %s | Changed JSONs: %d | Removed JSONs: %d", args.portal, len(changed_files), len(removed_files))
    for f in changed_files:
        logging.info("  Changed: %s", f)

    write_github_output(has_changes, changed_files)


if __name__ == "__main__":
    main()
