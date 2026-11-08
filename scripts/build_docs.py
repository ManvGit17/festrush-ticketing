"""Build docs/api.md from the app's OpenAPI schema, and check that every local link resolves.

    uv run python scripts/build_docs.py          # rewrite docs/api.md
    uv run python scripts/build_docs.py --check  # fail if it is stale or a link is broken

Every repository link in the docs is rewritten to the address in docs/site.json, so renaming the
repository is a one-line change there plus one run of this script.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def markdown_files() -> list[Path]:
    """Tracked and new Markdown files; ignored working files are left out."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [ROOT / p for p in out]


sys.path.insert(0, str(ROOT))
os.environ.setdefault("FESTRUSH_SECRET_KEY", "docs-build-only-secret-key")


def render_api() -> str:
    from festrush.app import create_app
    from tests.conftest import make_settings

    schema = create_app(make_settings()).openapi()
    out = [
        "# API reference",
        "",
        "Generated from the OpenAPI schema by `scripts/build_docs.py`; do not edit by hand.",
        "Authenticate with `Authorization: Bearer <token>` from `POST /api/login`. Writes accept an",
        "`Idempotency-Key` header: send the same key to retry safely.",
        "",
    ]
    for path, ops in sorted(schema["paths"].items()):
        if not path.startswith("/api/"):
            continue
        for method, op in ops.items():
            out.append(f"## `{method.upper()} {path}`")
            out.append("")
            doc = (op.get("description") or op.get("summary") or "").strip().replace("\n", " ")
            if doc:
                out += [doc, ""]
            out.append("| Status | Meaning |")
            out.append("| :-- | :-- |")
            for status, resp in sorted(op["responses"].items()):
                out.append(f"| {status} | {resp.get('description', '')} |")
            out.append("")
    return "\n".join(out)


def broken_links() -> list[str]:
    bad = []
    files = markdown_files()
    for md in files:
        for target in re.findall(r"\]\(([^)\s]+)\)", md.read_text(encoding="utf-8")):
            if re.match(r"(https?:|mailto:|#)", target):
                continue
            if not (md.parent / target.split("#")[0]).exists():
                bad.append(f"{md.relative_to(ROOT)} -> {target}")
    return bad


def sync_repo_links(write: bool) -> list[str]:
    """Every repository link in the docs must equal docs/site.json; with write, fix them."""
    site = json.loads((ROOT / "docs" / "site.json").read_text(encoding="utf-8"))
    repo = site["repository"]
    owner = repo.rsplit("/", 2)[1]
    names = "|".join(re.escape(n) for n in site["names"])  # this repository, under any past name
    pattern = re.compile(rf"https://github\.com/{re.escape(owner)}/(?:{names})(?![\w.-])")
    stale = []
    for md in markdown_files():
        text = md.read_text(encoding="utf-8")
        fixed = pattern.sub(repo, text)
        if fixed != text:
            stale.append(f"{md.relative_to(ROOT)}: repository link differs from docs/site.json")
            if write:
                md.write_text(fixed, encoding="utf-8")
    return stale


def main() -> int:
    target = ROOT / "docs" / "api.md"
    text = render_api() + "\n"
    if "--check" in sys.argv:
        problems = broken_links() + sync_repo_links(write=False)
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            problems.append("docs/api.md is stale: run scripts/build_docs.py")
        print("\n".join(problems) or "docs ok")
        return 1 if problems else 0
    sync_repo_links(write=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
