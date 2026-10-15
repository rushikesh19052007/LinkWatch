
from __future__ import annotations

import ast
import json
import re
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from pathlib import Path


IGNORED_DIRS = {
    ".git", ".venv", "venv", "__pycache__python .\depguard\scanner.py .",
    "node_modules", "dist", "build",
}


def normalize(name: str) -> str:
    """Normalize a Python distribution name for comparison."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement(line: str) -> tuple[str, str] | None:
    """Extract a package name and its version constraint."""
    line = line.strip()

    if not line or line.startswith("#") or line.startswith("-"):
        return None

    match = re.match(
        r"^([A-Za-z0-9][A-Za-z0-9._-]*)"
        r"\s*(.*)$",
        line,
    )
    if not match:
        return None

    return normalize(match.group(1)), match.group(2).strip()


def read_dependencies(root: Path) -> dict[str, list[dict]]:
    """Read dependency declarations from supported manifests."""
    declarations: dict[str, list[dict]] = {}

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data.get("project", {})

        entries = list(project.get("dependencies", []))
        optional = project.get("optional-dependencies", {})
        for group in optional.values():
            entries.extend(group)

        for entry in entries:
            parsed = parse_requirement(entry)
            if parsed:
                name, constraint = parsed
                declarations.setdefault(name, []).append({
                    "file": "pyproject.toml",
                    "constraint": constraint or "(unspecified)",
                    "raw": entry,
                })

    requirements = root / "requirements.txt"
    if requirements.exists():
        for line_number, line in enumerate(
            requirements.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            parsed = parse_requirement(line)
            if parsed:
                name, constraint = parsed
                declarations.setdefault(name, []).append({
                    "file": "requirements.txt",
                    "line": line_number,
                    "constraint": constraint or "(unspecified)",
                    "raw": line.strip(),
                })

    return declarations


def scan_imports(root: Path) -> dict[str, list[dict]]:
    """Map top-level Python imports to their source locations."""
    usages: dict[str, list[dict]] = {}

    for path in root.rglob("*.py"):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue

        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            usages.setdefault("_scan_errors", []).append({
                "file": str(path.relative_to(root)),
                "error": str(exc),
            })
            continue

        for node in ast.walk(tree):
            module = None

            if isinstance(node, ast.Import):
                for alias in node.names:
                    usages.setdefault(alias.name.split(".")[0], []).append({
                        "file": str(path.relative_to(root)),
                        "line": node.lineno,
                        "module": alias.name,
                    })

            elif isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
                usages.setdefault(module.split(".")[0], []).append({
                    "file": str(path.relative_to(root)),
                    "line": node.lineno,
                    "module": module,
                })

    return usages


def analyze(root: Path) -> dict:
    declarations = read_dependencies(root)
    imports = scan_imports(root)

    conflicts = []
    for package, entries in sorted(declarations.items()):
        if len(entries) > 1:
            constraints = {
                entry["constraint"] for entry in entries
            }
            conflicts.append({
                "package": package,
                "declarations": entries,
                "different_constraints": len(constraints) > 1,
                "risk": (
                    "Review differing constraints"
                    if len(constraints) > 1
                    else "Repeated declaration"
                ),
            })

    return {
        "repository": str(root.resolve()),
        "manifests_found": [
            name for name in ("pyproject.toml", "requirements.txt")
            if (root / name).exists()
        ],
        "dependencies": declarations,
        "declaration_conflicts": conflicts,
        "import_usages": imports,
        "limitations": [
            "Import names do not always match distribution names.",
            "Dynamic imports may not be detected.",
            "Declarations alone do not prove runtime incompatibility.",
            "This scanner does not execute or install replacement packages.",
        ],
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scan dependency declarations and Python imports."
    )
    parser.add_argument(
        "repository",
        nargs="?",
        default=".",
        help="Path to the repository to analyze.",
    )
    parser.add_argument(
        "--output",
        default="evidence/dependency-scan.json",
        help="Path for the JSON report.",
    )
    args = parser.parse_args()

    root = Path(args.repository).resolve()
    report = analyze(root)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    print(f"Manifests: {len(report['manifests_found'])}")
    print(f"Dependencies: {len(report['dependencies'])}")
    print(f"Repeated declarations: {len(report['declaration_conflicts'])}")
    print(f"Report saved to: {output.resolve()}")
